"""
⚠️ 这些规则是从 formatter.py 已经防住的坑抄过来的。
   网页上重新挖开就白做了。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src import store
from src.web import pages, render
from src.web import queries as q


def test_缺失显示为空而不是零或NA():
    """
    ⚠️ formatter.py 的铁律:判空用 is None。
       amount_usd=0.0 是**有意义的真实值**(清仓就靠「剩余 $0.00」体现),
       用 if not x 会把它和 None 一起吞掉。
    """
    assert render.money(None) == ""
    assert render.money(0.0) == "$0.00", "0 是真实值,必须显示"
    assert render.mult(None) == ""
    assert render.mult(0.0) == "0.00x"


def test_倍数保留两位():
    assert render.mult(1.5) == "1.50x"
    assert render.mult(30.559) == "30.56x"


def test_市值用紧凑写法():
    assert render.mcap(1_234_567) == "$1.23M"
    assert render.mcap(45_800) == "$45.8K"
    assert render.mcap(None) == ""


def test_行情超一小时标stale():
    """⚠️ token_snapshot 对清仓的币会冻住,实测 29.6% 的快照已超 3 天"""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    assert render.stale_mark(old) != ""
    assert render.stale_mark(fresh) == ""
    assert render.stale_mark(None) != "", "完全没有行情也要标出来"


def test_转义防止昵称里的尖括号打乱页面():
    assert "&lt;script&gt;" in render.esc("<script>")


def test_币名缺失时退回合约地址前缀():
    """⚠️ symbol 缺 31.3%,不能显示空白"""
    assert render.token_label(None, "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump") == "GCa9TZ…"
    assert render.token_label("TOAD", "GCa9TZ") == "$TOAD"


def test_导航栏仅当前页高亮():
    """plan 的测试块没覆盖这条,但 nav 高亮逻辑容易在重构时悄悄错位。"""
    html_out = render.page("标题", "<p>body</p>", active="/people")
    assert '<a href="/people" class="on">' in html_out
    for href in ("/", "/board", "/hot", "/copy"):
        assert f'<a href="{href}" class="on">' not in html_out
        assert f'<a href="{href}" class="">' in html_out


def test_导航栏首页是信号流旧看板挪到board():
    """
    ⚠️ 信号卡片流是新首页,旧看板必须还能从导航栏点到 —— 硬规则「四个老页面都要
       保持可达」,不能因为换了首页就把 /board 从导航里漏掉。
    """
    html_out = render.page("标题", "<p>body</p>", active="/")
    assert '<a href="/" class="on">信号</a>' in html_out
    assert '<a href="/board" class="">看板</a>' in html_out


def test_走势图点数不足时显示提示而不是图表():
    """
    ⚠️ token_price_history 刚上线,重启 + 跑够采样间隔前几乎全是空的 ——
       少于 2 个有效点必须降级成安静的提示文案,绝不能是空 <svg>(看着像坏了)
       或者拿假数据填(硬规则:不许伪造数据)。
    """
    for pts in ([], [1.0], [None, None], [1.0, None]):
        out = render.sparkline(pts)
        assert "还没有足够的行情历史" in out
        assert "<svg" not in out


def test_走势图有两个以上有效点时画svg():
    out = render.sparkline([100.0, 300.0, 200.0])
    assert "<svg" in out
    assert "还没有足够的行情历史" not in out
    # None 混在有效点之间只应该被跳过,不该让整条线报废
    out2 = render.sparkline([100.0, None, 200.0])
    assert "<svg" in out2


def test_ATH进度条按现价占峰值的比例算():
    out = render.ath_bar(50_000, 100_000)
    assert "50%" in out
    assert "width:50.0%" in out


def test_ATH进度条缺任一数据时为空():
    """⚠️ 判空用 is None —— 但缺了没法除,退化成空条不崩不算错"""
    assert render.ath_bar(None, 100_000) == ""
    assert render.ath_bar(50_000, None) == ""


def test_ATH进度条峰值为0时不崩溃():
    """峰值 0 是脏数据但仍是「取到值」的真实值(is None 判空铁律),
    只是除零算不出比例,这里只要求不崩、退化成空条,不是把 0 当缺失处理"""
    assert render.ath_bar(50_000, 0) == ""


def test_币龄复用formatter同一套算法():
    """⚠️ 不能自己另写一份换算,否则迟早和 Telegram 消息里的币龄对不上"""
    import time

    from src.formatter import fmt_token_age

    created = time.time() - 3600 * 5
    assert render.token_age(created) == fmt_token_age(created)
    assert render.token_age(None) == ""


def test_负数金额符号在美元符号前面():
    """
    carry-over fix(Task 4 review):与 formatter.py 的 _fmt_usd 对齐。
    ⚠️ "-$5.00" 而不是 "$-5.00" —— Task 8 的跟单人均 PnL 列会有大额负数
       (实测数据里有 -87,897 / -144,223),两种写法混用在同一页会很扎眼。
    """
    assert render.money(-5) == "-$5.00"
    assert render.money(-87897) == "-$87,897.00"
    assert render.money(0.0) == "$0.00", "0 不是负数,不能被误判"


def test_时间戳损坏时不当作新鲜行情处理():
    """
    carry-over fix(Task 4 review):stale_mark 对无法解析的时间戳原来是
    `except ValueError: return ""`,等价于"看起来很新鲜"。
    ⚠️ 一条脏数据不该看起来和实时行情一样 —— stale 标记正是用户判断
       "这个倍数能不能信"的依据。
    """
    assert render.stale_mark("not-a-timestamp") != ""
    assert render.stale_mark("") != ""


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    with store.get_conn() as c:
        yield c


def test_空库也能渲染每个页面(conn):
    """⚠️ 刚建库、一条数据都没有时页面不能崩 —— 那是第一次跑的人看到的画面"""
    for fn in (pages.feed, pages.dashboard, pages.people, pages.hot, pages.copy_ledger):
        out = fn(conn)
        assert out.startswith("<!doctype html>")
        assert "<main>" in out


def _buy(c, uid, handle, ca, ts, mcap):
    """
    写一条买入事件,badge_reason 显式给 REASON_LOCAL_STATS ——
    留空会被 COUNTABLE_REASONS 过滤掉,测试会以"一条数据都没有"的方式假绿。
    """
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    ev = FomoEvent(
        event_id=f"{uid}:{ca}:{ts}", event_type=EVENT_BUY, user_id=uid,
        handle=handle, user_handle=handle, network_id="solana",
        token_address=ca, token_symbol=ca.upper(), amount_usd=100.0,
        event_ts=ts, market_cap=mcap, raw_json="{}",
        badge_reason=REASON_LOCAL_STATS,
    )
    store.insert_event(c, ev)


def _ready(c, uid, handle):
    store.add_watch_user(c, uid, handle, handle)
    store.mark_stats_ready(c, uid)


def test_信号流按币聚合同一人加仓两次只算一张卡(conn):
    """⚠️ 与 queries.py 那条聚合口径测试对应的页面级验证:同一个人分批加仓
    不该被拆成两张长得一样的卡,「N 人买入」也不该被加仓笔数撑大"""
    from src.models import iso_minutes_ago

    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", iso_minutes_ago(120), 100_000)
    _buy(conn, "u1", "alice", "ca1", iso_minutes_ago(60), 500_000)  # 加仓

    out = pages.feed(conn, {"min_buyers": ["1"], "hours": ["24"]})
    assert out.count('class=scard-token') == 1, "两笔买入只能出一张卡"
    assert '<b>1</b> 人买入' in out


def test_信号流买家数阈值真的会过滤(conn):
    """默认门槛 ≥2 人:只有 1 个买家的币不该出现在默认信息流里"""
    from src.models import iso_minutes_ago

    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "solo", iso_minutes_ago(60), 100_000)
    _buy(conn, "u1", "alice", "duo", iso_minutes_ago(60), 100_000)
    _buy(conn, "u2", "bob", "duo", iso_minutes_ago(55), 120_000)

    out = pages.feed(conn)  # 不传 query,走默认 min_buyers=2
    assert "$DUO" in out
    assert "$SOLO" not in out

    out_relaxed = pages.feed(conn, {"min_buyers": ["1"]})
    assert "$SOLO" in out_relaxed, "放宽到 1 人门槛后应该能看到"


def test_信号流买家昵称转义不会转两次(conn):
    """
    ⚠️ 回归测试:昵称先在 pages._signal_card 里拼成「、」分隔的字符串,
       又要同时塞进 title 属性和可见文字两处 —— 之前的实现在拼接阶段就转义了
       一次,渲染阶段又对整串转义了一次,'&' 会变成 '&amp;amp;'(双重转义)。
       正确结果是原文只转义一次。
    """
    from src.models import iso_minutes_ago

    _ready(conn, "u1", "AT&T")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "AT&T", "ca1", iso_minutes_ago(60), 100_000)
    _buy(conn, "u2", "bob", "ca1", iso_minutes_ago(55), 100_000)

    out = pages.feed(conn, {"min_buyers": ["2"]})
    assert "AT&amp;T" in out, "必须转义一次"
    assert "AT&amp;amp;T" not in out, "不能转义两次"


def test_信号流没有快照的币仍能渲染不崩(conn):
    """硬规则:token_snapshot 没有对应行时,卡片照样渲染,那几格空着不报错"""
    from src.models import iso_minutes_ago

    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "nosnap", iso_minutes_ago(60), 100_000)
    _buy(conn, "u2", "bob", "nosnap", iso_minutes_ago(55), 100_000)

    out = pages.feed(conn, {"min_buyers": ["2"]})
    assert "$NOSNAP" in out
    assert "还没有足够的行情历史" in out  # 没有 token_price_history,sparkline 该降级


def test_信号流查询串参数被夹住不会产生荒谬查询(conn):
    """
    ⚠️ 硬规则:查询串来的值必须校验+夹值。这里用垃圾输入(非数字、超大数、
    负数)砸 pages.feed,只要求不崩、不抛异常 —— 校验/夹值本身在
    test_web_queries.py 那条 signal_feed 测试里已经钉死了具体边界。
    """
    from src.models import iso_minutes_ago

    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", iso_minutes_ago(60), 100_000)

    for query in (
        {"min_buyers": ["abc"], "hours": ["xyz"]},
        {"min_buyers": ["-999999"], "hours": ["-999999"]},
        {"min_buyers": ["999999999999999999"], "hours": ["999999999999999999"]},
    ):
        out = pages.feed(conn, query)
        assert out.startswith("<!doctype html>")


def test_人员榜缺显示名时不显示空白(conn):
    """
    ⚠️ /people 只展示样本 >= MIN_TOKENS_FOR_RANK 个币的人(queries.follow_value 的
       排名门槛),plan 原文的测试只 add_watch_user 不造买入事件,凑不够门槛,
       "alice" 根本不会出现在任何一行 —— 实测跑过,断言会假败。
       这里补足到门槛线,真正测的是 display_name 缺失时回退到 handle 显示。
    """
    store.add_watch_user(conn, "u1", "alice", None)
    store.mark_stats_ready(conn, "u1")
    for i in range(q.MIN_TOKENS_FOR_RANK):
        ca = f"ca{i}"
        _buy(conn, "u1", "alice", ca, f"2026-08-12T{i:02d}:00:00+00:00", 100_000.0)
        store.upsert_token_snapshots(conn, [("solana", ca, ca.upper(), 1.0, 200_000.0)])
    out = pages.people(conn)
    assert "alice" in out


def test_人员榜显示名单盈亏且与跟单价值分隔(conn):
    """
    Task 8:「他自己 7d / 生涯」两列答的是「他自己赚了多少」,
    与前面「跟单价值」三列(跟着他买我能拿到什么)是两个问题,
    页面上必须有视觉分隔,不能让读者以为一个能推出另一个。
    """
    store.add_watch_user(conn, "u1", "alice", None)
    store.mark_stats_ready(conn, "u1")
    for i in range(q.MIN_TOKENS_FOR_RANK):
        ca = f"ca{i}"
        _buy(conn, "u1", "alice", ca, f"2026-08-12T{i:02d}:00:00+00:00", 100_000.0)
        store.upsert_token_snapshots(conn, [("solana", ca, ca.upper(), 1.0, 200_000.0)])
    store.save_user_pnl(conn, [
        {"user_id": "u1", "total_pnl": 491_083.0, "pnl_24h": 100.0,
         "pnl_7d": -144_223.0, "pnl_30d": 200.0},
    ])

    out = pages.people(conn)
    assert "他自己 7d" in out
    assert "他自己生涯" in out
    assert "-$144,223.00" in out, "pnl_7d 要用 money() 渲染,负号在 $ 前面"
    assert "$491,083.00" in out
    assert "border-left" in out, "两列必须与跟单价值列有视觉分隔(竖线/换色之一)"


def test_人员榜在盈亏表不存在时仍能渲染(conn):
    """
    ⚠️ user_pnl_snapshot 由 store.init_db() 建表,只在 --run 才会真正建出来
       (--web 故意不建表)。机器还没重启过监控进程时这张表就是不存在的,
       /people 绝不能因此崩掉 —— 这两格留空即可。
    """
    store.add_watch_user(conn, "u1", "alice", None)
    store.mark_stats_ready(conn, "u1")
    for i in range(q.MIN_TOKENS_FOR_RANK):
        ca = f"ca{i}"
        _buy(conn, "u1", "alice", ca, f"2026-08-12T{i:02d}:00:00+00:00", 100_000.0)
        store.upsert_token_snapshots(conn, [("solana", ca, ca.upper(), 1.0, 200_000.0)])
    with store.tx(conn):
        conn.execute("DROP TABLE user_pnl_snapshot")

    out = pages.people(conn)
    assert out.startswith("<!doctype html>")
    assert "alice" in out


def test_跟单页显示整体倍数(conn):
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="TOAD",
        buyers=2, entry_mcap=100_000.0, age_sec=60, amount_usd=40.0, status="paper")
    store.upsert_token_snapshots(conn, [("solana", "ca1", "TOAD", 1.0, 200_000.0)])
    out = pages.copy_ledger(conn)
    assert "2.00x" in out
    assert "$TOAD" in out


def test_跟单页把新鲜和冻结的倍数分开算(conn):
    """
    ⚠️ 实测真实数据里 40% 的「现价」是停更的 —— token_snapshot 只更新
       「名单里还有人持有」的币,大家清仓后那个价就冻在那儿了。
       只给一个合计倍数会把这件事抹平,而冻结子集系统性更差
       (实测 0.65x vs 新鲜 0.71x)。
    ⚠️ 这条测试存在的理由:审查时把分类逻辑改成「全部算新鲜」,
       原有 12 条测试**全绿** —— 这个功能当时是裸奔的。
    """
    from datetime import UTC, datetime, timedelta

    # 新鲜的一条:翻倍
    store.record_copy_signal(
        conn, network_id="solana", token_address="fresh1", token_symbol="FRESH",
        buyers=2, entry_mcap=100_000.0, age_sec=60, amount_usd=40.0, status="paper")
    store.upsert_token_snapshots(conn, [("solana", "fresh1", "FRESH", 1.0, 200_000.0)])
    # 冻结的一条:腰斩,且行情停在 3 天前
    store.record_copy_signal(
        conn, network_id="solana", token_address="frozen1", token_symbol="FROZEN",
        buyers=2, entry_mcap=100_000.0, age_sec=60, amount_usd=40.0, status="paper")
    store.upsert_token_snapshots(conn, [("solana", "frozen1", "FROZEN", 1.0, 50_000.0)])
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    with store.tx(conn):
        conn.execute("UPDATE token_snapshot SET updated_at = ? WHERE token_address = ?",
                     (old, "frozen1"))

    out = pages.copy_ledger(conn)
    assert "新鲜倍数(1条)" in out, "新鲜子集应当只有 1 条"
    assert "冻结倍数(1条)" in out, "冻结子集应当只有 1 条"
    assert "2.00x" in out, "新鲜子集应当是 2.00x"
    assert "0.50x" in out, "冻结子集应当是 0.50x"
    assert "行情停在" in out, "冻结那行必须带 stale 标记"
