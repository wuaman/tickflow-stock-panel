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

# .sync-loop/ 的忽略规则在我们自己的二开提交里(.gitignore); rebase 重演到它之前, 状态目录是"未跟踪且未忽略",
# agent 解冲突时一句 `git add -A` 就会把日志/存档提交进去。写进 .git/info/exclude(本地, 不随分支切换变化)
EXCLUDE_FILE="$(git rev-parse --git-path info/exclude)"
grep -qxF '.sync-loop/' "$EXCLUDE_FILE" 2>/dev/null || echo '.sync-loop/' >>"$EXCLUDE_FILE"

# 存档脚本拷到仓库外的 .sync-loop/bin/(git 不管, rebase 不会动) —— agent 若停在 rebase 中途,
# 工作区里根本没有 scripts/upstream-sync/, 收尾时只能从这里调 park.sh
PARK_BIN="$STATE_DIR/bin"
install -D -m 644 "$REPO_ROOT/scripts/upstream-sync/park.sh" "$PARK_BIN/park.sh"
install -D -m 644 "$REPO_ROOT/scripts/upstream-sync/config.sh" "$PARK_BIN/config.sh"

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

# ---- 断点续跑: 上一轮没跑完(park.sh 留了存档)且存档仍然有效 → 只重演剩下的提交 ----
# 续跑时同步目标钉在上一轮的上游 commit(不追最新), 比它新的上游提交留给下一轮 —— 否则已解的冲突全白做。
export SYNC_RESUME=0 SYNC_TARGET="$UPSTREAM"
discard_resume() {
  log "丢弃断点存档: $1"
  rm -f "$RESUME_ENV" "$RESUME_PARTIAL"
  git update-ref -d refs/upstream-sync/resume-done 2>/dev/null || true
}
if [ -f "$RESUME_ENV" ]; then
  # shellcheck disable=SC1090
  source "$RESUME_ENV"
  if [ "$REWRITTEN" = true ]; then
    discard_resume "上游重写了历史"
  elif ! git cat-file -e "${RESUME_DONE:-x}^{commit}" 2>/dev/null || ! git cat-file -e "${RESUME_FROM:-x}^{commit}" 2>/dev/null; then
    discard_resume "存档里的提交已不存在"
  elif ! git merge-base --is-ancestor "$RESUME_FROM" main; then
    discard_resume "main 被改写过, 存档的起点不在 main 历史里"
  elif ! git merge-base --is-ancestor "$RESUME_TARGET" origin/main; then
    discard_resume "存档的上游目标已不在 origin/main 历史里"
  elif [ "${RESUME_ATTEMPTS:-0}" -gt "$MAX_RESUME_ATTEMPTS" ]; then
    discard_resume "已中断 $RESUME_ATTEMPTS 次(上限 $MAX_RESUME_ATTEMPTS), 从头来"
    bash "$REPO_ROOT/scripts/upstream-sync/notify.sh" --title "⚠️ 上游同步: 续跑 $RESUME_ATTEMPTS 次仍未完成, 已丢弃进度从头来" --level warn \
      --file <(printf '同一个上游目标 `%s` 连续 %s 轮没跑完, 可能每次都卡在同一处。\n本轮从头 rebase; 若再超时请人工看日志: %s\n' "${RESUME_TARGET:0:12}" "$RESUME_ATTEMPTS" "$RUN_LOG")
  else
    export SYNC_RESUME=1 SYNC_TARGET="$RESUME_TARGET" \
      SYNC_RESUME_DONE="$RESUME_DONE" SYNC_RESUME_FROM="$RESUME_FROM" \
      SYNC_RESUME_STOPPED="${RESUME_STOPPED:-}" SYNC_RESUME_ATTEMPTS="$RESUME_ATTEMPTS"
    log "断点续跑(第 $RESUME_ATTEMPTS 次): 目标=${RESUME_TARGET:0:12} 已完成=$(git rev-list --count "$RESUME_TARGET..$RESUME_DONE") 个提交, 剩余=$(git rev-list --count --no-merges "$RESUME_FROM..main") 个"
    if [ "$RESUME_TARGET" != "$UPSTREAM" ]; then
      log "上游在那之后又有 $(git rev-list --count "$RESUME_TARGET..$UPSTREAM") 个新提交, 留给下一轮"
    fi
  fi
fi

set +e
# --permission-prompts none: 权限清单之外的操作直接拒绝, 而不是挂起等超时(无人值守必须)
# -k 60: 到点先 TERM, 60 秒还不退就 KILL
timeout -k 60 "$AGENT_TIMEOUT" claude -p "/sync-upstream" \
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

# 存档半截进度 + 恢复仓库到 main(否则工作区停在 rebase 中途, 下一轮连本脚本都找不到)
set +e
SYNC_REPO_ROOT="$REPO_ROOT" bash "$PARK_BIN/park.sh" "$SYNC_TARGET"
PARK_RC=$?
set -e
if [ "$PARK_RC" -eq 0 ] && [ -f "$RESUME_ENV" ]; then
  PARK_NOTE="进度已存档, 下一轮从断点续跑(\`$RESUME_ENV\`)。"
elif [ "$PARK_RC" -eq 0 ]; then
  PARK_NOTE="没有可存的进度, 仓库已恢复到 main, 下一轮从头来。"
else
  PARK_NOTE="⚠ 存档失败(park.sh 退出码 $PARK_RC), 仓库可能还停在 rebase 中途 —— **需人工处理**, 否则下一轮 cron 找不到脚本。"
fi
log "$PARK_NOTE"
bash "$REPO_ROOT/scripts/upstream-sync/notify.sh" --title "⚠️ 上游同步 loop 未正常结束" --level warn \
  --file <(printf 'agent 退出码 %s(124/137=超时 %s 小时), 没有写出本轮状态文件。\n\n%s\n\n**线上未被改动即代表安全**: 同步全程在 `syncing/*` 分支上进行, 只有部署脚本会碰容器。\n\n日志尾部:\n```\n%s\n```\n' "$AGENT_RC" "$((AGENT_TIMEOUT / 3600))" "$PARK_NOTE" "$(tail -n 30 "$RUN_LOG")")
finish 1
