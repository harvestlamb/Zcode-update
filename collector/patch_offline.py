"""Offline-readiness patches applied to mirrored HTML.

Run after :mod:`crawler` has written the mirrored pages. Concerns:

  1. **Strip analytics.** Remove Google Tag Manager (the gtm.js bootstrap
     script, the noscript iframe, and the GTM reference embedded in the Next.js
     RSC payload). These phone home and are useless offline.

  2. **Rewrite download links to local releases.** The latest Windows x64
     installer is bundled under ``/releases/``. Point the homepage download
     buttons at that local file. Windows ARM64, macOS and Linux buttons are
     marked unavailable (the package ships Windows x64 only).

  3. **Neutralise the Next.js client router.** Next.js intercepts in-app link
     clicks and does history-based navigation, which breaks for a static mirror
     (no server to return RSC payloads). We inject a tiny script that forces
     full-page (browser-native) navigation for same-origin links.

  4. **Reveal the homepage hero conversation.** SSR leaves the gomoku demo
     chat pane at ``opacity-0``; live React hydration fades it in and pins it
     to the bottom. Without that, the walkthrough looks empty/missing.

  5. **Relocate window traffic-lights.** The close/minimize/maximize dots are
     SSR'd as an absolute overlay under the page root, so they appear at the
     top of the webpage; move them into the hero client chrome.

  6. **Login placeholder.** ``/cn/login`` is an OAuth gate that cannot function
     offline; :func:`write_login_placeholder` writes a static notice instead.

Usage:
    python -m collector.patch_offline [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from . import config
from .util import setup_logging, build_paths, ensure_dir

log = logging.getLogger("zcode-collector.patch")

# Regexes for analytics removal. Applied to the raw HTML string (not the DOM)
# so we can also scrub references buried inside the JSON RSC payload.
#
# The GTM bootstrap is an IIFE that builds a gtm.js URL at runtime. It is
# whitespace-heavy (newlines inside) and appears both as an inline <script> and
# embedded verbatim inside the RSC JSON payload. We match from the IIFE's
# opening signature to its closing 'GTM-XXXX');  call and delete the lot.
_GTM_BOOTSTRAP_RE = re.compile(
    r"\(function\(w,d,s,l,i\)\{.*?'GTM-[A-Z0-9]+'\);",
    re.DOTALL,
)
# Any gtm.js / gtag.js <script src> in the head.
_GTM_SCRIPT_RE = re.compile(
    r'<script[^>]*src="https?://www\.googletagmanager\.com/[^"]*"[^>]*>\s*</script>',
    re.IGNORECASE,
)
# The ns.html noscript iframe URL appears both as a real <iframe src> and as a
# JSON string inside the RSC payload. Replace the URL with a harmless anchor.
_GTM_NS_RE = re.compile(
    r'https://www\.googletagmanager\.com/ns\.html\?id=GTM-[A-Z0-9]+',
    re.IGNORECASE,
)


def _strip_analytics(html: str) -> tuple:
    """Return (cleaned_html, count_of_removals).

    Removals counted: bootstrap IIFEs deleted + gtm.js script tags deleted +
    ns.html URLs neutralised.
    """
    removals = 0
    new_html, n = _GTM_BOOTSTRAP_RE.subn("", html)
    removals += n
    new_html, n = _GTM_SCRIPT_RE.subn("", new_html)
    removals += n
    new_html, n = _GTM_NS_RE.subn("#", new_html)
    removals += n
    return new_html, removals


# ---------------------------------------------------------------------------
# Download-link rewriting
# ---------------------------------------------------------------------------

# A single, comprehensive matcher for ANY zcode release CDN URL. Captures the
# version + a tail so we can decide per-match what to do. Covers the nested
# layout (windows-x64/...), the flat layout (3.1.x/...), and all platforms.
# The trailing extension capture handles .exe/.dmg/.deb/.AppImage/.yml etc.
_CDN_URL_RE = re.compile(
    r"https?://cdn-zcode\.z\.ai/zcode/electron/releases/"
    r"(?P<ver>\d+\.\d+\.\d+)"
    r"(?:/(?P<archdir>windows-x64|macos-(?:arm64|x64)|linux-(?:x64|arm64)))?/"
    r"(?P<file>[^\"\\?\s]+)"
)
# Only Windows x64 (64-bit) is bundled; ARM64 .exe are treated as unavailable.
_WINDOWS_ARCHS = {"x64"}
_WIN_EXE_RE = re.compile(r"ZCode-(?P<ver>\d+\.\d+\.\d+)-win-(?P<arch>x64)\.exe$", re.IGNORECASE)


def _local_release_path(version: str, arch: str) -> str:
    """Local path (served at /releases/...) for a bundled Windows installer."""
    fname = f"ZCode-{version}-win-{arch}.exe"
    return f"/releases/{version}/{fname}"


def _rewrite_downloads(html: str, latest_version: Optional[str]) -> tuple:
    """Rewrite every zcode release CDN URL in the page (DOM + RSC payload).

    Policy per matched URL:
      * Windows .exe of the *latest bundled* version  -> local /releases/...
      * Windows .exe of any other version             -> #unavailable-<ver>-<arch>
      * Non-Windows file (mac/linux .dmg/.deb/.AppImage/.yml/...) -> #unavailable-platform
      * Anything else under the CDN                   -> #unavailable-platform

    Returns ``(html, n_rewrites)``.
    """
    rewrites = 0

    def _sub(m: re.Match) -> str:
        nonlocal rewrites
        rewrites += 1
        ver = m.group("ver")
        archdir = m.group("archdir") or ""
        file_name = m.group("file")

        # Is it a Windows .exe?
        exe = _WIN_EXE_RE.search(file_name)
        if exe:
            arch = exe.group("arch")
            if latest_version and ver == latest_version:
                return _local_release_path(ver, arch)
            return f"#unavailable-{ver}-{arch}"

        # latest.yml manifests we ship alongside Windows — keep those local too.
        if file_name.lower() == "latest.yml" and archdir.startswith("windows") \
                and latest_version and ver == latest_version:
            return f"/releases/{ver}/latest-{archdir.split('-')[-1]}.yml"

        # Everything else (mac/linux/other) is not bundled.
        return "#unavailable-platform"

    return _CDN_URL_RE.subn(_sub, html)


# ---------------------------------------------------------------------------
# Next.js client-router neutraliser
# ---------------------------------------------------------------------------

_ROUTER_NEUTRALISER = """<script>(function(){
// ZCode offline mirror: force native full-page navigation.
// Next.js's App Router intercepts same-origin clicks to fetch RSC payloads;
// in a static mirror there is no server, so those would 404. We intercept the
// interceptors and let the browser do a normal navigation instead.
function n(e){var a=e.target&&e.target.closest;if(!a)return;var t=e.target.closest('a');if(!t)return;var h=t.getAttribute('href');if(!h)return;if(h.indexOf('#')===0||h.indexOf('javascript:')===0||h.indexOf('mailto:')===0)return;e.stopPropagation();}
document.addEventListener('click',n,true);
// Some Next builds call preventDefault on popstate/mouseup too; a defensive
// re-flag keeps the navigation native.
window.__ZCODE_OFFLINE__=true;
})();</script>"""


def _inject_router_neutraliser(html: str) -> str:
    if "__ZCODE_OFFLINE__" in html:
        return html  # already injected (idempotent)
    # Insert right after <body> so it runs before Next's chunk scripts.
    idx = html.find("<body")
    if idx == -1:
        return _ROUTER_NEUTRALISER + html
    # find end of the <body ...> tag
    end = html.find(">", idx) + 1
    return html[:end] + _ROUTER_NEUTRALISER + html[end:]


# ---------------------------------------------------------------------------
# Collapsible-panel interaction restore
# ---------------------------------------------------------------------------
#
# The homepage product demo (the "create a smart gomoku game" walkthrough)
# uses shadcn-style collapsible panels for tool-call summaries and file diffs:
#   <div data-slot="collapsible" data-state="closed">
#     <... data-slot="collapsible-trigger" ...>summary line</...>
#     <... data-slot="collapsible-content" class="... grid-rows-[0fr] ..." aria-hidden="true">
#       (SSR content, if any)
#     </...>
#   </div>
#
# On the live site these expand/collapse via client-side React, which we strip
# for offline use (it crashes without a server). The panels default to closed
# and become dead, unclickable text. This tiny script restores the click
# interaction: clicking a trigger toggles its panel's data-state + the CSS grid
# row (1fr <-> 0fr) that controls height. SSR content inside each panel (file
# names, +/- line counts, terminal summaries) then becomes reachable.
#
# Note: the actual code-diff *text* is generated client-side on the live site
# too, so some panels remain empty after expanding — that matches the original.

_COLLAPSE_RESTORER = """<script>(function(){
// ZCode offline mirror: restore collapsible-panel toggle (no React needed).
function init(){
  var triggers=document.querySelectorAll('[data-slot="collapsible-trigger"]');
  if(!triggers.length)return;
  triggers.forEach(function(trig){
    // Find the enclosing collapsible container.
    var panel=trig.closest('[data-slot="collapsible"]');
    if(!panel)return;
    // Avoid double-binding on re-runs.
    if(trig.getAttribute('data-zc-bound')==='1')return;
    trig.setAttribute('data-zc-bound','1');
    trig.style.cursor='pointer';
    trig.addEventListener('click',function(e){
      // Let real links/buttons inside a trigger (e.g. a file name) work.
      if(e.target.closest('a,button:not([data-slot="collapsible-trigger"])'))return;
      e.preventDefault();
      toggle(panel);
    },false);
  });
}
function toggle(panel){
  var open=panel.getAttribute('data-state')==='open';
  panel.setAttribute('data-state',open?'closed':'open');
  var content=panel.querySelector('[data-slot="collapsible-content"]');
  if(content){
    if(open){
      content.classList.remove('grid-rows-[1fr]');content.classList.add('grid-rows-[0fr]');
      content.classList.remove('opacity-100');content.classList.add('opacity-0');
      content.setAttribute('aria-hidden','true');
    }else{
      // Unhide any element that the SSR render marked hidden for a closed panel.
      content.removeAttribute('hidden');
      content.classList.remove('grid-rows-[0fr]');content.classList.add('grid-rows-[1fr]');
      content.classList.remove('opacity-0');content.classList.add('opacity-100');
      content.setAttribute('aria-hidden','false');
    }
  }
  // Rotate the chevron indicator if present.
  var chev=panel.querySelector('.lucide-chevron-right, .lucide-chevron-down');
  if(chev){
    if(open)chev.classList.remove('rotate-90');
    else chev.classList.add('rotate-90');
  }
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);
else init();
})();</script>"""


def _inject_collapse_restorer(html: str) -> str:
    # Idempotent: skip if already injected, or if the page has no panels.
    if "restore collapsible-panel toggle" in html or 'data-slot="collapsible"' not in html:
        return html
    # Inject before </body> so the DOM is parsed when it runs.
    idx = html.rfind("</body>")
    if idx == -1:
        return html + _COLLAPSE_RESTORER
    return html[:idx] + _COLLAPSE_RESTORER + html[idx:]


# ---------------------------------------------------------------------------
# Reveal homepage hero conversation (gomoku demo)
# ---------------------------------------------------------------------------
#
# The homepage product mockup's chat pane is SSR'd with ``opacity-0`` /
# ``aria-hidden="true"``. On the live site, React hydration fades it in
# (``opacity-100``), adds the scroll-mask classes, and sticks the pane to the
# bottom so the finished walkthrough is visible. Without client JS the pane
# stays invisible — which looks like the "创建一个智能五子棋游戏…" demo is
# missing or empty. Mirror that hydrated end-state statically.

_HERO_CONV_CLOSED = (
    '<div aria-hidden="true" '
    'class="min-h-0 w-full flex-1 overflow-y-auto transition-opacity '
    'duration-150 opacity-0" data-bottom="true" data-top="true">'
)
_HERO_CONV_OPEN = (
    '<div aria-hidden="false" '
    'class="min-h-0 w-full flex-1 overflow-y-auto transition-opacity '
    'duration-150 hero-conversation-scroll-mask hero-scrollbar opacity-100" '
    'data-bottom="true" data-top="false">'
)

_HERO_CONV_SCROLLER = """<script>(function(){
// ZCode offline mirror: pin hero demo conversation to bottom (matches live).
function pin(){
  var el=document.querySelector('.hero-conversation-scroll-mask');
  if(!el)return;
  el.scrollTop=el.scrollHeight;
  el.setAttribute('data-top','false');
  el.setAttribute('data-bottom','true');
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',pin);
else pin();
// Re-pin after fonts/layout settle so the bottom stick survives reflow.
window.addEventListener('load',pin);
})();</script>"""


def _reveal_hero_conversation(html: str) -> str:
    """Make the homepage gomoku-demo chat pane visible without React hydration."""
    if _HERO_CONV_CLOSED not in html:
        return html  # already open, or not the homepage
    html = html.replace(_HERO_CONV_CLOSED, _HERO_CONV_OPEN, 1)
    if "pin hero demo conversation" in html:
        return html
    idx = html.rfind("</body>")
    if idx == -1:
        return html + _HERO_CONV_SCROLLER
    return html[:idx] + _HERO_CONV_SCROLLER + html[idx:]


# ---------------------------------------------------------------------------
# Relocate macOS window traffic-lights onto the hero client chrome
# ---------------------------------------------------------------------------
#
# SSR places the close/minimize/maximize dots in an ``absolute left-0 top-0``
# overlay that ends up as a child of the page root (``bg-background``), so the
# dots sit at the very top of the webpage instead of the product-demo window.
# Move that overlay into ``.hero-visual-theme.relative`` so ``top-0`` anchors
# to the client mockup.

_TOPOVERLAY_MARKER = "@container/topoverfay"
_HERO_VISUAL_RE = re.compile(
    r'(<div class="hero-visual-theme relative\b[^"]*"[^>]*>)',
)


def _extract_balanced_div(html: str, start: int) -> tuple:
    """Return ``(end_index_exclusive, fragment)`` for the ``<div>`` at start.

    ``start`` must point at the leading ``<`` of a ``<div...>``. Returns
    ``(-1, "")`` if the tag cannot be balanced.
    """
    if not html.startswith("<div", start):
        return -1, ""
    depth = 0
    i = start
    n = len(html)
    while i < n:
        next_open = html.find("<div", i)
        next_close = html.find("</div>", i)
        if next_close == -1:
            return -1, ""
        if next_open != -1 and next_open < next_close:
            # Self-closing unlikely for these divs; count as open.
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            i = next_close + len("</div>")
            if depth == 0:
                return i, html[start:i]
    return -1, ""


def _relocate_window_controls(html: str) -> str:
    """Pin the hero traffic-light overlay to the client window top-left."""
    marker = html.find(_TOPOVERLAY_MARKER)
    if marker == -1:
        return html
    # Walk back to the opening <div ... topoverfay ...>
    start = html.rfind("<div", 0, marker)
    if start == -1:
        return html
    end, fragment = _extract_balanced_div(html, start)
    if end < 0 or not fragment:
        return html
    hero_m = _HERO_VISUAL_RE.search(html)
    if not hero_m:
        return html
    # Already inside the hero visual? (marker between hero open and its first
    # nested window chrome is enough — hero open tag end < marker < end of hero)
    if hero_m.end() <= marker < hero_m.end() + 50:
        return html  # already first child
    # More precise: if overlay start is immediately after hero open tag
    after_hero = html[hero_m.end():hero_m.end() + len(fragment) + 10]
    if fragment[:40] in after_hero:
        return html

    without = html[:start] + html[end:]
    # Re-find hero in the spliced string (offset may shift if overlay was before)
    hero_m = _HERO_VISUAL_RE.search(without)
    if not hero_m:
        return html
    insert_at = hero_m.end()
    return without[:insert_at] + fragment + without[insert_at:]


# ---------------------------------------------------------------------------
# Disable Next.js client-side JS (hydration crashes offline)
# ---------------------------------------------------------------------------

# The mirrored pages are full SSR snapshots: all visible content (text,
# headings, images, links) is already in the HTML. The Next.js client bundles
# (webpack/main-app/chunks) try to hydrate against a React tree that expects a
# live server + RSC payload; with neither available they throw
# "Application error: a client-side exception has occurred". Since the static
# content needs no JS to display, we strip every Next client <script> and the
# inline RSC payload (self.__next_f...). We KEEP: CSS <link>s, our injected
# router neutraliser, and the AI widget (/_zc/*).
_NEXT_CHUNK_SCRIPT_RE = re.compile(
    r'<script[^>]*\ssrc="(?:[^"]*/_next/static/chunks/[^"]+)"[^>]*>\s*</script>',
    re.IGNORECASE,
)
# Any remaining src'd _next script we haven't matched (webpack runtime, etc.)
_NEXT_ANY_SCRIPT_RE = re.compile(
    r'<script[^>]*\ssrc="(?:[^"]*/_next/static/[^"]+\.js)"[^>]*>\s*</script>',
    re.IGNORECASE,
)
# Preload hints for _next scripts. Attribute order is not fixed in the wild
# (e.g. `as="script" ... href="..." rel="preload"`), so use lookaheads to match
# any <link> that carries rel="preload" + as="script" + an /_next/static/*.js href.
_NEXT_PRELOAD_LINK_RE = re.compile(
    r'<link\b(?=[^>]*\brel="preload")(?=[^>]*\bas="script")(?=[^>]*\bhref="[^"]*/_next/static/[^"]+\.js")[^>]*/?>',
    re.IGNORECASE,
)
# Inline RSC payload bootstrap + pushes:
#   <script>(self.__next_f=self.__next_f||[]).push([0])</script>
#   <script>self.__next_f.push([1,"..."])</script>
# Allow optional '(' before self.__next_f and any chars up to </script>.
_NEXT_RSC_PAYLOAD_RE = re.compile(
    r'<script[^>]*>\s*\(?\s*self\.__next_f\b.*?</script>',
    re.IGNORECASE | re.DOTALL,
)


def _disable_next_client_js(html: str) -> tuple:
    """Strip Next.js client <script> tags, preload hints, and inline RSC payload.

    Returns (html, n_removed). Does NOT touch CSS <link>s, our router
    neutraliser, or the /_zc AI widget script.
    """
    n = 0
    html, c = _NEXT_RSC_PAYLOAD_RE.subn("", html); n += c
    html, c = _NEXT_CHUNK_SCRIPT_RE.subn("", html); n += c
    html, c = _NEXT_ANY_SCRIPT_RE.subn("", html); n += c
    html, c = _NEXT_PRELOAD_LINK_RE.subn("", html); n += c
    return html, n


# ---------------------------------------------------------------------------
# Disable the EN locale switch (mirror has no English content)
# ---------------------------------------------------------------------------

_EN_LINK_RE = re.compile(r'href="/en/[^"]*"')


def _disable_en_switch(html: str) -> str:
    """Neutralise the "Switch to English" links.

    The mirror only carries the Chinese locale; clicking an ``/en/...`` link
    falls through nginx's catch-all and silently returns the homepage under a
    wrong-looking URL. Replace those hrefs with a disabled anchor so the
    language toggle visibly does nothing instead of breaking navigation.
    Idempotent: skips links already marked aria-disabled.
    """
    def _sub(m: re.Match) -> str:
        return ('href="#" aria-disabled="true" '
                'style="opacity:.5;cursor:not-allowed" '
                'title="英文版在内网镜像中不可用"')
    return _EN_LINK_RE.sub(_sub, html)


# ---------------------------------------------------------------------------
# Top-level patch pass
# ---------------------------------------------------------------------------


def patch_html(html: str, latest_version: Optional[str] = None) -> tuple:
    """Apply all offline patches to one page's HTML.

    Returns ``(patched_html, stats_dict)``.
    """
    html, analytics_removals = _strip_analytics(html)
    html, download_rewrites = _rewrite_downloads(html, latest_version)
    html = _disable_en_switch(html)
    html = _inject_router_neutraliser(html)
    html, scripts_removed = _disable_next_client_js(html)
    html = _inject_collapse_restorer(html)
    html = _reveal_hero_conversation(html)
    html = _relocate_window_controls(html)
    return html, {
        "analytics_removed": analytics_removals,
        "downloads_rewritten": download_rewrites,
        "client_scripts_removed": scripts_removed,
    }


def patch_all(build_root: str, latest_version: Optional[str] = None) -> dict:
    """Patch every .html file under build/site/."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    totals = {"files": 0, "analytics_removed": 0,
              "downloads_rewritten": 0, "client_scripts_removed": 0}
    for dirpath, _dirs, files in os.walk(site_dir):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
            patched, stats = patch_html(html, latest_version)
            if patched != html:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(patched)
            totals["files"] += 1
            totals["analytics_removed"] += stats["analytics_removed"]
            totals["downloads_rewritten"] += stats["downloads_rewritten"]
            totals["client_scripts_removed"] += stats["client_scripts_removed"]
    log.info("Patched %d HTML files: %d analytics refs removed, %d download links "
             "rewritten, %d client JS scripts removed.",
             totals["files"], totals["analytics_removed"],
             totals["downloads_rewritten"], totals["client_scripts_removed"])
    return totals


def write_login_placeholder(build_root: str) -> None:
    """Write a static placeholder at /cn/login/ (OAuth cannot work offline)."""
    target_dir = os.path.join(build_root, config.BUILD_SITE_DIR, "cn", "login")
    ensure_dir(target_dir)
    html = _LOGIN_PLACEHOLDER
    with open(os.path.join(target_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    log.info("Wrote login placeholder at %s", os.path.relpath(target_dir, build_root))


_LOGIN_PLACEHOLDER = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>管理后台 - ZCode 内网镜像</title>
<style>
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0b0d10;color:#e6e6e6;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:2rem;box-sizing:border-box}
  .card{max-width:520px;background:#14181d;border:1px solid #232a31;border-radius:12px;padding:2.5rem}
  h1{margin:0 0 .5rem;font-size:1.4rem}
  p{color:#9aa4ad;line-height:1.7;margin:.6rem 0}
  a{color:#4d9eff;text-decoration:none}
</style>
<meta http-equiv="refresh" content="0; url=/admin/"></head>
<body><div class="card">
  <h1>正在进入管理后台…</h1>
  <p>如未自动跳转，请<a href="/admin/">点这里</a>进入管理后台。</p>
  <p>如需查阅文档，请返回 <a href="/cn/index.html">首页</a> 或 <a href="/cn/docs/welcome/index.html">文档</a>。</p>
</div></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch mirrored HTML for offline use.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("--latest-version", default=None,
                        help="latest Windows version bundled (e.g. 3.6.5); "
                             "auto-detected from manifest if omitted")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)

    latest = args.latest_version
    if latest is None:
        latest = _detect_latest_version(build_root)
    log.info("Latest bundled Windows version: %s", latest or "(none)")

    patch_all(build_root, latest)
    write_login_placeholder(build_root)
    return 0


def _detect_latest_version(build_root: str) -> Optional[str]:
    """Read the latest version from manifest.json if it exists."""
    manifest = os.path.join(build_root, config.BUILD_MANIFEST)
    if os.path.exists(manifest):
        import json
        try:
            with open(manifest, encoding="utf-8") as fh:
                return json.load(fh).get("content_version")
        except Exception:  # noqa: BLE001
            pass
    return None


if __name__ == "__main__":
    sys.exit(main())
