"""Persistent runtime config for the admin UI.

Two JSON files live under CONFIG_DIR (mounted at /config, sibling-independent
of /data so an import never wipes the saved settings):

  /config/llm.json   — model profiles (+ which one is active)
  /config/admin.json — admin credentials (username + salted sha256 password hash)

Writes are atomic (tmp file + os.replace). Missing files/dirs fall back to
sensible defaults, so the first run needs no setup.

llm.json shape (v2):
  {
    "active_id": "m1",
    "models": [
      {"id": "m1", "label": "DeepSeek", "base_url": "...", "model": "...", "api_key": "..."}
    ]
  }

Legacy single-object files ({base_url, model, api_key, ...}) are migrated on load.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("zcode-ai.config")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
LLM_FILE = os.path.join(CONFIG_DIR, "llm.json")
ADMIN_FILE = os.path.join(CONFIG_DIR, "admin.json")

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"

MODEL_KEYS = ("id", "label", "base_url", "model", "api_key")

_lock = threading.Lock()


def _ensure_dir() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create config dir %s: %s", CONFIG_DIR, exc)


def _atomic_write(path: str, data: dict) -> None:
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _new_id() -> str:
    return "m" + secrets.token_hex(4)


def _clean_model(raw: dict, *, keep_id: Optional[str] = None) -> dict:
    mid = (keep_id or raw.get("id") or _new_id()).strip()
    label = str(raw.get("label") or raw.get("model") or "未命名模型").strip()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    model = str(raw.get("model") or "").strip()
    api_key = "" if raw.get("api_key") is None else str(raw.get("api_key"))
    if not model:
        raise ValueError("模型名称不能为空")
    if not base_url:
        raise ValueError("服务地址不能为空")
    if not label:
        label = model
    return {
        "id": mid,
        "label": label,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
    }


def _normalize_store(data: Any) -> dict:
    """Accept v2 store or legacy single-model object; return {active_id, models}."""
    if not isinstance(data, dict):
        return {"active_id": "", "models": []}

    if isinstance(data.get("models"), list):
        models: List[dict] = []
        for item in data["models"]:
            if not isinstance(item, dict):
                continue
            try:
                models.append(_clean_model(item, keep_id=str(item.get("id") or "") or None))
            except ValueError:
                continue
        active = str(data.get("active_id") or "")
        ids = {m["id"] for m in models}
        # Allow empty active_id (all stopped). Only repair stale ids.
        if active and active not in ids:
            active = models[0]["id"] if models else ""
        if not models:
            active = ""
        return {"active_id": active, "models": models}

    # Legacy: flat {base_url, model, api_key, temperature, ...}
    if data.get("model") or data.get("base_url"):
        try:
            m = _clean_model({
                "id": "default",
                "label": data.get("model") or "默认模型",
                "base_url": data.get("base_url") or "",
                "model": data.get("model") or "",
                "api_key": data.get("api_key") or "",
            }, keep_id="default")
            return {"active_id": "default", "models": [m]}
        except ValueError:
            pass
    return {"active_id": "", "models": []}


# ---------------------------------------------------------------------------
# LLM / model profiles
# ---------------------------------------------------------------------------

def load_llm_store(*, persist_migration: bool = False) -> dict:
    """Return {active_id, models}. Optionally rewrite legacy files to v2."""
    try:
        with open(LLM_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {"active_id": "", "models": []}
    except (OSError, ValueError) as exc:
        log.warning("Bad llm.json (%s); ignoring.", exc)
        return {"active_id": "", "models": []}

    store = _normalize_store(raw)
    legacy = not isinstance(raw.get("models"), list)
    if persist_migration and legacy and store["models"]:
        with _lock:
            _atomic_write(LLM_FILE, store)
        log.info("Migrated llm.json to multi-model format (%d profiles).", len(store["models"]))
    return store


def save_llm_store(store: dict) -> dict:
    cleaned = _normalize_store(store)
    with _lock:
        _atomic_write(LLM_FILE, cleaned)
    return cleaned


def list_models(*, mask_keys: bool = True) -> dict:
    store = load_llm_store(persist_migration=True)
    out = []
    for m in store["models"]:
        item = {
            "id": m["id"],
            "label": m["label"],
            "base_url": m["base_url"],
            "model": m["model"],
            "api_key_set": bool(m.get("api_key")) and m.get("api_key") != "not-required",
            "active": m["id"] == store["active_id"],
        }
        if not mask_keys:
            item["api_key"] = m.get("api_key") or ""
        out.append(item)
    return {"active_id": store["active_id"], "models": out}


def get_model(model_id: Optional[str] = None) -> Optional[dict]:
    """Return a profile by id, or the currently active one.

    When model_id is omitted and nothing is active, returns None (does not
    fall back to the first profile).
    """
    store = load_llm_store(persist_migration=True)
    if not store["models"]:
        return None
    if model_id:
        for m in store["models"]:
            if m["id"] == model_id:
                return dict(m)
        return None
    if not store["active_id"]:
        return None
    for m in store["models"]:
        if m["id"] == store["active_id"]:
            return dict(m)
    return None


def upsert_model(payload: dict, model_id: Optional[str] = None) -> dict:
    """Create or update a model profile. Empty api_key keeps the previous key."""
    store = load_llm_store(persist_migration=True)
    existing = None
    if model_id:
        for m in store["models"]:
            if m["id"] == model_id:
                existing = m
                break
        if existing is None:
            raise KeyError("模型不存在")

    data = dict(payload)
    if existing and not data.get("api_key"):
        data["api_key"] = existing.get("api_key") or ""
    cleaned = _clean_model(data, keep_id=model_id or data.get("id"))

    if existing:
        store["models"] = [cleaned if m["id"] == model_id else m for m in store["models"]]
    else:
        if any(m["id"] == cleaned["id"] for m in store["models"]):
            cleaned["id"] = _new_id()
        store["models"].append(cleaned)
        if not store["active_id"]:
            store["active_id"] = cleaned["id"]

    save_llm_store(store)
    return cleaned


def delete_model(model_id: str) -> dict:
    store = load_llm_store(persist_migration=True)
    before = len(store["models"])
    store["models"] = [m for m in store["models"] if m["id"] != model_id]
    if len(store["models"]) == before:
        raise KeyError("模型不存在")
    if store["active_id"] == model_id:
        store["active_id"] = store["models"][0]["id"] if store["models"] else ""
    return save_llm_store(store)


def set_active_model(model_id: str) -> dict:
    """Enable exactly one model (any previous active is replaced)."""
    store = load_llm_store(persist_migration=True)
    if model_id not in {m["id"] for m in store["models"]}:
        raise KeyError("模型不存在")
    store["active_id"] = model_id
    return save_llm_store(store)


def deactivate_model(model_id: Optional[str] = None) -> dict:
    """Stop the active model. If model_id is given, only clear when it matches."""
    store = load_llm_store(persist_migration=True)
    if model_id:
        if model_id not in {m["id"] for m in store["models"]}:
            raise KeyError("模型不存在")
        if store["active_id"] == model_id:
            store["active_id"] = ""
    else:
        store["active_id"] = ""
    return save_llm_store(store)


# Backward-compatible helpers used by older call sites / tests.
def load_llm_config() -> dict:
    """Return active model as a flat dict (legacy shape, no temperature fields)."""
    m = get_model()
    if not m:
        return {}
    return {
        "base_url": m["base_url"],
        "model": m["model"],
        "api_key": m.get("api_key") or "",
    }


def save_llm_config(settings: dict) -> dict:
    """Legacy: overwrite/create the active profile from a flat settings dict."""
    active = get_model()
    payload = {
        "label": settings.get("label") or (active or {}).get("label") or settings.get("model") or "默认模型",
        "base_url": settings.get("base_url") or (active or {}).get("base_url") or "",
        "model": settings.get("model") or (active or {}).get("model") or "",
        "api_key": settings.get("api_key") if settings.get("api_key") is not None else "",
    }
    mid = (active or {}).get("id")
    saved = upsert_model(payload, model_id=mid)
    return {
        "base_url": saved["base_url"],
        "model": saved["model"],
        "api_key": saved.get("api_key") or "",
    }


# ---------------------------------------------------------------------------
# Admin credentials
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def load_admin() -> dict:
    """Return {username, password_hash, salt, is_default}."""
    try:
        with open(ADMIN_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "username": data.get("username", DEFAULT_ADMIN_USER),
            "password_hash": data.get("password_hash", ""),
            "salt": data.get("salt", ""),
            "is_default": False,
        }
    except FileNotFoundError:
        return {"username": DEFAULT_ADMIN_USER, "is_default": True}
    except (OSError, ValueError) as exc:
        log.warning("Bad admin.json (%s); using default.", exc)
        return {"username": DEFAULT_ADMIN_USER, "is_default": True}


def verify_credentials(username: str, password: str) -> bool:
    rec = load_admin()
    if rec["username"] != username:
        return False
    if rec.get("is_default"):
        return password == DEFAULT_ADMIN_PASS
    return rec["password_hash"] == _hash_password(password, rec["salt"])


def set_admin_password(username: str, new_password: str) -> None:
    if not username or not new_password:
        raise ValueError("用户名和密码不能为空")
    salt = secrets.token_hex(8)
    data = {
        "username": username,
        "salt": salt,
        "password_hash": _hash_password(new_password, salt),
    }
    with _lock:
        _atomic_write(ADMIN_FILE, data)
