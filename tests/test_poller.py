"""
Poller 回归测试 —— 钉死设计文档 §3.4 称为「唯一的结构性约束」的那条规则。

⚠️ 这个文件存在的理由:
   在它之前,「不允许边落库边推送」这条约束**没有任何测试保护**。
   谁哪天顺手把 notifier.send() 挪进落库循环里,所有单测照样全绿,
   而线上表现是"同一秒买同一个币的两个人看到 1 和 2 两个不同的共识数" ——
   这种 bug 只会在真实并发下偶发,靠人眼 review 根本抓不住。

全部测试跑在临时 DB 文件上(Poller 走 store.get_conn(),必须有真实文件),
不碰网络、不碰 data/fomo.db。
"""
from __future__ import annotations

# 测试函数名用中文(项目规范),与其余 tests/ 保持一致
# ruff: noqa: N802
import math
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from src import store
from src.client import NotSupportedError, UserGoneError, UserSnapshot
from src.models import EVENT_BUY, now_iso
from src.poller import _FEED_EVERY_N_TICKS, _PNL_EVERY_N_TICKS, _SEND_BURST, Poller
from tests.conftest import CA_TOAD

# ---- 时间锚点 ------------------------------------------------------------
# 事件必须晚于 /add 时写入的游标才会被推送。游标是 now_iso()(截断到秒),
# 所以用"未来 60 秒"最稳 —— 仍在 to_iso 的 [2020-01-01, now+1d] 合理区间内。
_FUTURE = datetime.now(UTC) + timedelta(seconds=60)
_FUTURE_MS = int(_FUTURE.timestamp() * 1000)
_OLD_CURSOR = "2026-01-01T00:00:00+00:00"


def _swap(sid: str, *, ca: str = CA_TOAD, usd: float = 2500.0, ms: int | None = None) -> dict:
    """一条形态完整的买入 swap。字段名取 _K_* 候选里最可能的那个。"""
    return {
        "id": sid,
        "networkId": "solana",
        "tokenAddress": ca,
        "symbol": "TOAD",
        "side": "buy",
        "timestamp": ms or _FUTURE_MS,
        "amountUsd": usd,
        "txHash": f"tx-{sid}",
        "holdingUsd": usd,
        "marketCap": 19_140_000,
    }


class FakeNotifier:
    """记录所有发出的消息;send 的返回值可控,用来验 mark_sent 的时机"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str, **kw) -> bool:
        self.sent.append(text)
        return self.ok


class FakeClient:
    """
    可编排的假 client。
    snaps: user_id -> UserSnapshot;取不到时返回全空快照。
    """

    def __init__(self, snaps: dict | None = None, *, seed_supported: bool = False,
                 feed: list | None = None, feed_boom: Exception | None = None,
                 me: str = "ME", following: list | None = None,
                 following_boom: Exception | None = None,
                 leaderboard: list | None = None,
                 leaderboard_boom: Exception | None = None):
        self.snaps = snaps or {}
        self.seed_supported = seed_supported
        self.feed = feed if feed is not None else []
        self.feed_boom = feed_boom
        self.feed_calls = 0
        # 默认:自己是 "ME"、关注了 snaps 里的所有人 —— 也就是"活动流全覆盖"的理想情况。
        # ⚠️ 真实世界里**自己一定不在自己的关注列表里**(人不关注自己),
        #    要复现线上那条路径,把自己的 uid 也放进 snaps、但不放进 following。
        self.me = me
        self.following = following if following is not None else list(self.snaps)
        self.following_boom = following_boom
        self.following_calls = 0
        # 名单盈亏采集(Task 8)用
        self.leaderboard = leaderboard if leaderboard is not None else []
        self.leaderboard_boom = leaderboard_boom
        self.leaderboard_calls = 0
        # 转账采集是降频的(20 轮一次),要能数出"到底哪几轮真的打了请求"
        self.transfer_calls: list[str] = []

    def get_activity_feed(self, limit: int = 100) -> list:
        self.feed_calls += 1
        if self.feed_boom:
            raise self.feed_boom
        return list(self.feed)

    def get_leaderboard(self, period: str = "24h", limit: int = 20) -> list:
        self.leaderboard_calls += 1
        if self.leaderboard_boom:
            raise self.leaderboard_boom
        return list(self.leaderboard)

    def get_current_user(self) -> dict:
        return {"id": self.me, "userHandle": "me"}

    def get_following(self, user_id: str, max_items: int = 300) -> list:
        self.following_calls += 1
        if self.following_boom:
            raise self.following_boom
        return [{"id": u} for u in self.following]

    def fetch_snapshot(self, user_id: str) -> UserSnapshot:
        return self.snaps.get(user_id) or UserSnapshot(
            user_id=user_id, swaps=[], transfers=[], thesis=[], balances=[]
        )

    def iter_swap_buys(self, user_id: str, max_items: int):
        # 默认不支持分页 → seeding 保持 stats_ready=0,
        # 正好用来构造"基线未就绪"的场景(验收 #2)
        if not self.seed_supported:
            raise NotSupportedError("测试用:不支持分页")
        return iter(())

    # ---- 按端点取。poller 改成两段式增量采集后走的是这三个,不再走 fetch_snapshot ----
    # ⚠️ 必须如实透传 None:None = 该项拉取失败,[] = 拉到了但确实没有。
    #    写成 `or []` 会把 None 抹平,正好抹掉 count_holders「拉不全就整段消失」那条路径。
    def get_swaps(self, user_id: str, limit: int = 50):
        return self.fetch_snapshot(user_id).swaps

    def get_balances(self, user_id: str):
        return self.fetch_snapshot(user_id).balances

    def get_trades(self, user_id: str):
        return self.fetch_snapshot(user_id).trades

    def get_transfers(self, user_id: str, limit: int = 25):
        self.transfer_calls.append(user_id)
        return self.fetch_snapshot(user_id).transfers


@pytest.fixture
def db(tmp_path):
    """
    把 store.DB_PATH 指到临时文件 —— Poller 内部走 get_conn(),必须有真实文件。

    ⚠️⚠️ 这里**刻意不复用 monkeypatch 夹具**,而是自己开一个 MonkeyPatch:
       monkeypatch 是函数级共享的,用例里任何一句 `monkeypatch.undo()` 都会把
       DB_PATH 一起还原成 data/fomo.db —— 于是那一刻起,测试打的是**生产库**。
       这不是假想:2026-08-26 就这么发生过一次(一条用例用 undo() 还原
       render 的桩,后半段直接连上了正在运行的生产库)。
       自己开一份就与用例的 undo() 彻底隔离。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


def _add_ready(user_id: str, handle: str) -> None:
    """加一个已就绪用户,并把游标拨到很早 —— 否则新事件会被当成历史丢掉"""
    with store.get_conn() as c:
        store.add_watch_user(c, user_id, handle, handle)
        store.mark_stats_ready(c, user_id)
        for kind in store.CURSOR_KINDS:
            store.set_cursor(c, user_id, kind, _OLD_CURSOR)


# ============================================================
# 结构性约束:先全部落库,再统一渲染发送
# ============================================================
def test_两人同tick首次买同一新币_共识数都是2(db):
    """
    验收用例 #6,也是「两阶段结构」最直接的可观测后果。

    如果谁把发送挪进落库循环,先处理的那个人会看到 1/2、后一个看到 2/2 ——
    数字自相矛盾一次,功能 B 在用户心里就永久作废了。
    """
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
        "uB": UserSnapshot("uB", swaps=[_swap("b1")], transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    n = Poller(client, notifier).tick()

    assert n == 2, "两条买入都该落库"
    assert len(notifier.sent) == 2
    for msg in notifier.sent:
        assert "🌱" in msg, "两人都是第一次买这个币,都该标首次建仓"
        assert "2/2" in msg, f"共识数必须是本 tick 结束后的一致快照,实际消息:\n{msg}"


def test_发送时全部事件已落库(db):
    """
    直接钉死两阶段结构:send() 被调用的那一刻,本 tick 的**所有**事件必须已经在库里。

    边落库边发的写法下,第一条消息发出时第二条还没入库,这里会当场失败。
    """
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    seen_counts: list[int] = []

    class ProbingNotifier(FakeNotifier):
        def send(self, text: str, **kw) -> bool:
            with store.get_conn() as c:
                seen_counts.append(
                    c.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"]
                )
            return super().send(text)

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
        "uB": UserSnapshot("uB", swaps=[_swap("b1")], transfers=[], thesis=[], balances=[]),
    })
    Poller(client, ProbingNotifier()).tick()

    assert seen_counts == [2, 2], (
        f"每次 send 时库里都该已有全部 2 条事件,实际 {seen_counts} —— "
        "落库与推送被混进了同一个循环"
    )


# ============================================================
# 降级路径
# ============================================================
def test_基线未就绪_不打徽章不算共识但照常推送(db):
    """验收用例 #2:未就绪期间推送不能停,但绝不能打徽章、不能显示共识"""
    with store.get_conn() as c:
        store.add_watch_user(c, "uN", "newguy", "newguy")   # stats_ready 保持 0
        for kind in store.CURSOR_KINDS:
            store.set_cursor(c, "uN", kind, _OLD_CURSOR)

    client = FakeClient({
        "uN": UserSnapshot("uN", swaps=[_swap("n1")], transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    assert len(notifier.sent) == 1, "基线没建好也必须照常推送"
    msg = notifier.sent[0]
    assert "🌱" not in msg, "基线未就绪时判不出首次,宁可漏标不可错标"
    assert "人买过" not in msg, "共识行必须整段消失"
    assert "⏳" in msg, "要让用户知道徽章/共识为什么暂时没有"

    with store.get_conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM user_token_stats").fetchone()["n"]
    assert n == 0, "未就绪用户的事件绝不能写进 stats —— 会污染日后建立的基线"


def test_某人balances拉取失败_只掉仍持有段(db):
    """验收用例 #8:holders 是副指标,拉不全就整段消失;buyers 与徽章完全不受影响"""
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
        # uB 的 balances 拉取失败
        "uB": UserSnapshot("uB", swaps=[], transfers=[], thesis=[], balances=None),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    msg = notifier.sent[0]
    assert "人买过" in msg, "主指标只查本地库,不该受 balances 影响"
    assert "仍持有" not in msg, "只要有一个人的 balances 缺失,副指标就必须整段消失"
    assert "🌱" in msg


def test_登录态失效必须上抛而不是被吞掉(db):
    """
    ⚠️ AuthError 若被 _fetch_snapshots 的裸 except 吞掉,程序会每 20 秒空转一次、
       只刷 ERROR 日志,而设计 §3.5 要求的「🔐 登录态失效」TG 告警永远发不出去 ——
       用户会一直以为监控还活着。
    """
    from src.auth import AuthError

    _add_ready("uA", "alice")

    class DeadClient(FakeClient):
        def get_swaps(self, user_id: str, limit: int = 50):
            raise AuthError("token 过期了")

    with pytest.raises(AuthError):
        Poller(DeadClient(), FakeNotifier()).tick()


# ============================================================
# dry-run 与 sent 时机
# ============================================================
def test_dry_run不发任何TG消息(db):
    """--dry-run 的帮助文案写的是「只打印不推送」,包括基线完成回执在内一条都不许发"""
    with store.get_conn() as c:
        store.add_watch_user(c, "uN", "newguy", "newguy")
        for kind in store.CURSOR_KINDS:
            store.set_cursor(c, "uN", kind, _OLD_CURSOR)

    client = FakeClient(
        {"uN": UserSnapshot("uN", swaps=[_swap("n1")], transfers=[], thesis=[], balances=[])},
        seed_supported=True,   # 让 seeding 真的跑完,它末尾会想发一条"基线完成"
    )
    notifier = FakeNotifier()
    Poller(client, notifier).tick(dry_run=True)

    assert notifier.sent == [], f"dry-run 期间发出了消息:{notifier.sent}"


def test_推送失败时不标记已发送(db):
    """
    ⚠️ sent=1 只能在 TG 确认收到之后写。"发之前就标已发"会让崩溃/失败的消息永久丢失 ——
       下一 tick 该事件已在库里,INSERT OR IGNORE 直接跳过,再也不会被重新发现。
    """
    _add_ready("uA", "alice")
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
    })
    Poller(client, FakeNotifier(ok=False)).tick()

    with store.get_conn() as c:
        row = c.execute("SELECT sent FROM fomo_events").fetchone()
        assert row["sent"] == 0, "TG 没收到就不能标已发"
        assert len(store.load_unsent_recent(c, minutes=10)) == 1, "必须还留在补发队列里"


# ============================================================
# 时间戳批级健康检查
# ============================================================
def test_整批时间戳解析失败时丢弃该批(db):
    """
    ⚠️ 这是本项目最危险的一条静默失效链:
       时间字段名/单位判错 → 每条 event_ts 都兜底成 now → 全部越过游标
       → 首个 tick 把几百条历史一次性轰进 TG,而日志里一个字都没有。
       批级检查把它变成一条 ERROR + 丢批。
    """
    _add_ready("uA", "alice")
    # 6 条(超过 _TS_GUARD_MIN_BATCH)全部没有任何可识别的时间字段
    broken = []
    for i in range(6):
        s = _swap(f"x{i}")
        del s["timestamp"]
        broken.append(s)

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=broken, transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    n = Poller(client, notifier).tick()

    assert n == 0, "整批时间戳不可信时必须丢弃,绝不能当成新事件推出去"
    assert notifier.sent == []


def test_少量时间戳兜底不影响正常推送(db):
    """只有个别记录缺时间戳时照常处理 —— 丢批阈值是 50%,不能一条失败就全丢"""
    _add_ready("uA", "alice")
    items = [_swap(f"g{i}") for i in range(5)]
    del items[0]["timestamp"]      # 1/5 = 20%,低于阈值

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=items, transfers=[], thesis=[], balances=[]),
    })
    n = Poller(client, FakeNotifier()).tick()
    assert n == 5, f"只有 1/5 兜底,不该丢批,实际落库 {n} 条"


# ============================================================
# 计价币
# ============================================================
def test_计价币不进stats也不算共识(db):
    """验收用例 #7:$SOL 的共识数若不排除会恒等于名单人数,功能 B 整体变噪音"""
    from tests.conftest import CA_WSOL

    _add_ready("uA", "alice")
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("q1", ca=CA_WSOL)],
                           transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    with store.get_conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 1, \
            "计价币事件照常落库照常推送 —— 排除的只是 A/B 判定"
        assert c.execute("SELECT COUNT(*) n FROM user_token_stats").fetchone()["n"] == 0
    assert notifier.sent and "🌱" not in notifier.sent[0]
    assert "人买过" not in notifier.sent[0]


def test_事件按时间升序处理(db):
    """
    ⚠️ 乱序时同一个币的第二笔买入可能先被判定,真正的第一笔反而拿到 ADD。
       徽章落库即冻结、永不重算,打错就是永久的。
    """
    _add_ready("uA", "alice")
    early_ms = int((datetime.now(UTC) + timedelta(seconds=30)).timestamp() * 1000)
    late_ms = int((datetime.now(UTC) + timedelta(seconds=90)).timestamp() * 1000)
    # 故意把晚的放前面
    client = FakeClient({
        "uA": UserSnapshot("uA",
                           swaps=[_swap("late", ms=late_ms), _swap("early", ms=early_ms)],
                           transfers=[], thesis=[], balances=[]),
    })
    Poller(client, FakeNotifier()).tick()

    with store.get_conn() as c:
        rows = {r["event_id"]: r["badge"] for r in
                c.execute("SELECT event_id, badge FROM fomo_events").fetchall()}
    early_badge = next(v for k, v in rows.items() if "early" in k)
    late_badge = next(v for k, v in rows.items() if "late" in k)
    assert early_badge == "FIRST", "时间上更早的那笔才是首次建仓"
    assert late_badge == "ADD"


def test_同一事件重复轮询不会重复计数(db):
    """INSERT OR IGNORE 返回 False 时绝不能再 upsert_stats,否则 buy_count 一路虚增"""
    _add_ready("uA", "alice")
    snap = UserSnapshot("uA", swaps=[_swap("dup1")], transfers=[], thesis=[], balances=[])
    client = FakeClient({"uA": snap})
    p = Poller(client, FakeNotifier())

    assert p.tick() == 1
    assert p.tick() == 0, "第二轮拉到同一笔,不该产生新事件"

    with store.get_conn() as c:
        row = c.execute("SELECT buy_count FROM user_token_stats").fetchone()
    assert row["buy_count"] == 1, f"重复轮询把 buy_count 累加到了 {row['buy_count']}"


def test_稳定币互换落库但不推送(db):
    """
    两侧都是计价币的兑换(USDT→USDC 等)既不是建仓也不是离场,
    「🟢 加仓 $USDC」零信号价值、纯占屏 —— 落库保留回溯能力,但不推 TG。

    ⚠️ 关键在于落库时就要把 sent 置 1。只是"跳过发送"的话 sent 永远是 0,
       补发队列会每 20 秒把它捞出来重试、连续捞 10 分钟,比推出去还费。
    """
    from tests.conftest import CA_USDC, CA_WSOL

    _add_ready("uA", "alice")
    two_leg = {
        "id": "q1",
        "networkId": "solana",
        "inTokenAddress": CA_WSOL, "inNetworkId": "solana", "inHumanAmount": 1.0,
        "outTokenAddress": CA_USDC, "outNetworkId": "solana", "outHumanAmount": 200.0,
        "humanUsdAmountIn": 200.0, "humanUsdAmountOut": 200.0,
        "timestamp": _FUTURE_MS,
    }
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[two_leg], transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    assert notifier.sent == [], f"稳定币互换不该推送,实际发了:{notifier.sent}"
    with store.get_conn() as c:
        row = c.execute("SELECT sent, event_type FROM fomo_events").fetchone()
        assert row is not None, "必须落库(要保留'他当时是不是在备钱'的回溯能力)"
        assert row["sent"] == 1, "落库时就要标记已处理,否则会被补发队列反复捞"
        assert store.load_unsent_recent(c, minutes=10) == [], "绝不能进补发队列"
        n = c.execute("SELECT COUNT(*) n FROM user_token_stats").fetchone()["n"]
        assert n == 0, "计价币不进 stats"


def test_停机后只发一条汇总而不是逐条推送(db):
    """
    用户拿自己的电脑跑、晚上关机。第二天开机时积压着一整夜的事件
    (实测 68 人名单约 857 条/天,停 8 小时近 300 条)。
    逐条推要发一个多小时,而且信息早就过期了。

    正确行为:事件**照常入库**(共识计数/首次建仓/hot 榜单都读库,数据不受影响),
    但只发一条汇总。
    """
    _add_ready("uA", "alice")
    with store.get_conn() as c:
        with store.tx(c):
            # 上一轮是 8 小时前 → 超过默认 45 分钟阈值
            store.set_state(c, "last_tick_at",
                            (datetime.now(UTC) - timedelta(hours=8)).isoformat(timespec="seconds"))

    items = [_swap(f"n{i}") for i in range(6)]
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=items, transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    n = Poller(client, notifier).tick()

    assert n == 6, "事件必须照常入库 —— 共识计数与 /hot 都靠它"
    assert len(notifier.sent) == 1, f"只该发一条汇总,实际发了 {len(notifier.sent)} 条"
    assert "停机期间汇总" in notifier.sent[0]

    with store.get_conn() as c:
        unsent = store.load_unsent_recent(c, minutes=10)
        assert unsent == [], "⚠️ 必须 mark_sent,否则补发队列会把积压一条条捞出来重推"
        assert c.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 6


# ============================================================
# 跟单:护栏(这些是无人值守自动成交的前提,见 tasks/todo.md A 档)
# ============================================================
_CA2 = "BBa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpum2"
_CA3 = "CCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpum3"
_CA4 = "DDa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpum4"


def _enable_copy(**kw) -> None:
    from src.copytrade import CopyConfig

    with store.get_conn() as c:
        store.save_copy_config(c, CopyConfig(
            **{"enabled": True, "paper_only": True, "min_buyers": 1,
               "max_age_hours": None, **kw}))


def _signals() -> list[dict]:
    with store.get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT token_address, status, amount_usd FROM copytrade_signals")]


def _stale_tick(hours: float) -> None:
    with store.get_conn() as c, store.tx(c):
        store.set_state(c, "last_tick_at",
                        (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds"))


def test_停机补数那一轮不生成跟单信号(db):
    """
    ⚠️ 推送侧承认积压过期、降级成一条汇总;跟单侧要是照常判定,
       就是拿**今天**的市值去买**昨晚**那批已经走完的信号。
       ($Plumber:第 2 个人进 31.62x,第 5 个人进 0.74x —— 晚一步是完全不同的生意)
    """
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(8)

    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_swap("s1")], transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    assert _signals() == [], "补数轮必须整段跳过跟单"


def test_正常轮照常生成跟单信号(db):
    """上一条的对照组 —— 否则「不生成」可能只是因为压根没跑通"""
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(0.01)     # 半分钟前,远低于 45 分钟阈值

    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_swap("s1")], transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    assert len(_signals()) == 1, "正常轮必须能跟出信号,否则上一条测试是假绿"


def test_一轮内命中多个币不会捅穿当日笔数上限(db):
    """
    ⚠️ taken_today 是循环**外**查的。不在循环里同步自增的话,
       一个 tick 命中 15 个币就是 15 单全过 —— 当日上限形同虚设。
    """
    _add_ready("uA", "alice")
    _enable_copy(daily_max=2)
    _stale_tick(0.01)

    swaps = [_swap("s1", ca=CA_TOAD), _swap("s2", ca=_CA2),
             _swap("s3", ca=_CA3), _swap("s4", ca=_CA4)]
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=swaps, transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    assert len(_signals()) == 2, f"上限 2 单,实际 {len(_signals())} 单"


def test_一轮内命中多个币不会捅穿当日金额上限(db):
    """笔数上限管不住钱:改了单笔金额忘了改笔数,当天敞口就是静默翻倍"""
    _add_ready("uA", "alice")
    # 纸上模式不出账,所以这里用 paper_only=False 让状态落到 pending(计入 SPENDING_STATUSES)
    _enable_copy(paper_only=False, daily_max=0, amount_usd=40.0, daily_spend_usd=100.0)
    _stale_tick(0.01)

    swaps = [_swap("s1", ca=CA_TOAD), _swap("s2", ca=_CA2),
             _swap("s3", ca=_CA3), _swap("s4", ca=_CA4)]
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=swaps, transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    spent = sum(s["amount_usd"] for s in _signals())
    assert spent <= 100.0, f"当日上限 $100,实际记了 ${spent}"
    assert len(_signals()) == 2, "$40 一单,$100 上限 → 只能下 2 单"


def test_无人值守时入队而不是推确认按钮(db, monkeypatch):
    """
    ⚠️ 自动模式**不能走 pending**:那样会同时存在「排队中」和「可点确认」两个入口,
       人点一次、队列再跑一次,而两条路的终态会互相覆盖。
    ⚠️ 也不能在 tick 里就地执行 —— 一笔买入几十秒,轮询间隔才 15s。
    """
    from src import copyworker

    _add_ready("uA", "alice")
    _enable_copy(paper_only=False, dry_run_execute=False, auto_execute=True,
                 amount_usd=40.0, daily_spend_usd=200.0)
    _stale_tick(0.01)

    jobs = []
    monkeypatch.setattr(copyworker.CopyWorker, "submit",
                        lambda self, job: jobs.append(job) or True)

    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_swap("s1")], transfers=[], thesis=[], balances=[])})
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    assert len(jobs) == 1, "应当入队一单"
    assert jobs[0].amount_usd == 40.0
    assert _signals()[0]["status"] == copyworker.ST_QUEUED, "状态必须是 auto_queued,不是 pending"
    assert not any("确认买入" in s for s in notifier.sent), "无人值守不该再推确认按钮"


def test_队列满了要记failed并告警而不是安静丢掉(db, monkeypatch):
    """
    ⚠️ 留在 auto_queued 的话,这个币因为主键冲突**再也不会被跟**,
       而没有任何人知道它丢了。
    """
    from src import copyworker

    _add_ready("uA", "alice")
    _enable_copy(paper_only=False, dry_run_execute=False, auto_execute=True,
                 amount_usd=40.0, daily_spend_usd=200.0)
    _stale_tick(0.01)
    monkeypatch.setattr(copyworker.CopyWorker, "submit", lambda self, job: False)

    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_swap("s1")], transfers=[], thesis=[], balances=[])})
    notifier = FakeNotifier()
    Poller(client, notifier).tick()

    assert _signals()[0]["status"] == "failed"
    assert any("未提交" in s for s in notifier.sent), f"必须告警:{notifier.sent}"


def test_入场市值取事件里的成交价(db):
    """
    ⚠️ 名单里那个人**刚刚**就是在这个价位买的 —— 这是最贴近我们跟进去时
       实际价位的数。原实现优先读 token_snapshot,而那张表清仓后就冻住了。
    """
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(0.01)
    # 快照里塞一个明显不同的旧值,证明取的不是它
    with store.get_conn() as c:
        store.upsert_token_snapshots(c, [("solana", CA_TOAD, "TOAD", 1.0, 999_999_999)])

    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_swap("s1")], transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    with store.get_conn() as c:
        got = c.execute("SELECT entry_mcap FROM copytrade_signals").fetchone()["entry_mcap"]
    assert got == 19_140_000, f"应取事件里的成交市值,实得 {got}"


def test_快照太旧就不拿来当入场价(db):
    """拿几小时前的低市值当「现在的价」,会让市值上限放行本该拦掉的币"""
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(0.01)
    with store.get_conn() as c, store.tx(c):
        # 一条 3 小时前的快照 —— 超过 SNAPSHOT_FRESH_MIN
        c.execute(
            "INSERT INTO token_snapshot(network_id, token_address, symbol, price_usd,"
            " market_cap, updated_at) VALUES (?,?,?,?,?,?)",
            ("solana", CA_TOAD, "TOAD", 1.0, 50_000.0,
             (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")))

    swap = _swap("s1")
    del swap["marketCap"]           # 事件里也没有 → 只剩那条旧快照
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[swap], transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    assert _signals() == [], "只有过期快照时应当不跟,而不是拿旧价建仓"


def test_拿不到入场市值就不跟(db):
    """与「拿不到币龄就不跟」同一条原则:宁可漏一单,不可蒙着眼建仓"""
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(0.01)

    swap = _swap("s1")
    del swap["marketCap"]
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[swap], transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    assert _signals() == []


def test_单个币处理失败不影响本轮其余币(db, monkeypatch):
    """
    ⚠️ 循环体里马上要接真实下单,而执行器有十几处 raise。没有 per-token 兜底的话,
       一个币出事会掀掉本轮剩下所有币,而外层那句「不影响推送」把它伪装成无害。
    """
    _add_ready("uA", "alice")
    _enable_copy()
    _stale_tick(0.01)

    real = store.count_recent_buyers

    def boom(conn, net, ca, *a, **kw):
        if ca == CA_TOAD:
            raise RuntimeError("模拟执行器炸了")
        return real(conn, net, ca, *a, **kw)

    monkeypatch.setattr(store, "count_recent_buyers", boom)

    swaps = [_swap("s1", ca=CA_TOAD), _swap("s2", ca=_CA2), _swap("s3", ca=_CA3)]
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=swaps, transfers=[], thesis=[], balances=[])})
    Poller(client, FakeNotifier()).tick()

    got = {s["token_address"] for s in _signals()}
    assert CA_TOAD not in got, "炸掉的那个币不该有信号"
    assert got == {_CA2, _CA3}, f"其余币必须照常处理,实际 {got}"


def test_间隔正常时不走汇总模式(db):
    """刚跑过一轮(1 分钟前)就该正常逐条推,别把日常推送误当成积压吞掉"""
    _add_ready("uA", "alice")
    with store.get_conn() as c:
        with store.tx(c):
            store.set_state(c, "last_tick_at",
                            (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"))

    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()
    assert len(notifier.sent) == 1
    assert "停机期间汇总" not in notifier.sent[0], "正常间隔被误判成停机了"
    assert "🌱" in notifier.sent[0]


def test_首次运行不走汇总模式(db):
    """
    全新的库没有 last_tick_at。此时游标也是 /add 时刚设的,本来就不会有积压 ——
    误判成"停机"会把第一批正常事件吞成一条汇总。
    """
    _add_ready("uA", "alice")
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("a1")], transfers=[], thesis=[], balances=[]),
    })
    notifier = FakeNotifier()
    Poller(client, notifier).tick()
    assert notifier.sent and "停机期间汇总" not in notifier.sent[0]

    # 跑完一轮后必须记下时间,否则下一轮又认不出间断
    with store.get_conn() as c:
        assert store.get_state(c, "last_tick_at"), "tick 结束后没有记录 last_tick_at"


def _thesis_item(uid: str, ca: str, text: str = "看好") -> dict:
    """一条 /feed/token/thesis 记录(实测结构:正文与代币标识都在嵌套的 comment 里)"""
    return {
        "type": "thesis",
        "id": f"th-{uid}-{ca[:6]}",
        "createdAt": _FUTURE.isoformat().replace("+00:00", "Z"),
        "userId": uid,
        "userHandle": "someone",
        "comment": {"comment": text, "tokenAddress": ca, "networkId": 1399811149},
        "authorTrade": {"usdValue": 100.0, "unrealizedPnlUsd": 5.0},
    }


class ThesisClient(FakeClient):
    """记录 get_token_thesis 被问过哪些币,并按币返回预置的观点"""

    def __init__(self, by_token=None, boom=None):
        super().__init__({})
        self.asked: list[str] = []
        self.by_token = by_token or {}
        self.boom = boom

    def get_token_thesis(self, token_address, network_id, after_ms=None, limit=100):
        self.asked.append(token_address)
        if self.boom:
            raise self.boom
        return self.by_token.get(token_address, [])


def _run_thesis(p, c, users):
    """
    跑完整条观点链路(选批 → 拉取 → 归一化)。
    ⚠️ tick() 里这三步是拆开的:中间那步跑在后台线程,与快照采集并行。
    """
    batch = p._pick_thesis_batch()
    return p._collect_thesis(c, users, batch, p._fetch_thesis_raw(batch))


def _seed_meta(p, n: int) -> list[str]:
    """预置 n 个代币的 _token_meta(network_raw 必须非 None,否则会被跳过)"""
    cas = [f"CA{i:03d}" for i in range(n)]
    p._token_meta = {("solana", ca): {"network_raw": 1399811149} for ca in cas}
    return cas


def test_观点轮转扫描不会漏币(db):
    """
    ⚠️ 持仓币数超过每轮上限时靠轮转覆盖。轮转游标若不前进,
       后面那些币的观点**永远看不到**,而且没有任何报错 —— 只是安静地少了。
    """
    _add_ready("uA", "alice")
    p = Poller(ThesisClient(), FakeNotifier())
    _seed_meta(p, 60)

    with store.get_conn() as c:
        users = store.list_active_users(c)
        _run_thesis(p, c, users)
        first = list(p.client.asked)
        p.client.asked.clear()
        _run_thesis(p, c, users)
        second = list(p.client.asked)

    from src.poller import _THESIS_TOKENS_PER_TICK as N
    assert len(first) == N and len(second) == N
    assert first != second, "轮转游标没前进,每轮都在扫同一批币"
    assert not (set(first) & set(second)), "两轮扫的币不该重叠(60 个币、每轮 25 个)"


def test_观点必须按监控名单过滤(db):
    """
    ⚠️ 按币查观点会把**该币下所有人**的观点都拉回来(热门币一次 100 条)。
       不按 userId 过滤就会把陌生人的观点当成监控对象推给用户。
    """
    _add_ready("uA", "alice")
    p = Poller(ThesisClient(), FakeNotifier())
    cas = _seed_meta(p, 1)
    p.client.by_token = {
        cas[0]: [
            _thesis_item("uA", cas[0], "监控对象发的"),
            _thesis_item("stranger", cas[0], "陌生人发的"),
        ]
    }
    with store.get_conn() as c:
        evs = _run_thesis(p, c, store.list_active_users(c))

    assert len(evs) == 1, f"应当只留下监控对象那条,实际 {len(evs)} 条"
    assert evs[0].user_id == "uA"
    assert "监控对象" in (evs[0].thesis_text or "")


def test_观点采集遇到登录态失效必须上抛(db):
    """AuthError 若被 `failed+=1` 吞掉,登录态挂了也不会告警(与 poller 主路径同一个洞)"""
    from src.auth import AuthError

    _add_ready("uA", "alice")
    p = Poller(ThesisClient(boom=AuthError("token 过期")), FakeNotifier())
    _seed_meta(p, 3)
    with store.get_conn() as c, pytest.raises(AuthError):
        _run_thesis(p, c, store.list_active_users(c))


def test_事件类型与去重键(db):
    """落库的事件类型必须是 BUY,且 event_id 用了原生 id"""
    _add_ready("uA", "alice")
    client = FakeClient({
        "uA": UserSnapshot("uA", swaps=[_swap("native-1")], transfers=[], thesis=[], balances=[]),
    })
    Poller(client, FakeNotifier()).tick()
    with store.get_conn() as c:
        row = c.execute("SELECT event_id, event_type, ingested_at FROM fomo_events").fetchone()
    assert row["event_type"] == EVENT_BUY
    assert "native-1" in row["event_id"]
    assert row["ingested_at"] <= now_iso()


# ============================================================
# 两段式增量采集(_fetch_snapshots)
# ⚠️ 这一段全是"少拉了会怎样"的验收:
#    多拉只是慢,少拉会让消息缺行、共识数出错,而且都不报错。
# ============================================================
class CountingClient(FakeClient):
    """记录每个端点被谁调过几次"""

    def __init__(self, snaps=None, **kw):
        super().__init__(snaps or {}, **kw)
        self.calls: dict[str, list[str]] = {"swaps": [], "balances": [], "trades": []}

    def get_swaps(self, user_id: str, limit: int = 50):
        self.calls["swaps"].append(user_id)
        return super().get_swaps(user_id, limit)

    def get_balances(self, user_id: str):
        self.calls["balances"].append(user_id)
        return super().get_balances(user_id)

    def get_trades(self, user_id: str):
        self.calls["trades"].append(user_id)
        return super().get_trades(user_id)


_MISSING = object()   # ⚠️ 不能拿 None 当"没传":None 在这里是有意义的取值(拉取失败)


def _snap(uid, swaps=_MISSING, balances=_MISSING, trades=_MISSING):
    d = {"swaps": swaps, "balances": balances, "trades": trades}
    return UserSnapshot(uid, transfers=[], thesis=[],
                        **{k: ([] if v is _MISSING else v) for k, v in d.items()})


def test_没有新swap的人第二轮不再拉balances和trades(db):
    """
    这是把单轮从 ~35s 压到个位数秒的核心:全员 swaps 每轮都拉(便宜且不可省),
    balances/trades 只给有动静的人。回归成"每轮全拉"不会有任何报错,只是又变慢。
    """
    for i in range(6):
        _add_ready(f"u{i}", f"h{i}")
    client = CountingClient({f"u{i}": _snap(f"u{i}") for i in range(6)})
    p = Poller(client, FakeNotifier())

    p.tick()                                   # 冷启动:必须全员拉一遍把缓存填满
    assert set(client.calls["balances"]) == {f"u{i}" for i in range(6)}

    client.calls = {"swaps": [], "balances": [], "trades": []}
    p.tick()                                   # 第二轮:没人有新 swap
    assert len(client.calls["swaps"]) == 6, "swaps 必须全员每轮拉,漏一个人就漏一笔交易"
    assert client.calls["trades"] == [], "没有新事件的人拉 trades 完全用不上"
    assert len(client.calls["balances"]) <= 6, "只应剩下轮转刷新的那几个"


def test_有新swap的人当轮必定重新拉balances和trades(db):
    """
    「已实现盈亏 / 剩余持仓 / 均价」只能从这个人本轮的 trades 里拿。
    漏拉不会报错 —— 只是那几行从消息里静静消失。
    """
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    snaps = {"uA": _snap("uA"), "uB": _snap("uB")}
    client = CountingClient(snaps)
    p = Poller(client, FakeNotifier())
    p.tick()

    client.calls = {"swaps": [], "balances": [], "trades": []}
    snaps["uA"] = _snap("uA", swaps=[_swap("new-1")])       # 只有 uA 有新动作
    p.tick()
    assert client.calls["trades"] == ["uA"]
    assert "uA" in client.calls["balances"]


def test_balances拉到空列表不会回落到旧缓存(db):
    """
    ⚠️ [] 是"拉到了、确实没有持仓",是有效值;None 才是"拉取失败"。
       组装时若写成 `bal_now.get(uid) or cache[uid]`,清仓的人会永远停在旧持仓上,
       「N 人仍持有」就再也减不下去。
    """
    _add_ready("uA", "alice")
    holding = [{"balance": {"tokenAddress": CA_TOAD, "tokenId": f"{CA_TOAD}:1399811149"},
                "tokenFilterResult": {"priceUSD": 1.0, "token": {"networkId": "solana"}},
                "userToken": {"humanAmountRemaining": 100.0}}]
    snaps = {"uA": _snap("uA", balances=holding)}
    p = Poller(CountingClient(snaps), FakeNotifier())
    p.tick()
    assert p._bal_cache["uA"] == holding

    snaps["uA"] = _snap("uA", swaps=[_swap("s2")], balances=[])   # 清仓了
    p.tick()
    assert p._bal_cache["uA"] == [], "空列表必须覆盖缓存,不能被当成假值丢掉"


def test_balances拉取失败才回落缓存(db):
    """拉取失败(None)时宁可用上一轮的持仓,也好过让「仍持有」整段消失"""
    _add_ready("uA", "alice")
    holding = [{"balance": {"tokenId": f"{CA_TOAD}:1399811149"},
                "tokenFilterResult": {"priceUSD": 1.0, "token": {"networkId": "solana"}},
                "userToken": {"humanAmountRemaining": 100.0}}]
    snaps = {"uA": _snap("uA", balances=holding)}
    p = Poller(CountingClient(snaps), FakeNotifier())
    p.tick()

    snaps["uA"] = _snap("uA", swaps=[_swap("s2")], balances=None)
    with store.get_conn() as c:
        got = p._fetch_snapshots(store.list_active_users(c))
    assert got["uA"].balances == holding


def test_移出名单的人不再占缓存(db):
    """69 人的 balances 原始体积约 8MB,删掉的人还留着就是纯泄漏"""
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    p = Poller(CountingClient({"uA": _snap("uA"), "uB": _snap("uB")}), FakeNotifier())
    p.tick()
    assert set(p._bal_cache) == {"uA", "uB"}

    with store.get_conn() as c:
        store.remove_watch_user(c, "bob")
    p.tick()
    assert set(p._bal_cache) == {"uA"}
    assert "uB" not in p._swap_seen


def test_swaps拉取失败不算有新动作也不污染记忆(db):
    """
    拉取失败 ≠ 没动静。若把失败当成"这一页就是全部"写进记忆,
    下一轮拉成功时那些条目会被当成"上一轮见过的",这个人的新交易就被永久跳过了。
    """
    _add_ready("uA", "alice")
    snaps = {"uA": _snap("uA", swaps=[_swap("s1")])}
    client = CountingClient(snaps)
    p = Poller(client, FakeNotifier())
    p.tick()
    assert p._swap_seen["uA"] == {"s1"}

    snaps["uA"] = _snap("uA", swaps=None)          # 本轮拉取失败
    p.tick()
    assert p._swap_seen["uA"] == {"s1"}, "记忆必须原样保留"

    client.calls = {"swaps": [], "balances": [], "trades": []}
    snaps["uA"] = _snap("uA", swaps=[_swap("s1"), _swap("s2")])
    p.tick()
    assert client.calls["trades"] == ["uA"], "恢复后 s2 必须被认出来是新的"


def test_补进来的更早swap也算有新动作(db):
    """
    跨链单两条腿到达时间不同,服务端会把一笔**更早**的 swap 补进列表。
    只比"最新一条变没变"会漏掉它 —— 那条消息的盈亏行就整行消失。
    """
    _add_ready("uA", "alice")
    late, early = _swap("late", ms=_FUTURE_MS), _swap("early", ms=_FUTURE_MS - 60_000)
    snaps = {"uA": _snap("uA", swaps=[late])}
    client = CountingClient(snaps)
    p = Poller(client, FakeNotifier())
    p.tick()

    client.calls = {"swaps": [], "balances": [], "trades": []}
    snaps["uA"] = _snap("uA", swaps=[late, early])     # 头一条没变,尾部补进来一条
    p.tick()
    assert client.calls["trades"] == ["uA"]


def test_轮转刷新会扫遍整个名单(db):
    """轮转是纠正"转账 / 价格跌破 dust 线"这两类看不见的漂移的唯一途径,漏人就是永远不纠正"""
    uids = [f"u{i:02d}" for i in range(20)]
    p = Poller(CountingClient(), FakeNotifier())
    seen: set[str] = set()
    for _ in range(20):
        seen |= p._rotate_balances(uids, hot=set())
    assert seen == set(uids)


def test_轮转不重复拉已经因交易刷新过的人(db):
    p = Poller(CountingClient(), FakeNotifier())
    uids = [f"u{i}" for i in range(10)]
    hot = {"u0", "u1", "u2"}
    assert not (p._rotate_balances(uids, hot) & hot)


# ============================================================
# 推送令牌桶(_throttle_send)
# ============================================================
def test_小批量推送不再逐条硬等(db):
    """
    原来固定 sleep(interval) 的写法下,一轮 6 条要凭空多花 5×interval,
    而这段等待整个计入单轮 tick 耗时 —— 正是日志里 67s 那种 tick 的来源。
    """
    p = Poller(CountingClient(), FakeNotifier())
    p.settings.fomo_send_interval_sec = 5.0
    t0 = time.monotonic()
    for _ in range(_SEND_BURST):
        p._throttle_send()
    assert time.monotonic() - t0 < 0.5, "桶容量之内必须一条都不等"


def test_超过桶容量后退化为匀速(db):
    """桶不能是"每轮重置",否则连续几轮爆量的实际速率会超过 TG 软限,反而挨 429"""
    p = Poller(CountingClient(), FakeNotifier())
    p.settings.fomo_send_interval_sec = 0.05
    for _ in range(_SEND_BURST):
        p._throttle_send()
    t0 = time.monotonic()
    p._throttle_send()
    assert time.monotonic() - t0 >= 0.04


def test_推送间隔配成0时不等待(db):
    p = Poller(CountingClient(), FakeNotifier())
    p.settings.fomo_send_interval_sec = 0
    t0 = time.monotonic()
    for _ in range(20):
        p._throttle_send()
    assert time.monotonic() - t0 < 0.2



def test_同一轮不会把同一个人的balances拉两遍(db):
    """
    ⚠️ 补漏池要按"第一池实际拉过谁"扣,不能按"活动流点过谁"扣。
       冷启动时活动流点名集是空的,而第一池已经全员拉过一遍 ——
       按后者扣会把 69 次 balances 整整重打一遍,冷启动直接翻倍。
    """
    for i in range(8):
        _add_ready(f"u{i}", f"h{i}")
    client = CountingClient({f"u{i}": _snap(f"u{i}", swaps=[_swap(f"s{i}")]) for i in range(8)})
    Poller(client, FakeNotifier()).tick()

    got = client.calls["balances"]
    assert len(got) == len(set(got)) == 8, f"每人一次,实际 {len(got)} 次"


# ============================================================
# 对抗性审查确认的三个缺陷(2026-08-11 多视角审查)
# ============================================================
def test_有新交易的人下一轮必须重拉balances(db):
    """
    ⚠️ 这条修的是「N 人仍持有」系统性偏小。
       有新交易的人,他的 balances 与 swaps 是同一个池子里**并发**发出的,
       拿回来必然是服务端还没索引到这笔交易的旧持仓。这份最不准的快照一旦进缓存,
       下一轮他既不 hot 也多半轮不到轮转,错误状态就被冻结整整一个轮转周期 ——
       而那一两分钟正是名单集体抢同一个新币、这个数字最该准的时刻。
    """
    for i in range(30):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    snaps = {f"u{i:02d}": _snap(f"u{i:02d}") for i in range(30)}
    client = CountingClient(snaps)
    p = Poller(client, FakeNotifier())
    p.tick()                                          # 冷启动

    snaps["u07"] = _snap("u07", swaps=[_swap("new-1")])   # u07 买入
    p.tick()
    assert "u07" in p._bal_dirty, "有新动作的人必须被挂上脏标记"

    client.calls["balances"] = []
    p.tick()                                          # 这一轮他已经不 hot 了
    assert "u07" in client.calls["balances"], \
        "上一轮有交易的人,这一轮必须重拉一次 balances,否则错误持仓会被冻结一整圈"

    client.calls["balances"] = []
    p.tick()
    assert "u07" not in p._bal_dirty, "脏标记必须是一次性的,不能永久占住配额"


def test_不可并发的实现绝不把观点采集丢进后台线程(db):
    """
    ⚠️ playwright 实现按 threading.local 私有创建整套 chromium,线程退出时不回收。
       把观点采集丢进每轮新建的后台线程 = 每 tick 泄漏一套浏览器(约 300MB),
       12s 一轮几分钟就吃光内存。这与 _fetch_snapshots 里那道守卫是同一个故障,
       只是换了个入口 —— 所以这里必须独立钉一次。
    """
    _add_ready("uA", "alice")

    class SerialClient(CountingClient):
        supports_concurrency = False

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.thesis_threads: set[str] = set()

        def get_token_thesis(self, ca, net, after_ms=None, limit=100):
            self.thesis_threads.add(threading.current_thread().name)
            return []

    client = SerialClient({"uA": _snap("uA")})
    p = Poller(client, FakeNotifier())
    p._token_meta = {("solana", CA_TOAD): {"network_raw": 1399811149}}
    p.tick()

    assert client.thesis_threads, "观点采集本身要真的跑到"
    assert not any(n.startswith("fomo-thesis") for n in client.thesis_threads), \
        f"不可并发的实现必须留在主线程,实际跑在 {client.thesis_threads}"
    assert p._thesis_pool is None, "更不该为它建后台线程池"


def test_观点线程池跨tick复用而不是每轮新建(db):
    """每轮新建线程 = 每轮重做 TLS 握手(curl 会话是 threading.local 的)"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")})
    p = Poller(client, FakeNotifier())
    p._token_meta = {("solana", CA_TOAD): {"network_raw": 1399811149}}
    p.tick()
    pool = p._thesis_pool
    assert pool is not None
    p.tick()
    assert p._thesis_pool is pool, "线程池必须跨 tick 复用"


def test_没有代币要扫时不建线程池(db):
    """冷启动第一轮 _token_meta 是空的,不该为此白起一条线程"""
    _add_ready("uA", "alice")
    p = Poller(CountingClient({"uA": _snap("uA")}), FakeNotifier())
    p.tick()
    assert p._thesis_pool is None


def test_swaps端点先于活动流发现的交易也要挂脏标记(db):
    """另一条信号来源:活动流没提,但兜底扫描的 swaps 差集发现了新交易"""
    for i in range(30):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    snaps = {f"u{i:02d}": _snap(f"u{i:02d}") for i in range(30)}
    client = CountingClient(snaps)
    p = Poller(client, FakeNotifier())
    p.tick()

    # 活动流一直是空的(模拟"这个人没被关注"),只有 swaps 差集能发现
    swept = None
    for _ in range(10):
        p.tick()
        if "u07" in p._swaps_attempted:
            swept = True
            break
    assert swept, "兜底轮转应当扫到 u07"

    snaps["u07"] = _snap("u07", swaps=[_swap("late-1")])
    for _ in range(10):
        p.tick()
        if "u07" in p._bal_dirty:
            break
    assert "u07" in p._bal_dirty, "swaps 差集发现的新交易同样要挂脏标记"


def test_冷启动不把全员挂成脏(db):
    """
    冷启动那轮没有差集基准,_hot_users 会把所有人判成 hot —— 但那不是"所有人刚交易过"。
    照单全收会让重启后的第二轮白白重拉一遍全员 balances(实测 2.4s → 5.9s)。
    """
    for i in range(20):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    client = CountingClient({f"u{i:02d}": _snap(f"u{i:02d}", swaps=[_swap(f"s{i}")])
                             for i in range(20)})
    p = Poller(client, FakeNotifier())
    p.tick()                                   # 冷启动
    assert not p._bal_dirty, f"冷启动不该把全员挂脏,实际 {len(p._bal_dirty)} 人"

    client.calls["balances"] = []
    p.tick()
    assert len(client.calls["balances"]) < 20, \
        f"第二轮不该重拉全员 balances,实际 {len(client.calls['balances'])} 次"




def test_冷启动不给全员补拉trades(db):
    """
    ⚠️ 冷启动是单轮请求量的峰值。再按"全员 hot"叠一份 trades 就是 3N 个请求
       砸在启动瞬间 —— 实测足以打爆限流窗口。
       而冷启动的 hot 只是"没有差集基准"的产物,并不代表这些人刚交易过。
    """
    from src.poller import _BALANCE_WARMUP_PER_TICK

    for i in range(20):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    snaps = {f"u{i:02d}": _snap(f"u{i:02d}", swaps=[_swap(f"s{i}")]) for i in range(20)}
    client = CountingClient(snaps)
    Poller(client, FakeNotifier()).tick()

    assert len(client.calls["swaps"]) == 20, "冷启动的全员 swaps 是必须的(它便宜)"
    # balances 是最重的端点,冷启动分批预热(见 test_冷启动分批预热balances而不是一次全打)
    assert len(client.calls["balances"]) <= _BALANCE_WARMUP_PER_TICK
    assert len(client.calls["trades"]) <= 1, \
        f"冷启动不该给全员补 trades,实际 {len(client.calls['trades'])} 次"


# ============================================================
# 全员每轮拉 swaps —— 用户实测「下单到推送 30s+」之后的定论
# ⚠️ 曾经用 /feed/tradingActivity 当"谁动了"的探针来省掉这一步。得不偿失,已回退:
#      · 只省 1~2s(实测全员 swaps 70 人 12 线程 1.2~3.6s)
#      · 却换来那个端点独立且严格的限流(每 10s 一次跑两分钟就持续 429)
#      · 而且它只覆盖"当前账号关注的人" —— 自己永远不在流里(实测 100 条中 0 条),
#        自己的交易只能靠兜底轮转,延迟 30~70s,正是用户量到的那个数
#    这一节钉死"没有例外、没有轮转、没有盲区"。
# ============================================================
def _feed_thesis_item(fid: str, uid: str, text: str = "看好这个", ca: str = CA_TOAD) -> dict:
    return {"type": "thesis", "id": fid, "userId": uid, "createdAt": "2026-08-11T15:00:00.000Z",
            "comment": {"comment": text, "tokenAddress": ca, "networkId": 1399811149},
            "authorTrade": {"usdValue": 500.0}}


def test_每轮都拉全员swaps没有例外(db):
    """谁都不许被"优化"掉 —— 包括自己、包括这一刻看起来没动静的人"""
    for i in range(30):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    everyone = {f"u{i:02d}" for i in range(30)}
    client = CountingClient({u: _snap(u) for u in everyone})
    p = Poller(client, FakeNotifier())
    for _ in range(3):
        client.calls["swaps"] = []
        p.tick()
        assert set(client.calls["swaps"]) == everyone, \
            f"少拉了 {sorted(everyone - set(client.calls['swaps']))}"


def test_活动流是低频补充不是每轮都调(db):
    """
    ⚠️ /feed/tradingActivity 的限流和 /v2/users/* 完全不是一个量级:
       每 10s 调一次跑两分钟就开始持续 429(retry-after: 0,重试也没用)。
       它现在只负责捞"发在老仓位上的观点",必须降频。
    """
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")})
    p = Poller(client, FakeNotifier())
    for _ in range(6):
        p.tick()
    assert client.feed_calls == 1, f"6 轮里只该调一次活动流,实际 {client.feed_calls} 次"


def test_活动流挂了不影响买卖推送(db):
    """买卖判定完全不依赖活动流 —— 它挂了只是少捞一批观点,不该有任何告警或降级"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA", swaps=[_swap("s1")])},
                            feed_boom=RuntimeError("活动流 429"))
    notifier = FakeNotifier()
    n = Poller(client, notifier).tick()
    assert n == 1 and len(notifier.sent) == 1
    assert set(client.calls["swaps"]) == {"uA"}


def test_活动流登录态失效仍然上抛(db):
    """限流可以吞,登录态不行 —— 吞掉就是一个看起来健康的、死掉的监控"""
    from src.auth import AuthError

    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")}, feed_boom=AuthError("token 过期"))
    with pytest.raises(AuthError):
        Poller(client, FakeNotifier()).tick()


def test_活动流捡到的观点能直接成事件(db):
    """流里的观点条目与 /feed/token/thesis 同形,normalize_thesis 直接吃得下"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")},
                            feed=[_feed_thesis_item("th-1", "uA", "这个币要起飞")])
    notifier = FakeNotifier()
    Poller(client, notifier).tick()
    assert any("这个币要起飞" in m for m in notifier.sent), f"实际发出:{notifier.sent}"


def test_同一条观点两条路都抓到也只推一次(db):
    """活动流与按币扫描会重叠。两边都用原生 id 生成 event_id,靠主键去重"""
    _add_ready("uA", "alice")
    raw = _feed_thesis_item("th-dup", "uA", "重复的观点")
    client = CountingClient({"uA": _snap("uA")}, feed=[raw])
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    p.tick()
    n_first = len(notifier.sent)

    p._token_meta = {("solana", CA_TOAD): {"network_raw": 1399811149}}
    client.get_token_thesis = lambda ca, net, after_ms=None, limit=100: [raw]
    p.tick()
    assert len(notifier.sent) == n_first, "同一条观点绝不能推两遍"


def test_刚买的币下一轮优先扫观点(db):
    """
    ⚠️ 观点几乎总是发在刚买的币上(实测:用户 10:05 买入、10:05 发观点)。
       按币轮转一圈约 400 个币 / 每轮 12 个 ≈ 6 分钟 —— 用户 10:09 才收到,
       观感就是"观点没被监控到"。优先名额就是为这个场景留的。
    """
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA", swaps=[_swap("s1")])})
    p = Poller(client, FakeNotifier())
    p.tick()

    key = ("solana", CA_TOAD)
    assert key in p._thesis_priority, "刚买的币必须进优先名额"

    # 塞满轮转池,验证优先名额确实能插队
    p._token_meta = {key: {"network_raw": 1399811149}}
    for i in range(200):
        p._token_meta[("solana", f"OTHER{i:03d}")] = {"network_raw": 1399811149}
    batch = p._pick_thesis_batch()
    assert any(b[1] == CA_TOAD for b in batch), \
        "刚买的币必须出现在本轮批次里,而不是等轮转慢慢转到"


def test_优先名额不会撑大每轮扫描总量(db):
    """插队可以,但不能把每轮调用量顶上去 —— 峰值并发是有上限的"""
    from src.poller import _THESIS_TOKENS_PER_TICK

    _add_ready("uA", "alice")
    p = Poller(CountingClient({"uA": _snap("uA")}), FakeNotifier())
    p._token_meta = {("solana", f"CA{i:03d}"): {"network_raw": 1399811149} for i in range(100)}
    deadline = time.monotonic() + 600
    for i in range(50):
        p._thesis_priority[("solana", f"CA{i:03d}")] = deadline
    assert len(p._pick_thesis_batch()) <= _THESIS_TOKENS_PER_TICK


def test_冷启动分批预热balances而不是一次全打(db):
    """
    ⚠️ /balances 是最重的端点(单个 100~400KB、p50 1.22s)。一启动就把全名单
       一次性打出去,源站直接 504 Gateway Timeout(不是限流,是 origin 超时)——
       而且重试也是 504,结果缓存反而填不满。用户日志里满屏的
       "FOMO 服务端错误 504 | …/balances" 就是这么来的。
    """
    from src.poller import _BALANCE_WARMUP_PER_TICK

    n = _BALANCE_WARMUP_PER_TICK * 3
    for i in range(n):
        _add_ready(f"u{i:03d}", f"h{i:03d}")
    client = CountingClient({f"u{i:03d}": _snap(f"u{i:03d}") for i in range(n)})
    p = Poller(client, FakeNotifier())

    p.tick()
    first = len(client.calls["balances"])
    assert first <= _BALANCE_WARMUP_PER_TICK, \
        f"冷启动一轮最多预热 {_BALANCE_WARMUP_PER_TICK} 个,实际打了 {first} 个"

    # 但必须真的在往前推进 —— 限量不能变成"永远填不满"
    for _ in range(6):
        p.tick()
    assert len(p._bal_cache) == n, f"几轮之内要把缓存填满,实际 {len(p._bal_cache)}/{n}"


def test_停机信号让在途请求立刻放弃(db):
    """
    Ctrl+C 之后线程池里还排着几十个请求,一个个跑完(每个还带重试退避)
    要等十几秒 —— 用户只能连按好几次 Ctrl+C。
    """
    from src.client import request_stop, reset_stop

    for i in range(20):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    client = CountingClient({f"u{i:02d}": _snap(f"u{i:02d}") for i in range(20)})
    p = Poller(client, FakeNotifier())
    request_stop()
    try:
        p.tick()
    finally:
        reset_stop()
    assert client.calls["swaps"] == [], "停机中排队的请求一个都不该发出去"


def test_close_关掉常驻线程池(db):
    """不关的话 atexit 会 join 它,而它可能卡在 HTTP 超时里 —— 进程就走不掉"""
    _add_ready("uA", "alice")
    p = Poller(CountingClient({"uA": _snap("uA")}), FakeNotifier())
    p._token_meta = {("solana", CA_TOAD): {"network_raw": 1399811149}}
    p.tick()
    assert p._thesis_pool is not None
    p.close()
    assert p._thesis_pool is None
    p.close()          # 幂等:cli 的 finally 里可能重复调


# ============================================================
# 上游 404:账号已不存在
# ⚠️ 实测过不做这件事的代价:2 个被删的账号被每 15 秒重试一次、连续 10 天,
#    约 11.5 万次注定失败的请求,而日志里只说"上游抖动"
# ============================================================
class GoneClient(FakeClient):
    """指定的人一律 404,其余正常"""

    def __init__(self, snaps, gone: set[str]):
        super().__init__(snaps)
        self.gone = gone
        self.calls: list[tuple[str, str]] = []

    def get_swaps(self, uid, **kw):
        self.calls.append(("swaps", uid))
        if uid in self.gone:
            raise UserGoneError("/v2/users/x/swaps HTTP 404: User not found")
        return super().get_swaps(uid, **kw)

    def get_balances(self, uid, **kw):
        self.calls.append(("balances", uid))
        if uid in self.gone:
            raise UserGoneError("/v2/users/x/balances HTTP 404: User not found")
        return super().get_balances(uid, **kw)


def _gone_setup(db):
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    snaps = {u: UserSnapshot(u, swaps=[], transfers=[], thesis=[], balances=[])
             for u in ("uA", "uB")}
    return GoneClient(snaps, {"uB"})


def test_上游404的人下一轮就不再拉了(db):
    """核心诉求:永久失败不能每轮重试。这是 11.5 万次废请求的来源"""
    client = _gone_setup(db)
    p = Poller(client, FakeNotifier())
    p.tick()
    assert any(u == "uB" for _, u in client.calls), "第一轮总要试一次才知道他没了"

    client.calls.clear()
    p.tick()
    assert not any(u == "uB" for _, u in client.calls), "第二轮起绝不能再拉他"
    assert any(u == "uA" for _, u in client.calls), "别把正常的人一起停了"


def test_404只告警一次(db):
    """⚠️ 这个状态会持续存在(实测 10 天)。每轮发一条 = TG 被刷爆 = 等于没有告警"""
    client = _gone_setup(db)
    n = FakeNotifier()
    p = Poller(client, n)
    for _ in range(3):
        p.tick()
    hits = [s for s in n.sent if "已不存在" in s]
    assert len(hits) == 1, f"只该告警一次,实际 {len(hits)} 次"
    assert "bob" in hits[0], "必须点名是谁,否则用户无从下手"


def test_404不自动移出名单(db):
    """
    ⚠️ 账号可能只是改名或临时不可见,而 /del 会连基线一起删掉 —— 不可逆。
       停止拉取是程序的事,删不删是用户的决定。
    """
    client = _gone_setup(db)
    Poller(client, FakeNotifier()).tick()
    with store.get_conn() as c:
        row = store.get_watch_user(c, "uB")
        assert row["active"] == 1, "绝不能自动踢出名单"
        assert row["missing_since"], "但要标记出来"
        assert len(store.list_active_users(c)) == 2, "共识分母仍是完整名单"
        assert [r["user_id"] for r in store.fetchable_users(c)] == ["uA"]


def test_404的人历史记录仍然算数(db):
    """他历史上的买入是真实发生过的事,不能因为账号后来没了就抹掉"""
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    with store.get_conn() as c:
        store.mark_user_missing(c, "uB")
        assert len(store.list_active_users(c)) == 2
        assert len(store.ready_user_ids(c)) == 2


def test_账号恢复后会重新开始采集(db):
    """改名/临时不可见会自己恢复 —— 一次误判不能变成永久失明"""
    from src import poller as pmod

    client = _gone_setup(db)
    p = Poller(client, FakeNotifier())
    p.tick()
    with store.get_conn() as c:
        assert store.get_watch_user(c, "uB")["missing_since"]

    client.gone.clear()                       # 上游恢复了
    monkey = pmod._MISSING_RECHECK_TICKS
    try:
        pmod._MISSING_RECHECK_TICKS = 1       # 别在测试里等一小时
        p.tick()
    finally:
        pmod._MISSING_RECHECK_TICKS = monkey
    with store.get_conn() as c:
        assert not store.get_watch_user(c, "uB")["missing_since"], "恢复后要撤掉标记"


# ============================================================
# 名单盈亏采集(Task 8)
# ⚠️ 这只是网页上的一列展示,买卖判定完全不依赖它 —— 失败必须静默降级,
#    绝不能让盈亏采集的异常影响推送或买卖判定。
# ============================================================
def _pnl_row(uid: str) -> dict:
    """
    一行形态完整的 leaderboard(period=following)返回,字段名取实测真实值:
    id / userHandle / displayName / totalPnL / pnl24h / pnl7d / pnl30d /
    totalHoldings / numTrades / totalVolume / swapCount。
    """
    return {
        "id": uid, "userHandle": f"h_{uid}", "displayName": uid,
        "totalPnL": 491_083.0, "pnl24h": 1234.5, "pnl7d": -144_223.0,
        "pnl30d": 20_316.0, "totalHoldings": 55_000.0, "numTrades": 17,
        "totalVolume": 999_999.0, "swapCount": 17,
    }


def test_名单盈亏采集提取字段与来源严格对应(db):
    """
    用与实测完全一致的字段名构造一行,确认 _maybe_poll_pnl 落库的字段
    不是错位、漏读,也没有把 None 悄悄填成 0。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA")}, leaderboard=[_pnl_row("uA")])
    p = Poller(client, FakeNotifier())
    with store.get_conn() as c:
        p._maybe_poll_pnl(c)          # 新建的 Poller._tick_no == 0,0 % 20 == 0,该轮必打
        row = store.load_user_pnl(c)[0]
    assert row["user_id"] == "uA"
    assert row["total_pnl"] == pytest.approx(491_083.0)
    assert row["pnl_24h"] == pytest.approx(1234.5)
    assert row["pnl_7d"] == pytest.approx(-144_223.0)
    assert row["pnl_30d"] == pytest.approx(20_316.0)
    assert row["total_holdings"] == pytest.approx(55_000.0)
    assert row["num_trades"] == 17


def test_名单盈亏缺字段时存None不存0(db):
    """⚠️ 0 是「不赚不亏」,None 是「拿不到」—— 上游没给的字段必须原样存 None"""
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA")}, leaderboard=[
        {"id": "uA", "userHandle": "alice"}  # 除了 id 什么都没给
    ])
    p = Poller(client, FakeNotifier())
    with store.get_conn() as c:
        p._maybe_poll_pnl(c)
        row = store.load_user_pnl(c)[0]
    assert row["total_pnl"] is None
    assert row["pnl_24h"] is None
    assert row["pnl_7d"] is None
    assert row["pnl_30d"] is None


def test_名单盈亏采集每20轮才调一次(db):
    """15s × 20 = 5 分钟一次,不能每轮都打这个接口"""
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA")}, leaderboard=[_pnl_row("uA")])
    p = Poller(client, FakeNotifier())
    for _ in range(19):
        p.tick()
    assert client.leaderboard_calls == 0, "前 19 轮不该调 leaderboard"
    p.tick()
    assert client.leaderboard_calls == 1, "第 20 轮该打一次"


def test_名单盈亏与活动流永不同轮触发(db):
    """
    ⚠️ _PNL_EVERY_N_TICKS=20 与 _FEED_EVERY_N_TICKS=6 并不互质(gcd=2)——
       两者错开完全靠调用顺序:_poll_feed 的降频判断在 _tick_no 自增**之前**,
       _maybe_poll_pnl 的判断排在 tick() 里更晚的位置,在自增**之后**。
       这条测试把这个隐性依赖钉死:谁把其中一次判断挪到自增的另一侧,
       活动流与名单盈亏采集就可能撞进同一轮(两个各自有独立限流的网络请求
       叠在一起打),这里会变红,而不是在生产环境里悄悄退化。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA")}, leaderboard=[_pnl_row("uA")])
    p = Poller(client, FakeNotifier())
    both_fired_together = False
    for _ in range(120):  # LCM(6, 20) = 60,跑两圈以上留足余量
        before_feed, before_lb = client.feed_calls, client.leaderboard_calls
        p.tick()
        fired_feed = client.feed_calls > before_feed
        fired_lb = client.leaderboard_calls > before_lb
        if fired_feed and fired_lb:
            both_fired_together = True
    assert not both_fired_together, "活动流与名单盈亏采集在同一轮同时触发了"
    # 两条低频任务各自确实按周期触发过 —— 不是因为都没触发才"没撞上"
    assert client.feed_calls > 0
    assert client.leaderboard_calls > 0


def test_名单盈亏采集失败不影响买卖推送(db):
    """
    挂了只是这轮没更新盈亏展示列,买卖判定和推送必须照常。

    ⚠️ review 抓到的坑:新建的 Poller._tick_no 从 0 起步,tick() 里
       _fetch_snapshots 先把它自增到 1 才轮到 _maybe_poll_pnl 检查,
       1 % 20 != 0,第一轮根本不会调 get_leaderboard —— leaderboard_boom
       从未被触发,这条测试原来是假的(删掉 tick() 里包 _maybe_poll_pnl 的
       try/except,424 个测试照样全绿)。
       把 _tick_no 手动拨到 19,tick() 里自增到 20 后 20 % 20 == 0,
       这一轮才会真的调用 get_leaderboard、真的抛出 leaderboard_boom。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", swaps=[_swap("s1")])},
                        leaderboard_boom=RuntimeError("leaderboard 500"))
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    p._tick_no = 19        # 本轮 _fetch_snapshots 自增后正好落在 20
    n = p.tick()
    assert client.leaderboard_calls == 1, "没真的调到采集,这条测试就是假的"
    assert n == 1 and len(notifier.sent) == 1, "盈亏采集失败绝不能挡住买卖推送"


def test_名单盈亏采集不支持时静默跳过(db):
    """
    playwright 等实现若不支持该端点,NotSupportedError 必须被吞掉,不是告警也不是崩溃。

    ⚠️ 同上一条的坑:必须把 _tick_no 拨到 19,否则第一轮直接被降频门槛挡住,
       get_leaderboard 根本没被调过,NotSupportedError 也就无从触发。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", swaps=[_swap("s1")])},
                        leaderboard_boom=NotSupportedError("测试用:不支持榜单"))
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    p._tick_no = 19        # 本轮 _fetch_snapshots 自增后正好落在 20
    n = p.tick()
    assert client.leaderboard_calls == 1, "没真的调到采集,这条测试就是假的"
    assert n == 1 and len(notifier.sent) == 1
    with store.get_conn() as c:
        assert store.load_user_pnl(c) == [], "没能力拉就该是没有数据,不是报错"


# ============================================================
# 价格历史采样(feat/price-history)
# ⚠️ 只采 self._token_meta 里当前有的币(名单还持有的),零额外 API 调用;
#    失败必须静默降级,绝不能让采样/清理的异常影响推送或买卖判定。
# ============================================================
def _bal(ca: str, *, price: float | None = 1.0, mcap: float | None = 1_000_000.0,
        network: str = "solana") -> dict:
    """一条形态完整的 balances 记录,含 marketCap —— _build_token_index 的市值只从这里来"""
    return {
        "balance": {"tokenAddress": ca, "tokenId": f"{ca}:1399811149"},
        "tokenFilterResult": {"priceUSD": price, "marketCap": mcap,
                              "token": {"networkId": network}},
        "userToken": {"humanAmountRemaining": 100.0},
    }


_ANCIENT = "2020-01-01T00:00:00+00:00"


def _n_and_offset(p: Poller) -> tuple[int, int]:
    """真实配置里的采样周期与错峰偏移 —— 全部测试都从这里取,不写死具体数字。"""
    n = p.settings.fomo_price_history_sample_ticks
    return n, n // 2


def test_价格采样只在降频轮触发(db):
    """
    不写死"20 轮"这个曾经的默认值 —— 直接从 p.settings 读真实配置算出该在第几次
    tick() 调用触发,配置的默认值以后再改,这条测试也不需要跟着改数字。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD)])})
    p = Poller(client, FakeNotifier())
    n, offset = _n_and_offset(p)
    for _ in range(offset - 1):
        p.tick()
    with store.get_conn() as c:
        assert store.load_price_history(c, "solana", CA_TOAD, _ANCIENT) == [], \
            f"前 {offset - 1} 轮都不该落库"
    p.tick()   # 第 offset 次调用,_tick_no 自增到 offset,offset % n == offset,该轮必采
    with store.get_conn() as c:
        rows = store.load_price_history(c, "solana", CA_TOAD, _ANCIENT)
    assert len(rows) == 1
    assert rows[0]["price_usd"] == pytest.approx(1.0)
    assert rows[0]["market_cap"] == pytest.approx(1_000_000.0)


def test_只采token_meta里出现的代币(db):
    """
    ⚠️ 采样源头就是 self._token_meta.items(),不是另起一套"名单持仓"的判定 ——
       这条测试把"落库的键集合 == _token_meta 的键集合"钉死,而不只是验证
       某一个币被采到(那样即使多采了别的币也测不出来)。
    """
    _add_ready("uA", "alice")
    _add_ready("uB", "bob")
    ca2 = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvbugz"
    client = FakeClient({
        "uA": _snap("uA", balances=[_bal(CA_TOAD, price=1.0, mcap=1_000_000.0)]),
        "uB": _snap("uB", balances=[_bal(ca2, price=2.0, mcap=2_000_000.0)]),
    })
    p = Poller(client, FakeNotifier())
    _, offset = _n_and_offset(p)
    p._tick_no = offset - 1
    p.tick()
    assert set(p._token_meta) == {("solana", CA_TOAD), ("solana", ca2)}
    with store.get_conn() as c:
        toad = store.load_price_history(c, "solana", CA_TOAD, _ANCIENT)
        other = store.load_price_history(c, "solana", ca2, _ANCIENT)
        untouched = store.load_price_history(c, "solana", "从未出现过的代币", _ANCIENT)
    assert len(toad) == 1 and toad[0]["price_usd"] == pytest.approx(1.0)
    assert len(other) == 1 and other[0]["price_usd"] == pytest.approx(2.0)
    assert untouched == []


def test_采样时市值缺失存NULL不存0(db):
    """⚠️ 0 是真实市值,None 是「这一刻没拿到」—— formatter.py 头部同一条规矩"""
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD, price=1.0, mcap=None)])})
    p = Poller(client, FakeNotifier())
    _, offset = _n_and_offset(p)
    p._tick_no = offset - 1
    p.tick()
    with store.get_conn() as c:
        row = store.load_price_history(c, "solana", CA_TOAD, _ANCIENT)[0]
    assert row["market_cap"] is None
    assert row["price_usd"] == pytest.approx(1.0)


def test_价格采样失败不影响买卖推送(db, monkeypatch):
    """
    复刻名单盈亏那条测试踩过的坑:新建的 Poller._tick_no 从 0 起步,tick() 里
    _fetch_snapshots 先自增到 1 才轮到采样门槛检查,而门槛是
    `_tick_no % n == n // 2`,第一轮根本不满足 —— 必须手动把 _tick_no 拨到
    `offset - 1`(offset 从真实配置算出,不写死),让本轮自增后正好命中。
    只 mock 掉 client 端没用(采样不打网络请求),这里改成 mock store 层函数,
    并用调用计数器断言它真的被触发过,不然这条测试就是在测一个从未发生的失败。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", swaps=[_swap("s1")], balances=[_bal(CA_TOAD)])})
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    _, offset = _n_and_offset(p)
    p._tick_no = offset - 1   # 本轮 _fetch_snapshots 自增后命中 offset,该轮必采

    calls = {"n": 0}

    def boom(conn, rows):
        calls["n"] += 1
        raise RuntimeError("price history boom")

    monkeypatch.setattr(store, "save_price_samples", boom)
    n = p.tick()
    assert calls["n"] == 1, "没真的调到采样,这条测试就是假的"
    assert n == 1 and len(notifier.sent) == 1, "价格采样失败绝不能挡住买卖推送"


def test_价格采样与名单盈亏永不同轮触发(db, monkeypatch):
    """
    ⚠️ 采样周期(fomo_price_history_sample_ticks,默认 60)与 _maybe_poll_pnl 的
       _PNL_EVERY_N_TICKS(=20)在改这条测试之前**曾经**恰好相等(都是 20),
       现在不再相等,但这条回归测试依然有意义:只要两个周期存在公约数,
       "同一 tick 触发"就不能只凭直觉排除,必须真的跑够 lcm(n, 20) 轮验证。
       _maybe_sample_price_history 用 `== n // 2` 而不是 `== 0` 来错峰:
       谁把这个比较符号改回 `== 0`,只要 n 恰好等于 20(比如以后又调回旧默认值),
       两个任务会在**每一次**该采样的轮次上都撞在一起,这里会变红。
       ⚠️ 周期数 n 从 p.settings 读真实配置,循环圈数按 lcm(n, 20) 动态算 ——
          默认值以后再改,这条测试也依然覆盖得到完整的联合周期,不会因为
          写死的圈数不够而"意外通过"。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD)])},
                        leaderboard=[_pnl_row("uA")])
    p = Poller(client, FakeNotifier())
    n, _ = _n_and_offset(p)
    period = math.lcm(n, _PNL_EVERY_N_TICKS)
    rounds = period * 3   # 跑足 3 个完整联合周期,不是拍脑袋定的轮数

    calls = {"n": 0}
    real_save = store.save_price_samples

    def counting(conn, rows):
        calls["n"] += 1
        return real_save(conn, rows)

    monkeypatch.setattr(store, "save_price_samples", counting)

    both_fired_together = False
    for _ in range(rounds):
        before_price, before_lb = calls["n"], client.leaderboard_calls
        p.tick()
        fired_price = calls["n"] > before_price
        fired_lb = client.leaderboard_calls > before_lb
        if fired_price and fired_lb:
            both_fired_together = True
    assert not both_fired_together, "价格采样与名单盈亏采集在同一轮同时触发了"
    # 两条任务各自确实按周期触发过 —— 不是因为都没触发才"没撞上"
    assert calls["n"] > 0
    assert client.leaderboard_calls > 0


def test_价格采样与活动流永不同轮触发(db, monkeypatch):
    """
    ⚠️ _poll_feed 的降频判断在 _tick_no 自增**之前**执行,价格采样排在自增
       **之后**(与 _maybe_poll_pnl 同侧)—— 不像 feed/pnl 那样靠调用位置
       天然错开一拍,这条关系必须单独验证,不能从"跟 PNL 没撞"就推断
       "跟 feed 也没撞"(两边的判断时机不一样,不能类推)。
       活动流是网络请求,价格采样是纯内存读 + 一次本地 executemany:
       即使某个配置组合下两者真的同轮触发,代价也不是"两个网络请求排队
       等待"那种量级,严格说不算不可接受;但当前默认配置下经过这条测试
       验证,两者其实完全不会撞见 —— 好过放着一个本可以避免的风险不管。
       周期数与循环圈数都从真实配置动态算,理由同上一条测试。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD)])})
    p = Poller(client, FakeNotifier())
    n, _ = _n_and_offset(p)
    period = math.lcm(n, _FEED_EVERY_N_TICKS)
    rounds = period * 3

    calls = {"n": 0}
    real_save = store.save_price_samples

    def counting(conn, rows):
        calls["n"] += 1
        return real_save(conn, rows)

    monkeypatch.setattr(store, "save_price_samples", counting)

    both_fired_together = False
    for _ in range(rounds):
        before_price, before_feed = calls["n"], client.feed_calls
        p.tick()
        fired_price = calls["n"] > before_price
        fired_feed = client.feed_calls > before_feed
        if fired_price and fired_feed:
            both_fired_together = True
    assert not both_fired_together, "价格采样与活动流在同一轮同时触发了"
    assert calls["n"] > 0
    assert client.feed_calls > 0


def test_价格历史清理按远低于采样的频率触发(db, monkeypatch):
    """清理不该每次采样都顺带做一遍 —— 独立降频、独立 try/except"""
    from src import poller as pmod

    _add_ready("uA", "alice")
    client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD)])})
    p = Poller(client, FakeNotifier())

    calls = {"n": 0}
    real_prune = store.prune_price_history

    def counting(conn, keep_days):
        calls["n"] += 1
        return real_prune(conn, keep_days)

    monkeypatch.setattr(store, "prune_price_history", counting)
    monkey = pmod._PRICE_HISTORY_PRUNE_EVERY_N_TICKS
    try:
        pmod._PRICE_HISTORY_PRUNE_EVERY_N_TICKS = 3
        for _ in range(2):
            p.tick()
        assert calls["n"] == 0, "还没到第 3 轮,不该触发清理"
        p.tick()
        assert calls["n"] == 1, "第 3 轮该触发一次清理"
    finally:
        pmod._PRICE_HISTORY_PRUNE_EVERY_N_TICKS = monkey


def test_清理按配置的保留天数生效不写死(db, monkeypatch):
    """
    ⚠️ 与采样那侧的 _n_and_offset(p) 呼应,守住对称的一个漏洞:
       _maybe_prune_price_history 把 self.settings.fomo_price_history_retain_days
       传给 store.prune_price_history,但此前没有任何测试验证过这个"传"字 ——
       如果谁把那一行悄悄改成 store.prune_price_history(conn, 999)(或者随便
       写死哪个数字),之前的清理测试全部照样绿(它们只关心"清理被调用过"
       和"清理本身按 keep_days 删对了行",从不关心 keep_days 是不是配置值)。
       这里把保留天数改成非默认的 1 天,插入横跨这条边界的样本,
       断言清理效果确实跟着配置值走,而不是跟着代码里某个写死的数字走。
    """
    from src.config import get_settings
    from src.models import iso_minutes_ago

    monkeypatch.setenv("FOMO_PRICE_HISTORY_RETAIN_DAYS", "1")
    get_settings.cache_clear()
    try:
        _add_ready("uA", "alice")
        client = FakeClient({"uA": _snap("uA", balances=[_bal(CA_TOAD)])})
        p = Poller(client, FakeNotifier())
        assert p.settings.fomo_price_history_retain_days == 1, "配置没吃到,测试前提不成立"

        with store.get_conn() as c:
            store.save_price_samples(c, [
                ("solana", "old", iso_minutes_ago(60 * 36), 1.0, None),    # 36 小时前:超过 1 天保留期
                ("solana", "fresh", iso_minutes_ago(60 * 12), 1.0, None),  # 12 小时前:在保留期内
            ])
            p._maybe_prune_price_history(c)   # 新建 Poller._tick_no == 0,0 % N == 0,必触发
            left = {r["token_address"] for r in
                    c.execute("SELECT token_address FROM token_price_history")}
        assert left == {"fresh"}, (
            "保留天数=1 时,36 小时前的样本该被清掉、12 小时前的该留住;"
            "如果这里失败,大概率是 keep_days 没有真的从配置传下去"
        )
    finally:
        get_settings.cache_clear()


# ============================================================
# 转账(TRANSFER_IN / TRANSFER_OUT)—— 取数、归一化、落库、告警
# ============================================================
# ⚠️ 下面这条是 2026-08-26 从 /v2/users/{uid}/transfers 抓下来的真实报文原件
#    ($fih 分发给 PoorGoat_ 的那一笔),一个字段都没删改。
#    手编简化 JSON 会把这里最容易错的三处一起测没:
#      · symbol 藏在 tokenMetadata 里(顶层没有 symbol,也没有 "token" 这个键)
#      · tokenAmount 被 JSON 数字精度毁了(8404 条里 6001 条是 0),真值在 humanAmount
#      · 方向靠 type=DEPOSIT/WITHDRAWAL,不是 direction/side
_REAL_DEPOSIT = {
    "id": "1604366a-1ca6-47c1-b817-4fcc52b58584",
    "toAddress": "7xYXu3gtFzbDa59fmCZnzQSo81frz9MNDBtVuJ8AfxTK",
    "fromAddress": "8FtY7n1ad4LvXqyw8FojCjc7aPLVyTgXXyMJPL2cZx72",
    "isNativeToken": False,
    "tokenAddress": "547tWxWhym8U7Y7DvhGJktpkcs5eHeywvSYnhwvdpump",
    "networkId": 1399811149,
    "humanAmount": 12000000,
    "tokenAmount": 12000000000000,
    "tokenAmountString": "12000000000000",
    "usdAmount": 2439.09,
    "type": "DEPOSIT",
    "createdAt": "2026-08-26T00:37:40.579Z",
    "fromTradeId": None,
    "toTradeId": "7d4252d4-8ccd-44c3-926e-d8a7a8278ed2",
    "isReferral": None,
    "isCrossmint": False,
    "tokenMetadata": {"imageLargeUrl": "https://x.png", "symbol": "fih"},
}
# 真实的 SOL 充值(gas)。tokenAddress 为 null、isNativeToken=true,
# 而 tokenMetadata.symbol = "SOL" —— 380/8404 条是这个形态
_REAL_NATIVE = {
    "id": "9b988f1f-0000-0000-0000-000000000001",
    "toAddress": "7xYXu3gtFzbDa59fmCZnzQSo81frz9MNDBtVuJ8AfxTK",
    "fromAddress": "8FtY7n1ad4LvXqyw8FojCjc7aPLVyTgXXyMJPL2cZx72",
    "isNativeToken": True,
    "tokenAddress": None,
    "networkId": 1399811149,
    "humanAmount": 0.5,
    "tokenAmount": 500000000,
    "tokenAmountString": "500000000",
    "usdAmount": 92.3,
    "type": "DEPOSIT",
    "createdAt": "2026-08-26T00:26:30.000Z",
    "tokenMetadata": {"imageLargeUrl": "https://x.png", "symbol": "SOL"},
}
_REAL_WITHDRAWAL = dict(_REAL_DEPOSIT, id="w-1", type="WITHDRAWAL")
_CA_FIH = "547tWxWhym8U7Y7DvhGJktpkcs5eHeywvSYnhwvdpump"


def _row(uid="uA", handle="PoorGoat_"):
    """归一化只用到 user_id / handle / display_name 三个键"""
    return {"user_id": uid, "handle": handle, "display_name": handle}


def _capture_warnings():
    """收集 loguru 的 WARNING 及以上;返回 (列表, 关闭函数)"""
    from loguru import logger as _lg

    out: list[str] = []
    hid = _lg.add(lambda m: out.append(str(m)), level="WARNING")
    return out, lambda: _lg.remove(hid)


def test_真实转账报文归一化出正确的方向与字段():
    """
    真实报文逐字段。三条断言各盯一个曾经会静默失效的地方:
      symbol=None    → 一条 memecoin 提醒最该显示的东西整个丢失,而且不报错
      数量 = 0       → 「💰 数量 0 ≈ $2,439.09」,而 0 在本项目里是有意义的真实值
      方向判不出     → 退化成 side_unknown,整条记录与"真·收到"无法区分
    """
    p = Poller(FakeClient(), FakeNotifier())
    ev_in = p.normalize_transfers(_row(), [_REAL_DEPOSIT])[0]
    assert ev_in.event_type == "TRANSFER_IN"
    assert ev_in.side_unknown is False, "type=DEPOSIT 是显式方向,不该走降级"
    assert ev_in.token_symbol == "fih", "symbol 在 tokenMetadata 里,不在顶层、也不在 token 下"
    assert ev_in.token_amount == "12000000", \
        "数量要取人类可读的 humanAmount(1200 万枚),不是 6 位小数的最小单位"
    assert ev_in.network_id == "solana"
    assert ev_in.token_address == _CA_FIH
    assert ev_in.amount_usd == 2439.09
    assert ev_in.event_ts == "2026-08-26T00:37:40+00:00"
    assert ev_in.ts_fallback is False
    assert ev_in.event_id == "TRANSFER_IN:1604366a-1ca6-47c1-b817-4fcc52b58584"

    ev_out = p.normalize_transfers(_row(), [_REAL_WITHDRAWAL])[0]
    assert ev_out.event_type == "TRANSFER_OUT", "WITHDRAWAL 必须判成转出"
    assert ev_out.side_unknown is False


def test_EVM转账的数量不能取被精度毁掉的tokenAmount():
    """
    真实 BSC 报文:tokenAmount 是 0(JSON 数字精度撑不下 18 位小数),
    真值在 tokenAmountString / humanAmount。8404 条里 6001 条是这个形态 ——
    取错字段的话七成以上的转账都会显示成「数量 0」。
    """
    evm = {
        "id": "9e0574eb-48af-5a1e-be09-3325f0575397",
        "isNativeToken": False,
        "tokenAddress": "0xbe9d156892e55e7154bcd3cb0fea677f9d3103e1",
        "networkId": 56,
        "humanAmount": 0.299361,
        "tokenAmount": 0,
        "tokenAmountString": "299361000000000000",
        "usdAmount": 41.1302,
        "type": "DEPOSIT",
        "createdAt": "2026-08-26T01:02:22.344Z",
        "tokenMetadata": {"symbol": "Broccoli"},
    }
    ev = Poller(FakeClient(), FakeNotifier()).normalize_transfers(_row(), [evm])[0]
    assert ev.token_amount == "0.299361", f"取到的是 {ev.token_amount!r}"


def test_原生币转账被显式跳过():
    """
    SOL/ETH/BNB 充值是给 gas 充钱,不是"有人给他分筹码",而且 tokenAddress 是 null、
    聚合键都构造不出来。

    ⚠️ 判据必须是 isNativeToken 这个**显式字段**,不能靠"取不到代币标识所以被丢掉"
       这个副作用 —— tokenMetadata.symbol 是有值的("SOL"),symbol 取值一修好,
       那道兜底门就再也拦不住它们。
    """
    p = Poller(FakeClient(), FakeNotifier())
    assert p.normalize_transfers(_row(), [_REAL_NATIVE]) == []
    # 同一批里的非原生币不受影响
    got = p.normalize_transfers(_row(), [_REAL_NATIVE, _REAL_DEPOSIT])
    assert [e.token_symbol for e in got] == ["fih"]


def test_名单handle索引必须与比对侧同一套归一化(db):
    """
    ⚠️ 名单里绝大多数 handle 含大写(PoorGoat_ / CryptoTalkMan / 0xAvast…)。
       建索引时不小写、比对时 .lower(),这个 in 判断对他们**恒为 False** ——
       「名单内转账」标记永远不出现,不报错、日志里也看不出来。
    """
    _add_ready("uA", "PoorGoat_")
    p = Poller(FakeClient(), FakeNotifier())
    with store.get_conn() as c:
        p._refresh_watched_index(store.list_active_users(c))
    # 索引里必须已经是归一化形态,而不是原样的 PoorGoat_
    assert "poorgoat_" in p._watched_handles
    # 端到端:对手方带 @ 和不同大小写,仍要认得出是名单内的人
    raw = dict(_REAL_DEPOSIT, fromUser={"userHandle": "@PoorGoat_"})
    ev = p.normalize_transfers(_row("uB", "bob"), [raw])[0]
    assert ev.counterparty_is_watched is True


def _many_users(n: int) -> list[dict]:
    """造 n 个已就绪用户,返回 _fetch_snapshots 要的 users 列表"""
    users = []
    for i in range(n):
        uid = f"u{i:03d}"
        _add_ready(uid, f"h{i:03d}")
        users.append({"user_id": uid, "handle": f"h{i:03d}", "display_name": f"h{i:03d}"})
    return users


def test_转账采集的单轮峰值必须与平常轮持平(db):
    """
    ⚠️⚠️ 这是个**峰值**问题,不是均值问题 —— 上一版实现"每 20 轮把 91 个人一次打完",
       均值只有每轮 4.6 个请求,看着无害;实际是:
         非转账轮 99~109 个请求 / 转账轮 190 个(91 swaps + 8 balances + 91 transfers)
         峰值 1.74x;190/12 线程 = 15.8 波 × p50 0.3~1.2s = 单轮 4.8~19.0s
       上界超过 15s 轮询间隔,而用户的生产日志里已经有
       「单轮耗时 17s > 轮询间隔 15s —— tick 会连轴转」。

    所以这里断言的是**任何一轮**的转账请求数都不超过 ⌈人数/周期⌉,
    也就是"再也不存在把全名单一次打完的那一轮"。
    ⚠️ 门槛写死字面量(91 人 · 20 轮 → 5),不从被测模块 import 常量 ——
       从 poller 里 import 周期再拿它算门槛,等于用被测代码给自己打分。
    """
    users = _many_users(91)
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    peaks = []
    for _ in range(60):
        before = len(client.transfer_calls)
        p._fetch_snapshots(users)
        peaks.append(len(client.transfer_calls) - before)
    assert max(peaks) <= 5, f"单轮转账请求峰值 {max(peaks)},超过 ⌈91/20⌉ = 5"
    assert max(peaks) >= 1, "一轮都没拉过转账,这条测试等于没测"
    assert 91 not in peaks, "又出现了『一轮打完全名单』的峰值轮"


def test_转账轮转必须在一个周期内覆盖到每一个人(db):
    """
    分摊的代价是覆盖延迟,所以延迟必须**可证**:91 人 · 每轮 5 个 → ⌈91/5⌉ = 19 轮
    (× 15s = 285s ≈ 4.75 分钟)必须每个人都被轮到至少一次,一个都不能漏。

    ⚠️ 漏采分析(为什么 4.75 分钟够):单人转账实测中位数 1.67 条/小时,
       285s 内期望新增 0.13 条,而单页 limit 25 条 ≈ 15.0 小时余量。
       要漏采得单人在 285s 内新增 >25 条 = 315 条/小时,比实测高 189 倍。
    ⚠️ 必须断言"**每个人**都被覆盖到",不能只断言总请求数 —— 游标写错(比如在
       每轮长度都在变的池子上取模)时总数照样对,但有人永远排不上队。
    """
    users = _many_users(91)
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    for _ in range(19):
        p._fetch_snapshots(users)
    missed = {u["user_id"] for u in users} - set(client.transfer_calls)
    assert missed == set(), f"19 轮里有 {len(missed)} 个人一次都没被轮到:{sorted(missed)[:5]}"


def test_名单比周期还短时也必须真的采到转账(db):
    """
    ⚠️ ⌈人数/周期⌉ 在人数 < 周期时会算出 0(3/20 → 0.15 → ceil=1,但 3//20 = 0)——
       批量取整取错方向的话小名单**一条转账都采不到**,而且悄无声息。
       所以下限必须是 1:3 个人 → 每轮 1 个 → 3 轮覆盖一圈。
    """
    users = _many_users(3)
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    for _ in range(3):
        p._fetch_snapshots(users)
    assert set(client.transfer_calls) == {"u000", "u001", "u002"}, \
        f"3 轮该把 3 个人都轮一遍,实际 {client.transfer_calls}"


def test_没轮到拉转账的那些轮次不刷警告(db):
    """
    ⚠️ snap.transfers is None 有两种来源:排到了却拿回 None(真失败,要 WARN)、
       和"这一轮压根没轮到转账"(正常)。不分开的话,20 轮里有 19 轮会对每个人
       各刷一条 WARNING —— 稳态下就是每分钟几十行噪音,把真正要紧的 ERROR 淹掉。
    """
    _add_ready("uA", "alice")
    p = Poller(FakeClient(), FakeNotifier())
    users = [{"user_id": "uA", "handle": "alice", "display_name": "alice"}]
    snaps = {"uA": UserSnapshot("uA", swaps=[], transfers=None, balances=[])}
    p._swaps_attempted = {"uA"}

    p._transfers_attempted = set()          # 这一轮没轮到
    warned, close = _capture_warnings()
    with store.get_conn() as c:
        try:
            p._collect_events(c, users, snaps)
        finally:
            close()
    assert not [w for w in warned if "transfers" in w], f"不该有警告,实际:{warned}"

    # 反向:排到了却拿回 None,必须 WARN —— 否则真的拉不到数据也没人知道
    p._transfers_attempted = {"uA"}
    warned, close = _capture_warnings()
    with store.get_conn() as c:
        try:
            p._collect_events(c, users, snaps)
        finally:
            close()
    assert [w for w in warned if "transfers" in w], "真失败必须留痕"


def test_转账落库但绝不逐条推送(db):
    """
    实测名单 91 人的非稳定币转入 13.1 条/人/天 ≈ 1200 条/天,绝大多数是几美元的空投灰尘。
    逐条推等于把真正要看的买卖推送整个淹掉。

    ⚠️ 而且必须当场 mark_sent:只是"跳过发送"的话 sent 永远是 0,
       补发队列会每 20 秒把它们捞出来重试一次、连捞 10 分钟。
    """
    _add_ready("uA", "PoorGoat_")
    p = Poller(FakeClient(), FakeNotifier())
    evs = p.normalize_transfers(_row(), [_REAL_DEPOSIT, _REAL_WITHDRAWAL])
    with store.get_conn() as c:
        new_events = p._persist(c, evs)
        rows = c.execute(
            "SELECT event_type, sent FROM fomo_events ORDER BY event_type").fetchall()
    assert new_events == [], "转账不该进入逐条推送的队列"
    assert len(p._new_transfers) == 2, "但要交给转入告警去聚合"
    assert [r["event_type"] for r in rows] == ["TRANSFER_IN", "TRANSFER_OUT"], "两条都必须落库"
    assert [r["sent"] for r in rows] == [1, 1], "sent 必须当场置 1,否则补发队列会反复捞"


def test_转账不改buy_count(db):
    """
    B-8:空投/领奖/内部划转绝不能造出假买入 —— 徽章会打在一笔没花钱的仓位上,
    功能 A/B 同时失真。这条防线的落点是 store.should_count(只认 BUY)。
    """
    _add_ready("uA", "PoorGoat_")
    p = Poller(FakeClient(), FakeNotifier())
    with store.get_conn() as c:
        p._persist(c, p.normalize_transfers(_row(), [_REAL_DEPOSIT]))
        n = c.execute("SELECT COUNT(*) n FROM user_token_stats").fetchone()["n"]
    assert n == 0


def test_收到时市值只在事件足够新时才补():
    """
    报文里没有 marketCap,只能拿本轮 balances 索引里的现价市值补。
    而首轮采集会一次性吃进最多 25 条、跨度可达十几小时的历史转账 ——
    给它们贴上"现在的市值",就是在断言我们并不知道的事(消息里那一格写的正是「收到时」)。
    """
    p = Poller(FakeClient(), FakeNotifier())
    p._token_meta[("solana", _CA_FIH)] = {"symbol": "fih", "market_cap": 198_200.0}

    old = p.normalize_transfers(_row(), [_REAL_DEPOSIT])[0]      # createdAt 是固定的历史时刻
    assert old.market_cap is None, "十几个小时前的转账不该被贴上现在的市值"

    fresh_raw = dict(_REAL_DEPOSIT, id="fresh-1",
                     createdAt=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    fresh = p.normalize_transfers(_row(), [fresh_raw])[0]
    assert fresh.market_cap == 198_200.0, "刚发生的转账,现价市值就是它收到时的市值"


def _seed_receivers(usd_list, *, symbol="fih", ages_min=None, from_addrs=None):
    """
    造 N 个名单成员各收到一笔 $fih 的库存,返回 Poller(_new_transfers 已就位)。

    ages_min   每个人是"多少分钟前"收到的(默认全是刚刚)。用来测时间窗。
    from_addrs 每个人的发货地址(默认全都是真实报文里那个 —— $fih 的真实形态就是
               同一个地址发给三个人)。用来测聚类的两种表述。
    """
    p = Poller(FakeClient(), FakeNotifier())
    evs = []
    for i, usd in enumerate(usd_list):
        _add_ready(f"u{i}", f"Holder{i}")
        mins = 0 if ages_min is None else ages_min[i]
        ts = (datetime.now(UTC) - timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")
        raw = dict(_REAL_DEPOSIT, id=f"tx-{i}", usdAmount=usd, createdAt=ts,
                   tokenMetadata={"symbol": symbol})
        if from_addrs is not None:
            raw["fromAddress"] = from_addrs[i]
        evs.extend(p.normalize_transfers(_row(f"u{i}", f"Holder{i}"), [raw]))
    with store.get_conn() as c:
        p._persist(c, evs)
    p._new_transfers = evs
    p._catchup_since = None
    return p, evs


def test_三个人收到同一个币就告警_两个人不告警(db):
    """用户要的那件事本身:同一个合约地址有 3 个以上他关注的人「收到」→ 推一条提醒。"""
    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1
    msg = p.notifier.sent[0]
    assert "收到" in msg and "不是在 FOMO 上买的" in msg
    assert "Holder0" in msg and "Holder1" in msg and "Holder2" in msg
    assert _CA_FIH in msg
    # ⚠️ 意图断言不许回来:报文里没有 userId,「一分钱没花」区分不了
    #    "项目方分发"和"本人从别处充值"(后者其实就是买入)
    assert "一分钱没花" not in msg and "没花" not in msg


def test_人数不够阈值时不告警(db):
    p, _ = _seed_receivers([901.37, 912.05])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert p.notifier.sent == []


def test_金额门槛之下的空投灰尘不算收到(db):
    """
    实测:1 小时窗口 ≥3 人的 42 次命中里,有 39 次单人到账不足 $100 —— 全是空投灰尘。
    不设门槛的话这个功能一天炸 138.8 次,用户会直接静音。
    """
    p, _ = _seed_receivers([3.0, 4.0, 5.0, 6.0])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert p.notifier.sent == [], "四个人都收到了,但全是灰尘,不该打扰用户"


def test_同一个币只告警一次(db):
    """分发是一次性事件。每 5 分钟提醒一遍等于让用户静音。"""
    p, evs = _seed_receivers([901.37, 912.05, 2439.09])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
        p._new_transfers = evs
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1


def test_停机补数轮整段跳过转入告警(db):
    """
    积压的转账不是"刚刚发生的事",而消息里写的是"最近 N 小时内" —— 两者对不上。
    与 _check_copytrade 同一道守卫。
    """
    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    p._catchup_since = "2026-08-25T00:00:00+00:00"
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert p.notifier.sent == []


def test_跟单开着的时候转账也绝不会下单(db):
    """
    ⚠️⚠️ 安全边界。「收到免费筹码」与「自己掏钱买入」是相反的含义,拿它去触发花钱的
       操作方向就是错的。

    ⚠️ 这条测试上一版是**无效的**:它整场跑在跟单**关闭**的状态下,而且只调
       _check_transfer_in、从不调 _check_copytrade —— 跟单关着时 _check_copytrade
       第一行就 return,所以它证明的其实是"关着的功能没运行",
       而要守的命题是"**开着**的时候转账也不会下单"。

    这一版:跟单**启用**(min_buyers=1,门槛低到只要有一笔买入就会下单)、
    跑**完整 tick**、同一轮里既有转账又有一笔真买入作**阳性对照** ——
    对照那个币必须生成跟单信号(证明执行器确实是活的、这一轮真的走到了判定),
    而被转账的那个币必须一条信号都没有。
    """
    _enable_copy(min_buyers=1)
    _stale_tick(0.01)                 # 别落进停机补数轮,那会整段跳过跟单

    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    snaps = {}
    for i in range(3):
        _add_ready(f"u{i}", f"Holder{i}")
        raw = dict(_REAL_DEPOSIT, id=f"boundary-tx-{i}", usdAmount=900.0 + i, createdAt=ts)
        # 只有 u0 额外真买了**另一个**币(CA_TOAD),作阳性对照
        swaps = [_swap("buy-control")] if i == 0 else []
        # ⚠️ 必须把被转账那个币的 balances(含 marketCap)也喂进来。
        #    否则它拿不到入场市值,decide 会以 SKIP_NO_MCAP 跳过 ——
        #    那样这条测试守住的就成了"碰巧没市值",而不是"边界成立"。
        #    (仓库里已经有过一模一样的教训:别拿巧合当防线。)
        snaps[f"u{i}"] = UserSnapshot(f"u{i}", swaps=swaps, transfers=[raw],
                                      balances=[_bal(_CA_FIH, mcap=368_000.0)])
    p = Poller(FakeClient(snaps), FakeNotifier())
    for _ in range(3):                # 3 个人 → 轮转 3 轮才凑齐转账
        p.tick()

    with store.get_conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 1, \
            "前提不成立:转入告警本身没触发,那这条测试什么都没证明"
        assert store.load_copy_config(c).enabled is True, "前提不成立:跟单没真的开着"
        sigs = [dict(r) for r in c.execute(
            "SELECT token_address, status FROM copytrade_signals")]
        # 阳性对照:执行器是活的,一笔真买入就足以生成信号
        assert [s["token_address"] for s in sigs] == [CA_TOAD], \
            f"阳性对照没生成跟单信号,判定这一轮压根没跑到:{sigs}"
        # 真正要守的:被转账的那个币,一条跟单信号都不许有
        assert _CA_FIH not in [s["token_address"] for s in sigs], \
            "转账把币送进了跟单执行器 —— 安全边界破了"
        # 白拿的筹码也不许被算成买入(共识分子的地基)
        stats = [dict(r) for r in c.execute(
            "SELECT token_address, buy_count FROM user_token_stats")]
        assert all(s["token_address"] != _CA_FIH for s in stats), \
            f"转账改了 user_token_stats,共识数会把白拿的人算成买家:{stats}"


def test_整条tick真的把转账采进来并在够人数时告警(db):
    """
    整条链路的集成证明:tick() → _fetch_snapshots(轮转到谁就拉谁)→ 归一化 →
    游标过滤 → 落库 → 转入告警。

    ⚠️ 这条测试存在的理由:normalize_transfers / _transfer_to_event 在此之前是
       **零调用者的孤儿代码** —— 它们各自的单测全绿,而线上一条转账都采不到。
       只测归一化函数是测不出"没人调它"的。
    ⚠️ 转账改成轮转采集之后,3 个人要 3 轮才轮完一圈 —— 也就是说这条链路只有在
       "最后一个人也被轮到"的那一轮才凑得齐 3 个收到者。这正是分摊换来的覆盖延迟,
       这里顺便把它钉住。
    """
    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    snaps = {}
    for i in range(3):
        _add_ready(f"u{i}", f"Holder{i}")
        raw = dict(_REAL_DEPOSIT, id=f"tick-tx-{i}", usdAmount=900.0 + i, createdAt=ts)
        snaps[f"u{i}"] = UserSnapshot(f"u{i}", swaps=[], transfers=[raw], balances=[])
    client = FakeClient(snaps)
    notifier = FakeNotifier()
    p = Poller(client, notifier)

    # 前两轮:各轮到一个人,只有 2 个人收到 —— 不够 3 人门槛,不该推
    p.tick()
    p.tick()
    assert len(client.transfer_calls) == 2, \
        f"每轮该只拉 1 个人(3 人 / 20 轮向上取整),实际 {client.transfer_calls}"
    assert notifier.sent == [], "才 2 个人收到就推,等于门槛没生效"

    # 第三轮:最后一个人也被轮到,凑够 3 人 → 推一条告警
    p.tick()
    assert sorted(client.transfer_calls) == ["u0", "u1", "u2"]
    with store.get_conn() as c:
        rows = c.execute(
            "SELECT event_type, sent FROM fomo_events ORDER BY event_ts").fetchall()
    assert [r["event_type"] for r in rows] == ["TRANSFER_IN"] * 3, "转账必须真的落库"
    assert all(r["sent"] == 1 for r in rows), "转账不逐条推送,但要当场标记已发"
    assert len(notifier.sent) == 1, f"应当只推那一条聚合告警,实际 {len(notifier.sent)} 条"
    assert "🚨" in notifier.sent[0] and "不是在 FOMO 上买的" in notifier.sent[0]


# ---- 时间窗:配置必须真的生效,而且不许被写死的数字架空 ----------------------
def _env(monkeypatch, **kw) -> None:
    """
    改环境变量并清缓存。⚠️ 必须在**构造 Poller 之前**调用 ——
    Poller.__init__ 里就把 get_settings() 的结果抓走了。
    (teardown 由 conftest 的 _no_send_throttle 统一 cache_clear)
    """
    from src.config import get_settings

    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()


def test_转入告警的默认阈值(monkeypatch):
    """
    ⚠️ 三个默认值一律**写死字面量**,不从 config import 再断言 ——
       从被测模块 import 默认值给自己打分,改成 1 小时或 720 小时都照样绿,
       而"最近多久内"正是这条告警的全部语义:
         1 小时   → 分发通常横跨几小时,真信号大面积漏掉
         720 小时 → 一个月内碰巧各自收到过的人被凑成"同时收到",全是假信号
    ⚠️ 用 _env_file=None 构造:否则会去读仓库根目录的 .env,
       断言就变成了"这台机器的配置是多少",而不是"代码的默认值是多少"。
    """
    from src.config import FomoSettings

    for k in ("FOMO_TRANSFER_ALERT_WINDOW_HOURS", "FOMO_TRANSFER_ALERT_RECEIVERS",
              "FOMO_TRANSFER_ALERT_MIN_USD"):
        monkeypatch.delenv(k, raising=False)
    s = FomoSettings(_env_file=None)
    assert s.fomo_transfer_alert_window_hours == 24
    assert s.fomo_transfer_alert_receivers == 3
    assert s.fomo_transfer_alert_min_usd == 500.0


def test_窗口之外的到账不算数_窗口是配置说了算(db, monkeypatch):
    """
    ⚠️⚠️ 这条守的是"时间窗真的存在,而且真的取自配置"。此前**四种**变异全绿:
         config 默认 24 → 1 / 24 → 720 / poller 里写死 720 小时 / since = ""(窗口取消)
       原因是从来没有一条用例让某个到账**落在窗口之外**。

    这里窗口配成 2 小时,三个人分别在 10 分钟前、20 分钟前、5 小时前收到 ——
    窗口内只有 2 个人,不够 3 人门槛,一条都不该推。
    只要窗口被写死成更大的数(720)、或者被取消(since=""),第三个人就会被算进来。
    """
    _env(monkeypatch, FOMO_TRANSFER_ALERT_WINDOW_HOURS=2)
    p, _ = _seed_receivers([901.37, 912.05, 2439.09], ages_min=[10, 20, 300])
    assert p.settings.fomo_transfer_alert_window_hours == 2, "配置没吃到,测试前提不成立"
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert p.notifier.sent == [], \
        "5 小时前那笔落在 2 小时窗口之外,却被算进了人数 —— 窗口没生效"


def test_窗口配多大就认多大_不许被写死成更小的数(db, monkeypatch):
    """
    上一条的反向对照。窗口配成 720 小时,三笔都在 100 小时前 ——
    必须照样告警。窗口要是被写死成 1 或 24 小时,这里就一条都推不出来。
    ⚠️ 两条方向缺一不可:只测"窗口外不算"挡不住 since 被写死成 1 小时,
       只测"窗口内算"挡不住 since 被取消。
    """
    _env(monkeypatch, FOMO_TRANSFER_ALERT_WINDOW_HOURS=720)
    p, _ = _seed_receivers([901.37, 912.05, 2439.09],
                           ages_min=[60 * 100, 60 * 100 + 1, 60 * 100 + 2])
    assert p.settings.fomo_transfer_alert_window_hours == 720, "配置没吃到,测试前提不成立"
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1, "窗口配到 720 小时,100 小时前的到账必须还算数"
    assert "最近 720 小时内" in p.notifier.sent[0], "消息里写的窗口也必须是配置值"


# ---- 「名单里有没有人真金白银买过」的回看窗口 --------------------------------
def _seed_old_buy(uid, handle, *, days_ago, ca=_CA_FIH, usd=3000.0):
    """给某人补一笔 days_ago 天前的真买入(走买入侧的严格谓词:ready + 可计数 reason)"""
    from tests.conftest import make_event

    _add_ready(uid, handle)
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    ev = make_event(event_type=EVENT_BUY, event_id=f"BUY:{uid}-old",
                    user_id=uid, handle=handle, network_id="solana",
                    token_address=ca, token_symbol="fih", event_ts=ts,
                    amount_usd=usd, market_cap=368_000.0)
    ev.badge_reason = "local_stats"
    with store.get_conn() as c:
        store.insert_event(c, ev)
        c.execute("UPDATE fomo_events SET user_handle = ? WHERE event_id = ?",
                  (handle, ev.event_id))


def test_一个月前买过的人也必须算进真金白银买过(db):
    """
    ⚠️⚠️ 这条守的是 _TRANSFER_BUYER_LOOKBACK_DAYS,而它此前**没有任何测试**:
       把 3650 天改成 0 全套测试照样绿。

    后果不是"少一行",是**印反话**:回看窗口塌成 0 天 → token_buyers 返回 []
    → 而 [] 与 None 语义不同,[] 会渲染成「名单里还没有人真金白银买过这个币」。
    $fih 的真实情况恰恰相反 —— CryptoTalkMan 在 $36.8 万市值上买了两笔。
    在一条"有人在分发筹码"的告警里印出"没人买过",与刚在 /ca 修掉的假事实同级。

    ⚠️ 这个问题问的是"名单认不认识这个币",不是"最近有没有人买" ——
       一个月前有人重仓过、现在有人在给别人发筹码,恰恰是最该看见的对照。
    """
    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    _seed_old_buy("uBuyer", "CryptoTalkMan", days_ago=30)
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1
    msg = p.notifier.sent[0]
    assert "CryptoTalkMan" in msg and "真金白银" in msg, \
        f"一个月前的真买入被回看窗口挡掉了:{msg}"
    assert "还没有人" not in msg, \
        "把『有人买过』印成『还没有人买过』—— 这是印反话,不是少一行"


def test_确实没人买过时才允许说没人买过(db):
    """上一条的对照组:没有任何买入记录时,[] 这条信息本身是有价值的,要显示。"""
    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert "还没有人" in p.notifier.sent[0]


# ---- 发货地址聚类:唯一可证的那条证据 ----------------------------------------
def test_同一个发货地址发给多人时告警里要点出来(db):
    """
    $fih 的真实形态,端到端:5 分多钟内同一个 fromAddress 发给名单里三个人。
    ⚠️ 这条链路整条都得通 —— fromAddress 要从报文里解析出来、要落库、
       要能被聚类查出来、最后要出现在消息里。断在任何一环这句话都不会出现。
    """
    p, _ = _seed_receivers([901.37, 912.05, 2439.09], ages_min=[5, 3, 0])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    msg = p.notifier.sent[0]
    assert "同一个发货地址" in msg
    assert "8FtY7n…cZx72" in msg, f"发货地址没出现在消息里:{msg}"
    assert "前后 5 分" in msg, "多长时间内发完必须写出来,「几分钟内」和「20 小时里」是两回事"


def test_发货地址各不相同时照样告警但不许声称有聚类(db):
    """
    ⚠️ 聚类是**加强证据,不是触发条件**。三个地址各不相同时:
       仍然是"3 个人同时收到同一个币",仍然该告警;
       但一个字都不能暗示有共同发货方 —— 而"各不相同"本身也是要说出来的信息。
    """
    p, _ = _seed_receivers(
        [901.37, 912.05, 2439.09],
        from_addrs=["3nQmLpZq7Rt2Vx9Kd8Hs1Wf4Yc6Ub5Ne0Ja7Mg2Pk3S",
                    "9zTbCwEr4Yu6Io8Pa1Sd3Fg5Hj7Kl9Zx2Cv4Bn6Mq8W",
                    "5aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890AbCdEf"])
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1, "没有聚类不是不告警的理由"
    msg = p.notifier.sent[0]
    assert "同一个发货地址" not in msg
    assert "各不相同" in msg


# ---- 推送失败:台账必须退回,而且真的会重试 ----------------------------------
def test_推送失败时台账退回并在下一轮重试(db):
    """
    ⚠️⚠️ record_transfer_in_signal 排在渲染/推送**之前**(两个 tick 撞上时靠主键
       决出唯一赢家),而它的主键语义是"这个币这辈子只告警一次" ——
       于是一次 TG 400 就等于**这个币的告警永久丢失**,日志里只留一行 error。

    ⚠️ notifier.send 在 TG 返回 400/403 时是**返回 False** 而不是抛异常,
       所以光 try/except 是接不住的:返回值必须看。
    ⚠️ 光把台账退回还不够:_check_transfer_in 只扫"本轮有新转账"的币,
       而失败之后这个币多半不会马上再来一笔转账 —— 所以第二轮这里**故意不喂新转账**,
       重试必须靠 poller 自己记着的重试队列。
    """
    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    p.notifier.ok = False                      # TG 拒收(返回 False,不抛异常)
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
        assert len(p.notifier.sent) == 1, "第一轮该尝试发一次"
        assert c.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 0, \
            "推送没成功,台账不能留着 —— 留着就是这个币永不再告警"

    # 第二轮:TG 恢复,而且**没有任何新转账**。必须靠重试队列把它重新发出去
    p.notifier.ok = True
    p._new_transfers = []
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 2, "TG 恢复后必须补发,而不是等下一笔转账"
    assert "🚨" in p.notifier.sent[1]
    with store.get_conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 1

    # 第三轮:已经发成功了,不许再发第三遍
    p._new_transfers = []
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 2, "补发成功后重试项必须出队,否则会每轮重复推送"


def test_推送抛异常时台账同样要退回(db):
    """返回 False 与抛异常是两条不同的失败路径,两条都会让这个币永久丢告警"""

    class _Boom:
        sent: list = []

        def send(self, text, **kw):
            raise RuntimeError("网络断了")

    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    p.notifier = _Boom()
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
        assert c.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 0

    p.notifier = FakeNotifier()
    p._new_transfers = []
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1, "异常之后必须还能补发出来"


def test_渲染失败时台账同样要退回(db):
    """
    渲染阶段抛异常与推送失败同理 —— 台账已经占位了,不退回就是永久丢。

    ⚠️ 打桩/还原**必须自己开一份 MonkeyPatch**,绝不能用 monkeypatch 夹具再 undo():
       那个夹具是函数级共享的,undo() 会把 db 夹具的 DB_PATH 补丁一起还原,
       后半段就打到 data/fomo.db(用户正在跑的生产库)上去了 —— 已经踩过一次。
    """
    import src.poller as pmod

    def _boom(**kw):
        raise ValueError("模板炸了")

    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    mp = pytest.MonkeyPatch()
    mp.setattr(pmod, "render_transfer_in_signal", _boom)
    try:
        with store.get_conn() as c:
            p._check_transfer_in(c, dry_run=False)
            assert p.notifier.sent == []
            assert c.execute(
                "SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 0
    finally:
        mp.undo()

    p._new_transfers = []
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
    assert len(p.notifier.sent) == 1, "渲染修好之后必须还能补发出来"


# ---- 0 是有意义的真实值 -------------------------------------------------------
def test_到账金额都是0时合计是0而不是不知道(db, monkeypatch):
    """
    ⚠️ 铁律:判空一律 is None。`total = sum(...) or None` 在配置允许的 min_usd=0 下
       (config 里的约束就是 ge=0)会把"三个人各收到 $0.00"这件**确定的事实**
       吞成 None。0 与"拿不到"在本项目里是两件事。

    ⚠️ 这一句 total 的**唯一去处是台账**(transfer_in_signals.total_usd),
       消息里那行合计是 formatter 自己按 receivers 重算的 —— 所以断言必须落在
       台账那一列上。只断言消息的话,这个变异是抓不住的(第一版就这么空转了一次)。
       台账那一列是事后回溯"这次分发一共发了多少钱"的唯一记录,
       写成 NULL = 永久丢失,而且看起来像"当时查不到金额"。
    ⚠️ 顺带守住 _transfer_to_event 里同一条铁律:amount_usd 用 `A or B` 取多来源时,
       $0.00 会被当成"顶层没给"而落到 None —— 那样这三个人连"收到"都算不上。
    """
    _env(monkeypatch, FOMO_TRANSFER_ALERT_MIN_USD=0)
    p, _ = _seed_receivers([0.0, 0.0, 0.0])
    assert p.settings.fomo_transfer_alert_min_usd == 0, "配置没吃到,测试前提不成立"
    assert [e.amount_usd for e in p._new_transfers] == [0.0, 0.0, 0.0], \
        "$0.00 在解析阶段就被真值判断吞成 None 了"
    with store.get_conn() as c:
        p._check_transfer_in(c, dry_run=False)
        row = c.execute("SELECT total_usd FROM transfer_in_signals").fetchone()
    assert len(p.notifier.sent) == 1
    assert row["total_usd"] == 0.0, \
        f"台账里的合计被 `or None` 吞成了 {row['total_usd']!r} —— 『确实是 0』变成了『不知道』"
    assert "合计 $0.00" in p.notifier.sent[0], \
        f"消息里的合计也不该消失:{p.notifier.sent[0]}"


# ---- 合计必须与人数是同一批人 -------------------------------------------------
def test_二十五人收到时合计是全部人的而不是前十个人的(db):
    """
    ⚠️⚠️ 病灶原样:人数取自 count_recent_receivers(**全量**),
       而明细取自 transfer_receivers(..., limit=10),合计对这 10 行求和 ——
       渲染成「25 人收到 · 合计 $X」,台账 transfer_in_signals.total_usd 也是这个 X。
       X 只是其中 10 个人的合计,却被摆在"25 人"旁边,是一句假话。
       迄今 3 次告警都是 3 人所以没暴露,而这功能抓的恰恰是分发事件 ——
       config 里那段实测记录着:一个平台级批量发放的币有 41 人收到。

    ⚠️ 台账那一列是事后回溯"这次分发一共发了多少钱"的唯一记录,写错 = 永久错。
       所以断言必须同时落在**台账**和**消息**上,只看一个都漏。
    ⚠️ 期望值 22834.25 是测试自己按那串金额算的,不从任何被测模块取常量。
    """
    usd = [901.37 + i for i in range(25)]          # 901.37 … 925.37,和 = 22834.25
    p, _ = _seed_receivers(usd)
    warns, close = _capture_warnings()
    try:
        with store.get_conn() as c:
            p._check_transfer_in(c, dry_run=False)
            row = c.execute(
                "SELECT receivers, total_usd FROM transfer_in_signals").fetchone()
    finally:
        close()

    assert row["receivers"] == 25, "前提不成立:25 个人没有都被算进来"
    assert round(row["total_usd"], 2) == 22834.25, \
        f"台账里的合计是 {row['total_usd']!r} —— 只是其中一部分人的合计," \
        f"却与 receivers={row['receivers']} 记在同一行"

    assert len(p.notifier.sent) == 1
    msg = p.notifier.sent[0]
    assert "25 人收到" in msg and "合计 $22,834.25" in msg, \
        f"消息里的「N 人 + 合计」不是同一批人:{[ln for ln in msg.split(chr(10)) if '合计' in ln]}"
    # 展示层照样收口:25 个人不会真的列 25 行,而且未显示人数要如实说出来
    shown = sum(1 for ln in msg.split("\n") if ln.startswith("👤 "))
    assert shown == 10, f"消息里列了 {shown} 行收到者"
    assert "…还有 15 人未显示" in msg
    # 干净路径上不许有漂移告警,否则那条告警就成了每轮都响的噪音
    assert [w for w in warns if "漂移" in w] == []


def test_两处谓词漂移时必须留下告警而不是悄悄发出假数(db):
    """
    ⚠️ store.transfer_receivers 的文档里写着"谓词必须与 count_recent_receivers
       逐条一致",但那**只是一句注释** —— 真漂移了没有任何人会知道:
       消息照发,只是写着 3 人、底下列得出 2 个,合计也跟着少算。
       两个函数各写各的 SQL,一人一行,行数本该恒等于人数;这里把那句注释
       变成每轮都跑的检查。

    ⚠️ 只告警**不抛异常**:漂移是"数字不精确",中断推送是"用户什么都收不到",
       后者更糟。所以这条测试同时钉死两件事:告警要有、推送不能停。
    ⚠️ 打桩必须自己开一份 MonkeyPatch,绝不能用 monkeypatch 夹具再 undo() ——
       那个夹具是函数级共享的,undo() 会把 db 夹具的 DB_PATH 补丁一起还原,
       后半段就打到 data/fomo.db(用户正在跑的生产库)上去了。已经踩过一次。
    """
    import src.store as smod

    p, _ = _seed_receivers([901.37, 912.05, 2439.09])
    real = smod.transfer_receivers

    def _drifted(*a, **kw):
        """模拟谓词漂移:明细比计数少一个人(多一条 AND 就是这个效果)"""
        return real(*a, **kw)[:-1]

    mp = pytest.MonkeyPatch()
    mp.setattr(smod, "transfer_receivers", _drifted)
    warns, close = _capture_warnings()
    try:
        with store.get_conn() as c:
            p._check_transfer_in(c, dry_run=False)
    finally:
        close()
        mp.undo()

    assert len(p.notifier.sent) == 1, "漂移只该记一笔账,绝不能把推送打断"
    drift = [w for w in warns if "漂移" in w]
    assert drift, f"谓词漂移了却一声不吭,日志里只有:{warns}"
    assert "3" in drift[0] and "2" in drift[0], \
        f"告警里必须写清楚两边各是多少,否则排查时无从下手:{drift[0]}"


# ============================================================
# 指定用户的转入逐条推送(/tin)
# ============================================================
# ⚠️ 门槛/上限一律**写死字面量**并显式配环境变量,不从 config / poller import ——
#    从被测模块 import 门槛再拿它断言,等于用被测代码给自己打分(这个文件里
#    转入告警那几条已经踩过一次:四种变异全绿)。
def _tin_env(monkeypatch, watch_usd=100, alert_usd=500, receivers=3) -> None:
    """
    把两个**语义不同**的门槛都钉死,再构造 Poller。

    ⚠️ 必须显式配 alert 那两项:仓库根目录的 .env 会覆盖 config 的默认值,
       不钉死的话这些用例的结果取决于"这台机器怎么配的"。
    """
    _env(monkeypatch,
         FOMO_TRANSFER_WATCH_MIN_USD=watch_usd,
         FOMO_TRANSFER_ALERT_MIN_USD=alert_usd,
         FOMO_TRANSFER_ALERT_RECEIVERS=receivers)


def _mark_tin(handle: str, on: bool = True) -> None:
    """打开/关闭某个人的转入逐条推送"""
    with store.get_conn() as c:
        ok, msg = store.set_transfer_watch(c, handle, on)
    assert ok, f"前提不成立,开关没拨动:{msg}"


def _deposit(i: int = 0, usd: float = 2439.09, minutes_ago: float = 0.0, **kw) -> dict:
    """一笔**刚刚到账**的转入,形态取真实报文;默认金额就是真实那笔 $2,439.09"""
    ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return dict(_REAL_DEPOSIT, id=f"tin-{i}", usdAmount=usd, createdAt=ts, **kw)


def _tick_transfers(raws, *, uid="uA", ok=True, poller=None):
    """跑一轮完整 tick,快照里只有这些转账。返回 (poller, notifier)"""
    client = FakeClient({uid: UserSnapshot(uid, swaps=[], transfers=list(raws),
                                           thesis=[], balances=[])})
    notifier = FakeNotifier(ok=ok)
    p = poller or Poller(client, notifier)
    p.client, p.notifier = client, notifier
    p.tick()
    return p, notifier


def _sent_flags() -> list[int]:
    with store.get_conn() as c:
        return [r["sent"] for r in
                c.execute("SELECT sent FROM fomo_events ORDER BY event_id")]


def test_没被点名的人的转账绝不逐条推送(db, monkeypatch):
    """
    ⚠️⚠️ **本功能最危险的回归**:这道门一旦漏,全名单的转入约 807 条/天
       (实测人均 8.49 条/天),把真正要看的买卖推送整个淹掉,而且没有任何报错。
       金额故意给到 $250,000 —— 门槛拦不住它,唯一拦得住的就是"他没被点名"。
    """
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")                       # 刻意**不**开 /tin
    p, notifier = _tick_transfers([_deposit(usd=250_000.0)])

    assert notifier.sent == [], f"没被点名的人的转入被推了出去:{notifier.sent}"
    assert _sent_flags() == [1], "没推的转账必须当场 mark_sent,否则补发队列会反复捞它"
    assert len(p._new_transfers) == 1, "但它仍然要喂给聚合告警(两个信号互不影响)"


def test_被点名的人的转入够门槛就逐条推(db, monkeypatch):
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    p, notifier = _tick_transfers([_deposit()])

    assert len(notifier.sent) == 1, f"该推一条,实际 {len(notifier.sent)} 条"
    msg = notifier.sent[0]
    assert msg.startswith("📥"), f"行首锚点应是 📥(收到转入):{msg[:40]}"
    assert "$2,439.09" in msg and "PoorGoat" in msg
    assert msg.split("\n")[-1] == f"<code>{_CA_FIH}</code>", "CA 必须独占最后一行"
    assert _sent_flags() == [1], "TG 确认收到之后才置 sent=1"


def test_金额不到门槛的转入不推_但照常落库(db, monkeypatch):
    """
    ⚠️ 门槛是这个功能能不能用的分水岭:不设门槛人均 8.49 条/天,≥$100 只剩 1.10 条/天。
       绝大多数被拦下的是几美元的空投灰尘。
    ⚠️ 边界必须是 >= :恰好等于门槛的那一笔是"够了",不是"差一点"。
    """
    _tin_env(monkeypatch, watch_usd=100)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")

    _, notifier = _tick_transfers([_deposit(1, usd=99.99)])
    assert notifier.sent == [], f"$99.99 低于 $100 门槛,不该推:{notifier.sent}"
    assert _sent_flags() == [1], "不推也要 mark_sent"

    _, notifier = _tick_transfers([_deposit(2, usd=100.0)])
    assert len(notifier.sent) == 1, "恰好等于门槛的必须推(>= 不是 >)"


def test_门槛取的是转入推送那项配置_不是聚合告警那项(db, monkeypatch):
    """
    ⚠️⚠️ 两个门槛语义完全不同,复用就是让两个功能互相改灵敏度:
         fomo_transfer_alert_min_usd  →「同一个币被**几个人**收到」的聚合信号,默认 $500
         fomo_transfer_watch_min_usd  →「**这一个人**又进货了」,默认 $100
       两个方向各测一次,任何一次拿错配置项都会红。
    """
    _tin_env(monkeypatch, watch_usd=100, alert_usd=5000)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    _, notifier = _tick_transfers([_deposit(1)])            # $2,439.09
    assert len(notifier.sent) == 1, \
        "$2,439.09 ≥ 转入推送门槛 $100 就该推 —— 拿去比聚合告警的 $5000 才会推不出来"

    _tin_env(monkeypatch, watch_usd=5000, alert_usd=100)
    _add_ready("uB", "Holder2")
    _mark_tin("Holder2")
    _, notifier = _tick_transfers([_deposit(2)], uid="uB")
    assert notifier.sent == [], \
        "$2,439.09 < 转入推送门槛 $5000,不该推 —— 拿去比聚合告警的 $100 就会漏出来"


def test_转入推送的默认门槛(monkeypatch):
    """
    ⚠️ 写死字面量,并用 _env_file=None 构造 —— 否则断言的是"这台机器怎么配的",
       而不是"代码的默认值是多少"。
    ⚠️ 默认 100 的依据(本地库 20 天真实数据,人均条/天):
       不设门槛 8.49 · ≥$100 1.10 · ≥$500 0.73。$500 会开始筛掉真的小额建仓。
    """
    from src.config import FomoSettings

    for k in ("FOMO_TRANSFER_WATCH_MIN_USD", "FOMO_TRANSFER_ALERT_MIN_USD"):
        monkeypatch.delenv(k, raising=False)
    s = FomoSettings(_env_file=None)
    assert s.fomo_transfer_watch_min_usd == 100.0
    assert s.fomo_transfer_alert_min_usd == 500.0, "两个门槛是两个数,不是同一个"


def test_方向不明或计价币不推(db, monkeypatch):
    """
    方向判不出时"收到"这个说法本身就没依据(那笔可能是转出);
    USDC 到账是在给自己充钱,不是"他拿到了某个币"。两者都与"他进货了"无关。
    """
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    no_side = _deposit(1)
    no_side.pop("type")                                  # 方向字段没了 → side_unknown
    usdc = _deposit(2, tokenAddress="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    tokenMetadata={"symbol": "USDC"})
    _, notifier = _tick_transfers([no_side, usdc])
    assert notifier.sent == [], f"这两条都不该推:{notifier.sent}"
    assert _sent_flags() == [1, 1], "不推也要 mark_sent"


def test_停机补数那一轮不逐条推转入(db, monkeypatch):
    """
    ⚠️ 停机后第一轮的转账是**整段积压**,而消息里写的是"N 分钟前到账" ——
       补数轮逐条推等于把几百条过期消息一次喷出来(与推送侧/跟单侧同一条守卫)。
    ⚠️ 光断言"没推出去"是不够的:`_dispatch` 在补数轮会整段改走汇总,
       于是**判定漏了也照样绿**(实测:把补数轮那道门删掉,只看推送结果的断言全绿)。
       所以这里同时钉死"这条转入根本没进逐条推送队列",而队列内容是
       _persist 的返回值 —— 可观测,且不依赖被测函数自己的判断。
    """
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    _stale_tick(8)                                       # 8 小时没跑过 → 补数轮
    p, notifier = _tick_transfers([_deposit()])

    assert not [m for m in notifier.sent if m.startswith("📥")], \
        f"补数轮不该有逐条转入推送:{notifier.sent}"
    assert _sent_flags() == [1], "补数轮的转账必须当场 mark_sent"

    p2 = Poller(FakeClient(), FakeNotifier())
    p2._catchup_since = now_iso()
    p2._tx_watch_ids = {"uA"}
    with store.get_conn() as c:
        queued = p2._persist(c, p2.normalize_transfers(_row(), [_deposit(9)]))
    assert queued == [], "补数轮的转入绝不能进逐条推送队列(积压信号已经过期)"


def test_推送失败绝不mark_sent_下一轮补发(db, monkeypatch):
    """
    ⚠️ 转账原来走的是"落库即 mark_sent"。改成推送路径之后,若还照旧当场标已发,
       TG 一次 400 / 网络一次抖动就是**这条消息永久丢失** —— 下一 tick 该事件
       已在库里,INSERT OR IGNORE 直接跳过,再也不会被重新发现。
    """
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")

    p, notifier = _tick_transfers([_deposit()], ok=False)
    assert len(notifier.sent) == 1, "试过发一次"
    assert _sent_flags() == [0], "TG 没收到,sent 必须还是 0"

    # 下一轮:补发队列把它捞回来重发
    p, notifier = _tick_transfers([], poller=p)
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("📥"), \
        f"补发队列没把它捞回来:{notifier.sent}"
    assert _sent_flags() == [1]


def test_补发队列不许把没点名的人的转入漏出去(db, monkeypatch):
    """
    ⚠️⚠️ 补发队列捞的是"库里所有 10 分钟内没发出去的行",它并不知道这条转入
       当初为什么留在那里。判定只写在落库侧的话,任何一条 sent=0 的转入都会
       顺着补发路径推出去 —— 也就是把"只推被点名的几个人"悄悄变成全名单。
       (这里用最自然的方式造出这种行:推送失败 → 用户随后把 /tin 关掉。)
    """
    _tin_env(monkeypatch)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    p, notifier = _tick_transfers([_deposit()], ok=False)
    assert _sent_flags() == [0], "前提不成立:库里没有待补发的转入"

    _mark_tin("PoorGoat_", on=False)                     # 用户改主意了
    p, notifier = _tick_transfers([], poller=p)
    assert notifier.sent == [], f"已经关掉的人的转入被补发出去了:{notifier.sent}"
    assert _sent_flags() == [1], "不推的必须就地排掉,否则它会每轮被捞一次、连捞 10 分钟"


def test_被点名的转入照样喂给聚合告警_人数不许少算(db, monkeypatch):
    """
    ⚠️ 同一条 TRANSFER_IN 既触发单人推送、又计入聚合告警的人数,**这是对的,不许去重**:
       两个信号回答的是不同的问题(这个人又进货了 / 这个币在被分发给一群人)。
       落库侧要是把点名的那条从 transfers 列表里摘出去,聚合告警就会少算一个人 ——
       3 人门槛下少一个人就是整条告警消失。

    ⚠️⚠️ **被点名的那个人必须是最后到的那一个**,否则这条测试抓不到东西:
       _check_transfer_in 只拿 _new_transfers 当"本轮该查哪些币"的候选集,
       真正的人数是 SQL 从库里数的。只要同一轮里还有别人的转账把这个币带进候选集,
       摘不摘那一条都照样告警 —— 实测:让被点名的人第一个到,把 transfers.append
       删掉之后这条用例仍然全绿。所以这里让前两个人先到(不够门槛、不告警),
       再让**被点名的人**补上第三个 —— 这一轮的候选集里只有他那一条。
    """
    _tin_env(monkeypatch, alert_usd=500, receivers=3)
    for i in range(3):
        _add_ready(f"u{i}", f"Holder{i}")
    _mark_tin("Holder2")                                  # 最后到的那个人被点名

    snaps = {f"u{i}": UserSnapshot(f"u{i}", swaps=[],
                                   transfers=[_deposit(i, usd=901.37 + i)],
                                   thesis=[], balances=[]) for i in range(2)}
    snaps["u2"] = UserSnapshot("u2", swaps=[], transfers=[], thesis=[], balances=[])
    notifier = FakeNotifier()
    p = Poller(FakeClient(snaps), notifier)
    for _ in range(2):                                    # 轮转:每轮 1 个 → u0、u1 先进库
        p.tick()
    assert notifier.sent == [], "才 2 个人收到,不该有任何推送(前提不成立的话下面都白测)"

    snaps["u2"].transfers = [_deposit(2, usd=903.37)]     # 第三个人(被点名的)到货
    p.tick()

    tin = [m for m in notifier.sent if m.startswith("📥")]
    sig = [m for m in notifier.sent if m.startswith("🚨")]
    assert len(tin) == 1, f"被点名的那个人该有一条逐条推送:{notifier.sent}"
    assert len(sig) == 1, \
        "聚合告警没发 —— 被点名的那条没被喂给它,这个币这一轮压根没进候选集"
    assert "3 人「收到」" in sig[0], f"聚合告警少算了人:{sig[0].splitlines()[0]}"


def test_转入绝不进跟单信号(db, monkeypatch):
    """
    ⚠️⚠️ 铁律:「收到免费筹码」与「自己掏钱买入」是相反的含义,拿它去触发花钱的操作
       方向就是错的。转入现在会进 new_events(为了走推送路径),而 _check_copytrade
       吃的正是 new_events —— 这条测试钉死那道 EVENT_BUY 过滤。
    """
    _tin_env(monkeypatch)
    _enable_copy(min_buyers=1)
    _add_ready("uA", "PoorGoat_")
    _mark_tin("PoorGoat_")
    _, notifier = _tick_transfers([_deposit()])

    assert _signals() == [], f"转入生成了跟单信号:{_signals()}"
    assert not [m for m in notifier.sent if "跟单" in m], \
        f"转入触发了跟单推送:{notifier.sent}"


# ---- 采集:被点名的人跳出轮转 --------------------------------------------
def _tin_rows():
    with store.get_conn() as c:
        return store.list_active_users(c)


def test_被点名的人每轮都拉转账_不参与轮转(db):
    """
    ⚠️ 转入既然等于这些人的买入,时效就该和买入推送一样(每轮)。
       靠轮转的话 60 人 → ⌈60/20⌉ = 3 个/轮 → 一圈 20 轮 ≈ 5 分钟才轮到他一次。
    ⚠️ 门槛写死字面量(60 人 · 20 轮 → 3,再加被点名的 1 个 = 4),
       不从 poller import 周期常量再拿它算门槛。
    """
    _many_users(60)
    _mark_tin("h007")
    rows = _tin_rows()
    client = FakeClient()
    p = Poller(client, FakeNotifier())

    batches = []
    for _ in range(5):
        p._refresh_watched_index(rows)
        before = len(client.transfer_calls)
        p._fetch_snapshots(rows)
        batches.append(client.transfer_calls[before:])

    missed = [i for i, b in enumerate(batches) if "u007" not in b]
    assert missed == [], f"被点名的人在第 {missed} 轮没被拉到 —— 他仍然在跟着轮转走"
    assert max(len(b) for b in batches) <= 4, \
        f"单轮转账请求峰值 {max(len(b) for b in batches)},超过 ⌈60/20⌉ + 1 = 4"


def test_点名不许破坏轮转_没被点名的人仍要在一圈内全被覆盖(db):
    """
    ⚠️ 分摊的代价是覆盖延迟,而覆盖必须**可证**:60 人 · 每轮 3 个 → 20 轮里
       每个人至少被轮到一次,一个都不能漏。
    ⚠️ 中途故意把点名对象换掉一次:如果实现把被点名的人从轮转池里**摘出去**,
       池子的长度/内容就会随开关变化,而在长度会变的列表上取模不构成扫描 ——
       会有人永远排不上队(_rotate_transfers 的 docstring 里写明了这条)。
    """
    _many_users(60)
    _mark_tin("h007")
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    for i in range(20):
        if i == 10:                                     # 用户改主意,换一个人盯
            _mark_tin("h007", on=False)
            _mark_tin("h042")
        rows = _tin_rows()
        p._refresh_watched_index(rows)
        p._fetch_snapshots(rows)

    missed = {u["user_id"] for u in _tin_rows()} - set(client.transfer_calls)
    assert missed == set(), f"一圈下来有 {len(missed)} 个人一次都没被轮到:{sorted(missed)[:5]}"


def test_点名不改变轮转对其余人的调度(db):
    """
    ⚠️ 这条把"轮转本身一个字节都不动"变成可执行的断言:开不开 /tin、开几个人,
       **没被点名的人被轮到的次序必须逐轮一模一样**。
    ⚠️ 为什么不能只断言"一圈内都被覆盖到":把被点名的人从轮转池里摘出去(这是最
       自然的写法)之后,取模基数从 60 变成 48 —— 覆盖照样完成,只是节奏全变了,
       而"在长度会变的列表上取模不构成扫描"正是 _rotate_transfers 明确警告过的坑
       (名单人数、点名人数都会变)。实测:只断言覆盖时那个改法全绿。
    """
    _many_users(60)
    rows = _tin_rows()
    c1 = FakeClient()
    p1 = Poller(c1, FakeNotifier())
    p1._refresh_watched_index(rows)                      # 一个人都没点名
    base = []
    for _ in range(20):
        before = len(c1.transfer_calls)
        p1._fetch_snapshots(rows)
        base.append(c1.transfer_calls[before:])

    for i in range(12):
        _mark_tin(f"h{i:03d}")
    rows2 = _tin_rows()
    marked = {f"u{i:03d}" for i in range(12)}
    c2 = FakeClient()
    p2 = Poller(c2, FakeNotifier())
    got = []
    for _ in range(20):
        p2._refresh_watched_index(rows2)
        before = len(c2.transfer_calls)
        p2._fetch_snapshots(rows2)
        got.append(sorted(u for u in c2.transfer_calls[before:] if u not in marked))

    want = [sorted(u for u in b if u not in marked) for b in base]
    first_bad = next((i for i, (a, b) in enumerate(zip(got, want, strict=True)) if a != b), None)
    assert got == want, (
        "点名之后,没被点名的人被轮到的次序变了 —— 轮转的取模基数被改动过;"
        f"第 {first_bad} 轮开始不一致")


def test_点名人数超上限就退回轮转并告警(db):
    """
    ⚠️ 每多点名一个人,每轮就多一个请求。稳态每轮约 104 个请求、间隔 15s、
       单轮 5~18s —— 余量不多,不封顶就会把一个已经超预算的 tick 推得更糟。
    ⚠️ 退回轮转只影响**时效**(最迟一圈),推送该来的一条都不会少;
       但必须**大声告警**,否则用户以为开着、实际时效已经不成立了。
    ⚠️ 上限写死 12(= 默认并发度,恰好多一整波),不从 poller import。
    """
    _many_users(60)
    for i in range(13):                                  # 13 > 12
        _mark_tin(f"h{i:03d}")
    rows = _tin_rows()
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    p._refresh_watched_index(rows)

    warned, close = _capture_warnings()
    try:
        p._fetch_snapshots(rows)
    finally:
        close()

    assert len(client.transfer_calls) <= 3, \
        f"超上限时该退回纯轮转(⌈60/20⌉ = 3 个),实际打了 {len(client.transfer_calls)} 个"
    assert [w for w in warned if "超过上限" in w], f"退回轮转必须留痕,实际日志:{warned}"


def test_点名十二个人时每轮多打十二个请求(db):
    """
    成本核算的**可执行版本**:上限内的 K 个人,每轮最多多 K 个请求。
    ⚠️ 断言的是上界(轮转批次可能与点名的人重合,重合就是白赚的),
       但"最多多 K 个"这条必须成立 —— 否则请求预算的算式就是假的。
    """
    _many_users(60)
    for i in range(12):
        _mark_tin(f"h{i:03d}")
    rows = _tin_rows()
    client = FakeClient()
    p = Poller(client, FakeNotifier())
    p._refresh_watched_index(rows)

    peaks = []
    for _ in range(10):
        before = len(client.transfer_calls)
        p._fetch_snapshots(rows)
        peaks.append(len(client.transfer_calls) - before)
    assert max(peaks) <= 3 + 12, f"单轮转账请求峰值 {max(peaks)},超过 ⌈60/20⌉ + 12"
    assert min(peaks) >= 12, f"被点名的 12 个人每轮都该被拉到,实际最少的一轮只有 {min(peaks)} 个"
