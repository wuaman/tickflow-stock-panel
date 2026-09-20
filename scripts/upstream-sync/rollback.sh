#!/usr/bin/env bash
# 手动/自动回退到某个已登记版本(镜像 + 代码一起回, 避免代码与镜像不一致)。
#
# 用法:
#   rollback.sh --list                 # 看本地保留了哪些可回退版本
#   rollback.sh --to 20260913          # 回退到该版本
#   rollback.sh --to baseline-20260920 --quiet
#
# 退出码: 0=回退后健康检查通过  1=失败

source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "$REPO_ROOT"

TARGET=""
QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TARGET="$2"; shift 2 ;;
    --list)
      printf '%-18s %-14s %s\n' "版本" "git ref" "部署时间"
      [ -s "$VERSIONS_FILE" ] && awk -F'\t' '{printf "%-18s %-14s %s\n", $1, $3, $4}' "$VERSIONS_FILE"
      exit 0 ;;
    --quiet) QUIET=1; shift ;;
    *) die "rollback.sh 未知参数: $1" ;;
  esac
done
[ -n "$TARGET" ] || die "需要 --to <版本> (用 --list 查看)"

say() { [ "$QUIET" = 1 ] || log "$@"; }

docker images -q "$IMAGE:$TARGET" | grep -q . || die "本地没有镜像标签 $IMAGE:$TARGET"

GIT_REF="$(awk -F'\t' -v v="$TARGET" '$1==v {print $3}' "$VERSIONS_FILE" | tail -n1)"
LATEST_ROW="$(tail -n1 "$VERSIONS_FILE" | cut -f1)"

say "回退镜像: $IMAGE:$TARGET (git ref: ${GIT_REF:-<未记录>})"
docker tag "$IMAGE:$TARGET" "$IMAGE:latest" || die "打 latest 标签失败"

docker compose up -d --no-build --force-recreate || die "compose up 失败"
restart_shim

if [ -n "$GIT_REF" ] && git rev-parse -q --verify "$GIT_REF" >/dev/null; then
  say "代码切到 $GIT_REF (detached HEAD; main 分支本身不动)"
  git checkout --detach "$GIT_REF" --quiet
fi

if wait_app_healthy 40 && wait_shim_healthy 20; then
  say "✅ 已回退到 $TARGET 且健康检查通过"
  [ "$QUIET" = 1 ] || printf '回到最新版: scripts/upstream-sync/rollback.sh --to %s\n' "$LATEST_ROW"
  exit 0
fi

log "回退后健康检查仍失败 —— 需要人工介入"
exit 1
