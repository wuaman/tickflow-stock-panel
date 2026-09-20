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

WEBHOOK="$(head -n1 "$WEBHOOK_FILE" 2>/dev/null | tr -d ' \t\r\n' || true)"
SECRET="$(head -n1 "$WEBHOOK_SECRET_FILE" 2>/dev/null | tr -d ' \t\r\n' || true)"

if [ "$DRY" = 1 ] || [ -z "$WEBHOOK" ]; then
  log "未发飞书(未配置 webhook 或 --dry-run), 落本地: $LOG_DIR/notify.log"
  printf '===== %s [%s] %s =====\n%s\n' "$(date '+%F %T')" "$LEVEL" "$TITLE" "$BODY" >>"$LOG_DIR/notify.log"
  exit 0
fi

TITLE="$TITLE" LEVEL="$LEVEL" BODY="$BODY" WEBHOOK="$WEBHOOK" SECRET="${SECRET:-}" \
  python3 - <<'PY'
import base64, hashlib, hmac, json, os, sys, time, urllib.request

title, level = os.environ["TITLE"], os.environ["LEVEL"]
body = os.environ["BODY"][:4500]           # 飞书单元素有长度上限, 超了整条会发不出去
webhook, secret = os.environ["WEBHOOK"], os.environ["SECRET"]
template = {"ok": "green", "warn": "orange", "fail": "red"}.get(level, "blue")

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
