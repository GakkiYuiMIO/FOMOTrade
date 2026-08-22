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
    REASON_LOCAL_STATS,
    REASON_NO_BASELINE,
    REASON_NO_SIDE,
    REASON_NO_TOKEN_KEY,
    REASON_NOT_BUY,
    REASON_QUOTE_TOKEN,
    FomoEvent,
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
                     ("token_created_at", "INTEGER")):
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
    """
    row = get_watch_user(conn, handle_or_id) or find_user_by_handle(conn, handle_or_id)
    if not row or not row["active"]:
        return False, f"⚠️ 未在监控名单中: {handle_or_id}"
    with tx(conn):
        conn.execute(
            "UPDATE watch_users SET active = 0, removed_at = ? WHERE user_id = ?",
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
             tx_hash, event_ts, ingested_at, badge, badge_reason, raw_json)
        VALUES (:event_id, :event_type, :user_id, :handle, :user_handle, :network_id,
                :token_address, :token_symbol, :amount_usd, :token_amount, :price_usd,
                :market_cap, :token_created_at, :tx_hash, :event_ts, :ingested_at,
                :badge, :badge_reason, :raw_json)
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
    """
    if ev.event_type != EVENT_BUY:
        return None, REASON_NOT_BUY
    if ev.side_unknown:                      # 方向不明:既不写 stats 也不显示共识
        return None, REASON_NO_SIDE
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
