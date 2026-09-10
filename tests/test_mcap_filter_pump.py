"""
买入推送的市值区间 —— pump.fun 买入成交这一侧。FOMO 那一侧与配置写法见 tests/test_mcap_filter.py。

⚠️ 断言里的阈值、边界、请求数一律**写死字面量**,绝不从被测模块 import。
⚠️ 全部离线夹具(复用 test_pumpfun 的 FakeClient:它会数 fetch_coin 被调了几次)。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src import pumpfun as pf
from src import store
from src.config import get_settings
from tests.test_pumpfun import (
    HEX_EVM,
    HEX_SVM,
    MINT_BSC,
    MINT_SOL,
    FakeClient,
    FakeNotifier,
    coin_payload,
    ledger_rows,
    pos_row,
    position_payload,
    seeded_user,
    snapshot_keys,
    trade_payload,
)


@pytest.fixture
def db(tmp_path):
    """真实文件库(PumpWatcher 内部走 store.get_conn())。⚠️ 自己开 MonkeyPatch,理由见 test_pumpfun.db"""
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "pump_mcap.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture
def cfg(monkeypatch):
    """门槛/窗口/市值区间全部显式给死,不吃 .env。默认:$50 门槛、区间关闭。"""
    def _apply(**kw):
        env = {"FOMO_PUMP_MIN_USD": "50", "FOMO_PUMP_MAX_MINTS": "8",
               "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200",
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


def _buy_and_sell() -> dict:
    """同一个 mint 上一笔买入(B1)+ 一笔卖出(S1),金额都过 $50"""
    payload = trade_payload(tx="B1", side="buy", usd="1234.5", slot="0001")
    payload[HEX_SVM] += trade_payload(tx="S1", side="sell", usd="1234.5", slot="0002")[HEX_SVM]
    return payload


def _client(trades, coins=None) -> FakeClient:
    return FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                      trades={MINT_SOL: trades}, coins=coins)


class Test只筛买入:
    def test_上限500K_大盘买入不推_同币卖出照推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1 and "卖出" in tg.sent[0], tg.sent
        # 被筛掉的买入当场记台账(每笔只判一次),卖出推成功后记台账
        assert {r["tx"] for r in ledger_rows()} == {"B1", "S1"}
        assert snapshot_keys() != set(), "全部处理干净了,快照必须前移"

    def test_关闭时同样的成交买卖都推(self, db, cfg):
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 2

    def test_恰好等于上限的买入要推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=500000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1

    def test_越过上限一分钱就不推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=500000.01)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_恰好等于下限的买入要推_低一分不推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MIN_MARKET_CAP="50K")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=50000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1
        seeded_user(username="u2", uid="uid-2", svm="BQ4Kzz", evm=None)
        client = FakeClient(portfolios={"BQ4Kzz": position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(addr="BQ4Kzz", tx="B2")},
                            coins={MINT_SOL: coin_payload(mcap=49999.99)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0


class Test无市值与0:
    def test_问不到市值时默认照推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={})          # fetch_coin → None
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1

    def test_无市值不推时问不到市值的买入不推且记台账(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert {r["tx"] for r in ledger_rows()} == {"B1"}

    def test_响应里没有usd_market_cap也算无市值(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: {"symbol": "X"}})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_市值恰好为0低于下限要筛_不许落进无市值照推(self, db, cfg):
        """⚠️ 传的必须是**真正的 0**(不是字符串 "0"),否则测不到真值判断那个坑"""
        cfg(FOMO_BUY_PUSH_MIN_MARKET_CAP="50K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="true")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_市值恰好为0在上限之内要推_不许落进无市值不推(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1


class Test请求数与顺序:
    def test_过不了50刀门槛的成交一个市值请求都不打(self, db, cfg):
        """
        ⚠️⚠️ 判定顺序:先 $50(免费)再市值。反过来就是为一笔本来就推不出去的粉尘成交
           去吃 frontend-api-v3 的限流额度(60 次/分)。
        """
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        client = _client(trade_payload(tx="DUST", usd="10"),
                         coins={MINT_SOL: coin_payload(mcap=100.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.coin_calls == []

    def test_关闭时请求数与改造前完全一样(self, db, cfg):
        """改造前:过了门槛的币问一次市值,过不了门槛的一次都不问。字面量写死。"""
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1
        assert client.portfolio_calls == [HEX_SVM]
        assert len(client.trade_calls) == 1
        assert client.coin_calls == [MINT_SOL]

    def test_关闭时过不了门槛的一次市值都不问(self, db, cfg):
        seeded_user()
        client = _client(trade_payload(tx="DUST", usd="10"))
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.coin_calls == []

    def test_开启时不为判定多打请求(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        pf.PumpWatcher(FakeNotifier(), client).run_once()
        assert client.coin_calls == [MINT_SOL]

    def test_开启时市值走TTL缓存_两轮只问一次(self, db, cfg):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(trade_payload(tx="TX_A"), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 0
        client.portfolios[HEX_SVM] = position_payload(pos_row(held=5.0))
        client.trades[MINT_SOL] = trade_payload(tx="TX_B")
        assert w.run_once() == 0
        assert client.coin_calls == [MINT_SOL], "第二轮必须命中 60 秒缓存,不许绕过 _coin_stats"


class Test台账与汇总:
    def test_被筛掉的买入每笔只判一次_市值后来跌进区间也不补推(self, db, cfg):
        """
        ⚠️⚠️ 不记台账的话,下一次持仓变动时 swap-api 会把这笔成交再给一遍,
           拿**那时的**市值重判 —— 2M 时的买入跌到 300K 后被当成新买入推出去。
        """
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = _client(trade_payload(tx="TX_A"), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

        client.portfolios[HEX_SVM] = position_payload(pos_row(held=5.0))
        client.coins[MINT_SOL] = coin_payload(mcap=300_000.0)
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 0, f"同一笔被重判后补推了:{tg.sent}"

    def test_额度见底时市值根本没问_不许按无市值去筛并记台账(self, db, cfg):
        """
        ⚠️⚠️ 单轮推送额度(20 条)被前一个人吃光时,后一个人的市值**根本没去问**(stats=None)。
           那时若照样判,「无市值不推」会把他区间内的成交当成无市值筛掉、记进台账 ——
           本该下一轮补推的成交就永久丢了。
        构造:两个人共用一个钱包、同一个币上 20 笔买入;第一个人吃满 20 条额度。
        """
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K", FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP="false")
        seeded_user()
        seeded_user(username="影子", uid="uid-2", svm=HEX_SVM, evm=None)
        payload = {HEX_SVM: []}
        for i in range(20):
            payload[HEX_SVM] += trade_payload(tx=f"T{i:02d}", slot=f"{i:04d}")[HEX_SVM]
        client = _client(payload, coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 20, "前提不成立:第一个人没有吃满 20 条额度"
        assert {r["user_id"] for r in ledger_rows()} == {"046999d1-1609-4654-b8ed-06827aa50ae9"}, \
            "额度见底的那个人一笔都不该进台账"
        assert w.run_once() == 20, "下一轮必须把他的 20 笔补推出来"

    def test_每轮只打一条INFO汇总(self, db, cfg, infos):
        cfg(FOMO_BUY_PUSH_MAX_MARKET_CAP="500K")
        seeded_user()
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=2.0),
                                                  pos_row(mint=MINT_BSC, chain=56, held=3.0))},
            trades={MINT_SOL: trade_payload(tx="SOL_B"),
                    MINT_BSC: trade_payload(addr=HEX_EVM, tx="BSC_B")},
            coins={MINT_SOL: coin_payload(mcap=2_000_000.0),
                   MINT_BSC: coin_payload(mcap=9_000_000.0)},
        )
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        logs = [m for m in infos if "按市值区间跳过" in m]
        assert len(logs) == 1, logs
        assert "跳过 2 笔买入成交(其中无市值 0 笔)" in logs[0]
        assert "≤ $500.00K | 无市值照推" in logs[0]

    def test_关闭时不打汇总(self, db, cfg, infos):
        seeded_user()
        client = _client(trade_payload(tx="B1"), coins={MINT_SOL: coin_payload(mcap=2_000_000.0)})
        pf.PumpWatcher(FakeNotifier(), client).run_once()
        assert not any("按市值区间跳过" in m for m in infos)
