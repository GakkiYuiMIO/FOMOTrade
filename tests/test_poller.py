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
from datetime import UTC, datetime, timedelta

import pytest

from src import store
from src.client import NotSupportedError, UserSnapshot
from src.models import EVENT_BUY, now_iso
from src.poller import Poller
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

    def __init__(self, snaps: dict | None = None, *, seed_supported: bool = False):
        self.snaps = snaps or {}
        self.seed_supported = seed_supported

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

    def get_balances(self, user_id: str) -> list:
        snap = self.snaps.get(user_id)
        return (snap.balances if snap else []) or []


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
        def fetch_snapshot(self, user_id: str):
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
