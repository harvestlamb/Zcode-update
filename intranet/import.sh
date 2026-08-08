#!/usr/bin/env bash
# import.sh — import / upgrade a ZCode mirror .zdoc package into the intranet.
#
# Flow:
#   1. verify the tarball + every file's SHA-256 against SHA256SUMS
#   2. read the package's content_version; skip if identical to current
#   3. atomically swap ./data (backup old -> extract new -> validate)
#   4. on any post-backup failure → restore ./data.backup (rollback)
#   5. append an upgrade record under ./config/upgrade_logs.jsonl
#   6. signal the AI container to rebuild its retrieval index
#
# Safe to re-run: identical versions are skipped; failures roll back.
#
# Usage:
#   ./import.sh <package>.zdoc
set -euo pipefail

# --- config -----------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
BACKUP_DIR="${BACKUP_DIR:-$HERE/data.backup}"
STAGING_DIR="${STAGING_DIR:-$HERE/.staging}"
CONFIG_DIR="${CONFIG_DIR:-$HERE/config}"
WEB_PORT="${WEB_PORT:-8090}"
UPGRADE_LOG="${CONFIG_DIR}/upgrade_logs.jsonl"

# --- helpers ----------------------------------------------------------------
log()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
err()  { printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32m[OK]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

record_upgrade() {
  # args: action ok from to message rolled_back package
  local action="$1" okflag="$2" from_v="$3" to_v="$4" message="$5" rolled="$6" package="$7"
  mkdir -p "$CONFIG_DIR"
  python3 - "$UPGRADE_LOG" "$action" "$okflag" "$from_v" "$to_v" "$message" "$rolled" "$package" <<'PY' || true
import json, sys
from datetime import datetime, timezone
path, action, okflag, from_v, to_v, message, rolled, package = sys.argv[1:9]
rec = {
    "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    "action": action,
    "ok": okflag.lower() in ("1", "true", "yes", "ok"),
    "from_version": from_v,
    "to_version": to_v,
    "message": message[:1000],
    "errors": [],
    "files_checked": 0,
    "rolled_back": rolled.lower() in ("1", "true", "yes"),
    "source": "import.sh",
    "package": package[:300],
    "operator": "cli",
}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}

rollback_data() {
  log "升级失败，正在从备份回滚 ..."
  if [ ! -d "$BACKUP_DIR" ] || [ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null || true)" ]; then
    err "没有可用备份，无法回滚"
    return 1
  fi
  rm -rf "$DATA_DIR"
  mkdir -p "$DATA_DIR"
  # Copy backup contents back (keep backup intact for inspection).
  cp -a "$BACKUP_DIR"/. "$DATA_DIR"/
  if [ ! -f "$DATA_DIR/manifest.json" ]; then
    err "回滚后仍缺少 manifest.json"
    return 1
  fi
  local ver
  ver="$(python3 -c "import json;print(json.load(open('$DATA_DIR/manifest.json')).get('content_version','unknown'))" 2>/dev/null || echo unknown)"
  ok "已回滚到版本 ${ver}"
  return 0
}

# --- arg check --------------------------------------------------------------
[ $# -ge 1 ] || die "用法: $0 <package>.zdoc   例: $0 ../dist/zcode-docs-3.6.5-20260808.zdoc"
PKG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
PKG_NAME="$(basename "$PKG")"
[ -f "$PKG" ] || die "找不到包文件: $PKG"

command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
  || die "需要 sha256sum 或 shasum (macOS 自带 shasum)。"

# use shasum on macOS, sha256sum on Linux
if command -v sha256sum >/dev/null 2>&1; then
  SHA_CMD=(sha256sum)
else
  SHA_CMD=(shasum -a 256)
fi

log "包: $PKG"

# --- 1. extract to staging + verify hashes ----------------------------------
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
log "解包到暂存目录 ..."
tar -xzf "$PKG" -C "$STAGING_DIR"
[ -f "$STAGING_DIR/SHA256SUMS" ]   || die "包内缺少 SHA256SUMS，可能损坏。"
[ -f "$STAGING_DIR/manifest.json" ] || die "包内缺少 manifest.json，可能损坏。"

log "校验每个文件的 SHA-256 (防拷贝损坏) ..."
# -c reads SHA256SUMS (paths are relative) and verifies each file.
# Works with both sha256sum (Linux) and shasum (macOS). Redirect the per-file
# OK lines to keep output quiet; failures still print to stderr.
if ( cd "$STAGING_DIR" && "${SHA_CMD[@]}" -c SHA256SUMS ) >/tmp/zcode_sha_$$.log 2>&1; then
  ok "完整性校验通过"
else
  cat /tmp/zcode_sha_$$.log >&2
  rm -f /tmp/zcode_sha_$$.log
  record_upgrade "error" "0" "" "" "SHA-256 校验失败" "0" "$PKG_NAME"
  die "SHA-256 校验失败！包可能损坏或被篡改，已中止。请重新拷贝。"
fi
rm -f /tmp/zcode_sha_$$.log

# --- 2. version check -------------------------------------------------------
NEW_VER="$(python3 -c "import json,sys;print(json.load(open('$STAGING_DIR/manifest.json'))['content_version'])" 2>/dev/null || echo unknown)"
log "包内容版本: ${NEW_VER}"

CUR_VER="none"
if [ -f "$DATA_DIR/manifest.json" ]; then
  CUR_VER="$(python3 -c "import json,sys;print(json.load(open('$DATA_DIR/manifest.json'))['content_version'])" 2>/dev/null || echo unknown)"
fi
log "当前数据版本: $CUR_VER"

if [ "$NEW_VER" != "unknown" ] && [ "$NEW_VER" = "$CUR_VER" ]; then
  ok "已是版本 ${NEW_VER}，无需升级。如需强制重装请先删除 ${DATA_DIR}/manifest.json。"
  record_upgrade "skipped" "1" "$CUR_VER" "$NEW_VER" "已是版本 ${NEW_VER}，无需升级" "0" "$PKG_NAME"
  rm -rf "$STAGING_DIR"
  exit 0
fi

# --- 3. atomic swap (with rollback on failure) ------------------------------
log "备份当前数据到 $BACKUP_DIR ..."
rm -rf "$BACKUP_DIR"
if [ -d "$DATA_DIR" ]; then
  mv "$DATA_DIR" "$BACKUP_DIR"
fi

cleanup_and_rollback() {
  local reason="$1"
  err "$reason"
  rm -rf "$STAGING_DIR"
  if rollback_data; then
    record_upgrade "rolled_back" "0" "$CUR_VER" "$NEW_VER" "升级失败已回滚: ${reason}" "1" "$PKG_NAME"
    die "升级失败，已自动回滚到 ${CUR_VER}"
  else
    record_upgrade "error" "0" "$CUR_VER" "$NEW_VER" "升级失败且回滚失败: ${reason}" "0" "$PKG_NAME"
    die "升级失败且回滚失败，请检查 ${BACKUP_DIR}"
  fi
}

log "写入新数据到 $DATA_DIR ..."
mkdir -p "$DATA_DIR"
# Move the standard subdirs + manifest files into place. Anything unexpected
# in staging is left behind (deliberately conservative).
for item in site content releases manifest.json SHA256SUMS; do
  if [ -e "$STAGING_DIR/$item" ]; then
    mv "$STAGING_DIR/$item" "$DATA_DIR/" || cleanup_and_rollback "移动 ${item} 失败"
  fi
done
rm -rf "$STAGING_DIR"

[ -f "$DATA_DIR/manifest.json" ] || cleanup_and_rollback "安装后缺少 manifest.json"
[ -f "$DATA_DIR/SHA256SUMS" ]   || cleanup_and_rollback "安装后缺少 SHA256SUMS"
[ -d "$DATA_DIR/content" ]      || cleanup_and_rollback "安装后缺少 content/"
[ -d "$DATA_DIR/site" ]         || cleanup_and_rollback "安装后缺少 site/"

ok "数据已就位: $DATA_DIR"

# --- 4. signal AI container to rebuild index ---------------------------------
REINDEX_OK=1
if docker compose -f "$HERE/docker-compose.yml" ps --format '{{.Service}}' 2>/dev/null | grep -q '^ai$'; then
  log "重建 AI 检索索引 ..."
  # Retry: the container might be mid-restart.
  REINDEX_OK=0
  for attempt in 1 2 3; do
    if docker compose -f "$HERE/docker-compose.yml" exec -T ai \
        curl -sf -X POST http://localhost:8000/api/reindex >/dev/null 2>&1; then
      ok "AI 索引已重建"; REINDEX_OK=1; break
    fi
    [ "$attempt" -eq 3 ] && err "AI 索引重建失败"
    sleep 2
  done
  if [ "$REINDEX_OK" -ne 1 ]; then
    # Content is already swapped; roll back so the site stays consistent.
    if rollback_data; then
      # Best-effort: rebuild index for the restored version.
      docker compose -f "$HERE/docker-compose.yml" exec -T ai \
        curl -sf -X POST http://localhost:8000/api/reindex >/dev/null 2>&1 || true
      record_upgrade "rolled_back" "0" "$CUR_VER" "$NEW_VER" "索引重建失败，已回滚" "1" "$PKG_NAME"
      die "索引重建失败，已自动回滚到 ${CUR_VER}"
    else
      record_upgrade "error" "0" "$CUR_VER" "$NEW_VER" "索引重建失败且回滚失败" "0" "$PKG_NAME"
      die "索引重建失败且回滚失败"
    fi
  fi
else
  log "AI 容器未运行，跳过索引重建。(首次部署请先 docker compose up -d)"
fi

ACTION="upgraded"
[ "$CUR_VER" = "none" ] || [ "$CUR_VER" = "unknown" ] && ACTION="imported"
record_upgrade "$ACTION" "1" "$CUR_VER" "$NEW_VER" "升级完成: ${CUR_VER} -> ${NEW_VER}" "0" "$PKG_NAME"

echo
ok "升级完成: ${CUR_VER} -> ${NEW_VER}"
log "访问: http://<本机IP>:${WEB_PORT}   旧数据备份: ${BACKUP_DIR}"
log "升级记录: ${UPGRADE_LOG}"
[ -d "$BACKUP_DIR" ] && log "确认无误后可删除备份: rm -rf ${BACKUP_DIR}"
