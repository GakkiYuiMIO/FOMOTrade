"""
⚠️ 聚合口径算错了**很难肉眼发现** —— 页面照样渲染、数字照样好看,
   但排序是错的。所以这里把每条口径钉死。
"""
# ruff: noqa: N802
from __future__ import annotations

import sqlite3

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


class _LockedPnlConn:
    """
    包一层真实连接,只让涉及 user_pnl_snapshot 的查询抛出「数据库被锁」,其余原样代理。

    ⚠️ sqlite3.Connection 是 C 扩展类型,实例不允许直接 monkeypatch .execute
       (会报 "attribute 'execute' is read-only"),所以用一层代理对象而不是
       monkeypatch.setattr(conn, ...)。
    """

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **kw):
        if "user_pnl_snapshot" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **kw)


def test_跟单价值_盈亏表其它OperationalError原样上抛(conn):
    """
    ⚠️ follow_value 只应该吞「表还不存在」这一种 OperationalError ——
       数据库被锁、磁盘错误、列名拼错这些真 bug 不能被这段兜底悄悄吃掉,
       否则会被永久藏起来,变成一个"看起来正常但数据一直是 None"的隐形故障。
    """
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", "2026-08-12T01:00:00+00:00", 100_000)
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 200_000)])

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        queries.follow_value(_LockedPnlConn(conn), min_tokens=1)


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


# ============================================================
# 信号卡片流(queries.signal_feed)
# ============================================================
# ⚠️ 必须用「现在往前推 N 分钟」而不是写死日期字符串 —— signal_feed 带
#    时间窗过滤(与本文件其它口径测试不同,那些函数不筛时间),写死的日期
#    一旦落到默认 24h 窗口之外,测试会以「查询串就是夹不住」的方式假败。
from src.models import iso_minutes_ago as _ago  # noqa: E402


def test_信号流按币聚合不是按笔(conn):
    """
    ⚠️ 核心口径:同一个人对同一个币加仓两次,必须聚合成一张卡、买家数算 1 ——
       按笔算的话,796 笔买入会变成 796 张卡,而且同一个人加仓三次会长出
       三张长得一样的卡。
    """
    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", _ago(120), 100_000)
    _buy(conn, "u1", "alice", "ca1", _ago(60), 500_000)  # 加仓

    rows = queries.signal_feed(conn, min_buyers=1)
    assert len(rows) == 1, "两笔买入只能聚合出一张卡"
    assert rows[0]["buyers"] == 1, "加仓不能把买家数撑大"
    assert rows[0]["buys"] == 2, "笔数本身照实记,只是不能拆成两张卡"


def test_信号流买家数阈值真的会过滤(conn):
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "solo", _ago(60), 100_000)
    _buy(conn, "u1", "alice", "duo", _ago(60), 100_000)
    _buy(conn, "u2", "bob", "duo", _ago(55), 120_000)

    only_two_plus = {r["token_address"] for r in queries.signal_feed(conn, min_buyers=2)}
    assert only_two_plus == {"duo"}, "只有 ≥2 人买过的币能进 ≥2 门槛的结果"

    everyone = {r["token_address"] for r in queries.signal_feed(conn, min_buyers=1)}
    assert everyone == {"solo", "duo"}, "门槛放到 1 之后两个都该在"


def test_信号流入场市值取窗口内最早一笔而不是快照(conn):
    """
    ⚠️ 铁律:entry 绝不能用 token_snapshot 回填 —— 那是「现在」的市值,
       回填会凭空造出纸面盈利。这里让快照市值远高于最早那笔买入的市值,
       entry 必须仍然等于最早那笔的值。
    """
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "ca1", _ago(120), 100_000)   # 最早
    _buy(conn, "u2", "bob", "ca1", _ago(60), 150_000)      # 更晚
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 999_000)])  # 现在的市值,离谱地高

    rows = queries.signal_feed(conn, min_buyers=2)
    assert len(rows) == 1
    assert rows[0]["entry_mcap"] == pytest.approx(100_000), "入场必须是最早那笔,不是快照"
    assert rows[0]["now_mcap"] == pytest.approx(999_000), "现在的市值才该来自快照"


def test_信号流没有快照的币仍能返回(conn):
    """⚠️ token_snapshot 没有对应行时不能崩,那几列该是 None 不是 0"""
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "ca1", _ago(60), 100_000)
    _buy(conn, "u2", "bob", "ca1", _ago(55), 100_000)

    rows = queries.signal_feed(conn, min_buyers=2)
    assert len(rows) == 1
    assert rows[0]["now_mcap"] is None
    assert rows[0]["peak_mcap"] is None
    assert rows[0]["mcap_at"] is None
    assert rows[0]["multiple"] is None, "算不出倍数时必须是 None,不能按 0 算"


def test_信号流缺金额的买入不会伪造出0合计(conn):
    """
    ⚠️ 判空铁律:一笔买入都拿不到金额时,合计/均笔必须是 None(格子空着),
       不能显示成一个假的「$0.00 合计」——0 是真实值,不是「不知道」。
    """
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    for uid, handle, ts in (("u1", "alice", _ago(60)), ("u2", "bob", _ago(55))):
        ev = FomoEvent(
            event_id=f"{uid}:noamt:{ts}", event_type=EVENT_BUY, user_id=uid,
            handle=handle, user_handle=handle, network_id="solana",
            token_address="noamt", token_symbol="NOAMT", amount_usd=None,
            event_ts=ts, market_cap=100_000, raw_json="{}",
            badge_reason=REASON_LOCAL_STATS,
        )
        store.insert_event(conn, ev)

    rows = queries.signal_feed(conn, min_buyers=2)
    assert len(rows) == 1
    assert rows[0]["total_usd"] is None
    assert rows[0]["avg_usd"] is None


def test_信号流价格历史表不存在时仍能返回(conn):
    """
    ⚠️ 实测踩过的坑(对着真实 data/fomo.db 的拷贝验证时发现):
       token_price_history 只在 store.init_db() 里建,而 --web 是只读进程
       故意不建表。正在跑的监控进程只要还没重启过,这张表在真实库里就是
       「压根不存在」,不是「存在但是空的」—— 直接查会是 sqlite3.OperationalError:
       no such table,不是空列表。signal_feed 绝不能因此崩掉。
    """
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _buy(conn, "u1", "alice", "ca1", _ago(60), 100_000)
    _buy(conn, "u2", "bob", "ca1", _ago(55), 100_000)
    with store.tx(conn):
        conn.execute("DROP TABLE token_price_history")

    rows = queries.signal_feed(conn, min_buyers=2)
    assert len(rows) == 1
    assert rows[0]["price_points"] == [], "表不存在时退化成空列表,不能抛异常"


def test_信号流的min_buyers和window会被夹在合理范围内(conn):
    """
    ⚠️ 硬规则:查询串来的值必须校验+夹值,不能直接拼进 SQL —— 这里钉的是
       queries.signal_feed 自己的防御性夹值(pages.py 那道校验单独测)。
    """
    from src.models import now_iso

    _ready(conn, "u1", "alice")
    _buy(conn, "u1", "alice", "ca1", now_iso(), 100_000)

    # 荒谬的负数下限应该被夹到 1,买家数=1 的币能出现
    assert len(queries.signal_feed(conn, min_buyers=-999, window_min=60 * 24)) == 1
    # 荒谬的超大上限应该被夹到 50,买家数=1 的币达不到门槛
    assert queries.signal_feed(conn, min_buyers=99999999, window_min=60 * 24) == []
