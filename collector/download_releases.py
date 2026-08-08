"""Download Windows installers and update manifests into the build.

Only the latest ZCode version is bundled (the package is meant for an air-gapped
intranet; bundling every historical release would balloon it to many GB). The
download buttons for older versions are marked unavailable by patch_offline.

Artifacts fetched per the latest version (e.g. 3.6.5):
  * windows-x64/ZCode-<ver>-win-x64.exe
  * windows-x64/latest.yml

Only the Windows x64 (64-bit) architecture is bundled.

Stored under build/releases/<version>/.

The Windows .exe installers are ~100 MB each; this step is the slowest part of
the build and is resumable (existing files are verified by size, not redownloaded).

Usage:
    python -m collector.download_releases [--build-root BUILD_ROOT] [--version VER]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from . import config
from .http_client import fetch_bytes, head
from .util import setup_logging, build_paths, ensure_dir, sha256_file

log = logging.getLogger("zcode-collector.releases")

# The Windows architecture we bundle (64-bit only).
_ARCHS = ["x64"]


def _detect_latest_version(build_root: str) -> Optional[str]:
    """Pick the latest version from extracted changelog markdown."""
    cdir = os.path.join(build_root, config.BUILD_CONTENT_DIR, "changelog")
    if not os.path.isdir(cdir):
        return None
    versions = []
    for fn in os.listdir(cdir):
        if fn.endswith(".md"):
            versions.append(fn[:-3])
    if not versions:
        return None

    def _key(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0, 0, 0)

    return max(versions, key=_key)


def _installer_url(version: str, arch: str) -> str:
    return f"{config.RELEASE_CDN}/zcode/electron/releases/{version}/windows-{arch}/ZCode-{version}-win-{arch}.exe"


def _manifest_url(version: str, arch: str) -> str:
    return f"{config.RELEASE_CDN}/zcode/electron/releases/{version}/windows-{arch}/latest.yml"


def download_releases(build_root: str, version: Optional[str] = None) -> dict:
    """Download the latest Windows installers + latest.yml into build/releases/.

    Returns a summary dict {version, files: [...]}.
    """
    if version is None:
        version = _detect_latest_version(build_root)
    if not version:
        log.warning("Could not determine latest version; skipping releases. "
                    "Run extract_markdown first or pass --version.")
        return {"version": None, "files": []}

    log.info("Bundling Windows installers for version %s ...", version)
    out_root = os.path.join(build_root, config.BUILD_RELEASES_DIR, version)
    ensure_dir(out_root)

    downloaded = []
    for arch in _ARCHS:
        # --- installer ---
        exe_url = _installer_url(version, arch)
        exe_name = f"ZCode-{version}-win-{arch}.exe"
        exe_path = os.path.join(out_root, exe_name)
        if _fetch_to(exe_url, exe_path, label=f"{arch} installer"):
            downloaded.append(exe_path)

        # --- latest.yml (electron-builder auto-update manifest) ---
        yml_url = _manifest_url(version, arch)
        yml_path = os.path.join(out_root, f"latest-{arch}.yml")
        _fetch_to(yml_url, yml_path, label=f"{arch} latest.yml", optional=True)
        if os.path.exists(yml_path):
            downloaded.append(yml_path)

    log.info("Downloaded %d release file(s) into %s",
             len(downloaded), os.path.relpath(out_root, build_root))
    return {"version": version, "files": downloaded}


def _fetch_to(url: str, dest: str, label: str, optional: bool = False) -> bool:
    """Download a URL to dest if not already present (size-verified)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log.info("  [skip] %s already present (%d MB) — %s",
                 label, os.path.getsize(dest) // (1 << 20), os.path.basename(dest))
        return True
    # Cheap probe first so we can warn cleanly on 404 (some manifests are missing).
    status = head(url)
    if status is not None and status >= 400:
        if optional:
            log.info("  [none] %s not available (HTTP %s) — skipping", label, status)
        else:
            log.error("  [FAIL] %s unavailable (HTTP %s): %s", label, status, url)
        return False
    try:
        data, _ = fetch_bytes(url, stream=True)
    except Exception as exc:  # noqa: BLE001
        if optional:
            log.info("  [none] %s could not be fetched (%s) — skipping", label, exc)
        else:
            log.error("  [FAIL] %s: %s", label, exc)
        return False
    ensure_dir(os.path.dirname(dest))
    with open(dest, "wb") as fh:
        fh.write(data)
    mb = len(data) // (1 << 20)
    log.info("  [ ok ] %s (%d MB, sha256 %s)",
             label, mb, sha256_file(dest)[:12] + "...")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Windows installers into the build.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("--version", default=None, help="override version (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    build_root = os.path.abspath(args.build_root)
    download_releases(build_root, args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
