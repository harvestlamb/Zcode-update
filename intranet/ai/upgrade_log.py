"""Append-only upgrade / import history for the intranet mirror.

Stored as JSON Lines under CONFIG_DIR so content swaps never wipe the audit trail.
Records every import attempt: success, skip, failure, and automatic/manual rollback.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("zcode-ai.upgradelog")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
LOG_FILE = os.path.join(CONFIG_DIR, "upgrade_logs.jsonl")
HARD_CAP_LINES = 2000

_lock = threading.Lock()


def _ensure_dir() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create config dir %s: %s", CONFIG_DIR, exc)


def append(
    *,
    action: str,
    ok: bool,
    from_version: str = "",
    to_version: str = "",
    message: str = "",
    errors: Optional[List[str]] = None,
    files_checked: int = 0,
    rolled_back: bool = False,
    source: str = "admin",
    package: str = "",
    operator: str = "",
) -> dict:
    """Append one upgrade event. Returns the written record."""
    rec = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "action": action or "",
        "ok": bool(ok),
        "from_version": from_version or "",
        "to_version": to_version or "",
        "message": (message or "")[:1000],
        "errors": list(errors or [])[:20],
        "files_checked": int(files_checked or 0),
        "rolled_back": bool(rolled_back),
        "source": source or "admin",
        "package": (package or "")[:300],
        "operator": (operator or "")[:80],
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        _ensure_dir()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error("Failed to write upgrade log: %s", exc)
            return rec
        _maybe_trim()
    return rec


def _maybe_trim() -> None:
    try:
        if not os.path.isfile(LOG_FILE) or os.path.getsize(LOG_FILE) < 512 * 1024:
            return
        with open(LOG_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= HARD_CAP_LINES:
            return
        keep = lines[-HARD_CAP_LINES:]
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, LOG_FILE)
    except OSError:
        pass


def list_logs(*, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    rows: List[Dict[str, Any]] = []
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        return {"total": 0, "items": [], "limit": limit, "offset": offset}
    except OSError as exc:
        log.error("Failed to read upgrade log: %s", exc)
        return {"total": 0, "items": [], "limit": limit, "offset": offset, "error": str(exc)}

    rows.reverse()
    total = len(rows)
    items = rows[offset: offset + limit]
    for i, rec in enumerate(items):
        rec["id"] = f"{rec.get('ts', '')}|{offset + i}"
    return {"total": total, "items": items, "limit": limit, "offset": offset}


def stats() -> dict:
    total = 0
    ok_n = 0
    fail_n = 0
    last = None
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                total += 1
                if rec.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
                last = rec
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("Failed to read upgrade log stats: %s", exc)
    return {
        "total": total,
        "ok": ok_n,
        "failed": fail_n,
        "last": last,
    }
