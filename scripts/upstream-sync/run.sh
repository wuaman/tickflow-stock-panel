#!/usr/bin/env bash
# cron 入口: 闸门 → (有更新才起 agent) → 兜底通知。
#
# crontab 建议(每周三/周六 19:03 北京时间; 宿主机时区已是 Asia/Shanghai, cron 直接用本地时间):
#   3 19 * * 3,6 /mnt/SDCard/codeSpace/tickflow-stock-panel/scripts/upstream-sync/run.sh
#
# 设计: 90% 的轮次上游没有更新 —— 那种情况这里只花一次 git fetch 就退出, 不烧 token。
#       agent 跑完必须留下 $RUN_STATE, 否则视为异常并兜底告警(防止"静默什么都没发生")。

source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "$REPO_ROOT"

# cron 的 PATH 极简, 显式补上 claude CLI 和常用命令
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# 无头认证: 桌面 app 的宿主托管认证在 cron 里不可用(~/.claude/.credentials.json 不存在),
# 所以从独立的环境文件读 API key。文件权限 600、在仓库外, 不会被 git 看到。
SYNC_ENV="${TICKFLOW_SYNC_ENV:-$HOME/.config/tickflow-sync/env}"
if [ -r "$SYNC_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SYNC_ENV"
  set +a
  if [ -z "${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]; then
    log "⚠ $SYNC_ENV 里既没有 ANTHROPIC_AUTH_TOKEN 也没有 ANTHROPIC_API_KEY —— 无头调用会失败"
  fi
  [ -n "${ANTHROPIC_BASE_URL:-}" ] && log "使用中转站 base_url: $ANTHROPIC_BASE_URL"
else
  log "⚠ 未找到 $SYNC_ENV —— 无头调用很可能因未登录而失败"
fi

# .claude/ 被上游 gitignore(不入库), 所以仓库里的 scripts/upstream-sync/SKILL.md 是权威版本,
# 每次运行把它装到 skill 发现路径上 —— 自愈, 不依赖 .claude/ 里的副本是否还在。
SKILL_DIR="$REPO_ROOT/.claude/skills/sync-upstream"
install -D -m 644 "$REPO_ROOT/scripts/upstream-sync/SKILL.md" "$SKILL_DIR/SKILL.md"

RUN_LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M).log"
exec >>"$RUN_LOG" 2>&1
# 本轮起点时间: archive-run.sh 靠它判断 last-run.json / last-report.md 是不是本轮的产物
export SYNC_RUN_START_TS="$(date +%s)"
log "=== 上游同步 loop 启动 (日志: $RUN_LOG) ==="

# 每轮无论成败都归档成 .sync-loop/runs/<轮次>/ —— 事后想查"那轮做了什么"看这里。
# 归档失败不影响本轮结论(它只是留痕)。
finish() {
  local code="$1"
  bash "$REPO_ROOT/scripts/upstream-sync/archive-run.sh" --run-log "$RUN_LOG" >/dev/null 2>&1 \
    || log "⚠ 归档失败(不影响本轮结论)"
  exit "$code"
}

set +e
bash "$REPO_ROOT/scripts/upstream-sync/gate.sh"
GATE_RC=$?
set -e

case "$GATE_RC" in
  10)
    log "上游无更新, 本轮结束(未消耗 agent)"
    finish 0
    ;;
  0) : ;;
  *)
    bash "$REPO_ROOT/scripts/upstream-sync/notify.sh" --title "⚠️ 上游同步: 闸门执行失败" --level warn \
      --file <(printf 'gate.sh 退出码 %s, 本轮未同步。\n日志: %s\n' "$GATE_RC" "$RUN_LOG")
    finish "$GATE_RC"
    ;;
esac

# shellcheck disable=SC1090
source "$GATE_ENV"
START_TS="$(date +%s)"
log "闸门通过: upstream=$UPSTREAM last=${LAST_SYNC:-<未记录>} new=$NEW_COUNT rewritten=$REWRITTEN"

set +e
# --permission-prompts none: 权限清单之外的操作直接拒绝, 而不是挂起等超时(无人值守必须)
timeout 3600 claude -p "/sync-upstream" \
  --settings "$REPO_ROOT/scripts/upstream-sync/settings-sync-loop.json" \
  --permission-mode acceptEdits \
  --permission-prompts none
AGENT_RC=$?
set -e
log "agent 退出码: $AGENT_RC"

# 兜底: agent 是否留下了本轮状态文件
if [ -f "$RUN_STATE" ] && [ "$(stat -c %Y "$RUN_STATE")" -ge "$START_TS" ]; then
  log "agent 正常结束, 状态文件已更新"
  finish 0
fi

log "agent 未留下本轮状态文件 —— 兜底告警(可能被权限拦停/超时/崩溃)"
bash "$REPO_ROOT/scripts/upstream-sync/notify.sh" --title "⚠️ 上游同步 loop 未正常结束" --level warn \
  --file <(printf 'agent 退出码 %s(124=超时 1 小时), 没有写出本轮状态文件。\n\n**线上未被改动即代表安全**: 同步全程在 `sync/*` 分支上进行, 只有部署脚本会碰容器。\n\n日志尾部:\n```\n%s\n```\n' "$AGENT_RC" "$(tail -n 30 "$RUN_LOG")")
finish 1
