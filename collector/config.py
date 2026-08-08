"""Shared configuration for the ZCode mirror collector.

This module is the single source of truth for:
  - the source site base URL
  - the definitive list of /cn routes to crawl
  - the changelog pagination scheme
  - the CDN host for binary releases

The route list was verified by probing the live site: all listed routes return
server-side rendered Chinese body content in the raw HTML, so no JavaScript
execution is required to extract content.
"""

from __future__ import annotations

# Source site (外网). Crawler must run on a machine with internet access.
SITE_BASE = "https://zcode.z.ai"

# Binary releases live on a separate CDN host (Aliyun OSS fronted by edge).
RELEASE_CDN = "https://cdn-zcode.z.ai"

# The 26 documentation slugs, in sidebar order. Note: ADE-tools is capitalised.
DOC_SLUGS = [
    # 开始使用
    "welcome",
    "install",
    "configuration",
    "feedback",
    # 核心功能
    "agents",
    "goal",
    "browser-use",
    "task-management",
    "repo-wiki",
    "memory",
    "automations",
    "idle-time-tasks",
    "edit-history",
    "remote-development",
    "remote-control",
    "bot-channel",
    "subagents",
    "plugin",
    "skill",
    "mcp-services",
    "commands",
    "hooks",
    "usage-stats",
    # 深度集成
    "safety-confirm",
    "ADE-tools",
    # 帮助
    "keyboard-shortcuts",
    "qa",
]

# Changelog uses cumulative ?page=N pagination, server-side rendered.
# page=3 contains the full 20-version history (oldest = v3.1.6).
# We crawl pages 1..3; higher pages duplicate page 3 and are a no-op.
CHANGELOG_PAGES = [1, 2, 3]

# Routes that have real, SSR Chinese content. We mirror these.
# (login is client-rendered OAuth and unusable offline -> handled separately.)
CONTENT_ROUTES = (
    ["/cn"]
    + [f"/cn/docs/{slug}" for slug in DOC_SLUGS]
    + [f"/cn/changelog?page={p}" for p in CHANGELOG_PAGES]
    + ["/cn/privacy", "/cn/terms"]
)

# /cn/login cannot work offline (OAuth against BigModel backend).
# The collector writes a static placeholder instead of mirroring it.
LOGIN_PLACEHOLDER_ROUTE = "/cn/login"

# Build artefact layout (relative to a build root).
BUILD_SITE_DIR = "site"            # served HTML + rewritten assets
BUILD_CONTENT_DIR = "content"      # clean markdown for RAG
BUILD_RELEASES_DIR = "releases"    # Windows installers
BUILD_MANIFEST = "manifest.json"
BUILD_SHA256SUMS = "SHA256SUMS"

# How to name the export package. export.py fills in the template values.
PACKAGE_NAME_TEMPLATE = "zcode-docs-{version}-{date}.zdoc"

# HTTP behaviour.
REQUEST_TIMEOUT = 30          # seconds per request
REQUEST_RETRIES = 3           # retry on network/5xx errors
REQUEST_DELAY = 0.5           # polite delay between requests (seconds)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 zcode-mirror-collector/1.0"
)

# Windows release artifacts to bundle (only the latest version).
# Tuple: (path-fragment-under-version, display-name)
WINDOWS_ARTIFACTS = [
    ("windows-x64/ZCode-{version}-win-x64.exe", "ZCode Windows x64 安装包"),
    ("windows-x64/latest.yml", "Windows x64 自动更新清单"),
]
