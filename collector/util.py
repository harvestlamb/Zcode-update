"""Filesystem, URL, and logging helpers shared across collector scripts."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
import sys
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, urldefrag, quote

from . import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure console logging once for the whole collector process."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout,
                        datefmt="%H:%M:%S", force=True)
    return logging.getLogger("zcode-collector")


# ---------------------------------------------------------------------------
# Build directory helpers
# ---------------------------------------------------------------------------


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def build_paths(build_root: str) -> dict:
    """Return absolute paths to the standard build artefact directories."""
    return {
        "root": build_root,
        "site": os.path.join(build_root, config.BUILD_SITE_DIR),
        "content": os.path.join(build_root, config.BUILD_CONTENT_DIR),
        "releases": os.path.join(build_root, config.BUILD_RELEASES_DIR),
        "manifest": os.path.join(build_root, config.BUILD_MANIFEST),
        "sha256sums": os.path.join(build_root, config.BUILD_SHA256SUMS),
    }


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

_SAFE_CHARS = "/:-_.~@!$&'()*+,;="  # don't double-encode these


def normalise_url(url: str) -> str:
    """Strip fragment, drop default ports, collapse nothing else.

    Used as the dedup key when collecting asset URLs to fetch.
    """
    url, _frag = urldefrag(url)
    return url


def is_same_site(url: str) -> bool:
    """True if url is on zcode.z.ai (any scheme/path)."""
    host = urlsplit(url).netloc.lower()
    return host in ("zcode.z.ai", "")


def absolutise(href: str, base: str) -> str:
    """Resolve a possibly-relative URL against a page base URL."""
    # urljoin drops the query when joining a path, so handle carefully.
    from urllib.parse import urljoin
    return urljoin(base, href)


def url_to_site_path(url: str) -> Tuple[str, str]:
    """Map a zcode.z.ai URL to (directory, filename) under build/site/.

    Rules:
      - /cn/docs/welcome                 -> (cn/docs/welcome, "index.html")
      - /cn/docs/welcome?x=1             -> (cn/docs/welcome, "index.html")
      - /cn                              -> (cn, "index.html")
      - /cn/changelog?page=2             -> (cn/changelog, "page-2.html")
                                               (each page snapshot saved separately)
      - /_next/static/...                -> (_next/static/..., basename)
      - /images/foo.png                  -> (images/foo.png, "")  -> see below
      - /content/docs/x.png              -> (content/docs, "x.png")

    Returns (abs_dir_under_site, filename). Callers join and mkdir as needed.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    query = parts.query

    # _next/static assets: mirror the exact path (it's cache-keyed by hash).
    if path.startswith("/_next/static/"):
        return path.lstrip("/"), ""

    # Image / content files: keep their directory structure verbatim.
    if path.startswith("/images/") or path.startswith("/content/"):
        return path.lstrip("/"), ""

    # HTML pages.
    # Treat /cn and /cn/docs as directory roots.
    if path in ("/cn", "/cn/", "/cn/docs", "/cn/docs/"):
        rel_dir = path.rstrip("/") if path != "/" else "cn"
        return rel_dir, "index.html"

    # /cn/<x> or /cn/docs/<slug>
    if path.startswith("/cn/"):
        rel = path.lstrip("/")  # e.g. "cn/changelog" or "cn/docs/welcome"
        # changelog with ?page=N: snapshot each page as its own file so every
        # version of the cumulative list is preserved offline. page=1 (the
        # newest 8 versions) is saved as index.html so nav links resolve to it.
        if rel == "cn/changelog" and query:
            m = re.search(r"page=(\d+)", query)
            page = int(m.group(1)) if m else 1
            fname = "index.html" if page == 1 else f"page-{page}.html"
            return "cn/changelog", fname
        return rel, "index.html"

    # Fallback: keep path, append index.html
    return path.lstrip("/"), "index.html"


def local_path_for_asset(asset_path: str) -> str:
    """Path on disk under site/ for a first-party asset URL-path.

    Asset paths like /_next/static/css/x.css, /images/a.png, /content/docs/b.png
    are stored verbatim relative to site/.
    """
    assert asset_path.startswith("/"), asset_path
    return asset_path.lstrip("/")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def slug_from_url(url: str) -> str:
    """Extract the docs slug from a /cn/docs/<slug> URL, or '' otherwise."""
    parts = urlsplit(url)
    m = re.match(r"^/cn/docs/([^/]+)/?$", parts.path)
    return m.group(1) if m else ""


def safe_filename(name: str) -> str:
    """Make a string safe to use as a single path component."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "index"
