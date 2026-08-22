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

from src.models import COUNTABLE_REASONS

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
        peak = r["peak_mc"] or now
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
    out.sort(key=lambda d: -d["median_peak"])
    return out
