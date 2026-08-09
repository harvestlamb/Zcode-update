"""FastAPI app: exposes /api/ask consumed by the injected chat widget.

Start-up builds the retrieval index from /data/content/*.md. The index is
rebuilt on demand via POST /api/reindex (called by import.sh after an upgrade).

The /api/ask flow:
  1. retrieve top-K relevant chunks (BM25 by default, dense if configured)
  2. build a grounded prompt with the snippets as context
  3. call the configured LLM (OpenAI-compatible; optional model_id)
  4. return {answer, sources}

Run: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import admin_auth
import ask_log
import config_store
import importer
import skills_store
import upgrade_log
from llm import LLMClient
from rag import Retriever, CONTENT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("zcode-ai")

app = FastAPI(title="ZCode Mirror AI", version="1.4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

retriever = Retriever(os.environ.get("CONTENT_DIR", CONTENT_DIR))
llm = LLMClient()
_index_lock = threading.Lock()
skills_store.ensure_dir()


SYSTEM_PROMPT = (
    "你是「ZCode 文档助手」，专门根据提供的 ZCode 官方文档片段回答用户关于 ZCode 使用的问题。"
    "请遵守以下规则：\n"
    "1. 只依据下方「参考资料」回答；若资料中没有相关内容，请明确说明「文档中暂无相关信息」，不要编造。\n"
    "2. 回答简洁、准确，使用中文，可适当分点。\n"
    "3. 涉及操作步骤时给出清晰步骤；可使用 Markdown（代码块、加粗、列表）。\n"
    "4. 不要提及「系统提示」「参考资料」等元信息，自然回答即可。\n"
)

SKILL_TRANSLATE_SYSTEM = (
    "你是技术文档译员，负责把 Agent Skill 的英文简介译成简洁中文。"
    "只输出译好的中文简介本身，不要加标题、引号或前后缀。"
)


def _translate_skill_intro(skill: dict, *, force: bool = False) -> dict:
    """Translate skill description to a short Chinese intro via the active LLM.

    Soft-fails: upload/create still succeeds if translation is skipped or errors.
    Returns {"ok": bool, "skipped"?: bool, "error"?: str, "description_zh"?: str}.
    When force=True, overwrite an existing Chinese intro.
    """
    dirname = skill.get("dirname") or skill.get("name") or ""
    desc = (skill.get("description") or "").strip()
    existing_zh = (skill.get("description_zh") or "").strip()
    if existing_zh and not force:
        return {"ok": True, "skipped": True, "reason": "already_translated", "description_zh": existing_zh}
    if not desc:
        return {"ok": True, "skipped": True, "reason": "empty_description"}
    if skills_store.looks_chinese(desc):
        # Already Chinese — store as the Chinese intro for consistent UI.
        try:
            updated = skills_store.set_description_zh(dirname, desc)
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_chinese",
                "description_zh": updated.get("description_zh") or desc,
            }
        except (FileNotFoundError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    display = skill.get("name") or dirname
    user = (
        f"技能名：{display}\n"
        f"目录名：{dirname}\n\n"
        f"请将下面的简介译成 1～2 句简洁中文，说明它做什么、适合什么场景。"
        f"不要添加原文没有的信息。\n\n"
        f"原文：\n{desc}"
    )
    try:
        result = llm.chat(SKILL_TRANSLATE_SYSTEM, user)
        zh = (result.get("content") or "").strip().strip("「」\"'")
        if not zh:
            return {"ok": False, "error": "模型返回空译文"}
        # Keep intro short for card UI
        if len(zh) > 280:
            zh = zh[:277].rstrip() + "…"
        updated = skills_store.set_description_zh(dirname, zh)
        return {
            "ok": True,
            "skipped": False,
            "description_zh": updated.get("description_zh") or zh,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("skill intro translation failed for %s: %s", dirname, exc)
        return {"ok": False, "error": str(exc)}


def _build_user_prompt(question: str, contexts) -> str:
    refs = "\n\n".join(
        f"【资料{i+1}】(来源: {c.title})\n{c.text}" for i, c in enumerate(contexts)
    )
    return (
        f"参考资料：\n{refs}\n\n"
        f"用户问题：{question}\n\n"
        f"请基于上述参考资料回答。"
    )


class AskResponse(BaseModel):
    answer: str
    sources: list
    model: Optional[str] = None
    model_id: Optional[str] = None
    backend: Optional[str] = None
    usage: Optional[dict] = None


@app.get("/api/health")
def health():
    active = config_store.get_model()
    return {
        "status": "ok",
        "retriever": retriever.stats(),
        "llm": {
            "base_url": llm.base_url,
            "model": llm.model,
            "label": llm.label,
            "model_id": llm.model_id,
            "profiles": len(config_store.load_llm_store()["models"]),
        },
        "active": {
            "id": (active or {}).get("id"),
            "label": (active or {}).get("label"),
            "model": (active or {}).get("model"),
        } if active else None,
    }


@app.get("/api/models")
def public_models():
    """Chat-widget facing list (no secrets)."""
    models = llm.public_models()
    return {
        "active_id": next((m["id"] for m in models if m.get("active")), ""),
        "models": models,
    }


def _log_ask(request: Request, *, question: str, cfg: dict, answer: str,
             ok: bool, error: str = "", sources: Optional[list] = None,
             backend: str = "", usage: Optional[dict] = None) -> None:
    meta = ask_log.client_meta(request)
    titles = [s.get("title") for s in (sources or []) if isinstance(s, dict) and s.get("title")]
    ask_log.append(
        ip=meta["ip"],
        user_agent=meta["user_agent"],
        question=question,
        model_id=cfg.get("id") or "",
        model=cfg.get("model") or "",
        label=cfg.get("label") or "",
        answer=answer,
        ok=ok,
        error=error,
        sources=titles,
        backend=backend,
        usage=usage,
    )


@app.post("/api/ask", response_model=AskResponse)
def ask(
    request: Request,
    question: Optional[str] = Form(default=""),
    model_id: Optional[str] = Form(default=None),
):
    q = (question or "").strip()
    mid = (model_id or "").strip() or None
    try:
        cfg = llm.resolve(mid)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    backend = retriever.stats()["backend"]
    if not q:
        return AskResponse(
            answer="请输入问题。", sources=[],
            model=cfg["model"], model_id=cfg.get("id") or None,
            backend=backend,
        )

    with _index_lock:
        hits = retriever.search(q, top_k=5)
    contexts = [chunk for chunk, _score in hits]
    sources = [c.source_dict() for c in contexts]

    if not contexts:
        answer = "抱歉，文档中暂无与该问题相关的内容。请尝试换个关键词，或查阅左侧文档目录。"
        _log_ask(request, question=q, cfg=cfg, answer=answer, ok=True,
                 sources=[], backend=backend)
        return AskResponse(
            answer=answer, sources=[],
            model=cfg["model"], model_id=cfg.get("id") or None, backend=backend,
        )

    prompt = _build_user_prompt(q, contexts)
    try:
        result = llm.chat(SYSTEM_PROMPT, prompt, model_id=mid)
        answer = result["content"]
        usage = result.get("usage") or {}
        _log_ask(request, question=q, cfg=cfg, answer=answer, ok=True,
                 sources=sources, backend=backend, usage=usage)
    except Exception as exc:  # noqa: BLE001
        log.error("LLM call failed: %s", exc)
        hint = "\n\n".join(f"- {c.title}：{c.text[:80].strip()}…" for c in contexts[:3])
        answer = f"检索到了相关文档，但大模型暂不可用（{exc}）。以下是相关片段：\n\n{hint}"
        _log_ask(request, question=q, cfg=cfg, answer=answer, ok=False,
                 error=str(exc), sources=sources, backend=backend,
                 usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        return AskResponse(
            answer=answer, sources=sources,
            model=cfg["model"], model_id=cfg.get("id") or None, backend=backend,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
    return AskResponse(
        answer=answer, sources=sources,
        model=cfg["model"], model_id=cfg.get("id") or None, backend=backend,
        usage=usage,
    )


@app.post("/api/reindex")
def reindex():
    """Rebuild the retrieval index. Called by import.sh after an upgrade."""
    with _index_lock:
        retriever.rebuild()
    return retriever.stats()


# ===========================================================================
# Public skill library (read-only)
# ===========================================================================

@app.get("/api/skills")
def public_list_skills():
    skills_store.ensure_dir()
    items = skills_store.list_skills()
    return {"skills": items, "count": len(items)}


@app.get("/api/skills/{name}")
def public_get_skill(name: str):
    try:
        return skills_store.get_skill(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/skills/{name}/download")
def public_download_skill(name: str):
    try:
        data = skills_store.pack_zip(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe = name.strip().lower()
    return RawResponse(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.zip"',
        },
    )


# ===========================================================================
# Admin API
# ===========================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class SkillTextCreate(BaseModel):
    name: str = ""
    description: str = ""
    body: str
    author: str = ""
    overwrite: bool = True


class ModelUpsert(BaseModel):
    label: str = ""
    base_url: str
    model: str
    api_key: Optional[str] = None


class ModelTestRequest(BaseModel):
    id: Optional[str] = None
    label: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


class PasswordUpdate(BaseModel):
    username: Optional[str] = None
    new_password: str


def _current_version() -> str:
    import json as _json
    try:
        with open(os.path.join(importer.DATA_DIR, "manifest.json"), encoding="utf-8") as fh:
            return str(_json.load(fh).get("content_version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


@app.post("/api/admin/login")
def admin_login(body: LoginRequest, response: Response):
    token = admin_auth.login(body.username, body.password)
    if not token:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        key=admin_auth.COOKIE_NAME, value=token, httponly=True,
        samesite="lax", max_age=admin_auth.SESSION_TTL, path="/",
    )
    return {"ok": True, "username": body.username}


@app.post("/api/admin/logout")
def admin_logout(response: Response, zc_admin: Optional[str] = Cookie(default=None)):
    admin_auth.destroy_session(zc_admin)
    response.delete_cookie(admin_auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/admin/state")
def admin_state(_user: str = Depends(admin_auth.require_admin)):
    backup_present = importer.backup_available()
    backup_ver = importer.backup_version() if backup_present else None
    models = config_store.list_models(mask_keys=True)
    active = next((m for m in models["models"] if m["active"]), None)
    return {
        "username": _user,
        "content_version": _current_version(),
        "retriever": retriever.stats(),
        "llm": {
            "active": active,
            "count": len(models["models"]),
            "models": models["models"],
        },
        "ask_log": ask_log.stats(),
        "upgrade_log": upgrade_log.stats(),
        "backup": {
            "available": backup_present,
            "path": importer.BACKUP_DIR,
            "version": backup_ver or None,
        },
    }


@app.get("/api/admin/ask-logs")
def admin_ask_logs(
    limit: int = 100,
    offset: int = 0,
    ip: Optional[str] = None,
    q: Optional[str] = None,
    ok: Optional[str] = None,
    days: Optional[int] = None,
    _user: str = Depends(admin_auth.require_admin),
):
    """Newest-first ask history for the admin UI (lightweight list items)."""
    return ask_log.list_logs(limit=limit, offset=offset, ip=ip, q=q, ok=ok, days=days)


@app.get("/api/admin/ask-logs/detail")
def admin_ask_log_detail(
    ts: str = "",
    ip: Optional[str] = None,
    _user: str = Depends(admin_auth.require_admin),
):
    """Full ask-log record for an expanded list row."""
    rec = ask_log.get_detail(ts=ts, ip=ip or "")
    if not rec:
        raise HTTPException(status_code=404, detail="记录不存在")
    return rec


@app.get("/api/admin/ask-logs/heatmap")
def admin_ask_heatmap(days: int = 84, _user: str = Depends(admin_auth.require_admin)):
    """Token usage heatmaps: daily calendar + weekday×hour."""
    return ask_log.heatmap(days=days)


@app.delete("/api/admin/ask-logs")
def admin_clear_ask_logs(_user: str = Depends(admin_auth.require_admin)):
    try:
        return ask_log.clear()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/admin/models")
def admin_list_models(_user: str = Depends(admin_auth.require_admin)):
    return config_store.list_models(mask_keys=True)


@app.post("/api/admin/models")
def admin_create_model(body: ModelUpsert, _user: str = Depends(admin_auth.require_admin)):
    try:
        saved = config_store.upsert_model(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    llm.reload()
    return {"ok": True, "model": {
        "id": saved["id"], "label": saved["label"], "base_url": saved["base_url"],
        "model": saved["model"],
        "api_key_set": bool(saved.get("api_key")) and saved.get("api_key") != "not-required",
    }}


@app.put("/api/admin/models/{model_id}")
def admin_update_model(model_id: str, body: ModelUpsert,
                       _user: str = Depends(admin_auth.require_admin)):
    try:
        saved = config_store.upsert_model(body.model_dump(), model_id=model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    llm.reload()
    return {"ok": True, "model": {
        "id": saved["id"], "label": saved["label"], "base_url": saved["base_url"],
        "model": saved["model"],
        "api_key_set": bool(saved.get("api_key")) and saved.get("api_key") != "not-required",
    }}


@app.delete("/api/admin/models/{model_id}")
def admin_delete_model(model_id: str, _user: str = Depends(admin_auth.require_admin)):
    try:
        store = config_store.delete_model(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    llm.reload()
    return {"ok": True, "active_id": store["active_id"], "count": len(store["models"])}


@app.post("/api/admin/models/{model_id}/activate")
def admin_activate_model(model_id: str, _user: str = Depends(admin_auth.require_admin)):
    """Enable this model; any previously enabled model is automatically stopped."""
    try:
        store = config_store.set_active_model(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    llm.reload()
    return {"ok": True, "active_id": store["active_id"]}


@app.post("/api/admin/models/{model_id}/deactivate")
def admin_deactivate_model(model_id: str, _user: str = Depends(admin_auth.require_admin)):
    """Stop this model if it is currently enabled."""
    try:
        store = config_store.deactivate_model(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    llm.reload()
    return {"ok": True, "active_id": store["active_id"]}


@app.post("/api/admin/models/test")
def admin_test_model(body: Optional[ModelTestRequest] = None,
                     _user: str = Depends(admin_auth.require_admin)):
    """Test a saved profile (by id) or a draft form payload."""
    draft = (body or ModelTestRequest()).model_dump(exclude_none=True)
    if draft.get("base_url") or draft.get("model"):
        return llm.test_connection(draft=draft)
    if draft.get("id"):
        try:
            return llm.test_connection(model_id=draft["id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return llm.test_connection()


@app.get("/api/admin/upgrades")
def admin_upgrades(limit: int = 50, offset: int = 0,
                   _user: str = Depends(admin_auth.require_admin)):
    """Newest-first content upgrade / rollback history."""
    return upgrade_log.list_logs(limit=limit, offset=offset)


@app.post("/api/admin/import")
async def admin_import(file: UploadFile = File(...),
                       _user: str = Depends(admin_auth.require_admin)):
    if not (file.filename or "").lower().endswith(".zdoc"):
        raise HTTPException(status_code=400, detail="请上传 .zdoc 包")
    tmp_path = os.path.join("/tmp", "upload.zdoc")
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    finally:
        await file.close()

    result = importer.import_package(tmp_path)
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    if result.ok and result.action in ("imported", "upgraded"):
        try:
            with _index_lock:
                retriever.rebuild()
        except Exception as exc:  # noqa: BLE001 — roll back content if index rebuild dies
            log.exception("reindex after import failed; rolling back")
            rb = importer.rollback_to_backup()
            result.ok = False
            result.rolled_back = rb.ok
            result.action = "rolled_back" if rb.ok else "error"
            result.errors.append(f"索引重建失败: {exc}")
            if rb.ok:
                result.message = (
                    f"索引重建失败，已自动回滚到 {result.from_version}"
                )
                try:
                    with _index_lock:
                        retriever.rebuild()
                except Exception as exc2:  # noqa: BLE001
                    result.errors.append(f"回滚后重建索引仍失败: {exc2}")
            else:
                result.message = f"索引重建失败且回滚失败: {rb.message}"
                result.errors.extend(rb.errors)

    upgrade_log.append(
        action=result.action,
        ok=result.ok,
        from_version=result.from_version,
        to_version=result.to_version,
        message=result.message,
        errors=result.errors,
        files_checked=result.files_checked,
        rolled_back=result.rolled_back,
        source="admin",
        package=file.filename or "",
        operator=_user,
    )

    status_code = 200 if result.ok or result.action == "skipped" else 422
    return JSONResponse(status_code=status_code, content=result.__dict__)


@app.post("/api/admin/rollback")
def admin_rollback(_user: str = Depends(admin_auth.require_admin)):
    """Manually restore the previous content tree from /backup."""
    result = importer.rollback_to_backup()
    if result.ok:
        try:
            with _index_lock:
                retriever.rebuild()
        except Exception as exc:  # noqa: BLE001
            result.ok = False
            result.action = "error"
            result.errors.append(f"回滚后索引重建失败: {exc}")
            result.message = f"内容已回滚，但索引重建失败: {exc}"

    upgrade_log.append(
        action=result.action,
        ok=result.ok,
        from_version=result.from_version,
        to_version=result.to_version,
        message=result.message,
        errors=result.errors,
        rolled_back=result.rolled_back,
        source="admin-rollback",
        operator=_user,
    )
    status_code = 200 if result.ok else 422
    return JSONResponse(status_code=status_code, content=result.__dict__)


@app.post("/api/admin/password")
def admin_password(body: PasswordUpdate, _user: str = Depends(admin_auth.require_admin)):
    username = body.username or _user or "admin"
    try:
        config_store.set_admin_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": username}


@app.get("/api/admin/skills")
def admin_list_skills(_user: str = Depends(admin_auth.require_admin)):
    skills_store.ensure_dir()
    items = skills_store.list_skills()
    return {
        "skills": items,
        "count": len(items),
        "skills_dir": skills_store.SKILLS_DIR,
    }


@app.post("/api/admin/skills")
def admin_create_skill_text(body: SkillTextCreate,
                            _user: str = Depends(admin_auth.require_admin)):
    try:
        skill = skills_store.save_skill_from_text(
            body.name, body.description, body.body,
            author=body.author, overwrite=body.overwrite,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    translation = _translate_skill_intro(skill)
    if translation.get("ok") and translation.get("description_zh"):
        skill = skills_store.get_skill(skill["dirname"])
    return {"ok": True, "skill": skill, "translation": translation}


@app.post("/api/admin/skills/upload")
async def admin_upload_skill(
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    overwrite: bool = Form(default=True),
    _user: str = Depends(admin_auth.require_admin),
):
    filename = (file.filename or "").lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 技能包")
    data = await file.read()
    await file.close()
    try:
        skill = skills_store.save_skill_from_zip(
            data,
            name_override=(name or None),
            overwrite=overwrite,
            zip_filename=file.filename,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    translation = _translate_skill_intro(skill)
    if translation.get("ok") and translation.get("description_zh"):
        skill = skills_store.get_skill(skill["dirname"])
    return {"ok": True, "skill": skill, "translation": translation}


@app.post("/api/admin/skills/{name}/translate")
def admin_translate_skill(
    name: str,
    force: bool = False,
    _user: str = Depends(admin_auth.require_admin),
):
    """Translate (or re-translate) one existing skill's intro with the active LLM."""
    try:
        skill = skills_store.get_skill(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    translation = _translate_skill_intro(skill, force=force)
    if translation.get("ok") and translation.get("description_zh"):
        skill = skills_store.get_skill(skill["dirname"])
    if not translation.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=translation.get("error") or "翻译失败（请确认已启用可用模型）",
        )
    return {"ok": True, "skill": skill, "translation": translation}


@app.post("/api/admin/skills/translate-missing")
def admin_translate_missing_skills(_user: str = Depends(admin_auth.require_admin)):
    """Batch-translate all skills that still lack a Chinese intro."""
    items = skills_store.list_skills()
    results = []
    translated = 0
    skipped = 0
    failed = 0
    for item in items:
        if (item.get("description_zh") or "").strip():
            skipped += 1
            results.append({
                "dirname": item.get("dirname"),
                "ok": True,
                "skipped": True,
                "reason": "already_translated",
            })
            continue
        try:
            skill = skills_store.get_skill(item["dirname"])
        except (FileNotFoundError, ValueError) as exc:
            failed += 1
            results.append({"dirname": item.get("dirname"), "ok": False, "error": str(exc)})
            continue
        translation = _translate_skill_intro(skill, force=False)
        entry = {"dirname": skill.get("dirname"), **translation}
        results.append(entry)
        if translation.get("ok") and not translation.get("skipped"):
            translated += 1
        elif translation.get("ok"):
            skipped += 1
        else:
            failed += 1
    return {
        "ok": failed == 0,
        "translated": translated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "skills": skills_store.list_skills(),
    }


@app.delete("/api/admin/skills/{name}")
def admin_delete_skill(name: str, _user: str = Depends(admin_auth.require_admin)):
    try:
        skills_store.delete_skill(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": name}


_admin_dir = os.path.join(os.path.dirname(__file__), "admin")
if os.path.isdir(_admin_dir):
    app.mount("/admin", StaticFiles(directory=_admin_dir, html=True), name="admin")

_skills_ui_dir = os.path.join(os.path.dirname(__file__), "skills")
if os.path.isdir(_skills_ui_dir):
    app.mount("/skills", StaticFiles(directory=_skills_ui_dir, html=True), name="skills")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
