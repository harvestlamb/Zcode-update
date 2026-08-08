"""Shared skill library stored under CONFIG_DIR/skills.

Layout:
  /config/skills/<skill-name>/SKILL.md
  /config/skills/<skill-name>/...optional extras...

Public LAN users browse/download; only admin APIs may write.
"""

from __future__ import annotations

import io
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


def _skill_path(name: str) -> str:
    safe = _safe_name(name)
    path = os.path.realpath(os.path.join(SKILLS_DIR, safe))
    root = os.path.realpath(SKILLS_DIR)
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("非法路径")
    return path


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Minimal YAML-like frontmatter parser (key: value lines only)."""
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            body = parts[2].lstrip("\n")
            for line in fm.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key:
                    meta[key] = val
    return meta, body


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
        items.append({
            "name": meta.get("name") or name,
            "dirname": name,
            "description": meta.get("description") or "",
            "author": meta.get("author") or "",
            "updated_at": _mtime_iso(md_path),
            "has_extra_files": _has_extra_files(dir_path),
        })
    items.sort(key=lambda x: x["name"].lower())
    return items


def get_skill(name: str) -> Dict[str, Any]:
    dir_path = _skill_path(name)
    md_path = os.path.join(dir_path, "SKILL.md")
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"技能不存在: {name}")
    text, meta, body = _read_skill_md(dir_path)
    dirname = os.path.basename(dir_path)
    return {
        "name": meta.get("name") or dirname,
        "dirname": dirname,
        "description": meta.get("description") or "",
        "author": meta.get("author") or "",
        "updated_at": _mtime_iso(md_path),
        "has_extra_files": _has_extra_files(dir_path),
        "content": text,
        "body": body,
    }


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
    name: str,
    description: str,
    body: str,
    *,
    author: str = "",
    overwrite: bool = True,
) -> Dict[str, Any]:
    ensure_dir()
    safe = _safe_name(name)
    dir_path = _skill_path(safe)
    if os.path.exists(dir_path) and not overwrite:
        raise FileExistsError(f"技能已存在: {safe}")
    if os.path.isdir(dir_path):
        shutil.rmtree(dir_path)
    os.makedirs(dir_path, exist_ok=True)

    desc = (description or "").strip()
    content = (body or "").strip()
    if not content:
        raise ValueError("SKILL.md 正文不能为空")

    author = (author or "").strip()
    if content.lstrip().startswith("---"):
        meta, rest = _parse_frontmatter(content)
        fm_name = meta.get("name") or safe
        fm_desc = meta.get("description") or desc
        fm_author = meta.get("author") or author
        lines = ["---", f"name: {fm_name}", f"description: {fm_desc}"]
        if fm_author:
            lines.append(f"author: {fm_author}")
        lines.append("---")
        text = "\n".join(lines) + "\n\n" + rest.lstrip()
        _write_raw(dir_path, text)
    else:
        lines = ["---", f"name: {safe}", f"description: {desc}"]
        if author:
            lines.append(f"author: {author}")
        lines.append("---")
        text = "\n".join(lines) + "\n\n" + content.lstrip() + "\n"
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


def save_skill_from_zip(
    data: bytes,
    *,
    name_override: Optional[str] = None,
    overwrite: bool = True,
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
        # Resolve skill name
        if name_override:
            safe = _safe_name(name_override)
        elif hint:
            safe = _safe_name(hint.lower().replace("_", "-"))
        else:
            # Read name from frontmatter
            md_name = (prefix or "") + "SKILL.md"
            raw = zf.read(md_name).decode("utf-8")
            meta, _ = _parse_frontmatter(raw)
            if not meta.get("name"):
                raise ValueError("请提供技能名，或在 SKILL.md frontmatter 中写 name")
            safe = _safe_name(meta["name"])

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

            # Ensure frontmatter name aligns with directory when missing
            try:
                text, meta, body = _read_skill_md(tmp)
                if not meta.get("name"):
                    _write_skill_md(tmp, safe, meta.get("description") or "", body or text)
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
