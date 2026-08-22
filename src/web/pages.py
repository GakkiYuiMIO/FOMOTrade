"""
四个页面的渲染。每个函数进只读连接,出完整 HTML。

⚠️ 页面只调 queries.* 取数、只调 render.* 出字符串,自己不写 SQL 也不拼 <html>。
"""
from __future__ import annotations

import sqlite3

from src import store
from src.web import queries as q
from src.web.render import esc, mcap, money, mult, page, pct, stale_mark, token_label

_SNAPSHOT_NOTE = ('<p class=note>所有数字按<b>当前行情快照</b>计算。'
                  '实测相邻两轮之间整体倍数会有百分之几的波动。</p>')


def _card(k: str, v: str) -> str:
    return f'<div class=card><div class=v>{v}</div><div class=k>{esc(k)}</div></div>'


def dashboard(conn: sqlite3.Connection) -> str:
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
    return page("看板", body, active="/")


def people(conn: sqlite3.Connection) -> str:
    rows = q.follow_value(conn)
    head = ("<tr><th>名单成员</th><th>币数</th><th>峰值中位</th>"
            "<th>现价中位</th><th>胜率</th></tr>")
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
            f'<td class=n>{pct(r["win_rate"])}</td></tr>'
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
        + _SNAPSHOT_NOTE
    )
    return page("跟单价值", body, active="/people")


def hot(conn: sqlite3.Connection) -> str:
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


def copy_ledger(conn: sqlite3.Connection) -> str:
    c = q.copy_summary(conn)
    priced = [r for r in c["rows"] if r["multiple"] is not None]
    # ⚠️ 实测(Task 3):token_snapshot 只覆盖「名单里还有人持有」的币,清仓后就停更 ——
    #    56 条能算出价的里有 23 条(41%)是三天前冻住的现价,却照样参与整体倍数。
    #    新鲜 0.681x、冻结 0.595x,合起来的 0.70x 把差别抹平了。
    #    这里按 stale_mark(同一套「多旧算冻住」的判据)把两类分开报,不能只给一个合计数。
    fresh_rows = [r for r in priced if not stale_mark(r["mcap_at"])]
    frozen_rows = [r for r in priced if stale_mark(r["mcap_at"])]
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
            # ⚠️ 每行标 stale:这里的告警文案与整体拆分用的是同一个 stale_mark 判据,
            #    行内看到"⚠️ 行情停在 3d 前"时,应该能对应到上面冻结子集里
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
        + '<p class=note>⚠️ 「冻结」= 现价快照已停更(通常是清仓后不再刷新)。'
          '它拉低还是拉高整体倍数,不看拆分看不出来 —— 合并成一个数会把这个信息抹掉。</p>'
        + '<p class=note>⚠️ 纸上盈亏按市值比折算,<b>没算手续费、滑点、gas</b>,真实结果只会更差。</p>'
        + _SNAPSHOT_NOTE
    )
    return page("我的跟单", body, active="/copy")
