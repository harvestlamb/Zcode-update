"""Offline-readiness patches applied to mirrored HTML.

Run after :mod:`crawler` has written the mirrored pages. Concerns:

  1. **Strip analytics.** Remove Google Tag Manager (the gtm.js bootstrap
     script, the noscript iframe, and the GTM reference embedded in the Next.js
     RSC payload). These phone home and are useless offline.

  2. **Rewrite download links to local releases.** The latest Windows x64
     installer is bundled under ``/releases/``. Point the homepage download
     buttons at that local file. Windows ARM64, macOS and Linux buttons are
     marked unavailable (the package ships Windows x64 only). Then switch the
     primary CTA *and* the docs-header 「下载」 chip to Windows x64, hide
     Mac/Linux download columns, and grey out remaining unavailable entries.

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

  7. **Drop the Coding Plan promo section.** Intranet mirrors should not show
     the homepage "使用 GLM Coding Plan 编程" pricing block (hardcoded removal).

  8. **Intranet badge.** Put ``Jushri内网版`` under the ZCODE wordmark on
     the homepage and docs/changelog headers. Docs pages also relax the
     fixed ``h-12`` chrome so the inner vertical scroller still fills the
     remaining viewport.

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
# Windows-only download UI (CTA + icons)
# ---------------------------------------------------------------------------
#
# After CDN URLs are rewritten, the site still SSR's a macOS primary CTA
# (homepage hero *and* the docs/changelog header chip) plus a three-column
# Mac / Windows / Linux download grid. Intranet mirrors only ship Windows
# x64, so we:
#   * retarget "立即下载 ZCode" and header "下载" CTAs to the local win-x64
#     .exe + Windows icon (official SSR defaults to Apple Silicon)
#   * hide MacOS / Linux (Beta) columns in the all-downloads grid
#   * grey out remaining #unavailable-* links (e.g. Windows ARM64)

# Monochrome Windows 11 mark (currentColor). The coloured four-tile logo
# reads as the macOS Finder icon at header-chip size, which is why users
# still reported an "Apple button" after the first retarget.
_WIN_LOGO_SVG = (
    '<svg aria-hidden="true" class="{cls}" data-zc-win-icon="1" fill="currentColor" '
    'focusable="false" height="1em" viewbox="0 0 32 32" width="1em" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<path d="M4 4h10.5v10.5H4zm13.5 0H28v10.5H17.5zM4 17.5h10.5V28H4zm13.5 0H28V28H17.5z"></path>'
    '</svg>'
)
_UNAVAILABLE_STYLE = (
    "opacity:.45;cursor:not-allowed;pointer-events:none;filter:grayscale(1)"
)
_UNAVAILABLE_TITLE = "内网镜像仅提供 Windows x64 安装包"
_HIDDEN_PLATFORM_TITLES = {"MacOS", "macOS", "Linux", "Linux Beta"}


def _ensure_style(tag, snippet: str) -> None:
    """Append CSS snippet to tag style= without duplicating."""
    existing = (tag.get("style") or "").strip()
    if snippet in existing:
        return
    tag["style"] = f"{existing};{snippet}" if existing else snippet


def _replace_platform_icon(anchor, size_class: str) -> None:
    """Swap the first platform SVG inside a CTA for the Windows logo."""
    svg = anchor.find("svg")
    if svg is None:
        return
    svg_html = str(svg)
    if svg.get("data-zc-win-icon") == "1" or 'data-zc-win-icon="1"' in svg_html:
        return
    cls = " ".join(svg.get("class") or []) or size_class
    cls = " ".join(c for c in cls.split() if c != "text-black") or size_class
    new_svg = BeautifulSoup(_WIN_LOGO_SVG.format(cls=cls), "html.parser").svg
    if new_svg is not None:
        svg.replace_with(new_svg)


def _is_primary_download_cta(anchor) -> bool:
    """True for homepage/install hero CTAs and the docs header download chip.

    Official SSR defaults both to macOS (Apple Silicon). The compact header
    button only says 「下载」 (not 「立即下载」), so it must be matched by
    aria-label / hash-rewritten href rather than the hero copy.
    """
    text = anchor.get_text(" ", strip=True)
    aria = anchor.get("aria-label") or ""
    href = anchor.get("href") or ""
    if "立即下载" in text:
        return True
    if "Download ZCODE" in aria:
        return True
    # Compact header chip after CDN rewrite (or a previous Windows retarget).
    if text == "下载" and (
        href.startswith("#unavailable") or "/releases/" in href
    ):
        return True
    return False


def _revive_download_cta(anchor) -> None:
    """Undo grey-out from a previous pass so the retargeted CTA is clickable."""
    style = (anchor.get("style") or "")
    if _UNAVAILABLE_STYLE in style:
        style = style.replace(_UNAVAILABLE_STYLE, "").replace(";;", ";").strip("; ")
        if style:
            anchor["style"] = style
        elif "style" in anchor.attrs:
            del anchor["style"]
    for attr in ("aria-disabled", "tabindex"):
        if attr in anchor.attrs:
            del anchor.attrs[attr]
    if anchor.get("title") == _UNAVAILABLE_TITLE:
        del anchor.attrs["title"]


def _windows_only_download_ui(html: str, latest_version: Optional[str]) -> tuple:
    """Retarget CTAs to Windows x64; hide/grey non-Windows download UI.

    Returns ``(html, stats)`` with keys ``cta_fixed``, ``cols_hidden``,
    ``links_disabled``, ``support_text_fixed``.
    """
    stats = {
        "cta_fixed": 0,
        "cols_hidden": 0,
        "links_disabled": 0,
        "support_text_fixed": 0,
    }
    if "立即下载" not in html and "all-downloads" not in html \
            and "#unavailable" not in html and "全部下载" not in html \
            and "Download ZCODE" not in html:
        return html, stats

    soup = BeautifulSoup(html, "lxml")
    win_href = (
        _local_release_path(latest_version, "x64") if latest_version else None
    )

    # 1) Primary / install / docs-header CTAs → Windows x64.
    for a in soup.find_all("a"):
        if not _is_primary_download_cta(a):
            continue
        if win_href:
            a["href"] = win_href
        _revive_download_cta(a)
        aria = a.get("aria-label") or ""
        if "Download ZCODE" in aria or "macOS" in aria or "Apple" in aria:
            a["aria-label"] = "Download ZCODE 适用于 Windows (x64)"
        for p in a.find_all("p"):
            pt = p.get_text(" ", strip=True)
            if pt.startswith("适用于") or "macOS" in pt or "Apple" in pt:
                p.clear()
                p.append("适用于 Windows (x64)")
        _replace_platform_icon(a, "size-8 shrink-0")
        # Compact header chip: make the platform explicit in the visible label.
        if a.get_text(" ", strip=True) == "下载":
            svg = a.find("svg")
            a.clear()
            if svg is not None:
                a.append(svg)
            a.append("下载 Windows")
        stats["cta_fixed"] += 1

    # 1b) Hide leftover Mac / Linux chips on the install-doc "其他平台" row.
    # Those still SSR an Apple / Tux icon; greying them left a visible Apple
    # button on the page even after the header CTA was retargeted.
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        classes = " ".join(a.get("class") or [])
        is_opt = "group/opt" in classes or href.startswith("#unavailable")
        if not is_opt:
            continue
        if text.startswith(("macOS", "MacOS", "Linux")) and "排查" not in text:
            if a.get("data-zc-hidden-platform") == "1":
                continue
            _ensure_style(a, "display:none")
            a["aria-hidden"] = "true"
            a["data-zc-hidden-platform"] = "1"
            stats["cols_hidden"] += 1

    # 2) Hide Mac / Linux columns in the all-downloads grid.
    # Collect first, then mutate — changing grid-cols mid-loop must not
    # prevent later columns from being found.
    cols_to_hide = []
    grids_to_shrink = []
    for h3 in soup.find_all("h3"):
        title = h3.get_text(strip=True)
        if title not in _HIDDEN_PLATFORM_TITLES:
            continue
        node = h3
        for _ in range(6):
            parent = node.parent
            if parent is None:
                break
            classes = parent.get("class") or []
            # Match the platform grid whether still cols-3 or already cols-1.
            if "grid" in classes and any(
                c.startswith("md:grid-cols-") or c.startswith("grid-cols-")
                for c in classes
            ):
                if node.get("data-zc-hidden-platform") != "1":
                    cols_to_hide.append(node)
                    grids_to_shrink.append(parent)
                break
            node = parent

    for col in cols_to_hide:
        _ensure_style(col, "display:none")
        col["aria-hidden"] = "true"
        col["data-zc-hidden-platform"] = "1"
        stats["cols_hidden"] += 1

    for grid in grids_to_shrink:
        classes = list(grid.get("class") or [])
        grid["class"] = [
            ("md:grid-cols-1" if c == "md:grid-cols-3" else c) for c in classes
        ]

    # 3) Grey out remaining unavailable download links (Windows ARM64, etc.).
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if not href.startswith("#unavailable"):
            continue
        # Skip links inside already-hidden columns.
        if a.find_parent(attrs={"data-zc-hidden-platform": "1"}):
            continue
        _ensure_style(a, _UNAVAILABLE_STYLE)
        a["aria-disabled"] = "true"
        a["tabindex"] = "-1"
        a["title"] = _UNAVAILABLE_TITLE
        stats["links_disabled"] += 1

    # 4) Install-doc support blurb: claim Windows x64 only.
    for p in soup.find_all("p"):
        pt = p.get_text(" ", strip=True)
        if "支持" in pt and "macOS" in pt and "Windows" in pt and "Linux" in pt:
            p.clear()
            p.append("内网镜像仅提供 Windows（x64）安装包。")
            stats["support_text_fixed"] += 1

    out = str(soup)
    if html.lstrip().startswith("<!DOCTYPE") and not out.lstrip().startswith("<!DOCTYPE"):
        out = "<!DOCTYPE html>\n" + out
    return out, stats


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
# Remove homepage "使用 GLM Coding Plan 编程" pricing section (intranet policy)
# ---------------------------------------------------------------------------

# Exact homepage H2 title — keep in sync with the live zcode.z.ai copy.
_CODING_PLAN_SECTION_TITLE = "使用 GLM Coding Plan 编程"


def _remove_coding_plan_section(html: str) -> tuple:
    """Delete the homepage Coding Plan pricing ``<section>``.

    Returns ``(html, n_removed)``. Idempotent: no-op when the section is gone.
    """
    if _CODING_PLAN_SECTION_TITLE not in html:
        return html, 0
    soup = BeautifulSoup(html, "lxml")
    removed = 0
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True) != _CODING_PLAN_SECTION_TITLE:
            continue
        section = h2.find_parent("section")
        if section is not None:
            section.decompose()
            removed += 1
    if not removed:
        return html, 0
    # BeautifulSoup wraps fragments; prefer the original doctype if present.
    out = str(soup)
    if html.lstrip().startswith("<!DOCTYPE") and not out.lstrip().startswith("<!DOCTYPE"):
        out = "<!DOCTYPE html>\n" + out
    return out, removed


# ---------------------------------------------------------------------------
# Homepage "Jushri内网版" badge under the top-left logo
# ---------------------------------------------------------------------------

_INTRANET_BADGE_TEXT = "Jushri内网版"
_INTRANET_BADGE_ATTR = "data-zc-intranet-badge"
_INTRANET_BADGE_CLASS = (
    "text-[11px] font-semibold leading-tight tracking-wide "
    "text-white/80 whitespace-nowrap"
)
_DOCS_BADGE_CLASS = (
    "text-[10px] font-semibold leading-tight tracking-wide "
    "text-muted-foreground whitespace-nowrap"
)
_BADGE_COL_CLASS = "flex flex-col items-start gap-1.5"
_HOMEPAGE_LOGO_ARIA = "ZCode homepage"
_DOCS_LOGO_ARIA = "ZCode 首页"
_DOCS_HEADER_ATTR = "data-zc-docs-header"
_DOCS_SCROLL_ATTR = "data-zc-docs-scroll"


def _soup_html(html: str, soup: BeautifulSoup) -> str:
    out = str(soup)
    if html.lstrip().startswith("<!DOCTYPE") and not out.lstrip().startswith("<!DOCTYPE"):
        out = "<!DOCTYPE html>\n" + out
    return out


def _logo_wordmark_svg(anchor):
    """Return the ZCODE wordmark ``<svg>``, skipping the icon inside size-9."""
    for child in anchor.children:
        if getattr(child, "name", None) == "svg":
            return child
        if getattr(child, "name", None) == "span":
            for inner in child.children:
                if getattr(inner, "name", None) == "svg":
                    return inner
    return None


def _unwrap_logo_row(anchor) -> None:
    """Undo the previous flex-col wrap so icon + wordmark are direct children."""
    for child in list(anchor.children):
        if getattr(child, "name", None) != "span":
            continue
        classes = child.get("class") or []
        if "flex" in classes and "items-center" in classes and "gap-2" in classes:
            for inner in list(child.contents):
                child.insert_before(inner)
            child.decompose()
            break
    anchor["class"] = ["relative", "flex", "items-center", "gap-2"]


def _iter_logo_anchors(soup):
    for aria in (_HOMEPAGE_LOGO_ARIA, _DOCS_LOGO_ARIA):
        yield from soup.find_all("a", attrs={"aria-label": aria})


def _badge_class_for(anchor) -> str:
    if (anchor.get("aria-label") or "") == _DOCS_LOGO_ARIA:
        return _DOCS_BADGE_CLASS
    return _INTRANET_BADGE_CLASS


def _set_class(tag, wanted: str) -> bool:
    """Assign a class string if it differs. Returns True when changed."""
    new = wanted.split()
    if list(tag.get("class") or []) == new:
        return False
    tag["class"] = new
    return True


def _adapt_docs_scroll_layout(soup: BeautifulSoup) -> int:
    """Let the docs header grow with the badge; keep the inner scroller filling the rest.

    Docs chrome is ``h-screen overflow-hidden`` with a fixed ``h-12`` header and
    a ``flex-1 overflow-auto`` article pane. A two-line logo would clip inside
    48px and steal height from the scroller unless the header is un-fixed and
    the pane keeps ``min-h-0``.
    """
    changed = 0
    for header in soup.find_all("header"):
        bar = None
        for child in header.find_all("div", recursive=False):
            classes = child.get("class") or []
            if "h-12" in classes or child.get(_DOCS_HEADER_ATTR) == "1":
                bar = child
                break
        if bar is None:
            continue
        classes = list(bar.get("class") or [])
        if "h-12" in classes:
            new = []
            for c in classes:
                if c == "h-12":
                    new.extend(["min-h-12", "h-auto", "py-1.5", "shrink-0"])
                else:
                    new.append(c)
            bar["class"] = new
            bar[_DOCS_HEADER_ATTR] = "1"
            changed += 1
        elif bar.get(_DOCS_HEADER_ATTR) != "1":
            bar[_DOCS_HEADER_ATTR] = "1"

    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if "overflow-auto" not in classes or "flex-1" not in classes:
            continue
        if "min-w-0" not in classes:
            continue
        extra = []
        if "min-h-0" not in classes:
            extra.append("min-h-0")
        if extra or div.get(_DOCS_SCROLL_ATTR) != "1":
            div["class"] = list(classes) + extra
            div[_DOCS_SCROLL_ATTR] = "1"
            if extra:
                changed += 1
    return changed


def _add_intranet_badge(html: str) -> tuple:
    """Add ``Jushri内网版`` under the ZCODE wordmark. Idempotent.

    Covers homepage/changelog (``ZCode homepage``) and docs (``ZCode 首页``).
    Docs pages also get a taller header + ``min-h-0`` on the article scroller.
    Relocates a badge that was previously placed under the whole logo.
    """
    if _HOMEPAGE_LOGO_ARIA not in html and _DOCS_LOGO_ARIA not in html:
        return html, 0

    soup = BeautifulSoup(html, "lxml")
    added = 0
    for a in _iter_logo_anchors(soup):
        existing = a.find(attrs={_INTRANET_BADGE_ATTR: True})
        # Already under the wordmark: badge sits in a column next to the icon.
        if existing is not None:
            parent = existing.parent
            prev = existing.find_previous_sibling("svg")
            if (
                parent is not None
                and parent.name == "span"
                and parent.parent is a
                and prev is not None
            ):
                classes = [c for c in (a.get("class") or []) if c != "items-center"]
                if "items-start" not in classes:
                    classes.append("items-start")
                    a["class"] = classes
                    added += 1
                if _set_class(existing, _badge_class_for(a)):
                    added += 1
                if _set_class(parent, _BADGE_COL_CLASS):
                    added += 1
                continue
            existing.extract()
            _unwrap_logo_row(a)

        wordmark = _logo_wordmark_svg(a)
        if wordmark is None:
            continue
        col = soup.new_tag("span")
        col["class"] = _BADGE_COL_CLASS.split()
        wordmark.insert_before(col)
        col.append(wordmark)
        badge = soup.new_tag("span")
        badge[_INTRANET_BADGE_ATTR] = "1"
        badge["class"] = _badge_class_for(a)
        badge.string = _INTRANET_BADGE_TEXT
        col.append(badge)
        classes = [c for c in (a.get("class") or []) if c != "items-center"]
        if "items-start" not in classes:
            classes.append("items-start")
        a["class"] = classes
        added += 1
    if _DOCS_LOGO_ARIA in html:
        added += _adapt_docs_scroll_layout(soup)
    if not added:
        return html, 0
    return _soup_html(html, soup), added


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
    html, coding_plan_removed = _remove_coding_plan_section(html)
    # After JS strip / coding-plan removal so BeautifulSoup sees a smaller tree.
    html, win_ui = _windows_only_download_ui(html, latest_version)
    html, intranet_badge = _add_intranet_badge(html)
    return html, {
        "analytics_removed": analytics_removals,
        "downloads_rewritten": download_rewrites,
        "cta_fixed": win_ui["cta_fixed"],
        "cols_hidden": win_ui["cols_hidden"],
        "links_disabled": win_ui["links_disabled"],
        "support_text_fixed": win_ui["support_text_fixed"],
        "client_scripts_removed": scripts_removed,
        "coding_plan_removed": coding_plan_removed,
        "intranet_badge": intranet_badge,
    }


def patch_all(build_root: str, latest_version: Optional[str] = None) -> dict:
    """Patch every .html file under build/site/."""
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    totals = {"files": 0, "analytics_removed": 0,
              "downloads_rewritten": 0, "cta_fixed": 0, "cols_hidden": 0,
              "links_disabled": 0, "support_text_fixed": 0,
              "client_scripts_removed": 0, "coding_plan_removed": 0,
              "intranet_badge": 0}
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
            for key in ("analytics_removed", "downloads_rewritten", "cta_fixed",
                        "cols_hidden", "links_disabled", "support_text_fixed",
                        "client_scripts_removed", "coding_plan_removed",
                        "intranet_badge"):
                totals[key] += stats.get(key, 0)
    log.info(
        "Patched %d HTML files: %d analytics refs removed, %d download links "
        "rewritten, %d CTAs→Windows, %d platform cols hidden, %d links greyed, "
        "%d client JS scripts removed, %d Coding Plan sections removed, "
        "%d intranet badges added.",
        totals["files"], totals["analytics_removed"],
        totals["downloads_rewritten"], totals["cta_fixed"],
        totals["cols_hidden"], totals["links_disabled"],
        totals["client_scripts_removed"], totals["coding_plan_removed"],
        totals["intranet_badge"],
    )
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
