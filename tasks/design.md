# FOMO 平台指定用户监控 → Telegram 推送 · 设计文档

> 状态：设计定稿，待实现
> 日期：2026-08-11
> 范围：监控 fomo.family 上指定用户的买入 / 卖出 / 观点 / 转入 / 转出，推送到 Telegram Bot

---

## 一、需求

监控 FOMO 平台（fomo.family）上指定用户的操作，实时推送到自己的 Telegram Bot。

用户已确认的决策：

| 项 | 决策 |
|---|---|
| 数据源 | FOMO 官方 API（方案 B），用自己的账号登录态调用 |
| 监控名单 | Telegram Bot 命令动态管理（`/add` `/del` `/list`），存 SQLite |
| 事件类型 | 买入 / 卖出 / 观点(thesis) / 转入 / 转出，**五类全要** |
| 过滤 | **全推，不过滤**；数据全量落库以便回溯 |
| 功能 A | 首次买入某币时要有明显提示 |
| 功能 B | 显示监控名单里有多少人买过这个币 |
| 共识口径 | 「买过」作主指标 + 「仍持有」作副指标（Q1） |
| 历史明细 | `/add` 时拉的历史只聚合，不写事件表（Q2） |
| 交易次数字段 | probe 确认语义后，启用为单向否决票（Q3） |

---

## 二、已确认的技术事实（逆向 fomo.family 前端 bundle 得到）

### 2.1 API 端点

Host：`https://prod-api.fomo.family`

| 用途 | 端点 |
|---|---|
| handle → userId | `GET /v2/users/userHandle/{handle}` |
| 用户详情 | `GET /v2/users/{userId}` |
| 买入 / 卖出流水 | `GET /v2/users/{userId}/swaps` |
| 当前持仓 | `GET /v2/users/{userId}/balances` |
| 持仓聚合快照 | `GET /v2/userTokens/aggregatedSnapshot?userId={id}&timestamp={ts}` |
| 转入 / 转出 | `GET /v2/transfers/with/{userId}` |
| 观点（thesis） | `GET /feed/token/thesis?tokenAddress={ca}&networkId={n}&afterTime={ts}&limit=100` |
| 全局动态流 | `GET /feed?limit={n}` |
| 排行榜 | `GET /v2/leaderboard?limit={n}` |

> FOMO 内部把「观点」叫 **thesis**。

### 2.2 鉴权

前端 `fomoFetch` 的实现（`/assets/fomoFetch-*.js`）：

```js
const headers = {
  "Content-Type": "application/json",
  Authorization: `Bearer ${await getPrivyAccessToken()}`,
  "X-Supported-Chains": getChains(),
};
fetch("https://prod-api.fomo.family" + path, { method: "GET", ...opts, headers });
```

- 鉴权走 **Privy**（第三方嵌入式钱包 / 登录方案）
- Privy appId：`cm6h485o300n3zj9yl6vpedq7`
- Privy clientId：`client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH`
- access token 短期有效，可用 refresh token 自动续期（`POST https://auth.privy.io/api/v1/sessions`）

### 2.3 已实测的障碍

- 匿名请求 → **Cloudflare WAF 403**（`server: cloudflare`，返回 "Attention Required!" 页面）
- 用 `curl_cffi` 的 Chrome TLS 指纹（`impersonate="chrome"`）**仍然 403** → 说明是「无合法 Bearer 即拦」的规则，不是 TLS 指纹问题
- `/profile/{handle}` 页面在 `layouts/authenticated` 下，匿名访问会被重定向回首页

**结论**：必须携带有效 Bearer token。带 token 后 Cloudflare 是否放行 **尚未验证**，这是实现第一步必须解决的问题（见 §八 Phase 0）。

---

## 三、架构

### 3.1 目录结构

本项目是**完全独立的仓库**（`E:\cryptoCode\FOMO` → `github.com/GakkiYuiMIO/FOMOTrade`），
与 claudeTrade 交易机器人无任何代码或运行时耦合。

```
FOMO/
├── .env / .env.example
├── .gitignore              ← 挡住 .env / session / probe dump / *.db*
├── pyproject.toml
├── bot.ps1                 快捷启动
├── README.md
├── data/                   (gitignored) fomo.db / fomo_session.json / fomo_probe/
├── logs/                   (gitignored)
├── tasks/
│   ├── design.md           本文档
│   └── todo.md             实现计划与进度
├── tests/
└── src/
    ├── __init__.py
    ├── config.py      FomoSettings（独立 BaseSettings）+ 路径常量 + mask()
    ├── logger.py      loguru 初始化 + token 脱敏
    ├── notifier.py    TelegramNotifier（send / get_updates）
    ├── models.py      FomoEvent + 归一化纯函数 + 常量区
    ├── store.py       SQLite（data/fomo.db）+ 功能 A/B 判定
    ├── auth.py        Privy token 获取 + 自动续期
    ├── client.py      FomoClient 协议 + HttpFomoClient / PlaywrightFomoClient
    ├── poller.py      轮询编排
    ├── formatter.py   FomoEvent → Telegram HTML
    ├── bot.py         TG 命令层（getUpdates 长轮询）
    └── cli.py         入口 --login / --probe / --check / --dry-run / --run
```

### 3.2 为什么是独立项目而不是 claudeTrade 的一个模块

1. **`claudeTrade/src/config.py:Settings` 对 `binance_api_key` 有必填 + 长度 + 占位符三重校验**。
   FOMO 监控若复用它，没配币安 Key 的机器直接启动失败 —— 而本项目根本不碰币安。
2. **`claudeTrade/src/main.py` 已 1800+ 行 / 75KB**，再往里塞 FOMO 命令只会更难维护。
3. **两个独立进程共用一个 SQLite 文件会抢写锁**，DB 必须分开。
4. 领域完全不同：一个是币安合约自动交易，一个是 Solana 社交平台只读监控。
   放一起没有任何复用收益，只有耦合成本。

### 3.3 从 claudeTrade 移植（复制，非 import）过来的部分

独立仓库后不能再跨项目 import，以下三处是**复制并按本项目需要改写**的：

| 本项目文件 | 源 | 改动 |
|---|---|---|
| `src/logger.py` | `claudeTrade/src/utils/logger.py` | 脱敏正则加入 `_-` 字符类（Privy 的 JWT 和 refresh token 含这两个字符，原正则漏掉）；加 `_INITIALIZED` 幂等保护；日志文件名 `trade_` → `fomo_` |
| `src/notifier.py` | `claudeTrade/src/notifier/telegram.py` | 新增 `get_updates()`（命令层需要）；新增 429 限流按 `retry_after` 退避重试；新增 4000 字符截断保护；支持代理 |
| `src/config.py` | `claudeTrade/src/config.py` | 只保留结构（`lru_cache` 单例 + `PROJECT_ROOT` 约定 + `field_validator`），字段全部重写 |

代码风格沿用：stdlib `sqlite3` 无 ORM、`_SCHEMA` 大常量字符串、loguru、argparse、**中文注释**。

### 3.4 数据流

```
[主线程 · APScheduler 每 20s]
  tick():
    1) seed_next_pending_user()          每 tick 最多为一个新用户建历史基线
    2) raw = 拉取所有 active 用户的四类数据（swaps / thesis / transfers / balances）
    3) 单事务内：归一化 → judge_badge → INSERT OR IGNORE → upsert_stats
    4) 全部落库完成后，再统一渲染 + 串行发送

[副线程 · getUpdates 长轮询]
  /add <handle>   /del <handle>   /list   /status   [可选 /who <CA>]
```

> **唯一的结构性约束：不允许边落库边推送。**
> 本 tick 全部 BUY 事件写完 stats 之后再统一渲染发送，否则同一秒买同一个币的两人会看到 1 和 2 两个不同的共识数，功能 B 当场失去可信度。
> 代价：推送延迟最坏 +20s。**明确接受。**

### 3.5 鉴权方案

```
首次：  python -m src.fomo.cli --login
        → Playwright 有头浏览器打开 fomo.family，用户手动登录（程序不碰密码）
        → 抓 Privy refresh token 存 data/fomo_session.json（必须进 .gitignore）

运行时：access token 快过期时自动用 refresh token 换新
        续期失败 → TG 告警「需要重新登录」+ 停止轮询，不空转刷日志
```

### 3.6 双 Client 实现（Phase 0 验证门）

```
python -m src.fomo.cli --probe   拿真实 token 打全部端点
   ✅ 200 → HttpFomoClient（curl_cffi 直连，轻量、快、内存占用小）
   ❌ 403 → PlaywrightFomoClient（页面上下文内 fetch，与真实 App 行为一致，必成但吃 ~300MB）
```

`FomoClient` 定义成 `Protocol`，两个实现可互换，切换只改一行配置。上层 poller / formatter / store 一行不用动。

> `PlaywrightFomoClient` 的 `iter_swap_buys()` 抛 `NotSupportedError` —— 在浏览器里翻 10 页 swaps 成本远高于收益。此时 `stats_ready` 恒 0，功能 A/B 整体降级为不显示，**主推送完全不受影响**。

---

## 四、功能 A —「首次买入」

> **总原则：功能 A/B 是增强信息，任何情况下都不得阻塞主推送。**
> 徽章与共识的计算全部包在 `try/except` 里，失败即 `None`，formatter 对 `None` 整行跳过。
> **绝不能出现「因为算不出共识数所以整条推送失败」。**

### 4.1 徽章三态

| 徽章 | 判定 | 渲染 |
|---|---|---|
| `FIRST` | `user_token_stats` 中该 `(user, network, token)` 不存在，或 `buy_count == 0` | 🌱 首次建仓 |
| `ADD` | `buy_count > 0` | 🟢 加仓 |
| `None` | 四道前置门任一命中 | 🟢 加仓，**绝不显示 🌱** |

**四道前置门**（任一命中 → 徽章 `None`，共识行一并不显示）：

1. `watch_users.stats_ready != 1` —— 历史基线没建好，判不了
2. `network_id` 或 `token_address` 缺失 —— 构造不出聚合键
3. 该代币命中**计价币白名单**（按 `(network_id, address)`）
4. 买卖方向无法确定（`side` 缺失且无法从计价币位置推断）

### 4.2 语义边界

- 「首次」= **首次市场买入**，不含转入 / 空投获得
- 判定半径 = seeding 回填窗口（默认 500 条 swaps）+ `/add` 当时的 balances 全量
- 徽章落库时判定并**永不重算**（存 `fomo_events.badge`）。后续乱序拉到更早的买入 → 更新 `first_buy_at`，但**不补发、不撤回、不发更正**，仅 INFO 日志
- `buy_count` 精确语义 = **记录到的 BUY 事件条数**。DEX 路由拆单会让它 +N，这是刻意接受的：它只作 `0 / >0` 门槛用，**拆单对 A/B 零影响**

### 4.3 为什么不用 API 的「交易次数」字段作主判据

截图里的 `🔄 交易 3 次` 暗示 API 可能返回累计交易次数，但**不作为主判据**：

1. 字段语义不可控 —— 可能是全网次数 / 含卖出 / 24h 窗口，probe 只能验证一时，改版即静默失效
2. 本地 `user_token_stats` 无论如何都必须存 —— 功能 B 靠它
3. 两套判据并存会产生「同一事件重放两次结果不同」的诡异 bug

**Q3 决策**：probe 确认语义后，启用为 **单向否决票** —— `tradeCount > 1` → 强制不标 🌱（**永不用它去肯定首次**）。否决是幂等的，不引入重放不一致。

⚠️ **语义未在 probe 中明确确认之前，绝不写这行**。若该字段实际是「全网交易次数」，则每笔买入都 `>1` → 🌱 永不出现且静默失效。确认后建议先跑一周对照日志（记录被否决次数）再固化。

---

## 五、功能 B —「共识计数」

```
watchlist = active=1 AND stats_ready=1 的人数              ← 分母
buyers    = 上述人群中，买过该代币的人数（含本人）           ← 主指标，分子
holders   = 上述人群中，本 tick balances 里仍持有该币的人数  ← 副指标，可缺失
```

渲染：`👥 名单内 3/12 人买过 · 2 人仍持有`

### 四条硬规则

1. **主指标是「买过」，不是「仍持有」**。「买过」只依赖本地库，**永不抖动**；「仍持有」依赖每 tick 的 balances 实时性，一次部分失败就会让同一个币在 3 和 1 之间来回跳。**能抖的数字不能当主指标。**
2. **`holders` 零持久化**。每 tick 本来就全量拉了所有 active 用户的 balances（消息里的 `📦 持仓` 就靠它），全名单持仓快照已在内存。数一次 `sum()` 即可 —— **不建列、不建表、不做对账、不设 EXITED 状态机**。
3. **分子分母同一谓词**（`active=1 AND stats_ready=1`）。口径不一致会算出 `6/8`，分子大于分母的那一刻这个数字在用户心里当场作废。
4. **共识一律在 `user_token_stats` 上聚合**（主键天然去重），**禁止在 `fomo_events` 上聚合** —— 拆单会把一个人算 N 次。

### 时间语义与降级

- **时间语义**：本 tick 全部落库完成之后的一致快照，同 tick 所有消息共用。天然包含「同一 tick 内一起买入的人」。
- **降级**：只要本 tick 有任一 `active & ready` 用户的 balances 请求失败，`holders` 段**整段消失**（只显示「N/M 人买过」）。宁可少一段，不可给个会跳的数。

---

## 六、两功能共享的排除规则

| 规则 | 内容 | 不做会怎样 |
|---|---|---|
| **计价币排除** | 硬编码计价币白名单，**按 `(network_id, address)`，绝不用 symbol**（假 USDC 遍地）。计价币不打徽章、不算共识、不进 stats，但**事件照常落库照常推送** | `$SOL` 的共识恒等于名单人数，功能 B 整体变噪音 |
| **转账不计入买入** | `TRANSFER_IN/OUT` **不改** `buy_count`。转入推送标注 `⚠️ 转账获得,非市场买入`，不显示徽章。**禁止用 balances diff 反推买入**（空投、领奖、内部划转都会造假买入） | 一批空投给 5 人 → 显示「5 人买过」，徽章打在一笔没花钱的仓位上 |

> **建模澄清（这是建模，不是过滤，不违反「全推」）**：
> 一笔 swap = 一条事件，方向由非计价币侧决定（`SOL→TOAD` 只产出「买入 TOAD」）。
> 两侧皆非计价币的币币互换产出两条（卖 A + 买 B）。
> 两侧皆计价币产出一条，落库推送但不进 stats。

---

## 七、数据模型

独立 DB 文件 `data/fomo.db`。风格严格对齐 `src/models/db.py`。

```python
# src/store.py

from src.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "fomo.db"
DB_PATH.parent.mkdir(exist_ok=True)

# 持仓额低于此值视为 dust，不计入「仍持有」。写死常量不做配置项：
# 它只影响副指标的边缘几例，给它一个开关反而让「当时这个值是多少」变成排查负担
HOLDING_MIN_USD = 1.0

_SCHEMA = """
-- ============ 监控名单 ============
CREATE TABLE IF NOT EXISTS watch_users (
    user_id      TEXT PRIMARY KEY,           -- FOMO userId（权威主键，handle 会改名）
    handle       TEXT NOT NULL,              -- @handle，仅展示，可变
    display_name TEXT,
    added_at     TEXT NOT NULL,              -- UTC ISO
    active       INTEGER NOT NULL DEFAULT 1, -- 软删除：/del 置 0 保留历史，再 /add 置回 1
    removed_at   TEXT,
    -- 【A/B】历史基线是否已建立。0 = 不打徽章、不计入共识分子分母
    stats_ready  INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_users_active ON watch_users(active);

-- ============ 事件流水（全量落库，不做任何过滤） ============
CREATE TABLE IF NOT EXISTS fomo_events (
    event_id      TEXT PRIMARY KEY,   -- 去重键：'{kind}:{原生id}'；无原生 id 时退化为
                                      -- sha1(kind|user|tx_hash|token|ts|amount|页内序号)
    event_type    TEXT NOT NULL,      -- BUY / SELL / THESIS / TRANSFER_IN / TRANSFER_OUT
    user_id       TEXT NOT NULL,
    handle        TEXT,
    network_id    TEXT,               -- 归一化后：solana / base / bsc；缺失为 NULL
    token_address TEXT,               -- 归一化后：EVM 转小写，Solana(base58) 保持原样
    token_symbol  TEXT,
    amount_usd    REAL,
    token_amount  TEXT,               -- 原始数量存字符串：memecoin 是 1e15 量级，REAL 丢精度
    price_usd     REAL,
    tx_hash       TEXT,               -- 兜底 event_id 的必要分量：等额拆单靠它才不会误去重
    event_ts      TEXT NOT NULL,      -- 事件发生时间（UTC ISO），时序比较的唯一基准
    ingested_at   TEXT NOT NULL,      -- 抓到的时间，用于诊断延迟 + 未发送补发窗口
    -- 【A】徽章在落库时判定并冻结，永不重算
    --      （否则重投时 stats 已含本笔，🌱 会退化成 🟢）
    badge         TEXT,               -- FIRST / ADD / NULL（数据不足）
    badge_reason  TEXT,               -- local_stats / no_baseline / quote_token / no_token_key / no_side / api_veto
    -- 【B】推送时的共识时点值。仅写入、不读取、不参与任何判定；
    --      存在的唯一理由：共识数是时点值，事后无法重算，
    --      而「共识数 vs 后续涨幅」是这个交易项目明确的回溯需求
    cs_buyers     INTEGER,
    cs_watchlist  INTEGER,
    sent          INTEGER NOT NULL DEFAULT 0,  -- 0=未发出。每 tick 末尾补发 10 分钟内未发出项
    raw_json      TEXT NOT NULL       -- 原始报文全量留存：字段语义未实测，必须留原始数据以便离线回填
);
CREATE INDEX IF NOT EXISTS idx_fomo_events_user_token ON fomo_events(user_id, network_id, token_address);
CREATE INDEX IF NOT EXISTS idx_fomo_events_ts   ON fomo_events(event_ts);
CREATE INDEX IF NOT EXISTS idx_fomo_events_sent ON fomo_events(sent);

-- ============ 游标（冷启动保护的唯一机制） ============
-- /add 落库时同一条 SQL 写入 cursor=now，历史事件因此永不进入推送。
-- 刻意不引入第二套抑制机制（watermark / suppressed 状态）：多套锁的优先级极易写错
CREATE TABLE IF NOT EXISTS fomo_cursors (
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,         -- swaps / transfers / thesis / balances
    cursor     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind)
);

-- ============ 【A/B】用户 × 代币 聚合状态 ============
-- 功能 A（首次判定）与功能 B（共识分子）的唯一事实源。
-- 只由 BUY 事件与 /add 基线驱动。
-- 刻意不含 holding_state / holding_usd：「仍持有」从本 tick 内存 balances 直接数，
-- 持久化持仓状态会引入一整套对账 + EXITED 状态机，
-- 且必然产生「新买入被判成已退出」的错误
CREATE TABLE IF NOT EXISTS user_token_stats (
    user_id       TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    token_address TEXT NOT NULL,
    buy_count     INTEGER NOT NULL DEFAULT 0,  -- 记录到的 BUY 事件条数（拆单会 +N；只作 0/>0 门槛）
                                               -- /add 基线里「当前持有但窗口内无买入记录」的老仓位写 1
    first_buy_at  TEXT,                        -- 最早买入时间；老仓位与兜底时间戳为 NULL
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, network_id, token_address)
);
-- 功能 B 主查询走这条索引，单条即可满足
CREATE INDEX IF NOT EXISTS idx_uts_token ON user_token_stats(network_id, token_address);
"""


@contextmanager
def get_conn():
    """autocommit + WAL（独立进程 + bot/poller 双线程，必须开）"""
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")     # 读不阻塞写
    conn.execute("PRAGMA busy_timeout = 5000")    # Windows 上锁竞争必备
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn):
    """
    显式事务。get_conn() 是 autocommit（isolation_level=None），不写 BEGIN 的话
    每条语句立即提交，「事件插入 + stats 更新」的原子性会静默丢失 ——
    崩在中间会让该事件永远被 INSERT OR IGNORE 跳过、stats 永远不更新，后续必然错标 🌱。
    用 BEGIN IMMEDIATE 而非 BEGIN：WAL 下立即取写锁，避免读锁升写锁时 SQLITE_BUSY。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

### 刻意不建的东西（附理由）

| 不建 | 理由 |
|---|---|
| `token_consensus` 物化计数表 | `user_token_stats` 只有几千行，实时 `COUNT` 亚毫秒；物化表在 `/del` 时要全表回撤该人所有币的计数，极易漏 |
| `fomo_balances` 快照表 / `holding_state` 三列 | 全名单持仓每 tick 已在内存。落一份每 20s 就重拉的数据，换来的是对账循环 + EXITED 状态机 + dust 抖动 + 「新买入被判已退出」四类 bug |
| `pre_existing` 标志列 | 它是「`buy_count==0` 且当前持有」的派生量，语义就是「至少买过一次，时间未知」—— 直接写 `buy_count=1, first_buy_at=NULL` 即可 |
| `watermark` 列 | 与 `fomo_cursors` 职责重复，且它从不推进，运行三个月后恒等于不存在 |
| `notify_state` 四态状态机 | TG 丢消息只留一列 `sent` 兜住即可 |
| `backfill_status` 四态 | 单线程同步执行，`running` 态没有观察者 |

---

## 八、判定逻辑

### 8.1 主循环

```python
def tick() -> None:
    """
    ⚠️ 唯一的结构性约束：不允许边落库边推送。
       本 tick 全部 BUY 事件写完 stats 之后，再统一渲染发送 ——
       否则同一秒买同一个币的两人会看到 1 和 2 两个不同的数字，功能 B 当场失去可信度。
       代价：推送延迟从「拉到即发」变成「tick 末尾统一发」，最坏 +20s。明确接受。
    """
    # --- 1) 每 tick 最多为一个新用户建立历史基线 ---
    seed_next_pending_user()

    # --- 2) 采集：拉完所有 active 用户的四类数据 ---
    raw = {u["user_id"]: fetch_all(u) for u in list_active_users()}

    # --- 3) 归一化 + 落库（单事务）：徽章在此判定并冻结 ---
    new_events = []
    with tx(conn):
        for ev in sorted(normalize(raw), key=lambda e: e.event_ts):   # 按事件时间升序
            badge, reason = judge_badge(conn, ev)          # ⚠️ 必须在 upsert_stats 之前
            if insert_event(conn, ev, badge, reason):      # INSERT OR IGNORE，rowcount==1 才 True
                if ev.event_type == "BUY" and reason in ("local_stats", "api_veto"):
                    upsert_stats(conn, ev)                 # ⚠️ 只有真插入了新行才更新
                new_events.append(ev)

    # --- 4) 渲染 + 发送：此时 stats 是一致快照 ---
    for ev in new_events + load_unsent_recent(conn, minutes=10):
        try:
            buyers, size = count_consensus(conn, ev)       # 一条 SQL
            holders = count_holders(raw, conn, ev)         # 内存 balances，可能为 None
        except Exception as e:
            logger.warning("共识计算失败，降级为不显示 | {} | {}", ev.event_id, e)
            buyers = size = holders = None                 # ⚠️ 绝不阻塞主推送
        if notifier.send(render(ev, buyers, size, holders)):
            mark_sent(conn, ev.event_id, cs_buyers=buyers, cs_watchlist=size)
        time.sleep(SEND_INTERVAL_S)                        # 串行 + 节流，规避 TG 限流
```

> **为什么不引入 `queue.Queue` 单写者**：真实量级是几十个用户 / 20s 一次，写事务毫秒级，`busy_timeout=5000` 给了约 1000 倍余量。bot 线程 `/add` 只做一条 INSERT，网络 IO 全在 poller 线程。为消除一个理论锁竞争而让 `/add` 变异步、用户最坏等 20s 才有回执，是拿必然发生的体验退化换极小概率问题。

### 8.2 徽章判定

```python
BADGE_FIRST = "FIRST"
BADGE_ADD   = "ADD"


def judge_badge(conn, ev) -> tuple[str | None, str]:
    """
    返回 (badge, reason)。badge=None 表示数据不足，一律不标 🌱 —— 宁可漏标，不可错标。
    ⚠️ 必须在把本事件 upsert 进 user_token_stats **之前** 调用，否则永远判不出 FIRST。
    """
    if ev.event_type != "BUY":
        return None, "not_buy"
    if ev.side_unknown:                                   # 方向不明：既不写 stats 也不显示共识
        return None, "no_side"
    if not ev.network_id or not ev.token_address:         # 构造不出聚合键
        return None, "no_token_key"
    if is_quote_token(ev.network_id, ev.token_address):   # 计价币不参与 A/B
        return None, "quote_token"

    u = get_watch_user(conn, ev.user_id)
    if not u or not u["stats_ready"]:                     # 基线没建好，判不了
        return None, "no_baseline"

    # 【Q3】probe 确认「交易次数」语义可靠后启用的单向否决票。
    # 只用于否定，永不用于肯定 —— 否决是幂等的，不引入「重放结果不同」。
    # ⚠️ probe 未确认语义前，这一行必须保持注释状态：
    #    若该字段实为「全网交易次数」，则每笔买入都 >1 → 🌱 永不出现，功能 A 静默全废
    # if ev.api_trade_count and ev.api_trade_count > 1:
    #     return BADGE_ADD, "api_veto"

    st = get_stats(conn, ev.user_id, ev.network_id, ev.token_address)
    if st is None or st["buy_count"] == 0:
        return BADGE_FIRST, "local_stats"
    return BADGE_ADD, "local_stats"


def upsert_stats(conn, ev) -> None:
    """
    ⚠️ 只在 insert_event 真插入了新行时调用，否则重复轮询会把 buy_count 反复累加。
    ⚠️ stats_ready=0 的用户直接跳过 —— 基线未建立期间的事件不得污染基线，
       否则 seeding 时「当前持有但无买入记录」的老仓位判据会失效，后续错标 🌱。
    first_buy_at 取 MIN：乱序拉到更早的买入时才不会把时间改晚。
    兜底时间戳（ts_fallback）不写 first_buy_at。
    """
    if not is_stats_ready(conn, ev.user_id):
        return
    fb = None if ev.ts_fallback else ev.event_ts
    conn.execute(
        """
        INSERT INTO user_token_stats (user_id, network_id, token_address,
                                      buy_count, first_buy_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(user_id, network_id, token_address) DO UPDATE SET
            buy_count    = buy_count + 1,
            first_buy_at = MIN(COALESCE(first_buy_at, excluded.first_buy_at),
                               COALESCE(excluded.first_buy_at, first_buy_at)),
            updated_at   = excluded.updated_at
        """,
        (ev.user_id, ev.network_id, ev.token_address, fb, _now_iso()),
    )
```

### 8.3 `/add` 与历史基线（seeding）

```python
def on_add_command(conn, handle: str) -> str:
    """
    bot 线程直接执行：一条 INSERT，毫秒级，立即回执。
    ⚠️ 输入归一化 strip() + 去前导 @ + lower()，以 user_id UPSERT ——
       重复 /add 同一人（不同大小写）绝不产生两行，否则共识把一个人算两次。
    """
    user_id, display = resolve_handle(handle)      # GET /v2/users/userHandle/{handle}
    u = get_watch_user(conn, user_id)
    if u and u["active"] and u["stats_ready"]:
        return f"ℹ️ {display} 已在监控中"           # 幂等：不重置已就绪的用户

    with tx(conn):
        conn.execute(
            """
            INSERT INTO watch_users (user_id, handle, display_name, added_at, active, stats_ready)
            VALUES (?, ?, ?, ?, 1, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                active = 1, removed_at = NULL, handle = excluded.handle,
                stats_ready = 0        -- /del 后回归也必须重建基线：空窗期的买入本地无记录
            """, (user_id, handle_norm, display, _now_iso()))
        # 冷启动保护的唯一机制：游标即刻设为 now，历史事件永不进入推送
        set_all_cursors_now(conn, user_id)
    return f"✅ 已加入 {display}，正在建立历史基线（约 1 分钟内完成）"


def seed_next_pending_user() -> None:
    """
    为一个 stats_ready=0 的用户建立功能 A/B 的基线。
    ⚠️ 只写 user_token_stats，**绝不写 fomo_events**（Q2 决策）——
       「不推历史」由 fomo_cursors 保证，不引入第二套抑制机制。
    终止条件：fomo_backfill_max_items（默认 500）条，内部硬上限 20 页。
    """
    u = pick_one_pending_user()
    if not u:
        return
    uid = u["user_id"]
    try:
        # 1) 分页拉 swaps，内存里聚合成 (net, ca) -> [笔数, 最早时间]
        agg: dict[tuple[str, str], list] = {}
        for ev in iter_swap_buys(uid, max_items=settings.fomo_backfill_max_items):
            if not ev.network_id or not ev.token_address:
                continue
            if is_quote_token(ev.network_id, ev.token_address):
                continue
            k = (ev.network_id, ev.token_address)
            if k in agg:
                agg[k][0] += 1
                agg[k][1] = min(agg[k][1], ev.event_ts)
            else:
                agg[k] = [1, ev.event_ts]

        # 2) balances 全量：兜住回填窗口外的老仓位
        balances = fetch_balances(uid)

        with tx(conn):
            for (net, ca), (cnt, first_ts) in agg.items():
                upsert_seed(conn, uid, net, ca, buy_count=cnt, first_buy_at=first_ts)
            for b in balances:
                net = normalize_network(b.get("networkId"))
                ca = normalize_token_address(b.get("tokenAddress"))
                if not net or not ca or is_quote_token(net, ca):
                    continue
                if float(b.get("usdValue") or 0) < HOLDING_MIN_USD:
                    continue
                # 当前持有 = 至少买过一次（时间未知）。INSERT OR IGNORE 保证不覆盖已知笔数。
                # 这一行同时做三件事：堵死「窗口外老仓位被误标 🌱」、
                # 让新人的历史持仓计入共识分子、替代掉整个 pre_existing 标志位机制
                conn.execute(
                    "INSERT OR IGNORE INTO user_token_stats "
                    "(user_id, network_id, token_address, buy_count, first_buy_at, updated_at) "
                    "VALUES (?, ?, ?, 1, NULL, ?)", (uid, net, ca, _now_iso()))
            conn.execute("UPDATE watch_users SET stats_ready = 1 WHERE user_id = ?", (uid,))

        notifier.send(f"✅ {u['handle']} 基线完成 · {len(agg)} 个代币")
    except NotSupportedError:
        # PlaywrightFomoClient 无法翻页 → 基线不可信 → stats_ready 保持 0 → A/B 整体不显示
        logger.warning("client 不支持 swaps 分页，{} 的功能 A/B 将保持降级", uid)
    except Exception as e:
        logger.warning("基线建立失败 user={} err={}，下一 tick 重试", uid, e)
```

### 8.4 共识计数

```python
def count_consensus(conn, ev) -> tuple[int | None, int | None]:
    """
    功能 B 主指标：名单内买过该代币的人数 / 名单总人数。
    ⚠️ 分子分母必须同一谓词（active=1 AND stats_ready=1），
       否则会算出 6/8 这种分子大于分母的输出。
    ⚠️ 统计对象必须是 user_token_stats（主键天然唯一），
       禁止在 fomo_events 上聚合（拆单会把一人算 N 次）。
    """
    if not ev.network_id or not ev.token_address:
        return None, None
    if is_quote_token(ev.network_id, ev.token_address) or ev.side_unknown:
        return None, None
    u = get_watch_user(conn, ev.user_id)
    if not u or not u["stats_ready"]:
        return None, None

    buyers = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM user_token_stats s
        JOIN watch_users w ON w.user_id = s.user_id
        WHERE w.active = 1 AND w.stats_ready = 1
          AND s.network_id = ? AND s.token_address = ? AND s.buy_count > 0
        """, (ev.network_id, ev.token_address)).fetchone()["n"]

    size = conn.execute(
        "SELECT COUNT(*) AS n FROM watch_users WHERE active = 1 AND stats_ready = 1"
    ).fetchone()["n"]
    return buyers, size


def count_holders(raw, conn, ev) -> int | None:
    """
    功能 B 副指标：本 tick 内存 balances 里仍持有该币的人数。零持久化。
    ⚠️ 只要有任一 active&ready 用户本 tick 的 balances 缺失，直接返回 None（整段消失）——
       部分覆盖会让同一个币的数字在 3 和 1 之间来回跳，比不显示糟得多。
    """
    ready = ready_user_ids(conn)
    if any(raw.get(uid) is None or raw[uid].balances is None for uid in ready):
        return None
    key = (ev.network_id, ev.token_address)
    n = sum(1 for uid in ready if held(raw[uid].balances, key, HOLDING_MIN_USD))
    # 索引延迟保护：balances 快照通常晚于 swaps 索引，买入者本人可能还没出现在自己的持仓里。
    # 一条「他刚买入」的消息配「0 人仍持有」会直接毁掉这个数字的可信度。
    if (ev.event_type == "BUY" and ev.user_id in ready
            and not held(raw[ev.user_id].balances, key, HOLDING_MIN_USD)):
        n += 1
    return n
```

### 8.5 聚合键归一化（最容易踩的坑）

```python
# 常量区放 models.py 顶部，与 src/models/db.py 里 _SCHEMA 大常量的写法一致 —— 不新增 constants.py
# ⚠️ probe 必须确认 FOMO 的实际取值，以下映射表只是外壳；未命中时原样返回小写
_NETWORK_ALIASES = {
    "solana": "solana", "sol": "solana", "1399811149": "solana",
    "base": "base", "8453": "base",
    "bsc": "bsc", "bnb": "bsc", "binance-smart-chain": "bsc", "56": "bsc",
}


def normalize_network(raw) -> str | None:
    """链标识归一化。probe 必须确认 swaps/balances/transfers 三处取值是否一致，
    不一致会让同一条链裂成两个聚合键，共识计数直接算错。"""
    if raw is None:
        return None
    k = str(raw).strip().lower()
    return _NETWORK_ALIASES.get(k, k)


def normalize_token_address(addr: str | None) -> str | None:
    """
    ⚠️ EVM hex 地址大小写不敏感，API 可能返回 checksum 混合大小写 → 必须 lower()，
       否则同一个币裂成两条记录，共识计数直接错。
    ⚠️ Solana 是 base58，**大小写敏感，绝不能 lower()**。
    判据用地址自身的编码形态（0x + 42 位），不用链名白名单 ——
    「这是不是 EVM 地址编码」是确定性的，「这是哪条 EVM 链」才是猜；
    用链白名单判会在遇到未知链（如 42161）时漏掉 lower()。
    """
    a = (addr or "").strip()
    if not a:
        return None
    return a.lower() if a.startswith("0x") and len(a) == 42 else a


def to_iso(v) -> tuple[str, bool]:
    """
    时间统一入口，返回 (iso, is_fallback)。
    秒 / 毫秒 / 微秒 / ISO 四种形态都可能出现（某些 Solana 侧接口返回微秒）。
    ⚠️ 档位判断不可靠，真正的兜底是**结果合理性断言**：
       解析结果必须落在 [2020-01-01, now+1d]，越界一律拒绝该字段、
       走「时间戳缺失」降级并 WARN。
       秒/毫秒判错会导致「永远拉不到新数据」的静默失效，
       是最危险的一类 bug，probe 必须人工核对一次。
    """
    ...
```

### 8.6 降级链

```
BUY 事件
 ├ 方向不明 / 无聚合键 / 计价币 / 基线未就绪 → badge=None，共识行整段消失 → 仍正常推送
 ├ 基线就绪 + buy_count==0 → 🌱 首次建仓
 └ 基线就绪 + buy_count>0  → 🟢 加仓

共识行
 ├ count_consensus 抛异常 → 整行消失（try/except 兜底）
 ├ 任一 ready 用户 balances 缺失 → 「N 人仍持有」段消失，只留「N/M 人买过」
 └ 正常 → 👥 名单内 3/12 人买过 · 2 人仍持有

client 不支持 swaps 分页（Playwright 实现）
 └ stats_ready 恒 0 → 功能 A/B 整体不显示，主推送完全不受影响
```

---

## 九、边界情况处置表

### A. 功能 A（首次买入）

| # | 场景 | 处置规则 | 理由 |
|---|---|---|---|
| A-1 | **冷启动失真**：库是空的 → 每条买入都 `buy_count==0` → 一屏全是 🌱 | `/add` 落库即建 `stats_ready=0`，下一 tick 建基线（500 条 swaps + 全量 balances，只写 stats）。未就绪期间徽章/共识全 `None`，消息尾附 `⏳ 基线建立中` | 一屏全 🌱 会让这个符号在用户心里当场作废，没有第二次机会 |
| A-2 | **回填窗口外的老仓位**被误判 🌱 | `/add` 时 balances 全量入库：凡当前持有、窗口内无买入记录的组合，直接写 `buy_count=1, first_buy_at=NULL` | 零额外 API 调用堵死整类误报，且不需要额外标志位列 |
| A-3 | **基线建立期间的实时事件污染基线** | `stats_ready=0` 时 `upsert_stats` 直接跳过（事件照常落库照常推送） | 不跳过的话，gap 期间写入的 `buy_count>0` 会让 A-2 判据失效，后续必然错标 |
| A-4 | **拆单**：一笔 $2500 被路由拆成 4 条 swap | ① `event_id` 唯一 + `INSERT OR IGNORE`；② 兜底 hash 必须掺 `tx_hash + 页内序号`（等额分片同秒同额，不掺会误去重、金额少报 75%）；③ `upsert_stats` 只在 `rowcount==1` 时调用；④ 第 2~4 笔判定时 `buy_count` 已 >0 → 自然变 🟢。**接受「一笔买入推成多条」，但绝不重复打 🌱** | `buy_count` 只作 0/>0 门槛，拆单对 A/B 零影响 |
| A-5 | **窗口外买过、且 `/add` 时已清仓**的代币再次买入 → 误标 🌱 | 已知限制。probe #7 若确认 `aggregatedSnapshot` 返回 per-token 首次买入时间，或 probe #8 确认交易次数语义（Q3 否决票），则该类误报被彻底消除 | 这是本设计唯一残留的「错标」路径，必须显式记录而非假装不存在 |
| A-6 | **`/del` → 两个月后 `/add` 回来** | `/add` 无条件把 `stats_ready` 置 0 并重建基线 | 空窗期的买入本地无记录，不重建会让空窗期建的仓位在下次加仓时被标 🌱 |
| A-7 | **handle 改名 / 大小写重复 add** | 主键是 `user_id`；`/add` 输入 `strip()` + 去前导 `@` + `lower()`，以 `user_id` UPSERT | 重复 `/add` 产生两行会让共识把一个人算两次 |
| A-8 | **事件乱序**：后拿到更早的买入 | `first_buy_at` 取 `MIN()`，但**不补发、不撤回、不发更正**，仅 INFO 日志 | 更正消息制造的混乱远大于收益 |

### B. 功能 B（共识计数）

| # | 场景 | 处置规则 | 理由 |
|---|---|---|---|
| B-1 | **计价币污染**：`SOL→TOAD` 若产出「卖出 SOL」，则名单所有人都「买过 SOL」 | 计价币白名单按 `(network_id, address)`，**绝不用 symbol**。计价币不进 stats、不打徽章、不算共识，但**事件照常落库照常推送** | `$SOL` 共识恒等于名单人数，功能 B 整体变噪音 |
| B-2 | **多链同 ticker**：Solana 的 $CATE 与 Base 的 $CATE | 聚合键 = `(network_id, token_address)`。`networkId` 缺失 → 不构造键；**禁止用 CA 长度猜链**（Base 与 BNB 之间必错） | — |
| B-3 | **同 tick 内两人买同一新币，一条显示 1 人一条显示 2 人** | 本 tick 全部落库完成后再统一渲染发送，所有消息共用一个快照。延迟最坏 +20s，明确接受 | 数字自相矛盾一次，功能 B 就永久失去可信度 |
| B-4 | **卖出消息里卖出者仍被算作「仍持有」** | 接受一 tick 滞后。文案上**不做任何时效承诺**（写「N 人仍持有」，不写「刚刚」） | balances 索引延迟于 swaps 是固有的 |
| B-5 | **部分用户 balances 拉取失败** | `holders` 整段消失，主指标 `buyers` **完全不受影响** | 部分覆盖会让数字在 3 和 1 之间来回跳，比不显示糟得多 |
| B-6 | **`/del` 后共识下降** | 软删除 `active=0`，共识 SQL 带 `WHERE active=1`。共识数下降是正确行为。`/del` 回执提示「相关代币共识数已下调」 | 语义是「我**现在**关注的这批人里有几个买过」 |
| B-7 | **分子分母跃迁**：新人基线完成瞬间从 `3/12` 跳到 `4/13` | 不回溯修改已发送消息。共识行措辞永远写「N 人买过」而非「N 人刚刚买入」 | 回溯改消息制造更大混乱 |
| B-8 | **转入/空投被当成买入** | stats 只由 BUY 事件与 `/add` 基线驱动；`TRANSFER_IN/OUT` 不改 `buy_count`。**禁止用 balances diff 反推买入** | 徽章会打在一笔没花钱的仓位上 |
| B-9 | **名单内部转账污染** | 对手方在名单内时消息标 `⚠️ 名单内转账` | 不标出来用户无法辨别筹码是不是在名单内搬家 |
| B-10 | **一人多号** | 只能在 `user_id` 层面去重，已知限制。缓解手段是可选的 `/who <CA>` | 自动识别马甲的误判成本远高于收益 |
| B-11 | **共识时间衰减**：三年前买过一次也算 | 不做硬性时间窗。基线窗口（默认 500 条）本身已是天然软衰减 | 硬时间窗会丢信息，且需要第三个数字来解释 |

### C. 工程

| # | 场景 | 处置规则 |
|---|---|---|
| C-1 | **SQLite 双线程写锁**（Windows 概率高于 Linux） | `WAL` + `busy_timeout=5000` + `synchronous=NORMAL`；bot 线程只做单条 INSERT，全部网络 IO 在 poller 线程；落库必须走 `tx()` |
| C-2 | **崩在「已落库、未发送」之间** → 消息永久丢失 | 单列 `sent`，每 tick 末尾补发 `sent=0 且 ingested_at` 在 10 分钟内的事件 |
| C-3 | **TG 限流**（同 chat ~20 msg/min） | 串行发送 + 固定间隔；429 按 `retry_after` 退避。`sent` 只在 TG 返回成功后置 1 |
| C-4 | **API 字段缺失 / 改版** | `normalize()` 里 `logger.warning` 一次；swaps 整体异常 `logger.error`。`/status` 展示每个用户的 `stats_ready` 与 stats 行数 |
| C-5 | **`FomoSettings` 误继承 `src/config.py:Settings`** | 必须独立 `BaseSettings`，代码注释写明原因 |
| C-6 | **`data/fomo_session.json` 泄漏登录态** | 必须加入 `.gitignore`；日志打印 token 时脱敏（沿用 lessons.md L1 的规矩） |

### 降级矩阵

| 缺失 / 异常 | 降级行为 | 用户看到 |
|---|---|---|
| `tokenAddress` / `networkId` 缺失 | 照常落库照常推送，不带徽章不带共识 | 正常消息，少两行 |
| `side` 无法推断 | 不写 stats、不打徽章、共识行也不显示 | 标题写「交易」，无徽章无共识 |
| USD 金额缺失 | 显示原生数量，A/B 不受影响 | `💰 买入 1,250,000 TOAD` |
| `timestamp` 缺失或越界 | 用拉取时刻兜底并标 `ts_fallback`，不写 `first_buy_at` | 无感 |
| 均价 / 交易次数字段不存在 | 对应行整行消失，**绝不本地推算** | 少一到两行 |
| API「交易次数」字段不存在 | **零影响** —— 本地 stats 是唯一事实源 | 无感 |
| balances 部分失败 | `holders` 段消失，`buyers` 不受影响 | 共识行短一截 |
| client 不支持 swaps 分页 | `stats_ready` 恒 0，A/B 整体不显示 | 所有消息带 `⏳` 尾行 |
| `swaps` 接口 404 / 改版 | `logger.error`，主推送继续跑其余三类 | 买卖消息消失，观点/转账仍在 |

---

## 十、消息模板

### 10.1 视觉铁律

> **标题行第一个字符必须是事件 emoji，且全局唯一，绝不被别的东西占用。**

全推场景一天几百条，快速滑动时人眼只稳定捕捉每条消息的第一个字符。保持行首 emoji 列干净，滑动时才是一条可读的色带。

| 事件 | 锚点 | 文案 |
|---|---|---|
| 首次建仓 | 🌱 | 首次建仓 |
| 加仓 | 🟢 | 加仓 |
| 卖出 | 🔴 | 卖出 |
| 观点 | 💭 | 发表观点 |
| 转入 | 📥 | 收到转入 |
| 转出 | 📤 | 转出 |

其余 emoji（💰📦📊💎👥🧬⚠️⏳）**只出现在第二列以后，永不占行首**。不设横幅行。

**「首次买入」怎么醒目而不廉价 —— 单点替换：**

| 不做 | 原因 |
|---|---|
| ❌ `🚨🚨🚨 首次买入！！！` | 全推下几十条同款，5 分钟就脱敏 |
| ❌ 独立横幅行 | 首次买入太常见（每人每新币一次），横幅贬值 |
| ❌ `【首次】` 中括号前缀 | 在行首把 emoji 锚点挤掉，破坏扫描色带 |

做法只有两步：① 🟢 → 🌱，**同一列同一位置同一个字符**，形状与颜色差异足够大，滑动时不需要阅读就能识别；② 文案 `加仓` → `首次建仓`。

**排版：不使用空行分组。** 手机端空行照样占行高，全推场景一屏只能放一条半消息是不可接受的。

### 10.2 完整示例

**场景 A — 首次买入（名单里第一个买的人）**

```html
🌱 <b>maxpain</b> · 首次建仓 · <b>$TOAD</b>
💰 买入 $2,500.00
📦 持仓 $2,498.10
💎 市值 $19.14M
👥 名单内 1/12 人买过
🧬 Solana
<code>A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump</code>
```

共识数是 1，他就是第一个 —— **不需要额外的 🥇 或「他是第 N 个」**。序数是会随 `/del` 变化的浮动量，把它渲染成一次性荣誉必然出现「同一个币两条名单首发」。

**场景 B — 首次买入，名单里已有 2 人买过**

```html
🌱 <b>cryptodude</b> · 首次建仓 · <b>$CATE</b>
💰 买入 $8,200.00
📦 持仓 $8,190.44
💎 市值 $4.02M
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Base
<code>0x8f2a4c19e7b3d5a0f16c88be2d7419aa3c05e6f1</code>
```

「3 人买过 · 2 人仍持有」里的差值本身就是负面信号（有人退出了），比额外写一句「已有 1 人退出」更短且信息量相同。

**场景 C — 加仓**（`📊 均价` 与 `🔄 第 N 次交易` 只在 API 提供时才有）

```html
🟢 <b>maxpain</b> · 加仓 · <b>$TOAD</b>
💰 买入 $1,200.00
📦 持仓 $12,355.49
📊 均价 $0.016
🔄 第 3 次交易
💎 市值 $19.14M
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Solana
<code>A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump</code>
```

> ⚠️ `📊 均价` 与 `🔄 第 N 次交易` **只透传 API 字段，绝不本地推算**。均价需要「累计买入 token 数量」，而 token 数量按设计存 TEXT、不做算术；`buy_count` 在拆单下会 +N，拿它当交易次数会给出错的数字。**显示一个错的次数比不显示更糟。**

**场景 D — 卖出**

```html
🔴 <b>0xsun</b> · 卖出 · <b>$TOAD</b>
💸 卖出 $8,355.49
📦 剩余 $0.00
💎 市值 $14.02M
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Solana
<code>A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump</code>
```

不设独立的「清仓」事件类型：清仓状态与 sell 事件天然不同步，强行区分必然误判。清仓自然体现为 `📦 剩余 $0.00`。

**场景 E — 发表观点**

```html
💭 <b>maxpain</b> · 发表观点 · <b>$TOAD</b>
<blockquote>筹码结构很干净，前十地址占 12%，dev 钱包已经烧掉。社区 meme 传播力强，短期看 50M。</blockquote>
📦 他持仓 $12,355.49
💎 市值 $19.14M
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Solana
<code>A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump</code>
```

- 正文用 `<blockquote>`（TG 渲染成左侧竖线），与数据行视觉分层
- **正文是用户生成内容，必须 `html.escape()`** —— 一个 `<` 就让整条消息 400 Bad Request，且这是可被投毒的攻击面
- 硬截断 500 字符，超长用 `<blockquote expandable>`（Bot API 7.4+，旧版退化为普通引用仍可读）
- `📦 他持仓` 的「他」字必须有，否则会被误读成 token 总量

**场景 F — 转入（名单内部转账）**

```html
📥 <b>cryptodude</b> · 收到转入 · <b>$TOAD</b>
💰 数量 1,200,000 ≈ $19,200.00
👤 来自 maxpain ⚠️ 名单内转账
⚠️ 转账获得，非市场买入
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Solana
<code>A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump</code>
```

**场景 G — 基线未就绪（前置门命中）**

```html
🟢 <b>newguy</b> · 加仓 · <b>$PEPE</b>
💰 买入 $500.00
💎 市值 $2.10B
🧬 Base
<code>0x6982508145454ce325ddbe47a25d4ec3d2311933</code>
⏳ 基线建立中，首次/共识暂不可用
```

### 10.3 CA 与链接：中国网络下的正确做法

> **Telegram 的内置浏览器不走 TG 自己的 MTProto 代理**（Android 走系统 WebView，iOS 走 SFSafariViewController）。即使消息秒到，点开 gmgn.ai 大概率白屏。**链接不能是主路径。**

**主路径：CA 独占最后一行，纯 `<code>`**

1. **点击 `<code>` 实体 = 一键复制到剪贴板**（iOS/Android 官方客户端原生行为）。这是唯一 100% 不依赖网络的操作，必须是主路径
2. **绝不截断 CA** —— 截断了就复制不了，整条消息实用价值归零。宁可换行
3. **前面不加「CA:」文字标签** —— tap-to-copy 命中区 = code 实体覆盖的字符范围，整行都是 code 时点哪都能复制
4. **绝不用 `<a href>` 包裹 CA** —— 那样点击变成跳转（大概率失败），把最可靠的操作换成了最不可靠的
5. `disable_web_page_preview=True` 必须开（现有 `TelegramNotifier.send` 已是）

> 若额外加 GMGN / DexScreener 链接行：链 slug 映射未命中时必须降级为不出链接 —— 错链的链接比没有链接更糟。

### 10.4 formatter 实现铁律

```python
# src/formatter.py 文件头注释
"""
1. 标题行第一个字符必须是事件 emoji，且全局唯一 —— 快速滑动时唯一的扫描锚点
2. 任何可选字段缺失时「整行消失」，绝不打印 "N/A" / "--" / "0"
3. badge 三态，None 表示数据不足 —— 宁可漏标不可错标，None 一律渲染成 🟢
4. 所有来自 API 的文本（handle / thesis 正文）必须 html.escape，否则一个 '<' 就 400
5. 不使用空行分组：手机端空行照样占行高，全推场景一屏只放得下一条半消息
"""
```

---

## 十一、probe 阶段必须验证的 API 字段清单

> **下面每一个字段名都是从前端 bundle 逆向推测的，未经实测。**
> `--probe` 必须把每个端点的完整原始 JSON dump 到 `data/fomo_probe/{endpoint}.json`，并打印「字段存在性 + 样本值」核对表，**逐项打勾后才能开始写 `normalize()`**。

### 阻断级（不确认就没法写代码）

| # | 端点 | 待验证 | 不符时的降级 |
|---|---|---|---|
| 1 | `/v2/users/{id}/swaps` | **是否有 `networkId`/`chainId`？数值还是字符串？** | 缺失 → 无法构造聚合键 → **功能 A/B 整体不可用**。唯一的硬阻断项 |
| 2 | `/v2/users/{id}/swaps` | 唯一 id 字段名（`id`/`txHash`/`signature`）是否稳定；**是否有 `txHash` 可用于兜底 hash** | 无稳定 id 且无 txHash → 等额拆单会误去重、金额少报，必须改用「页游标 + 页内序号」作分量 |
| 3 | `/v2/users/{id}/swaps` | 时间字段名与**单位**（秒/毫秒/微秒/ISO），是否 UTC | 判错会导致「永远拉不到新数据」的静默失效 —— **必须用已知交易人工核对一次** |
| 4 | `/v2/users/{id}/swaps` | 买卖方向：有 `side`/`type` 还是只能从 in/out 推断？单侧记录还是双侧（tokenIn/tokenOut）？ | 决定计价币排除与「一笔 swap = 一条事件」的实现方式 |
| 5 | `/v2/users/{id}/swaps` | **分页参数（`limit`/`offset`/`cursor`/`before`）、单页上限、总条数上限、保留期限** | 分页不可行 → seeding 只能靠 balances → `stats_ready` 保持 0 → **功能 A/B 永久降级** |
| 6 | `/v2/users/{id}/balances` | `tokenAddress` + `networkId` + `usdValue` 是否齐全；**是否一次调用返回全部链** | 无 `usdValue` → dust 判定退化为「数量 > 0」；分链调用 → `count_holders` 覆盖判据改成按链粒度 |

### 影响成本但不阻断

| # | 待验证 | 影响 |
|---|---|---|
| 7 | `aggregatedSnapshot` **是否直接返回 per-token 的首次买入时间 / 累计买入额** | 若是，seeding 从「多次分页」降到「1 次调用」，且 A-5 的残留误标路径被彻底消除。**最高优先级探索项** |
| 8 | 截图里「交易 3 次」对应的字段（`tradeCount`/`txCount`）：是否存在、挂在 swap 还是 balances、语义是「该用户对该币」还是「全网」、含不含卖出 | 决定 `🔄 第 N 次交易` 行是否渲染；以及是否启用 Q3 的单向否决票 |
| 9 | 是否有 per-token 的**均价 / 成本价**字段 | 决定 `📊 均价` 行是否渲染。本地无法推算 |
| 10 | swaps / balances / transfers **三处的 `networkId` 表示是否一致** | 不一致会让同一条链裂成两个聚合键 —— probe 时打印所有出现过的取值 |
| 11 | `/v2/transfers/with/{id}` 的 `direction`、对手方地址、**是否包含 swap 产生的 transfer** | 后者为真则同一笔 tx 推两条消息；处置：transfer 的 `tx_hash` 若已在当日 swaps 中则跳过 |
| 12 | `/feed/token/thesis` 是否同时返回 `tokenAddress` **和** `networkId`；`afterTime` 单位 | 缺 networkId 则观点事件无法显示共识 |
| 13 | 计价币在各链的实际 CA（SOL/WSOL/USDC/USDT/ETH/WETH/BNB/WBNB） | 白名单必须用真实 CA 填充，不能用 symbol |
| 14 | token 过期表现：401 还是 403？body 长什么样？ | 决定 auth 自动续期的触发条件 |
| 15 | `X-Supported-Chains` 取值格式，改值是否影响返回的 networkId 表示 | 同 #10 |
| 16 | 有无速率限制头 `X-RateLimit-*` | 决定 seeding 的分页间隔 |
| 17 | **带 Bearer token 后 Cloudflare 是否放行**（Phase 0 的核心问题） | 403 → 走 `PlaywrightFomoClient` |

---

## 十二、模块职责与工作量

| 文件 | 职责 | 规模 |
|---|---|---|
| `src/config.py` | `FomoSettings`（独立 `BaseSettings`）。配置项：token/chat_id、轮询间隔、`fomo_backfill_max_items=500`、client 实现选择 | 极小 |
| `src/auth.py` | Privy 登录态获取（Playwright 一次性）+ refresh token 自动续期 + 失效告警 | 中 |
| `src/client.py` | `FomoClient` Protocol；`HttpFomoClient`（curl_cffi）；`PlaywrightFomoClient`（`iter_swap_buys` 抛 `NotSupportedError`） | 中 |
| `src/models.py` | `FomoEvent` dataclass；计价币白名单 + `_NETWORK_ALIASES` 常量区；`normalize_network` / `normalize_token_address` / `to_iso` / `is_quote_token` 四个纯函数 | 小 |
| `src/store.py` | `_SCHEMA`（4 张表）；`get_conn` / `tx`；`judge_badge` / `upsert_stats` / `count_consensus` 等 | **中（主要工作量）** |
| `src/poller.py` | `tick()` 主循环；`seed_next_pending_user()`；`count_holders()`；`load_unsent_recent()` | **中** |
| `src/formatter.py` | 按 §十 实现 | 中 |
| `src/bot.py` | `/add`（直接写库 + 立即回执 + 幂等）/ `/del` / `/list` / `/status`；可选 `/who <CA>` | 小 |
| `src/cli.py` | `--login` / `--probe`（含字段核对表输出）/ `--dry-run` / `--run` | 小 |

**不动的东西**：`src/models/db.py`（fomo 用独立 `data/fomo.db`）、`src/notifier/telegram.py`、`src/main.py`、主交易进程的任何代码。**不新增 `constants.py`**（常量放 `models.py` 顶部）。

---

## 十三、验收用例（每条对应一个必现 bug）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 空库 + `/add maxpain`（3 年历史） | **0 条历史事件推送**；`user_token_stats` 有 N 行；`fomo_events` 仍为空 |
| 2 | `/add` 后基线未就绪时发生买入 | **推送照常发出**，无徽章无共识，尾行 `⏳ 基线建立中`；该事件**不写 stats** |
| 3 | 基线窗口外的老仓位（当时持有）再次买入 | 徽章 `ADD`，不是 `FIRST` |
| 4 | 一笔买入被路由拆成 4 条等额同秒 swap（无原生 id） | 4 条都落库、金额合计不少报；🌱 只出现 1 次；共识里该用户只算 1 人 |
| 5 | 名单 12 人，其中 4 人基线未就绪，3 人持有 | 共识显示 `k/8`，**分子必定 ≤ 分母** |
| 6 | 同一 tick 内 A、B 首次买同一新币 | 两条消息的共识数**都是 2**；两条都标 🌱 |
| 7 | swap 另一侧是 SOL | `$SOL` 不进 stats、不打徽章、不算共识；相关事件仍落库仍推送 |
| 8 | 某用户 balances 拉取失败 | 「N 人仍持有」段消失；「N/M 人买过」与徽章**完全不受影响** |
| 9 | 同一人 `@Maxpain` / `maxpain` 各 add 一次 | 只有一行 `watch_users`，共识只算 1 人，第二次回复「已在监控中」 |
| 10 | thesis 正文含 `<script>` | 消息正常发出（已 escape），不 400 |

---

## 十四、实现阶段划分

| Phase | 内容 | 出口条件 |
|---|---|---|
| **0** | `--login` + `--probe`：拿到 token，dump 全部端点原始 JSON，逐项核对 §十一 清单 | 17 项全部打勾；确定走 `HttpFomoClient` 还是 `PlaywrightFomoClient` |
| **1** | `config` / `models` / `store`：配置、数据结构、建表、归一化纯函数 | 建表成功；归一化纯函数单测通过 |
| **2** | `client` + `poller` 最小闭环：拉 swaps → 归一化 → 去重落库 → `--dry-run` 打印（不推 TG） | 跑 10 分钟无重复、无崩溃，落库数据肉眼核对正确 |
| **3** | `formatter` + TG 推送打通；四类事件全接 | 五种消息模板在真实 TG 里渲染正确 |
| **4** | `bot.py` 命令层 + `seed_next_pending_user` 基线建立 | `/add` `/del` `/list` `/status` 可用；验收用例 1/2/9 通过 |
| **5** | 功能 A/B：徽章 + 共识计数 | 验收用例 3~8 全部通过 |
| **6** | 长跑稳定性：token 续期、429 退避、`sent` 补发、异常告警 | 连续跑 24h 无人工干预 |

> **Phase 0 是硬门槛**：§十一 的字段假设一个都没实测过，probe 结果可能推翻 §八 的部分实现细节（但不会推翻架构 —— 双 Client 抽象和「A/B 失败不阻塞主推送」的原则就是为此而设）。

---

## 十五、安全与合规注意事项

沿用 `tasks/lessons.md` 已有的规矩：

- **L1 严禁硬编码任何凭据**：Privy token / refresh token / TG Bot Token 全部走 `FomoSettings`，其他文件不得重复读取
- **日志脱敏**：打印 token 时必须 `token[:8] + "***"`
- 本功能**只读 FOMO 接口，不做任何交易操作**，不触碰用户资产
- 登录环节由用户在有头浏览器里自己完成，**程序不经手密码**

### ⚠️ `.gitignore` 现存缺口（Phase 0 第一件事就补上）

当前 `.gitignore` 只有 `data/*.db` 和 `data/*.db-journal`，**挡不住本功能要落地的三类新文件**：

```gitignore
# FOMO 监控 —— 登录态与原始数据，绝不提交
data/fomo_session.json     # 含 Privy refresh token，泄漏 = 账号被接管
data/fomo_probe/           # probe dump 的原始 API 响应，含账号与钱包信息
data/*.db-wal              # SQLite WAL 模式产生，现有规则只挡了 -journal
data/*.db-shm
```

前两条是**安全问题**（凭据 + 个人数据），后两条是卫生问题（WAL 模式必然产生这两个文件，现有 `data/*.db-journal` 规则覆盖不到）。
