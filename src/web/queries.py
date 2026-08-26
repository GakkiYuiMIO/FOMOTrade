"""
网页版的全部 SQL 聚合。

⚠️ 本模块**不产生任何 HTML**,只进连接、出 dict/list。渲染是 render.py 的事。
   这条边界让口径能脱离 HTTP 单测 —— 而口径正是算错了也看不出来的地方。
⚠️ 本模块**绝不 SELECT raw_json**。它占了库体积的 52%(13.8MB/26.4MB),
   页面拿它只会变成瓶颈。
"""
from __future__ import annotations

import sqlite3
import statistics as st
from datetime import UTC, datetime

from src import store as _store
from src.models import (
    COUNTABLE_REASONS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    iso_minutes_ago,
)

# 少于这么多个币就不给排名。⚠️ 不是不显示,是不参与排序 ——
# 3 个币的「胜率 100%」是噪声,放进榜单顶端会直接误导决策。
MIN_TOKENS_FOR_RANK = 8


def follow_value(conn: sqlite3.Connection,
                 min_tokens: int = MIN_TOKENS_FOR_RANK) -> list[dict]:
    """
    跟单价值:**跟着这个人买,能拿到什么**。

    ⚠️ 这不是「这个人赚了多少」。有人自己很赚但进场早、跑得快,跟着他反而接盘。
       用户看 PNL 的目的是「判断单个信号值不值得跟」,所以口径必须是跟随者视角。

    每个 (人, 币) 只取**第一次**买入:
      entry = 那条事件的 market_cap(**买入当时**的市值)
      now   = token_snapshot.market_cap(当前)
      peak  = token_snapshot.max_market_cap(峰值)

    ⚠️ entry 绝不能用 token_snapshot 回填 —— 那是「现在」的市值,
       混用会把当时市值变成现在市值,凭空造出纸面盈利。

    ⚠️ 只筛 active=1 AND stats_ready=1(与 count_recent_buyers 同一套谓词),
       否则榜单和推送里的数字对不上。

    ⚠️ 必须额外按 COALESCE(badge_reason,'') IN COUNTABLE_REASONS 过滤,**且是刻意的**:
       它排除的是买入计价币(USDC/WSOL/WETH/WBNB 等,badge_reason=quote_token)以及
       基线未就绪时记的行(no_baseline)。买 USDC 不是一个可跟的信号,
       而且计价币市值几乎不动,混进来会把每个人的中位数都往 1.0x 拽 ——
       实测过带这道过滤 vs 不带:带了之后峰值中位的区分度是 1.125~2.088,
       不带则塌缩到 1.050~1.843,前者才是真实信号。
       这条谓词也让本表的口径与 count_recent_buyers 保持一致,
       否则网页上的数字会和 Telegram 推送的对不上。
    """
    marks = ",".join("?" * len(COUNTABLE_REASONS))
    rows = conn.execute(
        f"""
        WITH first_buy AS (
            SELECT e.user_id, e.network_id, e.token_address, MIN(e.event_ts) AS ts
            FROM fomo_events e
            JOIN watch_users w ON w.user_id = e.user_id
                              AND w.active = 1 AND w.stats_ready = 1
            WHERE e.event_type = 'BUY'
              AND e.market_cap IS NOT NULL
              AND COALESCE(e.badge_reason, '') IN ({marks})
            GROUP BY e.user_id, e.network_id, e.token_address
        )
        SELECT w.user_id, w.handle, w.display_name, w.starred,
               (SELECT x.market_cap FROM fomo_events x
                 WHERE x.user_id = f.user_id AND x.network_id = f.network_id
                   AND x.token_address = f.token_address AND x.event_ts = f.ts
                   AND x.market_cap IS NOT NULL
                 LIMIT 1) AS entry,
               s.market_cap AS now_mc, s.max_market_cap AS peak_mc
        FROM first_buy f
        JOIN watch_users w ON w.user_id = f.user_id
        JOIN token_snapshot s ON s.network_id = f.network_id
                             AND s.token_address = f.token_address
        """,  # noqa: S608
        COUNTABLE_REASONS,
    ).fetchall()

    per: dict[str, dict] = {}
    for r in rows:
        entry, now = r["entry"], r["now_mc"]
        # ⚠️ entry 为 0 或 None 都要跳过 —— 按 0 算会造出无穷大倍数
        if not entry or now is None:
            continue
        # ⚠️ 判空必须用 is None,不能用真值判断 —— peak_mc 为 0 是脏数据但仍是
        #    「有取到值」的真实值,`or` 会把它和 NULL(没取到值)混为一谈,
        #    悄悄回落成 now 从而抹掉这条脏数据本该暴露出来的异常
        peak = r["peak_mc"] if r["peak_mc"] is not None else now
        p = per.setdefault(r["user_id"], {
            "user_id": r["user_id"],
            "handle": r["handle"],
            "display_name": r["display_name"],
            "starred": bool(r["starred"]),
            "_now": [], "_peak": [],
        })
        p["_now"].append(now / entry)
        p["_peak"].append(peak / entry)

    out = []
    for p in per.values():
        n = len(p["_now"])
        if n < min_tokens:
            continue
        out.append({
            "user_id": p["user_id"],
            "handle": p["handle"],
            "display_name": p["display_name"],
            "starred": p["starred"],
            "tokens": n,
            "median_now": st.median(p["_now"]),
            "median_peak": st.median(p["_peak"]),
            "win_rate": sum(1 for x in p["_now"] if x > 1.0) / n,
        })

    # ⚠️ user_pnl_snapshot 由 store.init_db() 建表,只在 --run 才会真正建出来
    #    (--web 故意不建表)。表还不存在时整块跳过,让这两列保持 None,
    #    而不是让 people 页直接崩掉。
    try:
        pnl = {r["user_id"]: r for r in conn.execute(
            "SELECT user_id, total_pnl, pnl_7d FROM user_pnl_snapshot")}
    except sqlite3.OperationalError as e:
        # ⚠️ 只吞「表还不存在」这一种 —— 新表要等 --run 建库才出现,
        #    而 --web 是只读进程不建库。其它 OperationalError(锁、磁盘、列名写错)
        #    必须原样抛出,否则真 bug 会被永久藏起来。
        if "no such table" not in str(e):
            raise
        pnl = {}
    for d in out:
        p = pnl.get(d["user_id"])
        # ⚠️ 拿不到就是 None,不要填 0 —— 0 是「不赚不亏」,会让人排到中间去
        d["total_pnl"] = p["total_pnl"] if p else None
        d["pnl_7d"] = p["pnl_7d"] if p else None

    out.sort(key=lambda d: -d["median_peak"])
    return out


def copy_ledger_full(conn: sqlite3.Connection) -> list[dict]:
    """
    跟单台账**全量**,不截断。

    ⚠️ TG 的 /paper 截到 15 条是聊天流的限制;网页没有这个限制,
       而「整体多少倍」这个结论只有看到全部才得得出来。
    """
    rows = conn.execute(
        """
        SELECT g.*, s.market_cap AS now_mc, s.max_market_cap AS peak_mc,
               s.updated_at AS mcap_at
        FROM copytrade_signals g
        LEFT JOIN token_snapshot s
               ON s.network_id = g.network_id AND s.token_address = g.token_address
        ORDER BY g.triggered_at DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        entry, now = r["entry_mcap"], r["now_mc"]
        # ⚠️ 缺任何一个都置 None,不要填 0 —— 0 会被读成「归零了」
        d["multiple"] = (now / entry) if (entry and now is not None) else None
        d["value"] = (r["amount_usd"] * d["multiple"]) if d["multiple"] is not None else None
        out.append(d)
    return out


def copy_summary(conn: sqlite3.Connection) -> dict:
    """跟单整体成绩。⚠️ 合计只统计**算得出价**的单子"""
    rows = copy_ledger_full(conn)
    priced = [r for r in rows if r["multiple"] is not None]
    invested = sum(r["amount_usd"] for r in priced)
    value = sum(r["value"] for r in priced)
    return {
        "rows": rows,
        "count": len(rows),
        "priced": len(priced),
        "invested": invested,
        "value": value,
        "multiple": (value / invested) if invested else None,
        "winners": sum(1 for r in priced if r["multiple"] > 1.0),
    }


def dashboard(conn: sqlite3.Connection) -> dict:
    """首页几个大数字"""
    last = _store.get_state(conn, "last_tick_at")
    age = None
    if last:
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - dt).total_seconds()
        except ValueError:
            age = None

    today = _store.now_iso()[:10] + "T00:00:00+00:00"
    # ⚠️ 必须排掉转入/转出。「今日事件」这张卡片的含义一直是"名单今天动了多少次"
    #    (买、卖、发观点),而转账是 2026-08 新加的采集,实测把它算进来会让这个数
    #    虚增约 10 倍(注入 60 条 TRANSFER_IN 实测 136 → 196)—— 卡片还是那张卡片,
    #    数字却换了含义,用户会以为名单突然活跃了十倍。
    #    转账有它自己的出口(筹码分发告警),不该在这里充数。
    n_events = conn.execute(
        "SELECT COUNT(*) n FROM fomo_events "
        "WHERE event_ts >= ? AND event_type NOT IN (?, ?)",
        (today, EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT),
    ).fetchone()["n"]
    n_tokens = conn.execute(
        "SELECT COUNT(DISTINCT token_address) n FROM fomo_events "
        "WHERE event_type = 'BUY' AND event_ts >= ?", (today,)
    ).fetchone()["n"]
    n_users = conn.execute(
        "SELECT COUNT(*) n FROM watch_users WHERE active = 1"
    ).fetchone()["n"]

    return {
        # ⚠️ None 表示「从来没跑过」,与 0(刚跑过)是完全不同的两件事
        "last_tick_age_sec": age,
        "events_today": n_events,
        "tokens_today": n_tokens,
        "watch_count": n_users,
        "copy": copy_summary(conn),
    }


# ============================================================
# 信号卡片流(首页)
# ============================================================
# ⚠️ 这两组范围是查询串校验的唯一事实源 —— pages.py 解析 ?min_buyers=/?hours=
#    时夹的也是这两个区间,不在两处各写一份数字。
FEED_MIN_BUYERS_DEFAULT = 2
FEED_MIN_BUYERS_RANGE = (1, 50)
# 单位:分钟。1 小时 ~ 14 天 —— 再短没有聚合意义,再长会把 fomo_events 全表扫一遍。
FEED_WINDOW_MIN_DEFAULT = 60 * 24
FEED_WINDOW_MIN_RANGE = (60, 60 * 24 * 14)

# 卡片流硬上限,**不接受查询串输入** —— 只作为「有人手改 min_buyers=1 导致
# 189 个币全进来」时的保底,不是用户可调参数。
_FEED_ROW_LIMIT = 300
# 每张卡「谁买的」最多展示几个人,超出的在 pages.py 那边折成「等 N 人」
_FEED_BUYER_SAMPLE = 8


def signal_feed(conn: sqlite3.Connection, *,
                min_buyers: int = FEED_MIN_BUYERS_DEFAULT,
                window_min: int = FEED_WINDOW_MIN_DEFAULT) -> list[dict]:
    """
    信号卡片流:按**币**聚合的「名单集体买入」信号,首页的数据源。

    ⚠️ 必须按币聚合,绝不能一笔买入出一张卡 —— 同一个人分批加仓会被拆成
       好几张长得一样的卡,而且信息流会被刷屏(实测近 24h:796 笔买入,
       去重到币只有 189 个)。「N 人买入」的 N 是 COUNT(DISTINCT user_id),
       不是笔数。

    ⚠️ 入场市值取窗口内**最早一笔有市值**的买入(与 hot_tokens / follow_value
       同一惯例:ROW_NUMBER 按「有没有市值」再按时间排序取第一名),
       绝不能拿 token_snapshot 回填 —— 那是「现在」的市值,
       回填等于凭空造出纸面盈利(见模块顶部铁律)。

    ⚠️ 谓词必须与 count_recent_buyers 完全一致(active=1 AND stats_ready=1 +
       COALESCE(badge_reason,'') IN COUNTABLE_REASONS),否则卡片上的
       「N 人买入」会和 Telegram 推送、/hot 页的共识数对不上。

    ⚠️ min_buyers / window_min 来自查询串,pages.py 已经夹过一次范围;
       这里再夹一遍是防御性的第二道线 —— 防止以后哪个新调用方漏掉那道校验,
       让一个荒谬的值变成荒谬的 SQL 时间窗口。
    """
    min_buyers = max(FEED_MIN_BUYERS_RANGE[0], min(FEED_MIN_BUYERS_RANGE[1], int(min_buyers)))
    window_min = max(FEED_WINDOW_MIN_RANGE[0], min(FEED_WINDOW_MIN_RANGE[1], int(window_min)))
    since_iso = iso_minutes_ago(window_min)

    marks = ",".join("?" * len(COUNTABLE_REASONS))
    rows = conn.execute(
        f"""
        WITH scoped AS (
            SELECT e.network_id, e.token_address, e.token_symbol, e.user_id,
                   e.amount_usd, e.market_cap, e.event_ts, e.token_created_at,
                   -- 该币最早**且有市值**的那一行:没市值的排到分区末尾。
                   -- 与 hot_tokens 完全同一套写法,道理也一样:market_cap 只来自
                   -- balances,可能晚于最早那笔 swap 出现。
                   ROW_NUMBER() OVER (
                       PARTITION BY e.network_id, e.token_address
                       ORDER BY CASE WHEN e.market_cap IS NULL THEN 1 ELSE 0 END, e.event_ts
                   ) AS rn_mcap
            FROM fomo_events e
            JOIN watch_users w ON w.user_id = e.user_id
                              AND w.active = 1 AND w.stats_ready = 1
            WHERE e.event_type = 'BUY'
              AND e.event_ts >= ?
              AND e.token_address IS NOT NULL
              AND COALESCE(e.badge_reason, '') IN ({marks})
        ),
        agg AS (
            SELECT network_id, token_address,
                   MAX(token_symbol)       AS symbol,
                   MAX(token_created_at)   AS token_created_at,
                   COUNT(DISTINCT user_id) AS buyers,
                   COUNT(*)                AS buys,
                   -- ⚠️ 不用 COALESCE(amount_usd,0):SUM/COUNT 对 NULL 的标准语义
                   --    (自动跳过 NULL,不吞真实的 0)刚好就是判空铁律要的效果 ——
                   --    一笔都拿不到金额时 total_usd 是 NULL(卡片上那格空着),
                   --    不会显示成一个假的「$0.00 合计」。
                   SUM(amount_usd)         AS total_usd,
                   COUNT(amount_usd)       AS priced_buys
            FROM scoped
            GROUP BY network_id, token_address
        )
        SELECT a.*,
               m.market_cap AS entry_mcap,
               s.market_cap AS now_mcap,
               -- 峰值缺失时退回现价(老库 max_market_cap 可能还没回填),与
               -- hot_tokens 的 COALESCE(s.max_market_cap, s.market_cap) 同一惯例
               COALESCE(s.max_market_cap, s.market_cap) AS peak_mcap,
               s.updated_at AS mcap_at
        FROM agg a
        LEFT JOIN scoped m ON m.network_id = a.network_id
                          AND m.token_address = a.token_address AND m.rn_mcap = 1
        LEFT JOIN token_snapshot s ON s.network_id = a.network_id
                                  AND s.token_address = a.token_address
        WHERE a.buyers >= ?
        ORDER BY a.buyers DESC, a.total_usd DESC, a.token_address
        LIMIT ?
        """,  # noqa: S608
        (since_iso, *COUNTABLE_REASONS, min_buyers, _FEED_ROW_LIMIT),
    ).fetchall()

    # ⚠️ 实测踩过的坑:token_price_history 是本分支新加的表,只在 store.init_db()
    #    的建表/迁移里创建 —— 而 --web 是只读进程故意不建表(与 user_pnl_snapshot
    #    同理)。正在跑的监控进程只要还没重启过一次,这张表在真实库里就是
    #    「压根不存在」而不是「存在但是空的」,直接查会是 OperationalError,
    #    不是空列表。这里只探测一次,不在循环里对每个币都 try/except 一遍。
    has_price_history = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_price_history'"
    ).fetchone() is not None

    out = []
    for r in rows:
        d = dict(r)
        entry, now = d["entry_mcap"], d["now_mcap"]
        # ⚠️ entry 为 0 或 None 都要跳过(按 0 算会造出无穷大倍数),
        #    now 判空必须用 is None —— 0 是「归零了」的真实值,不是缺失
        d["multiple"] = (now / entry) if (entry and now is not None) else None
        d["avg_usd"] = (d["total_usd"] / d["priced_buys"]) if d["priced_buys"] else None
        # ⚠️ 谁买的:复用 store.token_buyers(与本函数同一套谓词、已单测覆盖),
        #    不在这里重新拼一遍「按人去重 + 取 handle」的逻辑
        d["buyer_handles"] = [b["who"] for b in _store.token_buyers(
            conn, d["network_id"], d["token_address"], since_iso, limit=_FEED_BUYER_SAMPLE)]
        # sparkline 用的价格历史。⚠️ 刚上线的表,重启 + 跑够采样间隔前基本是空的 ——
        #    render.sparkline 负责把 <2 个点降级成提示文案,这里不做任何特殊处理;
        #    表还不存在时直接给空列表,走的是同一条降级路径。
        d["price_points"] = [p["market_cap"] for p in _store.load_price_history(
            conn, d["network_id"], d["token_address"], since_iso)] if has_price_history else []
        out.append(d)
    return out
