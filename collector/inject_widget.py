"""Inject the AI chat widget into every mirrored HTML page.

Copies the site-overlay assets (chat-widget.js / .css) into build/site/ and
adds a <link> + <script> reference to each .html file. Idempotent: pages that
already reference the widget are left alone.

Run after patch_offline (so the widget sees the patched HTML) and before
manifest/export (so the injected assets are hashed into the package).

Usage:
    python -m collector.inject_widget [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from bs4 import BeautifulSoup

from . import config
from .util import setup_logging

log = logging.getLogger("zcode-collector.inject")

# Where the overlay assets live, relative to the collector package.
_OVERLAY_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "site-overlay"))
_ASSET_FILES = ["chat-widget.css", "chat-widget.js"]
# Markers to detect prior injection.
_JS_MARKER = "chat-widget.js"
_CSS_MARKER = "chat-widget.css"


def _copy_assets(build_root: str) -> list:
    """Copy overlay assets into build/site/_zc/ . Returns their site-relative paths."""
    dest_dir = os.path.join(build_root, config.BUILD_SITE_DIR, "_zc")
    os.makedirs(dest_dir, exist_ok=True)
    copied = []
    for fn in _ASSET_FILES:
        src = os.path.join(_OVERLAY_DIR, fn)
        if not os.path.isfile(src):
            log.warning("Overlay asset missing: %s", src)
            continue
        with open(src, encoding="utf-8") as fh:
            data = fh.read()
        with open(os.path.join(dest_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(data)
        copied.append("_zc/" + fn)
    return copied


def _inject_into_html(html_path: str) -> bool:
    """Add <link>/<script> for the widget if absent. Returns True if changed."""
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    if _JS_MARKER in html and _CSS_MARKER in html:
        return False

    soup = BeautifulSoup(html, "lxml")
    head = soup.head or soup.new_tag("head")
    if not soup.head:
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)

    # CSS
    if _CSS_MARKER not in html:
        link = soup.new_tag("link", rel="stylesheet", href="/_zc/chat-widget.css")
        head.append(link)
    # JS (at end of body so the DOM is ready; if no body, append to head)
    if _JS_MARKER not in html:
        script = soup.new_tag("script", src="/_zc/chat-widget.js", defer=None)
        if soup.body:
            soup.body.append(script)
        else:
            head.append(script)

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(str(soup))
    return True


def inject_all(build_root: str) -> dict:
    """Copy overlay assets and inject references into all mirrored HTML."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    copied = _copy_assets(build_root)
    log.info("Copied overlay assets: %s", ", ".join(copied) or "(none)")

    changed = 0
    total = 0
    for dirpath, _dirs, files in os.walk(site_dir):
        for name in files:
            if not name.endswith(".html"):
                continue
            total += 1
            if _inject_into_html(os.path.join(dirpath, name)):
                changed += 1
    log.info("Injected widget into %d of %d HTML pages.", changed, total)
    return {"assets": copied, "pages_changed": changed, "pages_total": total}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject the AI chat widget into mirrored pages.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    inject_all(os.path.abspath(args.build_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
