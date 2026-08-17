"""Re-apply local intranet customizations after a .zdoc import.

.zdoc packages replace all of /data. Fixes that live only in HTML/_zc are
lost unless we copy them back from site-overlay / config/overlay.

This module:
  1. copies the docs assistant + intranet-fixes.js onto site/_zc
  2. injects those assets into HTML that is missing them
  3. restores changelog/index.html when a package only shipped page-N.html
  4. writes site/_zc/version.json so the runtime fixer can find the .exe
"""

from __future__ import annotations

import json
import logging
import os
import shutil

log = logging.getLogger("zcode-ai.overlay")

_JS = "chat-widget.js"
_CSS = "chat-widget.css"
_FIX = "intranet-fixes.js"
_ASSETS = (_CSS, _JS, _FIX)
_LINK = '<link href="/_zc/chat-widget.css" rel="stylesheet"/>'
_SCRIPT = '<script defer src="/_zc/chat-widget.js"></script>'
_FIX_SCRIPT = '<script defer src="/_zc/intranet-fixes.js"></script>'


def _candidates() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.environ.get("CONFIG_DIR", "/config")
    return [
        os.environ.get("OVERLAY_DIR") or "",
        "/overlay",
        os.path.join(cfg, "overlay"),
        os.path.normpath(os.path.join(here, "..", "..", "site-overlay")),
        os.path.normpath(os.path.join(here, "..", "config", "overlay")),
    ]


def _find_overlay() -> str:
    for raw in _candidates():
        if not raw:
            continue
        path = os.path.abspath(raw)
        if os.path.isfile(os.path.join(path, _JS)) and os.path.isfile(os.path.join(path, _CSS)):
            return path
    return ""


def _copy_assets(overlay: str, site_dir: str) -> list:
    dest = os.path.join(site_dir, "_zc")
    os.makedirs(dest, exist_ok=True)
    copied = []
    extra_dirs = [d for d in _candidates() if d]
    for name in _ASSETS:
        src = os.path.join(overlay, name)
        if not os.path.isfile(src):
            for raw in extra_dirs:
                cand = os.path.join(os.path.abspath(raw), name)
                if os.path.isfile(cand):
                    src = cand
                    break
        if not os.path.isfile(src):
            continue
        shutil.copy2(src, os.path.join(dest, name))
        copied.append(name)
    cfg_overlay = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "overlay")
    try:
        os.makedirs(cfg_overlay, exist_ok=True)
        for name in copied:
            shutil.copy2(os.path.join(dest, name), os.path.join(cfg_overlay, name))
    except OSError as exc:
        log.debug("Could not persist overlay into config: %s", exc)
    return copied


def _inject_html(site_dir: str) -> int:
    changed = 0
    for dirpath, _dirs, files in os.walk(site_dir):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    html = fh.read()
            except OSError:
                continue
            original = html
            if _CSS not in html:
                html = html.replace("</head>", _LINK + "</head>", 1) if "</head>" in html else _LINK + html
            if _JS not in html:
                html = html.replace("</body>", _SCRIPT + "</body>", 1) if "</body>" in html else html + _SCRIPT
            if _FIX not in html:
                html = html.replace("</body>", _FIX_SCRIPT + "</body>", 1) if "</body>" in html else html + _FIX_SCRIPT
            if html != original:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(html)
                changed += 1
    return changed


def _write_version(data_dir: str, site_dir: str) -> str:
    ver = "unknown"
    manifest = os.path.join(data_dir, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            ver = str(json.load(fh).get("content_version") or "unknown")
    except (OSError, ValueError):
        pass
    dest = os.path.join(site_dir, "_zc")
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"content_version": ver}, fh)
    return ver


def ensure_changelog_index(site_dir: str) -> str:
    """If nav points at /changelog/ but index.html is missing, recover it."""
    folder = os.path.join(site_dir, "changelog")
    if not os.path.isdir(folder):
        return "missing-dir"
    index = os.path.join(folder, "index.html")
    if os.path.isfile(index) and os.path.getsize(index) > 1000:
        return "ok"
    for name in ("page-1.html", "page-2.html", "page-3.html"):
        src = os.path.join(folder, name)
        if os.path.isfile(src):
            shutil.copy2(src, index)
            log.info("Restored changelog/index.html from %s", name)
            return name
    return "unrecoverable"


def apply(data_dir: str | None = None) -> dict:
    """Copy overlay assets and restore local HTML fixes. Safe no-op if absent."""
    data_dir = data_dir or os.environ.get("DATA_DIR", "/data")
    site_dir = os.path.join(data_dir, "site")
    result = {"ok": True, "changelog": "skipped"}
    if not os.path.isdir(site_dir):
        log.warning("Site dir missing (%s); skip overlay", site_dir)
        return {"ok": False, "skipped": True, "reason": "no site"}

    result["changelog"] = ensure_changelog_index(site_dir)
    result["version"] = _write_version(data_dir, site_dir)

    overlay = _find_overlay()
    if not overlay:
        log.info("No docs-assistant overlay found; changelog/version still applied")
        result["skipped"] = True
        return result
    copied = _copy_assets(overlay, site_dir)
    injected = _inject_html(site_dir)
    result.update({"overlay": overlay, "copied": copied, "injected": injected})
    log.info(
        "Local intranet fixes applied from %s (%s, injected %d, changelog %s)",
        overlay, ", ".join(copied), injected, result["changelog"],
    )
    return result
