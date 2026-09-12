"""
pump.fun「持仓涨了、却查不到 pump.fun 成交」的补推(🟦 持仓变动)。

触发场景(2026-09-12 实测):0xSun 经 Relay 路由合约收进 7,601,172.86 枚 OPENAITOKEN,
portfolio 看得见、swap-api 两个钱包都查不到成交,原来的逻辑静默吃掉了这次变动。

⚠️ 断言里的门槛、文案、请求数一律**写死字面量**,绝不从被测模块 import。
⚠️ 全部离线夹具(复用 test_pumpfun 的 FakeClient),临时库,绝不碰 data/fomo.db。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

import time

import pytest

from src import pumpfun as pf
from src import store
from src.config import FomoSettings, get_settings
from src.formatter import render_pump_untracked
from tests.test_pumpfun import (
    HEX_EVM,
    HEX_SVM,
    HEX_UID,
    MINT_BSC,
    MINT_SOL,
    FakeClient,
    FakeNotifier,
    add_user,
    coin_payload,
    position_payload,
    seeded_user,
    trade_payload,
)

SOL_CHAIN = "1399811149"
# 持仓行 updatedAt 距今 10 分钟:已过一轮收录宽限(60s),仍在 2h 新鲜窗口内
SETTLED = 600.0
# 另一个 Solana mint(字典序排在 MINT_SOL 之后)
MINT_SOL2 = "H" + MINT_SOL[1:]
# 与 HEX_SVM 只差一个字符的另一个合法 base58 地址(名单外)
OTHER_SVM = "3" + HEX_SVM[1:]
MINT_RH = "0xe06f748b55563a94778b792f257e1fb9a5191e18"


@pytest.fixture
def db(tmp_path):
    """真实文件库(PumpWatcher 内部走 store.get_conn())。⚠️ 自己开 MonkeyPatch,理由见 test_pumpfun.db"""
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "pump_untracked.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture
def cfg(monkeypatch):
    """门槛/窗口/间隔/开关/市值区间全部显式给死,不吃 .env。"""
    def _apply(**kw):
        env = {"FOMO_PUMP_MIN_USD": "50", "FOMO_PUMP_MAX_MINTS": "8",
               "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200", "FOMO_PUMP_INTERVAL_SEC": "60",
               "FOMO_PUMP_UNTRACKED_PUSH_ENABLED": "true", "FOMO_SELL_PUSH_ENABLED": "true",
               "FOMO_BUY_PUSH_MIN_MARKET_CAP": "", "FOMO_BUY_PUSH_MAX_MARKET_CAP": "",
               "FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP": "true"}
        env.update({k: str(v) for k, v in kw.items()})
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    _apply()
    yield _apply
    get_settings.cache_clear()


@pytest.fixture
def infos():
    from loguru import logger as _lg

    out: list[str] = []
    hid = _lg.add(lambda m: out.append(m.record["message"]), level="INFO")
    yield out
    _lg.remove(hid)


def _iso_ago(sec: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - sec))


def _row(held, *, value=None, updated_age=SETTLED, mint=MINT_SOL, chain=1399811149,
         symbol="PUNCHMA", pnl=0.0, exited=None) -> dict:
    """portfolio 的一行。value=None → valueUsd 键不出现;updated_age=None → updatedAt 为 null。"""
    r = {"coinMint": mint, "chainId": chain, "amountHeld": held, "realizedPnlUsd": pnl,
         "updatedAt": None if updated_age is None else _iso_ago(updated_age),
         "isExited": (held == 0) if exited is None else exited,
         "coin": {"symbol": symbol, "name": symbol}}
    if value is not None:
        r["valueUsd"] = value
    return r


def _snap(amount, *, updated_age=3600.0, updated=None, mint=MINT_SOL, chain=SOL_CHAIN) -> None:
    """快照里写一行「上一轮观测到他持有 amount,那一行的 updatedAt 是 updated_age 秒前」"""
    upd = updated if updated is not None else _iso_ago(updated_age)
    with store.get_conn() as conn, store.tx(conn):
        store.upsert_pump_positions(conn, HEX_UID, [(chain, mint, amount, 0.0, upd)])


def _snap_amount(mint=MINT_SOL, chain=SOL_CHAIN):
    with store.get_conn() as conn:
        r = store.pump_position(conn, HEX_UID, chain, mint)
    return None if r is None else r["amount_held"]


def _ledger(tx: str, age_sec: float, mint=MINT_SOL) -> None:
    """已推台账里记一笔(= 之前某一轮已经推过这笔成交)"""
    with store.get_conn() as conn, store.tx(conn):
        store.record_pump_pushed(conn, [(HEX_UID, mint, tx, "0001", pf._iso_utc(time.time() - age_sec))])


def _client(*rows, trades=None, mcap=300_000.0) -> FakeClient:
    """trades 缺省 = 这个人在 MINT_SOL 上一笔成交都没有(请求成功、空数组);mcap=None → 市值拿不到"""
    coins = {} if mcap is None else {r["coinMint"]: coin_payload(mcap=mcap) for r in rows}
    return FakeClient(portfolios={HEX_SVM: position_payload(*rows)},
                      trades={MINT_SOL: {HEX_SVM: []}} if trades is None else trades,
                      coins=coins)


def _untracked(tg) -> list[str]:
    return [t for t in tg.sent if t.startswith("🟦")]


# ============================================================
# 该推的
# ============================================================
class Test该推:
    def test_持仓增加且查不到成交_补推一条_快照前移(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0))              # 单价 $1,增加 1000 枚 ≈ $1000
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1 and len(_untracked(tg)) == 1, tg.sent
        msg = tg.sent[0]
        assert "持仓变动" in msg and "hexiecs" in msg and "$PUNCHMA" in msg
        assert "➕ 增加 1,000 枚 ≈ $1,000.00" in msg, msg
        assert "🔍 pump.fun 查不到对应成交(站外买入、转入都会这样)" in msg
        # ⚠️ 说明行里「站外买入」是列举**原因**,不是断言;除掉它之后一个「买入」都不许有
        claim = msg.replace("🔍 pump.fun 查不到对应成交(站外买入、转入都会这样)", "")
        assert "买入" not in claim and "🟩" not in msg, "没有成交记录,绝不能说成买入"
        assert "🧾" not in msg, "没有签名就不许出现签名行"
        assert msg.rstrip().endswith(f"<code>{MINT_SOL}</code>"), "CA 必须独占最后一行"
        assert _snap_amount() == 1001.0, "推成功之后快照必须前移,否则每轮重推"
        assert client.coin_calls == [MINT_SOL], "要推才问市值,且只问一次"

    def test_快照里没有这一行_说首次看到持有而不是增加(self, db, cfg):
        seeded_user()
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(5000.0, value=100.0))).run_once() == 1
        assert "➕ 首次看到持有 5,000 枚 ≈ $100.00" in tg.sent[0], tg.sent
        assert "增加" not in tg.sent[0]

    def test_估值恰好等于门槛要推(self, db, cfg):
        seeded_user()
        _snap(100.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(150.0, value=150.0))).run_once() == 1   # +50 枚 × $1

    def test_上一轮之前已推过的成交_解释不了这次变动_照推(self, db, cfg):
        """
        ⚠️⚠️ 复审反例 1:40 分钟前买了一笔(已推),30 分钟前的快照已经反映了它;
           之后经 Relay 又收进 1000 枚。那笔旧买入还在 2h 新鲜窗口里,拿窗口判就会把这次收币「解释」掉。
        """
        seeded_user()
        _snap(1.0, updated_age=1800)
        _ledger("B0", 2400)
        tg = FakeNotifier()
        old_buy = trade_payload(tx="B0", side="buy", usd="500", age_sec=2400)
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: old_buy})).run_once() == 1
        assert len(_untracked(tg)) == 1, tg.sent

    def test_Robinhood链EVM币也能补推(self, db, cfg):
        """事故本身就发生在这条链上:成交挂在 EVM 地址下"""
        seeded_user()
        tg = FakeNotifier()
        row = _row(7_601_172.86, value=9654.0, mint=MINT_RH, chain=4663, symbol="OPENAITOKEN")
        client = _client(row, trades={MINT_RH: {HEX_SVM: [], HEX_EVM: []}})
        assert pf.PumpWatcher(tg, client).run_once() == 1
        msg = tg.sent[0]
        assert "➕ 首次看到持有 7,601,173 枚 ≈ $9,654.00" in msg, msg
        assert "Robinhood Chain" in msg
        assert (MINT_RH, (HEX_SVM, HEX_EVM)) in client.trade_calls or \
            any(m == MINT_RH and HEX_EVM in a for m, a in client.trade_calls), "问成交必须带上 EVM 钱包"

    def test_同一地址串在两条链上_涨的是第二行也能推(self, db, cfg):
        seeded_user()
        _snap(5.0, mint=MINT_BSC, chain="56")
        _snap(1.0, mint=MINT_BSC, chain="8453")
        tg = FakeNotifier()
        rows = (_row(5.0, value=5.0, mint=MINT_BSC, chain=56, pnl=10.0),      # 只有盈亏变了
                _row(1001.0, value=1001.0, mint=MINT_BSC, chain=8453))        # 数量涨了
        client = _client(*rows, trades={MINT_BSC: {HEX_SVM: [], HEX_EVM: []}})
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "➕ 增加 1,000 枚" in tg.sent[0], tg.sent
        # ⚠️ 链名必须是**涨的那一行**的(Base),不是 rows[0] 那条只变了盈亏的(BNB Chain)
        assert "Base" in tg.sent[0] and "BNB Chain" not in tg.sent[0], tg.sent
        assert _snap_amount(MINT_BSC, "8453") == 1001.0

    def test_同一次变动只推一次(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        w = pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0)))
        assert w.run_once() == 1
        assert w.run_once() == 0
        assert len(tg.sent) == 1

    def test_推送失败_快照不前移_下一轮补上且只补一次(self, db, cfg):
        seeded_user()
        _snap(1.0)
        client = _client(_row(1001.0, value=1001.0))
        assert pf.PumpWatcher(FakeNotifier(ok=False), client).run_once() == 0
        assert _snap_amount() == 1.0, "TG 没收下就前移快照 = 这次变动永久丢失"
        tg = FakeNotifier()
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 1
        assert w.run_once() == 0
        assert len(_untracked(tg)) == 1

    def test_变动不到一轮_先等一轮_过了宽限再推(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0, updated_age=30))
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 0
        assert _snap_amount() == 1.0, "先别判的这一轮快照不许前移,否则下一轮根本不会再问"
        assert client.coin_calls == [], "先别判时不为它问市值"
        client.portfolios[HEX_SVM] = position_payload(_row(1001.0, value=1001.0, updated_age=SETTLED))
        assert w.run_once() == 1
        assert len(_untracked(tg)) == 1

    def test_宽限期内成交收录进来了_只推正常成交不推持仓变动(self, db, cfg):
        """⚠️⚠️ 复审反例 3:portfolio 已经涨了、成交还没收录。先说成 🟦 再前移快照,真正的 🟩 就永远丢了"""
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0, updated_age=30))
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 0
        client.trades[MINT_SOL] = trade_payload(tx="B1", side="buy", usd="1234.5", age_sec=40)
        assert w.run_once() == 1
        assert len(tg.sent) == 1 and tg.sent[0].startswith("🟩"), tg.sent

    def test_推成功当场前移这一行快照(self, db, cfg):
        """调用方末尾那次 upsert 若抛异常,没有这一步下一轮就会再推一条"""
        seeded_user()
        _snap(1.0)
        w = pf.PumpWatcher(FakeNotifier(), _client(_row(1.0)))
        with store.get_conn() as conn:
            watched = pf._to_watched(store.list_pump_users(conn)[0])
        pos = pf.parse_positions(position_payload(_row(1001.0, value=1001.0)))[0]
        assert w._push_untracked(watched, pos, (1000.0, 1000.0, False), 1) == (True, 1, 1)
        assert _snap_amount() == 1001.0

    def test_额度被正常成交用完_持仓变动留到下一轮(self, db, cfg, monkeypatch):
        monkeypatch.setattr(pf, "MAX_PUSH_PER_ROUND", 1)
        seeded_user()
        _snap(1.0)
        _snap(1.0, mint=MINT_SOL2)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0), _row(1001.0, value=1001.0, mint=MINT_SOL2),
                         trades={MINT_SOL: trade_payload(tx="B1", side="buy", usd="1234.5", age_sec=60),
                                 MINT_SOL2: {HEX_SVM: []}})
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 1 and tg.sent[0].startswith("🟩")
        assert _snap_amount(MINT_SOL2) == 1.0, "额度见底没处理的,快照不许前移"
        assert w.run_once() == 1
        assert len(_untracked(tg)) == 1


# ============================================================
# 不该推的(防误报的每一道闸)
# ============================================================
class Test不该推:
    def test_上一轮之后有成交_哪怕金额没过门槛_也不补推(self, db, cfg):
        """⚠️⚠️ 必须拿**原始**成交判:拿筛完 $50 的 fresh 判,每笔小额成交都会多一条 🟦"""
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        dust = trade_payload(tx="DUST", side="buy", usd="10")
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: dust})).run_once() == 0
        assert tg.sent == []
        assert _snap_amount() == 1001.0

    def test_上一轮之后有正常成交_走成交推送_不另推持仓变动(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        buy = trade_payload(tx="B1", side="buy", usd="1234.5")
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: buy})).run_once() == 1
        assert len(tg.sent) == 1 and tg.sent[0].startswith("🟩"), tg.sent

    def test_卖出被开关筛掉时_这笔卖出照样解释了变动(self, db, cfg):
        cfg(FOMO_SELL_PUSH_ENABLED="false")
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        sell = trade_payload(tx="S1", side="sell", usd="1234.5")
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: sell})).run_once() == 0
        assert tg.sent == []

    def test_上一轮之后已推过的成交_照样解释变动(self, db, cfg):
        """
        ⚠️⚠️ 同一轮里 portfolio 拉完之后才成交、当轮就推了的那一笔:下一轮数量才涨上来。
           拿快照写入时刻判,它会被当成解释不了 —— 活跃交易的人每拆一单就多一条假 🟦。
        """
        seeded_user()
        _snap(1.0, updated_age=600)
        _ledger("B1", 300)
        tg = FakeNotifier()
        buy = trade_payload(tx="B1", side="buy", usd="1234.5", age_sec=300)
        client = _client(_row(1001.0, value=1001.0, updated_age=200), trades={MINT_SOL: buy})
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []

    def test_快照停在旧值_之后的真实成交哪怕在窗口外_也不说查不到(self, db, cfg):
        """⚠️⚠️ 复审反例 2:进程停过,3 小时前真买了 4000 枚;现在发观点刷新了 updatedAt"""
        seeded_user()
        _snap(1.0, updated_age=5 * 3600)
        tg = FakeNotifier()
        old_buy = trade_payload(tx="B3H", side="buy", usd="500", age_sec=3 * 3600)
        client = _client(_row(4001.0, value=4001.0), trades={MINT_SOL: old_buy})
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []

    def test_上一轮updatedAt解析不出_有任何成交就不补推(self, db, cfg):
        seeded_user()
        _snap(1.0, updated="not-a-time")
        tg = FakeNotifier()
        dust = trade_payload(tx="DUST", side="buy", usd="10", age_sec=5 * 3600)
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: dust})).run_once() == 0
        assert tg.sent == []

    def test_持仓行updatedAt在新鲜窗口外_不推(self, db, cfg):
        """停机期间攒下的变动:updatedAt 是当时的,不是刚刚"""
        seeded_user()
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(5000.0, value=100.0, updated_age=3 * 3600))).run_once() == 0
        assert tg.sent == []
        assert _snap_amount() == 5000.0, "判完不推也要前移快照,否则每轮重判"

    def test_持仓行updatedAt缺失_不推(self, db, cfg):
        seeded_user()
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(5000.0, value=100.0, updated_age=None))).run_once() == 0
        assert tg.sent == []

    def test_首次看到但有过旧成交_是pump上买过的老仓位滑进来_不推(self, db, cfg):
        seeded_user()
        tg = FakeNotifier()
        old = trade_payload(tx="OLD", side="buy", usd="500", age_sec=3 * 3600)
        assert pf.PumpWatcher(tg, _client(_row(5000.0, value=100.0), trades={MINT_SOL: old})).run_once() == 0
        assert tg.sent == []

    def test_估值低于门槛不推(self, db, cfg):
        seeded_user()
        _snap(100.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(149.99, value=149.99))).run_once() == 0   # +49.99 枚 × $1
        assert tg.sent == []
        assert _snap_amount() == 149.99

    def test_门槛设成0_估值不到一分钱也不推(self, db, cfg):
        cfg(FOMO_PUMP_MIN_USD="0")
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1.000001, value=1.000001))).run_once() == 0
        assert tg.sent == []

    def test_拿不到估值不推_留一行INFO(self, db, cfg, infos):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0))).run_once() == 0
        assert tg.sent == []
        assert any("拿不到估值,不推" in m for m in infos), infos

    def test_持仓减少不推(self, db, cfg):
        seeded_user()
        _snap(1001.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1.0, value=1.0))).run_once() == 0
        assert tg.sent == []

    def test_持仓减少且拿不到估值_不许打持仓增加的INFO(self, db, cfg, infos):
        """⚠️ 估值门槛本来就挡得住负数;数量那道闸真正守的是这行日志不说假话"""
        seeded_user()
        _snap(1001.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1.0))).run_once() == 0
        assert not any("拿不到估值" in m for m in infos), infos

    def test_快照里上一轮数量是None_判不了不推(self, db, cfg):
        seeded_user()
        _snap(None)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0))).run_once() == 0
        assert tg.sent == []

    def test_isExited为真_哪怕数量大于0_不推(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0, exited=True))).run_once() == 0
        assert _untracked(tg) == []

    def test_成交请求失败_绝不读成查不到成交_快照不动(self, db, cfg):
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0), trades={MINT_SOL: None})
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []
        assert _snap_amount() == 1.0

    @pytest.mark.parametrize("how", ["userAddress 与分组键不符", "名单外的地址键"])
    def test_有成交在归属阶段被丢弃_不补推(self, db, cfg, how):
        seeded_user()
        _snap(1.0)
        if how == "userAddress 与分组键不符":
            payload = trade_payload(tx="X1", side="buy", usd="1234.5")
            payload[HEX_SVM][0]["userAddress"] = OTHER_SVM
        else:
            payload = trade_payload(addr=OTHER_SVM, tx="X1", side="buy", usd="1234.5")
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), trades={MINT_SOL: payload})).run_once() == 0
        assert tg.sent == []

    def test_市值不在买入推送区间_不推_留INFO_快照前移(self, db, cfg, infos):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), mcap=2_000_000.0)).run_once() == 0
        assert tg.sent == []
        assert _snap_amount() == 1001.0
        assert any("市值不在买入推送区间" in m for m in infos), infos

    def test_拿不到市值且无市值不推_不推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(1001.0, value=1001.0), mcap=None)).run_once() == 0
        assert tg.sent == []

    def test_开关关掉_不推_也不为它多问市值(self, db, cfg):
        cfg(FOMO_PUMP_UNTRACKED_PUSH_ENABLED="false")
        seeded_user()
        _snap(1.0)
        tg = FakeNotifier()
        client = _client(_row(1001.0, value=1001.0))
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []
        assert client.coin_calls == []
        assert _snap_amount() == 1001.0

    def test_播种轮一条都不推(self, db, cfg):
        add_user()                                            # 没播过种
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, _client(_row(5000.0, value=100.0))).run_once() == 0
        assert tg.sent == []

    def test_额度见底_不推_返回未处理干净(self, db, cfg):
        seeded_user()
        w = pf.PumpWatcher(FakeNotifier(), _client(_row(1.0)))
        with store.get_conn() as conn:
            watched = pf._to_watched(store.list_pump_users(conn)[0])
        pos = pf.parse_positions(position_payload(_row(1001.0, value=1001.0)))[0]
        assert w._push_untracked(watched, pos, (1000.0, 1000.0, False), 0) == (False, 0, 0)


# ============================================================
# 渲染
# ============================================================
class Test渲染:
    def test_陌生人可控的用户名与符号过门禁(self):
        out = render_pump_untracked(username="t.me/scamgroup", token_symbol="t.me/pumpgrp",
                                    coin_mint=MINT_SOL, added_amount=10.0, amount_usd=60.0)
        assert "t.me" not in out, out
        assert out.startswith("🟦 <b>pump.fun</b> · 持仓变动")

    def test_数量拿不到时那一行整行消失_说明行照旧(self):
        out = render_pump_untracked(username="bob", token_symbol="MEME", coin_mint=MINT_SOL)
        assert "➕" not in out
        assert "🔍 pump.fun 查不到对应成交" in out

    def test_开关默认开(self):
        assert FomoSettings.model_fields["fomo_pump_untracked_push_enabled"].default is True
