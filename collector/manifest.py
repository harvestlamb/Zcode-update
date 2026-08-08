"""Generate manifest.json + SHA256SUMS for a build directory.

The manifest is the contract between the collector and the intranet importer:
  * ``content_version``  — latest ZCode version (drives the "is this an upgrade?"
    check on import).
  * ``built_at``         — ISO timestamp.
  * ``source``           — base URL mirrored.
  * ``counts``           — docs/changelog counts.
  * ``files``            — list of {path, size, sha256} for everything under
    site/, content/, releases/.

SHA256SUMS is the same hashes in ``<sha>  <path>`` format for shell-level
verification by import.sh.

Usage:
    python -m collector.manifest [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from typing import List

from . import config
from .util import setup_logging, build_paths, sha256_file

log = logging.getLogger("zcode-collector.manifest")

# Directories whose contents are part of the package.
_PACKAGE_DIRS = [config.BUILD_SITE_DIR, config.BUILD_CONTENT_DIR, config.BUILD_RELEASES_DIR]


def _walk_files(build_root: str) -> List[str]:
    """Return package-relative paths (POSIX separators) for all bundled files."""
    out: List[str] = []
    for sub in _PACKAGE_DIRS:
        root = os.path.join(build_root, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, build_root)
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _detect_version(build_root: str) -> str:
    cdir = os.path.join(build_root, config.BUILD_CONTENT_DIR, "changelog")
    if os.path.isdir(cdir):
        vers = [f[:-3] for f in os.listdir(cdir) if f.endswith(".md")]

        def _key(v):
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (0, 0, 0)
        if vers:
            return max(vers, key=_key)
    return "unknown"


def _count_content(build_root: str) -> dict:
    docs = os.path.join(build_root, config.BUILD_CONTENT_DIR, "docs")
    clog = os.path.join(build_root, config.BUILD_CONTENT_DIR, "changelog")
    return {
        "docs": len([f for f in os.listdir(docs) if f.endswith(".md")]) if os.path.isdir(docs) else 0,
        "changelog": len([f for f in os.listdir(clog) if f.endswith(".md")]) if os.path.isdir(clog) else 0,
    }


def generate_manifest(build_root: str) -> dict:
    """Write manifest.json + SHA256SUMS. Returns the manifest dict."""
    paths = build_paths(build_root)
    files_rel = _walk_files(build_root)

    log.info("Hashing %d files ...", len(files_rel))
    file_records = []
    sha_lines = []
    for rel in files_rel:
        full = os.path.join(build_root, rel)
        size = os.path.getsize(full)
        digest = sha256_file(full) if size > 0 else ""
        file_records.append({"path": rel, "size": size, "sha256": digest})
        sha_lines.append(f"{digest}  {rel}")

    manifest = {
        "format": "zcode-docs-mirror",
        "format_version": 1,
        "content_version": _detect_version(build_root),
        "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": config.SITE_BASE,
        "counts": _count_content(build_root),
        "files": file_records,
    }

    with open(paths["manifest"], "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(paths["sha256sums"], "w", encoding="utf-8") as fh:
        fh.write("\n".join(sha_lines) + "\n")

    log.info("manifest.json: content_version=%s, %d files, %d docs, %d changelog entries",
             manifest["content_version"], len(file_records),
             manifest["counts"]["docs"], manifest["counts"]["changelog"])
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate manifest.json + SHA256SUMS.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    generate_manifest(os.path.abspath(args.build_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
