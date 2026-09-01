"""
SQLite 持久化 —— 监控名单 / 事件流水 / 游标 / 用户×代币聚合状态

- 用 sqlite3 stdlib,不引入 ORM
- 单文件 data/fomo.db(与 claudeTrade 的 trade.db 完全隔离:
  两个独立进程共用一个 SQLite 文件会抢写锁)
- 功能 A(首次买入判定)与功能 B(共识计数)的全部判定逻辑都在本文件

⚠️ 本文件是 poller / bot 的唯一数据入口。改这里的函数签名前先 grep 调用点。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from loguru import logger

from src.config import DATA_DIR
from src.copytrade import CopyConfig
from src.models import (
    BADGE_ADD,
    BADGE_FIRST,
    COUNTABLE_REASONS,  # noqa: F401  —— hot_tokens / token_buyers 的 SQL 参数用到
    EVENT_BUY,
    EVENT_TRANSFER_IN,
    REASON_LOCAL_STATS,
    REASON_NO_BASELINE,
    REASON_NO_SIDE,
    REASON_NO_TOKEN_KEY,
    REASON_NOT_BUY,
    REASON_QUOTE_TOKEN,
    FomoEvent,
    is_quote_token,
    iso_minutes_ago,
    now_iso,
)

DB_PATH = DATA_DIR / "fomo.db"

# 持仓额低于此值视为 dust,不计入"仍持有"。
# 写死常量不做配置项:它只影响副指标的边缘几例,
# 给它一个开关反而让"当时这个值是多少"变成排查负担。
HOLDING_MIN_USD = 1.0

# 四类数据的游标 kind
CURSOR_KINDS = ("swaps", "transfers", "thesis", "balances")


_SCHEMA = """
-- ============ 监控名单 ============
CREATE TABLE IF NOT EXISTS watch_users (
    user_id      TEXT PRIMARY KEY,           -- FOMO userId(权威主键,handle 会改名)
    handle       TEXT NOT NULL,              -- @handle,仅展示,可变
    display_name TEXT,
    added_at     TEXT NOT NULL,              -- UTC ISO
    active       INTEGER NOT NULL DEFAULT 1, -- 软删除:/del 置 0 保留历史,再 /add 置回 1
    removed_at   TEXT,
    -- 【功能 A/B】历史基线是否已建立。0 = 不打徽章、不计入共识分子分母
    stats_ready  INTEGER NOT NULL DEFAULT 0,
    -- 特别关注:这个人的推送要加醒目标识。纯展示,**不影响任何判定**
    -- (不改徽章、不改共识分子分母、不改采集频率),所以哪怕它错了也只是不好看
    starred      INTEGER NOT NULL DEFAULT 0,
    -- 【转入逐条推送】这个人的 TRANSFER_IN 够金额就逐条推,且转账采集不再参与轮转。
    -- ⚠️ 与 starred **语义不同,绝不合并**:starred 是纯展示(推送加 ⭐),
    --    这一位会实打实地改采集频率与推送量 —— 全名单打开约 807 条/天。
    --    它存在的理由:有些人在别处成交、币是转进来的,对这些人来说「收到」
    --    才是他动手的那一刻。是不是这种情况由用户自己判断,我们只推可证的事实。
    watch_transfer_in INTEGER NOT NULL DEFAULT 0,
    -- 这个人**自己**的转入门槛(美元)。⚠️ 可空,**NULL = 跟着全局
    --    fomo_transfer_watch_min_usd 走**,不是 0 —— 0 是"这个人全推"这个真实意图。
    -- ⚠️⚠️ 刻意另起一列,**绝不把 watch_transfer_in 改成存金额**:
    --    1. `watch_transfer_in = 1` 是 transfer_watch_user_ids / remove_watch_user
    --       / poller._refresh_watched_index 一路在用的谓词,改成存金额之后这些查询
    --       的语义当场变掉(只有门槛恰好 1 美元的人才算"开着"),而且一声不吭。
    --    2. 「门槛 0」与「关掉」会撞成同一个值 —— 用户要的"这个人全推"
    --       会被静默解释成"这个人不推"。
    --    开关归开关、门槛归门槛,两件事就是两列。
    transfer_in_min_usd REAL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_users_active ON watch_users(active);
CREATE INDEX IF NOT EXISTS idx_watch_users_handle ON watch_users(handle);

-- ============ 事件流水(全量落库,不做任何过滤) ============
CREATE TABLE IF NOT EXISTS fomo_events (
    event_id      TEXT PRIMARY KEY,   -- 去重键,见 models.make_event_id
    event_type    TEXT NOT NULL,      -- BUY / SELL / THESIS / TRANSFER_IN / TRANSFER_OUT
    user_id       TEXT NOT NULL,
    handle        TEXT,               -- 展示名(displayName)
    user_handle   TEXT,               -- @handle
    network_id    TEXT,               -- 归一化后:solana / base / bsc;缺失为 NULL
    token_address TEXT,               -- 归一化后:EVM 转小写,Solana(base58)保持原样
    token_symbol  TEXT,
    amount_usd    REAL,
    token_amount  TEXT,               -- 原始数量存字符串:memecoin 是 1e15 量级,REAL 丢精度
    price_usd     REAL,
    tx_hash       TEXT,               -- 兜底 event_id 的必要分量:等额拆单靠它才不会误去重
    event_ts      TEXT NOT NULL,      -- 事件发生时间(UTC ISO),时序比较的唯一基准
    ingested_at   TEXT NOT NULL,      -- 抓到的时间,用于诊断延迟 + 未发送补发窗口
    -- 【功能 A】徽章在落库时判定并冻结,永不重算
    --   (否则重投时 stats 已含本笔,🌱 会退化成 🟢)
    badge         TEXT,               -- FIRST / ADD / NULL(数据不足)
    badge_reason  TEXT,
    -- 【功能 B】推送时的共识时点值。仅写入、不读取、不参与任何判定;
    --   存在的唯一理由:共识数是时点值事后无法重算,
    --   而"共识数 vs 后续涨幅"是这个交易项目明确的回溯需求
    cs_buyers     INTEGER,
    cs_watchlist  INTEGER,
    -- 【买入榜】事件发生时的市值。与 cs_* 同理:这是**时点值,事后无法重算**,
    --   而"名单买入时 $1M → 现在 $15M"正是判断金狗的核心依据。
    --   price_usd 已经在上面存了,两者合起来才能算倍数。
    market_cap    REAL,
    -- 代币合约创建时间(unix 秒)→ 消息里的「币龄」。落库是为了让补发的消息也能显示它
    -- (formatter 是纯函数、不查库),而且它是恒定值,不像市值那样会过期
    token_created_at INTEGER,
    -- 转账的对手方钱包地址(转入 = fromAddress,转出 = toAddress)。
    -- 【转入告警】"同一个发货地址发给了几个人"是这条告警里唯一**可证**的证据:
    --   报文里没有 userId,「项目方在分发」与「本人从别处充值」在数据上完全一样,
    --   只有地址聚类能把两者分开一点。买卖事件没有这个字段,恒为 NULL。
    counterparty_address TEXT,
    sent          INTEGER NOT NULL DEFAULT 0,  -- 0=未发出;每 tick 末尾补发 10 分钟内未发出项
    raw_json      TEXT NOT NULL       -- 原始报文全量留存,便于日后离线回填
);
CREATE INDEX IF NOT EXISTS idx_events_user_token ON fomo_events(user_id, network_id, token_address);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON fomo_events(event_ts);
CREATE INDEX IF NOT EXISTS idx_events_sent ON fomo_events(sent);

-- ============ 游标(冷启动保护的唯一机制) ============
-- /add 落库时同一条 SQL 写入 cursor=now,历史事件因此永不进入推送。
-- 刻意不引入第二套抑制机制(watermark / suppressed 状态):多套锁的优先级极易写错。
CREATE TABLE IF NOT EXISTS fomo_cursors (
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,         -- swaps / transfers / thesis / balances
    cursor     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind)
);

-- ============ 【功能 A/B】用户 × 代币 聚合状态 ============
-- 功能 A(首次判定)与功能 B(共识分子)的唯一事实源,只由 BUY 事件与 /add 基线驱动。
-- 刻意不含 holding_state / holding_usd:"仍持有"从本 tick 内存 balances 直接数。
-- 持久化持仓状态会引入一整套对账 + EXITED 状态机,
-- 且必然产生"新买入被判成已退出"的错误。
CREATE TABLE IF NOT EXISTS user_token_stats (
    user_id       TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    token_address TEXT NOT NULL,
    buy_count     INTEGER NOT NULL DEFAULT 0,  -- 记录到的 BUY 事件条数(拆单会 +N;只作 0/>0 门槛)
                                               -- /add 基线里"当前持有但窗口内无买入记录"的老仓位写 1
    first_buy_at  TEXT,                        -- 最早买入时间;老仓位与兜底时间戳为 NULL
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, network_id, token_address)
);
CREATE INDEX IF NOT EXISTS idx_uts_token ON user_token_stats(network_id, token_address);

-- ============ 运行时状态(key-value) ============
-- 目前只存 last_tick_at:用来识别"关机了一晚上"这种长间断。
-- 不存的话进程重启后无从知道离开了多久,只能把积压的几百条逐条推出来。
CREATE TABLE IF NOT EXISTS runtime_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ============ 【跟单】信号台账 ============
-- 命中"N 个关注的人买了同一个币"时记一行。纸上跟单与真实下单共用这张表,
-- 靠 status 区分 —— 两套表会立刻产生"纸上赚了但实际没买"这类对不上的账。
--
-- ⚠️ 主键就是 (network_id, token_address):"单币只跟一次"这条规则由主键保证,
--    而不是靠代码里先 SELECT 再 INSERT —— 后者在两个 tick 撞上时会重复建仓。
CREATE TABLE IF NOT EXISTS copytrade_signals (
    network_id     TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    token_symbol   TEXT,
    triggered_at   TEXT NOT NULL,      -- UTC ISO
    trigger_buyers INTEGER NOT NULL,   -- 触发那一刻已有多少个名单成员买过
    entry_mcap     REAL,               -- 触发那一刻的市值 = 纸上建仓成本基准
    token_age_sec  INTEGER,            -- 触发时的币龄,用来事后复盘"跟太老的币是不是更差"
    amount_usd     REAL NOT NULL,      -- 跟单金额(纸上或真实)
    status         TEXT NOT NULL,      -- paper=纸上 / pending=等确认 / filled=已成交
                                       -- / rejected=你按了忽略 / failed=下单失败
    decided_at     TEXT,
    note           TEXT,
    PRIMARY KEY (network_id, token_address)
);
CREATE INDEX IF NOT EXISTS idx_copy_time ON copytrade_signals(triggered_at);

-- ============ 【转入告警】「N 个名单成员收到同一个币」的去重台账 ============
-- 命中阈值时记一行,主键保证同一个币**这辈子只告警一次** —— 分发是一次性事件,
-- 同一个币每 5 分钟提醒一遍等于让用户静音。
--
-- ⚠️ **刻意不复用 copytrade_signals**,尽管两张表长得很像。它的主键同样是
--    (network_id, token_address),一个币只占得下一行 —— 复用的话两类信号会互相
--    吃掉:一个币先被跟单信号占了行,转入告警就永远发不出;反过来转入告警占了行,
--    这个币的跟单信号也会被当成"已经跟过"而静默跳过。
-- ⚠️⚠️ 这张表与跟单执行器**没有任何关系**,永远不要让它参与下单判定:
--    「有人收到了免费筹码」与「有人自己掏钱买入」是相反的含义。它只驱动一条推送。
--
-- 【产品决定,不是漏洞】一个币这辈子只告警一次 —— 触发之后哪怕收到的人
--    从 3 个涨到 15 个、金额从 $1 万涨到 $50 万,**也不会有任何后续推送**。
--    用户 2026-08-26 明确拍板过"只报一次"(问的就是要不要做阶梯再报,答案是不要)。
--    ⚠️ 这一条很像缺陷,历次审查大概率会有人把它当 bug 报上来。
--       要改成阶梯再报(如 5 人/10 人各再报一次)必须**先问用户**,不要"顺手修好"。
--       真要做,主键得让位给 (network_id, token_address, 档位),而不是删去重。
CREATE TABLE IF NOT EXISTS transfer_in_signals (
    network_id     TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    token_symbol   TEXT,
    triggered_at   TEXT NOT NULL,      -- UTC ISO
    receivers      INTEGER NOT NULL,   -- 触发那一刻窗口内有几个名单成员收到过
    total_usd      REAL,               -- 这几个人合计收到多少美元(拿不到就 NULL,不写 0)
    PRIMARY KEY (network_id, token_address)
);
CREATE INDEX IF NOT EXISTS idx_transfer_in_time ON transfer_in_signals(triggered_at);

-- ============ 【币安 Alpha】已推送台账 ============
-- 记住哪些 Alpha 代币已经推过。
--
-- ⚠️⚠️ 存在的理由是**单靠水位线会永久静默漏推**:实测一次真实响应,665 条名单里
--    32 组代币共享同一个 listingTime,覆盖 100 个币(15%),最大一组 10 个。
--    「进名单的时刻」与「listingTime」是脱钩的两件事 —— 名单里存在尚未到上架时刻的币
--    (formatter 那个「即将上架」分支就是为它写的)就是证据。于是同一毫秒的几个币
--    完全可能分两轮进名单:水位线被第一批顶到 T 之后,第二批不满足 `> T`,
--    **一条日志都不留地永久丢失**。
--    改用 `>=` 只是把静默漏推换成必然的重复推送(listingTime 等于水位线的那个币每轮重推),
--    两害都不取:候选放宽到「水位线 - 宽限窗口」,重复的由这张表的主键挡住。
--
-- ⚠️ 主键与 copytrade_signals / transfer_in_signals 同一口径 (network_id, token_address),
--    但**另起一张表** —— 共用主键会让几类信号互相吃掉彼此的行(见那两张表的注释)。
-- ⚠️ listing_time_ms 必须存:清理任务靠它判「已经掉出宽限窗口、永远不会再成为候选」。
-- ⚠️⚠️ 这张表与跟单执行器**没有任何关系**,永远不要让它参与下单判定:
--    「币安上了个新币」与「名单里的人掏钱买了」是完全不同的含义。
CREATE TABLE IF NOT EXISTS binance_alpha_pushed (
    network_id      TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    token_symbol    TEXT,
    listing_time_ms INTEGER NOT NULL,   -- 币安给的上架时刻,候选窗口与清理都按它判
    pushed_at       TEXT NOT NULL,      -- UTC ISO
    PRIMARY KEY (network_id, token_address)
);
-- 专给清理任务用:prune 按 listing_time_ms 单列判过期,用不上以 network_id 打头的主键索引
CREATE INDEX IF NOT EXISTS idx_alpha_pushed_listing ON binance_alpha_pushed(listing_time_ms);

-- ============ 【pump.fun】被盯的人 ============
-- ⚠️⚠️ **刻意另起一张表,绝不给 watch_users 加 platform 列。**
--    watch_users.user_id 是 FOMO 的 UUID,被 fomo_events / user_token_stats /
--    fomo_cursors / copytrade 一路当聚合键。加一列 platform 之后,
--    list_active_users / fetchable_users / ready_user_ids / starred_user_ids /
--    transfer_watch_user_ids / bot._cmd_list / bot._cmd_status / web 查询
--    **每一个都要补 `AND platform='fomo'`,漏一个就是错的**。
--    最贵的是 fetchable_users —— 它直接驱动 poller 打 FOMO API:
--    一行 pump 数据漏进去,poller 会拿 Solana 钱包去问 fomo.family,
--    404 之后把这个人标成「账号已不存在」,而他本来好好的。
--    这与 transfer_in_signals 表上写过的是同一条教训(复用会让两类东西互相吃掉)。
-- ⚠️ 主键用 pump 的 userId(UUID),**不用 username** ——
--    /users/{name} 的响应里带 last_username_update_timestamp,
--    证明用户名是本人可改的,拿它当主键等于改个名就换了个人。
-- ⚠️ 两个 canonical 钱包都要存,理由见 pumpfun.py 顶部:
--    portfolio 用哪个查都返回同一份(2026-08-31 实测逐行相同),
--    但**逐笔成交必须两个都查** —— EVM 链上的成交记在 canonical_evm_wallet 名下,
--    只传 SVM 地址会得到空数组(实测:1000XCryptoD 的 BSC 买入只在 EVM 地址下)。
-- ⚠️ 这张表与跟单执行器**没有任何关系**,永远不要让它参与下单判定。
CREATE TABLE IF NOT EXISTS pump_watch_users (
    user_id     TEXT PRIMARY KEY,           -- pump.fun userId(UUID),权威主键
    username    TEXT,                       -- 仅展示,可被本人改
    svm_wallet  TEXT,                       -- canonical_svm_wallet
    evm_wallet  TEXT,                       -- canonical_evm_wallet
    added_at    TEXT NOT NULL,              -- UTC ISO
    active      INTEGER NOT NULL DEFAULT 1, -- 软删除:/pump del 置 0,再 add 置回 1
    removed_at  TEXT,
    -- 冷启动静默播种位。0 = 还没播过种,这一轮只记快照、**一条都不推**。
    -- ⚠️ 必须是**每人一位**,不能做成全局一位:名单跑了三天之后再加一个新人,
    --    全局位早就是 1 了,那个新人的几百个持仓会在第一轮全部当成"刚变动"推出去
    --    (实测 1000XCryptoD 有 1905 个持仓)。
    -- ⚠️ 也不能靠「他在快照表里有没有行」来猜:一个真的一个持仓都没有的人
    --    会永远被判成"没播过种",于是他第一次买入永远被静默吃掉。
    seeded      INTEGER NOT NULL DEFAULT 0,
    -- 观点(callout)的冷启动播种位。⚠️⚠️ **必须与 seeded 分开一列,绝不复用**:
    --    两路的开关是分别打开的 —— 一个人可能已经被买卖监控播过种(seeded=1)好几天,
    --    而观点开关今天才打开。复用一位的话他的历史观点会在第一轮整段推出来
    --    (实测 hexiecs 光最近 5 条就跨了 37.6 小时,往前还有)。
    callout_seeded INTEGER NOT NULL DEFAULT 0
);

-- ============ 【pump.fun】持仓快照 ============
-- 上一轮看到的持仓,用来 diff 出「哪些 mint 变了」。
-- ⚠️ 它只是**发现层**:只回答"要去问哪些 mint 的逐笔成交",
--    绝不拿它自己的差值当成交额推出去 —— 那是"两次快照之间的净变化",不是成交。
--    真相一律来自 swap-api 的逐笔记录。
-- ⚠️ amount_held 允许为 0:filter=ALL 会带回 isExited=true / amountHeld=0 的行,
--    清仓因此是**正面可观测**的,不用靠"这一行消失了"去猜。
--    所以判空一律 is None,绝不用真值判断(0 是有意义的真实值)。
-- ⚠️ 主键必须带 chain_id:pump.fun 是多链的(实测 hexiecs 一页里就有
--    Solana / Robinhood / BNB Chain 三条链),同一个地址串在两条链上是两个币。
CREATE TABLE IF NOT EXISTS pump_positions (
    user_id     TEXT NOT NULL,
    chain_id    TEXT NOT NULL,              -- pump 的原始 chainId,原样存(1399811149 等)
    coin_mint   TEXT NOT NULL,              -- 归一化后的 mint/CA
    amount_held REAL,                       -- 拿不到就 NULL,**不写 0**(0 是"已清仓")
    realized_pnl_usd REAL,
    updated_at  TEXT,                       -- pump 给的这一行的更新时刻
    snapshot_at TEXT NOT NULL,              -- 我们写下这一行的时刻(UTC ISO)
    PRIMARY KEY (user_id, chain_id, coin_mint)
);

-- ============ 【pump.fun】已推逐笔成交台账 ============
-- ⚠️⚠️ 存在的理由:portfolio 的一次变动会让我们把该 mint 的逐笔成交整段拉回来,
--    而下一轮这个 mint 若又变了,**同一批成交会再被拉回来一次**。
--    没有这张表就是每变动一次重推一遍。
-- ⚠️ 主键带 slot_index_id:一个 tx 里可以有同一个 mint 的多笔成交(拆单),
--    只按 tx 去重会把后面几笔静默吃掉。上游没给 slot_index_id 时存空串 ——
--    那时退化成"按 tx 去重",仍然不会重推。
-- ⚠️ traded_at 必须存:清理任务靠它判「已经掉出新鲜窗口、永远不会再成为候选」。
CREATE TABLE IF NOT EXISTS pump_pushed_trades (
    user_id       TEXT NOT NULL,
    coin_mint     TEXT NOT NULL,
    tx            TEXT NOT NULL,
    slot_index_id TEXT NOT NULL DEFAULT '',
    traded_at     TEXT NOT NULL,            -- 成交时刻 UTC ISO(pump 给的 timestamp)
    pushed_at     TEXT NOT NULL,            -- UTC ISO
    PRIMARY KEY (user_id, coin_mint, tx, slot_index_id)
);
-- 专给清理任务用:prune 按 traded_at 单列判过期,用不上以 user_id 打头的主键索引
CREATE INDEX IF NOT EXISTS idx_pump_pushed_traded ON pump_pushed_trades(traded_at);

-- ============ 【pump.fun】已推观点(callout)台账 ============
-- ⚠️⚠️ 存在的理由与成交台账逐字相同:/callout/list 每轮都把这个人**最近一页**
--    观点整段返回,没有这张表就是每轮重推一遍同样的几条。
-- ⚠️ 主键**只用 callout_id**:它是 pump 自己给的 UUID,全局唯一、天然稳定
--    (成交那张表要凑 user+mint+tx+slot 四列才拼得出主键,是因为 tx 会拆单;
--     这里没有这个问题,再往主键里加列只会让"同一条观点"有机会被写成两行)。
-- ⚠️ created_at 必须存:清理任务靠它判「已经掉出新鲜窗口、永远不会再成为候选」。
CREATE TABLE IF NOT EXISTS pump_pushed_callouts (
    callout_id  TEXT PRIMARY KEY,           -- pump 的 calloutId(UUID)
    user_id     TEXT NOT NULL,              -- 只为排查用,不参与去重
    coin_mint   TEXT,                       -- 同上;上游真缺时允许 NULL
    created_at  TEXT NOT NULL,              -- 发表时刻 UTC ISO(由 epoch 毫秒归一化而来)
    pushed_at   TEXT NOT NULL               -- UTC ISO
);
-- 专给清理任务用:prune 按 created_at 单列判过期,主键是 callout_id 用不上
CREATE INDEX IF NOT EXISTS idx_pump_callout_created ON pump_pushed_callouts(created_at);

-- ============ 【买入榜】代币行情快照 ============
-- 每 tick 从 balances 拿到的最新价与市值,按币覆盖写一行。
-- 存在的理由:/hot 要算"买入时市值 → 现在市值"的倍数,
-- 而"现在"这个值只有在轮询到持仓时才拿得到 —— 不落地的话命令执行时得现拉 N 个币。
-- 只覆盖**名单里还有人持有**的币;清仓后不再更新,updated_at 就是它最后已知的时间。
CREATE TABLE IF NOT EXISTS token_snapshot (
    network_id    TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol        TEXT,
    price_usd     REAL,
    market_cap    REAL,
    -- 我们**观测到的**最高市值(每轮取 max,只增不减)。
    -- ⚠️ 不是真 ATH:只在名单里有人持有、且轮询到的时刻才采样,币在我们看它之前
    --    冲过多高无从知道。所以文案写「峰值」而不是「ATH」。
    -- 存在的理由:没有它,"$41.9K → $2.9M" 会被读成"起点→最高",
    --    于是一个在 $4.19M 进场的买家看着像不可能 —— 而真相是这个币冲到 4.19M 后回落了,
    --    追高的那批人正套着。这恰恰是最该看见的信息。
    max_market_cap REAL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (network_id, token_address)
);

-- ============ 【价格历史】名单持仓代币的采样序列 ============
-- 每 fomo_price_history_sample_ticks 轮(默认 20×15s=5 分钟)把当轮
-- self._token_meta 里每个币的现价/市值采一行,供仪表盘画迷你走势图(sparkline)。
-- 这是全项目唯一的价格时间序列 —— token_snapshot 每 tick 覆盖写,历史看不见。
-- ⚠️ 只采名单里**还有人持有**的币,这本来就是 _token_meta 的范围,零额外 API 调用;
--    没人持有了就不再出现在这里,是正确行为(这个币的价格已经不再是名单关心的事)。
-- ⚠️ price_usd / market_cap 都必须允许 NULL,且拿不到时要存 NULL 而不是 0 ——
--    见 formatter.py 头部的规矩:0 是真实值(真的归零了),不能被"没采到"占用。
CREATE TABLE IF NOT EXISTS token_price_history (
    network_id    TEXT NOT NULL,
    token_address TEXT NOT NULL,
    sampled_at    TEXT NOT NULL,   -- 采样时刻(UTC ISO)。同一 tick 落的所有行共用同一个值
    price_usd     REAL,
    market_cap    REAL,
    -- 主键顺序与唯一的读路径完全对齐:WHERE network_id=? AND token_address=?
    -- AND sampled_at>=? ORDER BY sampled_at —— PK 自带的 autoindex 本身就是
    -- 这条路径需要的全部索引,不必再重复建一张。
    -- 顺带把"同一个币同一时刻重复采样"变成主键冲突,INSERT OR IGNORE 天然幂等。
    PRIMARY KEY (network_id, token_address, sampled_at)
);
-- 专给清理任务用:prune 按 sampled_at 单列判过期,用不上以 network_id 打头的
-- 上面那条 PK 索引(sampled_at 是第三列,不能做范围扫描)——没有它,清理会
-- 退化成全表扫描,而这张表稳态下是百万行量级。
CREATE INDEX IF NOT EXISTS idx_price_history_sampled_at ON token_price_history(sampled_at);

-- 名单成员的盈亏快照。来自 /v2/leaderboard/following。
-- ⚠️ 实测:period="following" **一个请求**就返回 79 行 × 全部四个盈亏字段
--    (totalPnL / pnl24h / pnl7d / pnl30d),而 period="7d" 只返回 pnl7d
--    (那是全站前 100 榜,不是我们的名单)。所以只能用 following。
--    client.get_leaderboard 的文档字符串漏写了后三个字段,顺手补上。
-- ⚠️ 按人覆盖写,只保留最新 —— 每轮追加一天就是几百行垃圾,
--    而这个值是慢变量,历史序列本期用不上。
CREATE TABLE IF NOT EXISTS user_pnl_snapshot (
    user_id        TEXT PRIMARY KEY,
    total_pnl      REAL,     -- 生涯总盈亏
    pnl_24h        REAL,
    pnl_7d         REAL,
    pnl_30d        REAL,
    total_holdings REAL,     -- 当前总持仓价值
    num_trades     INTEGER,
    updated_at     TEXT NOT NULL
);
"""


# ============================================================
# 连接与事务
# ============================================================
@contextmanager
def get_conn():
    """autocommit + WAL(独立进程 + bot/poller 双线程,必须开)"""
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")     # 读不阻塞写
    conn.execute("PRAGMA busy_timeout = 5000")    # Windows 上锁竞争必备
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """
    显式事务。

    ⚠️ get_conn() 是 autocommit(isolation_level=None),不写 BEGIN 的话每条语句立即提交,
       "事件插入 + stats 更新"的原子性会静默丢失 ——
       崩在两者之间会让该事件永远被 INSERT OR IGNORE 跳过、stats 永远不更新,
       后续必然错标 🌱。
    ⚠️ 用 BEGIN IMMEDIATE 而非 BEGIN:WAL 下立即取写锁,避免读锁升写锁时 SQLITE_BUSY。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# 链标识重命名。早期版本对未收录的链直接存原始数字 ID,后来补上了名称。
# ⚠️ 不迁移的话同一条链会裂成两个聚合键("4663" 和 "robinhood" 各算一份):
#    已经建过仓的币会被重新判成「首次建仓」,而徽章落库即冻结、错了就是永久的。
_NETWORK_RENAMES = {"4663": "robinhood", "143": "monad", "1337": "hyperliquid"}


def _migrate(conn: sqlite3.Connection) -> None:
    """
    幂等迁移。CREATE TABLE IF NOT EXISTS 不会给已存在的表补列,
    所以新增列必须在这里 ALTER,否则老库升级后直接报 no such column。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(fomo_events)").fetchall()}
    for col, ddl in (("user_handle", "TEXT"), ("market_cap", "REAL"),
                     ("token_created_at", "INTEGER"),
                     ("counterparty_address", "TEXT")):
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE fomo_events ADD COLUMN {col} {ddl}")  # noqa: S608
            logger.info("迁移:fomo_events 补列 {}", col)

    wcols = {r["name"] for r in conn.execute("PRAGMA table_info(watch_users)").fetchall()}
    if wcols and "starred" not in wcols:
        conn.execute("ALTER TABLE watch_users ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        logger.info("迁移:watch_users 补列 starred(特别关注)")

    if wcols and "missing_since" not in wcols:
        conn.execute("ALTER TABLE watch_users ADD COLUMN missing_since TEXT")
        logger.info("迁移:watch_users 补列 missing_since(上游 404,账号已不存在)")

    if wcols and "watch_transfer_in" not in wcols:
        # ⚠️ 默认 0 —— 老库升级后行为与升级前**逐字节相同**:没人被标记,
        #    转账照旧只入库不推送。这个功能只能由用户一个个 /tin 打开。
        conn.execute(
            "ALTER TABLE watch_users ADD COLUMN watch_transfer_in INTEGER NOT NULL DEFAULT 0")
        logger.info("迁移:watch_users 补列 watch_transfer_in(转入逐条推送)")

    if wcols and "transfer_in_min_usd" not in wcols:
        # ⚠️ **可空、无 DEFAULT** —— 老库里已经开着 /tin 的那个人补列之后拿到 NULL,
        #    NULL 落回全局 fomo_transfer_watch_min_usd(默认 $100),也就是他升级前后的
        #    推送**逐字节相同**。写成 `NOT NULL DEFAULT 0` 就是把他静默改成"全推"
        #    (实测 1.1 条/天 → 8.1 条/天);写成 `DEFAULT 100` 则是把当时的全局值
        #    **冻结**进库,以后改 .env 对他不再生效 —— 两种都是没人下过的指令。
        conn.execute("ALTER TABLE watch_users ADD COLUMN transfer_in_min_usd REAL")
        logger.info("迁移:watch_users 补列 transfer_in_min_usd(每人各自的转入门槛,NULL=跟全局)")

    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(pump_watch_users)").fetchall()}
    if pcols and "callout_seeded" not in pcols:
        # ⚠️ 默认 0 —— 老库升级后行为与升级前**逐字节相同**:名单里的人一律当成
        #    "观点还没播过种",第一轮只记水位线、一条都不推。
        #    默认 1 的话,升级那一刻每个人的历史观点会一次性倒出来。
        conn.execute("ALTER TABLE pump_watch_users "
                     "ADD COLUMN callout_seeded INTEGER NOT NULL DEFAULT 0")
        logger.info("迁移:pump_watch_users 补列 callout_seeded(观点冷启动播种位)")

    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(token_snapshot)").fetchall()}
    if tcols and "max_market_cap" not in tcols:
        conn.execute("ALTER TABLE token_snapshot ADD COLUMN max_market_cap REAL")
        # ⚠️ 用历史买入记录里的最高市值**回填**,而不是从今天开始重新攒:
        #    fomo_events.market_cap 是每笔买入的时点市值,它天然采样了这个币涨的过程。
        #    不回填的话,已经冲高回落的币(恰恰是最该看到峰值的那些)要等下一次冲高
        #    才有数 —— 而它多半不会再冲了。
        conn.execute("""
            UPDATE token_snapshot SET max_market_cap = MAX(
                COALESCE(market_cap, 0),
                COALESCE((SELECT MAX(e.market_cap) FROM fomo_events e
                           WHERE e.network_id = token_snapshot.network_id
                             AND e.token_address = token_snapshot.token_address), 0))
            WHERE max_market_cap IS NULL
        """)
        logger.info("迁移:token_snapshot 补列 max_market_cap(峰值,已用历史买入记录回填)")

    for old, new in _NETWORK_RENAMES.items():
        for table in ("fomo_events", "user_token_stats"):
            cur = conn.execute(
                f"UPDATE {table} SET network_id = ? WHERE network_id = ?", (new, old)  # noqa: S608
            )
            if cur.rowcount:
                logger.info("迁移:{} 里 {} 行的链标识 {} → {}", table, cur.rowcount, old, new)


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """建表 + 迁移,幂等"""
    if conn is not None:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        return
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as c:
        c.executescript(_SCHEMA)
        _migrate(c)
    logger.info("数据库已就绪: {}", DB_PATH)


# ============================================================
# 监控名单
# ============================================================
def normalize_handle(raw: str) -> str:
    """
    handle **查找键**归一化:去空白、去前导 @、转小写。

    ⚠️ 只用于比较和查找,**不要拿它当存储值** ——
       转小写会把 @GakkiYuiTifa 显示成 @gakkiyuitifa,而 handle 是要给人看、
       给人拿去搜的标识,大小写属于它本身的一部分。
       存库存原样、查询时两边都过这个函数,既保留展示又不会重复添加。
    ⚠️ 去重的最终保证是 user_id 主键,不是 handle。
    """
    return (raw or "").strip().lstrip("@").strip().lower()


def clean_handle(raw: str) -> str:
    """handle 存储值:只去空白与前导 @,**保留原始大小写**"""
    return (raw or "").strip().lstrip("@").strip()


def get_watch_user(conn, user_id: str):
    return conn.execute(
        "SELECT * FROM watch_users WHERE user_id = ?", (user_id,)
    ).fetchone()


def find_user_by_handle(conn, handle: str):
    """按 handle 查。⚠️ 必须忽略大小写:库里存的是原始大小写,用户输入未必一致"""
    return conn.execute(
        "SELECT * FROM watch_users WHERE lower(handle) = ?", (normalize_handle(handle),)
    ).fetchone()


def list_active_users(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watch_users WHERE active = 1 ORDER BY added_at"
    ).fetchall()


def fetchable_users(conn) -> list[sqlite3.Row]:
    """
    本轮真正要去拉数据的人 —— 排除掉上游已经 404 的账号。

    ⚠️ 与 list_active_users 分开是有意的:/list、共识计数用的仍是完整名单
       (那些人历史上的买入是**真实发生过的事**,不能因为账号后来没了就抹掉),
       只有"去拉他今天的数据"这件事没有意义。
    """
    return conn.execute(
        "SELECT * FROM watch_users WHERE active = 1 AND missing_since IS NULL "
        "ORDER BY added_at"
    ).fetchall()


def mark_user_missing(conn, user_id: str) -> bool:
    """标记为「上游说不存在」。返回 True 表示这次是**新**标上的(用来只告警一次)"""
    with tx(conn):
        cur = conn.execute(
            "UPDATE watch_users SET missing_since = ? "
            "WHERE user_id = ? AND missing_since IS NULL",
            (now_iso(), user_id),
        )
    return cur.rowcount == 1


def clear_user_missing(conn, user_id: str) -> bool:
    """账号又能拉到了 —— 撤掉标记。返回 True 表示确实撤掉了一个"""
    with tx(conn):
        cur = conn.execute(
            "UPDATE watch_users SET missing_since = NULL "
            "WHERE user_id = ? AND missing_since IS NOT NULL",
            (user_id,),
        )
    return cur.rowcount == 1


def missing_users(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watch_users WHERE active = 1 AND missing_since IS NOT NULL "
        "ORDER BY missing_since"
    ).fetchall()


def ready_user_ids(conn) -> list[str]:
    """既 active 又已建好基线的用户 —— 共识计数的分母口径,分子必须用同一个谓词"""
    rows = conn.execute(
        "SELECT user_id FROM watch_users WHERE active = 1 AND stats_ready = 1"
    ).fetchall()
    return [r["user_id"] for r in rows]


def starred_user_ids(conn) -> set[str]:
    """特别关注的人。纯展示用途,拿不到就当没有 —— 绝不能因此挡住推送"""
    rows = conn.execute(
        "SELECT user_id FROM watch_users WHERE active = 1 AND starred = 1"
    ).fetchall()
    return {r["user_id"] for r in rows}


def set_starred(conn, handle_or_id: str, on: bool) -> tuple[bool, str]:
    """
    设/取消特别关注。返回 (是否改动了, 回执文案)。

    ⚠️ 与 /del 一样按 handle 或 user_id 找人,且**只认 active 的** ——
       给一个已经移出名单的人加星标没有任何意义,只会让 /list 的星标数对不上。
    """
    key = (handle_or_id or "").strip()
    if not key:
        return False, "❓ 用法:/star <handle>"
    row = conn.execute(
        "SELECT user_id, handle, display_name, starred FROM watch_users "
        "WHERE active = 1 AND (lower(handle) = ? OR user_id = ?)",
        (normalize_handle(key), key),
    ).fetchone()
    if row is None:
        return False, f"❓ 名单里没有 {clean_handle(key)}(先 /add 加进来)"

    who = row["display_name"] or row["handle"]
    if bool(row["starred"]) == on:
        return False, f"ℹ️ {who} 已经{'在' if on else '不在'}特别关注里了"
    with tx(conn):
        conn.execute("UPDATE watch_users SET starred = ? WHERE user_id = ?",
                     (1 if on else 0, row["user_id"]))
    return True, (f"⭐ 已把 {who} 加入特别关注" if on else f"☆ 已把 {who} 移出特别关注")


def transfer_watch_user_ids(conn) -> set[str]:
    """
    开了「转入逐条推送」的人。

    ⚠️ 与 starred_user_ids 长得像,但**后果完全不同**:星标查不出来只是不好看,
       这个查不出来就是"该推的没推 / 不该推的推了"。所以调用方绝不该 except 之后
       当成空集继续 —— 空集恰好是"谁都不推",那是安全的一侧,但要留痕。
    """
    rows = conn.execute(
        "SELECT user_id FROM watch_users WHERE active = 1 AND watch_transfer_in = 1"
    ).fetchall()
    return {r["user_id"] for r in rows}


# set_transfer_watch 的 min_usd 哨兵:这次**不动门槛**。
# ⚠️ 不能拿 None 当"没传":None 是这一列的**合法取值**(= 清回全局默认),
#    两者撞在一起之后"别动它"和"把它清掉"就再也分不开了。
KEEP_MIN_USD = object()


def resolve_transfer_min_usd(row_min_usd: float | None, default_min_usd: float) -> float:
    """
    这个人的这一笔按多少钱判。**门槛语义的唯一出处**,poller(判定)与 bot(回执/清单)
    共用同一个函数 —— 两处各写一遍迟早漂移,而漂移出来的是"该推的没推"。

    ⚠️⚠️ 必须 `is None`,**绝不能写 `row_min_usd or default_min_usd`**:
       0 是用户显式设过的合法门槛(= 这个人的转入全推),真值判断会把它当成"没设过"
       而悄悄换回全局默认 —— 用户在清单里看到 $0.00,实际却按 $100 在筛,一声不吭。
    """
    return default_min_usd if row_min_usd is None else float(row_min_usd)


def fmt_transfer_min_usd(row_min_usd: float | None,
                         default_min_usd: float | None = None) -> str:
    """
    门槛的展示文案。

    ⚠️ 「没设过、跟着全局走」与「显式设成了同一个数」必须**看得出区别**:
       前者会随 .env 里的 FOMO_TRANSFER_WATCH_MIN_USD 一起变,后者不会。
       两个都印成 `$100.00` 的话,用户没法判断改 .env 会不会影响到这个人。
    ⚠️ default_min_usd 由调用方注入(与 max_on 同一条理由,见下)。没注入时只写
       「默认」两个字 —— 它只影响回执好不好看,**判定永远不经过这里**。
    """
    if row_min_usd is None:
        return "默认" if default_min_usd is None else f"${default_min_usd:,.2f}(默认)"
    return f"${float(row_min_usd):,.2f}"


def _min_usd_differs(a: float | None, b: float | None) -> bool:
    """两个门槛是不是不一样。⚠️ NULL 与 0.0 是**不同的两件事**,不能靠真值判断合并"""
    if a is None or b is None:
        return (a is None) != (b is None)
    return float(a) != float(b)


def set_transfer_watch(conn, handle_or_id: str, on: bool | None = None,
                       *, min_usd=KEEP_MIN_USD, default_min_usd: float | None = None,
                       max_on: int | None = None) -> tuple[bool, str]:
    """
    开/关一个人的「转入逐条推送」,并可同时设定**他自己的**金额门槛。
    返回 (是否改动了, 回执文案)。

    on = None 时是**开关**(已开就关),这是 /tin 不带金额时的用法;传显式 True/False
    则是幂等设置,此时"本来就是这样"会如实回执 —— 开关命令唯一的反馈就是回执,
    含糊的回执会让用户分不清自己刚才是开了还是关了。

    ⚠️⚠️ **带金额时调用方传 on=True(只开不切),不带金额才传 on=None(切换)。**
       这个歧义只有这一种解法:
         - 带金额时也切换 → 「已开在 $30000,再发 /tin alice 200」既可以读成
           "关掉他"也可以读成"改成 200",用户无从预期;
         - 不带金额时也当成"设置" → 就再也没有任何写法能**关掉**了。
    ⚠️ min_usd 三态:KEEP_MIN_USD = 不动门槛;float = 设成这个数(**0 合法** = 全推);
       None = 清回 NULL(重新跟着全局默认走)。
    ⚠️ 与 set_starred 同一套找人规矩(handle 或 user_id,**只认 active 的**):
       给已经移出名单的人开这个开关没有意义 —— poller 根本不会去拉他的转账。
    ⚠️ max_on 是"最多同时开几个人"的上限,**由调用方注入**:上限的依据是 poller
       的单轮请求预算(见 poller.TRANSFER_WATCH_MAX 那段算式),而 poller 依赖
       本模块 —— 在这里 import 它就是循环依赖。传 None 表示不限。
    ⚠️ 门槛**不做上下限校验**(负数在调用方就被拦掉了):这里只负责存,
       "什么样的数算合法"是命令语法的事,两处都判会各判各的。
    """
    key = (handle_or_id or "").strip()
    if not key:
        return False, "❓ 用法:/tin <handle> [金额]"
    row = conn.execute(
        "SELECT user_id, handle, display_name, watch_transfer_in, transfer_in_min_usd "
        "FROM watch_users WHERE active = 1 AND (lower(handle) = ? OR user_id = ?)",
        (normalize_handle(key), key),
    ).fetchone()
    if row is None:
        return False, f"❓ 名单里没有 {clean_handle(key)}(先 /add 加进来)"

    who = row["display_name"] or row["handle"]
    cur_on = bool(row["watch_transfer_in"])
    cur_min = row["transfer_in_min_usd"]
    target_on = (not cur_on) if on is None else bool(on)
    # ⚠️ 没传 min_usd 时目标就是**现在这个值**,而不是 None —— 写成 None 等于
    #    每次开关都顺手把用户设过的门槛清掉,而他并没有下过这个指令。
    target_min = cur_min if min_usd is KEEP_MIN_USD else min_usd

    if target_on == cur_on and not _min_usd_differs(cur_min, target_min):
        if not target_on:
            return False, f"ℹ️ {who} 的转入推送本来就是关着的"
        return False, (f"ℹ️ {who} 的转入推送本来就是开着的 · 门槛 "
                       f"{fmt_transfer_min_usd(cur_min, default_min_usd)}")
    if target_on and not cur_on and max_on is not None:
        # ⚠️ 上限在**开之前**拦,而不是让 poller 事后降级:poller 那边超限只会
        #    退回轮转 + 刷日志,用户在 TG 里什么都看不到,还以为开成功了。
        # ⚠️ 拦下来时门槛也一并不写:半开半设的状态比什么都没发生更难解释。
        n = len(transfer_watch_user_ids(conn))
        if n >= max_on:
            return False, (f"❗ 转入推送最多同时开 {max_on} 人(现在已开 {n} 人)—— "
                           f"每多一个人,每轮就多一个请求。先 /tin 关掉一个再来")
    with tx(conn):
        conn.execute(
            "UPDATE watch_users SET watch_transfer_in = ?, transfer_in_min_usd = ? "
            "WHERE user_id = ?",
            (1 if target_on else 0, target_min, row["user_id"]),
        )
    shown = fmt_transfer_min_usd(target_min, default_min_usd)
    if not target_on:
        # ⚠️ 关掉时**门槛留着**(与 remove_watch_user 清零 watch_transfer_in 的决定
        #    并不矛盾,理由见那里)。留着这件事必须写进回执,否则就是暗的。
        if target_min is None:
            return True, f"🔕 已关闭 {who} 的转入逐条推送"
        return True, f"🔕 已关闭 {who} 的转入逐条推送 · 门槛 {shown} 留着,下次打开还是它"
    if cur_on:
        return True, (f"🎚 {who} 的转入门槛已改为 {shown}"
                      f"(之前 {fmt_transfer_min_usd(cur_min, default_min_usd)})")
    return True, f"📥 已开启 {who} 的转入逐条推送 · 门槛 {shown}"


def add_watch_user(conn, user_id: str, handle: str, display_name: str | None) -> tuple[bool, str]:
    """
    加入监控名单。返回 (是否需要建基线, 给用户的回执文案)。

    ⚠️ 幂等的判据是「**是否已 active**」,不是「基线是否就绪」。
       两者混在一起会出事:68 人的基线要 68 轮(约 23 分钟)才全就绪,
       这期间再跑一次 /following(第一次超时、或想确认结果),
       那 67 个 stats_ready=0 的人就全落到 ON CONFLICT 分支、
       四个游标被推到 now —— 上一轮 tick 之后发生的买卖**永久丢弃**,
       因为游标只在这里前进,而 _drop_before_cursor 的判据是 event_ts > cursor。
       所以只要还 active 就绝不动游标,只在真·回归(active 0→1)时才重置。
    ⚠️ /del 后回归必须重建基线:空窗期的买入本地无记录,
       不重建会让空窗期建的仓位在下次加仓时被误标 🌱。
    """
    # 存原始大小写(展示要用),去重靠 user_id 主键 —— 见 normalize_handle 的说明
    h = clean_handle(handle)
    existing = get_watch_user(conn, user_id)
    if existing and existing["active"]:
        # 已在监控中(不论基线建没建好):不碰游标、不重置 stats_ready,只刷新名字。
        # 名字只是展示,更新它不影响任何判定。
        if (existing["handle"], existing["display_name"]) != (h, display_name):
            with tx(conn):
                conn.execute(
                    "UPDATE watch_users SET handle = ?, display_name = ? WHERE user_id = ?",
                    (h, display_name, user_id),
                )
            logger.info("刷新名字 | {} → {} (@{})", existing["handle"], display_name, h)
        if existing["stats_ready"]:
            return False, f"ℹ️ {display_name or h} 已在监控中"
        # 基线还在排队:返回 True 让调用方按"待建基线"计数,但游标一动没动
        return True, f"ℹ️ {display_name or h} 已在监控中,历史基线仍在排队建立"

    with tx(conn):
        conn.execute(
            """
            INSERT INTO watch_users (user_id, handle, display_name, added_at, active, stats_ready)
            VALUES (?, ?, ?, ?, 1, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                active       = 1,
                removed_at   = NULL,
                handle       = excluded.handle,
                display_name = excluded.display_name,
                stats_ready  = 0
            """,
            (user_id, h, display_name, now_iso()),
        )
        # 冷启动保护的唯一机制:游标即刻设为 now,历史事件永不进入推送
        _set_all_cursors_now(conn, user_id)
    return True, f"✅ 已加入 {display_name or h},正在建立历史基线…"


def remove_watch_user(conn, handle_or_id: str) -> tuple[bool, str]:
    """
    软删除。保留历史数据,只把 active 置 0。

    共识 SQL 带 WHERE active=1,所以移除后相关代币的共识数会下降 —— 这是正确行为,
    语义是"我**现在**关注的这批人里有几个买过"。

    ⚠️ **watch_transfer_in 必须一并清零**,不能像 starred 那样留着等回归时自动恢复。
       留着有两个后果,而且都是静默的:
         1. 上限被绕过。set_transfer_watch 数的是 active=1 且开着的人,而 /add 回归走
            add_watch_user 的 ON CONFLICT 分支、根本不碰这一位 —— 开满 12 → /del 掉一个
            (名额空出来)→ 再 /tin 开一个 → /add 把那个人加回来,于是 active 且开着的
            变成 13。poller 每轮都会撞上超限分支、整体退回轮转,这个功能的时效承诺
            当场作废,而用户在 TG 里看到的还是「13/12 人」。
         2. 就算不谈上限:/add 回来的那一刻,采集频率和推送量会在用户**没下过任何指令**
            的情况下自己变回去。starred 只是给消息加个 ⭐,恢复了也无所谓;这一位是
            花请求、发消息的开关,静默恢复不叫贴心叫失控。
       代价是回归后要重新 /tin 一次 —— 与「默认全员关闭、必须一个个开」是同一条规矩。
       清零之后 `watch_transfer_in = 1 ⟹ active = 1` 成为库上恒真的不变式,
       set_transfer_watch 那道人数上限才真的兜得住。
    ⚠️⚠️ **但 transfer_in_min_usd 反过来:留着,不清。** 上面两条理由对它一条都不成立 ——
       开关关着时这一列**完全不参与判定**,既不花请求也不发消息,留着是惰性的。
       清掉它才是那种"用户没下指令、行为自己变了":他把某人设成 $30000 之后 /del、
       再 /add 回来 /tin 打开,推送量会从 0.5 条/天静默涨回 $100 的 1.1 条/天。
       两条决定其实是同一条原则——**往推送变多的方向永远不许静默发生**——
       作用在语义不同的两列上,所以结论相反。
       而且它并不是暗的:回执与 /tin 清单永远写明当前门槛是多少。
    """
    row = get_watch_user(conn, handle_or_id) or find_user_by_handle(conn, handle_or_id)
    if not row or not row["active"]:
        return False, f"⚠️ 未在监控名单中: {handle_or_id}"
    with tx(conn):
        conn.execute(
            "UPDATE watch_users SET active = 0, removed_at = ?, watch_transfer_in = 0 "
            "WHERE user_id = ?",
            (now_iso(), row["user_id"]),
        )
    name = row["display_name"] or row["handle"]
    return True, f"✅ 已移除 {name}(相关代币共识数已下调)"


def reset_all_baselines(conn) -> int:
    """
    把所有 active 用户的 stats_ready 置 0,让 seeding 重跑一遍。返回受影响人数。

    用途:回填逻辑本身改好之后(比如分页参数修对了、回填条数上调了),
    已经建好的旧基线仍是按旧规则建的,不重建就一直用着不准的判据。

    ⚠️ **绝不碰游标**。游标只在真·新增用户时设为 now;这里动它等于把
       上一轮之后发生的事件全部丢弃(_drop_before_cursor 的判据是 event_ts > cursor)。
    ⚠️ 重建期间这些人不打徽章、不计入共识分子分母(stats_ready=0 的既定语义),
       推送照常。每 tick 只建一个人,所以 N 人要 N 轮。
    """
    with tx(conn):
        cur = conn.execute("UPDATE watch_users SET stats_ready = 0 WHERE active = 1")
    n = cur.rowcount or 0
    logger.info("已重置 {} 人的历史基线,将逐轮重建", n)
    return n


def pick_one_pending_user(conn):
    """
    取一个待建基线的用户。每 tick 只处理一个 ——
    批量 /add 10 人 = 10 个 tick 内全部就绪,期间照常推送、只是不打徽章。
    """
    return conn.execute(
        "SELECT * FROM watch_users WHERE active = 1 AND stats_ready = 0 "
        "ORDER BY added_at LIMIT 1"
    ).fetchone()


def is_stats_ready(conn, user_id: str) -> bool:
    row = conn.execute(
        "SELECT stats_ready FROM watch_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return bool(row and row["stats_ready"])


def mark_stats_ready(conn, user_id: str) -> None:
    conn.execute("UPDATE watch_users SET stats_ready = 1 WHERE user_id = ?", (user_id,))


def stats_row_count(conn, user_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM user_token_stats WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]


# ============================================================
# 游标
# ============================================================
def get_cursor(conn, user_id: str, kind: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM fomo_cursors WHERE user_id = ? AND kind = ?", (user_id, kind)
    ).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn, user_id: str, kind: str, cursor: str | None) -> None:
    conn.execute(
        """
        INSERT INTO fomo_cursors (user_id, kind, cursor, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, kind) DO UPDATE SET cursor = excluded.cursor,
                                                 updated_at = excluded.updated_at
        """,
        (user_id, kind, cursor, now_iso()),
    )


def _set_all_cursors_now(conn, user_id: str) -> None:
    """把四类游标一次性设为当前时刻 —— 这就是"不推历史"的全部实现"""
    ts = now_iso()
    for kind in CURSOR_KINDS:
        conn.execute(
            """
            INSERT INTO fomo_cursors (user_id, kind, cursor, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, kind) DO UPDATE SET cursor = excluded.cursor,
                                                     updated_at = excluded.updated_at
            """,
            (user_id, kind, ts, ts),
        )


def set_all_cursors_now(conn, user_id: str) -> None:
    """外部调用版本(自带事务)"""
    with tx(conn):
        _set_all_cursors_now(conn, user_id)


# ============================================================
# 事件落库
# ============================================================
def insert_event(conn, ev: FomoEvent) -> bool:
    """
    INSERT OR IGNORE 落库。返回 True 表示确实插入了新行。

    ⚠️ 调用方必须用返回值决定要不要 upsert_stats ——
       重复轮询拉到同一笔时若无脑累加,buy_count 会一路虚增。
    """
    r = ev.to_row()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO fomo_events
            (event_id, event_type, user_id, handle, user_handle, network_id, token_address,
             token_symbol, amount_usd, token_amount, price_usd, market_cap, token_created_at,
             tx_hash, event_ts, ingested_at, badge, badge_reason, counterparty_address, raw_json)
        VALUES (:event_id, :event_type, :user_id, :handle, :user_handle, :network_id,
                :token_address, :token_symbol, :amount_usd, :token_amount, :price_usd,
                :market_cap, :token_created_at, :tx_hash, :event_ts, :ingested_at,
                :badge, :badge_reason, :counterparty_address, :raw_json)
        """,
        r,
    )
    return cur.rowcount == 1


def mark_sent(conn, event_id: str, cs_buyers: int | None, cs_watchlist: int | None) -> None:
    """
    标记已发送 + 记录共识时点值。

    ⚠️ 只能在 Telegram 确认收到之后调用。"发之前就标已发"会在崩溃时永久丢消息
       (下一 tick 该事件已在库里,INSERT OR IGNORE 直接跳过,再也不会被重新发现)。
    """
    conn.execute(
        "UPDATE fomo_events SET sent = 1, cs_buyers = ?, cs_watchlist = ? WHERE event_id = ?",
        (cs_buyers, cs_watchlist, event_id),
    )


def load_unsent_recent(conn, minutes: int = 10) -> list[sqlite3.Row]:
    """
    补发窗口:落库成功但推送失败/崩溃的事件。

    只捞最近 N 分钟的 —— 更早的补发出去已经没有交易价值,反而制造困惑。

    ⚠️ 下界必须用 models.iso_minutes_ago() 算,**绝不能用 SQL 的 datetime('now', ?)**。
       ingested_at 是 now_iso() 产出的 'T' 分隔带偏移量的 ISO,
       而 SQL 的 datetime() 产出空格分隔无偏移量的格式,两者字符串比较恒为真,
       会把「10 分钟窗口」变成「同一 UTC 日全部」——
       一条永远发不出去的消息就会被每 20 秒重试一整天,
       积到几百条时单个 tick 要跑几十分钟,正常推送全部被饿死。
    """
    return conn.execute(
        """
        SELECT * FROM fomo_events
        WHERE sent = 0 AND ingested_at >= ?
        ORDER BY event_ts
        """,
        (iso_minutes_ago(minutes),),
    ).fetchall()


# ============================================================
# 【功能 A】首次买入判定
# ============================================================
def get_stats(conn, user_id: str, network_id: str, token_address: str):
    return conn.execute(
        "SELECT * FROM user_token_stats WHERE user_id = ? AND network_id = ? AND token_address = ?",
        (user_id, network_id, token_address),
    ).fetchone()


def judge_badge(conn, ev: FomoEvent) -> tuple[str | None, str]:
    """
    返回 (badge, reason)。badge=None 表示数据不足 —— **宁可漏标,不可错标**。

    ⚠️ 必须在把本事件 upsert 进 user_token_stats **之前** 调用,
       否则 buy_count 已经 +1,永远判不出 FIRST。

    ⚠️ side_unknown 必须**先于** event_type 判。badge_reason 是"方向到底可不可信"
       这件事唯一被落库的痕迹(side_unknown 自己不落库,补发时靠
       poller._event_from_row 反查 reason == 'no_side' 还原),而 poller 在方向判不出时
       会把转账**兜底成 TRANSFER_IN**。顺序反过来的话这条记录落库时 reason='not_buy',
       与一笔真·收到转入完全无法区分 —— 于是「N 个人收到同一个币」可能把一笔实际是
       **转出**的记录算成收到,补发的消息也会把它渲染成「📥 收到转入」。
    ⚠️ 影响面(动手前已核对):judge_badge 只在落库那一刻跑一次、存量行永不重算,
       所以这个改动**不动任何一行历史数据**;对未来的行,只有"非 BUY 且方向判不出"
       这一类的取值从 'not_buy' 变成 'no_side' —— BUY 走哪个顺序结果都一样。
       所有按 badge_reason 过滤的 SQL(store.hot_tokens / token_buyers /
       count_recent_buyers、bot._ca_buyers_with_usd、web.queries 两处)都**同时**带
       event_type='BUY',非 BUY 的行本来就在门外。实测生产库副本 21984 行里,
       非 BUY 且 badge_reason 落在 COUNTABLE_REASONS 内的有 **0** 行。
    """
    if ev.side_unknown:                      # 方向不明:既不写 stats 也不显示共识
        return None, REASON_NO_SIDE
    if ev.event_type != EVENT_BUY:
        return None, REASON_NOT_BUY
    if ev.token_key is None:                 # 构造不出聚合键
        return None, REASON_NO_TOKEN_KEY
    if ev.is_quote:                          # 计价币不参与功能 A/B
        return None, REASON_QUOTE_TOKEN
    if not is_stats_ready(conn, ev.user_id):  # 基线没建好,判不了
        return None, REASON_NO_BASELINE

    # 【Q3】probe #8 确认"交易次数"字段语义可靠后,在这里启用单向否决票:
    #   if ev.api_trade_count and ev.api_trade_count > 1:
    #       return BADGE_ADD, REASON_API_VETO
    # 只用于否定,永不用于肯定 —— 否决是幂等的,不引入"同一事件重放结果不同"。
    # ⚠️ probe 未确认语义前这一行必须保持注释:若该字段实为"全网交易次数",
    #    则每笔买入都 >1 → 🌱 永不出现,功能 A 静默全废。

    net, ca = ev.token_key
    st = get_stats(conn, ev.user_id, net, ca)
    if st is None or st["buy_count"] == 0:
        return BADGE_FIRST, REASON_LOCAL_STATS
    return BADGE_ADD, REASON_LOCAL_STATS


def upsert_stats(conn, ev: FomoEvent) -> None:
    """
    把一笔买入计入 user_token_stats。

    ⚠️ 只在 insert_event 返回 True 时调用,否则重复轮询会把 buy_count 反复累加。
    ⚠️ stats_ready=0 的用户直接跳过 —— 基线未建立期间的事件不得污染基线,
       否则 seeding 时"当前持有但无买入记录"的老仓位判据会失效,后续错标 🌱。
    ⚠️ first_buy_at 取 MIN:乱序拉到更早的买入时才不会把时间改晚。
       兜底时间戳(ts_fallback)不写 first_buy_at,免得污染排序。
    """
    if ev.token_key is None:
        return
    if not is_stats_ready(conn, ev.user_id):
        return
    net, ca = ev.token_key
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
        (ev.user_id, net, ca, fb, now_iso()),
    )


def should_count(ev: FomoEvent, reason: str) -> bool:
    """本事件是否应计入 user_token_stats —— 判定与落库解耦,便于单测"""
    return ev.event_type == EVENT_BUY and reason in COUNTABLE_REASONS


# ============================================================
# 基线建立(seeding)
# ============================================================
def upsert_seed(conn, user_id: str, network_id: str, token_address: str,
                buy_count: int, first_buy_at: str | None) -> None:
    """回填历史买入笔数(来自分页拉取的 swaps 聚合)"""
    conn.execute(
        """
        INSERT INTO user_token_stats (user_id, network_id, token_address,
                                      buy_count, first_buy_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, network_id, token_address) DO UPDATE SET
            buy_count    = MAX(buy_count, excluded.buy_count),
            first_buy_at = MIN(COALESCE(first_buy_at, excluded.first_buy_at),
                               COALESCE(excluded.first_buy_at, first_buy_at)),
            updated_at   = excluded.updated_at
        """,
        (user_id, network_id, token_address, buy_count, first_buy_at, now_iso()),
    )


def seed_holding(conn, user_id: str, network_id: str, token_address: str) -> None:
    """
    把"当前持有但回填窗口内没有买入记录"的老仓位记为 buy_count=1。

    这一行同时做三件事:
      1) 堵死"回填窗口外的老仓位被误标 🌱"这类误报(零额外 API 调用)
      2) 让新加入用户的历史持仓计入共识分子
      3) 替代掉整个 pre_existing 标志位机制 —— 语义就是"至少买过一次,时间未知"

    ⚠️ 必须 INSERT OR IGNORE,不能覆盖上一步 upsert_seed 写入的真实笔数。
    """
    conn.execute(
        "INSERT OR IGNORE INTO user_token_stats "
        "(user_id, network_id, token_address, buy_count, first_buy_at, updated_at) "
        "VALUES (?, ?, ?, 1, NULL, ?)",
        (user_id, network_id, token_address, now_iso()),
    )


# ============================================================
# 【功能 B】共识计数
# ============================================================
def count_consensus(conn, ev: FomoEvent) -> tuple[int | None, int | None]:
    """
    返回 (买过该代币的人数, 名单总人数)。任一前置条件不满足返回 (None, None) → 共识行整段消失。

    ⚠️ 分子分母必须用**同一个谓词**(active=1 AND stats_ready=1)。
       口径不一致会算出 6/8 这种分子大于分母的输出,
       那一刻这个数字在用户心里就当场作废了。
    ⚠️ 统计对象必须是 user_token_stats(主键 (user,net,token) 天然唯一),
       **禁止在 fomo_events 上聚合** —— 拆单会把一个人算 N 次。
    """
    if ev.token_key is None or ev.is_quote or ev.side_unknown:
        return None, None
    if not is_stats_ready(conn, ev.user_id):
        return None, None

    net, ca = ev.token_key
    buyers = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM user_token_stats s
        JOIN watch_users w ON w.user_id = s.user_id
        WHERE w.active = 1 AND w.stats_ready = 1
          AND s.network_id = ? AND s.token_address = ? AND s.buy_count > 0
        """,
        (net, ca),
    ).fetchone()["n"]

    size = conn.execute(
        "SELECT COUNT(*) AS n FROM watch_users WHERE active = 1 AND stats_ready = 1"
    ).fetchone()["n"]
    return buyers, size


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def count_events_since(conn, since_iso: str) -> dict:
    """停机汇总用:窗口内各类事件的条数"""
    rows = conn.execute(
        "SELECT event_type, COUNT(*) n FROM fomo_events WHERE event_ts >= ? GROUP BY event_type",
        (since_iso,),
    ).fetchall()
    return {r["event_type"]: r["n"] for r in rows}


def upsert_token_snapshots(conn, rows: list[tuple]) -> None:
    """
    批量写入代币行情快照。rows = [(net, ca, symbol, price, market_cap), ...]

    每 tick 覆盖一次。只覆盖名单里还有人持有的币 ——
    清仓之后不再更新,updated_at 就是它最后已知的时间点(/hot 会据此标注数据新鲜度)。
    """
    ts = now_iso()
    conn.executemany(
        """
        INSERT INTO token_snapshot (network_id, token_address, symbol, price_usd,
                                    market_cap, max_market_cap, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(network_id, token_address) DO UPDATE SET
            symbol     = COALESCE(excluded.symbol, symbol),
            price_usd  = COALESCE(excluded.price_usd, price_usd),
            market_cap = COALESCE(excluded.market_cap, market_cap),
            -- 峰值只增不减。⚠️ MAX 在两个都非空时才有意义,所以先各自 COALESCE 兜底
            max_market_cap = CASE
                WHEN excluded.market_cap IS NULL THEN max_market_cap
                WHEN max_market_cap IS NULL      THEN excluded.market_cap
                ELSE MAX(max_market_cap, excluded.market_cap) END,
            -- ⚠️ 只有真的带来新市值才推进 updated_at。
            --    否则清仓后的币仍会被 trades(closedTrades 里有 currentPrice)每轮刷新时间戳,
            --    /hot 的「行情已超过 1 小时未更新」提示就永远触发不了,
            --    用户会拿着一个早已过期的倍数当真。
            updated_at = CASE WHEN excluded.market_cap IS NOT NULL
                              THEN excluded.updated_at ELSE updated_at END
        """,
        [(n, c, s, p, m, m, ts) for n, c, s, p, m in rows],
    )


def hot_tokens(conn, since_iso: str, limit: int = 12) -> list[sqlite3.Row]:
    """
    【买入榜】给定时间窗内,名单里的人买了哪些币。

    排序:**按最高倍数**(峰值市值 ÷ 名单最早买入时的市值)从高到低 ——
    这个榜要回答的是"名单挖到了什么金狗",而金狗的价值在于它**跑出来过**多少。
    用现价排的话,一个冲到 100x 又回落到 60x 的币会排在稳在 70x 的币后面,
    而前者才是那次真正抓住了的机会。现价照常在结果里(now_mcap),回撤自己看得见。
    算不出倍数的排在最后(按人数 + 总额),而不是排在最前:
    ⚠️ SQLite 里 NULL 在 DESC 排序中会排到最后,但**不能依赖它** ——
       显式写 `mult IS NULL` 做第一排序键,意图才留在代码里。

    ⚠️ 只统计 BUY 且**排除掉计价币**(badge_reason='quote_token' 的那些):
       稳定币互换会让 $USDC 恒居榜首,整个榜就废了。
    ⚠️ 基准市值取窗口内**最早那笔买入**时的值 ——
       "名单开始买的时候多大" 才是算倍数的基准,取最近一笔就没意义了。
    ⚠️ first_ts 是**真·最早那笔**的时间,而 first_mcap 取的是**最早那笔有市值的**。
       两者可能不是同一行:市值只来自 balances,而 balances 快照晚于 swaps 索引 ——
       "名单第一个人抢到新币"的那一刻他本人还没出现在自己的持仓里,
       那一行的 market_cap 就是 NULL。
       所以展示时绝不能写成"@某人在 $42K 时买入" —— 那是在断言我们并不知道的事。
    ⚠️ "谁先买的"由 token_buyers 提供(它按首笔时间正序,且**同一套谓词**)。
       这里刻意不再另出一个 first_buyer 列:同一个事实两处算,迟早会不一致。
    """
    countable = ",".join("?" * len(COUNTABLE_REASONS))
    return conn.execute(
        f"""
        WITH scoped AS (
            -- 窗口内所有"算数"的买入。⚠️ 必须 JOIN watch_users 且与 count_consensus /
            --    list_buyers 同一谓词:不 JOIN 的话,已被 /del 的人(软删除,历史事件仍在)
            --    会被算进人数、handle 还会被列在 👤 行上;刚 /add 还在建基线的人也会被算进去。
            --    结果是 /hot、/who、推送里的共识行三个"名单人数"互相矛盾。
            SELECT
                e.network_id, e.token_address, e.token_symbol, e.user_id,
                e.user_handle, e.handle, e.amount_usd, e.market_cap, e.event_ts,
                -- 最早**且有市值**的那一行:没市值的排到分区末尾
                ROW_NUMBER() OVER (
                    PARTITION BY e.network_id, e.token_address
                    ORDER BY CASE WHEN e.market_cap IS NULL THEN 1 ELSE 0 END, e.event_ts
                ) AS rn_mcap
            FROM fomo_events e
            JOIN watch_users w
              ON w.user_id = e.user_id AND w.active = 1 AND w.stats_ready = 1
            WHERE e.event_type = 'BUY'
              AND e.event_ts >= ?
              AND e.token_address IS NOT NULL
              -- 与 should_count 同一套判据:计价币、方向不明的都不算买入
              AND COALESCE(e.badge_reason, '') IN ({countable})
        ),
        agg AS (
            SELECT
                network_id, token_address,
                MAX(token_symbol)              AS symbol,
                COUNT(DISTINCT user_id)        AS buyers,
                COUNT(*)                       AS buys,
                SUM(COALESCE(amount_usd, 0))   AS total_usd,
                MIN(event_ts)                  AS first_ts,
                MAX(event_ts)                  AS last_ts
            FROM scoped
            GROUP BY network_id, token_address
        )
        SELECT
            a.*,
            m.market_cap                       AS first_mcap,
            m.event_ts                         AS first_mcap_at,
            s.market_cap                       AS now_mcap,
            s.max_market_cap                   AS peak_mcap,
            s.updated_at                       AS mcap_at,
            -- 倍数 = **峰值** ÷ 名单最早买入时的市值,也就是"名单摸到之后最多涨过多少倍"。
            -- ⚠️ 刻意不用现价:这个榜要回答"名单挖到了什么金狗",而金狗的价值在于
            --    它跑出来过多少 —— 一个冲到 100x 又回落到 60x 的币,排在一个稳在 70x 的
            --    币后面是不合理的。现价照常在 💎 行里显示,回撤自己看得见。
            -- ⚠️ COALESCE 兜底:老库里 max_market_cap 可能还没回填上
            CASE WHEN COALESCE(s.max_market_cap, s.market_cap) IS NOT NULL AND m.market_cap > 0
                 THEN COALESCE(s.max_market_cap, s.market_cap) * 1.0 / m.market_cap
            END AS mult
        FROM agg a
        LEFT JOIN scoped m ON m.network_id = a.network_id
                          AND m.token_address = a.token_address AND m.rn_mcap = 1
        LEFT JOIN token_snapshot s ON s.network_id = a.network_id
                                  AND s.token_address = a.token_address
        ORDER BY (mult IS NULL), mult DESC, buyers DESC, total_usd DESC
        LIMIT ?
        """,  # noqa: S608
        (since_iso, *COUNTABLE_REASONS, int(limit)),
    ).fetchall()


def token_buyers(conn, network_id: str, token_address: str, since_iso: str,
                 limit: int = 6) -> list[sqlite3.Row]:
    """
    某个币在窗口内被谁买过(按首次买入时间正序 —— 谁先发现的排前面)。

    每行:who(@handle)/ ts(他第一笔的时间)/ usd(窗口内累计买入额)/ buys(笔数)/
          mcap(他**进场时**的市值)。
    ⚠️ usd 是**累计**不是首笔:一个人分五笔建仓,只报首笔会把他的实际投入
       低报成五分之一,而"谁下的注最大"正是这一行的价值所在。
    ⚠️ mcap 取的是他**最早一笔有市值**的那笔,不是最早那笔:
       市值只来自 balances,而 balances 快照晚于 swaps 索引 ——
       抢到新币的那一刻本人还没出现在自己的持仓里,那行 market_cap 就是 NULL。
       不往后找的话,恰恰是"抢得最早的人"没有进场市值可显示。
       多笔建仓时它只代表**第一笔**的位置,所以展示时旁边必须带上笔数。

    ⚠️ 谓词必须与 hot_tokens / count_consensus 完全一致,否则「👥 5 人买入」
       下面列出来的名字会对不上,甚至把已 /del 的人的 handle 摆在那里。
    """
    countable = ",".join("?" * len(COUNTABLE_REASONS))
    return conn.execute(
        f"""
        WITH scoped AS (
            SELECT e.user_id, e.user_handle, e.handle, e.event_ts, e.amount_usd, e.market_cap,
                   -- 该用户最早**且有市值**的那一行:没市值的排到分区末尾
                   ROW_NUMBER() OVER (
                       PARTITION BY e.user_id
                       ORDER BY CASE WHEN e.market_cap IS NULL THEN 1 ELSE 0 END, e.event_ts
                   ) AS rn_mcap
            FROM fomo_events e
            JOIN watch_users w
              ON w.user_id = e.user_id AND w.active = 1 AND w.stats_ready = 1
            WHERE e.event_type = 'BUY' AND e.network_id = ? AND e.token_address = ?
              AND e.event_ts >= ?
              AND COALESCE(e.badge_reason, '') IN ({countable})
        ),
        agg AS (
            SELECT user_id,
                   COALESCE(MAX(user_handle), MAX(handle)) AS who,
                   MIN(event_ts)                           AS ts,
                   SUM(COALESCE(amount_usd, 0))            AS usd,
                   COUNT(*)                                AS buys
            FROM scoped GROUP BY user_id
        )
        SELECT a.who, a.ts, a.usd, a.buys, m.market_cap AS mcap
        FROM agg a
        LEFT JOIN scoped m ON m.user_id = a.user_id AND m.rn_mcap = 1
        ORDER BY a.ts
        LIMIT ?
        """,  # noqa: S608
        (network_id, token_address, since_iso, *COUNTABLE_REASONS, int(limit)),
    ).fetchall()


def save_user_pnl(conn, rows: list[dict]) -> None:
    """
    覆盖写名单成员盈亏。

    ⚠️ None 必须原样写 None,不能填 0 —— 0 是「不赚不亏」,None 是「拿不到」,
       写成 0 会让拿不到数据的人在排行里排到中间去。
    """
    ts = now_iso()
    cols = ("total_pnl", "pnl_24h", "pnl_7d", "pnl_30d", "total_holdings", "num_trades")
    with tx(conn):
        conn.executemany(
            """
            INSERT INTO user_pnl_snapshot(user_id, total_pnl, pnl_24h, pnl_7d,
                                          pnl_30d, total_holdings, num_trades, updated_at)
            VALUES (:user_id, :total_pnl, :pnl_24h, :pnl_7d,
                    :pnl_30d, :total_holdings, :num_trades, :ts)
            ON CONFLICT(user_id) DO UPDATE SET
                total_pnl = excluded.total_pnl, pnl_24h = excluded.pnl_24h,
                pnl_7d = excluded.pnl_7d, pnl_30d = excluded.pnl_30d,
                total_holdings = excluded.total_holdings,
                num_trades = excluded.num_trades, updated_at = excluded.updated_at
            """,
            [{"user_id": r["user_id"], "ts": ts,
              **{c: r.get(c) for c in cols}} for r in rows],
        )


def load_user_pnl(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM user_pnl_snapshot").fetchall()


# ============================================================
# 【价格历史】采样 / 读取 / 清理
# ============================================================
def save_price_samples(conn, rows: list[tuple]) -> None:
    """
    批量写入价格历史采样。

    rows = [(network_id, token_address, sampled_at, price_usd, market_cap), ...]
    调用方(poller._maybe_sample_price_history)每轮为整批代币生成**同一个**
    sampled_at,代表"这一刻的截面"——这里不再自己生成时间戳,是为了让
    "同一个币同一时刻重复采样"这件事完全由调用方的输入决定,而不是被
    now_iso() 秒级精度的巧合悄悄影响,测试也因此能稳定复现幂等性。

    ⚠️ INSERT OR IGNORE:命中主键(network_id, token_address, sampled_at)冲突时
       直接跳过,不报错也不产生第二行 —— 这就是"再次采样同一时刻"的幂等实现。
    ⚠️ price_usd / market_cap 必须原样传 None,绝不能在调用方把 None 改写成 0 ——
       0 是真实价格/市值(见 formatter.py 头部的规矩),executemany 会把 None
       正确绑定成 SQL NULL,这里唯一要守住的是不能提前把它填掉。
    """
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO token_price_history
            (network_id, token_address, sampled_at, price_usd, market_cap)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )


def load_price_history(conn, network_id: str, token_address: str,
                       since_iso: str) -> list[sqlite3.Row]:
    """
    某个币的价格采样序列,升序(旧→新)—— sparkline 从左到右画的顺序,
    也是图表唯一会用到的读法(见主键顺序的说明)。
    """
    return conn.execute(
        """
        SELECT * FROM token_price_history
        WHERE network_id = ? AND token_address = ? AND sampled_at >= ?
        ORDER BY sampled_at ASC
        """,
        (network_id, token_address, since_iso),
    ).fetchall()


# 分批删除的批大小与单次调用的批数上限。
# ⚠️ 稳态下这张表是百万行量级,一条不加限制的 DELETE 会长时间占住写锁,
#    堵住同一时刻的采样 / 事件落库(WAL 下写者互斥)。分批 + 每批独立事务,
#    把最坏情况下单批的阻塞时间摊薄到毫秒级;命中批数上限时剩余的留到
#    下一次 prune(远低于采样频率,见 poller._maybe_prune_price_history)继续删 ——
#    保留期本身就是"3 天左右"的模糊承诺,没必要为了删干净而让某一次 tick 卡顿。
_PRUNE_BATCH_SIZE = 5000
_PRUNE_MAX_BATCHES = 20


def prune_price_history(conn, keep_days: int) -> int:
    """清理超过 keep_days 天的价格历史,返回本次实际删除的行数。"""
    cutoff = iso_minutes_ago(keep_days * 24 * 60)
    deleted = 0
    for _ in range(_PRUNE_MAX_BATCHES):
        with tx(conn):
            cur = conn.execute(
                """
                DELETE FROM token_price_history
                WHERE rowid IN (
                    SELECT rowid FROM token_price_history
                    WHERE sampled_at < ? LIMIT ?
                )
                """,
                (cutoff, _PRUNE_BATCH_SIZE),
            )
        deleted += cur.rowcount
        if cur.rowcount < _PRUNE_BATCH_SIZE:
            break
    return deleted


# ============================================================
# 【跟单】配置与台账
# ============================================================
_COPY_KEY = "copytrade_config"


# 这几项的 None 是**合法值**(= 不限);其余字段的 None 一律当成坏数据。
# ⚠️ 区分它俩是必须的:daily_max 若为 None,decide() 里的 `None > 0` 会抛 TypeError,
#    被 tick 那层 try 吞掉 —— 表现是跟单**静默停摆**,日志里只有一行"判定失败"。
_COPY_NULLABLE = frozenset({"max_age_hours", "max_entry_mcap", "daily_spend_usd"})
_COPY_TYPES = {
    "enabled": bool, "paper_only": bool, "dry_run_execute": bool, "auto_execute": bool,
    "starred_only": bool,
    "min_buyers": int, "window_hours": int, "daily_max": int, "max_age_hours": int,
    "amount_usd": float, "max_entry_mcap": float, "daily_spend_usd": float,
    "networks": tuple,
}


def _coerce_copy_field(name: str, val, default):
    """把 JSON 里读到的值收敛成字段该有的类型;收不动就退回默认值"""
    if val is None:
        return None if name in _COPY_NULLABLE else default
    t = _COPY_TYPES.get(name)
    try:
        if t is tuple:
            return tuple(str(x) for x in val)
        if t is not None:
            return t(val)
    except (TypeError, ValueError):
        return default
    return val


def load_copy_config(conn) -> CopyConfig:
    """
    从 runtime_state 读跟单配置,读不到 / 坏了都退回默认值(enabled=False)。

    ⚠️ 任何异常都必须退回**默认值**而不是上抛:配置读坏了就把整个 tick 打挂,
       等于一个展示性功能能停掉主推送。而默认值是"不启用",最坏情况是不跟单,安全。
    ⚠️ 逐字段收敛类型,不要直接把 JSON 灌进 dataclass:坏掉的**单个**字段
       不该让整份配置退回默认(那会把用户调好的参数悄悄换掉),更不该在下游抛异常。
    """
    try:
        raw = get_state(conn, _COPY_KEY)
        if not raw:
            return CopyConfig()
        d = json.loads(raw)
        base = CopyConfig()
        return CopyConfig(**{
            f: _coerce_copy_field(f, d.get(f, getattr(base, f)), getattr(base, f))
            for f in base.__dataclass_fields__
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("跟单配置读取失败,按未启用处理: {}", e)
        return CopyConfig()


def save_copy_config(conn, cfg: CopyConfig) -> None:
    d = {f: getattr(cfg, f) for f in cfg.__dataclass_fields__}
    d["networks"] = list(cfg.networks)          # tuple 不是 JSON 类型
    with tx(conn):
        set_state(conn, _COPY_KEY, json.dumps(d, ensure_ascii=False))


def count_recent_buyers(conn, network_id: str, token_address: str,
                        since_iso: str, starred_only: bool = False) -> int:
    """
    窗口内买过这个币的**名单成员**数(去重到人)。跟单信号的分子。

    ⚠️ 必须带时间窗:一个币被 3 个人在三个月里分别买过,不构成"大家在抢"。
    ⚠️ 谓词与 count_consensus / hot_tokens 完全一致(active=1 AND stats_ready=1
       + COUNTABLE_REASONS),否则 /hot 上写着 5 人、跟单却按 3 人算。
    """
    star = " AND w.starred = 1" if starred_only else ""
    countable = ",".join("?" * len(COUNTABLE_REASONS))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT e.user_id) AS n
        FROM fomo_events e
        JOIN watch_users w
          ON w.user_id = e.user_id AND w.active = 1 AND w.stats_ready = 1{star}
        WHERE e.event_type = 'BUY' AND e.network_id = ? AND e.token_address = ?
          AND e.event_ts >= ?
          AND COALESCE(e.badge_reason, '') IN ({countable})
        """,  # noqa: S608
        (network_id, token_address, since_iso, *COUNTABLE_REASONS),
    ).fetchone()
    return int(row["n"] or 0)


# 入场市值允许有多旧。⚠️ 这个值同时是**筛选闸门**和**台账成本**,
#    而 token_snapshot 只覆盖"名单里还有人持有"的币 —— 清仓后就冻在那儿不动了。
#    拿一个几小时前的低市值当"现在的价",会让 max_entry_mcap 放行本该拦掉的币,
#    还会在真实仓位上凭空记出一笔纸面盈利。
#    实测:snapshot 有 90% 在 1 小时内,所以 60 分钟这道线几乎不损失覆盖率。
SNAPSHOT_FRESH_MIN = 60


def fresh_snapshot_mcap(conn, network_id: str, token_address: str,
                        max_age_min: int = SNAPSHOT_FRESH_MIN) -> float | None:
    """够新的快照市值;太旧或没有都返回 None(由调用方决定要不要因此不跟)"""
    row = conn.execute(
        """
        SELECT market_cap FROM token_snapshot
        WHERE network_id = ? AND token_address = ? AND updated_at >= ?
        """,
        (network_id, token_address, iso_minutes_ago(max_age_min)),
    ).fetchone()
    return None if row is None else row["market_cap"]


def copy_taken_today(conn) -> int:
    """今天(UTC)已经触发了几单 —— 每日**笔数**上限的分子(含纸上跟单)"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM copytrade_signals WHERE triggered_at >= ?",
        (now_iso()[:10] + "T00:00:00+00:00",),
    ).fetchone()
    return int(row["n"] or 0)


# 「钱已经出去或正在出去」的状态。每日**金额**上限只数这些。
# ⚠️ 与 copy_taken_today 的口径**故意不同**:那个数的是"今天触发了几个信号"
#    (纸上跟单也算,因为它就是用来限制信号量的);这个数的是真金白银。
#    两者混用会出两种错:要么纸上信号吃掉真实额度,要么失败单白白占住上限。
# ⚠️ 'failed' 不在其中是有依据的:executor.py 里每一处 raise 都在
#    submit.click() **之前** —— 抛异常就意味着那一下根本没点。
#    这条依赖以后改 executor 时要一起看。
# ⚠️ unknown 也算。它的定义就是"钱可能已经出去了但程序不知道" ——
#    对**上限**而言,不确定必须按花了算,否则重启一次就能把额度洗掉一遍。
SPENDING_STATUSES = ("pending", "executing", "auto_queued", "auto_executing",
                     "filled", "unknown")


def copy_spent_today(conn) -> float:
    """今天(UTC)真实花掉(或正在花)多少美元 —— 每日金额上限的分子"""
    marks = ",".join("?" * len(SPENDING_STATUSES))
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(amount_usd), 0) AS s FROM copytrade_signals
        WHERE triggered_at >= ? AND status IN ({marks})
        """,  # noqa: S608
        (now_iso()[:10] + "T00:00:00+00:00", *SPENDING_STATUSES),
    ).fetchone()
    return float(row["s"] or 0.0)


def record_copy_signal(conn, *, network_id: str, token_address: str, token_symbol: str | None,
                       buyers: int, entry_mcap: float | None, age_sec: int | None,
                       amount_usd: float, status: str) -> bool:
    """
    记一条跟单信号。返回 True 表示**这次真的新建了**(而不是撞上已有的)。

    ⚠️ 靠主键冲突保证"单币只跟一次",不是先 SELECT 再 INSERT ——
       后者在两个 tick 撞上时会重复建仓,而重复建仓在真实下单模式下就是真的多花一份钱。
    """
    with tx(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO copytrade_signals
                (network_id, token_address, token_symbol, triggered_at, trigger_buyers,
                 entry_mcap, token_age_sec, amount_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (network_id, token_address, token_symbol, now_iso(), buyers,
             entry_mcap, age_sec, amount_usd, status),
        )
    return cur.rowcount == 1


# 进程一启动,这两个状态就**必然是孤儿** —— 中间态只存在于某个正在跑的进程里,
# 而那个进程已经没了。区别在于钱有没有可能已经出去。
_INFLIGHT_SPENT = ("executing", "auto_executing")   # 可能已点成交 → 只能人工核对
_INFLIGHT_CLEAN = ("auto_queued",)                  # 还没轮到执行 → 确定没花钱


def reconcile_inflight(conn) -> tuple[list[sqlite3.Row], int]:
    """
    启动对账。返回 (需要人工核对的行, 已判定为未执行的条数)。

    ⚠️ 为什么必须有这一步:关闭时**不等**在途的买入(那可能要一分钟),
       所以强杀/Ctrl+C 必然留下 auto_executing。不认领的话:
       ① 没人知道那一单到底成没成;
       ② 这个币因主键冲突再也不会被跟,而且没有任何迹象。

    ⚠️ auto_executing **绝不能**自动判成 failed:CAS 抢占发生在点击之前,
       但点击之后到写终态之间也有一段 —— 钱可能已经出去了。
       只有 auto_queued 是安全的:worker 会先 CAS 成 auto_executing 再执行,
       所以还停在 auto_queued 就一定没开始跑。
    """
    marks = ",".join("?" * len(_INFLIGHT_SPENT))
    rows = conn.execute(
        f"SELECT * FROM copytrade_signals WHERE status IN ({marks}) "  # noqa: S608
        "ORDER BY triggered_at",
        _INFLIGHT_SPENT,
    ).fetchall()
    with tx(conn):
        for r in rows:
            conn.execute(
                "UPDATE copytrade_signals SET status = 'unknown', decided_at = ?, "
                "note = COALESCE(note, '') || ' | 进程重启时仍在执行中,结果未知' "
                "WHERE network_id = ? AND token_address = ?",
                (now_iso(), r["network_id"], r["token_address"]),
            )
        cur = conn.execute(
            "UPDATE copytrade_signals SET status = 'failed', decided_at = ?, "
            "note = '进程重启时还在排队,未执行' "
            f"WHERE status IN ({','.join('?' * len(_INFLIGHT_CLEAN))})",  # noqa: S608
            (now_iso(), *_INFLIGHT_CLEAN),
        )
    return list(rows), cur.rowcount


def expire_stale_pending(conn, max_age_hours: int = 24) -> int:
    """
    把太老的待确认信号作废。返回作废条数。

    ⚠️ TG 里的按钮**不会过期**。三天前那条消息上的 [确认买入] 现在点下去,
       买的是今天的价、依据的是三天前的判定 —— 而这个信号的全部前提就是"刚刚"。
       与其指望人记得别点,不如让它点不动。
    """
    with tx(conn):
        cur = conn.execute(
            "UPDATE copytrade_signals SET status = 'expired', decided_at = ?, "
            "note = '超过 ' || ? || ' 小时未确认,已作废' "
            "WHERE status = 'pending' AND triggered_at < ?",
            (now_iso(), max_age_hours, iso_minutes_ago(max_age_hours * 60)),
        )
    return cur.rowcount


def copy_day_summary(conn, day_iso: str | None = None) -> dict:
    """
    某一天(UTC)的跟单对账:各状态几单、花了多少、几单结果待核对。

    ⚠️ 「待核对」单独算一格 —— 那是**钱可能出去了但程序不知道**的那些,
       混在总数里等于没报。
    """
    day = (day_iso or now_iso())[:10]
    lo, hi = f"{day}T00:00:00+00:00", f"{day}T23:59:59+00:00"
    rows = conn.execute(
        "SELECT status, COUNT(*) n, COALESCE(SUM(amount_usd), 0) usd "
        "FROM copytrade_signals WHERE triggered_at BETWEEN ? AND ? GROUP BY status",
        (lo, hi),
    ).fetchall()
    by = {r["status"]: {"n": r["n"], "usd": float(r["usd"])} for r in rows}
    marks = ",".join("?" * len(SPENDING_STATUSES))
    spent = conn.execute(
        f"SELECT COALESCE(SUM(amount_usd), 0) s FROM copytrade_signals "  # noqa: S608
        f"WHERE triggered_at BETWEEN ? AND ? AND status IN ({marks})",
        (lo, hi, *SPENDING_STATUSES),
    ).fetchone()["s"]
    # note 里带「待核对」的是 executor 那条 confirmed=False 的分支
    unclear = conn.execute(
        "SELECT COUNT(*) n FROM copytrade_signals WHERE triggered_at BETWEEN ? AND ? "
        "AND (status = 'unknown' OR (status = 'filled' AND COALESCE(note,'') LIKE '%没读到%'))",
        (lo, hi),
    ).fetchone()["n"]
    return {"day": day, "by_status": by, "spent_usd": float(spent),
            "total": sum(v["n"] for v in by.values()), "unclear": int(unclear)}


def copy_ledger(conn, limit: int = 20) -> list[sqlite3.Row]:
    """跟单台账 + 当前市值(算盈亏用)。按触发时间倒序"""
    # ⚠️ 一并带出 now_mcap 的时间。这张快照只覆盖"名单里还有人持有"的币,
    #    清仓后就冻住了 —— 而 /paper 拿它算盈亏。不把新鲜度暴露出来的话,
    #    一个三天前的价会和实时价长得一模一样。
    return conn.execute(
        """
        SELECT g.*, s.market_cap AS now_mcap, s.max_market_cap AS peak_mcap,
               s.updated_at AS mcap_at
        FROM copytrade_signals g
        LEFT JOIN token_snapshot s
               ON s.network_id = g.network_id AND s.token_address = g.token_address
        ORDER BY g.triggered_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def set_copy_status(conn, network_id: str, token_address: str, status: str,
                    note: str | None = None, *, expect: str | tuple[str, ...] | None = None) -> bool:
    """
    改一条信号的状态。返回 True 表示**这次真的改到了**。

    expect 给了就是一次 CAS(compare-and-swap):只有当前状态在 expect 里才会改。
    ⚠️ 抢占语义必须靠它,不能靠"先 SELECT 判断、再 UPDATE" ——
       那两步不在同一个事务里,而 poller(主线程)和 bot(daemon 线程)是两条独立连接,
       WAL 挡不住这种读改写竞态。
    ⚠️ 后果不是"买两次"(浏览器 profile 锁天然互斥),而是**状态互相覆盖**:
       买入成功写了 filled,另一条路径把它盖成 rejected/failed ——
       钱花出去了、台账写着"未成交"、TG 还弹个 ❌ 反过来诱导人再点一次。
    """
    if expect is None:
        sql = ("UPDATE copytrade_signals SET status = ?, decided_at = ?, note = ? "
               "WHERE network_id = ? AND token_address = ?")
        args: tuple = (status, now_iso(), note, network_id, token_address)
    else:
        want = (expect,) if isinstance(expect, str) else tuple(expect)
        marks = ",".join("?" * len(want))
        sql = ("UPDATE copytrade_signals SET status = ?, decided_at = ?, note = ? "
               f"WHERE network_id = ? AND token_address = ? AND status IN ({marks})")  # noqa: S608
        args = (status, now_iso(), note, network_id, token_address, *want)
    with tx(conn):
        cur = conn.execute(sql, args)
    return cur.rowcount == 1


# ============================================================
# 【转入告警】N 个名单成员收到同一个币
# ============================================================
# ⚠️⚠️ 本节与跟单执行器**没有任何关系**,并且永远不该有。
#    「收到免费筹码」与「自己掏钱买入」是相反的含义:前者多半是项目方/内部人在分发,
#    拿它去触发花钱的操作方向就是错的。这几个函数只服务于一条推送。
#    这条边界的落点是 should_count(只认 EVENT_BUY)——**别去动它**。

# ⚠️ 金额门槛(min_usd)**没有默认值,必须由调用方传**。
#    这里曾经放过一个 TRANSFER_MIN_USD = 500.0 的"兜底默认值",而生产路径永远传
#    settings.fomo_transfer_alert_min_usd —— 于是同一个阈值有两份写法,改了配置
#    这边不动、看代码的人还以为 500 生效着。唯一真源是 config.fomo_transfer_alert_min_usd,
#    这条注释是它在本模块留下的全部痕迹(阈值取值的实测依据见 config 里那段说明)。


def count_recent_receivers(conn, network_id: str, token_address: str, since_iso: str,
                           min_usd: float) -> int:
    """
    窗口内**收到**这个币的名单成员数(去重到人)。转入告警的分子。

    ⚠️ 三处与买入侧 count_recent_buyers 刻意不同,照抄任何一条都会静默失效:

    1) **绝不能带 `badge_reason IN COUNTABLE_REASONS`**。judge_badge 对非 BUY 事件
       一律返回 REASON_NOT_BUY,TRANSFER_IN 永远进不了 COUNTABLE_REASONS ——
       照抄的话本函数**恒返回 0 且不报任何错**,正是最难查的那类失效。
       (守这一条的回归测试:tests/test_store.py::test_收到计数不得复用买入侧的可计数过滤)

    2) **必须自己补一条计价币过滤**。买入侧是靠 COUNTABLE_REASONS 顺带把
       badge_reason='quote_token' 滤掉的;这里没有那层,而 USDC/USDT/WETH 的内部划转
       量极大(名单里天天有人搬稳定币),不滤就是刷屏。原生币(SOL/ETH/BNB)在
       poller._transfer_to_event 就按 isNativeToken 显式跳过了,进不到库里。

    3) **必须排除 badge_reason='no_side' 的行**。poller 在方向判不出时会把记录兜底成
       TRANSFER_IN —— 那可能实际是一笔**转出**。把它算成"收到"就是在报相反的事实。

    可以照抄的只有:JOIN watch_users(active)、时间窗、COUNT(DISTINCT user_id)。

    ⚠️ **不带 stats_ready=1**,与买入侧不同。stats_ready 的语义是"这个人的历史买入基线
       已经回填好了",它保护的是徽章判定与共识分子分母 —— 而"他收到过这个币"是一条
       与基线毫无关系的事实,一笔转账就是一笔转账。带上它的唯一效果是让刚 /add 进来、
       基线还没建完的人在这个信号里凭空消失,而分发信号最该抓的恰恰是刚加进来的新人。

    ⚠️ 金额门槛的判据是 `amount_usd >= ?`,SQL 里 NULL 参与比较结果是 NULL(不成立),
       所以拿不到金额的记录天然被排除 —— 这正是要的语义:金额未知就不能声称它达标。
       不能写成 COALESCE(amount_usd, 0) 之类,那会把"不知道"当成"知道是 0"。
    """
    # 计价币直接短路。写在函数里而不是留给调用方,是为了让这条过滤**不可能被漏掉**
    if is_quote_token(network_id, token_address):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT e.user_id) AS n
        FROM fomo_events e
        JOIN watch_users w ON w.user_id = e.user_id AND w.active = 1
        WHERE e.event_type = ?
          AND e.network_id = ? AND e.token_address = ?
          AND e.event_ts >= ?
          AND COALESCE(e.badge_reason, '') <> ?
          AND e.amount_usd >= ?
        """,
        (EVENT_TRANSFER_IN, network_id, token_address, since_iso,
         REASON_NO_SIDE, float(min_usd)),
    ).fetchone()
    return int(row["n"] or 0)


def transfer_receivers(conn, network_id: str, token_address: str, since_iso: str,
                       min_usd: float) -> list[sqlite3.Row]:
    """
    窗口内收到这个币的人**全部**列出来:谁、各自收到多少、在什么市值收到的
    (按时间正序 —— 谁先拿到的排前面)。

    ⚠️ 谓词必须与 count_recent_receivers **逐条一致**,否则消息里写着 5 人、
       底下只列得出 3 个名字。
       (这条不再只是一句注释:poller 每轮都把 len(rows) 与 count_recent_receivers
        的返回值对一次,不等就打 WARNING —— 漂移了总得有人知道。)
    ⚠️⚠️ **这里没有 LIMIT,而且不许加回来。**
       它曾经是 `limit: int = 12`、poller 传 10。于是收到者超过 10 人时,
       poller 拿这 10 行求和,写进 transfer_in_signals.total_usd、也渲进消息,
       却把它摆在 count_recent_receivers 数出来的**全量人数**旁边 ——
       「25 人收到 · 合计 $X」里的 X 只是其中 10 个人的合计,是一句假话,
       而这个功能抓的恰恰是分发事件,超过 10 人本来就正常。
       LIMIT 是**展示层**的关注点(一条 TG 消息装不下 91 行),漏到查询层就会
       污染"合计"与台账这两个必须与人数同批的事实。截断现在在 formatter 里做。
       全取回来没有成本:SQL 是 GROUP BY user_id,一人一行,行数上限就是名单人数。
    ⚠️ mcap 取该用户**最早一笔有市值**的那条:市值来自本 tick 的 balances 索引
       (报文本身没有 marketCap),名单里还没人持有这个币时它就是 NULL。
       取不到就是 NULL,由渲染层让那一格消失 —— 绝不拿"现在的市值"冒充"收到时的市值"。
    """
    if is_quote_token(network_id, token_address):
        return []
    return conn.execute(
        """
        WITH scoped AS (
            SELECT e.user_id, e.user_handle, e.handle, e.event_ts, e.amount_usd, e.market_cap,
                   ROW_NUMBER() OVER (
                       PARTITION BY e.user_id
                       ORDER BY CASE WHEN e.market_cap IS NULL THEN 1 ELSE 0 END, e.event_ts
                   ) AS rn_mcap
            FROM fomo_events e
            JOIN watch_users w ON w.user_id = e.user_id AND w.active = 1
            WHERE e.event_type = ?
              AND e.network_id = ? AND e.token_address = ?
              AND e.event_ts >= ?
              AND COALESCE(e.badge_reason, '') <> ?
              AND e.amount_usd >= ?
        ),
        agg AS (
            SELECT user_id,
                   COALESCE(MAX(user_handle), MAX(handle)) AS who,
                   MIN(event_ts)                           AS ts,
                   -- ⚠️ 不用 COALESCE(amount_usd,0):谓词已经保证每行都有金额,
                   --    SUM 的标准语义在这里就是对的
                   SUM(amount_usd)                         AS usd,
                   COUNT(*)                                AS hits
            FROM scoped GROUP BY user_id
        )
        SELECT a.who, a.ts, a.usd, a.hits, m.market_cap AS mcap
        FROM agg a
        LEFT JOIN scoped m ON m.user_id = a.user_id AND m.rn_mcap = 1
        ORDER BY a.ts
        """,
        (EVENT_TRANSFER_IN, network_id, token_address, since_iso,
         REASON_NO_SIDE, float(min_usd)),
    ).fetchall()


def transfer_senders(conn, network_id: str, token_address: str, since_iso: str,
                     min_usd: float) -> dict:
    """
    这些币**是从哪些钱包发过来的** —— 转入告警里唯一站得住的那条证据。

    返回 {"known": 查得到发货地址的人数, "distinct": 这些地址去重后几个,
          "top": {"address", "receivers", "first_ts", "last_ts"} | None}
    top 只在某个地址发给了 **≥2 个人** 时才有值。

    ⚠️ 为什么需要它:报文里只有 fromAddress / toAddress,**没有 userId**
       (8404 条真实转账里 userId 键出现 0 次)。所以下面这两件事在数据上一模一样:
         (a) 项目方/内部人在给一群人分发筹码   ← 用户关心的
         (b) 这个人把自己在别处买的币充进 FOMO ← 与买入同义,方向相反
       消息里绝不能替用户断言是哪一种。但"同一个钱包在 5 分钟内发给了三个不同的人"
       是**可证的事实**,而且它把概率明显推向 (a) —— 这才是该写进消息里的东西。
    ⚠️ 谓词必须与 count_recent_receivers 逐条一致,再加一条"地址非空":
       地址缺失的行不能算进 known,否则"各不相同"这句话会建立在没查到的数据上。
    ⚠️ 聚类是**加强证据,不是触发条件**:地址各不相同照样告警(仍然是 N 个人同时
       收到同一个币),只是消息里不能声称有共同发货方。判定仍然只看
       count_recent_receivers,这个函数一行都不参与。
    """
    empty = {"known": 0, "distinct": 0, "top": None}
    if is_quote_token(network_id, token_address):
        return empty
    rows = conn.execute(
        """
        WITH scoped AS (
            SELECT e.user_id, e.counterparty_address AS addr, e.event_ts
            FROM fomo_events e
            JOIN watch_users w ON w.user_id = e.user_id AND w.active = 1
            WHERE e.event_type = ?
              AND e.network_id = ? AND e.token_address = ?
              AND e.event_ts >= ?
              AND COALESCE(e.badge_reason, '') <> ?
              AND e.amount_usd >= ?
              AND e.counterparty_address IS NOT NULL
              AND TRIM(e.counterparty_address) <> ''
        ),
        -- ⚠️ 先塌到"每个地址 × 每个人最早那一笔":同一个人被同一个地址连发五笔时,
        --    时间跨度该按**人**算,否则"5 分 23 秒内发给 3 个人"会被自己的补发拉长
        per_user AS (
            SELECT addr, user_id, MIN(event_ts) AS ts FROM scoped GROUP BY addr, user_id
        )
        SELECT addr, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM per_user GROUP BY addr
        ORDER BY n DESC, first_ts ASC
        """,
        (EVENT_TRANSFER_IN, network_id, token_address, since_iso,
         REASON_NO_SIDE, float(min_usd)),
    ).fetchall()
    if not rows:
        return empty
    # known 要**去重到人**:一个人从两个地址各收一笔时,他只是一个人
    known = len({r["user_id"] for r in conn.execute(
        """
        SELECT DISTINCT e.user_id
        FROM fomo_events e
        JOIN watch_users w ON w.user_id = e.user_id AND w.active = 1
        WHERE e.event_type = ?
          AND e.network_id = ? AND e.token_address = ?
          AND e.event_ts >= ?
          AND COALESCE(e.badge_reason, '') <> ?
          AND e.amount_usd >= ?
          AND e.counterparty_address IS NOT NULL
          AND TRIM(e.counterparty_address) <> ''
        """,
        (EVENT_TRANSFER_IN, network_id, token_address, since_iso,
         REASON_NO_SIDE, float(min_usd)),
    )})
    top = rows[0]
    return {
        "known": known,
        "distinct": len(rows),
        "top": ({"address": top["addr"], "receivers": int(top["n"]),
                 "first_ts": top["first_ts"], "last_ts": top["last_ts"]}
                if int(top["n"]) >= 2 else None),
    }


def record_transfer_in_signal(conn, *, network_id: str, token_address: str,
                              token_symbol: str | None, receivers: int,
                              total_usd: float | None) -> bool:
    """
    记一行转入告警台账。返回 True 表示**这次是新的**(该推),False = 这个币已经告警过。

    ⚠️ "单币只告警一次"由主键保证,而不是靠先 SELECT 再 INSERT —— 后者在两个 tick
       撞上时会推两条。与 copytrade 的 record_copy_signal 同一个道理,但**另起一张表**
       (理由见 _SCHEMA 里 transfer_in_signals 的注释:共用主键会让两类信号互相吃掉)。
    """
    with tx(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transfer_in_signals
                (network_id, token_address, token_symbol, triggered_at, receivers, total_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (network_id, token_address, token_symbol, now_iso(), int(receivers), total_usd),
        )
    return cur.rowcount == 1


def drop_transfer_in_signal(conn, network_id: str, token_address: str) -> bool:
    """
    把一行转入告警台账**退回去**。返回 True 表示确实删掉了一行。

    ⚠️ 存在的唯一理由:record_transfer_in_signal 必须排在渲染/推送**之前**
       (两个 tick 撞上时靠主键决出唯一赢家),可主键 (network_id, token_address)
       的语义是"这个币这辈子只告警一次" —— 于是一次 TG 400 或网络抖动就等于
       **这个币的告警永久丢失**,而且日志里只有一行 error。
       所以推送没成功时要把占位退掉,让下一轮重新走一遍。
    ⚠️ 退回之后重试靠 poller._transfer_retry 兜着(本轮没有新转账的币不会再被扫到),
       两者缺一不可:只退台账不重试 = 要等这个币下一笔转账才补发。
    """
    with tx(conn):
        cur = conn.execute(
            "DELETE FROM transfer_in_signals WHERE network_id = ? AND token_address = ?",
            (network_id, token_address),
        )
    return cur.rowcount == 1


# ============================================================
# 【币安 Alpha】已推送台账
# ============================================================
# ⚠️ 这三个函数**都不自己开事务**,与 set_state 同一约定 ——
#    调用方要把「记台账 + 前移水位线 + 清理」放进同一个 tx 里,
#    拆成三个事务的话中途崩一次就会留下自相矛盾的状态(水位线前移了但台账没记 →
#    那一批币掉进宽限窗口里被重推;台账记了但水位线没动 → 下一轮白算一遍)。
def alpha_pushed_since(conn, after_ms: int) -> set[tuple[str, str]]:
    """
    台账里 listing_time_ms 严格大于阈值的那些键。

    ⚠️ 刻意按时间捞整段、而不是拿候选键逐个 IN 查:候选键在异常轮次可能有几百个
       (上游重写 listingTime 那种),拼一条几百个占位符的 SQL 只会更慢更脆;
       而这张表被清理策略钉死在宽限窗口内,整段捞出来也就几十行。
    """
    rows = conn.execute(
        "SELECT network_id, token_address FROM binance_alpha_pushed WHERE listing_time_ms > ?",
        (int(after_ms),),
    ).fetchall()
    return {(r["network_id"], r["token_address"]) for r in rows}


def record_alpha_pushed(conn, rows) -> None:
    """
    记若干行「这个币已经处理过」。rows: (network_id, token_address, symbol, listing_time_ms)。

    ⚠️ INSERT OR IGNORE:重复键不是错误 —— 冷启动播种与后续推送本来就会撞上同一个币。
    """
    payload = [(net, ca, sym, int(ms), now_iso()) for net, ca, sym, ms in rows]
    if not payload:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO binance_alpha_pushed
            (network_id, token_address, token_symbol, listing_time_ms, pushed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        payload,
    )


def prune_alpha_pushed(conn, upto_ms: int) -> int:
    """
    删掉 listing_time_ms **小于等于** 阈值的行,返回删除行数。

    ⚠️ 阈值传的是候选窗口下界(水位线 - 宽限窗口):等于下界的那个币已经不满足
       「严格大于下界」,永远不会再成为候选,留着它只是让表无限长大。
    ⚠️ 不分批:这张表稳态只有几十行(每天上新个位数 × 宽限窗口),
       与 token_price_history 那种百万行量级不是一回事,分批反而是过度设计。
    """
    cur = conn.execute(
        "DELETE FROM binance_alpha_pushed WHERE listing_time_ms <= ?", (int(upto_ms),)
    )
    return cur.rowcount


# ============================================================
# 【pump.fun】名单 / 持仓快照 / 已推台账
# ============================================================
# ⚠️ 与 alpha 那三个同一约定:本节函数**都不自己开事务**。
#    调用方要把「写快照 + 记台账 + 置播种位」放进同一个 tx,
#    拆开的话中途崩一次就会留下自相矛盾的状态(快照前移了但台账没记 → 重推;
#    播种位置了但快照没写 → 下一轮把全部持仓当成刚变动全推一遍)。
def add_pump_user(conn, user_id: str, username: str | None,
                  svm_wallet: str | None, evm_wallet: str | None) -> bool:
    """
    加人 / 复活一个软删除掉的人。返回 True = 这次真的是新加(或从删除态复活)。

    ⚠️ 复活时 **seeded 归零**:人被移出去这段时间他照样在交易,
       快照早就过期了。不归零的话复活那一轮会把这期间的全部变动一次推出来。
    ⚠️ **callout_seeded 同样归零**,同一条理由:人被移出去这段时间他照样在发观点,
       台账里那几条早被清理任务按新鲜窗口删掉了,不归零就会把这期间的观点整段补推。
    ⚠️ 钱包与用户名每次都覆盖写:用户名可改(last_username_update_timestamp),
       钱包也可能被 pump 换掉,库里存着旧值会让逐笔成交永远查不到人。
    """
    row = conn.execute(
        "SELECT active FROM pump_watch_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO pump_watch_users "
            "(user_id, username, svm_wallet, evm_wallet, added_at, active, seeded, "
            " callout_seeded) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
            (user_id, username, svm_wallet, evm_wallet, now_iso()),
        )
        return True
    was_active = row["active"] == 1
    conn.execute(
        "UPDATE pump_watch_users SET username = ?, svm_wallet = ?, evm_wallet = ?, "
        "active = 1, removed_at = NULL, "
        "seeded = CASE WHEN active = 1 THEN seeded ELSE 0 END, "
        "callout_seeded = CASE WHEN active = 1 THEN callout_seeded ELSE 0 END "
        "WHERE user_id = ?",
        (username, svm_wallet, evm_wallet, user_id),
    )
    return not was_active


def remove_pump_user(conn, user_id: str) -> bool:
    """
    软删除。返回 True = 这次真的关掉了一个开着的人。

    ⚠️ 软删除而不是 DELETE:快照与台账都以 user_id 为键,行留着,
       再 add 回来时**先归零 seeded 再重新播种**(见 add_pump_user)。
    """
    cur = conn.execute(
        "UPDATE pump_watch_users SET active = 0, removed_at = ? "
        "WHERE user_id = ? AND active = 1",
        (now_iso(), user_id),
    )
    return cur.rowcount == 1


def list_pump_users(conn, *, active_only: bool = True) -> list[sqlite3.Row]:
    """名单。默认只给还开着的 —— 软删除掉的人绝不能进巡检(那等于没删)。"""
    sql = "SELECT * FROM pump_watch_users"
    if active_only:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY added_at").fetchall()


def find_pump_user(conn, key: str):
    """
    按 userId / 用户名 / 任一钱包查一个人。/pump del 用它把用户输入落到 user_id。

    ⚠️ 用户名比对忽略大小写(库里存的是 pump 给的规范大小写,用户敲的未必一致);
       钱包不能一刀切 lower —— Solana 是 base58、大小写敏感,所以两种形态都比。
    """
    k = (key or "").strip().lstrip("@")
    if not k:
        return None
    return conn.execute(
        "SELECT * FROM pump_watch_users WHERE user_id = ? OR lower(username) = ? "
        "OR svm_wallet = ? OR lower(evm_wallet) = ? LIMIT 1",
        (k, k.lower(), k, k.lower()),
    ).fetchone()


def pump_positions(conn, user_id: str) -> dict[tuple[str, str], sqlite3.Row]:
    """这个人上一轮的持仓快照,键是 (chain_id, coin_mint)。"""
    rows = conn.execute(
        "SELECT * FROM pump_positions WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {(r["chain_id"], r["coin_mint"]): r for r in rows}


# ⚠️⚠️ 这里**曾经有一个 pump_mint_holders(conn, chain_id, coin_mint)**,
#    用「快照表里 amount_held > 0 的行」去数「名单内 N 人持有」。已删,不要再加回来。
#    删除的理由是它数出来的那个数**会多报,而多报是主动说假话**:
#    upsert_pump_positions 只 upsert、不删除本轮没出现的行,而 portfolio 只拉 page 0
#    的 50 行 —— 一个人在某个币上清了仓、那次清仓又恰好没被看见(进程停过,
#    或他持仓多到 50 行放不下,实测有人 1905 个持仓),他那行 amount_held 就永远
#    停在旧值,于是三个月前的快照会被当成"他现在还拿着"推出去。
#    现在的口径见 pumpfun.PumpWatcher._check 里的 `observed`:
#    **只数本轮真的在 page 0 里看到的人**,一个都不从库里补。
def upsert_pump_positions(conn, user_id: str, rows) -> None:
    """
    覆盖写若干行快照。rows: (chain_id, coin_mint, amount_held, realized_pnl_usd, updated_at)。

    ⚠️ 只 upsert、**不删除本轮没出现的行**:我们只拉 page 0(最近变动的那一页),
       没出现在这一页里的币不等于消失了,删掉它下一轮它再冒出来就成了"新持仓"。
    """
    payload = [(user_id, str(chain), mint, amt, pnl, upd, now_iso())
               for chain, mint, amt, pnl, upd in rows]
    if not payload:
        return
    conn.executemany(
        """
        INSERT INTO pump_positions
            (user_id, chain_id, coin_mint, amount_held, realized_pnl_usd, updated_at, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, chain_id, coin_mint) DO UPDATE SET
            amount_held = excluded.amount_held,
            realized_pnl_usd = excluded.realized_pnl_usd,
            updated_at = excluded.updated_at,
            snapshot_at = excluded.snapshot_at
        """,
        payload,
    )


def mark_pump_seeded(conn, user_id: str) -> None:
    """置播种位 —— 这个人的历史持仓已记为已知,从下一轮起他的变动才会被推。"""
    conn.execute("UPDATE pump_watch_users SET seeded = 1 WHERE user_id = ?", (user_id,))


def mark_pump_callout_seeded(conn, user_id: str) -> None:
    """
    置**观点**播种位 —— 这个人当前的历史观点已记为已知,从下一轮起新观点才会被推。

    ⚠️ 与 mark_pump_seeded 是两回事、两列,理由见建表注释(两个开关分别打开)。
    """
    conn.execute("UPDATE pump_watch_users SET callout_seeded = 1 WHERE user_id = ?",
                 (user_id,))


def pump_pushed_since(conn, after_iso: str) -> set[tuple[str, str, str, str]]:
    """
    台账里 traded_at 严格大于阈值的那些键 (user_id, coin_mint, tx, slot_index_id)。

    ⚠️ 与 alpha_pushed_since 同一条理由:按时间捞整段,不拿候选键逐个 IN 查 ——
       候选在异常轮次可能有几百个,拼几百个占位符只会更慢更脆;
       而这张表被清理策略钉死在新鲜窗口内,整段捞出来也就几十行。
    """
    rows = conn.execute(
        "SELECT user_id, coin_mint, tx, slot_index_id FROM pump_pushed_trades "
        "WHERE traded_at > ?",
        (after_iso,),
    ).fetchall()
    return {(r["user_id"], r["coin_mint"], r["tx"], r["slot_index_id"]) for r in rows}


def record_pump_pushed(conn, rows) -> None:
    """
    记若干行「这笔成交已经推过」。rows: (user_id, coin_mint, tx, slot_index_id, traded_at)。

    ⚠️ INSERT OR IGNORE:重复键不是错误,两轮之间的竞态撞上同一笔是正常的。
    """
    payload = [(uid, mint, tx, sid or "", ts, now_iso())
               for uid, mint, tx, sid, ts in rows]
    if not payload:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO pump_pushed_trades
            (user_id, coin_mint, tx, slot_index_id, traded_at, pushed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def prune_pump_pushed(conn, upto_iso: str) -> int:
    """
    删掉 traded_at **小于等于** 阈值的行,返回删除行数。

    ⚠️ 阈值传的是新鲜窗口下界:等于下界的那笔已经不满足「严格大于下界」,
       永远不会再成为候选,留着只是让表无限长大。
    """
    cur = conn.execute("DELETE FROM pump_pushed_trades WHERE traded_at <= ?", (upto_iso,))
    return cur.rowcount


def pump_callouts_pushed_since(conn, after_iso: str) -> set[str]:
    """
    台账里 created_at 严格大于阈值的那些 callout_id。

    ⚠️ 与 pump_pushed_since 同一条理由:按时间捞整段,不拿候选逐个 IN 查 ——
       这张表被清理策略钉死在新鲜窗口内,整段捞出来也就几行。
    """
    rows = conn.execute(
        "SELECT callout_id FROM pump_pushed_callouts WHERE created_at > ?",
        (after_iso,),
    ).fetchall()
    return {r["callout_id"] for r in rows}


def record_pump_callouts_pushed(conn, rows) -> None:
    """
    记若干行「这条观点已经推过」。rows: (callout_id, user_id, coin_mint, created_at)。

    ⚠️ INSERT OR IGNORE:重复键不是错误,两轮之间的竞态撞上同一条是正常的。
    """
    payload = [(cid, uid, mint, created, now_iso())
               for cid, uid, mint, created in rows]
    if not payload:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO pump_pushed_callouts
            (callout_id, user_id, coin_mint, created_at, pushed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        payload,
    )


def prune_pump_callouts_pushed(conn, upto_iso: str) -> int:
    """
    删掉 created_at **小于等于**阈值的行,返回删除行数。

    ⚠️ 阈值传的是新鲜窗口下界:等于下界的那条已经不满足「严格大于下界」,
       永远不会再成为候选,留着只是让表无限长大。
    """
    cur = conn.execute("DELETE FROM pump_pushed_callouts WHERE created_at <= ?", (upto_iso,))
    return cur.rowcount


def list_buyers(conn, network_id: str, token_address: str) -> list[sqlite3.Row]:
    """/who <CA> 用:列出名单里买过该币的人(按最早买入时间排序)"""
    return conn.execute(
        """
        SELECT w.handle, w.display_name, s.buy_count, s.first_buy_at
        FROM user_token_stats s
        JOIN watch_users w ON w.user_id = s.user_id
        WHERE w.active = 1 AND w.stats_ready = 1
          AND s.network_id = ? AND s.token_address = ? AND s.buy_count > 0
        ORDER BY COALESCE(s.first_buy_at, '9999') ASC
        """,
        (network_id, token_address),
    ).fetchall()
