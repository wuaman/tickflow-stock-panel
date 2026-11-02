#!/usr/bin/env bash
# 断点存档: agent 没正常结束(超时/崩溃/被拦)时, 把半截的同步进度存下来, 并把仓库恢复到 main。
#
# 为什么必须恢复仓库: 本 loop 的脚本就在工作区里。rebase 中途被杀时工作区停在上游的旧状态,
# scripts/upstream-sync/ 整个不存在 —— 不恢复的话下一轮 cron 连 run.sh 都找不到。
#
# 存什么(下一轮由 run.sh 判定能否续跑, skill 用 `git rebase --onto DONE FROM` 只重演剩下的):
#   refs/upstream-sync/resume-done  已 rebase 好的提交(建在 TARGET 之上)
#   $RESUME_ENV                     TARGET / DONE / FROM / 卡在哪个提交 / 第几次续跑
#   $RESUME_PARTIAL                 卡住那个提交的冲突半成品(git diff, 仅供参考)
#
# 用法: park.sh <TARGET 上游 commit>     (run.sh 传 gate.env 里的 UPSTREAM, 续跑时传 SYNC_TARGET)
# 退出码: 0 = 已存档或无需存档, 3 = 可能正在部署, 不敢动(需人工)

source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "$REPO_ROOT"

TARGET="$(git rev-parse -q --verify "${1:-}^{commit}" 2>/dev/null || true)"
[ -n "$TARGET" ] || die "park.sh: 缺少/无效的 TARGET 参数: ${1:-<空>}"

MAIN="$(git rev-parse main)"
APPLY_DIR="$(git rev-parse --git-path rebase-apply)"
MERGE_DIR="$(git rev-parse --git-path rebase-merge)"

DONE="" FROM="" STOPPED="" BRANCH=""

if [ -d "$APPLY_DIR" ] || [ -d "$MERGE_DIR" ]; then
  # ---- rebase 停在半路 ----
  if [ -d "$APPLY_DIR" ]; then D="$APPLY_DIR"; else D="$MERGE_DIR"; fi
  ONTO="$(cat "$D/onto")"
  BRANCH="$(sed 's#^refs/heads/##' "$D/head-name" 2>/dev/null || true)"
  STOPPED="$(cat "$D/original-commit" 2>/dev/null || cat "$D/stopped-sha" 2>/dev/null || true)"
  if [ -z "$STOPPED" ] && [ -f "$D/next" ]; then
    # 停在两个补丁之间: 下一个补丁文件头的 "From <sha>" 就是还没应用的那个提交
    STOPPED="$(head -n1 "$D/$(printf '%04d' "$(cat "$D/next")")" 2>/dev/null | awk '{print $2}')"
  fi
  [ -n "$STOPPED" ] || die "park.sh: 认不出 rebase 停在哪个提交($D), 不存档"
  # 常规轮 onto = TARGET; 续跑轮 onto = 上次的 resume-done(建在 TARGET 之上) —— 两种都对
  if ! git merge-base --is-ancestor "$TARGET" "$ONTO"; then
    log "⚠ rebase 的 onto($ONTO) 不在 TARGET($TARGET) 之上, 以 onto 为准"
    TARGET="$(git rev-parse "$ONTO")"
  fi
  DONE="$(git rev-parse HEAD)"
  FROM="$(git rev-parse "$STOPPED~1")"
  git diff HEAD >"$RESUME_PARTIAL" 2>/dev/null || true
  [ -s "$RESUME_PARTIAL" ] || rm -f "$RESUME_PARTIAL"
  log "存档: rebase 停在 $(git log -1 --format='%h %s' "$STOPPED"), 已完成 $(git rev-list --count "$TARGET..$DONE") 个提交"
  git rebase --abort
else
  # ---- 不在 rebase 中: 要么 rebase 已做完(卡在对账/测试/构建), 要么根本没开始 ----
  BRANCH="$(git symbolic-ref -q --short HEAD || true)"
  case "$BRANCH" in
    syncing/*) ;;
    *) log "不在 rebase 中, 也不在 syncing/* 分支上(当前: ${BRANCH:-<游离>}), 无进度可存"; exit 0 ;;
  esac
  if git merge-base --is-ancestor "$TARGET" HEAD && [ "$(git rev-parse HEAD)" != "$MAIN" ]; then
    DONE="$(git rev-parse HEAD)"
    FROM="$MAIN"   # 工作分支从 main 拉出, rebase 已全部做完 → 续跑时只需补 main 上的新提交(通常为空)
    log "存档: rebase 已完成($BRANCH), 续跑时直接从对账开始"
  else
    log "$BRANCH 还没 rebase 到 TARGET, 无进度可存"
  fi
fi

# 部署保护: 工作分支上已经打了 sync/* 标签 = 可能已进入 deploy.sh, 此时不碰任何东西
if [ -n "$DONE" ] && git tag --points-at "$DONE" | grep -q '^sync/'; then
  log "⚠ $DONE 已打 sync/* 标签, 可能正在/已经部署 —— 不存档、不切分支, 需人工确认"
  exit 3
fi

# 恢复仓库: 回到 main, 丢掉工作分支(进度已由 resume-done 引用保住, 不会被 gc)
if [ -n "$DONE" ]; then
  git update-ref refs/upstream-sync/resume-done "$DONE"
fi
git reset -q --hard
git checkout -q main
[ -n "$BRANCH" ] && [ "$BRANCH" != main ] && git branch -D "$BRANCH" >/dev/null 2>&1 || true

if [ -z "$DONE" ]; then
  log "已恢复到 main(无进度可存)"
  exit 0
fi

ATTEMPTS=1
if [ -f "$RESUME_ENV" ]; then
  # shellcheck disable=SC1090
  PREV_TARGET="$(. "$RESUME_ENV"; echo "${RESUME_TARGET:-}")"
  PREV_ATTEMPTS="$(. "$RESUME_ENV"; echo "${RESUME_ATTEMPTS:-0}")"
  if [ "$PREV_TARGET" = "$TARGET" ]; then ATTEMPTS=$((PREV_ATTEMPTS + 1)); fi
fi

{
  echo "RESUME_TARGET=$TARGET"
  echo "RESUME_DONE=$DONE"
  echo "RESUME_FROM=$FROM"
  echo "RESUME_STOPPED=$STOPPED"
  echo "RESUME_MAIN=$MAIN"
  echo "RESUME_ATTEMPTS=$ATTEMPTS"
  echo "RESUME_PARKED_AT='$(date '+%F %T')'"
} >"$RESUME_ENV"
log "已存档到 $RESUME_ENV (第 $ATTEMPTS 次中断), 仓库已恢复到 main"
