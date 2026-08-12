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
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from src import store
from src.client import NotSupportedError, UserSnapshot
from src.models import EVENT_BUY, now_iso
from src.poller import _SEND_BURST, Poller
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
                 feed: list | None = None, feed_boom: Exception | None = None):
        self.snaps = snaps or {}
        self.seed_supported = seed_supported
        self.feed = feed if feed is not None else []
        self.feed_boom = feed_boom
        self.feed_calls = 0

    def get_activity_feed(self, limit: int = 100) -> list:
        self.feed_calls += 1
        if self.feed_boom:
            raise self.feed_boom
        return list(self.feed)

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


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把 store.DB_PATH 指到临时文件 —— Poller 内部走 get_conn(),必须有真实文件"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    return store.DB_PATH


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


# ============================================================
# 活动流(/feed/tradingActivity)当变更检测器
# ⚠️ 这条路省掉的是"每轮 69 次 swaps"。它一旦静默失效,
#    表现是"监控还在跑但再也不报新交易" —— 比直接报错难查得多。
# ============================================================
def _feed_swap(fid: str, uid: str, ca: str = CA_TOAD) -> dict:
    return {"type": "swap_buy", "id": fid, "userId": uid, "createdAt": "2026-08-11T15:00:00.000Z",
            "tokenAddress": ca, "networkId": 1399811149, "ticker": "TOAD", "usdAmount": 100.0}


def _feed_thesis(fid: str, uid: str, text: str = "看好这个", ca: str = CA_TOAD) -> dict:
    return {"type": "thesis", "id": fid, "userId": uid, "createdAt": "2026-08-11T15:00:00.000Z",
            "comment": {"comment": text, "tokenAddress": ca, "networkId": 1399811149},
            "authorTrade": {"usdValue": 500.0}}


def test_活动流命中时不再全员拉swaps(db):
    """这一步是整轮提速的大头:69 次 swaps(3~5s)压成 1 次活动流(0.35s)"""
    for i in range(30):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    client = CountingClient({f"u{i:02d}": _snap(f"u{i:02d}") for i in range(30)})
    p = Poller(client, FakeNotifier())
    p.tick()                                     # 冷启动:全员扫一遍建基准
    assert len(client.calls["swaps"]) == 30

    client.calls = {"swaps": [], "balances": [], "trades": []}
    client.feed = [_feed_swap("f1", "u07")]      # 只有 u07 有动作
    p.tick()
    asked = set(client.calls["swaps"])
    assert "u07" in asked, "活动流点名的人必须拉"
    assert len(asked) < 30, "其余人不该再全员拉一遍"
    assert client.feed_calls >= 2


def test_活动流失败必须退回全员扫描(db):
    """
    ⚠️ "流挂了"与"没人有动作"绝不能是同一个返回值 ——
       混在一起的表现就是监控静悄悄地停止工作,日志上还一切正常。
    """
    for i in range(5):
        _add_ready(f"u{i}", f"h{i}")
    client = CountingClient({f"u{i}": _snap(f"u{i}") for i in range(5)})
    p = Poller(client, FakeNotifier())
    p.tick()

    client.calls = {"swaps": [], "balances": [], "trades": []}
    client.feed_boom = RuntimeError("活动流 503")
    p.tick()
    assert set(client.calls["swaps"]) == {f"u{i}" for i in range(5)}


def test_活动流登录态失效必须上抛(db):
    from src.auth import AuthError

    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")}, feed_boom=AuthError("token 过期"))
    with pytest.raises(AuthError):
        Poller(client, FakeNotifier()).tick()


def test_兜底轮转能捞到活动流看不见的人(db):
    """
    活动流只覆盖"当前登录账号关注的人"。名单里没关注的人永远不出现在流里,
    只能靠兜底轮转扫描捞回来 —— 这不是优化项,是正确性要求。
    """
    for i in range(30):
        _add_ready(f"u{i:02d}", f"h{i:02d}")
    client = CountingClient({f"u{i:02d}": _snap(f"u{i:02d}") for i in range(30)})
    p = Poller(client, FakeNotifier())
    p.tick()

    swept: set[str] = set()
    for _ in range(10):                          # 30 人 / 每轮 12 个 → 几轮内必须全覆盖
        client.calls["swaps"] = []
        p.tick()
        swept |= set(client.calls["swaps"])
    assert swept == {f"u{i:02d}" for i in range(30)}, f"漏扫: {30 - len(swept)} 人"


def test_活动流里的观点不额外调用就能成事件(db):
    """观点从"轮转扫币最坏 4 分钟才轮到"变成"下一轮就推",且不多打一个请求"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")})
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    p.tick()

    client.feed = [_feed_thesis("th-1", "uA", "这个币要起飞")]
    p.tick()
    assert any("这个币要起飞" in m for m in notifier.sent), \
        f"活动流里的观点必须直接成事件,实际发出:{notifier.sent}"


def test_同一条观点两条路都抓到也只推一次(db):
    """活动流与按币扫描会重叠。两边都用原生 id 生成 event_id,靠主键去重"""
    _add_ready("uA", "alice")
    raw = _feed_thesis("th-dup", "uA", "重复的观点")
    client = CountingClient({"uA": _snap("uA")}, feed=[raw])
    notifier = FakeNotifier()
    p = Poller(client, notifier)
    p.tick()
    n_first = len(notifier.sent)

    # 第二轮:同一条既在活动流里、又被按币扫描捞到
    p._token_meta = {("solana", CA_TOAD): {"network_raw": 1399811149}}
    client.get_token_thesis = lambda ca, net, after_ms=None, limit=100: [raw]
    p.tick()
    assert len(notifier.sent) == n_first, "同一条观点绝不能推两遍"


def test_活动流里已见过的条目不再重复触发(db):
    """流是滚动窗口,同一条会在里面待很久。不记 id 的话每轮都当成新动作,提速全白做"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")}, feed=[_feed_swap("f1", "uA")])
    p = Poller(client, FakeNotifier())
    p.tick()
    p.tick()                                     # 让记忆稳定下来

    client.calls["trades"] = []
    p.tick()                                     # 流里还是那条 f1
    assert client.calls["trades"] == [], "旧条目不该再被当成新动作"


def test_活动流记忆不会无界增长(db):
    """滚动窗口里滚出去的 id 必须一起忘掉,否则跑几天就是一个只增不减的集合"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")}, feed=[_feed_swap(f"f{i}", "uA") for i in range(50)])
    p = Poller(client, FakeNotifier())
    p.tick()
    p.tick()
    assert len(p._feed_seen) == 50

    client.feed = [_feed_swap("f99", "uA")]      # 窗口整个滚过去了
    p.tick()
    assert p._feed_seen == {"f99"}


def test_活动流里名单外的人一律忽略(db):
    """流里理论上只有关注的人,但绝不能因此就不过滤 —— 多一层不值钱,少一层会推错人"""
    _add_ready("uA", "alice")
    client = CountingClient({"uA": _snap("uA")}, feed=[_feed_swap("f1", "stranger")])
    p = Poller(client, FakeNotifier())
    p.tick()
    client.calls = {"swaps": [], "balances": [], "trades": []}
    p.tick()
    assert "stranger" not in client.calls["swaps"]
    assert "stranger" not in client.calls["trades"]


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
    client = CountingClient({f"u{i:02d}": _snap(f"u{i:02d}") for i in range(30)})
    p = Poller(client, FakeNotifier())
    p.tick()                                          # 冷启动

    client.feed = [_feed_swap("f1", "u07")]           # u07 买入
    p.tick()
    assert "u07" in p._bal_dirty, "有新动作的人必须被挂上脏标记"

    client.feed = []                                  # 下一轮他已经不 hot 了
    client.calls["balances"] = []
    p.tick()
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
