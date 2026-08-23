"""
五个页面的渲染。每个函数进只读连接(+ 查询串参数),出完整 HTML。

⚠️ 页面只调 queries.*/render.* 取数出串,自己不写 SQL 也不拼 <html>——
   唯一例外是 hot() 直接调 store.hot_tokens:那本来就是聚合好的只读函数,
   不是绕过 queries.py 现拼的 SQL,queries.py 里没有它是因为不需要再包一层。
⚠️ 每个路由函数统一签名 (conn, query),query 是 urllib.parse.parse_qs 的原样输出
   (dict[str, list[str]])。只有 feed() 真的读它 —— 其余四个页面没有可调参数,
   加这个形参只是为了让 server.ROUTES 能一视同仁地调用,不必为首页单独分支。
"""
from __future__ import annotations

import sqlite3

from src import store
from src.models import NETWORK_DISPLAY
from src.web import queries as q
from src.web.render import (
    age_minutes,
    ath_bar,
    esc,
    mcap,
    money,
    mult,
    page,
    pct,
    sparkline,
    stale_mark,
    token_age,
    token_label,
)

_SNAPSHOT_NOTE = ('<p class=note>所有数字按<b>当前行情快照</b>计算。'
                  '实测相邻两轮之间整体倍数会有百分之几的波动。</p>')

# 台账页「新鲜/冻结」拆分用的阈值(分钟)。
# ⚠️ 故意不复用 render.STALE_MIN —— 之前这里写过"那个常量同时是
#    bot._STALE_MCAP_MIN,是买入安全线的实时判据",经核实这个说法是错的:
#    bot._STALE_MCAP_MIN 只喂给 bot._stale_mark(),而后者只在 /paper 这条
#    Telegram 命令里给台账现价加"⚠️ 行情停在 Xh 前"的展示标注(bot.py:986),
#    不参与任何买入判断。真正的买入闸门是 copytrade.CopyConfig.max_entry_mcap,
#    拿当次实时抓到的 entry_mcap 比较(copytrade.py:174-179);买入时机的安全阀
#    是 copyworker.MAX_STALE_SEC(排队等执行的时效上限,copyworker.py:43)——
#    这两个都和 _STALE_MCAP_MIN 无关。
#    不复用的决定本身没错,只是理由要换一个:render.STALE_MIN 回答的是
#    "这条现价展示该不该标 stale",LEDGER_FREEZE_MIN 回答的是"这条该不该被
#    计入冻结子集统计口径",是两个独立动机的阈值,只是当前恰好都取 60 分钟。
#    共用同一个数字的话,以后谁为了调其中一个用途改了这个值,
#    另一处的口径会被无声地一起带偏。
#    阈值本身对结果的影响是实质性的,不是装饰:实测 60min → 29/28 条、
#    0.71x/0.65x;24h → 34/23 条、0.78x/0.59x。
LEDGER_FREEZE_MIN = 60


def _card(k: str, v: str) -> str:
    return f'<div class=card><div class=v>{v}</div><div class=k>{esc(k)}</div></div>'


def dashboard(conn: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    d = q.dashboard(conn)
    c = d["copy"]
    age = d["last_tick_age_sec"]
    # ⚠️ None = 从来没跑过,与 0 = 刚跑过是两回事
    health = "从未运行" if age is None else (
        f"{int(age)}s 前" if age < 300 else f"⚠️ {int(age / 60)} 分钟前")
    body = (
        "<h1>看板</h1><div class=cards>"
        + _card("今日事件", str(d["events_today"]))
        + _card("今日在买的币", str(d["tokens_today"]))
        + _card("名单人数", str(d["watch_count"]))
        + _card("最后一轮", esc(health))
        + _card("跟单整体", mult(c["multiple"]) or "—")
        + _card("跟单赚钱单数", f'{c["winners"]}/{c["priced"]}')
        + "</div>"
        + f'<p class=note>跟单台账共 {c["count"]} 条,其中 {c["priced"]} 条能算出现价。'
          f'投入 {money(c["invested"])} → 现值 {money(c["value"])}。'
          f'<a href="/copy">看全部</a></p>'
        + _SNAPSHOT_NOTE
    )
    return page("看板", body, active="/board")


def people(conn: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    rows = q.follow_value(conn)
    # ⚠️「他自己 7d / 生涯」两列与前面「跟单价值」三列是两个完全不同的问题:
    #    前者是「他自己赚了多少」,后者是「跟着他买我能拿到什么」——
    #    本期数据里两者相关性很弱、时常反向,不能让读者以为前者能推出后者。
    #    用一条竖线把两组视觉上分开,不新增 CSS 框架。
    sep = ' style="border-left:2px solid var(--line)"'
    head = ("<tr><th>名单成员</th><th>币数</th><th>峰值中位</th>"
            "<th>现价中位</th><th>胜率</th>"
            f"<th{sep}>他自己 7d</th><th>他自己生涯</th></tr>")
    trs = []
    for r in rows:
        name = r["display_name"] or r["handle"] or r["user_id"][:8]
        star = "⭐ " if r["starred"] else ""
        cls = "up" if r["median_now"] > 1 else "down"
        trs.append(
            f'<tr><td>{star}{esc(name)}</td>'
            f'<td class=n>{r["tokens"]}</td>'
            f'<td class=n>{mult(r["median_peak"])}</td>'
            f'<td class="n {cls}">{mult(r["median_now"])}</td>'
            f'<td class=n>{pct(r["win_rate"])}</td>'
            f'<td class=n{sep}>{money(r.get("pnl_7d"))}</td>'
            f'<td class=n>{money(r.get("total_pnl"))}</td></tr>'
        )
    empty = "<p class=note>还没有足够的数据。</p>" if not trs else ""
    body = (
        "<h1>跟单价值</h1>"
        "<p class=note>口径是<b>「跟着这个人买能拿到什么」</b>,不是他自己赚了多少。"
        f"每个币只取他第一次买入时的市值作成本。样本少于 {q.MIN_TOKENS_FOR_RANK} 个币的不进榜。</p>"
        + (f'<div class=scroll><table>{head}{"".join(trs)}</table></div>' if trs else empty)
        + "<p class=note><b>峰值中位</b>是「最好的时候能到多少」,"
          "<b>现价中位</b>是「拿到现在还剩多少」。两列一起看才有意义 —— "
          "差别往往不在选币,在卖点。</p>"
        + "<p class=note>竖线右侧的<b>「他自己 7d / 生涯」</b>是这个人自己的盈亏,"
          "和左边「跟单价值」三列回答的是两个问题,不能互相推导 —— "
          "有人自己很赚但进场早、跑得快,跟着他反而接盘。数据缺失时这两格留空。</p>"
        + _SNAPSHOT_NOTE
    )
    return page("跟单价值", body, active="/people")


def hot(conn: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    from src.models import iso_minutes_ago

    rows = store.hot_tokens(conn, iso_minutes_ago(60 * 24))
    head = ("<tr><th>代币</th><th>买家</th><th>入场市值</th>"
            "<th>峰值</th><th>现在</th><th>倍数</th><th></th></tr>")
    trs = []
    for r in rows[:50]:
        d = dict(r)
        # ⚠️ hot_tokens 返回的列叫 symbol,不是 token_symbol(已实测确认)
        trs.append(
            f'<tr><td>{esc(token_label(d.get("symbol"), d["token_address"]))}</td>'
            f'<td class=n>{d.get("buyers", "")}</td>'
            f'<td class=n>{mcap(d.get("first_mcap"))}</td>'
            f'<td class=n>{mcap(d.get("peak_mcap"))}</td>'
            f'<td class=n>{mcap(d.get("now_mcap"))}</td>'
            f'<td class=n>{mult(d.get("mult"))}</td>'
            f'<td class=dim>{esc(stale_mark(d.get("mcap_at")))}</td></tr>'
        )
    empty = "<p class=note>近 24 小时名单还没有买入。</p>" if not trs else ""
    body = ("<h1>热门币 · 近 24 小时</h1>"
            + (f'<div class=scroll><table>{head}{"".join(trs)}</table></div>' if trs else empty)
            + _SNAPSHOT_NOTE)
    return page("热门币", body, active="/hot")


def _sub_multiple(rows: list[dict]) -> float | None:
    """子集的「投入 → 现值」合计倍数,复用 copy_summary 同一套算法(现值 ÷ 投入)"""
    invested = sum(r["amount_usd"] for r in rows)
    value = sum(r["value"] for r in rows)
    return (value / invested) if invested else None


def _is_frozen(updated_at: str | None) -> bool:
    """
    行情快照是否已经停更超过 LEDGER_FREEZE_MIN 分钟。

    ⚠️ 阈值判断独立于 render.stale_mark() —— 后者比较的是 render.STALE_MIN,
       与本页的分类阈值 LEDGER_FREEZE_MIN 概念上是两件事(见上面常量的注释),
       即便当前取值恰好相同也不该共用同一个阈值。两者共用的只是
       render.age_minutes() 这段解析逻辑本身,不是阈值比较。
       缺失 / 解析不了的时间戳都算冻结,不能当新鲜处理。
    """
    mins = age_minutes(updated_at)
    return mins is None or mins > LEDGER_FREEZE_MIN


def copy_ledger(conn: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    c = q.copy_summary(conn)
    priced = [r for r in c["rows"] if r["multiple"] is not None]
    # ⚠️ 实测(Task 3):token_snapshot 只覆盖「名单里还有人持有」的币,清仓后就停更 ——
    #    56 条能算出价的里有 23 条(41%)是三天前冻住的现价,却照样参与整体倍数。
    #    新鲜 0.681x、冻结 0.595x,合起来的 0.70x 把差别抹平了。
    #    这里用 LEDGER_FREEZE_MIN 把两类分开报,不能只给一个合计数。
    fresh_rows = [r for r in priced if not _is_frozen(r["mcap_at"])]
    frozen_rows = [r for r in priced if _is_frozen(r["mcap_at"])]
    fresh_mult = _sub_multiple(fresh_rows)
    frozen_mult = _sub_multiple(frozen_rows)

    head = ("<tr><th>代币</th><th>状态</th><th>触发人数</th><th>入场市值</th>"
            "<th>投入</th><th>现值</th><th>倍数</th><th></th></tr>")
    trs = []
    for r in c["rows"]:
        m = r["multiple"]
        cls = "" if m is None else ("up" if m > 1 else "down")
        trs.append(
            f'<tr><td>{esc(token_label(r["token_symbol"], r["token_address"]))}</td>'
            f'<td class=dim>{esc(r["status"])}</td>'
            f'<td class=n>{r["trigger_buyers"]}</td>'
            f'<td class=n>{mcap(r["entry_mcap"])}</td>'
            f'<td class=n>{money(r["amount_usd"])}</td>'
            f'<td class=n>{money(r["value"])}</td>'
            f'<td class="n {cls}">{mult(m)}</td>'
            # ⚠️ 每行标 stale(展示用 render.stale_mark,阈值 render.STALE_MIN)。
            #    与上面冻结子集的判据(LEDGER_FREEZE_MIN)是两个独立的常量,
            #    当前取值恰好都是 60 分钟,所以行内的"⚠️ 行情停在 Xh 前"
            #    目前能对上冻结子集,但这只是巧合,不是绑定关系。
            f'<td class=dim>{esc(stale_mark(r["mcap_at"]))}</td></tr>'
        )
    empty = "<p class=note>还没有跟单信号。</p>" if not trs else ""
    body = (
        f'<h1>我的跟单 · 全部 {c["count"]} 条</h1>'
        + "<div class=cards>"
        + _card("整体倍数", mult(c["multiple"]) or "—")
        + _card(f"新鲜倍数({len(fresh_rows)}条)", mult(fresh_mult) or "—")
        + _card(f"冻结倍数({len(frozen_rows)}条)", mult(frozen_mult) or "—")
        + _card("投入", money(c["invested"]))
        + _card("现值", money(c["value"]))
        + _card("赚钱单数", f'{c["winners"]}/{c["priced"]}')
        + "</div>"
        + (f'<div class=scroll><table>{head}{"".join(trs)}</table></div>' if trs else empty)
        + (f'<p class=note>⚠️ 超过 {LEDGER_FREEZE_MIN} 分钟没更新行情的算「冻结」:'
           '轮询默认每 15 秒刷新一次持有中的币的现价,所以停更基本等于'
           '「名单里已经没人持有这个币了」。它拉低还是拉高整体倍数,'
           '不看拆分看不出来 —— 合并成一个数会把这个信息抹掉。</p>')
        + '<p class=note>⚠️ 纸上盈亏按市值比折算,<b>没算手续费、滑点、gas</b>,真实结果只会更差。</p>'
        + _SNAPSHOT_NOTE
    )
    return page("我的跟单", body, active="/copy")


# ============================================================
# 信号卡片流(新首页)
# ============================================================
# 筛选条的可选项。⚠️ 只是链接文案,不限制用户能不能在地址栏手打别的数字 ——
# 真正的边界是 queries.FEED_MIN_BUYERS_RANGE / FEED_WINDOW_MIN_RANGE。
_BUYER_OPTIONS = (1, 2, 3, 5, 10)
_HOUR_OPTIONS = (1, 6, 24, 72, 168)
# 小时是给人看的查询串单位,换算成分钟后仍落在 queries.py 的同一个区间里 ——
# 除出来的上下界当作事实源,不在这里另写一遍数字。
_HOUR_RANGE = (max(1, q.FEED_WINDOW_MIN_RANGE[0] // 60), q.FEED_WINDOW_MIN_RANGE[1] // 60)


def _clamp_int(raw: list[str] | None, default: int, lo: int, hi: int) -> int:
    """
    从查询串里夹一个整数。⚠️ 这是本页唯一接收不可信输入的地方 ——
    读者能直接在地址栏改数字,不夹的话一个荒谬的 min_buyers 或 hours
    会变成荒谬的 SQL 时间窗口(硬规则:查询串必须校验+夹值,不能信任)。
    parse_qs 给的是 list[str] 或缺失;解析不出数字就退回默认值,不报错。
    """
    try:
        v = int(raw[0]) if raw else default
    except (ValueError, TypeError):
        v = default
    return max(lo, min(hi, v))


def _feed_filters(min_buyers: int, hours: int) -> str:
    """筛选条:买家数阈值 + 时间窗,纯 <a> 链接,不上 JS。"""
    def _link(label: str, href: str, active: bool) -> str:
        cls = " class=on" if active else ""
        return f'<a href="{esc(href)}"{cls}>{esc(label)}</a>'

    buyers = "".join(
        _link(f"≥{n}人", f"/?min_buyers={n}&hours={hours}", n == min_buyers)
        for n in _BUYER_OPTIONS
    )
    hrs = "".join(
        _link(f"{h}h" if h < 24 else f"{h // 24}d", f"/?min_buyers={min_buyers}&hours={h}", h == hours)
        for h in _HOUR_OPTIONS
    )
    return f'<div class=filters>{buyers}<span class=dim>丨</span>{hrs}</div>'


def _mcap_range(entry: float | None, now: float | None) -> str:
    """
    「入场 → 现在」市值。⚠️ 两边独立判空 —— 只缺一边就显示拿到的那一半,
    不能因为缺了一个数就把另一个已知数也藏起来。
    """
    if entry is None and now is None:
        return ""
    if entry is None:
        return f"现在 {mcap(now)}"
    if now is None:
        return f"入场 {mcap(entry)}"
    return f"{mcap(entry)} → {mcap(now)}"


def _signal_card(d: dict) -> str:
    """一张信号卡:字段来源见 queries.signal_feed 的注释。"""
    shown = d["buyer_handles"]
    more = d["buyers"] - len(shown)
    # ⚠️ 保持原文不转义,esc() 只在拼进 HTML 的那一刻调用一次 ——
    #    title 属性和下面的可见文字各用一次 esc(who),都是同一份未转义原文,
    #    不能像之前那样对已经转义过的字符串再转义一次(& 会变成 &amp;amp;)。
    who = "、".join(shown) + (f" 等{more}人" if more > 0 else "")
    age = token_age(d.get("token_created_at"))
    net = esc(NETWORK_DISPLAY.get(d["network_id"], d["network_id"]))
    mc_range = _mcap_range(d.get("entry_mcap"), d.get("now_mcap"))
    stale = stale_mark(d.get("mcap_at"))
    avg_line = money(d.get("avg_usd"))
    total_line = money(d.get("total_usd"))

    return (
        '<div class=scard>'
        '<div class=scard-head>'
        f'<span class=scard-token>{esc(token_label(d.get("symbol"), d["token_address"]))}</span>'
        f'<span class=dim>{net}</span>'
        + (f'<span class=dim>· {esc(age)}</span>' if age else "")
        + '</div>'
        # ⚠️ 「N 人买入」不做成链接:现有四个页面里没有一个能按单币筛选,
        #    硬链过去只会把人带到一个无关的列表。改用 title 悬浮 + 下面这行
        #    直接把人名列出来 —— 这正是我们相对 Debot 的优势:知道具体是谁。
        + f'<div class=scard-buyers title="{esc(who)}"><b>{d["buyers"]}</b> 人买入</div>'
        + (f'<div class="dim scard-who">{esc(who)}</div>' if who else "")
        + '<div class=scard-row>'
        + (f'<span class=dim>均笔 {avg_line}</span>' if avg_line else "")
        + (f'<span class=dim>合计 {total_line}</span>' if total_line else "")
        + '</div>'
        + (f'<div class=scard-row><span class=dim>{esc(mc_range)}</span></div>' if mc_range else "")
        + (f'<div class=scard-mult>{mult(d.get("multiple"))}</div>' if d.get("multiple") is not None else "")
        + ath_bar(d.get("now_mcap"), d.get("peak_mcap"))
        + sparkline(d["price_points"])
        + (f'<div class="dim scard-stale">{esc(stale)}</div>' if stale else "")
        + '</div>'
    )


def feed(conn: sqlite3.Connection, query: dict[str, list[str]] | None = None) -> str:
    """
    信号卡片流:新首页。一个币一张卡,回答「名单刚买了什么、几个人买的」。

    ⚠️ 默认 ≥2 人 / 24 小时 —— 实测近 24h 189 个在买的币里,126 个只有
       1 个人买过,那是噪声不是信号;默认门槛把它们先滤掉。
    """
    query = query or {}
    min_buyers = _clamp_int(query.get("min_buyers"), q.FEED_MIN_BUYERS_DEFAULT, *q.FEED_MIN_BUYERS_RANGE)
    hours = _clamp_int(query.get("hours"), q.FEED_WINDOW_MIN_DEFAULT // 60, *_HOUR_RANGE)

    rows = q.signal_feed(conn, min_buyers=min_buyers, window_min=hours * 60)
    cards = "".join(_signal_card(d) for d in rows)
    empty = (f'<p class=note>近 {hours} 小时内没有 ≥{min_buyers} 人买入的币。'
             '试试放宽筛选条件。</p>')
    body = (
        "<h1>信号卡片流</h1>"
        + _feed_filters(min_buyers, hours)
        + (f'<div class=feed>{cards}</div>' if rows else empty)
        + '<p class=note>市值、峰值按<b>当前行情快照</b>计算,入场市值取窗口内该币'
          '<b>最早一笔有市值的买入</b> —— 两者时点不同,差值不代表已实现盈亏。</p>'
    )
    return page("信号", body, active="/")
