"""
⚠️ 聚合口径算错了**很难肉眼发现** —— 页面照样渲染、数字照样好看,
   但排序是错的。所以这里把每条口径钉死。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src import store
from src.web import queries


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    with store.get_conn() as c:
        yield c


def _ready(c, uid, handle):
    store.add_watch_user(c, uid, handle, handle)
    store.mark_stats_ready(c, uid)


def _buy(c, uid, handle, ca, ts, mcap, reason=None):
    """
    写一条买入事件。

    ⚠️ badge_reason 必须显式给 —— 真实链路里它由 judge_badge 填,
       而这里绕过了它。留 NULL 的话所有事件都会被 COUNTABLE_REASONS 过滤掉,
       测试会以「一条数据都没有」的方式假绿。
    """
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    ev = FomoEvent(
        event_id=f"{uid}:{ca}:{ts}", event_type=EVENT_BUY, user_id=uid,
        handle=handle, user_handle=handle, network_id="solana",
        token_address=ca, token_symbol=ca.upper(), amount_usd=100.0,
        event_ts=ts, market_cap=mcap, raw_json="{}",
        badge_reason=reason or REASON_LOCAL_STATS,
    )
    store.insert_event(c, ev)


def test_跟单价值按第一次买入算(conn):
    """
    ⚠️ 同一个币加仓多次只能算一次,而且取**第一次**那条 ——
       按每条买入算会让加仓多的人被重复计数,权重完全失真。
    """
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T02:00:00+00:00", 500_000)  # 加仓
    store.upsert_token_snapshots(conn, [("solana", "ca1", "CA1", 1.0, 200_000)])

    rows = queries.follow_value(conn, min_tokens=1)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 1, "同一个币只能算一次"
    assert rows[0]["median_now"] == pytest.approx(2.0), "应按第一次的 10 万算,不是 50 万"


def test_样本太少的人不进榜(conn):
    """样本 3 个币的「胜率 100%」是噪声,不是信号"""
    _ready(conn, "u1", "alice")
    for i in range(3):
        _buy(conn, "u1", "alice", f"ca{i}", f"2026-08-12T0{i}:00:00+00:00", 100_000)
        store.upsert_token_snapshots(conn, [("solana", f"ca{i}", "X", 1.0, 200_000)])
    assert queries.follow_value(conn, min_tokens=8) == []
    assert len(queries.follow_value(conn, min_tokens=3)) == 1


def test_拿不到入场市值的币直接跳过(conn):
    """⚠️ 不能按 0 算,那会造出无穷大倍数"""
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", None)
    _buy(conn, "u1", "alice", "ca2", "2026-08-12T02:00:00+00:00", 100_000)
    for ca in ("ca1", "ca2"):
        store.upsert_token_snapshots(conn, [("solana", ca, "X", 1.0, 200_000)])
    rows = queries.follow_value(conn, min_tokens=1)
    assert rows[0]["tokens"] == 1, "缺市值的那个必须被跳过"


def test_胜率和峰值分开算(conn):
    """
    ⚠️ 实测 45 人里只有 1 人现价中位在 1x 以上,而峰值中位有 1.05~1.82x 的区分度。
       只显示其中一列都会误导 —— 差别不在选币,在卖点。
    """
    _ready(conn, "u1", "alice")
    # 两个币:一个冲高回落(现价亏、峰值赚),一个原地不动
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 300_000)])
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 50_000)])
    _buy(conn, "u1", "alice", "ca2", "2026-08-12T02:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca2", "X", 1.0, 100_000)])

    r = queries.follow_value(conn, min_tokens=1)[0]
    assert r["median_now"] == pytest.approx(0.75)     # (0.5 + 1.0) / 2
    assert r["median_peak"] == pytest.approx(2.0)     # (3.0 + 1.0) / 2
    assert r["win_rate"] == pytest.approx(0.0)        # 两个都不 > 1x


def test_只算名单里就绪的人(conn):
    """与 count_recent_buyers 同一套谓词,否则榜单和推送里的数字对不上"""
    store.add_watch_user(conn, "u2", "bob", "Bob")   # 没 mark_stats_ready
    _buy(conn, "u2", "bob", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 200_000)])
    assert queries.follow_value(conn, min_tokens=1) == []


def test_稳定币互换不算跟单标的(conn):
    """
    ⚠️ 买 USDC 不是一个可跟的信号,而且稳定币几乎不动,
       把它算进去会把所有人的中位数往 1.0x 拽 —— 实测会让区分度从
       1.125~2.088 塌缩到 1.050~1.843。
       与 count_recent_buyers 用同一套 COUNTABLE_REASONS 谓词。
    """
    from src.models import REASON_QUOTE_TOKEN

    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "real", "2026-08-12T01:00:00+00:00", 100_000)
    _buy(conn, "u1", "alice", "usdc", "2026-08-12T02:00:00+00:00", 100_000,
         reason=REASON_QUOTE_TOKEN)
    for ca in ("real", "usdc"):
        store.upsert_token_snapshots(conn, [("solana", ca, "X", 1.0, 200_000)])

    rows = queries.follow_value(conn, min_tokens=1)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 1, "稳定币互换必须被排除"


def test_峰值为0时不当成缺失(conn):
    """
    ⚠️ 项目铁律:判空用 is None。0 在这个库里经常是有意义的真实值,
       真值判断会把它和缺失一起吞掉 —— formatter.py 为这条吃过亏。
       这里 0 虽然是脏数据,但口径要一致,否则 queries.py 作为模板会把
       真值判断扩散到后面几个函数里。
    """
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 200_000)])
    # 把峰值强行改成 0(真实数据里不会出现,但口径必须确定)
    with store.tx(conn):
        conn.execute("UPDATE token_snapshot SET max_market_cap = 0")

    r = queries.follow_value(conn, min_tokens=1)[0]
    assert r["median_peak"] == pytest.approx(0.0), "0 是真实值,不能回落成现价 2.0x"


def test_跟单价值合并名单盈亏(conn):
    """
    Task 8:follow_value 的返回里要带上 total_pnl / pnl_7d,
    但这两列答的是「他自己赚了多少」,与其余列(跟单价值)是两码事。
    ⚠️ 拿不到数据的人必须是 None,不能填 0 —— 0 是「不赚不亏」的真实值,
       填 0 会让拿不到数据的人在排行里排到中间去。
    """
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    _buy(conn, "u2", "bob", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 200_000)])
    store.save_user_pnl(conn, [
        {"user_id": "u1", "total_pnl": 491_083.0, "pnl_24h": 100.0,
         "pnl_7d": -144_223.0, "pnl_30d": 200.0},
        # u2 故意不写盈亏,模拟「这轮没采集到」
    ])

    rows = {r["user_id"]: r for r in queries.follow_value(conn, min_tokens=1)}
    assert rows["u1"]["total_pnl"] == pytest.approx(491_083.0)
    assert rows["u1"]["pnl_7d"] == pytest.approx(-144_223.0)
    assert rows["u2"]["total_pnl"] is None, "没采集到就该是 None,不能是 0"
    assert rows["u2"]["pnl_7d"] is None


def test_名单盈亏表不存在时跟单价值仍能返回(conn):
    """
    ⚠️ user_pnl_snapshot 由 store.init_db() 建表,只在 --run 时跑过一次才会存在
       (--web 故意不建表)。机器还没重启过监控进程时这张表就是不存在的,
       follow_value 绝不能因此抛异常炸掉整个 /people 页。
    """
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 200_000)])
    with store.tx(conn):
        conn.execute("DROP TABLE user_pnl_snapshot")

    rows = queries.follow_value(conn, min_tokens=1)
    assert len(rows) == 1
    assert rows[0]["total_pnl"] is None
    assert rows[0]["pnl_7d"] is None


def test_跟单台账不截断且带整体倍数(conn):
    """
    ⚠️ TG 的 /paper 只显 15 条,55 条里 40 条永远看不到,
       「整体 0.64x」这个结论在 TG 上根本得不出来。这正是网页存在的理由。
    """
    for i in range(20):
        store.record_copy_signal(
            conn, network_id="solana", token_address=f"ca{i}", token_symbol="X",
            buyers=2, entry_mcap=100_000.0, age_sec=60,
            amount_usd=40.0, status="paper")
        store.upsert_token_snapshots(
            conn, [("solana", f"ca{i}", "X", 1.0, 50_000.0)])   # 全部腰斩

    r = queries.copy_summary(conn)
    assert r["count"] == 20, "不能截断"
    assert r["invested"] == pytest.approx(800.0)
    assert r["value"] == pytest.approx(400.0)
    assert r["multiple"] == pytest.approx(0.5)
    assert r["winners"] == 0


def test_跟单台账缺行情时不计入合计(conn):
    """⚠️ 拿不到现价的单子按 0 算会把整体倍数拉垮,那是假的亏损"""
    store.record_copy_signal(
        conn, network_id="solana", token_address="known", token_symbol="X",
        buyers=2, entry_mcap=100_000.0, age_sec=60, amount_usd=40.0, status="paper")
    store.upsert_token_snapshots(conn, [("solana", "known", "X", 1.0, 200_000.0)])
    store.record_copy_signal(
        conn, network_id="solana", token_address="nomcap", token_symbol="X",
        buyers=2, entry_mcap=100_000.0, age_sec=60, amount_usd=40.0, status="paper")

    r = queries.copy_summary(conn)
    assert r["count"] == 2, "两条都要列出来"
    assert r["priced"] == 1, "但只有一条能算价"
    assert r["multiple"] == pytest.approx(2.0), "合计只按能算价的那条"


def test_看板健康度报出最后一轮距今多久(conn):
    from src.models import now_iso

    with store.tx(conn):
        store.set_state(conn, "last_tick_at", now_iso())
    r = queries.dashboard(conn)
    assert r["last_tick_age_sec"] is not None
    assert r["last_tick_age_sec"] < 60


def test_从没跑过时健康度是None而不是0(conn):
    """⚠️ 0 会被读成「刚刚跑过」,而真相是「从来没跑过」"""
    assert queries.dashboard(conn)["last_tick_age_sec"] is None
