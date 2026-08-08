"""Append-only ask/access log for the intranet AI assistant.

Stored as JSON Lines under CONFIG_DIR so content imports never wipe history.
Each /api/ask records client IP, user-agent, question, model, and answer summary.

Read path is optimized for larger volumes: reverse tail reads for unfiltered
pages, streaming windows for filters, and mtime-keyed caches for stats/heatmap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("zcode-ai.asklog")

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
LOG_FILE = os.path.join(CONFIG_DIR, "ask_logs.jsonl")
MAX_ANSWER_CHARS = 4000
MAX_QUESTION_CHARS = 2000
ANSWER_PREVIEW_CHARS = 180
DEFAULT_READ_LIMIT = 200
HARD_CAP_LINES = 50000  # trim oldest when file grows past this
TRIM_SIZE_TRIGGER = 20 * 1024 * 1024  # ~20MB before considering trim
TAIL_CHUNK = 64 * 1024

_lock = threading.Lock()
_trim_pending = False

# Cached aggregates keyed by (mtime_ns, size).
_stats_cache: Optional[Tuple[Tuple[int, int], dict]] = None
_stats_ips: Optional[set] = None
_stats_day: Optional[str] = None
_heatmap_cache: Dict[int, Tuple[Tuple[int, int], dict]] = {}
_line_count_cache: Optional[Tuple[Tuple[int, int], int]] = None


def _ensure_dir() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create config dir %s: %s", CONFIG_DIR, exc)


def _file_key() -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(LOG_FILE)
        return (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    except OSError:
        return None


def _invalidate_caches() -> None:
    global _stats_cache, _stats_ips, _stats_day, _heatmap_cache, _line_count_cache
    _stats_cache = None
    _stats_ips = None
    _stats_day = None
    _heatmap_cache = {}
    _line_count_cache = None


def client_meta(request) -> dict:
    """Extract LAN client identity from the proxied request."""
    ip = ""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        ip = xff.split(",")[0].strip()
    if not ip:
        ip = (request.headers.get("x-real-ip") or "").strip()
    if not ip and request.client:
        ip = request.client.host or ""
    ua = (request.headers.get("user-agent") or "")[:300]
    return {"ip": ip or "unknown", "user_agent": ua}


def _norm_usage(usage: Optional[dict]) -> dict:
    u = usage or {}
    try:
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        total = int(u.get("total_tokens") or 0)
    except (TypeError, ValueError):
        prompt = completion = total = 0
    if total <= 0:
        total = prompt + completion
    return {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
        "estimated": bool(u.get("estimated")),
    }


def append(
    *,
    ip: str,
    user_agent: str,
    question: str,
    model_id: str = "",
    model: str = "",
    label: str = "",
    answer: str = "",
    ok: bool = True,
    error: str = "",
    sources: Optional[List[str]] = None,
    backend: str = "",
    usage: Optional[dict] = None,
) -> dict:
    """Append one ask event. Returns the written record."""
    tok = _norm_usage(usage)
    rec = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "ip": ip or "unknown",
        "user_agent": (user_agent or "")[:300],
        "question": (question or "")[:MAX_QUESTION_CHARS],
        "model_id": model_id or "",
        "model": model or "",
        "label": label or "",
        "ok": bool(ok),
        "error": (error or "")[:500],
        "answer": (answer or "")[:MAX_ANSWER_CHARS],
        "sources": list(sources or [])[:10],
        "backend": backend or "",
        "prompt_tokens": tok["prompt_tokens"],
        "completion_tokens": tok["completion_tokens"],
        "total_tokens": tok["total_tokens"],
        "tokens_estimated": tok["estimated"],
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        _ensure_dir()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            log.error("Failed to write ask log: %s", exc)
            return rec
        _bump_stats_incremental(rec)
        _schedule_trim()
    return rec


def _bump_stats_incremental(rec: dict) -> None:
    """Update in-memory stats after a successful append (caller holds _lock)."""
    global _stats_cache, _stats_ips, _stats_day, _heatmap_cache, _line_count_cache
    key = _file_key()
    day = datetime.now().astimezone().date().isoformat()
    tt = _rec_tokens(rec)
    try:
        pt = int(rec.get("prompt_tokens") or 0)
        ct = int(rec.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        pt = ct = 0

    if _stats_cache is not None and _stats_day == day and _stats_ips is not None:
        base = dict(_stats_cache[1])
        base["total"] = int(base.get("total") or 0) + 1
        base["today"] = int(base.get("today") or 0) + 1
        base["tokens_total"] = int(base.get("tokens_total") or 0) + tt
        base["tokens_today"] = int(base.get("tokens_today") or 0) + tt
        base["prompt_tokens"] = int(base.get("prompt_tokens") or 0) + max(0, pt)
        base["completion_tokens"] = int(base.get("completion_tokens") or 0) + max(0, ct)
        ip = rec.get("ip")
        if ip:
            _stats_ips.add(ip)
        base["unique_ips"] = len(_stats_ips)
        if key is not None:
            _stats_cache = (key, base)
    else:
        _stats_cache = None
        _stats_ips = None
        _stats_day = None

    # Heatmap and line-count need a rescan or cheap bump; invalidate heatmap.
    _heatmap_cache = {}
    if _line_count_cache is not None and key is not None:
        _line_count_cache = (key, int(_line_count_cache[1]) + 1)
    else:
        _line_count_cache = None


def _schedule_trim() -> None:
    """Mark trim needed; run off the request path when size looks large."""
    global _trim_pending
    try:
        if os.path.getsize(LOG_FILE) < TRIM_SIZE_TRIGGER:
            return
    except OSError:
        return
    if _trim_pending:
        return
    _trim_pending = True
    threading.Thread(target=_trim_worker, name="ask-log-trim", daemon=True).start()


def _trim_worker() -> None:
    global _trim_pending
    try:
        with _lock:
            _maybe_trim_locked()
            _invalidate_caches()
    finally:
        _trim_pending = False


def _maybe_trim_locked() -> None:
    """Keep the file from growing without bound (best-effort). Caller holds _lock."""
    try:
        if os.path.getsize(LOG_FILE) < TRIM_SIZE_TRIGGER:
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
        log.info("Trimmed ask log to %d lines.", len(keep))
    except OSError:
        pass


def _parse_ok_filter(ok: Optional[Any]) -> Optional[bool]:
    if ok is None or ok == "":
        return None
    if isinstance(ok, bool):
        return ok
    s = str(ok).strip().lower()
    if s in ("", "all", "any", "*"):
        return None
    if s in ("1", "true", "yes", "ok", "success"):
        return True
    if s in ("0", "false", "no", "fail", "failed", "error"):
        return False
    return None


def _parse_days(days: Optional[Any]) -> Optional[int]:
    """Parse days window: 7 / 30 / etc. None means all time."""
    if days is None or days == "":
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(1, min(n, 366))


def _days_cutoff(days: Optional[int]) -> Optional[datetime]:
    """Inclusive local-day cutoff for「近 N 天」(today + previous N-1 days)."""
    if not days:
        return None
    now = datetime.now().astimezone()
    start = now.date() - timedelta(days=int(days) - 1)
    return datetime(start.year, start.month, start.day, tzinfo=now.tzinfo)


def _matches(
    rec: dict,
    ip_filter: str,
    needle: str,
    ok_filter: Optional[bool],
    since: Optional[datetime] = None,
) -> bool:
    if since is not None:
        dt = _parse_local_dt(str(rec.get("ts") or ""))
        if not dt or dt < since:
            return False
    if ip_filter and rec.get("ip") != ip_filter:
        return False
    if ok_filter is not None and bool(rec.get("ok", True)) != ok_filter:
        return False
    if needle:
        blob = f"{rec.get('question', '')} {rec.get('answer', '')} {rec.get('ip', '')}".lower()
        if needle not in blob:
            return False
    return True


def _answer_preview(answer: str) -> str:
    text = answer or ""
    if len(text) <= ANSWER_PREVIEW_CHARS:
        return text
    return text[:ANSWER_PREVIEW_CHARS].rstrip() + "…"


def _to_list_item(rec: dict, index: int) -> dict:
    """Lightweight row for list API (no full answer / UA)."""
    return {
        "id": f"{rec.get('ts', '')}|{rec.get('ip', '')}|{index}",
        "ts": rec.get("ts") or "",
        "ip": rec.get("ip") or "unknown",
        "question": rec.get("question") or "",
        "model_id": rec.get("model_id") or "",
        "model": rec.get("model") or "",
        "label": rec.get("label") or "",
        "ok": bool(rec.get("ok", True)),
        "error": (rec.get("error") or "")[:200],
        "answer_preview": _answer_preview(str(rec.get("answer") or "")),
        "backend": rec.get("backend") or "",
        "prompt_tokens": rec.get("prompt_tokens") or 0,
        "completion_tokens": rec.get("completion_tokens") or 0,
        "total_tokens": rec.get("total_tokens") or 0,
        "tokens_estimated": bool(rec.get("tokens_estimated")),
        "sources_count": len(rec.get("sources") or []),
    }


def _count_lines_cached() -> int:
    """Return non-empty line count, cached by mtime/size."""
    global _line_count_cache
    key = _file_key()
    if key is None:
        return 0
    if _line_count_cache is not None and _line_count_cache[0] == key:
        return _line_count_cache[1]
    total = 0
    try:
        with open(LOG_FILE, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                total += chunk.count(b"\n")
            # Last line without trailing newline still counts if file non-empty.
            if key[1] > 0:
                fh.seek(max(0, key[1] - 1))
                if fh.read(1) != b"\n":
                    total += 1
    except FileNotFoundError:
        total = 0
    except OSError as exc:
        log.error("Failed to count ask log lines: %s", exc)
        total = 0
    _line_count_cache = (key, total)
    return total


def _iter_jsonl_forward():
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return
    except OSError as exc:
        log.error("Failed to read ask log: %s", exc)
        return


def _read_newest(need: int) -> List[dict]:
    """Read up to `need` newest records by scanning the file from the end."""
    if need <= 0:
        return []
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            if pos == 0:
                return []
            buf = b""
            rows: List[dict] = []
            while pos > 0 and len(rows) < need:
                read_size = min(TAIL_CHUNK, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                buf = chunk + buf
                parts = buf.split(b"\n")
                # Keep incomplete head for next iteration.
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if len(rows) >= need:
                        break
            if len(rows) < need and buf.strip():
                try:
                    rows.append(json.loads(buf.strip().decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    pass
            return rows[:need]
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.error("Failed to tail ask log: %s", exc)
        return []


def _iter_newest_records():
    """Yield parsed records newest-first by scanning the file from the end."""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            if pos == 0:
                return
            buf = b""
            while pos > 0:
                read_size = min(TAIL_CHUNK, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                buf = chunk + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
            if buf.strip():
                try:
                    yield json.loads(buf.strip().decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return
    except FileNotFoundError:
        return
    except OSError as exc:
        log.error("Failed to reverse-read ask log: %s", exc)
        return


def list_logs(
    *,
    limit: int = DEFAULT_READ_LIMIT,
    offset: int = 0,
    ip: Optional[str] = None,
    q: Optional[str] = None,
    ok: Optional[Any] = None,
    days: Optional[Any] = None,
) -> dict:
    """Return newest-first records with simple filters (lightweight items)."""
    limit = max(1, min(int(limit or DEFAULT_READ_LIMIT), 1000))
    offset = max(0, int(offset or 0))
    needle = (q or "").strip().lower()
    ip_filter = (ip or "").strip()
    ok_filter = _parse_ok_filter(ok)
    days_n = _parse_days(days)
    since = _days_cutoff(days_n)
    filtered = bool(needle or ip_filter or ok_filter is not None or since is not None)

    if not filtered:
        total = _count_lines_cached()
        if total == 0:
            return {"total": 0, "items": [], "limit": limit, "offset": offset, "days": None}
        need = offset + limit
        newest = _read_newest(need)
        page = newest[offset: offset + limit]
        items = [_to_list_item(rec, offset + i) for i, rec in enumerate(page)]
        return {"total": total, "items": items, "limit": limit, "offset": offset, "days": None}

    # Time window: reverse-scan and stop once past cutoff (file is append-ordered).
    if since is not None:
        page: List[dict] = []
        total = 0
        try:
            for rec in _iter_newest_records():
                dt = _parse_local_dt(str(rec.get("ts") or ""))
                if dt is not None and dt < since:
                    break
                if not _matches(rec, ip_filter, needle, ok_filter, since=since):
                    continue
                if total >= offset and len(page) < limit:
                    page.append(rec)
                total += 1
        except Exception as exc:  # noqa: BLE001
            log.error("Failed days-filtered list: %s", exc)
            return {
                "total": 0, "items": [], "limit": limit, "offset": offset,
                "days": days_n, "error": str(exc),
            }
        items = [_to_list_item(rec, offset + i) for i, rec in enumerate(page)]
        return {"total": total, "items": items, "limit": limit, "offset": offset, "days": days_n}

    # Other filters, all-time: forward pass with sliding window.
    window: Deque[dict] = deque(maxlen=offset + limit)
    total = 0
    try:
        for rec in _iter_jsonl_forward():
            if not _matches(rec, ip_filter, needle, ok_filter, since=None):
                continue
            total += 1
            window.append(rec)
    except Exception as exc:  # noqa: BLE001 — surface as empty page
        log.error("Failed filtered list: %s", exc)
        return {
            "total": 0, "items": [], "limit": limit, "offset": offset,
            "days": None, "error": str(exc),
        }

    newest_first = list(reversed(window))
    page = newest_first[offset: offset + limit]
    items = [_to_list_item(rec, offset + i) for i, rec in enumerate(page)]
    return {"total": total, "items": items, "limit": limit, "offset": offset, "days": None}


def get_detail(*, ts: str = "", ip: str = "") -> Optional[dict]:
    """Return the newest matching full record for ts (+ optional ip).

    Searches from the file tail first (recent asks) before falling back to a
    full forward scan for older rows.
    """
    ts = (ts or "").strip()
    ip_filter = (ip or "").strip()
    if not ts:
        return None

    # Recent rows are the common case when expanding a list item.
    for rec in _read_newest(400):
        if str(rec.get("ts") or "") != ts:
            continue
        if ip_filter and rec.get("ip") != ip_filter:
            continue
        out = dict(rec)
        out["id"] = f"{out.get('ts', '')}|{out.get('ip', '')}|detail"
        return out

    found: Optional[dict] = None
    for rec in _iter_jsonl_forward():
        if str(rec.get("ts") or "") != ts:
            continue
        if ip_filter and rec.get("ip") != ip_filter:
            continue
        found = rec
    if found is None:
        return None
    out = dict(found)
    out["id"] = f"{out.get('ts', '')}|{out.get('ip', '')}|detail"
    return out


def stats() -> dict:
    """Lightweight counters for the overview cards (mtime-cached)."""
    global _stats_cache, _stats_ips, _stats_day
    key = _file_key()
    day = datetime.now().astimezone().date().isoformat()
    if (
        key is not None
        and _stats_cache is not None
        and _stats_cache[0] == key
        and _stats_day == day
    ):
        return dict(_stats_cache[1])

    total = 0
    ips: set = set()
    today = 0
    tokens_total = 0
    tokens_today = 0
    prompt_total = 0
    completion_total = 0
    try:
        for rec in _iter_jsonl_forward():
            total += 1
            if rec.get("ip"):
                ips.add(rec["ip"])
            try:
                pt = int(rec.get("prompt_tokens") or 0)
                ct = int(rec.get("completion_tokens") or 0)
                tt = int(rec.get("total_tokens") or 0) or (pt + ct)
            except (TypeError, ValueError):
                pt = ct = tt = 0
            prompt_total += pt
            completion_total += ct
            tokens_total += tt
            ts = str(rec.get("ts") or "")
            if ts.startswith(day):
                today += 1
                tokens_today += tt
    except Exception:  # noqa: BLE001
        pass

    result = {
        "total": total,
        "unique_ips": len(ips),
        "today": today,
        "tokens_total": tokens_total,
        "tokens_today": tokens_today,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
    }
    if key is not None:
        _stats_cache = (key, result)
        _stats_ips = ips
        _stats_day = day
        _line_count_cache_set(key, total)
    return dict(result)


def _line_count_cache_set(key: Tuple[int, int], total: int) -> None:
    global _line_count_cache
    _line_count_cache = (key, total)


def _rec_tokens(rec: dict) -> int:
    try:
        pt = int(rec.get("prompt_tokens") or 0)
        ct = int(rec.get("completion_tokens") or 0)
        tt = int(rec.get("total_tokens") or 0) or (pt + ct)
        return max(0, tt)
    except (TypeError, ValueError):
        return 0


def _parse_local_dt(ts: str) -> Optional[datetime]:
    """Parse ISO timestamps into aware local datetimes."""
    if not ts:
        return None
    raw = str(ts).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except ValueError:
        return None


def heatmap(days: int = 84) -> dict:
    """Aggregate token usage for calendar + weekday×hour heatmaps (mtime-cached)."""
    global _heatmap_cache
    days = max(7, min(int(days or 84), 366))
    key = _file_key()
    cached = _heatmap_cache.get(days)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]

    today = datetime.now().astimezone().date()
    start = today - timedelta(days=days - 1)

    by_day: Dict[str, Dict[str, int]] = {}
    dow_hour = [[0 for _ in range(24)] for _ in range(7)]

    try:
        for rec in _iter_jsonl_forward():
            dt = _parse_local_dt(str(rec.get("ts") or ""))
            if not dt:
                continue
            d = dt.date()
            if d < start or d > today:
                continue
            tok = _rec_tokens(rec)
            dkey = d.isoformat()
            bucket = by_day.setdefault(dkey, {"tokens": 0, "asks": 0})
            bucket["tokens"] += tok
            bucket["asks"] += 1
            dow_hour[dt.weekday()][dt.hour] += tok
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to build heatmap: %s", exc)

    day_list = []
    max_tokens = 0
    cur = start
    while cur <= today:
        dkey = cur.isoformat()
        bucket = by_day.get(dkey, {"tokens": 0, "asks": 0})
        max_tokens = max(max_tokens, bucket["tokens"])
        day_list.append({
            "date": dkey,
            "tokens": bucket["tokens"],
            "asks": bucket["asks"],
            "weekday": cur.weekday(),
            "js_weekday": (cur.weekday() + 1) % 7,
        })
        cur += timedelta(days=1)

    max_dow = max((v for row in dow_hour for v in row), default=0)
    result = {
        "days": day_list,
        "max_tokens": max_tokens,
        "dow_hour": dow_hour,
        "max_dow_hour": max_dow,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "range_days": days,
    }
    if key is not None:
        _heatmap_cache[days] = (key, result)
    return result


def clear() -> dict:
    """Wipe the log file (admin action)."""
    with _lock:
        try:
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
        except OSError as exc:
            raise RuntimeError(str(exc)) from exc
        _invalidate_caches()
    return {"ok": True, "cleared_at": time.time()}
