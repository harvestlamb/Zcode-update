"""HTML rewriting and asset discovery.

Two responsibilities:
  1. Rewrite every URL reference inside a mirrored HTML page so it points at a
     local, offline path (rewriting the Next.js image optimizer is the key bit).
  2. Collect the set of first-party asset URLs that must be downloaded.

The rewriter is pure: it takes HTML + a base URL and returns new HTML + a list
of asset URLs to fetch. ``crawler.py`` drives the download of those assets and
writes the patched HTML to disk.
"""

from __future__ import annotations

import logging
import posixpath
from typing import List, Optional, Set, Tuple
from urllib.parse import urlsplit, parse_qs, unquote, urlencode, urljoin

from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("zcode-collector.rewrite")

# Hosts we consider first-party (assets we are allowed/willing to mirror).
_FIRST_PARTY_HOSTS = {"zcode.z.ai"}


def _is_first_party(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in _FIRST_PARTY_HOSTS


def _next_image_source_path(src: str) -> Optional[str]:
    """If ``src`` is a ``/_next/image?url=...`` URL, return the underlying
    source path (e.g. ``/images/foo.png`` or ``/content/docs/bar.png``).

    Returns None for non-image-optimizer URLs.
    """
    parts = urlsplit(src)
    if parts.path != "/_next/image":
        return None
    qs = parse_qs(parts.query)
    url_vals = qs.get("url")
    if not url_vals:
        return None
    inner = unquote(url_vals[0])
    inner_parts = urlsplit(inner)
    # Only rewrite first-party, root-relative originals.
    if inner_parts.netloc and inner_parts.netloc not in _FIRST_PARTY_HOSTS:
        return None
    return inner_parts.path


# Attributes that may hold URLs, per tag. Used for generic link rewriting.
_URL_ATTRS = {
    "a": "href",
    "link": "href",
    "script": "src",
    "img": "src",
    "source": "src",
    "source": "srcset",
    "video": "src",
    "audio": "src",
    "use": "href",
}


def _first_party_path(url: str) -> Optional[str]:
    """Return the absolute site-relative path for a first-party URL, or None.

    Returned paths are absolute (start with '/') and are stored verbatim under
    build/site/. External hosts and unsupported schemes return None.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", ""):
        return None
    if parts.netloc and parts.netloc not in _FIRST_PARTY_HOSTS:
        return None
    if parts.scheme in ("http", "https") and not parts.netloc:
        return None
    # Root-relative or same-host absolute.
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def rewrite_html(html: str, page_url: str) -> Tuple[str, List[str]]:
    """Rewrite a page's HTML for offline use.

    Returns ``(new_html, asset_paths)`` where ``asset_paths`` is a de-duplicated
    list of site-relative paths (strings starting with ``/``) that must be
    downloaded into build/site/. HTML page links are excluded from the asset
    list (they are crawled separately) but CSS/JS/image/font references are
    included.
    """
    soup = BeautifulSoup(html, "lxml")
    asset_paths: Set[str] = set()
    base = page_url

    # --- <link rel="stylesheet|preload|modulepreload|icon" href> ---
    for link in soup.find_all("link"):
        href = link.get("href")
        if not href:
            continue
        rel = link.get("rel") or []
        rel_str = " ".join(rel).lower()
        # Skip preconnect/dns-prefetch (external hosts we don't mirror).
        if any(r in rel_str for r in ("preconnect", "dns-prefetch")):
            continue
        # canonical / alternate (hreflang) point to HTML pages, not assets.
        # They carry no value in an offline mirror, so drop the tag entirely.
        if any(r in rel_str for r in ("canonical", "alternate")):
            link.decompose()
            continue
        absolute = urljoin(base, href)
        local = _first_party_path(absolute)
        if local is None:
            # External (e.g. GTM) — leave as-is; patch_offline strips GTM.
            continue
        # Fonts (preload as=font), icons, stylesheets all mirror verbatim.
        asset_paths.add(local)
        link["href"] = local

    # --- <script src> ---
    for script in soup.find_all("script"):
        src = script.get("src")
        if not src:
            continue
        absolute = urljoin(base, src)
        local = _first_party_path(absolute)
        if local is None:
            continue
        asset_paths.add(local)
        script["src"] = local

    # --- <img src> (incl. /_next/image) and srcset ---
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            local = _resolve_img_src(src, base, asset_paths)
            if local:
                img["src"] = local
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            new_srcset = _rewrite_srcset(srcset, base, asset_paths)
            if new_srcset:
                img["srcset"] = new_srcset

    # --- <source srcset> (picture elements) ---
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            new_srcset = _rewrite_srcset(srcset, base, asset_paths)
            if new_srcset:
                source["srcset"] = new_srcset

    # --- <use href> (svg sprites) ---
    for use in soup.find_all("use"):
        href = use.get("href") or use.get("xlink:href")
        if href and not href.startswith("#"):
            absolute = urljoin(base, href)
            local = _first_party_path(absolute)
            if local:
                asset_paths.add(local)
                if use.get("xlink:href"):
                    use["xlink:href"] = local
                else:
                    use["href"] = local

    # --- <a href>: rewrite same-site links to local relative paths ---
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(base, href)
        local = _first_party_path(absolute)
        if local is None:
            # External outbound link — leave (patch_offline may annotate).
            continue
        a["href"] = _html_link_target(absolute, local)

    # Drop the base tag if present (we've resolved everything already).
    for base_tag in soup.find_all("base"):
        base_tag.decompose()

    return str(soup), sorted(asset_paths)


def _resolve_img_src(src: str, base: str, asset_paths: Set[str]) -> Optional[str]:
    """Resolve an <img src> to a local path, recording the asset.

    Handles the /_next/image optimizer by extracting the underlying source path.
    """
    absolute = urljoin(base, src)
    inner = _next_image_source_path(absolute)
    if inner is not None:
        # The real file is first-party; mirror it verbatim.
        asset_paths.add(inner)
        return inner
    local = _first_party_path(absolute)
    if local is None:
        return None
    asset_paths.add(local)
    return local


def _rewrite_srcset(srcset: str, base: str, asset_paths: Set[str]) -> str:
    """Rewrite a srcset attribute, returning the new value or '' on failure."""
    items = []
    changed = False
    for item in srcset.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split()
        url = parts[0]
        descriptor = " ".join(parts[1:])
        absolute = urljoin(base, url)
        inner = _next_image_source_path(absolute)
        if inner is not None:
            asset_paths.add(inner)
            new_url = inner
            changed = True
        else:
            local = _first_party_path(absolute)
            if local:
                asset_paths.add(local)
                new_url = local
                changed = True
            else:
                new_url = url
        items.append(f"{new_url} {descriptor}".strip())
    return ", ".join(items) if changed else ""


# Mapping of /cn routes -> their local index.html path (for link rewriting).
def _html_link_target(absolute_url: str, local_path: str) -> str:
    """Compute the href to put on an <a> tag so it works offline.

    We rewrite to the local file path so links work via file:// or any static
    server. /cn/docs/welcome -> /cn/docs/welcome/index.html etc.
    """
    parts = urlsplit(absolute_url)
    path = parts.path
    query = parts.query

    # changelog with page query -> the snapshot file for that page.
    # page=1 maps to index.html (the canonical changelog landing page).
    if path.rstrip("/") == "/cn/changelog" and query:
        from urllib.parse import parse_qs as _pqs
        q = _pqs(query)
        page = int((q.get("page") or ["1"])[0])
        return "/cn/changelog/index.html" if page == 1 else f"/cn/changelog/page-{page}.html"

    # directory-root pages
    if path in ("/cn", "/cn/", "/cn/docs", "/cn/docs/"):
        return path.rstrip("/") + "/index.html"
    if path == "/cn/changelog":
        return "/cn/changelog/index.html"

    # /cn/<...> -> append /index.html
    if not local_path.endswith("/"):
        return local_path + "/index.html"
    return local_path + "index.html"
