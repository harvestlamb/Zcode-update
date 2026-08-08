"""Flatten the ``cn/`` locale directory up to the site root.

The crawler writes every Chinese page under ``build/site/cn/...`` (mirroring
the live site's ``/cn`` locale prefix). For an offline intranet mirror there is
only one locale, so the ``/cn`` prefix is pure noise in the URL bar. This step
lifts ``cn/*`` to the site root and rewrites every in-page reference so the
site works with no ``/cn`` segment at all — ``/`` is the homepage,
``/docs/agents`` is a doc page, etc.

What it does
------------
1. Move ``site/cn/<sub>`` → ``site/<sub>`` for every entry under ``cn/``
   (``index.html``, ``docs/``, ``changelog/``, ``login/``, ``privacy/``,
   ``terms/``). Conflicts with existing root entries (``_next``, ``_zc``,
   ``images``, ``favicon*``, ``apple-icon.png``) are refused — the locale
   content never names those, so this is just a safety check.

2. Rewrite relative paths inside the moved HTML/CSS. Moving every file up one
   directory means each ``../`` climbs one level too many, so every relative
   path loses one ``../`` segment. References that pointed at sibling ``cn/``
   pages additionally drop the ``cn/`` component:

       (in a page formerly at cn/docs/x/, now at docs/x/)
       "../../_next/..."   ->  "../_next/..."      (one less ../)
       "../../../cn/docs/y/index.html"
                          ->  "../../docs/y/index.html"  (one less ../ + drop cn/)

3. Drop ``/cn`` from any residual site-absolute paths and from DOM ids /
   ``og:url`` / canonical metadata so the sidebar anchor ids and SEO tags no
   longer carry the prefix.

Run AFTER ``relativise`` (so inputs are already relative) and BEFORE
``manifest`` (so the SHA-256 sums are computed on the final, flattened tree).

Idempotent: if ``cn/`` is already gone, it is a no-op.

Usage:
    python -m collector.flatten_cn [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys

from . import config
from .util import setup_logging

log = logging.getLogger("zcode-collector.flatten")

SITE_DIR_NAME = config.BUILD_SITE_DIR  # "site"
LOCALE_DIR = "cn"

# Root-level entries the locale must not shadow (safety; locale never names these).
_PROTECTED_ROOT = {
    "_next", "_zc", "images", "releases", "content",
    "favicon.ico", "apple-icon.png",
    "favicon-16x16.png", "favicon-32x16.png", "favicon-32x32.png",
    "favicon-48x48.png", "favicon-192x192.png",
}


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

# Match href="..." / src="..." values. We rewrite ones that are relative
# (start with ../ ) or site-absolute (start with / ). Captures the value.
_ATTR_RE = re.compile(
    r'''(?P<attr>\b(?:href|src)\s*=\s*)(?P<q>["'])(?P<val>[^"']*)(?P=q)'''
)
# srcset="a 1x, b 2x"
_SRCSET_RE = re.compile(r'''(?P<attr>\bsrcset\s*=\s*)(?P<q>["'])(?P<val>[^"']*)(?P=q)''')
# url(...) in inline style / <style> / .css
_CSS_URL_RE = re.compile(r'''url\(\s*(?P<q>["']?)(?P<val>[^)"'\s]+)(?P=q)\s*\)''')


def _rewrite_path_value(val: str) -> str:
    """Rewrite a single path value (href/src/url/srcset-item) for the flatten.

    Rules:
      * ``../cn/<rest>``   -> ``<rest>``            (sibling locale page, drop cn + one ../)
      * ``../../cn/<rest>``-> ``../<rest>``         (deeper sibling locale page)
      * more generally, ``(../+)cn/<rest>`` -> drop one ``../`` AND the ``cn/``
      * any other ``(../+)<rest>`` -> drop exactly one ``../``  (resource ref)
      * site-absolute ``/cn/<rest>`` -> ``/<rest>``
      * site-absolute ``/<rest>`` (no cn) -> unchanged (already root-relative)
    Query/fragment are preserved.
    """
    # Preserve query/fragment.
    query = ""
    frag = ""
    path = val
    hi = path.find("#")
    if hi != -1:
        frag = path[hi:]
        path = path[:hi]
    qi = path.find("?")
    if qi != -1:
        query = path[qi:]
        path = path[:qi]

    if not path:
        return val

    # --- site-absolute ---
    if path.startswith("/") and not path.startswith("//"):
        # /cn/<rest> -> /<rest> ; other /X unchanged
        if path.startswith("/" + LOCALE_DIR + "/"):
            path = "/" + path[len(LOCALE_DIR) + 2:]
        elif path == "/" + LOCALE_DIR:
            path = "/"
        return path + query + frag

    # --- relative: count leading ../ ---
    m = re.match(r"^(?P<dots>(?:\.\./)+)(?P<rest>.*)$", path)
    if not m:
        return val  # not a ../ path; leave alone

    dots = m.group("dots")
    rest = m.group("rest")
    n = dots.count("../")

    # If rest begins with "cn/", that segment disappears too.
    if rest == LOCALE_DIR:
        rest = ""
    elif rest.startswith(LOCALE_DIR + "/"):
        rest = rest[len(LOCALE_DIR) + 1:]

    # Remove one ../ (file moved up one dir). Floor at 0.
    n = max(n - 1, 0)
    new_path = ("../" * n) + rest
    return new_path + query + frag


def _rewrite_html(html: str) -> tuple:
    """Rewrite all path-bearing attributes + inline url() in one HTML doc."""
    n = [0]

    def _attr_sub(m: re.Match) -> str:
        val = m.group("val")
        new = _rewrite_path_value(val)
        if new != val:
            n[0] += 1
        return f'{m.group("attr")}{m.group("q")}{new}{m.group("q")}'

    def _srcset_sub(m: re.Match) -> str:
        items = []
        changed = False
        for item in m.group("val").split(","):
            item = item.strip()
            if not item:
                continue
            parts = item.split()
            url = parts[0]
            desc = " ".join(parts[1:])
            new = _rewrite_path_value(url)
            if new != url:
                changed = True
                n[0] += 1
            items.append(f"{new} {desc}".strip())
        return f'{m.group("attr")}{m.group("q")}{", ".join(items)}{m.group("q")}'

    def _url_sub(m: re.Match) -> str:
        new = _rewrite_path_value(m.group("val"))
        if new != m.group("val"):
            n[0] += 1
        return f"url({m.group('q')}{new}{m.group('q')})"

    html = _ATTR_RE.sub(_attr_sub, html)
    html = _SRCSET_RE.sub(_srcset_sub, html)
    html = _CSS_URL_RE.sub(_url_sub, html)

    # --- DOM ids / metadata carrying /cn (sidebar anchors, og:url, canonical) ---
    # id="/cn/docs/welcome" -> id="/docs/welcome"  (purely cosmetic; safe to trim)
    new_html, c1 = re.subn(r'(id=")/' + LOCALE_DIR + r'/', r'\1/', html)
    # og:url / canonical pointing at the live site: strip the /cn piece too so
    # metadata is consistent (these are SEO tags, inert offline).
    new_html, c2 = re.subn(r'(zcode\.z\.ai)/' + LOCALE_DIR, r'\1', new_html)
    html = new_html
    n[0] += c1 + c2
    return html, n[0]


def _rewrite_css(css: str) -> tuple:
    n = [0]

    def _sub(m: re.Match) -> str:
        new = _rewrite_path_value(m.group("val"))
        if new != m.group("val"):
            n[0] += 1
        return f"url({m.group('q')}{new}{m.group('q')})"

    new_css, _ = _CSS_URL_RE.subn(_sub, css)
    return new_css, n[0]


# ---------------------------------------------------------------------------
# Move + walk
# ---------------------------------------------------------------------------

def _move_locale_up(site_dir: str) -> int:
    """Move every entry under site/cn/ to site/. Returns count moved."""
    cn_dir = os.path.join(site_dir, LOCALE_DIR)
    if not os.path.isdir(cn_dir):
        return 0
    moved = 0
    for name in sorted(os.listdir(cn_dir)):
        src = os.path.join(cn_dir, name)
        dst = os.path.join(site_dir, name)
        if name in _PROTECTED_ROOT and os.path.exists(dst):
            log.error("Refusing to overwrite root entry %r with locale content.", name)
            raise SystemExit(1)
        if os.path.exists(dst):
            # Re-running collect after a previous flatten leaves stale locale
            # pages at the site root (docs/, changelog/, index.html, …). Prefer
            # the freshly crawled cn/ content.
            log.warning("Replacing stale root entry %r with locale content.", name)
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        shutil.move(src, dst)
        moved += 1
    # Remove the now-empty cn/ dir.
    try:
        os.rmdir(cn_dir)
    except OSError:
        log.warning("cn/ not empty after move; leaving it in place.")
    return moved


def _walk_and_rewrite(site_dir: str) -> dict:
    totals = {"files": 0, "rewrites": 0}
    for dirpath, _dirs, files in os.walk(site_dir):
        for name in files:
            path = os.path.join(dirpath, name)
            if name.endswith(".html"):
                with open(path, encoding="utf-8") as fh:
                    html = fh.read()
                new_html, n = _rewrite_html(html)
                if new_html != html:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(new_html)
                totals["files"] += 1
                totals["rewrites"] += n
            elif name.endswith(".css"):
                with open(path, encoding="utf-8") as fh:
                    css = fh.read()
                new_css, n = _rewrite_css(css)
                if new_css != css:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(new_css)
                totals["files"] += 1
                totals["rewrites"] += n
    return totals


def flatten_all(build_root: str) -> dict:
    """Lift cn/ to the site root and rewrite all in-page paths."""
    site_dir = os.path.join(build_root, SITE_DIR_NAME)
    if not os.path.isdir(site_dir):
        log.error("Site dir not found: %s", site_dir)
        raise SystemExit(1)

    cn_dir = os.path.join(site_dir, LOCALE_DIR)
    if not os.path.isdir(cn_dir):
        log.info("No cn/ locale dir present — nothing to flatten (idempotent).")
        return {"moved": 0, "files": 0, "rewrites": 0}

    moved = _move_locale_up(site_dir)
    log.info("Moved %d entries from cn/ to site root.", moved)

    totals = _walk_and_rewrite(site_dir)
    log.info("Rewrote %d files (%d path edits) after flattening.",
             totals["files"], totals["rewrites"])
    return {"moved": moved, **totals}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flatten the cn/ locale directory up to the site root.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)
    flatten_all(build_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
