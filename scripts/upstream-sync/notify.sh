#!/usr/bin/env bash
# 飞书群机器人推送(二开运维通知)。
#
# 用法:
#   notify.sh --title "标题" --level ok|warn|fail [--file report.md]
#   echo "正文" | notify.sh --title "标题" --level ok
#   notify.sh --title X --dry-run      # 只落本地日志, 不发
#
# 配置: 把 webhook URL 单独一行写进 data/user_data/feishu_webhook.txt
#       若机器人开了"签名校验", 把密钥写进 data/user_data/feishu_webhook_secret.txt
#       (两个文件都在 gitignore 覆盖的 data/ 下, 不会进 git)
#
# 未配置 webhook 时不报错, 只落日志 —— 保证 loop 在配置好之前也能跑通。

source "$(cd "$(dirname "$0")" && pwd)/config.sh"

TITLE="上游同步通知"
LEVEL=ok
BODY_FILE=""
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --level) LEVEL="$2"; shift 2 ;;
    --file) BODY_FILE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) die "notify.sh 未知参数: $1" ;;
  esac
done

if [ -n "$BODY_FILE" ]; then
  # ⚠ 不能用 [ -f ]: 调用方常用 <(printf ...) 进程替换, 那是管道不是普通文件
  BODY="$(cat "$BODY_FILE" 2>/dev/null)" || die "报告文件读不到: $BODY_FILE"
else
  BODY="$(cat)"
fi
BODY="$(printf '%s\n\n---\n%s · %s' "$BODY" "$(date '+%F %T')" "$(hostname)")"

# 留一份"最近一次实际推送的正文", 供 archive-run.sh 归档进 .sync-loop/runs/<轮次>/report.md
printf '%s\n' "$BODY" >"$STATE_DIR/last-report.md"

WEBHOOK="$(head -n1 "$WEBHOOK_FILE" 2>/dev/null | tr -d ' \t\r\n' || true)"
SECRET="$(head -n1 "$WEBHOOK_SECRET_FILE" 2>/dev/null | tr -d ' \t\r\n' || true)"

if [ "$DRY" = 1 ] || [ -z "$WEBHOOK" ]; then
  log "未发飞书(未配置 webhook 或 --dry-run), 落本地: $LOG_DIR/notify.log"
  printf '===== %s [%s] %s =====\n%s\n' "$(date '+%F %T')" "$LEVEL" "$TITLE" "$BODY" >>"$LOG_DIR/notify.log"
  exit 0
fi

# 无论成功与否都留一份本地记录 —— 否则"飞书到底发出去了什么"事后无从核对。
# 本地存完整正文(便于事后找全), 超出飞书上限时注明"实际发出的是截断版"。
SENT_NOTE=""
if [ "${#BODY}" -gt 4500 ]; then SENT_NOTE=" (超出飞书上限, 实际发出的是前 4500 字符 + 截断标记)"; fi
printf '===== %s [%s] %s (已发飞书%s) =====\n%s\n' \
  "$(date '+%F %T')" "$LEVEL" "$TITLE" "$SENT_NOTE" "$BODY" >>"$LOG_DIR/notify.log"

TITLE="$TITLE" LEVEL="$LEVEL" BODY="$BODY" WEBHOOK="$WEBHOOK" SECRET="${SECRET:-}" \
  python3 - <<'PY'
import base64, hashlib, hmac, json, os, sys, time, urllib.request

title, level = os.environ["TITLE"], os.environ["LEVEL"]
webhook, secret = os.environ["WEBHOOK"], os.environ["SECRET"]
template = {"ok": "green", "warn": "orange", "fail": "red"}.get(level, "blue")

LIMIT = 4500                      # 飞书单元素有长度上限, 超了整条会发不出去
body = os.environ["BODY"]
if len(body) > LIMIT:             # 静默截断会让人以为"报告就这些", 所以留个明确标记
    body = body[: LIMIT - 40] + "\n\n…(报告过长, 已截断)"

payload = {
    "msg_type": "interactive",
    "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [{"tag": "markdown", "content": body}],
    },
}

if secret:
    ts = str(int(time.time()))
    string_to_sign = "%s\n%s" % (ts, secret)          # 飞书签名: key=时间戳+密钥, 消息体为空
    payload["timestamp"] = ts
    payload["sign"] = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    ).decode()

req = urllib.request.Request(
    webhook, data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    resp = json.load(urllib.request.urlopen(req, timeout=15))
except Exception as exc:                    # noqa: BLE001
    print("飞书推送失败: %s" % exc, file=sys.stderr)
    sys.exit(1)

if resp.get("code") not in (0, None):
    print("飞书返回异常: %s" % resp, file=sys.stderr)
    sys.exit(1)
print("飞书已推送: %s" % resp.get("msg", resp.get("code", "ok")))
PY
