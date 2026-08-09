"""Shared skill library stored under CONFIG_DIR/skills.

Layout:
  /config/skills/<skill-name>/SKILL.md
  /config/skills/<skill-name>/...optional extras...

Public LAN users browse/download; only admin APIs may write.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("zcode-ai.skills")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SKILLS_DIR = os.path.join(CONFIG_DIR, "skills")

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
# Strip trailing semver from package folder / zip names: foo-1.2.0, foo-v1.2.0
_SEMVER_SUFFIX_RE = re.compile(
    r"(?:-|_)v?\d+(?:\.\d+){1,3}(?:[-.]?(?:alpha|beta|rc)\.?\d*)?$",
    re.IGNORECASE,
)
MAX_ZIP_BYTES = 8 * 1024 * 1024  # 8 MiB compressed
MAX_UNCOMPRESSED = 32 * 1024 * 1024  # 32 MiB total extracted
MAX_FILES = 200
MAX_SKILL_MD = 512 * 1024  # 512 KiB


def ensure_dir() -> None:
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create skills dir %s: %s", SKILLS_DIR, exc)


def _safe_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(
            "技能名仅允许小写字母、数字与连字符，且不能以连字符开头/结尾（最长 64）"
        )
    if name in (".", "..") or name.startswith("_"):
        raise ValueError("非法技能名")
    return name


def _slugify_name(raw: str) -> str:
    """Convert human / package names into a valid skill dirname.

    Examples:
      Verilog Design Flow -> verilog-design-flow
      verilog-design-1.2.0 -> verilog-design
      verilog_design -> verilog-design
    """
    s = (raw or "").strip().lower()
    if s.lower().endswith(".zip"):
        s = s[:-4]
    s = _SEMVER_SUFFIX_RE.sub("", s)
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > 64:
        s = s[:64].rstrip("-")
    if not s:
        raise ValueError("无法从名称生成合法技能名")
    return _safe_name(s)


def _skill_path(name: str) -> str:
    safe = _safe_name(name)
    path = os.path.realpath(os.path.join(SKILLS_DIR, safe))
    root = os.path.realpath(SKILLS_DIR)
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("非法路径")
    return path


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Minimal YAML-like frontmatter parser.

    Supports single-line values and block scalars (`>`, `|`) / indented continuations.
    """
    meta: Dict[str, str] = {}
    body = text
    if not text.startswith("---"):
        return meta, body
    parts = text.split("---", 2)
    if len(parts) < 3:
        return meta, body
    fm = parts[1]
    body = parts[2].lstrip("\n")

    key: Optional[str] = None
    buf: List[str] = []
    mode: Optional[str] = None  # fold | literal | cont

    def flush() -> None:
        nonlocal key, buf, mode
        if not key:
            return
        if mode == "literal":
            val = "\n".join(buf).strip("\n")
        else:
            val = " ".join(x.strip() for x in buf if x.strip())
        meta[key] = val.strip().strip("'\"")
        key, buf, mode = None, [], None

    for raw_line in fm.splitlines():
        if key and mode:
            if raw_line[:1] in (" ", "\t") or (not raw_line.strip() and mode == "literal"):
                buf.append(raw_line.strip() if mode != "literal" else raw_line.rstrip())
                continue
            flush()
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, val = line.partition(":")
        k = k.strip()
        val = val.strip()
        if not k:
            continue
        if val in (">", ">-", ">+"):
            key, buf, mode = k, [], "fold"
        elif val in ("|", "|-", "|+"):
            key, buf, mode = k, [], "literal"
        elif val == "":
            key, buf, mode = k, [], "cont"
        else:
            meta[k] = val.strip("'\"")
    flush()
    return meta, body


def _read_pkg_meta_bytes(data: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        log.warning("invalid skill package meta json: %s", exc)
        return {}
    return obj if isinstance(obj, dict) else {}


def _load_zip_pkg_meta(zf: zipfile.ZipFile, prefix: Optional[str]) -> Dict[str, Any]:
    for name in ("_meta.json", "meta.json"):
        path = (prefix or "") + name
        try:
            return _read_pkg_meta_bytes(zf.read(path))
        except KeyError:
            continue
    return {}


def _read_dir_pkg_meta(dir_path: str) -> Dict[str, Any]:
    for name in ("_meta.json", "meta.json"):
        path = os.path.join(dir_path, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                return _read_pkg_meta_bytes(fh.read())
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
    return {}


def _locale_path(dir_path: str) -> str:
    return os.path.join(dir_path, "_locale.json")


def _read_locale(dir_path: str) -> Dict[str, Any]:
    path = _locale_path(dir_path)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return _read_pkg_meta_bytes(fh.read())
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}


def _write_locale(dir_path: str, locale: Dict[str, Any]) -> None:
    path = _locale_path(dir_path)
    payload = json.dumps(locale, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)


def cjk_ratio(text: str) -> float:
    """Fraction of CJK ideographs in text (rough Chinese detector)."""
    s = (text or "").strip()
    if not s:
        return 0.0
    cjk = 0
    for ch in s:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
    return cjk / len(s)


def looks_chinese(text: str, threshold: float = 0.2) -> bool:
    return cjk_ratio(text) >= threshold


def set_description_zh(name: str, description_zh: str) -> Dict[str, Any]:
    """Persist a Chinese intro for a skill (sidecar _locale.json)."""
    dir_path = _skill_path(name)
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"技能不存在: {name}")
    zh = (description_zh or "").strip()
    if not zh:
        raise ValueError("中文简介不能为空")
    locale = _read_locale(dir_path)
    locale["description_zh"] = zh
    locale["translated_at"] = datetime.now(timezone.utc).isoformat()
    _write_locale(dir_path, locale)
    return get_skill(name)


def _skill_public_fields(
    dirname: str,
    meta: Dict[str, str],
    pkg_meta: Dict[str, Any],
    md_path: str,
    *,
    has_extra: bool,
    locale: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    author = (
        meta.get("author")
        or meta.get("authors")
        or (str(pkg_meta["author"]) if pkg_meta.get("author") else "")
        or ""
    )
    version = (
        meta.get("version")
        or (str(pkg_meta["version"]) if pkg_meta.get("version") else "")
        or ""
    )
    locale = locale or {}
    description_zh = (
        (locale.get("description_zh") if isinstance(locale.get("description_zh"), str) else "")
        or meta.get("description_zh")
        or ""
    ).strip()
    return {
        "name": meta.get("name") or dirname,
        "dirname": dirname,
        "description": meta.get("description") or "",
        "description_zh": description_zh,
        "author": author.strip() if isinstance(author, str) else str(author),
        "version": version.strip() if isinstance(version, str) else str(version),
        "updated_at": _mtime_iso(md_path),
        "has_extra_files": has_extra,
    }


def _mtime_iso(path: str) -> str:
    try:
        ts = os.path.getmtime(path)
    except OSError:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _read_skill_md(dir_path: str) -> Tuple[str, Dict[str, str], str]:
    md_path = os.path.join(dir_path, "SKILL.md")
    with open(md_path, "r", encoding="utf-8") as fh:
        text = fh.read(MAX_SKILL_MD + 1)
    if len(text) > MAX_SKILL_MD:
        raise ValueError("SKILL.md 过大")
    meta, body = _parse_frontmatter(text)
    return text, meta, body


def _has_extra_files(dir_path: str) -> bool:
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            if f == "SKILL.md" and os.path.realpath(root) == os.path.realpath(dir_path):
                continue
            if f.startswith("."):
                continue
            return True
    return False


def list_skills() -> List[Dict[str, Any]]:
    ensure_dir()
    items: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return []
    for name in names:
        if name.startswith(".") or name.startswith("_"):
            continue
        dir_path = os.path.join(SKILLS_DIR, name)
        if not os.path.isdir(dir_path):
            continue
        md_path = os.path.join(dir_path, "SKILL.md")
        if not os.path.isfile(md_path):
            continue
        try:
            _safe_name(name)
            _text, meta, _body = _read_skill_md(dir_path)
        except (OSError, ValueError, UnicodeError) as exc:
            log.warning("skip skill %s: %s", name, exc)
            continue
        items.append(
            _skill_public_fields(
                name,
                meta,
                _read_dir_pkg_meta(dir_path),
                md_path,
                has_extra=_has_extra_files(dir_path),
                locale=_read_locale(dir_path),
            )
        )
    items.sort(key=lambda x: x["name"].lower())
    return items


def get_skill(name: str) -> Dict[str, Any]:
    dir_path = _skill_path(name)
    md_path = os.path.join(dir_path, "SKILL.md")
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"技能不存在: {name}")
    text, meta, body = _read_skill_md(dir_path)
    dirname = os.path.basename(dir_path)
    out = _skill_public_fields(
        dirname,
        meta,
        _read_dir_pkg_meta(dir_path),
        md_path,
        has_extra=_has_extra_files(dir_path),
        locale=_read_locale(dir_path),
    )
    out["content"] = text
    out["body"] = body
    return out


def delete_skill(name: str) -> None:
    dir_path = _skill_path(name)
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"技能不存在: {name}")
    shutil.rmtree(dir_path)


def pack_zip(name: str) -> bytes:
    dir_path = _skill_path(name)
    if not os.path.isdir(dir_path) or not os.path.isfile(os.path.join(dir_path, "SKILL.md")):
        raise FileNotFoundError(f"技能不存在: {name}")
    buf = io.BytesIO()
    dirname = os.path.basename(dir_path)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dir_path):
            for f in files:
                if f.startswith("."):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, dir_path)
                arc = os.path.join(dirname, rel).replace("\\", "/")
                zf.write(full, arcname=arc)
    return buf.getvalue()


def _write_skill_md(dir_path: str, name: str, description: str, body: str) -> None:
    desc = (description or "").strip()
    content_body = body if body is not None else ""
    # If body already has frontmatter, keep as-is (but ensure file exists).
    stripped = content_body.lstrip()
    if stripped.startswith("---"):
        text = content_body if content_body.endswith("\n") else content_body + "\n"
    else:
        text = (
            f"---\n"
            f"name: {name}\n"
            f"description: {desc}\n"
            f"---\n\n"
            f"{content_body.lstrip()}"
        )
        if not text.endswith("\n"):
            text += "\n"
    os.makedirs(dir_path, exist_ok=True)
    md_path = os.path.join(dir_path, "SKILL.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(text)


def save_skill_from_text(
    name: str = "",
    description: str = "",
    body: str = "",
    *,
    author: str = "",
    overwrite: bool = True,
) -> Dict[str, Any]:
    ensure_dir()
    content = (body or "").strip()
    if not content:
        raise ValueError("SKILL.md 正文不能为空")

    meta: Dict[str, str] = {}
    rest = content
    if content.lstrip().startswith("---"):
        meta, rest = _parse_frontmatter(content)

    # Directory name: explicit override → frontmatter name → fail
    if (name or "").strip():
        safe = _slugify_name(name)
    elif meta.get("name"):
        safe = _slugify_name(meta["name"])
    else:
        raise ValueError(
            "无法确定技能目录名：请在 SKILL.md frontmatter 写 name，或另行指定"
        )

    dir_path = _skill_path(safe)
    if os.path.exists(dir_path) and not overwrite:
        raise FileExistsError(f"技能已存在: {safe}")
    if os.path.isdir(dir_path):
        shutil.rmtree(dir_path)
    os.makedirs(dir_path, exist_ok=True)

    desc = (description or "").strip() or (meta.get("description") or "").strip()
    author = (author or "").strip() or (meta.get("author") or meta.get("authors") or "").strip()
    # Keep human-readable display name from frontmatter when present
    display_name = (meta.get("name") or "").strip() or safe

    lines = ["---", f"name: {display_name}", f"description: {desc}"]
    if author:
        lines.append(f"author: {author}")
    lines.append("---")
    text = "\n".join(lines) + "\n\n" + rest.lstrip()
    if not text.endswith("\n"):
        text += "\n"
    _write_raw(dir_path, text)

    return get_skill(safe)


def _write_raw(dir_path: str, text: str) -> None:
    if len(text.encode("utf-8")) > MAX_SKILL_MD:
        raise ValueError("SKILL.md 过大")
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def _zip_members_safe(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    members = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"zip 含非法路径: {info.filename}")
        if info.file_size > MAX_UNCOMPRESSED:
            raise ValueError("zip 内单文件过大")
        total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise ValueError("zip 解压后体积过大")
        members.append(info)
    if len(members) > MAX_FILES:
        raise ValueError(f"zip 内文件过多（最多 {MAX_FILES}）")
    if not members:
        raise ValueError("zip 为空")
    return members


def _detect_skill_root(members: List[zipfile.ZipInfo]) -> Tuple[Optional[str], str]:
    """Return (prefix_inside_zip or None, skill_dirname_hint).

    Accepts either:
      skill-name/SKILL.md + extras
      SKILL.md at zip root
    """
    paths = [m.filename.replace("\\", "/") for m in members]
    skill_mds = [p for p in paths if p.endswith("SKILL.md") or p.endswith("skill.md")]
    # Normalize to SKILL.md case
    skill_mds = [p for p in paths if os.path.basename(p) == "SKILL.md"]
    if not skill_mds:
        raise ValueError("zip 中未找到 SKILL.md")
    # Prefer shallowest SKILL.md
    skill_mds.sort(key=lambda p: (p.count("/"), len(p)))
    md = skill_mds[0]
    if md == "SKILL.md":
        return None, ""
    parts = md.split("/")
    if len(parts) == 2 and parts[1] == "SKILL.md":
        return parts[0] + "/", parts[0]
    # Nested deeper — use parent of SKILL.md as root prefix
    prefix = "/".join(parts[:-1]) + "/"
    hint = parts[-2]
    return prefix, hint


def _resolve_skill_dirname(
    *,
    name_override: Optional[str],
    pkg_meta: Dict[str, Any],
    frontmatter: Dict[str, str],
    dir_hint: str,
    zip_filename: Optional[str],
) -> str:
    """Pick a valid skill dirname from package metadata, preferring slug."""
    candidates: List[str] = []
    if name_override:
        candidates.append(name_override)
    slug = pkg_meta.get("slug") or pkg_meta.get("name")
    if slug:
        candidates.append(str(slug))
    if dir_hint:
        candidates.append(dir_hint)
    if frontmatter.get("name"):
        candidates.append(frontmatter["name"])
    if zip_filename:
        candidates.append(os.path.basename(zip_filename))

    last_err = "请提供技能名，或在包内提供 _meta.json.slug / SKILL.md name"
    for raw in candidates:
        try:
            return _slugify_name(raw)
        except ValueError as exc:
            last_err = str(exc)
            continue
    raise ValueError(last_err)


def save_skill_from_zip(
    data: bytes,
    *,
    name_override: Optional[str] = None,
    overwrite: bool = True,
    zip_filename: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_dir()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"zip 过大（最大 {MAX_ZIP_BYTES // (1024*1024)}MB）")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("无效的 zip 文件") from exc

    with zf:
        members = _zip_members_safe(zf)
        prefix, hint = _detect_skill_root(members)
        md_name = (prefix or "") + "SKILL.md"
        try:
            raw_md = zf.read(md_name).decode("utf-8")
        except KeyError as exc:
            raise ValueError("zip 中未找到 SKILL.md") from exc
        except UnicodeError as exc:
            raise ValueError("SKILL.md 不是合法 UTF-8") from exc

        frontmatter, _body = _parse_frontmatter(raw_md)
        pkg_meta = _load_zip_pkg_meta(zf, prefix)
        safe = _resolve_skill_dirname(
            name_override=name_override,
            pkg_meta=pkg_meta,
            frontmatter=frontmatter,
            dir_hint=hint,
            zip_filename=zip_filename,
        )

        dest = _skill_path(safe)
        if os.path.exists(dest) and not overwrite:
            raise FileExistsError(f"技能已存在: {safe}")

        tmp = tempfile.mkdtemp(prefix="skill-", dir=SKILLS_DIR)
        try:
            for info in members:
                name = info.filename.replace("\\", "/")
                if prefix:
                    if not name.startswith(prefix):
                        continue
                    rel = name[len(prefix):]
                else:
                    rel = name
                if not rel or rel.endswith("/"):
                    continue
                if ".." in rel.split("/"):
                    raise ValueError(f"zip 含非法路径: {name}")
                target = os.path.realpath(os.path.join(tmp, rel))
                if not target.startswith(os.path.realpath(tmp) + os.sep):
                    raise ValueError(f"zip 含非法路径: {name}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

            md_path = os.path.join(tmp, "SKILL.md")
            if not os.path.isfile(md_path):
                raise ValueError("解压后未找到 SKILL.md")

            # Keep package frontmatter (description / display name / author).
            # Only synthesize a minimal frontmatter when name+description are both missing.
            try:
                text, meta, body = _read_skill_md(tmp)
                if not meta.get("name") and not meta.get("description"):
                    _write_skill_md(tmp, safe, "", body or text)
                elif not meta.get("name"):
                    # Preserve description/author; ensure a display name exists.
                    author = (meta.get("author") or "").strip()
                    desc = (meta.get("description") or "").strip()
                    lines = ["---", f"name: {safe}", f"description: {desc}"]
                    if author:
                        lines.append(f"author: {author}")
                    lines.append("---")
                    _write_raw(tmp, "\n".join(lines) + "\n\n" + (body or "").lstrip())
            except ValueError:
                pass

            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.replace(tmp, dest)
            tmp = ""  # moved
        finally:
            if tmp and os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)

    return get_skill(safe)
