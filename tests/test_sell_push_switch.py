"""
卖出推送开关(FOMO_SELL_PUSH_ENABLED):配置 / FOMO 推送路径 / pump.fun 成交 / 与买入筛选叠加 /
启动日志 / /status。

⚠️ 断言里的文案、条数一律**写死字面量**,绝不从被测模块 import。
⚠️ 全部离线、临时库,绝不碰 data/fomo.db。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

import pytest

from src import pumpfun as pf
from src import store
from src.config import FomoSettings, get_settings
from src.poller import Poller
from tests.conftest import CA_TOAD
from tests.test_mcap_filter import _FUTURE_MS, _rows, _run_cli, _tick
from tests.test_mcap_filter_pump import _buy_and_sell, _client
from tests.test_poller import _add_ready, _deposit, _mark_tin, _tick_transfers, _tin_env
from tests.test_pumpfun import (
    HEX_SVM,
    MINT_SOL,
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
    """临时文件库。⚠️ 自己开 MonkeyPatch,理由见 tests/test_mcap_filter.db"""
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "sell_switch.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture
def infos():
    from loguru import logger as _lg

    out: list[str] = []
    hid = _lg.add(lambda m: out.append(m.record["message"]), level="INFO")
    yield out
    _lg.remove(hid)


def _env(monkeypatch, *, sells: str = "true", min_usd: str = "", hi: str = "") -> None:
    """卖出开关 + 两道买入筛选一起钉死(连 pump 的门槛也钉死),再清配置缓存"""
    for k, v in {"FOMO_SELL_PUSH_ENABLED": sells, "FOMO_BUY_PUSH_MIN_USD": min_usd,
                 "FOMO_BUY_PUSH_MIN_MARKET_CAP": "", "FOMO_BUY_PUSH_MAX_MARKET_CAP": hi,
                 "FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP": "true", "FOMO_PUMP_MIN_USD": "50",
                 "FOMO_PUMP_MAX_MINTS": "8", "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def _swap(sid: str, sym: str, *, side: str = "buy", amount: float = 2500.0,
          mcap: float = 300_000) -> dict:
    return {"id": sid, "networkId": "solana", "tokenAddress": CA_TOAD, "symbol": sym,
            "side": side, "timestamp": _FUTURE_MS, "amountUsd": amount, "marketCap": mcap,
            "txHash": f"tx-{sid}", "holdingUsd": 2500.0}


def _pushed(notifier) -> set[str]:
    return {s for s in ("BUYX", "DUMP", "TINY", "WHALE")
            if any(f"${s}" in t for t in notifier.sent)}


def _sell_logs(lines: list[str]) -> list[str]:
    return [m for m in lines if "跳过" in m and "卖出" in m]


# ============================================================
# 配置
# ============================================================
class Test配置:
    def test_默认开着(self):
        assert FomoSettings.model_fields["fomo_sell_push_enabled"].default is True

    @pytest.mark.parametrize("raw, want", [("false", False), ("0", False), ("true", True), ("1", True)])
    def test_常见布尔写法(self, raw, want):
        assert FomoSettings(fomo_sell_push_enabled=raw).fomo_sell_push_enabled is want


# ============================================================
# FOMO 推送路径
# ============================================================
class TestFOMO推送:
    def test_开着时卖出照推_不打汇总(self, db, monkeypatch, infos):
        _env(monkeypatch)
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("b1", "BUYX"), _swap("s1", "DUMP", side="sell")]})
        assert _pushed(notifier) == {"BUYX", "DUMP"}
        assert _sell_logs(infos) == []

    def test_关掉后卖出不推_买入照推_卖出照常入库且当场标已处理(self, db, monkeypatch, infos):
        _env(monkeypatch, sells="false")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("b1", "BUYX"), _swap("s1", "DUMP", side="sell")]})
        assert _pushed(notifier) == {"BUYX"}, notifier.sent
        assert _rows() == {"BUYX": 1, "DUMP": 1}, "关掉的卖出也必须落库并 mark_sent"
        with store.get_conn() as c:
            assert store.load_unsent_recent(c, minutes=10) == []
        assert _sell_logs(infos) == [
            "本轮按开关跳过 1 条卖出推送 | FOMO_SELL_PUSH_ENABLED=false | 已入库,只是不推"]

    def test_关掉后下一轮也不会补发(self, db, monkeypatch):
        _env(monkeypatch, sells="false")
        _add_ready("uA", "alice")
        p, notifier = _tick({"uA": [_swap("s1", "DUMP", side="sell")]})
        assert notifier.sent == []
        _, notifier = _tick({"uA": []}, poller=p)
        assert notifier.sent == []

    def test_补发队列走同一判据_关掉后积压的卖出不补推(self, db, monkeypatch):
        """开着时 TG 挂了 → 用户随后关了卖出并重启"""
        _env(monkeypatch)
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("s1", "DUMP", side="sell")]}, ok=False)
        assert _rows() == {"DUMP": 0}, "前提不成立:库里没有待补发的卖出"

        _env(monkeypatch, sells="false")
        _, notifier = _tick({"uA": []})                  # 新 Poller = 重启后读新配置
        assert notifier.sent == [], f"补发路径绕过了卖出开关:{notifier.sent}"
        assert _rows() == {"DUMP": 1}

    def test_跟单信号拿到完整的新事件_不受卖出开关影响(self, db, monkeypatch):
        got: list[str] = []
        monkeypatch.setattr(Poller, "_check_copytrade",
                            lambda self, conn, new_events, dry_run=False:
                            got.extend(e.token_symbol for e in new_events))
        _env(monkeypatch, sells="false")
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("b1", "BUYX"), _swap("s1", "DUMP", side="sell")]})
        assert sorted(got) == ["BUYX", "DUMP"], f"跟单信号的输入被推送侧的卖出开关动过了:{got}"

    def test_转入推送不受卖出开关影响(self, db, monkeypatch):
        _tin_env(monkeypatch)
        _env(monkeypatch, sells="false")
        _add_ready("uA", "PoorGoat_")
        _mark_tin("PoorGoat_")
        _, notifier = _tick_transfers([_deposit()])
        assert len(notifier.sent) == 1 and notifier.sent[0].startswith("📥"), notifier.sent

    def test_与买入金额和市值筛选叠加_各记各的(self, db, monkeypatch, infos):
        _env(monkeypatch, sells="false", min_usd="100", hi="500K")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [
            _swap("b1", "BUYX"),                                   # 推
            _swap("t1", "TINY", amount=50.0),                      # 金额不够
            _swap("w1", "WHALE", mcap=2_000_000),                  # 市值超
            _swap("s1", "DUMP", side="sell", amount=5.0, mcap=2_000_000),  # 卖出关了
        ]})
        assert _pushed(notifier) == {"BUYX"}, notifier.sent
        assert len(_sell_logs(infos)) == 1 and "跳过 1 条卖出推送" in _sell_logs(infos)[0]
        assert len([m for m in infos if "按买入金额跳过 1 条" in m]) == 1
        assert len([m for m in infos if "按市值区间跳过 1 条买入推送" in m]) == 1


# ============================================================
# pump.fun 成交
# ============================================================
class Testpump成交:
    def test_开着时买卖都推(self, db, monkeypatch):
        _env(monkeypatch)
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 2

    def test_关掉后只推买入_卖出记台账_快照照常前移(self, db, monkeypatch, infos):
        _env(monkeypatch, sells="false")
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1 and "卖出" not in tg.sent[0], tg.sent
        assert {r["tx"] for r in ledger_rows()} == {"B1", "S1"}, "关掉的卖出必须当场记台账,每笔只判一次"
        assert snapshot_keys() != set(), "全部处理干净了,快照必须前移"
        assert [m for m in infos if "pump.fun 本轮按开关跳过" in m] == [
            "pump.fun 本轮按开关跳过 1 笔卖出成交 | FOMO_SELL_PUSH_ENABLED=false | 已记台账不再重判"]

    def test_关掉后只剩卖出的币一次市值都不问(self, db, monkeypatch):
        _env(monkeypatch, sells="false")
        seeded_user()
        client = _client(trade_payload(tx="S1", side="sell"),
                         coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.coin_calls == [], "判卖出不需要市值,不许为它吃 coins-v3 的限流额度"
        assert {r["tx"] for r in ledger_rows()} == {"S1"}

    def test_汇总按轮计数_不跨轮累加(self, db, monkeypatch, infos):
        _env(monkeypatch, sells="false")
        seeded_user()
        client = _client(trade_payload(tx="S1", side="sell"))
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 0
        client.portfolios[HEX_SVM] = position_payload(pos_row(held=5.0))   # 持仓又变了 → 下一轮重新选中
        client.trades[MINT_SOL] = trade_payload(tx="S2", side="sell")
        assert w.run_once() == 0
        line = "pump.fun 本轮按开关跳过 1 笔卖出成交 | FOMO_SELL_PUSH_ENABLED=false | 已记台账不再重判"
        assert [m for m in infos if "pump.fun 本轮按开关跳过" in m] == [line, line]

    def test_关掉后方向不明的成交照推(self, db, monkeypatch):
        """证明不了它是卖出,就不许按卖出筛掉"""
        _env(monkeypatch, sells="false")
        seeded_user()
        client = _client(trade_payload(tx="U1", side=None),
                         coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1


# ============================================================
# 可观测
# ============================================================
class Test可观测:
    def test_关掉时启动日志说出来_两行买入筛选不再说卖出照推(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_sell_push_enabled=False, fomo_buy_push_min_usd="100",
                 fomo_buy_push_max_market_cap="500K")
        assert any("卖出推送已关闭(FOMO_SELL_PUSH_ENABLED=false)" in m for m in infos), infos
        mcap_line = [m for m in infos if m.startswith("买入推送市值区间")]
        amount_line = [m for m in infos if m.startswith("买入推送金额门槛")]
        assert mcap_line == ["买入推送市值区间 ≤ $500.00K | 无市值照推 | 只筛买入(FOMO + pump.fun),转入/观点照推"]
        assert amount_line == ["买入推送金额门槛 ≥ $100.00 | 只筛 FOMO 买入(pump.fun 另用 FOMO_PUMP_MIN_USD)"]

    def test_开着时启动日志一个字都不多打(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_sell_push_enabled=True, fomo_buy_push_min_usd="100",
                 fomo_buy_push_max_market_cap="500K")
        assert not any("卖出推送已关闭" in m for m in infos), infos
        assert [m for m in infos if m.startswith("买入推送市值区间")] == [
            "买入推送市值区间 ≤ $500.00K | 无市值照推 | 只筛买入(FOMO + pump.fun),卖出/转入/观点照推"]
        assert [m for m in infos if m.startswith("买入推送金额门槛")] == [
            "买入推送金额门槛 ≥ $100.00 | 只筛 FOMO 买入,卖出照推(pump.fun 另用 FOMO_PUMP_MIN_USD)"]

    def test_status关掉时显示一行_且买入筛选行不再说卖出照推(self, monkeypatch, tmp_path):
        from tests.test_bot import _bot

        _env(monkeypatch, sells="false", min_usd="100", hi="500K")
        b, _ = _bot(monkeypatch, tmp_path)
        status = b._cmd_status()
        assert "🔕 卖出推送已关闭(FOMO + pump.fun)" in status
        assert "💎 买入推送市值 ≤ $500.00K | 无市值照推\n" in status + "\n"
        assert "💰 买入推送金额 ≥ $100.00" in status
        assert "卖出照推" not in status

    def test_status开着时不显示开关行(self, monkeypatch, tmp_path):
        from tests.test_bot import _bot

        _env(monkeypatch, min_usd="100")
        b, _ = _bot(monkeypatch, tmp_path)
        status = b._cmd_status()
        assert "卖出推送已关闭" not in status
        assert "💰 买入推送金额 ≥ $100.00(卖出照推)" in status
