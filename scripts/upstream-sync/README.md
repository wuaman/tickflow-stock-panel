# 上游同步 loop（二开运维工具）

每周三/六 19:03（北京时间）自动检查上游 [`shy3130/tickflow-stock-panel`](https://github.com/shy3130/tickflow-stock-panel) 有没有更新；有的话在**独立分支**上 rebase 到我们 31 个二开提交之上，对账验证、重建镜像、部署上线，失败自动回退，结果推飞书。

```
run.sh (cron)
  └─ gate.sh            上游有更新吗? 历史被重写了吗?   ← 没有更新就到此为止, 不烧 token
       └─ claude -p "/sync-upstream"   (skill: .claude/skills/sync-upstream/SKILL.md)
            ├─ 建 sync/<日期> 分支 → rebase → 解冲突
            ├─ 对账三关(提交数/文件范围/工作树) + 容器内 pytest
            ├─ deploy.sh   重建镜像 → 部署 → 健康检查 → 失败自动回退
            └─ notify.sh   推飞书 + 写 .sync-loop/last-run.json
```

## 安全设计（为什么它不会把线上搞挂）

| 机制 | 作用 |
|---|---|
| 全程在 `sync/<日期>` 分支 | `main` 只在部署成功后移动；rebase 失败/冲突解不动 → 丢分支，线上与 main 都不受影响 |
| `deploy.sh` 只部署带 `sync/<日期>` 标签的提交 | HEAD 与标签不一致直接拒绝 —— 未验证的树进不了线上 |
| 对账三关 | 二开提交标题逐条对账 + 文件范围对账，防"冲突解错悄悄丢上游代码" |
| 失败自动回退 | 健康检查不过就把镜像和数据都退回上一版，并告警 |
| 权限清单 `settings-sync-loop.json` | 无人值守时工具放行清单；配合 `--permission-prompts none`，清单外的操作**直接拒绝**而不是挂起等超时 |

## 文件

| 文件 | 作用 |
|---|---|
| `run.sh` | cron 入口：闸门 → 起 agent → 兜底告警 |
| `gate.sh` | 低成本判断上游有无更新 / 是否被 force push 重写 |
| `SKILL.md` | agent 行动手册（**权威版本**；`run.sh` 每次自动安装到 `.claude/skills/sync-upstream/`） |
| `settings-sync-loop.json` | 无人值守的权限清单（只放行 git/docker/本目录脚本） |
| `deploy.sh` / `rollback.sh` | 部署与回退 |
| `notify.sh` | 飞书推送（未配置 webhook 时静默落本地日志） |
| `config.sh` | 公共配置：容器名、构建参数、保留版本数、健康检查函数 |

> `.claude/` 被上游 gitignore，所以**改 SKILL.md 请改 `scripts/upstream-sync/SKILL.md`**，那个才是入库的权威版本。

## 一次性配置

### 1. 飞书机器人（不配也能跑，只是通知落本地日志）

飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 webhook 地址：

```bash
echo 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx' > data/user_data/feishu_webhook.txt
# 若机器人开了「签名校验」, 密钥另存一行:
echo '你的签名密钥' > data/user_data/feishu_webhook_secret.txt
```

两个文件都在 `data/`（已被 gitignore），不会进 git。

### 2. 给 cron 配无头认证

桌面 app 用的是宿主托管认证，`~/.claude/.credentials.json` 并不存在 —— 所以 cron 里的 `claude -p` 会报 `Not logged in`。本 loop 从独立的环境文件读凭据（**支持第三方中转站**）：

```bash
vi ~/.config/tickflow-sync/env      # 模板已建好, 按注释填 4 项
chmod 600 ~/.config/tickflow-sync/env
```

要填的 4 项：

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_BASE_URL` | 中转站地址，**不要带 `/v1` 后缀**（CLI 自己拼 `/v1/messages`，带了会变 `/v1/v1/messages`） |
| `ANTHROPIC_AUTH_TOKEN` | 中转说 "Bearer / Authorization" 时用这个；说 "API key / x-api-key" 时改用 `ANTHROPIC_API_KEY`。**只设一个**，都设时 AUTH_TOKEN 优先 |
| `ANTHROPIC_MODEL` | 主模型 ID（中转站的模型名通常和官方不同名）。**别用弱模型** —— 这个 loop 要解 rebase 冲突 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 后台小任务（标题/摘要）用的模型。**这一项直接关系账单**：用 bearer 中转而不钉它，后台任务会走你的主模型 |

验证（不跑业务、不碰容器）：

```bash
set -a; . ~/.config/tickflow-sync/env; set +a
claude -p "运行 git rev-parse --short HEAD, 把输出作为唯一回复"
```

返回 commit 短哈希 = 认证 + 工具调用都通了；返回纯文本 = 工具调用有问题；401 = 凭据变量选错了（两个变量对调再试）。

其他说明：

- 文件在仓库外（`$HOME/.config/tickflow-sync/env`，权限 600），不会被 git 看到；换路径用 `export TICKFLOW_SYNC_ENV=/path/to/env`。
- 走中转站时按量计费。若更想用订阅额度，可以在终端跑一次 `claude` → `/login`（两者不冲突，有环境变量时优先用环境变量）。
- 模板里已预设 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`（关遥测/版本检查，**副作用是 CLI 不再自动更新**）和 `API_TIMEOUT_MS=1200000`。
- 常见报错对应开关：`400 Extra inputs are not permitted` → `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`；`400` 且消息涉及 `thinking`/`adaptive` → `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`；中转要额外 header → `ANTHROPIC_CUSTOM_HEADERS`。模板第 5 节都有。

### 3. 装 cron（每周三/六 19:03 北京时间）

```bash
( crontab -l 2>/dev/null; echo '3 19 * * 3,6 /mnt/SDCard/codeSpace/tickflow-stock-panel/scripts/upstream-sync/run.sh' ) | crontab -
```

时区依赖：cron 用本机时区，宿主机是 `Asia/Shanghai`（已确认）。若哪天改了主机时区，这里的时间会跟着漂，需要同步调整。

## 日常用法

```bash
scripts/upstream-sync/gate.sh                     # 只看上游有没有更新(退出码 10 = 没有)
scripts/upstream-sync/rollback.sh --list          # 本地保留了哪些可回退版本
scripts/upstream-sync/rollback.sh --to 20260913   # 回退(镜像 + 代码一起回)
scripts/upstream-sync/deploy.sh --version 20260920  # 手动部署某个已验证版本
```

在 Claude Code 里直接 `/sync-upstream` 也能手动跑一轮完整同步。

**验证模式**：做完 rebase、对账、测试就停下，不部署，留给人先看 rebase 结果：

```bash
SYNC_LOOP_STOP_BEFORE_DEPLOY=1 bash scripts/upstream-sync/run.sh
# 看过没问题想上线: bash scripts/upstream-sync/deploy.sh --version <版本>
```

## 飞书通知长什么样

报告以**使用者视角**写，不是提交清单。固定四段（`SKILL.md` §8 里有完整模板）：

| 段落 | 内容 |
|---|---|
| 🎉 新功能(你能用上的) | **报告的重点**。用人话说明"去哪儿、能干什么"，例如「AI 对话助手 — 悬浮球/⌘K 呼出，直接问『今天大盘怎么样』…」。不确定的宁可不写，绝不编。 |
| 🔧 修复(影响日常使用的) | 只挑影响数据口径、指标错值、页面显示的，纯内部重构不列。 |
| ⚙️ 本次同步做了什么 | 上游提交数、是否被 force push 重写、冲突数、对账与测试结论、**被丢的提交也要如实写出**。 |
| 🧩 冲突怎么解的 | 每个冲突文件一行：上游改了什么 / 我们怎么合。这是最需要人复核的部分。 |
| 📦 版本与回退 | 当前版本 + 回退命令 + `rollback.sh --list`。 |

报告超过飞书单条长度上限时会被截断，并附 `…(报告过长, 已截断)` 标记 —— 不会静默丢内容。

## 状态与日志

| 路径 | 内容 |
|---|---|
| `.sync-loop/runs/<轮次ID>/` | **每轮一份归档**（见下），轮次 ID 形如 `20260920-1101` |
| `.sync-loop/last-run.json` | 最近一轮的结构化结果（每轮覆盖；归档里存有副本） |
| `.sync-loop/last-report.md` | 最近一次实际推送给飞书的正文 |
| `.sync-loop/gate.env` | 最近一次闸门结论（上游 sha / 是否重写 / 新增数） |
| `.sync-loop/versions.tsv` | 已部署版本 → 镜像 ID / git 标签 / 时间（保留最近 3 个） |
| `.sync-loop/logs/` | 每轮 stdout 与通知留痕 |

### 每轮归档里有什么

`run.sh` 每轮结束（**含"上游无更新"和失败的情况**）都会调 `archive-run.sh` 存一份：

| 文件 | 内容 |
|---|---|
| `summary.md` | **人读的说明**：本轮结论 / 带来的新功能 / 冲突文件 / 可回退版本 / 推送正文 |
| `session.jsonl` | **无头 agent 的完整工具调用轨迹**（harness 的转录会随会话清理消失，这份是保底） |
| `report.md` | 实际推送给飞书的正文 |
| `run.log` | `run.sh` 的完整 stdout/stderr |
| `run.json` | 结构化结果（上游范围、重演数、测试结论、回退命令…） |
| `gate.env` / `gate-subjects.txt` | 本轮上游变更清单 |

保留最近 10 轮，旧的自动清理。手工归档某一轮（例如补记一次人工操作）：

```bash
scripts/upstream-sync/archive-run.sh --list
scripts/upstream-sync/archive-run.sh --runid 20260920-1101 --note "手工补部署" --session <某个.jsonl>
```

`data/` 不参与本 loop 的任何写入 —— 数据永远是安全的。

## 回退粒度

同一版本号在三处一致：git 标签 `sync/<版本>`、镜像标签 `$IMAGE:<版本>`、`versions.tsv` 一行。回退时三者一起回，避免出现"代码是旧的、镜像是新的"这种最难查的状态。

保留 3 个版本；基线版本（`baseline-*`，首次运行时把当时的运行镜像登记下来）永不删除 —— 它是最后的保底。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 飞书没收到通知 | 检查 `data/user_data/feishu_webhook.txt`；`notify.sh --dry-run` 可只落本地日志 |
| 收到 `Not logged in` | `~/.config/tickflow-sync/env` 里凭据没填；或把变量写成了空字符串（空串也算已设置） |
| 请求 401 | 凭据变量选错了：`ANTHROPIC_AUTH_TOKEN` 与 `ANTHROPIC_API_KEY` 对调再试 |
| 路径变成 `/v1/v1/messages` | `ANTHROPIC_BASE_URL` 多带了 `/v1` 后缀，去掉 |
| cron 跑了但什么都没发生 | 上游没更新（正常，看 `.sync-loop/logs/`）；或 CLI 未登录 |
| 收到"未正常结束"告警 | agent 被权限拦停/超时，看日志尾部；线上未被改动即代表安全 |
| agent 说"命令被拒绝" | 权限判断按**整条命令的首个程序**匹配：`echo "==="; git diff` 这种复合命令会被整条拒（里面的 git 在白名单也没用）。现状是放行整个 Bash 工具，若你收紧了清单请记住这条 |
| 东财源 Connection refused | shim 没跟 app 一起重启 —— `docker restart TickFlow_FinancialShim` |
| 上游 force push 重写历史 | gate 会报 `rewritten=true`，skill 改用 `git rebase --onto` 重演 |
