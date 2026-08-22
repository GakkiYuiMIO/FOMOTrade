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
    for href in ("/", "/hot", "/copy"):
        assert f'<a href="{href}" class="on">' not in html_out
        assert f'<a href="{href}" class="">' in html_out


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
    for fn in (pages.dashboard, pages.people, pages.hot, pages.copy_ledger):
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
