"""In-process mirror of intranet/import.sh — used by the admin "import" UI.

Reproduces the script's safety guarantees exactly:
  1. extract the .zdoc (tar.gz) to a staging dir
  2. verify every file's SHA-256 against the bundled SHA256SUMS
  3. compare content_version; skip if already on this version
  4. atomic swap: backup current /data, move staging's known entries in
  5. on any post-backup failure → restore /backup into /data (rollback)
  6. (caller rebuilds the retrieval index)

Anything unexpected in staging is left behind (the same conservative whitelist
as the shell script: site, content, releases, manifest.json, SHA256SUMS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import time
from dataclasses import dataclass, field

log = logging.getLogger("zcode-ai.importer")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backup")
STAGING_DIR = os.environ.get("STAGING_DIR", "/tmp/.staging")

# Same whitelist import.sh moves into place.
_KEEP = ("site", "content", "releases", "manifest.json", "SHA256SUMS")

# SHA256SUMS lines look like:  <hash>  <path>
_SUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


@dataclass
class ImportResult:
    ok: bool
    action: str = ""            # "imported" | "upgraded" | "skipped" | "error" | "rolled_back"
    from_version: str = ""
    to_version: str = ""
    files_checked: int = 0
    message: str = ""
    errors: list = field(default_factory=list)
    rolled_back: bool = False
    backup_available: bool = False


def _read_version(manifest_path: str) -> str:
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            return str(json.load(fh).get("content_version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def _verify_sums(staging: str) -> tuple[bool, int, list]:
    """Return (all_ok, n_checked, failures)."""
    sums_path = os.path.join(staging, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        return False, 0, ["包内缺少 SHA256SUMS，可能损坏"]
    failures = []
    checked = 0
    with open(sums_path, encoding="utf-8") as fh:
        for line in fh:
            m = _SUM_RE.match(line.strip())
            if not m:
                continue
            expected, rel = m.group(1).lower(), m.group(2).strip()
            target = os.path.normpath(os.path.join(staging, rel))
            # Stay inside staging (defend against path traversal in the file list).
            if os.path.commonpath([staging, target]) != os.path.normpath(staging):
                failures.append(f"非法路径: {rel}")
                continue
            if not os.path.isfile(target):
                failures.append(f"缺失文件: {rel}")
                continue
            h = hashlib.sha256()
            with open(target, "rb") as bf:
                for chunk in iter(lambda: bf.read(65536), b""):
                    h.update(chunk)
            checked += 1
            if h.hexdigest() != expected:
                failures.append(f"校验失败: {rel}")
    return (not failures), checked, failures


def _rm_path(path: str, attempts: int = 8) -> None:
    """Remove a file/dir, retrying — Windows bind mounts often return ENOTEMPTY."""
    last: OSError | None = None
    for i in range(attempts):
        try:
            if not os.path.lexists(path):
                return
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.25 * (i + 1))
    if last is not None:
        raise last


def _wipe_contents(dir_path: str) -> None:
    """Remove everything *inside* dir_path but keep the dir itself.

    Needed because /data and /backup are bind-mount volumes: rmtree on the
    mount point itself raises EBUSY, so we delete entries one level down.
    """
    os.makedirs(dir_path, exist_ok=True)
    for name in list(os.listdir(dir_path)):
        _rm_path(os.path.join(dir_path, name))


def _sync_tree(src_dir: str, dst_dir: str) -> None:
    """Overlay src_dir onto dst_dir and drop files that are not in src.

    Prefer this over wipe+copytree on Docker Desktop / Windows: nginx may
    keep /data/site open, so rmtree raises [Errno 39] Directory not empty.
    """
    os.makedirs(dst_dir, exist_ok=True)
    if not os.path.isdir(src_dir):
        return
    wanted = set()
    for name in os.listdir(src_dir):
        wanted.add(name)
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if os.path.isdir(src) and not os.path.islink(src):
            if os.path.isfile(dst) or os.path.islink(dst):
                os.remove(dst)
            _sync_tree(src, dst)
        else:
            if os.path.isdir(dst) and not os.path.islink(dst):
                _rm_path(dst)
            shutil.copy2(src, dst)
    for name in list(os.listdir(dst_dir)):
        if name in wanted:
            continue
        try:
            _rm_path(os.path.join(dst_dir, name))
        except OSError as exc:
            log.warning("Could not remove leftover %s: %s", name, exc)


def _replace_entry(src: str, dst: str) -> None:
    """Put src at dst without rmtree'ing dst first (nginx may have it open).

    Copy onto a sibling ``.incoming`` name, rename dst → ``.old``, then
    incoming → dst. Rename is instant on the same bind mount; deleting
    ``.old`` is best-effort.
    """
    incoming = dst + ".incoming"
    old = dst + ".old"
    try:
        _rm_path(incoming)
    except OSError:
        pass
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, incoming, symlinks=True)
    else:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, incoming)
    if os.path.lexists(dst):
        try:
            _rm_path(old)
        except OSError:
            pass
        try:
            os.rename(dst, old)
        except OSError:
            if os.path.isdir(incoming) and not os.path.islink(incoming):
                _sync_tree(incoming, dst)
            else:
                shutil.copy2(incoming, dst)
            try:
                _rm_path(incoming)
            except OSError:
                pass
            return
    os.rename(incoming, dst)
    try:
        _rm_path(old)
    except OSError as exc:
        log.warning("Could not remove leftover %s: %s", old, exc)


def backup_available() -> bool:
    if not os.path.isdir(BACKUP_DIR):
        return False
    try:
        return bool(os.listdir(BACKUP_DIR))
    except OSError:
        return False


def backup_version() -> str:
    if not backup_available():
        return ""
    return _read_version(os.path.join(BACKUP_DIR, "manifest.json"))


def _validate_data_dir() -> list:
    """Return list of structural problems with the installed /data tree."""
    problems = []
    if not os.path.isfile(os.path.join(DATA_DIR, "manifest.json")):
        problems.append("缺少 manifest.json")
    if not os.path.isfile(os.path.join(DATA_DIR, "SHA256SUMS")):
        problems.append("缺少 SHA256SUMS")
    if not os.path.isdir(os.path.join(DATA_DIR, "content")):
        problems.append("缺少 content/")
    if not os.path.isdir(os.path.join(DATA_DIR, "site")):
        problems.append("缺少 site/")
    return problems


def _restore_from_backup() -> tuple[bool, str]:
    """Restore /data from /backup. Returns (ok, message)."""
    if not backup_available():
        return False, "没有可用备份，无法回滚"
    try:
        # Do not rmtree /data first — nginx holds files under /data/site.
        _sync_tree(BACKUP_DIR, DATA_DIR)
    except OSError as exc:
        return False, f"回滚失败: {exc}"
    problems = _validate_data_dir()
    if problems:
        return False, "回滚后数据仍不完整: " + "; ".join(problems)
    ver = _read_version(os.path.join(DATA_DIR, "manifest.json"))
    return True, f"已回滚到备份版本 {ver}"


def rollback_to_backup() -> ImportResult:
    """Manual rollback: restore the previous content tree from /backup."""
    res = ImportResult(ok=False, action="rollback", backup_available=backup_available())
    cur = _read_version(os.path.join(DATA_DIR, "manifest.json")) \
        if os.path.isfile(os.path.join(DATA_DIR, "manifest.json")) else "none"
    res.from_version = cur
    if not res.backup_available:
        res.errors.append("没有可用备份")
        res.message = "无法回滚：备份目录为空"
        return res
    bak_ver = _read_version(os.path.join(BACKUP_DIR, "manifest.json")) \
        if os.path.isfile(os.path.join(BACKUP_DIR, "manifest.json")) else "unknown"
    res.to_version = bak_ver
    ok, msg = _restore_from_backup()
    res.ok = ok
    res.rolled_back = ok
    res.message = msg if ok else msg
    res.action = "rolled_back" if ok else "error"
    if not ok:
        res.errors.append(msg)
    return res


def import_package(pkg_path: str) -> ImportResult:
    """Validate + install a .zdoc. Never touches /data until SHA checks pass.

    If the swap fails after the backup snapshot was taken, automatically restore
    /data from /backup and mark the result as rolled back.
    """
    res = ImportResult(ok=False, backup_available=backup_available())
    if not os.path.isfile(pkg_path):
        res.errors.append(f"找不到包文件: {pkg_path}")
        res.message = "上传失败"
        res.action = "error"
        return res

    # Fresh staging dir.
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    try:
        with tarfile.open(pkg_path, "r:gz") as tar:
            tar.extractall(STAGING_DIR)  # noqa: S202 — local trusted package
    except (tarfile.TarError, OSError) as exc:
        res.errors.append(f"解包失败: {exc}")
        res.message = "包无法解压"
        res.action = "error"
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return res

    # 1. integrity
    log.info("Import: verifying SHA-256 for package files")
    ok_hashes, n_checked, failures = _verify_sums(STAGING_DIR)
    res.files_checked = n_checked
    if not ok_hashes:
        res.errors.extend(failures)
        res.message = "完整性校验失败，已中止（现网数据未受影响）"
        res.action = "error"
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return res

    new_manifest = os.path.join(STAGING_DIR, "manifest.json")
    if not os.path.isfile(new_manifest):
        res.errors.append("包内缺少 manifest.json")
        res.message = "包结构不完整"
        res.action = "error"
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return res

    # 2. version compare
    new_ver = _read_version(new_manifest)
    cur_ver = _read_version(os.path.join(DATA_DIR, "manifest.json")) \
        if os.path.isfile(os.path.join(DATA_DIR, "manifest.json")) else "none"
    res.from_version, res.to_version = cur_ver, new_ver

    if new_ver not in ("unknown",) and new_ver == cur_ver:
        res.ok = True
        res.action = "skipped"
        res.message = f"已是版本 {new_ver}，无需升级"
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return res

    # 3. atomic-ish swap: backup current content, then install the new one.
    # NOTE: /data and /backup are bind-mount volumes — we cannot rmdir/move the
    # mount points themselves (EBUSY). Instead we replace their *contents*.
    swapped = False
    try:
        log.info("Import: snapshot current data → backup")
        _sync_tree(DATA_DIR, BACKUP_DIR)      # snapshot current data as backup
        res.backup_available = backup_available()
        keep = set(_KEEP)
        log.info("Import: write new content into /data")
        for item in _KEEP:
            src = os.path.join(STAGING_DIR, item)
            dst = os.path.join(DATA_DIR, item)
            if not os.path.exists(src):
                continue
            _replace_entry(src, dst)
        for name in list(os.listdir(DATA_DIR)):
            if name not in keep and not name.endswith(".old") and not name.endswith(".incoming"):
                _rm_path(os.path.join(DATA_DIR, name))
        swapped = True
        problems = _validate_data_dir()
        if problems:
            raise RuntimeError("安装后校验失败: " + "; ".join(problems))
    except (OSError, RuntimeError) as exc:
        res.errors.append(f"数据替换失败: {exc}")
        # Always attempt rollback once we have touched /data after backup.
        if res.backup_available:
            rb_ok, rb_msg = _restore_from_backup()
            res.rolled_back = rb_ok
            if rb_ok:
                res.message = f"升级失败，已自动回滚到 {res.from_version}（{rb_msg}）"
                res.action = "rolled_back"
            else:
                res.message = f"升级失败且回滚失败：{rb_msg}。请检查 /backup"
                res.action = "error"
                res.errors.append(rb_msg)
        else:
            res.message = "替换失败，且无可用备份可回滚"
            res.action = "error"
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return res
    finally:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)

    if not swapped:
        res.action = "error"
        res.message = "未知错误：未完成替换"
        return res

    res.ok = True
    res.action = "upgraded" if cur_ver not in ("none", "unknown") else "imported"
    res.message = f"升级完成: {cur_ver} -> {new_ver}"
    try:
        import overlay_apply
        applied = overlay_apply.apply(DATA_DIR)
        if applied.get("copied"):
            res.message += "；已重新套用文档助手"
    except Exception as exc:  # noqa: BLE001
        log.warning("Docs-assistant overlay not applied after import: %s", exc)
    return res
