"""
🏅 盈利榜持有人**接进推送主路径**的测试:poller 的买卖推送、转入逐条(只读缓存)、
pump.fun 成交推送,以及"哪些推送**没有**这一块"。

⚠️ 全部离线(假 client + 真实夹具),一个请求都不打。
⚠️ 断言写死字面量。
"""
# ruff: noqa: N802
from __future__ import annotations

import json
import pathlib

import pytest

from src import boardholders as bh
from src import store
from src.boardholders import BoardHoldersLookup
from src.config import get_settings
from src.dexscreener import PoolQuoteLookup
from src.poller import Poller
from tests.test_poller import (
    _CA_AI,
    FakeClient,
    FakeNotifier,
    UserSnapshot,
    _add_ready,
    _FakeDex,
    _rh_swap,
)

FIX = pathlib.Path(__file__).parent / "fixtures"
BOARD_RAW = json.loads((FIX / "fomo_leaderboard_24h.json").read_text(encoding="utf-8"))
# 榜单第 1 名(unipcs)与第 144 名(deliveryydriver)的真实 UUID
UID_1 = "36adb85a-c0fd-5fa8-916d-8fdc32fe4237"
UID_144 = "716957e5-6177-5bb1-94e7-92364bb327b7"


@pytest.fixture
def db(tmp_path):
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "board.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _pin_env(monkeypatch):
    monkeypatch.setenv("FOMO_PROXY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _holders(*uids, total=None):
    rows = [{"humanAmount": 1000000.0 + i, "pnl": -999999.0,
             "user": {"id": u, "userHandle": "x", "displayName": "X"}}
            for i, u in enumerate(uids)]
    return {"totalHolders": len(rows) if total is None else total, "topHolders": rows}


class BoardFake:
    """假 FOMO client:只实现这一块用得着的两个端点,并记下每次调用。"""

    def __init__(self, holders=None, board=None, board_boom=None, holders_boom=None):
        self._holders = holders or {}
        self._board = BOARD_RAW if board is None else board
        self._board_boom = board_boom
        self._holders_boom = holders_boom
        self.board_calls = 0
        self.holder_calls: list[str] = []

    def get_leaderboard(self, period="24h", limit=20, *, auth_invalidate=True,
                        fast_fail):
        self.board_calls += 1
        if self._board_boom is not None:
            raise self._board_boom
        return self._board

    def get_top_holders(self, token_address, network_id, *, auth_invalidate=True,
                        fast_fail):
        self.holder_calls.append(token_address)
        if self._holders_boom is not None:
            raise self._holders_boom
        return self._holders.get(token_address, {})

    def close(self):
        pass


def _poller(client, notifier, dex, board_client):
    p = Poller(client, notifier)
    p._pool_lookup = PoolQuoteLookup(client=dex)
    p._board_holders = BoardHoldersLookup(board_client, board_cache=bh._BoardCache())
    return p


def _one_buy(sid="a1"):
    _add_ready("uA", "alice")
    return FakeClient({"uA": UserSnapshot("uA", swaps=[_rh_swap(sid)], transfers=[],
                                          thesis=[], balances=[])})


# ============================================================
# poller:买入推送
# ============================================================
def test_买入推送带上这一块(db):
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1, UID_144, total=15305)})
    _poller(_one_buy(), tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1
    lines = tg.sent[0].split("\n")
    assert "🏅 盈利榜持有人 ≥2 人" in lines
    assert any(ln.startswith("   #1 「unipcs」") for ln in lines)
    assert any(ln.startswith("   #144 「deliveryydriver」") for ln in lines)
    assert "   ⚠️ 只比对了前 2/15,305 名持有人,榜只到前 150 名 —— 没显示≠没有" in lines
    # ⚠️ 位置:在 🧬 链之前、CA 独占最后一行的规矩不变
    assert lines.index("🏅 盈利榜持有人 ≥2 人") < next(
        i for i, ln in enumerate(lines) if ln.startswith("🧬"))
    assert lines[-1].startswith("<code>")


def test_零命中时整块不出现(db):
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders("not-on-board")})
    _poller(_one_buy(), tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1
    assert "🏅" not in tg.sent[0]
    assert "0 人" not in tg.sent[0]


def test_一个tick只拉一次榜单(db):
    """⚠️ 榜与币无关:三条推送、三个持有人请求,但榜只拉一次。"""
    _add_ready("uA", "alice")
    # ⚠️ 从 1 起编号:全 0 的那个是零地址,models 会把它当计价/无效币筛掉
    swaps = [_rh_swap(f"a{i}", ca=f"0x{i:040x}") for i in (1, 2, 3)]
    client = FakeClient({"uA": UserSnapshot("uA", swaps=swaps, transfers=[],
                                            thesis=[], balances=[])})
    tg = FakeNotifier()
    bc = BoardFake(holders={f"0x{i:040x}": _holders(UID_1) for i in (1, 2, 3)})
    _poller(client, tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 3
    assert bc.board_calls == 1
    assert len(bc.holder_calls) == 3


def test_榜单请求失败时推送照发(db):
    """⚠️⚠️ **绝不能出现"因为查不到盈利榜所以整条推送没发出去"**。"""
    tg = FakeNotifier()
    bc = BoardFake(board_boom=RuntimeError("上游 500"))
    _poller(_one_buy(), tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1
    assert "🏅" not in tg.sent[0]
    assert "$AI" in tg.sent[0] and tg.sent[0].startswith("🌱")


def test_持有人请求失败时推送照发(db):
    tg = FakeNotifier()
    bc = BoardFake(holders_boom=TimeoutError("超时"))
    _poller(_one_buy(), tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1
    assert "🏅" not in tg.sent[0]


def test_401不会把推送带走也不会停轮询(db):
    """
    ⚠️⚠️ AuthError 在 poller 里的处置是**停机**。这一块的 401 绝不许捅到主路径去,
       否则一次"锦上添花"的鉴权抖动就能让整个监控停下来。
    """
    from src.auth import AuthError

    tg = FakeNotifier()
    bc = BoardFake(board_boom=AuthError("HTTP 401"))
    assert _poller(_one_buy(), tg, _FakeDex(), bc).tick() == 1
    assert len(tg.sent) == 1
    assert "🏅" not in tg.sent[0]


def test_lookup整个崩了也只丢这一块(db):
    class Boom(BoardHoldersLookup):
        def lookup(self, pairs):
            raise RuntimeError("内部炸了")

    tg = FakeNotifier()
    p = Poller(_one_buy(), tg)
    p._pool_lookup = PoolQuoteLookup(client=_FakeDex())
    p._board_holders = Boom(BoardFake(), board_cache=bh._BoardCache())
    p.tick()
    assert len(tg.sent) == 1 and "🏅" not in tg.sent[0]


def test_每个tick重置预算(db):
    """⚠️ begin_round 没被调 = 第二轮起额度是空的,这一块永远不再出现。"""
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1)})
    p = _poller(_one_buy("a1"), tg, _FakeDex(), bc)
    p._board_holders._per_round = 1
    p.tick()
    assert "🏅" in tg.sent[0]
    # 第二轮换一个币(绕开内存缓存),额度必须已经重置
    _add_ready("uA", "alice")
    p.client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_rh_swap("a2", ca="0x" + "b" * 40)], transfers=[], thesis=[],
        balances=[])})
    bc._holders["0x" + "b" * 40] = _holders(UID_144)
    p.tick()
    assert len(tg.sent) == 2
    assert "🏅" in tg.sent[1]


# ============================================================
# 只读缓存的那条路径(转入逐条)
# ============================================================
def test_转入推送走只读缓存一个请求都不发(db):
    """
    ⚠️ /tin 那条路径 poller 传 cached_only=True,走 boardholders.cached():
       缓存里没有就整块不出现,**一个请求都不发**。这里直接驱动生产函数
       poller._name_extras(与转入推送用的是同一个调用),证据比端到端更直接。
    """
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1)})
    p = _poller(FakeClient({}), tg, _FakeDex(), bc)

    names = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True)

    assert "board_holders" not in names and "board_scope" not in names
    assert bc.holder_calls == [] and bc.board_calls == 0


def test_买卖那条路径才会发请求(db):
    """⚠️ 反向对照:cached_only=False 时用的是本轮批量查好的那份(仍然不在这里发请求)。"""
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1)})
    p = _poller(FakeClient({}), tg, _FakeDex(), bc)
    blocks = p._board_holders.lookup([("robinhood", _CA_AI)])

    names = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=False,
                           boards=blocks)

    assert names["board_scope"][0] == 1
    assert names["board_holders"][0][:2] == (1, "unipcs")


def test_缓存里有的时候转入推送带得上():
    """⚠️ 只读缓存命中时它是能出现的(实际上 90 秒的 TTL 让这几乎不会发生,如实记在 README)。"""
    bc = BoardFake(holders={_CA_AI: _holders(UID_1)})
    lk = BoardHoldersLookup(bc, board_cache=bh._BoardCache())
    lk.lookup([("robinhood", _CA_AI)])
    blk = lk.cached("robinhood", _CA_AI)
    assert blk is not None
    assert blk.render_args()["board_scope"][0] == 1


# ============================================================
# 没有这一块的那些推送
# ============================================================
def test_转入聚合告警没有这一块(db):
    """
    ⚠️ 转入聚合(分发预警)讲的是"谁把币分给了谁",而且它有长度预算(_fit_signal),
       再塞四行会把主体挤掉 —— 刻意不接。这条钉住"不接"这件事。
    """
    import inspect

    from src.formatter import render_transfer_in_signal

    params = inspect.signature(render_transfer_in_signal).parameters
    assert "board_holders" not in params
    assert "board_scope" not in params


def test_ca与chips两条命令一个字都没改():
    """
    ⚠️ `/ca` 与 `/chips` 明确不改。它们全在 bot.py 里拼,这条从源码层面钉住
       bot.py 根本没碰过这一块。
    """
    src = (pathlib.Path(__file__).parent.parent / "src" / "bot.py").read_text(
        encoding="utf-8")
    assert "boardholders" not in src
    assert "🏅" not in src
    assert "盈利榜持有人" not in src


def test_币安上新与pump喊单也没有这一块():
    """⚠️ 币安 Alpha 的币不在 FOMO 的持有人榜口径内;pump 喊单不是成交。"""
    import inspect

    from src.formatter import render_alpha_listing, render_pump_callout

    for fn in (render_alpha_listing, render_pump_callout):
        assert "board_holders" not in inspect.signature(fn).parameters, fn.__name__


# ============================================================
# pump.fun 成交推送
# ============================================================
def test_pump成交推送带上这一块(monkeypatch):
    """⚠️ 走 PumpWatcher 的真实渲染路径(_name_extras → render_pump_trade)。"""
    from src.pumpfun import PumpWatcher

    w = PumpWatcher(FakeNotifier())
    bc = BoardFake(holders={"0xmint": _holders(UID_1, total=1)})
    w._board_holders = BoardHoldersLookup(bc, board_cache=bh._BoardCache())
    blk = w._board_holders.lookup([("solana", "0xmint")])[("solana", "0xmint")]
    names = w._name_extras("MEME", None, None, None, None, blk)

    assert names["board_scope"] == (1, 1, 1, True, 150)
    assert names["board_holders"][0][:2] == (1, "unipcs")


def test_pump那个watcher默认不会自己建client(monkeypatch):
    """
    ⚠️⚠️ 它手上只有 PumpClient。BoardHoldersLookup 的 client 是 None,
       真要发请求时才 build_client() —— 名单为空 / 本轮没成交时一个连接都不建。
    """
    from src.pumpfun import PumpWatcher

    w = PumpWatcher(FakeNotifier())
    assert w._board_holders._client is None
    assert w._board_holders._owns_client is True


def test_poller那个复用自己的client(db):
    """⚠️ poller 手上已经有一个 client,绝不再建第二份连接。"""
    client = FakeClient({})
    p = Poller(client, FakeNotifier())
    assert p._board_holders._client is client
    assert p._board_holders._owns_client is False


# ============================================================
# ⚠️⚠️ 卖出推送 / 计价币过滤 —— 上一版这两条**零覆盖**
# ============================================================
# 变异跑抓到:`_board_holder_map` 只收 EVENT_BUY(卖出推送悄悄少一块)、
# 去掉 `ev.is_quote` 过滤(每一笔 $SOL / $USDC 成交都白打一个持有人请求),
# 两条改动上一版全量 4587 条**一条都不红**。
def _sell_swap(sid="s1", ca=_CA_AI, sym="AI") -> dict:
    """一条 Robinhood 链上的**卖出** swap(与 test_poller._rh_swap 同形,只换方向)。"""
    from tests.test_poller import _FUTURE_MS

    return {"id": sid, "networkId": "robinhood", "tokenAddress": ca, "symbol": sym,
            "side": "sell", "timestamp": _FUTURE_MS, "amountUsd": 2500.0,
            "txHash": f"tx-{sid}", "holdingUsd": 0.0}


def test_卖出推送同样带上这一块(db):
    """
    ⚠️⚠️ 卖出与买入是**同一族**推送(README 的表里两行都打了 ✅)。
       `_board_holder_map` 只收 EVENT_BUY 时这条当场红。
    """
    _add_ready("uA", "alice")
    client = FakeClient({"uA": UserSnapshot("uA", swaps=[_sell_swap()], transfers=[],
                                            thesis=[], balances=[])})
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1, UID_144, total=15305)})
    _poller(client, tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1
    assert "卖出" in tg.sent[0]
    lines = tg.sent[0].split("\n")
    assert "🏅 盈利榜持有人 ≥2 人" in lines
    assert any(ln.startswith("   #1 「unipcs」") for ln in lines)
    assert bc.holder_calls == [_CA_AI]


def test_计价币的成交一个持有人请求都不发(db):
    """
    ⚠️⚠️ 计价币($SOL / $USDC / 原生代币哨兵)照常推送,但**绝不为它问持有人** ——
       那是每一笔计价币成交白打一个请求,而 🏅 对计价币也没有任何意义。
       去掉 `ev.is_quote` 过滤时这条当场红。
    """
    quote_ca = "0x" + "e" * 40           # 原生代币哨兵,models.is_quote_token 认它
    _add_ready("uA", "alice")
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_rh_swap("q1", ca=quote_ca, sym="ETH")],
        transfers=[], thesis=[], balances=[])})
    tg = FakeNotifier()
    bc = BoardFake(holders={quote_ca: _holders(UID_1)})
    _poller(client, tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 1, "计价币的成交本身照常推送"
    assert bc.holder_calls == [], "但绝不为它问持有人"
    assert bc.board_calls == 0, "一个币都不用问,连榜都不该拉"
    assert "🏅" not in tg.sent[0]


def test_买入与卖出混在一轮里各自都问到了(db):
    """⚠️ 两种方向同轮:两个币各一个请求,榜只拉一次。"""
    _add_ready("uA", "alice")
    other = "0x" + "c" * 40
    client = FakeClient({"uA": UserSnapshot(
        "uA", swaps=[_rh_swap("b1"), _sell_swap("s1", ca=other, sym="ZZZ")],
        transfers=[], thesis=[], balances=[])})
    tg = FakeNotifier()
    bc = BoardFake(holders={_CA_AI: _holders(UID_1), other: _holders(UID_144)})
    _poller(client, tg, _FakeDex(), bc).tick()

    assert len(tg.sent) == 2
    assert sorted(bc.holder_calls) == sorted([_CA_AI, other])
    assert bc.board_calls == 1
    assert all("🏅" in m for m in tg.sent)


# ============================================================
# ⚠️⚠️ pump.fun 那条接线的**端到端**用例(真的跑到 run_once)
# ============================================================
# 上一版那两条 pump 用例一条只驱动 `_name_extras`、一条只查 client 是不是 None,
# **没有一条跑到 run_once**。变异跑抓到:把 `board_blocks` 断掉(传空 dict)、
# 或者干脆不查(`_board_holder_map` 直接 return {}),全量 4587 条一条都不红 ——
# 也就是说这一整条接线可以被悄悄拆掉而 CI 全绿。
@pytest.fixture
def pump_cfg(monkeypatch):
    """pump 那条路的门槛/窗口显式给死,不吃 .env 的默认值。"""
    for k, v in {"FOMO_PUMP_MIN_USD": "100", "FOMO_PUMP_MAX_MINTS": "8",
                 "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200",
                 "FOMO_PUMP_CALLOUT_MAX_AGE_SEC": "7200"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _pump_watcher(board_client, notifier):
    """一个真的 PumpWatcher,只把 🏅 那一块的 FOMO client 换成假的。"""
    from src import pumpfun as pf
    from tests.test_pumpfun import (
        HEX_SVM,
        MINT_SOL,
        pos_row,
        position_payload,
        seeded_user,
        trade_payload,
    )
    from tests.test_pumpfun import (
        FakeClient as PumpFakeClient,
    )

    seeded_user()
    client = PumpFakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload()})
    w = pf.PumpWatcher(notifier, client)
    w._board_holders = BoardHoldersLookup(board_client, board_cache=bh._BoardCache())
    return w, MINT_SOL, client


def test_pump成交推送跑到run_once才带上这一块(db, pump_cfg):
    """
    ⚠️⚠️ 端到端:`PumpWatcher.run_once()` → 真的发出去的那条消息里必须有这一块。
       把 `_board_holder_map` 拆掉 / 把 board_blocks 断掉,这条当场红。
    """
    from tests.test_pumpfun import FakeNotifier as PumpNotifier

    tg = PumpNotifier()
    bc = BoardFake()
    w, mint, _pc = _pump_watcher(bc, tg)
    bc._holders[mint] = _holders(UID_1, UID_144, total=15305)

    assert w.run_once() == 1
    assert len(tg.sent) == 1
    lines = tg.sent[0].split("\n")
    assert "🏅 盈利榜持有人 ≥2 人" in lines
    assert any(ln.startswith("   #1 「unipcs」") for ln in lines)
    assert any(ln.startswith("   #144 「deliveryydriver」") for ln in lines)
    assert "   ⚠️ 只比对了前 2/15,305 名持有人,榜只到前 150 名 —— 没显示≠没有" in lines
    assert bc.holder_calls == [mint], "每个 mint 一个持有人请求"
    assert bc.board_calls == 1


def test_pump那条路问的是数字链id(db, pump_cfg):
    """⚠️ 传链名服务端直接 400 —— 这条钉住 pump 那一侧也转成了数字。"""
    from tests.test_pumpfun import FakeNotifier as PumpNotifier

    seen: list = []

    class Recording(BoardFake):
        def get_top_holders(self, token_address, network_id, *, auth_invalidate=True,
                            fast_fail):
            seen.append((network_id, auth_invalidate, fast_fail))
            return super().get_top_holders(token_address, network_id,
                                           auth_invalidate=auth_invalidate,
                                           fast_fail=fast_fail)

    tg = PumpNotifier()
    bc = Recording()
    w, mint, _pc = _pump_watcher(bc, tg)
    bc._holders[mint] = _holders(UID_1)

    assert w.run_once() == 1
    assert seen == [(1399811149, False, True)]


def test_pump零命中时整块不出现但成交照推(db, pump_cfg):
    from tests.test_pumpfun import FakeNotifier as PumpNotifier

    tg = PumpNotifier()
    bc = BoardFake()
    w, mint, _pc = _pump_watcher(bc, tg)
    bc._holders[mint] = _holders("not-on-board")

    assert w.run_once() == 1
    assert "🏅" not in tg.sent[0]
    assert "0 人" not in tg.sent[0]


def test_pump这一块整个炸了成交照样推(db, pump_cfg):
    """⚠️⚠️ **绝不能出现「因为查不到盈利榜所以成交没推出去」**。"""
    from tests.test_pumpfun import FakeNotifier as PumpNotifier

    tg = PumpNotifier()
    bc = BoardFake(board_boom=RuntimeError("上游 500"))
    w, mint, _pc = _pump_watcher(bc, tg)

    assert w.run_once() == 1
    assert len(tg.sent) == 1
    assert "🏅" not in tg.sent[0]


def test_pump那条路每轮都重置预算(db, pump_cfg):
    """
    ⚠️⚠️ begin_round 没被调 = 第二轮起额度是空的,这一块从此**永久消失**。
       把额度压到 1,连跑两轮 —— 第二轮必须还有。
    """
    from tests.test_pumpfun import (
        HEX_SVM,
        pos_row,
        position_payload,
    )
    from tests.test_pumpfun import (
        FakeNotifier as PumpNotifier,
    )

    tg = PumpNotifier()
    bc = BoardFake()
    w, mint, pc = _pump_watcher(bc, tg)
    bc._holders[mint] = _holders(UID_1)
    w._board_holders._per_round = 1

    assert w.run_once() == 1
    assert "🏅" in tg.sent[0]

    # 第二轮:持仓再变一次(换一笔成交),额度必须已经重置
    pc.portfolios[HEX_SVM] = position_payload(pos_row(held=9.0))
    pc.trades[mint] = _second_trade()
    w._board_holders._cache.clear()          # 绕开 90 秒内存缓存,逼它真的再问一次
    assert w.run_once() == 1
    assert len(tg.sent) == 2
    assert "🏅" in tg.sent[1]
    assert bc.holder_calls == [mint, mint]


def _second_trade():
    """第二笔成交(换 tx / slot,否则会被去重挡掉)。"""
    from tests.test_pumpfun import trade_payload

    return trade_payload(tx="TX_2", slot="0002")
