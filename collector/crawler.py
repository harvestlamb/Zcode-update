"""Crawl all /cn content routes, rewrite for offline use, and download assets.

This is the content orchestrator: it fetches each HTML page in the crawl list,
rewrites its internal URLs, writes the patched HTML to build/site/, and
downloads every first-party asset (CSS/JS/font/image) referenced by any page.

Asset downloads are deduplicated across all pages and resumable: an asset that
already exists on disk with the expected size is skipped.

Usage:
    python -m collector.crawler [--build-root BUILD_ROOT]

Writes:
    <build-root>/site/...         (mirrored HTML + assets)
"""

from __future__ import annotations

import argparse
import logging
import os
import posixpath
import re
import shutil
import sys
from typing import Dict, List, Set

from . import config
from .http_client import fetch_text, fetch_bytes, head
from .rewriter import rewrite_html
from .util import (
    setup_logging, build_paths, ensure_dir, normalise_url,
    url_to_site_path, local_path_for_asset, slug_from_url,
)

log = logging.getLogger("zcode-collector.crawl")

# Locale pages that flatten lifts to the site root. A previous successful
# collect leaves these at site/*, while a new crawl only rewrites site/cn/*.
# Drop the stale root copies before crawling so flatten won't collide.
_FLATTENED_LOCALE_ENTRIES = (
    "index.html",
    "docs",
    "changelog",
    "login",
    "privacy",
    "terms",
)


def _prune_stale_flattened_root(build_root: str) -> None:
    """Remove previously flattened locale pages sitting at the site root."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    if not os.path.isdir(site_dir):
        return
    for name in _FLATTENED_LOCALE_ENTRIES:
        path = os.path.join(site_dir, name)
        if not os.path.exists(path):
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        log.info("Removed stale flattened root entry: %s", name)


def _write_html(build_root: str, site_rel_dir: str, filename: str, html: str) -> str:
    """Write a rewritten HTML page under build/site/. Returns its disk path."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    abs_dir = os.path.normpath(os.path.join(site_dir, site_rel_dir.lstrip("/")))
    ensure_dir(abs_dir)
    out = os.path.join(abs_dir, filename)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out


def _asset_disk_path(build_root: str, asset_path: str) -> str:
    """Disk path under build/site/ for an asset URL-path (e.g. /_next/...)."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    return os.path.normpath(os.path.join(site_dir, asset_path.lstrip("/")))


def _download_asset(build_root: str, asset_path: str, stats: Dict[str, int]) -> None:
    """Download one first-party asset URL-path to build/site/<path>.

    Skips assets that already exist with non-zero size (idempotent re-runs).
    """
    disk = _asset_disk_path(build_root, asset_path)
    if os.path.exists(disk) and os.path.getsize(disk) > 0:
        stats["skipped"] += 1
        return

    url = config.SITE_BASE + asset_path
    # Cheap existence check for assets that may 404 (some referenced files
    # only exist via the image optimizer; we mirror the underlying file).
    try:
        data, _ctype = fetch_bytes(url, stream=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("  ! asset 404/err: %s (%s) — leaving reference, skipping", asset_path, exc)
        stats["failed"] += 1
        return

    ensure_dir(os.path.dirname(disk))
    with open(disk, "wb") as fh:
        fh.write(data)
    stats["downloaded"] += 1
    log.debug("  ✓ asset %s (%d bytes)", asset_path, len(data))


# CSS url() references (fonts, background images) are invisible to the HTML
# asset scan. This regex matches url(...) with an absolute site-relative path.
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?(\/[^)'\"]+)\s*['\"]?\)")


def _collect_css_url_refs(build_root: str, known_assets: Set[str]) -> Set[str]:
    """Scan every downloaded CSS file for url(/...) references.

    Returns first-party, root-relative paths that are not already in
    ``known_assets``. Only same-site absolute paths are returned (data: URIs,
    https external URLs, and relative urls are skipped).
    """
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    found: Set[str] = set()
    for dirpath, _dirs, files in os.walk(site_dir):
        for name in files:
            if not name.endswith(".css"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    css = fh.read()
            except Exception:  # noqa: BLE001
                continue
            for m in _CSS_URL_RE.finditer(css):
                ref = m.group(1)
                # only first-party, root-relative, file-like refs (skip /api, #, etc.)
                if ref.startswith("/") and not ref.startswith("/api/"):
                    found.add(ref)
    return found - known_assets


def _materialise_redirect_aliases(build_root: str) -> None:
    """Create index.html for routes that redirect on the live site.

    The live /cn/docs 307-redirects to /cn/docs/welcome. Links in the mirrored
    pages point at /cn/docs/index.html, so we copy the welcome page there. This
    keeps navigation working without a server-side redirect.
    """
    import shutil

    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    aliases = [
        # (source page, target index that nav links reference)
        ("cn/docs/welcome/index.html", "cn/docs/index.html"),
    ]
    for src_rel, dst_rel in aliases:
        src = os.path.join(site_dir, src_rel)
        dst = os.path.join(site_dir, dst_rel)
        if os.path.isfile(src) and not os.path.isfile(dst):
            ensure_dir(os.path.dirname(dst))
            shutil.copy2(src, dst)
            log.info("Created redirect alias %s -> %s", dst_rel, src_rel)


def crawl_all(build_root: str) -> dict:
    """Crawl every content route and all referenced first-party assets.

    Returns a summary dict with counts.
    """
    paths = build_paths(build_root)
    ensure_dir(paths["site"])
    _prune_stale_flattened_root(build_root)

    asset_paths: Set[str] = set()
    pages_written: List[str] = []

    # --- 1. Crawl HTML pages, rewrite, collect asset URLs ---
    total = len(config.CONTENT_ROUTES)
    log.info("Crawling %d content routes from %s ...", total, config.SITE_BASE)
    for i, route in enumerate(config.CONTENT_ROUTES, 1):
        url = config.SITE_BASE + route
        log.info("[%d/%d] %s", i, total, route)
        try:
            html = fetch_text(url)
        except Exception as exc:  # noqa: BLE001
            log.error("  ! failed to fetch %s: %s", route, exc)
            continue

        new_html, page_assets = rewrite_html(html, url)
        asset_paths.update(page_assets)

        site_rel_dir, filename = url_to_site_path(route)
        disk = _write_html(build_root, site_rel_dir, filename, new_html)
        pages_written.append(disk)
        log.debug("  wrote %s", os.path.relpath(disk, build_root))

    # --- 1b. Materialise redirect targets that the site exposes as nav hubs. ---
    # The live /cn/docs 307-redirects to /cn/docs/welcome. We did not crawl that
    # route (it has no content of its own), but several links point at
    # /cn/docs/index.html. Copy the welcome page so those links resolve to real
    # content instead of falling through to the homepage via nginx try_files.
    _materialise_redirect_aliases(build_root)

    # --- 2. Download all collected assets (deduped, resumable) ---
    stats = {"downloaded": 0, "skipped": 0, "failed": 0}
    log.info("Downloading %d unique first-party assets ...", len(asset_paths))
    for asset in sorted(asset_paths):
        _download_asset(build_root, asset, stats)

    # --- 2b. Scan downloaded CSS for url() references (fonts etc.) ---
    # CSS @font-face uses url(/_next/static/media/x.woff2) which the HTML scan
    # cannot see. Parse each downloaded CSS, queue newly-discovered first-party
    # url() targets, and download them in a second pass.
    extra_assets = _collect_css_url_refs(build_root, asset_paths)
    if extra_assets:
        log.info("CSS scan found %d extra assets (fonts, etc.) ...", len(extra_assets))
        for asset in sorted(extra_assets):
            _download_asset(build_root, asset, stats)
        asset_paths.update(extra_assets)

    log.info("Done. Pages: %d | assets downloaded: %d, skipped: %d, failed: %d",
             len(pages_written), stats["downloaded"], stats["skipped"], stats["failed"])

    # Persist the asset list so manifest.py can record it without re-parsing.
    assets_file = os.path.join(build_root, ".assets.txt")
    with open(assets_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(asset_paths)))

    return {
        "pages": len(pages_written),
        "page_paths": pages_written,
        "assets": sorted(asset_paths),
        "asset_stats": stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl ZCode /cn docs into a static mirror.")
    parser.add_argument("--build-root", default="build", help="output build directory")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)
    log.info("Build root: %s", build_root)
    crawl_all(build_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
