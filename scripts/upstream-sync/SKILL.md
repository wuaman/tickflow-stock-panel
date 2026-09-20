---
name: sync-upstream
description: 检查上游 shy3130/tickflow-stock-panel 是否有更新; 有则在独立分支 rebase、硬指标对账、重建镜像并部署(失败自动回退)、推送飞书通知、保留 3 个可回退版本。每周三/六 19:03(北京时间) 由宿主机 cron 自动触发, 也可手动 /sync-upstream 运行。
---

# 上游同步 loop

把上游新提交合进本 fork 的二开之上, 验证后重建镜像并部署。**全程无人值守**, 所以每一步都要留下可核查的证据。

## 硬约束(违反其中任何一条都算这次运行失败)

1. **绝不直接在 `main` 上 rebase**。全部工作在 `syncing/<VER>` 分支上做, 只有部署成功才把 `main` 指过去。失败就丢掉分支, `main` 和线上都不受影响。
2. **不用 `git merge origin/main`**。上游会 force push 重写历史(2026-09-20 就发生过一次: `merge-base` 直接为空), merge 会把几百个重复提交搅进历史。
3. **部署只走 `scripts/upstream-sync/deploy.sh`**, 它带安全不变量: 只允许部署带 `sync/<VER>` 标签的提交, HEAD 与标签不一致直接拒绝。
4. **对账没全过就不部署**。宁可这次不同步、发一条"人工介入"通知, 也不要上一个可能丢了上游改动的版本。
5. **不碰 `data/`**、不改容器内的代码(不要用 `docker cp` 打补丁 —— 那会造成代码与镜像不一致)。
6. **调用本 loop 的脚本一律用仓库根的相对路径**:`bash scripts/upstream-sync/deploy.sh ...`。
   权限清单是按相对路径前缀匹配的,**绝对路径(`bash /mnt/.../deploy.sh`)永远会被拒绝**;
   而 `--permission-prompts none` 下拒绝是**静默的** —— 你会看到"没有输出/被拦", 不会看到询问。
   被拒绝时不要换着花样重试, 按第 9 节写 `blocked_by_permissions` 并通知。

## 0. 起点与命名

- `VER` = 当天日期 `YYYYMMDD`。
- **工作分支用 `syncing/<VER>`, 发布标签用 `sync/<VER>` —— 两者刻意不同名**。同名会让 git 引用产生歧义
  (解析顺序里 tags 优先于 heads), `git rev-parse sync/<VER>` 会静默解析成标签而不是分支。
- **本机 git 是 2.17**, 没有 `git switch` / `git restore`, `cherry-pick -q` 也不支持 —— 一律用
  `git checkout`; 需要引用标签时写全 `refs/tags/sync/<VER>`。
- 先读闸门产物 `.sync-loop/gate.env`(`run.sh` 已跑过 `gate.sh`)。手动触发时若产物过期(超过 10 分钟)或不存在, 先自己跑一次 `bash scripts/upstream-sync/gate.sh`。
- 找出**上次同步点 `BASE`**, 即本次要重演的那些二开提交的起点:

```bash
BASE="$(git rev-parse -q --verify refs/upstream-sync/last 2>/dev/null || true)"
if [ -z "$BASE" ]; then
  # 首次运行: 取最近一次 "Merge remote-tracking branch 'origin/main'" 合并的第二个父提交
  BASE="$(git log -1 --format=%P --merges --grep="Merge remote-tracking branch 'origin/main'" HEAD | awk '{print $2}')"
fi
git rev-parse "$BASE"   # 必须在 HEAD 历史里, 否则停下问人
```

记录**重演前的提交标题清单**, 后面要对账:

```bash
git log --no-merges --format=%s "$BASE"..HEAD > /tmp/pre-subjects.txt
wc -l /tmp/pre-subjects.txt
```

## 1. 建工作分支

```bash
git checkout -b "syncing/$VER" main
```

## 2. rebase

- `REWRITTEN=false` 且 `git merge-base HEAD origin/main` 非空 → 常规:

```bash
git rebase origin/main
```

- `REWRITTEN=true`(上游重写了历史) → 用 `--onto` 把我们的提交重演到新上游之上:

```bash
git rebase --onto origin/main "$BASE" "syncing/$VER"
```

merge 提交会被自动丢弃 —— 这是**对的**, 因为那些 merge 只是把旧上游历史并进来, 现在由新上游取代。

## 3. 解冲突

- 逐个文件看 `git status`, **优先保留上游的结构与重构, 把我们的语义改动重新表达进去**; 不要为了省事整体取 ours。
- 每个冲突文件都要在报告里留一行: 文件 / 谁改了什么 / 怎么解的。这是这次运行最需要人复核的部分。
- 解完确认无残留标记:

```bash
grep -rn '^<<<<<<< \|^>>>>>>> ' --include='*.py' --include='*.ts' --include='*.tsx' . | head
```

## 4. 对账(硬指标, 全部通过才继续)

```bash
# a) 我们的提交一条都不能少(标题+数量)
git log --no-merges --format=%s origin/main..HEAD > /tmp/post-subjects.txt
diff /tmp/pre-subjects.txt /tmp/post-subjects.txt
```

- 有缺失 → 查原因: `git log --oneline origin/main --grep=<缺失标题的关键词>`。若上游已独立实现了同一改动(提交变空被丢弃), 记为"上游已含", **继续**; 否则**停下**, 按失败处理。
- 多出条目 → 停下, 说明 rebase 把不该带的提交带进来了。

```bash
# b) 与上游的差异只应是我们预期的二开文件
git diff --name-only origin/main HEAD
```

- 出现没见过的文件 → 很可能是冲突解错或误带文件, **停下**。

```bash
# c) 工作树干净
git status --porcelain
```

## 5. 跑测试(容器内 pytest, 一次性容器, 别往运行容器里装东西)

```bash
docker run --rm \
  -v "$PWD/backend/app:/app/app" \
  -v "$PWD/backend/tests:/app/tests" \
  -v "$PWD/backend/scripts:/app/scripts" \
  -w /app tickflow-stock-panel-app \
  sh -c "uv pip install --python /app/.venv/bin/python pytest pytest-asyncio -q; \
         /app/.venv/bin/python -m pytest tests -q -p no:cacheprovider"
```

- **必须显式装 `pytest-asyncio`**, 漏装会让所有 async 测试假红。
- **必须挂 `backend/scripts`**, 否则 `test_probe_tickflow_pro_*.py` 收集期就报 `ModuleNotFoundError: scripts`。
- 判定"失败是本次引入的"要用对照: 本仓库有一批 flaky 并发测试(`backtest/test_matrix_strategy.py`、`test_pipeline_capacity.py`), 同一份代码重复跑结果都会变。**单跑一次的红不算证据**; 用 `git archive "$BASE" backend | tar -x -C /tmp/x` 抽对照版本跑同样命令, 比 FAILED 列表。
- 用完清掉 root 属主的缓存:

```bash
docker run --rm -v "$PWD/backend:/b" tickflow-stock-panel-app \
  sh -c 'find /b -name __pycache__ -type d -prune -exec rm -rf {} +'
```

## 6. 部署

**先看 `$STOP_BEFORE_DEPLOY`**(由 `config.sh` 从环境读取; =1 表示验证模式):

- `=1` → **跳过部署**。把 rebase 结果、冲突解决方式、对账与测试结论写进报告并推飞书, 状态文件写 `"result": "verified_not_deployed"`, 并在报告里给出人工放行方式(见下)。**不要**动容器、不要打 `sync/$VER` 标签之外的东西、不要移 `main`。
- `=0`(默认) → 继续下面的部署流程。

```bash
git tag -f "sync/$VER"          # deploy.sh 的安全不变量: 只部署带标签的提交
bash scripts/upstream-sync/deploy.sh --version "$VER"
```

`deploy.sh` 自己会: 重建镜像(`INCLUDE_STOCKSDK=1` + 阿里云 PyPI) → 重启 app → **重启 shim** → 健康检查(`/health` 与 shim `/health`) → 失败自动回退到上一版并告警。**不要绕过它手工 build/up**, 否则回退与版本登记都会断。

人工放行(验证模式下看过 rebase 结果后想上线):

```bash
bash scripts/upstream-sync/deploy.sh --version "$VER"    # 分支与标签都还在, 直接部署
# 然后按第 7 步收尾(移 main、更新 refs/upstream-sync/last、推 fork)
```

前端不需要单独构建 —— Dockerfile 里的 `frontend-builder` stage 会在 `docker compose build` 时一起 build。

## 7. 收尾(仅在部署成功时做)

```bash
git branch -f main refs/tags/sync/$VER     # main 指向已验证的提交
git checkout main
git branch -D "syncing/$VER"               # 工作分支使命完成; 版本由标签标记
```

⚠ `refs/upstream-sync/last` 要记的是**上游**的 commit, 不是我们 rebase 后的提交 —— 记错会让下一轮闸门误判"上游重写了历史":

```bash
git update-ref refs/upstream-sync/last "$(git rev-parse origin/main)"
```

保留 3 个版本: 旧的 `sync/<VER>` 标签只留最近 3 个(镜像标签由 `deploy.sh` 自动清理)。

```bash
git tag -l 'sync/*' | sort | head -n -3 | xargs -r git tag -d
```

推送 fork(**只在部署成功之后**):

```bash
git push fork main --force-with-lease
git push fork --tags --force-with-lease
```

## 8. 写状态文件 + 通知

写 `.sync-loop/last-run.json`(`run.sh` 靠它判断本轮是否正常运行):

```json
{"ts": "2026-09-20T19:52:00+08:00", "result": "deployed", "version": "20260920",
 "upstream": "0cd46f9", "new_commits": 31, "replayed": 29,
 "new_features": ["AI 对话助手(悬浮球/⌘K 呼出)", "自选页「加入日期」「加入以来」两列"],
 "conflicts": ["backend/app/api/watchlist.py"],
 "tests": "无新增失败(对照同步前 main)", "retained": ["20260920", "baseline-20260920"],
 "rollback_cmd": "scripts/upstream-sync/rollback.sh --to baseline-20260920"}
```

然后推送飞书(正文写进临时文件, 别用超长命令行):

```bash
bash scripts/upstream-sync/notify.sh --title "✅ 上游同步: 新增 <N> 项功能, 已上线 <版本>" --level ok --file /tmp/report.md
```

- 验证模式(`$STOP_BEFORE_DEPLOY=1`)下标题改用 `"🔎 上游同步已验证, 待放行: <N> 项功能"`, `--level warn`, 报告末尾附上放行命令。
- 需要人介入时用 `"⚠️ 上游同步需人工处理: <卡在哪一步>"`, `--level warn`。

### 写报告前必须先读上游变更(别只抄 commit 标题)

**这份报告的第一读者是使用者, 不是开发者** —— 他要的是"这次更新给我带来了什么", 而不是提交清单。所以先花几步读实际内容:

```bash
# 1) 上游这轮改了哪些文件、哪些是新增功能
git log --no-merges --format='%h %s' <上次同步点对应的上游提交>..origin/main
git diff --name-only <上次同步点> origin/main

# 2) 上游的 README 差异 —— 上游会用"用户能做什么"的语言描述新功能, 直接借用最准
git diff <上次同步点> origin/main -- README.md

# 3) 拿不准某个功能到底做了什么时, 看它的实际改动
git show --stat <某个上游提交>
git diff <上次同步点> origin/main -- <相关文件>
```

### 报告模板(飞书 markdown, **别用表格** —— 飞书卡片对表格支持差)

按"使用者最关心 → 次要"排序。飞书单条有长度上限(`notify.sh` 会在 4500 字符截断), 所以要**挑**不要全列:

```markdown
**🎉 新功能(你能用上的)**
1. **AI 对话助手** — 悬浮球 / 侧栏 AI 徽标旁 / ⌘K 都能呼出。直接问"今天大盘怎么样""600519 的财务和走势", 助手在你本地数据上调工具取数后逐字作答, 每次取数都能展开核对; 覆盖个股·大盘·板块·自选·持仓·信号·策略·因子。
2. **自选页多了「加入日期」「加入以来」两列** — 日K 图上也会标出"加入自选"那天(周末加的会落到最近交易日)。
3. ...

**🔧 修复(会影响你日常使用的)**
- ETF 监控不再拿上一交易日的快照误报
- 长假后 MA60 等指标不再算成空
- 板块轮动的「1 小时前」午后不再拿 11:30 的口径对照
- ...

**⚙️ 本次同步做了什么**
- 上游 40 个提交 (`0cd46f9` → `a31c169`)
- ⚠️ 上游改写过历史 → 用 `--onto` 把我们的 30 个二开提交重演到新上游之上
- 12 个文件有冲突, 逐个人工解决(见下)
- 对账 3/3 通过 · 测试无新增失败(与同步前 main 对照) · 健康检查通过
- 3 个提交变空被丢: 已逐一核对, 都是上游已含同样修复, 无内容丢失

**🧩 冲突怎么解的(这里最需要你复核)**
- `backend/app/api/watchlist.py` — 上游加了「加入日期」列的取数, 我们把自定义财务列接在它后面
- ...

**📦 版本与回退**
当前 `20260920`(v0.3.0)
回退目标 `baseline-20260920` —— 回退: `scripts/upstream-sync/rollback.sh --to baseline-20260920`
更早版本: `rollback.sh --list`
```

写法要求:

- **新功能必须写"使用者能做什么"**, 不要写"重构了 XX 模块""新增了 YY 接口"这类实现层描述。
  写完自问一句: 一个只在网页上点的人, 看完知道去哪儿、能干什么吗?
- **不确定就不要写**。宁可少写一条, 也不要猜一个功能是怎么用的 —— 报告是使用者唯一的知情来源, 编造比遗漏更糟。
- 修复只挑**影响日常使用**的(数据口径、指标错值、页面显示), 纯内部重构不列。
- 同步动作与冲突解决要**如实**: 被丢的提交、失败的测试、没做到的步骤, 全部写出来, 不要只报喜。
- 没有新功能时就直说"本次上游以修复为主, 无新功能", 别硬凑。

## 9. 失败时

- **rebase 冲突解不动 / 对账不过 / 测试红** → `git rebase --abort`(或 `git checkout main && git branch -D "syncing/$VER"`), 线上与 `main` 原封不动。发一条 `--level warn` 通知, 说清卡在哪一步、需要你决定什么, 状态文件写 `"result": "needs_human"`。
- **部署失败** → `deploy.sh` 已经自动回退并自己发了告警, 你只需在报告里补充冲突/测试结论。
- **上游重写历史且 `BASE` 找不到** → 停下问人, 别猜着 rebase。
- **命令被权限拒绝** → 状态文件写 `"result": "blocked_by_permissions"` 并在 `reason` 里写清是哪一步、
  哪条命令(照抄命令原文), 发 `--level warn` 通知。**不要**绕过权限(不要改用别的命令形态规避,
  那既不可靠又不该做)。线上未被改动即代表安全。

## 环境速查(本项目特有, 每条都踩过)

| 事项 | 正确做法 |
|---|---|
| 健康检查端点 | `/health`(不是 `/api/health`, 后者被 SPA 兜底返回 200 假绿灯) |
| shim 检查 | 从 app 容器内部探 `127.0.0.1:3021/health` → `{"ok":true}`(共享 netns, 宿主端口不通) |
| 重启 app 后 | **必须** `docker restart TickFlow_FinancialShim`, 否则东财源 Connection refused |
| 构建参数 | `INCLUDE_STOCKSDK=1` + `PYPI_INDEX=阿里云`(清华源对 ARM64 wheel 403) |
| 数据 | 全部在 `./data`(bind mount), 重建镜像不会丢; 不要动 |
| 镜像落后 git | 别 `docker cp` 单文件; 要改就重建镜像(本 loop 就是干这个的) |
| 时区 | 宿主机已 `Asia/Shanghai`; 别在脚本里做 UTC 换算 |
| 本机 git | **2.17**(旧): 没有 `git switch`/`git restore`, `cherry-pick -q` 不支持 —— 用 `git checkout`; 引用标签写 `refs/tags/sync/<VER>` |
| 同名引用 | 分支与标签同名会让 `rev-parse` 静默选错(tags 优先), 所以分支叫 `syncing/*`、标签叫 `sync/*` |
