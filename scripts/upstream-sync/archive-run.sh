#!/usr/bin/env bash
# 把一轮 loop 的痕迹归档成 .sync-loop/runs/<轮次ID>/ —— 事后想查"那轮到底做了什么"就看这里。
#
# 产出:
#   summary.md       人读的说明(本轮做了什么 + 结论 + 报告正文)
#   run.json         本轮结构化结果(agent 写的 last-run.json 快照)
#   report.md        实际推送给飞书的正文
#   run.log          run.sh 的完整 stdout/stderr
#   session.jsonl    无头 agent 的完整工具调用轨迹(harness 转录)
#   gate.env / gate-subjects.txt   本轮闸门结论与上游提交清单
#
# 用法:
#   archive-run.sh                          # 自动判定轮次(取最新 run.log 的时间)
#   archive-run.sh --runid 20260920-1101 --note "手工补部署"
#   archive-run.sh --session <某个.jsonl>   # 手工指定转录(默认自动找本轮那个)
#   archive-run.sh --list                   # 看已归档的轮次

source "$(cd "$(dirname "$0")" && pwd)/config.sh"
cd "$REPO_ROOT"

KEEP_RUNS=10          # 保留最近多少轮的归档
RUNS_DIR="$STATE_DIR/runs"
PROJECT_SLUG="$(printf '%s' "$REPO_ROOT" | sed 's#/#-#g')"
TRANSCRIPT_DIR="$HOME/.claude/projects/$PROJECT_SLUG"

RUNID=""
NOTE=""
SESSION=""
RUN_LOG=""
SINCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runid) RUNID="$2"; shift 2 ;;
    --note) NOTE="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --run-log) RUN_LOG="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --list)
      [ -d "$RUNS_DIR" ] || { echo "(还没有归档)"; exit 0; }
      for d in "$RUNS_DIR"/*/; do
        [ -d "$d" ] || continue
        id="$(basename "$d")"
        res="$(python3 -c "
import json,sys
try:
    d=json.load(open('$d/run.json'))
    print('%s | %s | %s' % (d.get('result','?'), d.get('version') or '-', (d.get('upstream') or '')[:8]))
except Exception: print('(无 run.json)')" 2>/dev/null)"
        printf '  %-16s %s\n' "$id" "$res"
      done
      exit 0 ;;
    *) die "archive-run.sh 未知参数: $1" ;;
  esac
done

[ -n "$RUN_LOG" ] || RUN_LOG="$(ls -t "$LOG_DIR"/run-*.log 2>/dev/null | head -1 || true)"
[ -n "$RUNID" ] || RUNID="$(basename "${RUN_LOG:-run-$(date +%Y%m%d-%H%M)}" .log | sed 's/^run-//')"
RUN_DIR="$RUNS_DIR/$RUNID"
mkdir -p "$RUN_DIR"

log "归档本轮 → $RUN_DIR"

# 本轮的起点时间: run.sh 会传 SYNC_RUN_START_TS; 手工归档时用 --since 指定。
# 用它判定"某个文件是不是本轮的" —— last-run.json / last-report.md 都是"最近一次"的文件,
# 无更新那轮它们还是上一轮的内容, 照抄会让归档假称"本轮部署了 XX"(记录骗人比没有记录更糟)。
START_TS="${SINCE:-${SYNC_RUN_START_TS:-$(stat -c %Y "${RUN_LOG:-$RUN_DIR}" 2>/dev/null || echo 0)}}"
is_fresh() { [ -f "$1" ] && [ "$(stat -c %Y "$1" 2>/dev/null || echo 0)" -ge "$START_TS" ]; }

# 1) run.sh 的完整输出
[ -n "$RUN_LOG" ] && [ -f "$RUN_LOG" ] && cp -f "$RUN_LOG" "$RUN_DIR/run.log" && log "  run.log"

# 2) agent 写的结构化结果(仅当是本轮写的)
if is_fresh "$RUN_STATE"; then
  cp -f "$RUN_STATE" "$RUN_DIR/run.json" && log "  run.json"
else
  printf '{"result": "no_agent_run", "note": "本轮未运行 agent(闸门判定上游无更新, 或 agent 起步即失败); 不沿用上一轮的结果文件"}\n' \
    >"$RUN_DIR/run.json"
  log "  run.json (本轮未跑 agent, 记 no_agent_run)"
fi

# 3) 闸门结论 + 上游提交清单
[ -f "$GATE_ENV" ] && cp -f "$GATE_ENV" "$RUN_DIR/gate.env"
[ -f "$GATE_SUBJECTS" ] && cp -f "$GATE_SUBJECTS" "$RUN_DIR/gate-subjects.txt"

# 4) 实际推送出去的正文(仅当是本轮发的)
if is_fresh "$STATE_DIR/last-report.md"; then
  cp -f "$STATE_DIR/last-report.md" "$RUN_DIR/report.md" && log "  report.md"
else
  log "  (本轮没有推送, 不沿用上一轮的报告)"
fi

# 5) 无头 agent 的完整工具调用轨迹 —— 这是最有价值的一份, harness 的转录会随会话清理而消失
if [ -z "$SESSION" ] && [ -d "$TRANSCRIPT_DIR" ]; then
  START_TS="$(stat -c %Y "${RUN_LOG:-$RUN_DIR}" 2>/dev/null || echo 0)"
  # 优先取本轮开始之后被写过、且含本 loop skill 字样的转录, 避免误抓用户自己的会话
  # ⚠ 先把候选列表整个物化, 再在 shell 里循环 —— 原实现是
  #   `find … | sort -rn | awk … | while read f; do … break; done`
  #   : 命中即 break 会让 while 提前关闭管道, 上游 find/sort 收到 SIGPIPE,
  #   在 set -o pipefail 下整条命令替换返回 141 → set -e 直接中断脚本,
  #   后面的 summary.md / session.jsonl(本归档最有价值的两份)静默不产出。
  #   候选文件越多越容易触发, 实测 2026-09-23 那轮 100% 复现。
  CANDIDATES="$(find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' \
                -newermt "@$((START_TS - 120))" -printf '%T@ %p\n' 2>/dev/null | sort -rn)"
  while read -r _ts cand; do
    [ -n "$cand" ] || continue
    # 用进程替换而非管道: grep -q 命中即退出, 管道形态下 head 会拿到 SIGPIPE,
    # pipefail 会把"匹配成功"误判成失败而漏掉这份转录
    if grep -q 'sync-upstream' <(head -c 200000 "$cand"); then SESSION="$cand"; break; fi
  done <<<"$CANDIDATES"
fi
if [ -n "$SESSION" ] && [ -f "$SESSION" ]; then
  cp -f "$SESSION" "$RUN_DIR/session.jsonl" && log "  session.jsonl (来自 $(basename "$SESSION"))"
else
  log "  ⚠ 没找到本轮的 agent 转录(只归档其余部分)"
fi

# 6) 生成人读的说明
python3 - "$RUN_DIR" "$RUNID" "$NOTE" <<'PY'
import json, os, sys, io, datetime
run_dir, runid, note = sys.argv[1], sys.argv[2], sys.argv[3]

def read(p):
    try: return io.open(p, encoding='utf-8', errors='replace').read().strip()
    except Exception: return ''

d = {}
try: d = json.load(io.open(os.path.join(run_dir, 'run.json'), encoding='utf-8'))
except Exception: pass

L = []
L.append('# 上游同步 loop 轮次记录 %s' % runid)
L.append('')
if note: L.append('> %s' % note); L.append('')

L.append('## 本轮结论')
L.append('')
L.append('- 结果: **%s**' % (d.get('result') or '未知'))
if d.get('version'):  L.append('- 版本: `%s`' % d['version'])
if d.get('upstream'): L.append('- 上游: `%s`%s' % (str(d['upstream'])[:10],
                              '（上游改写过历史）' if d.get('upstream_rewritten') else ''))
if d.get('new_commits') is not None: L.append('- 上游新增提交: %s' % d['new_commits'])
if d.get('replayed') is not None:    L.append('- 重演二开提交: %s' % d['replayed'])
if d.get('tests'):    L.append('- 测试: %s' % d['tests'])
if d.get('reason'):   L.append('- 说明: %s' % d['reason'])
L.append('')

fr = d.get('new_features') or []
if fr:
    L.append('## 本轮带来的新功能')
    L.append('')
    for x in fr: L.append('- %s' % x)
    L.append('')

cf = d.get('conflicts') or []
cd = d.get('conflicts_detail') or []
if cd:
    # 冲突逐条细节刻意不进飞书(消息要短), 只在这里留 —— 这是事后复核的唯一依据
    L.append('## 冲突怎么解的（%s 处；飞书里只报计数）' % (len(cf) or len(cd)))
    L.append('')
    for x in cd: L.append('- %s' % x)
    L.append('')
elif cf:
    L.append('## 冲突文件（%d 处，未记录解决方式）' % len(cf))
    L.append('')
    for x in cf: L.append('- `%s`' % x)
    L.append('')

if d.get('retained'):
    L.append('## 可回退版本')
    L.append('')
    L.append('- %s' % ' / '.join('`%s`' % v for v in d['retained']))
    if d.get('rollback_cmd'): L.append('- 回退: `%s`' % d['rollback_cmd'])
    L.append('')

rep = read(os.path.join(run_dir, 'report.md'))
if rep:
    L.append('## 推送给飞书的正文')
    L.append('')
    L.append(rep)
    L.append('')

L.append('---')
L.append('')
L.append('本目录其他文件：`run.log`(完整输出) · `session.jsonl`(agent 的逐步工具调用轨迹) · `gate.env`/`gate-subjects.txt`(上游变更清单)')
io.open(os.path.join(run_dir, 'summary.md'), 'w', encoding='utf-8').write('\n'.join(L) + '\n')
print('  summary.md')
PY

# 7) 只保留最近 KEEP_RUNS 轮
n="$(ls -1 "$RUNS_DIR" 2>/dev/null | wc -l)"
if [ "$n" -gt "$KEEP_RUNS" ]; then
  ls -1 "$RUNS_DIR" | sort | head -n "$((n - KEEP_RUNS))" | while read -r old; do
    rm -rf "${RUNS_DIR:?}/$old" && log "  清理旧归档: $old"
  done
fi

log "归档完成: $RUN_DIR"
