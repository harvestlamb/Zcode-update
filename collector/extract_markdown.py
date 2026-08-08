"""Extract clean Markdown from crawled pages for the AI RAG index.

Two output families, written under build/content/:

  * ``docs/<slug>.md``  — one file per documentation article, cleaned of nav,
    sidebar, footer, copy-buttons, and the category breadcrumb.
  * ``changelog/<version>.md`` — one file per release version, extracted from
    the cumulative changelog page.

Each file carries a small YAML-ish front-matter block (title, slug/url, type)
that the RAG indexer reads to attribute answers with source links.

Usage:
    python -m collector.extract_markdown [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import markdownify

from . import config
from .util import setup_logging, build_paths, ensure_dir, slug_from_url

log = logging.getLogger("zcode-collector.markdown")

# Markdownify options used everywhere.
_MD_KW = dict(heading_style="ATX", strip=["img", "button"], bullets="-", code_language="text")


# ---------------------------------------------------------------------------
# Docs articles
# ---------------------------------------------------------------------------

def _clean_article(article: BeautifulSoup) -> str:
    """Strip UI chrome from an <article> element, return its Markdown body.

    Removed:
      * all <button> (copy / anchor buttons)
      * the leading category+copy header div ("mb-3 flex ... justify-between")
      * the "下一步" / next-steps block (link list, not article content) — kept,
        actually, because it sometimes contains useful pointers; we keep it.
      * empty/whitespace-only leading lines
    """
    # Remove all interactive buttons.
    for b in article.find_all("button"):
        b.decompose()

    # Remove the top breadcrumb/category header: it's the first child div with
    # the 'justify-between' layout (category label + copy button).
    for div in article.find_all("div", attrs={"class": True}):
        cls = " ".join(div.get("class") or [])
        if "justify-between" in cls and "mb-3" in cls:
            div.decompose()
            break

    # Drop the <h1> from the body — the caller writes its own title heading
    # and front-matter title, so a second inline H1 would just duplicate.
    h1 = article.find("h1")
    if h1:
        h1.decompose()

    md = markdownify.markdownify(str(article), **_MD_KW)
    # Tidy: collapse 3+ blank lines, strip leading blank lines.
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


def _frontmatter(meta: dict) -> str:
    """Render a tiny front-matter block the RAG indexer can parse."""
    lines = ["---"]
    for k, v in meta.items():
        # escape any stray quotes
        sv = str(v).replace('"', "'")
        lines.append(f'{k}: "{sv}"')
    lines.append("---\n")
    return "\n".join(lines)


def extract_docs(build_root: str) -> int:
    """Extract each docs article to build/content/docs/<slug>.md.

    Reads from the crawled HTML in build/site/ (not re-fetching the network),
    so this step is fully offline and idempotent.
    """
    site_docs = os.path.join(build_root, config.BUILD_SITE_DIR, "cn", "docs")
    out_dir = os.path.join(build_root, config.BUILD_CONTENT_DIR, "docs")
    ensure_dir(out_dir)
    count = 0

    if not os.path.isdir(site_docs):
        log.warning("No docs site dir at %s — run crawler first.", site_docs)
        return 0

    for slug in sorted(os.listdir(site_docs)):
        idx = os.path.join(site_docs, slug, "index.html")
        if not os.path.isfile(idx):
            continue
        with open(idx, encoding="utf-8") as fh:
            html = fh.read()
        soup = BeautifulSoup(html, "lxml")
        article = soup.find("article")
        if not article:
            log.debug("  skip %s: no <article>", slug)
            continue
        h1 = article.find("h1")
        title = h1.get_text(strip=True) if h1 else slug
        body = _clean_article(article)
        meta = {
            "title": title,
            "slug": slug,
            "url": f"/cn/docs/{slug}",
            "type": "docs",
        }
        out = os.path.join(out_dir, f"{slug}.md")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(_frontmatter(meta))
            fh.write(f"\n# {title}\n\n")
            fh.write(body)
            fh.write("\n")
        count += 1
        log.debug("  wrote %s (%d chars)", os.path.relpath(out, build_root), len(body))

    log.info("Extracted %d docs articles to %s", count, os.path.relpath(out_dir, build_root))
    return count


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

# Matches a release heading like "Release v3.6.5".
_RELEASE_HEADING_RE = re.compile(r"Release\s+v?(\d+\.\d+\.\d+)", re.IGNORECASE)


def _parse_changelog_versions(soup: BeautifulSoup) -> List[Tuple[str, BeautifulSoup]]:
    """Split the changelog DOM into per-version fragments.

    Returns a list of (version_string, fragment_soup). Each fragment is a new
    BeautifulSoup containing the release heading + its body up to the next
    release heading.
    """
    # Find the container holding the version list. The h1 '版本发布与更新' is the
    # page title; each release is introduced by an h2 'Release vX'.
    release_headings = [
        h for h in soup.find_all(["h2", "h3"])
        if _RELEASE_HEADING_RE.search(h.get_text(" "))
    ]
    versions: List[Tuple[str, BeautifulSoup]] = []
    seen = set()
    for heading in release_headings:
        m = _RELEASE_HEADING_RE.search(heading.get_text(" "))
        if not m:
            continue
        version = m.group(1)
        if version in seen:
            continue
        seen.add(version)
        # Collect siblings until the next release heading.
        frag = BeautifulSoup("<div></div>", "lxml").div
        frag.append(heading.__copy__())
        for sib in heading.find_all_next():
            # stop at next release heading at the same level
            if sib.name in ("h2", "h3") and _RELEASE_HEADING_RE.search(sib.get_text(" ")):
                break
            # stop if we've left the version list container (hit footer etc.)
            frag.append(sib.__copy__())
        versions.append((version, frag))
    return versions


def extract_changelog(build_root: str) -> Tuple[int, Optional[str]]:
    """Extract each changelog version to build/content/changelog/<version>.md.

    Reads the cumulative page=3 snapshot (contains all versions) from the
    crawled site. Returns (count, latest_version).
    """
    # page=3 is saved as page-3.html (cumulative, all 20 versions).
    src = os.path.join(build_root, config.BUILD_SITE_DIR, "cn", "changelog", "page-3.html")
    if not os.path.isfile(src):
        log.warning("No cumulative changelog at %s — run crawler first.", src)
        return 0, None

    out_dir = os.path.join(build_root, config.BUILD_CONTENT_DIR, "changelog")
    ensure_dir(out_dir)

    with open(src, encoding="utf-8") as fh:
        html = fh.read()
    soup = BeautifulSoup(html, "lxml")
    # strip buttons before splitting
    for b in soup.find_all("button"):
        b.decompose()

    versions = _parse_changelog_versions(soup)
    if not versions:
        log.warning("No release headings found in changelog.")
        return 0, None

    count = 0
    for version, frag in versions:
        body = markdownify.markdownify(str(frag), **_MD_KW)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        meta = {
            "title": f"ZCode {version} 更新日志",
            "version": version,
            "url": "/cn/changelog",
            "type": "changelog",
        }
        out = os.path.join(out_dir, f"{version}.md")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(_frontmatter(meta))
            fh.write(body)
            fh.write("\n")
        count += 1

    # latest = highest semver among the versions
    def _key(v: str):
        return tuple(int(x) for x in v.split("."))
    latest = max((v for v, _ in versions), key=_key)
    log.info("Extracted %d changelog versions to %s (latest %s)",
             count, os.path.relpath(out_dir, build_root), latest)
    return count, latest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def extract_all(build_root: str) -> dict:
    """Run both extractors. Returns a summary dict."""
    n_docs = extract_docs(build_root)
    n_versions, latest = extract_changelog(build_root)
    return {
        "docs_count": n_docs,
        "changelog_count": n_versions,
        "latest_version": latest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Markdown for the RAG index.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)
    summary = extract_all(build_root)
    log.info("Summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
