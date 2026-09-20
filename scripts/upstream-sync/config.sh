#!/usr/bin/env bash
# 上游同步 loop —— 公共配置。所有脚本 source 本文件。
#
# 这是本 fork 的二开运维工具, 与上游无关。设计说明见同目录 README.md。
# 状态与日志落在 $REPO_ROOT/.sync-loop/ (已 gitignore), 不进仓库、不进容器 data/。

set -euo pipefail

# `${BASH_SOURCE[0]:-$0}` 兜底: 本文件正常由 bash 执行, 但被非 bash shell(zsh 等)source
# 时 BASH_SOURCE 不存在, 少了兜底 REPO_ROOT 会解析成空路径
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
export REPO_ROOT

# ---- 运行时对象 ----------------------------------------------------------
IMAGE="tickflow-stock-panel-app"
APP_CONTAINER="TickFlow_Stock_Panel"
SHIM_CONTAINER="TickFlow_FinancialShim"
HEALTH_URL="http://127.0.0.1:3018/health"   # ⚠ 是 /health, 不是 /api/health
SHIM_HEALTH_URL="http://127.0.0.1:3021/health"

# ---- 状态目录 ------------------------------------------------------------
STATE_DIR="$REPO_ROOT/.sync-loop"
LOG_DIR="$STATE_DIR/logs"
VERSIONS_FILE="$STATE_DIR/versions.tsv"   # 版本<TAB>镜像ID<TAB>git ref<TAB>时间, 最旧在前
GATE_ENV="$STATE_DIR/gate.env"            # gate.sh 产物, 可 source
GATE_SUBJECTS="$STATE_DIR/gate-subjects.txt"
RUN_STATE="$STATE_DIR/last-run.json"      # 每轮结束由 skill 写入, run.sh 兜底检查

# ---- 飞书 ----------------------------------------------------------------
WEBHOOK_FILE="$REPO_ROOT/data/user_data/feishu_webhook.txt"
WEBHOOK_SECRET_FILE="$REPO_ROOT/data/user_data/feishu_webhook_secret.txt"

# ---- 同步语义 ------------------------------------------------------------
SYNC_REF="refs/upstream-sync/last"   # 上次成功同步到的上游 commit
KEEP_VERSIONS=3                      # 本地保留几版可回退

# 验证模式: =1 时 agent 做完 rebase + 对账 + 测试就停下, 不部署 —— 留给人先看 rebase 结果。
# 用法: SYNC_LOOP_STOP_BEFORE_DEPLOY=1 bash scripts/upstream-sync/run.sh
STOP_BEFORE_DEPLOY="${SYNC_LOOP_STOP_BEFORE_DEPLOY:-0}"
export STOP_BEFORE_DEPLOY

# ---- 构建参数(本机 ARM64 踩坑固化, 别改回默认值) ------------------------
# 默认 PYPI_INDEX 是清华源, 对 ARM64 wheel 会 403; stock-sdk 插件必须显式打开
BUILD_ARGS=(
  --build-arg INCLUDE_STOCKSDK=1
  --build-arg PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple
  --build-arg PYPI_FALLBACK=https://pypi.org/simple
)

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# 等 app 起来(用容器内自检, 不依赖宿主端口绑定)
wait_app_healthy() {
  local tries="${1:-40}" i
  for ((i = 1; i <= tries; i++)); do
    if docker exec "$APP_CONTAINER" /app/.venv/bin/python -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('$HEALTH_URL', timeout=5))
assert d.get('status') == 'ok', d
" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# shim 复用 app 的 netns, 只能从 app 容器内部探
wait_shim_healthy() {
  local tries="${1:-20}" i
  for ((i = 1; i <= tries; i++)); do
    if docker exec "$APP_CONTAINER" /app/.venv/bin/python -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('$SHIM_HEALTH_URL', timeout=5))
assert d.get('ok') is True, d
" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# 重启 sidecar —— app 重启后 netns 重建, shim 不跟着重启会监听在旧 netns(东财源 refused)
restart_shim() {
  log "重启 $SHIM_CONTAINER (app 重启后 netns 已变, 必须跟一步)"
  docker restart "$SHIM_CONTAINER" >/dev/null
}

# 只保留最近 KEEP_VERSIONS 个版本的镜像标签(基线版本永不删 —— 它是最后的保底)
prune_versions() {
  local total
  total="$(wc -l <"$VERSIONS_FILE")"
  [ "$total" -gt "$KEEP_VERSIONS" ] || return 0
  head -n "$((total - KEEP_VERSIONS))" "$VERSIONS_FILE" | while IFS=$'\t' read -r v _id _ref _ts; do
    case "$v" in baseline-*) continue ;; esac
    docker rmi "$IMAGE:$v" >/dev/null 2>&1 && log "清理旧版本镜像标签: $v"
  done
  tail -n "$KEEP_VERSIONS" "$VERSIONS_FILE" >"$VERSIONS_FILE.tmp" && mv "$VERSIONS_FILE.tmp" "$VERSIONS_FILE"
}
