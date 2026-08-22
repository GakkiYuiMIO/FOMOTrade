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


def _buy(c, uid, handle, ca, ts, mcap):
    """写一条买入事件"""
    from src.models import EVENT_BUY, FomoEvent

    ev = FomoEvent(
        event_id=f"{uid}:{ca}:{ts}", event_type=EVENT_BUY, user_id=uid,
        handle=handle, user_handle=handle, network_id="solana",
        token_address=ca, token_symbol=ca.upper(), amount_usd=100.0,
        event_ts=ts, market_cap=mcap, raw_json="{}",
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
