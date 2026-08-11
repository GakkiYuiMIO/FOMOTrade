# FOMOTrade 实现计划与进度

> 设计文档：[design.md](design.md)（唯一权威规格）
> 本文件只记录**做到哪了**和**下一步做什么**。

---

## 阶段划分

| Phase | 内容 | 出口条件 | 状态 |
|---|---|---|---|
| **0** | `--login` + `--probe`：拿 token、dump 端点、核对 17 项字段清单 | 17 项全部打勾；定下走 `HttpFomoClient` 还是 `PlaywrightFomoClient` | ⏳ **需要用户操作** |
| **1** | 配置 / 数据结构 / 建表 / 归一化纯函数 | 建表成功；纯函数单测通过 | ✅ 完成 |
| **2** | client + poller 最小闭环：拉取 → 归一化 → 去重落库 → `--dry-run` | 跑 10 分钟无重复无崩溃，落库数据肉眼核对正确 | ⏳ 待 Phase 0 |
| **3** | formatter + TG 推送打通，五类事件全接 | 七种消息模板在真实 TG 里渲染正确 | ⏳ 待 Phase 0 |
| **4** | bot 命令层 + 基线建立 | `/add` `/del` `/list` `/status` 可用；验收用例 1/2/9 通过 | ⏳ 待 Phase 0 |
| **5** | 功能 A/B：徽章 + 共识计数 | 验收用例 3~8 全部通过 | ✅ 逻辑完成，待真实数据验证 |
| **6** | 长跑稳定性：token 续期、429 退避、`sent` 补发、异常告警 | 连续跑 24h 无人工干预 | ⏳ 待 Phase 0 |

---

## 已完成

- [x] 项目骨架：`.gitignore`（先于 `git init` 落地，杜绝 `.env`/session 有任何一瞬间可提交）、`pyproject.toml`、`requirements.txt`、`.env.example`、`README.md`
- [x] `src/config.py` —— `FomoSettings` 独立 `BaseSettings`；路径常量；`mask()` 脱敏
- [x] `src/logger.py` —— loguru 初始化；脱敏正则**补了 `_-` 字符类**（原 claudeTrade 版本会漏掉 Privy 的 JWT 和 refresh token）
- [x] `src/notifier.py` —— `send()` + `get_updates()`；429 按 `retry_after` 退避；4000 字符截断保护
- [x] `src/models.py` —— `FomoEvent`、计价币白名单、归一化纯函数
- [x] `src/store.py` —— 4 张表、`tx()` 显式事务、功能 A/B 全部判定逻辑
- [x] `src/auth.py` —— Privy 登录态获取 + refresh token 自动续期
- [x] `src/client.py` —— `FomoClient` 协议 + `HttpFomoClient`(curl_cffi) + `PlaywrightFomoClient`
- [x] `src/formatter.py` —— 七种场景消息模板
- [x] `src/poller.py` —— 两阶段 tick、seeding、共识计算、归一化
- [x] `src/bot.py` / `src/cli.py` —— TG 命令层与 CLI 入口
- [x] `tests/` —— **204 项测试全绿（0.89s）**，`ruff check` 全通过

### 集成评审修复（并行开发后的契约漂移，逐条已修）

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 2.1 | `AuthError` 被 `_fetch_snapshots` 的裸 `except` 吞掉 | token 过期后每 20s 空转刷日志，§3.5 要求的「🔐 登录态失效」TG 告警**永远发不出去**，用户以为监控还活着 | 加 `except AuthError: raise`；`seed_next_pending_user` 同理 |
| 2.2 | 方向判不出的转账被渲染成「📥 收到转入」 | 一笔实际是转出的记录显示成收到转入，**彻底的错误信息** | `_title_anchor` 前置 `side_unknown` 门 → 标题写「交易」；对手方改中性「对手方」；不再无条件加「转账获得」 |
| 2.3 | `load_unsent_recent` 的「10 分钟窗口」实际是「同一 UTC 日全部」 | 一条发不出去的消息被每 20s 重试一整天，积到几百条时单 tick 跑几十分钟，**正常推送被饿死** | 下界改用 `models.iso_minutes_ago()`，与 `now_iso()` 同一生产者 |
| 2.4 | `--dry-run` 会真发 TG，且该消息未转义 | 名不副实；昵称含一个裸 `<` 就 400 | `seed_next_pending_user(dry_run)` 透传 + `html.escape` |
| 2.5 | `Poller.run_forever` 与 `cli.cmd_run` 各一套调度器 | 前者是死代码且不回填 `last_tick_at`，谁改用它 `/status` 会静默失准 | 删掉 `Poller.run_forever`，调度只留 cli 一个入口 |
| 2.6 | 时间戳解析失败时静默兜底成 now | 字段名/单位判错 → 全部越过游标 → 首个 tick 把几百条历史轰进 TG，**日志里一个字都没有** | 新增 `_guard_ts` 批级检查：>50% 失败则丢批 + ERROR |
| 三.2 | `raw_get` 丢掉响应头 | probe #15/#16 永远只能标「无法验证」，Phase 0 打不满勾 | 返回值改三元组，probe 表实际检查限流头 |
| 三.3 | 「不允许边落库边推送」无任何测试保护 | 谁顺手改一行，单测全绿但线上共识数自相矛盾 | 新增 `tests/test_poller.py`（14 条） |
| 三.5 | `cli.py` 导入 `models._NETWORK_ALIASES` 私有名 | 改名即 ImportError | 加公开的 `known_networks()` |

> 评审同时确认：**无致命问题**，无循环导入，被冻结的 5 个契约文件未被改动，
> `judge_badge` 在 `upsert_stats` 之前、`upsert_stats` 只在真插入时调用、
> `mark_sent` 只在 `send()` 成功后调用 —— 三条关键时序全部正确。

---

## 下一步（需要用户操作）

Phase 0 是硬门槛，**必须由用户本人完成登录**（程序绝不代填密码）：

```powershell
.\bot.ps1 --login      # 弹浏览器，自己登录 FOMO
.\bot.ps1 --probe      # dump 端点原始 JSON + 打印字段核对表
```

probe 结果决定三件事：

1. **带 Bearer token 后 Cloudflare 放不放行** → 决定用 `HttpFomoClient`（轻量）还是 `PlaywrightFomoClient`（兜底）
2. **`swaps` 有没有 `networkId`** → 这是唯一的硬阻断项，没有的话功能 B（共识计数）整体不可用
3. **`swaps` 的分页参数** → 不能翻页就建不了历史基线，功能 A/B 永久降级为不显示

拿到 probe 输出后要做的收尾：
- [ ] 按真实字段名收窄 `poller.normalize_*` 里的 `pick()` 候选列表
- [ ] 核对时间戳单位（**用一笔已知时间的真实交易人工比对**，判错会导致"永远拉不到新数据"的静默失效）
- [ ] 用真实 CA 校准 `models.QUOTE_TOKENS` 计价币白名单
- [ ] 确认「交易次数」字段语义 → 决定是否启用 Q3 的单向否决票（`store.judge_badge` 里那段注释）
- [ ] 真实数据跑通验收用例 1/4/6/8/10

---

## 已知问题

| 问题 | 现状 | 处置 |
|---|---|---|
| `poetry install` 报连不上清华镜像，但 pip 走同一镜像正常 | 已复现（代理环境下） | 用 `requirements.txt` + pip 安装，效果等价；README 里已写明 |
| 全部 API 字段名未经实测 | 设计文档 §11 列了 17 项 | Phase 0 的 `--probe` 逐项核对 |
| `PlaywrightFomoClient` 不支持 swaps 分页 | 刻意为之 | 走该实现时 `stats_ready` 恒 0，功能 A/B 降级为不显示，**主推送不受影响** |

---

## 设计决策速查（改代码前先看这个）

| 决策 | 绝不能改成 | 后果 |
|---|---|---|
| `tick()` 必须「先全部落库，再统一渲染发送」两个独立循环 | 边落库边发 | 同一秒买同一个币的两人会看到 1 和 2 两个不同的共识数，功能 B 当场作废 |
| `judge_badge` 必须在 `upsert_stats` **之前**调用 | 之后 | `buy_count` 已 +1，🌱 永远判不出来 |
| `upsert_stats` 只在 `insert_event` 返回 True 时调用 | 无条件调 | 重复轮询让 `buy_count` 一路虚增 |
| `mark_sent` 只在 `send()` 返回 True 之后调用 | 发之前就标 | 崩在中间的消息永久丢失（下一轮被 `INSERT OR IGNORE` 跳过，再也不会被发现） |
| 共识只在 `user_token_stats` 上聚合 | 在 `fomo_events` 上聚合 | 拆单把一个人算 N 次 |
| 分子分母同一谓词 `active=1 AND stats_ready=1` | 两处不一致 | 算出 `6/8` 这种分子大于分母的输出 |
| 徽章 `None` 一律渲染成 🟢 | 渲染成 🌱 或报错 | 宁可漏标不可错标——🌱 一旦贬值没有第二次机会 |
| `stats_ready=0` 时 `upsert_stats` 直接跳过 | 照常写 | 污染基线，后续必然错标 🌱 |
