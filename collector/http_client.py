"""HTTP fetch helper with retries, polite delays, and binary support.

All collector scripts import :func:`fetch_text` / :func:`fetch_bytes` instead of
using ``requests`` directly, so retry policy and headers live in one place.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import requests

from . import config

log = logging.getLogger("zcode-collector.http")

# A module-level session reuses connections across requests.
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
    return _session


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Run a request with exponential backoff on retriable failures."""
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
    session = _get_session()
    last_exc: Optional[Exception] = None
    for attempt in range(1, config.REQUEST_RETRIES + 1):
        try:
            resp = session.request(method, url, **kwargs)
            # Retry on 5xx and 429; everything else is a definitive answer.
            if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            wait = 2 ** (attempt - 1)
            log.warning("  attempt %d/%d failed for %s: %s (retry in %ds)",
                        attempt, config.REQUEST_RETRIES, url, exc, wait)
            if attempt < config.REQUEST_RETRIES:
                time.sleep(wait)
    raise RuntimeError(f"Request failed after {config.REQUEST_RETRIES} attempts: {url}") from last_exc


def fetch_text(url: str) -> str:
    """Fetch a URL and return decoded text, following redirects."""
    resp = _request_with_retry("GET", url)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    time.sleep(config.REQUEST_DELAY)
    return resp.text


def fetch_bytes(url: str, stream: bool = True) -> Tuple[bytes, str]:
    """Fetch a URL and return (raw_bytes, content_type). Used for assets/binaries."""
    resp = _request_with_retry("GET", url, stream=stream)
    resp.raise_for_status()
    data = resp.content
    content_type = resp.headers.get("Content-Type", "")
    time.sleep(config.REQUEST_DELAY)
    return data, content_type


def head(url: str) -> Optional[int]:
    """Return the HTTP status code via HEAD, or None on failure.

    Used by collectors to cheaply probe whether an asset URL exists before
    downloading (e.g. per-version latest.yml manifests, which are inconsistent).
    """
    try:
        resp = _request_with_retry("HEAD", url, allow_redirects=True)
        return resp.status_code
    except Exception as exc:  # noqa: BLE001 - probe best-effort
        log.debug("  HEAD %s -> error %s", url, exc)
        return None
