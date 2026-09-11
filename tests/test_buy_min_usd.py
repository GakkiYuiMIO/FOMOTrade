"""
买入推送的单笔金额门槛(FOMO_BUY_PUSH_MIN_USD):配置写法 / FOMO 推送路径 / 与市值区间叠加 /
不影响 pump.fun / 启动日志 / /status。

⚠️ 断言里的门槛、边界一律**写死字面量**,绝不从 src.config import 常量来断言自己。
⚠️ 全部离线、临时库,绝不碰 data/fomo.db。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src import store
from src.config import FomoSettings, get_settings
from tests.conftest import CA_TOAD
from tests.test_mcap_filter import _FUTURE_MS, _rows, _run_cli, _tick
from tests.test_poller import _add_ready


@pytest.fixture
def db(tmp_path):
    """临时文件库。⚠️ 自己开 MonkeyPatch,理由见 tests/test_mcap_filter.db"""
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "buy_min.db")
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


def _env(monkeypatch, *, min_usd: str = "", hi: str = "") -> None:
    """金额门槛 + 市值上限一起钉死再清配置缓存"""
    monkeypatch.setenv("FOMO_BUY_PUSH_MIN_USD", min_usd)
    monkeypatch.setenv("FOMO_BUY_PUSH_MIN_MARKET_CAP", "")
    monkeypatch.setenv("FOMO_BUY_PUSH_MAX_MARKET_CAP", hi)
    monkeypatch.setenv("FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP", "true")
    get_settings.cache_clear()


def _swap(sid: str, sym: str, amount: float | None, *, side: str = "buy",
          mcap: float = 300_000) -> dict:
    """一条 swap。amount=None 时 amountUsd 键**整个不出现**(= 上游没给金额)。"""
    d = {"id": sid, "networkId": "solana", "tokenAddress": CA_TOAD, "symbol": sym,
         "side": side, "timestamp": _FUTURE_MS, "marketCap": mcap,
         "txHash": f"tx-{sid}", "holdingUsd": 2500.0}
    if amount is not None:
        d["amountUsd"] = amount
    return d


def _pushed(notifier) -> set[str]:
    return {s for s in ("TINY", "EDGE", "BIG", "DUMP", "NOAMT", "WHALE")
            if any(f"${s}" in t for t in notifier.sent)}


def _amount_logs(lines: list[str]) -> list[str]:
    return [m for m in lines if "按买入金额跳过" in m]


# ============================================================
# 配置写法
# ============================================================
class Test写法:
    @pytest.mark.parametrize("raw, want", [
        ("100", 100.0), ("1K", 1000.0), ("1,000", 1000.0), ("$100", 100.0), (" 100 ", 100.0),
        ("0", 0.0), (100, 100.0),
    ])
    def test_常见写法都认(self, raw, want):
        assert FomoSettings(fomo_buy_push_min_usd=raw).fomo_buy_push_min_usd == want

    def test_空串与默认都是不设(self):
        assert FomoSettings(fomo_buy_push_min_usd="").fomo_buy_push_min_usd is None
        assert FomoSettings.model_fields["fomo_buy_push_min_usd"].default is None

    @pytest.mark.parametrize("bad", ["abc", "-5", "100刀", "nan"])
    def test_写坏的启动报错_不许静默回落成不限(self, bad):
        with pytest.raises(ValidationError):
            FomoSettings(fomo_buy_push_min_usd=bad)


# ============================================================
# FOMO 推送路径
# ============================================================
class TestFOMO推送:
    def test_关闭时小额买入照推_且不打汇总日志(self, db, monkeypatch, infos):
        _env(monkeypatch)
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("t1", "TINY", 5.0)]})
        assert _pushed(notifier) == {"TINY"}
        assert _amount_logs(infos) == []

    def test_门槛100_低于的买入不推_等于的推_卖出小额照推(self, db, monkeypatch, infos):
        _env(monkeypatch, min_usd="100")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [
            _swap("t1", "TINY", 99.99),                 # 差一分 → 筛
            _swap("t2", "EDGE", 100.0),                 # 恰好等于 → 推
            _swap("t3", "BIG", 2500.0),                 # 推
            _swap("t4", "DUMP", 5.0, side="sell"),      # 卖出不管金额 → 推
        ]})
        assert _pushed(notifier) == {"EDGE", "BIG", "DUMP"}, notifier.sent
        assert _rows() == {"TINY": 1, "EDGE": 1, "BIG": 1, "DUMP": 1}, \
            "被筛的也必须落库且当场 mark_sent"
        assert _amount_logs(infos) == [
            "本轮按买入金额跳过 1 条买入推送 | 门槛 ≥ $100.00 | 已入库、照常计入共识,只是不推"]

    def test_拿不到金额的买入照推(self, db, monkeypatch):
        _env(monkeypatch, min_usd="100")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("n1", "NOAMT", None)]})
        assert _pushed(notifier) == {"NOAMT"}, "缺金额证明不了它不够门槛"

    def test_被筛掉的下一轮不会被补发(self, db, monkeypatch):
        _env(monkeypatch, min_usd="100")
        _add_ready("uA", "alice")
        p, notifier = _tick({"uA": [_swap("t1", "TINY", 50.0)]})
        assert notifier.sent == []
        with store.get_conn() as c:
            assert store.load_unsent_recent(c, minutes=10) == []
        _, notifier = _tick({"uA": []}, poller=p)
        assert notifier.sent == []

    def test_补发队列按落库金额判_配置收紧后积压的小额买入不补推(self, db, monkeypatch):
        """
        没开门槛时 TG 挂了 → 用户设了 100 并重启。
        ⚠️ 这条同时钉住「补发行的金额是从库里还原的」:还原成 None 的话会走「缺金额照推」而漏筛。
        """
        _env(monkeypatch)
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("t1", "TINY", 50.0)]}, ok=False)
        assert _rows() == {"TINY": 0}, "前提不成立:库里没有待补发的买入"

        _env(monkeypatch, min_usd="100")
        _, notifier = _tick({"uA": []})                  # 新 Poller = 重启后读新配置
        assert notifier.sent == [], f"补发路径绕过了金额门槛:{notifier.sent}"
        assert _rows() == {"TINY": 1}

    def test_补发队列里够门槛的照常补推(self, db, monkeypatch):
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("b1", "BIG", 2500.0)]}, ok=False)
        _env(monkeypatch, min_usd="100")
        _, notifier = _tick({"uA": []})
        assert len(notifier.sent) == 1 and "$BIG" in notifier.sent[0], notifier.sent

    def test_被金额筛掉的买入仍计入别的推送里的名单内N人买过(self, db, monkeypatch):
        _env(monkeypatch, min_usd="100")
        _add_ready("uA", "alice")
        _add_ready("uB", "bob")
        _, notifier = _tick({
            "uA": [_swap("a1", "TINY", 20.0)],       # 被筛
            "uB": [_swap("b1", "BIG", 2500.0)],      # 同一个币,推
        })
        assert len(notifier.sent) == 1 and "$BIG" in notifier.sent[0]
        assert "2/2" in notifier.sent[0], f"被筛掉的买入没计入共识:\n{notifier.sent[0]}"

    def test_与市值区间叠加_两道各筛各的_日志各记各的(self, db, monkeypatch, infos):
        _env(monkeypatch, min_usd="100", hi="500K")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [
            _swap("t1", "TINY", 50.0),                         # 金额不够
            _swap("w1", "WHALE", 2500.0, mcap=2_000_000),      # 市值超
            _swap("e1", "EDGE", 100.0, mcap=500_000),          # 两道都恰好在边界 → 推
            _swap("d1", "DUMP", 5.0, side="sell", mcap=2_000_000),
        ]})
        assert _pushed(notifier) == {"EDGE", "DUMP"}, notifier.sent
        assert len(_amount_logs(infos)) == 1 and "跳过 1 条" in _amount_logs(infos)[0]
        mcap_logs = [m for m in infos if "按市值区间跳过" in m]
        assert len(mcap_logs) == 1 and "跳过 1 条买入推送(其中无市值 0 条)" in mcap_logs[0], mcap_logs

    def test_两道都不过的只归到金额_不重复计数(self, db, monkeypatch, infos):
        _env(monkeypatch, min_usd="100", hi="500K")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("t1", "TINY", 50.0, mcap=2_000_000)]})
        assert notifier.sent == []
        assert len(_amount_logs(infos)) == 1
        assert [m for m in infos if "按市值区间跳过" in m] == []


# ============================================================
# pump.fun 不受影响
# ============================================================
class Testpump不受影响:
    def test_FOMO金额门槛设得再高_pump买入照推(self, db, monkeypatch):
        """pump.fun 有自己的 FOMO_PUMP_MIN_USD,这一项绝不许串过去"""
        from src import pumpfun as pf
        from tests.test_mcap_filter_pump import _buy_and_sell, _client
        from tests.test_pumpfun import MINT_SOL, FakeNotifier, coin_payload, seeded_user

        for k, v in {"FOMO_PUMP_MIN_USD": "50", "FOMO_PUMP_MAX_MINTS": "8",
                     "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200"}.items():
            monkeypatch.setenv(k, v)
        _env(monkeypatch, min_usd="1M")
        seeded_user()
        client = _client(_buy_and_sell(), coins={MINT_SOL: coin_payload(mcap=300_000.0)})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 2, tg.sent
        assert any("买入" in t for t in tg.sent), tg.sent


# ============================================================
# 可观测
# ============================================================
class Test可观测:
    def test_设了时启动日志印出解析后的门槛(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_buy_push_min_usd="1K")
        assert any("买入推送金额门槛 ≥ $1.00K" in m for m in infos), infos

    def test_不设时启动日志一个字都不打(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_buy_push_min_usd=None)
        assert not any("买入推送金额门槛" in m for m in infos), infos

    def test_status设了时显示门槛_不设时不显示(self, monkeypatch, tmp_path):
        from tests.test_bot import _bot

        _env(monkeypatch, min_usd="100")
        b, _ = _bot(monkeypatch, tmp_path)
        assert "💰 买入推送金额 ≥ $100.00(卖出照推)" in b._cmd_status()

        _env(monkeypatch)
        b, _ = _bot(monkeypatch, tmp_path)
        assert "买入推送金额" not in b._cmd_status()
