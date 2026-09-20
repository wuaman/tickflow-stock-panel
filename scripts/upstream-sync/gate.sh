#!/usr/bin/env bash
# 闸门: 判断上游有没有新提交, 以及上游是否 force push 重写了历史。
#
# 退出码:
#   0  = 有更新, 可以继续(产物见 $GATE_ENV / $GATE_SUBJECTS)
#   10 = 无更新, 静默结束(不消耗 agent token)
#   2  = 环境异常(fetch 失败等)
#
# 这个脚本只读: 不建分支、不改工作树、不碰容器。

source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "$REPO_ROOT"

git fetch origin --prune --tags -q || die "git fetch origin 失败(网络?)"

UPSTREAM="$(git rev-parse origin/main)"
LAST="$(git rev-parse -q --verify "$SYNC_REF" 2>/dev/null || echo '')"

# 上游被 force push 重写的两个判据: 记录点不再是上游祖先, 或本地与上游已无共同祖先
REWRITTEN=false
if [ -z "$(git merge-base HEAD origin/main 2>/dev/null || true)" ]; then
  REWRITTEN=true
elif [ -n "$LAST" ] && ! git merge-base --is-ancestor "$LAST" origin/main 2>/dev/null; then
  REWRITTEN=true
fi

if [ -n "$LAST" ] && [ "$LAST" = "$UPSTREAM" ] && [ "$REWRITTEN" = false ]; then
  log "上游无更新 (origin/main = $UPSTREAM)"
  {
    echo "STATUS=none"
    echo "UPSTREAM=$UPSTREAM"
    echo "LAST_SYNC=$LAST"
    echo "REWRITTEN=false"
    echo "NEW_COUNT=0"
  } >"$GATE_ENV"
  exit 10
fi

# 新增提交数: 只有历史连续时才数得准; 被重写时报 "?"
if [ "$REWRITTEN" = false ] && [ -n "$LAST" ]; then
  NEW_COUNT="$(git rev-list --count "$LAST..origin/main")"
else
  NEW_COUNT="?"
fi

# 上游最近的提交标题(供 agent 写报告; 重写时多给一些)
git log --format='%h %ad %an %s' --date=short -n 60 origin/main >"$GATE_SUBJECTS"

{
  echo "STATUS=update"
  echo "UPSTREAM=$UPSTREAM"
  echo "LAST_SYNC=$LAST"
  echo "REWRITTEN=$REWRITTEN"
  echo "NEW_COUNT=$NEW_COUNT"
} >"$GATE_ENV"

log "上游有更新: origin/main=$UPSTREAM 上次同步=${LAST:-<未记录>} 新增=$NEW_COUNT rewritten=$REWRITTEN"
exit 0
