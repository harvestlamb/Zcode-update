"""Build a complete .zdoc package (the collector's main entry point).

Pipeline (each step is idempotent and resumable):
  1. crawl          — fetch all /cn routes, rewrite for offline, download assets
  2. extract        — docs + changelog -> markdown for the RAG index
  3. patch          — strip analytics, rewrite download links, neutralise router
  4. inject         — add the AI chat widget (/_zc/chat-widget.{css,js})
  5. flatten        — lift cn/ to the site root (drop the /cn URL prefix); paths
                     stay site-absolute so they resolve under any access path
  6. fill_demo      — fill the homepage gomoku-demo diff panels with real source
  7. releases       — bundle latest Windows installers + latest.yml
  8. manifest       — record content_version + per-file SHA-256
  9. package        — tar -czf into a single .zdoc with embedded SHA256SUMS

Why crawl before extract/patch: extract reads the crawled HTML, and patch
needs the latest version which extract discovers.
Why flatten after inject: paths are kept site-absolute throughout (/_next/...,
/_zc/..., /images/...), so every reference resolves correctly under nginx
regardless of the URL a page is served through. flatten only rewrites the
/cn locale prefix; it must run before manifest so SHA-256 sums cover the
final, flattened tree.

Usage:
    python -m collector.export [--build-root BUILD] [--out OUT] [--skip-releases]
                               [--no-package] [--from-step STEP]

Examples:
    python -m collector.export                 # full build + package
    python -m collector.export --skip-releases # skip the ~200 MB Windows downloads
    python -m collector.export --from-step patch  # re-run patch+manifest+package only
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import shutil
import subprocess
import sys
import tarfile

from . import config
from .util import setup_logging, build_paths, ensure_dir
from . import crawler, extract_markdown, patch_offline, inject_widget, flatten_cn, fill_demo_diff, download_releases, manifest

log = logging.getLogger("zcode-collector.export")

STEPS = ["crawl", "extract", "patch", "inject", "flatten", "fill_demo", "releases", "manifest", "package"]


def _step_crawl(build_root: str) -> None:
    crawler.crawl_all(build_root)


def _step_extract(build_root: str) -> dict:
    return extract_markdown.extract_all(build_root)


def _step_patch(build_root: str, version: str) -> None:
    patch_offline.patch_all(build_root, latest_version=version)
    patch_offline.write_login_placeholder(build_root)


def _step_inject(build_root: str) -> None:
    inject_widget.inject_all(build_root)


def _step_flatten(build_root: str) -> None:
    flatten_cn.flatten_all(build_root)


def _step_fill_demo(build_root: str) -> None:
    fill_demo_diff.fill_all(build_root)


def _step_releases(build_root: str, version: str, skip: bool) -> None:
    if skip:
        log.info("Skipping release downloads (--skip-releases).")
        return
    download_releases.download_releases(build_root, version)


def _step_manifest(build_root: str) -> dict:
    return manifest.generate_manifest(build_root)


def _step_package(build_root: str, out_dir: str, version: str) -> str:
    """Create the .zdoc tarball. Returns its path."""
    ensure_dir(out_dir)
    date = _dt.datetime.now().strftime("%Y%m%d")
    name = config.PACKAGE_NAME_TEMPLATE.format(version=version, date=date)
    out_path = os.path.join(out_dir, name)

    paths = build_paths(build_root)
    log.info("Packaging %s ...", name)

    # tar -cz the standard subdirs + manifest + SHA256SUMS (relative paths so
    # the importer can extract directly over a data dir). A filter excludes
    # macOS junk (._AppleDouble, .DS_Store) so packages stay clean cross-OS.
    def _clean(tarinfo):
        name = os.path.basename(tarinfo.name)
        if name.startswith("._") or name == ".DS_Store":
            return None
        return tarinfo

    with tarfile.open(out_path, "w:gz") as tar:
        for sub in [config.BUILD_SITE_DIR, config.BUILD_CONTENT_DIR, config.BUILD_RELEASES_DIR]:
            src = os.path.join(build_root, sub)
            if os.path.isdir(src):
                tar.add(src, arcname=sub, filter=_clean)
        tar.add(paths["manifest"], arcname=config.BUILD_MANIFEST)
        tar.add(paths["sha256sums"], arcname=config.BUILD_SHA256SUMS)

    size_mb = os.path.getsize(out_path) // (1 << 20)
    log.info("Package written: %s (%d MB)", out_path, size_mb)
    return out_path


def build(build_root: str, out_dir: str, skip_releases: bool = False,
          from_step: str = "crawl", no_package: bool = False) -> str:
    """Run the full build pipeline. Returns the package path (or '' if no package)."""
    from_step = from_step if from_step in STEPS else "crawl"
    start_idx = STEPS.index(from_step)
    ensure_dir(build_root)

    # Step 1: crawl
    if start_idx <= STEPS.index("crawl"):
        log.info("=== STEP 1/9: crawl ===")
        _step_crawl(build_root)

    # Step 2: extract (also discovers latest version)
    version = None
    if start_idx <= STEPS.index("extract"):
        log.info("=== STEP 2/9: extract markdown ===")
        summary = _step_extract(build_root)
        version = summary.get("latest_version")
    else:
        version = extract_markdown.extract_changelog(build_root)[1]
    if not version:
        version = download_releases._detect_latest_version(build_root) or "unknown"
    log.info("Latest version: %s", version)

    # Step 3: patch
    if start_idx <= STEPS.index("patch"):
        log.info("=== STEP 3/9: patch for offline ===")
        _step_patch(build_root, version)

    # Step 4: inject AI widget
    if start_idx <= STEPS.index("inject"):
        log.info("=== STEP 4/9: inject AI chat widget ===")
        _step_inject(build_root)

    # Step 5: flatten cn/ locale dir to site root (drop the /cn URL prefix).
    # Paths stay site-absolute (/_next/..., /images/..., /docs/...): that resolves
    # correctly under nginx no matter which URL a page is reached through.
    if start_idx <= STEPS.index("flatten"):
        log.info("=== STEP 5/9: flatten cn/ to site root ===")
        _step_flatten(build_root)

    # Step 6: fill the homepage gomoku-demo diff panels with real source so
    # the collapsible file panels show code when expanded.
    if start_idx <= STEPS.index("fill_demo"):
        log.info("=== STEP 6/9: fill homepage demo diffs ===")
        _step_fill_demo(build_root)

    # Step 7: releases
    if start_idx <= STEPS.index("releases"):
        log.info("=== STEP 7/9: download Windows releases ===")
        _step_releases(build_root, version, skip_releases)

    # Step 8: manifest
    if start_idx <= STEPS.index("manifest"):
        log.info("=== STEP 8/9: generate manifest ===")
        _step_manifest(build_root)

    # Step 9: package
    if no_package:
        log.info("Skipping packaging (--no-package). Build artifacts in %s", build_root)
        return ""
    log.info("=== STEP 9/9: package ===")
    return _step_package(build_root, out_dir, version)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a complete .zdoc mirror package.")
    parser.add_argument("--build-root", default="build", help="working build directory")
    parser.add_argument("--out", default="dist", help="output directory for the .zdoc")
    parser.add_argument("--skip-releases", action="store_true",
                        help="skip downloading Windows installers (~200 MB)")
    parser.add_argument("--no-package", action="store_true",
                        help="stop after manifest; don't create the .zdoc tarball")
    parser.add_argument("--from-step", default="crawl", choices=STEPS,
                        help="resume from a given step")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)
    out_dir = os.path.abspath(args.out)
    pkg = build(build_root, out_dir,
                skip_releases=args.skip_releases,
                from_step=args.from_step,
                no_package=args.no_package)
    if pkg:
        print("\n" + "=" * 60)
        print("Done. Package ready:")
        print(f"  {pkg}")
        print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
