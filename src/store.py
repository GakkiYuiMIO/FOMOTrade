"""
SQLite 持久化 —— 监控名单 / 事件流水 / 游标 / 用户×代币聚合状态

- 用 sqlite3 stdlib,不引入 ORM
- 单文件 data/fomo.db(与 claudeTrade 的 trade.db 完全隔离:
  两个独立进程共用一个 SQLite 文件会抢写锁)
- 功能 A(首次买入判定)与功能 B(共识计数)的全部判定逻辑都在本文件

⚠️ 本文件是 poller / bot 的唯一数据入口。改这里的函数签名前先 grep 调用点。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from loguru import logger

from src.config import DATA_DIR
from src.models import (
    BADGE_ADD,
    BADGE_FIRST,
    COUNTABLE_REASONS,
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
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_users_active ON watch_users(active);
CREATE INDEX IF NOT EXISTS idx_watch_users_handle ON watch_users(handle);

-- ============ 事件流水(全量落库,不做任何过滤) ============
CREATE TABLE IF NOT EXISTS fomo_events (
    event_id      TEXT PRIMARY KEY,   -- 去重键,见 models.make_event_id
    event_type    TEXT NOT NULL,      -- BUY / SELL / THESIS / TRANSFER_IN / TRANSFER_OUT
    user_id       TEXT NOT NULL,
    handle        TEXT,
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


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """建表,幂等"""
    if conn is not None:
        conn.executescript(_SCHEMA)
        return
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as c:
        c.executescript(_SCHEMA)
    logger.info("数据库已就绪: {}", DB_PATH)


# ============================================================
# 监控名单
# ============================================================
def normalize_handle(raw: str) -> str:
    """
    handle 输入归一化:去空白、去前导 @、转小写。

    ⚠️ 不归一化的话,同一个人用 @Maxpain / maxpain 各 /add 一次会产生两行,
       共识计数把一个人算两次。
    """
    return (raw or "").strip().lstrip("@").strip().lower()


def get_watch_user(conn, user_id: str):
    return conn.execute(
        "SELECT * FROM watch_users WHERE user_id = ?", (user_id,)
    ).fetchone()


def find_user_by_handle(conn, handle: str):
    return conn.execute(
        "SELECT * FROM watch_users WHERE handle = ?", (normalize_handle(handle),)
    ).fetchone()


def list_active_users(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM watch_users WHERE active = 1 ORDER BY added_at"
    ).fetchall()


def ready_user_ids(conn) -> list[str]:
    """既 active 又已建好基线的用户 —— 共识计数的分母口径,分子必须用同一个谓词"""
    rows = conn.execute(
        "SELECT user_id FROM watch_users WHERE active = 1 AND stats_ready = 1"
    ).fetchall()
    return [r["user_id"] for r in rows]


def add_watch_user(conn, user_id: str, handle: str, display_name: str | None) -> tuple[bool, str]:
    """
    加入监控名单。返回 (是否需要建基线, 给用户的回执文案)。

    ⚠️ 幂等:已 active 且基线就绪的用户重复 /add 只回"已在监控中",绝不重置 stats_ready ——
       重置会让他退回不打徽章的状态,白白损失已建好的基线。
    ⚠️ /del 后回归必须重建基线:空窗期的买入本地无记录,
       不重建会让空窗期建的仓位在下次加仓时被误标 🌱。
    """
    h = normalize_handle(handle)
    existing = get_watch_user(conn, user_id)
    if existing and existing["active"] and existing["stats_ready"]:
        return False, f"ℹ️ {display_name or h} 已在监控中"

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
            (event_id, event_type, user_id, handle, network_id, token_address, token_symbol,
             amount_usd, token_amount, price_usd, tx_hash, event_ts, ingested_at,
             badge, badge_reason, raw_json)
        VALUES (:event_id, :event_type, :user_id, :handle, :network_id, :token_address,
                :token_symbol, :amount_usd, :token_amount, :price_usd, :tx_hash,
                :event_ts, :ingested_at, :badge, :badge_reason, :raw_json)
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
