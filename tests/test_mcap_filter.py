"""
买入推送的市值区间(FOMO_BUY_PUSH_*):配置写法 / 判据 / FOMO 推送路径 / 启动日志 / /status。
pump.fun 那一侧见 tests/test_mcap_filter_pump.py。

⚠️ 断言里的阈值、写法、边界一律**写死字面量**,绝不从 src.config import 常量来断言自己 ——
   那种断言等价于 `x == x`,把常量改坏了它照样绿(本项目踩过)。
⚠️ 全部离线、临时库,绝不碰 data/fomo.db。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src import store
from src.client import UserSnapshot
from src.config import FomoSettings, MarketCapRange, get_settings
from src.models import EVENT_BUY, FomoEvent, dump_raw, now_iso
from src.poller import Poller
from tests.conftest import CA_TOAD
from tests.test_poller import (
    FakeClient,
    FakeNotifier,
    _add_ready,
    _deposit,
    _mark_tin,
    _tick_transfers,
    _tin_env,
)

# 事件必须晚于 /add 写入的游标才会被推送 —— 与 test_poller 同一个锚点写法
_FUTURE_MS = int((datetime.now(UTC) + timedelta(seconds=60)).timestamp() * 1000)


@pytest.fixture
def db(tmp_path):
    """
    临时文件库。⚠️ 自己开一个 MonkeyPatch,不复用函数级 monkeypatch ——
    用例里任何一句 monkeypatch.undo() 都会把 DB_PATH 还原成生产库(见 test_poller.db)。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "mcap.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture
def infos():
    """收集 loguru 的 INFO 及以上(只要格式化后的消息正文)"""
    from loguru import logger as _lg

    out: list[str] = []
    hid = _lg.add(lambda m: out.append(m.record["message"]), level="INFO")
    yield out
    _lg.remove(hid)


def _mcap_env(monkeypatch, *, lo: str = "", hi: str = "", unknown: str = "true") -> None:
    """三项一起钉死再清配置缓存 —— 之后构造的 Poller / CommandBot 才读得到"""
    monkeypatch.setenv("FOMO_BUY_PUSH_MIN_MARKET_CAP", lo)
    monkeypatch.setenv("FOMO_BUY_PUSH_MAX_MARKET_CAP", hi)
    monkeypatch.setenv("FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP", unknown)
    get_settings.cache_clear()


def _swap(sid: str, sym: str, *, side: str = "buy", mcap: float | None) -> dict:
    """一条 swap。mcap=None 时**整个键不出现**(= 上游没给市值)。"""
    d = {"id": sid, "networkId": "solana", "tokenAddress": CA_TOAD, "symbol": sym,
         "side": side, "timestamp": _FUTURE_MS, "amountUsd": 2500.0,
         "txHash": f"tx-{sid}", "holdingUsd": 2500.0}
    if mcap is not None:
        d["marketCap"] = mcap
    return d


def _tick(snaps: dict[str, list[dict]], *, ok: bool = True, poller: Poller | None = None):
    """跑一轮完整 tick(生产路径)。snaps: user_id → 这一轮的 swaps。返回 (poller, notifier)。"""
    client = FakeClient({uid: UserSnapshot(uid, swaps=list(sw), transfers=[], thesis=[], balances=[])
                         for uid, sw in snaps.items()})
    notifier = FakeNotifier(ok=ok)
    p = poller or Poller(client, notifier)
    p.client, p.notifier = client, notifier
    p.tick()
    return p, notifier


def _rows() -> dict[str, int]:
    """库里每个币符号 → sent。顺带证明事件都**落了库**。"""
    with store.get_conn() as c:
        return {r["token_symbol"]: r["sent"]
                for r in c.execute("SELECT token_symbol, sent FROM fomo_events")}


def _pushed(notifier) -> set[str]:
    """推出去的消息里出现了哪些测试币符号"""
    return {s for s in ("SMOL", "WHALE", "NOMC", "DUMP", "EDGE", "ZERO")
            if any(f"${s}" in t for t in notifier.sent)}


def _skip_logs(lines: list[str]) -> list[str]:
    return [m for m in lines if "按市值区间跳过" in m]


# 一轮里四种典型事件:小盘买入 / 大盘买入 / 无市值买入 / 大盘卖出
_FOUR = [
    _swap("s1", "SMOL", mcap=300_000),
    _swap("s2", "WHALE", mcap=2_000_000),
    _swap("s3", "NOMC", mcap=None),
    _swap("s4", "DUMP", side="sell", mcap=2_000_000),
]


# ============================================================
# 配置写法
# ============================================================
class Test写法:
    @pytest.mark.parametrize("raw, want", [
        ("500K", 500000.0), ("500k", 500000.0), ("1.5M", 1500000.0), ("2B", 2000000000.0),
        ("500,000", 500000.0), ("$500000", 500000.0), ("50万", 500000.0), ("50w", 500000.0),
        ("1.1K", 1100.0), ("0", 0.0), (" 500K ", 500000.0), (500000, 500000.0),
    ])
    def test_常见写法都认(self, raw, want):
        """⚠️ 1.1K 必须**恰好**是 1100.0:float 相乘会得到 1100.0000000000002,边界差一个 ulp"""
        s = FomoSettings(fomo_buy_push_max_market_cap=raw)
        assert s.fomo_buy_push_max_market_cap == want

    def test_M是百万不是千(self):
        """市值语境里 M 就是 million(推送里印的就是 $19.14M)—— 当成千就是差 1000 倍的静默全筛"""
        assert FomoSettings(fomo_buy_push_min_market_cap="2M").fomo_buy_push_min_market_cap == 2000000.0

    def test_空串等于不设_功能关闭(self):
        s = FomoSettings(fomo_buy_push_min_market_cap="", fomo_buy_push_max_market_cap="")
        assert s.fomo_buy_push_min_market_cap is None
        assert s.fomo_buy_push_max_market_cap is None
        assert s.buy_push_mcap.enabled is False

    def test_默认两个都不设_无市值默认照推(self):
        """conftest 已把 .env 里这三项钉成空/true;这里验的是字段自己的默认值不许悄悄翻转"""
        s = FomoSettings(_env_file=None, fomo_buy_push_min_market_cap=None,
                         fomo_buy_push_max_market_cap=None)
        assert s.buy_push_mcap.enabled is False
        assert FomoSettings.model_fields["fomo_buy_push_unknown_market_cap"].default is True
        assert FomoSettings.model_fields["fomo_buy_push_max_market_cap"].default is None
        assert FomoSettings.model_fields["fomo_buy_push_min_market_cap"].default is None

    @pytest.mark.parametrize("bad", [
        "-500K", "abc", "5MM", "nan", "inf", "1e6", "５00K", "500KK", "1.5.0M", "500 K M",
    ])
    def test_写坏的一律启动报错_不许静默回落成不限(self, bad):
        with pytest.raises(ValidationError):
            FomoSettings(fomo_buy_push_max_market_cap=bad)
        with pytest.raises(ValidationError):
            FomoSettings(fomo_buy_push_min_market_cap=bad)

    def test_负数字面量也拒(self):
        with pytest.raises(ValidationError):
            FomoSettings(fomo_buy_push_min_market_cap=-1)

    def test_下限大于上限启动即报错(self):
        with pytest.raises(ValidationError) as ei:
            FomoSettings(fomo_buy_push_min_market_cap="600K", fomo_buy_push_max_market_cap="500K")
        assert "大于" in str(ei.value)

    def test_下限等于上限是合法区间(self):
        s = FomoSettings(fomo_buy_push_min_market_cap="500K", fomo_buy_push_max_market_cap="500K")
        assert s.buy_push_mcap.allows(500000.0) is True

    def test_区间报错信息不许带出bot_token(self):
        """
        ⚠️⚠️ model_validator 报错时 pydantic 会把整份输入的 repr 印进异常 ——
           启动失败的那条栈就成了凭据泄漏。第一版实测就印出了 fomo_telegram_bot_token。
        """
        with pytest.raises(ValidationError) as ei:
            FomoSettings(fomo_telegram_bot_token="123456:SECRET-TOKEN-xyz",
                         fomo_buy_push_min_market_cap="600K", fomo_buy_push_max_market_cap="500K")
        text = str(ei.value)
        assert "SECRET" not in text
        assert "fomo_telegram_bot_token" not in text


# ============================================================
# 判据(两个平台共用的那一个函数)
# ============================================================
class Test判据:
    def test_上限含边界(self):
        r = MarketCapRange(min_usd=None, max_usd=500000.0, push_unknown=True)
        assert r.allows(500000.0) is True
        assert r.allows(499999.99) is True
        assert r.allows(500000.01) is False

    def test_下限含边界(self):
        r = MarketCapRange(min_usd=50000.0, max_usd=None, push_unknown=True)
        assert r.allows(50000.0) is True
        assert r.allows(49999.99) is False

    def test_0是真实值_不是缺失(self):
        """
        ⚠️⚠️ 两个方向都测:只测一半的话把 `is None` 改成真值判断仍然全绿。
          - 下限 50K + 无市值照推:0 < 50K 必须筛掉(当成缺失就会照推)
          - 上限 500K + 无市值不推:0 ≤ 500K 必须推(当成缺失就会被筛)
        """
        assert MarketCapRange(min_usd=50000.0, max_usd=None, push_unknown=True).allows(0.0) is False
        assert MarketCapRange(min_usd=None, max_usd=500000.0, push_unknown=False).allows(0.0) is True

    def test_无市值两种配置(self):
        assert MarketCapRange(min_usd=None, max_usd=500000.0, push_unknown=True).allows(None) is True
        assert MarketCapRange(min_usd=None, max_usd=500000.0, push_unknown=False).allows(None) is False

    def test_关闭时恒放行_包括只设了无市值不推(self):
        r = MarketCapRange(min_usd=None, max_usd=None, push_unknown=False)
        assert r.enabled is False
        assert r.allows(None) is True
        assert r.allows(0.0) is True
        assert r.allows(1e12) is True


# ============================================================
# FOMO 推送路径(完整 tick)
# ============================================================
class TestFOMO推送:
    def test_关闭时_大盘小盘无市值卖出全推_且不打汇总日志(self, db, infos):
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": _FOUR})
        assert _pushed(notifier) == {"SMOL", "WHALE", "NOMC", "DUMP"}
        assert len(notifier.sent) == 4
        assert _rows() == {"SMOL": 1, "WHALE": 1, "NOMC": 1, "DUMP": 1}
        assert _skip_logs(infos) == []

    def test_上限500K_只筛大盘买入_卖出与无市值照推(self, db, monkeypatch, infos):
        _mcap_env(monkeypatch, hi="500K")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": _FOUR})
        assert _pushed(notifier) == {"SMOL", "NOMC", "DUMP"}, \
            f"卖出必须照推、无市值默认照推,只筛 >500K 的买入:{notifier.sent}"
        assert len(notifier.sent) == 3
        # 四条都落了库,被筛掉的那条当场 sent=1
        assert _rows() == {"SMOL": 1, "WHALE": 1, "NOMC": 1, "DUMP": 1}
        logs = _skip_logs(infos)
        assert len(logs) == 1
        assert "跳过 1 条买入推送(其中无市值 0 条)" in logs[0]
        assert "≤ $500.00K | 无市值照推" in logs[0]

    def test_恰好等于上下限的要推_越过一点就筛(self, db, monkeypatch):
        _mcap_env(monkeypatch, lo="50K", hi="500K")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [
            _swap("e1", "EDGE", mcap=500000),
            _swap("e2", "SMOL", mcap=50000),
            _swap("e3", "WHALE", mcap=500001),
            _swap("e4", "ZERO", mcap=49999),
        ]})
        assert _pushed(notifier) == {"EDGE", "SMOL"}

    def test_无市值不推时无市值买入被筛_无市值卖出照推(self, db, monkeypatch):
        _mcap_env(monkeypatch, hi="500K", unknown="false")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [
            _swap("n1", "NOMC", mcap=None),
            _swap("n2", "DUMP", side="sell", mcap=None),
        ]})
        assert _pushed(notifier) == {"DUMP"}
        assert _rows() == {"NOMC": 1, "DUMP": 1}

    def test_只设无市值不推不设区间_等于没开(self, db, monkeypatch, infos):
        _mcap_env(monkeypatch, unknown="false")
        _add_ready("uA", "alice")
        _, notifier = _tick({"uA": [_swap("n1", "NOMC", mcap=None)]})
        assert _pushed(notifier) == {"NOMC"}, "没设上下限 = 功能关闭,无市值的买入必须照推"
        assert _skip_logs(infos) == []

    def test_被筛掉的当场mark_sent_下一轮也不会被补发(self, db, monkeypatch):
        _mcap_env(monkeypatch, hi="500K")
        _add_ready("uA", "alice")
        p, notifier = _tick({"uA": [_swap("w1", "WHALE", mcap=2_000_000)]})
        assert notifier.sent == []
        assert _rows() == {"WHALE": 1}, "不 mark_sent 的话补发队列下一轮就会把它捞出来"
        with store.get_conn() as c:
            assert store.load_unsent_recent(c, minutes=10) == []
        _, notifier = _tick({"uA": []}, poller=p)
        assert notifier.sent == []

    def test_补发队列走同一判据_配置收紧后积压的大盘买入不补推(self, db, monkeypatch):
        """用最自然的方式造出积压行:没开筛选时 TG 挂了 → 用户随后设了 500K 上限并重启。"""
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("w1", "WHALE", mcap=2_000_000)]}, ok=False)
        assert _rows() == {"WHALE": 0}, "前提不成立:库里没有待补发的买入"

        _mcap_env(monkeypatch, hi="500K")
        _, notifier = _tick({"uA": []})                  # 新 Poller = 重启后读新配置
        assert notifier.sent == [], f"补发路径绕过了市值判据:{notifier.sent}"
        assert _rows() == {"WHALE": 1}, "补发侧筛掉的也要就地 mark_sent,否则每轮被捞一次"

    def test_补发时按落库的市值判_而不是还原出来的None(self, db, monkeypatch):
        """
        ⚠️⚠️ _event_from_row 不还原市值(补发消息不带市值行)。拿它的 None 去判的话,
           「无市值不推」时一条区间内、只是 TG 抖了一下的买入会在重试时被永久筛掉。
        """
        _add_ready("uA", "alice")
        _tick({"uA": [_swap("s1", "SMOL", mcap=300_000)]}, ok=False)
        assert _rows() == {"SMOL": 0}, "前提不成立:库里没有待补发的买入"

        _mcap_env(monkeypatch, hi="500K", unknown="false")
        _, notifier = _tick({"uA": []})
        assert _pushed(notifier) == {"SMOL"}, f"区间内的积压买入没被补推:{notifier.sent}"
        assert _rows() == {"SMOL": 1}

    def test_补发行市值恰好为0_是真实值(self, db, monkeypatch):
        """
        ⚠️ 走 _dispatch 的真实补发路径,喂一条**落库市值就是 0.0** 的积压买入。
           两个方向各测一次,把 0 当缺失的实现必然红一边。
        """
        _add_ready("uA", "alice")
        ev = FomoEvent(event_id="BUY:zero", event_type=EVENT_BUY, user_id="uA",
                       event_ts=now_iso(), raw_json=dump_raw({}), handle="alice",
                       network_id="solana", token_address=CA_TOAD, token_symbol="ZERO",
                       amount_usd=2500.0, market_cap=0.0)
        with store.get_conn() as c:
            assert store.insert_event(c, ev)

        _mcap_env(monkeypatch, lo="50K", unknown="true")     # 0 < 50K → 筛;当成缺失才会照推
        _, notifier = _tick({"uA": []})
        assert notifier.sent == [], f"市值 0 被当成了「无市值照推」:{notifier.sent}"

        with store.get_conn() as c:
            c.execute("UPDATE fomo_events SET sent = 0 WHERE event_id = 'BUY:zero'")
        _mcap_env(monkeypatch, hi="500K", unknown="false")   # 0 ≤ 500K → 推;当成缺失才会被筛
        _, notifier = _tick({"uA": []})
        assert _pushed(notifier) == {"ZERO"}, "市值 0 被当成了「无市值不推」"

    def test_被市值筛掉的买入仍计入别的推送里的名单内N人买过(self, db, monkeypatch):
        """
        ⚠️⚠️ 共识计数读的是落库的事件。筛选若做在落库之前(或顺手跳过 upsert_stats),
           名单里真实买过的人就凭空少一个 —— 推出去的「2/2」会变成「1/2」。
        """
        _mcap_env(monkeypatch, hi="500K")
        _add_ready("uA", "alice")
        _add_ready("uB", "bob")
        _, notifier = _tick({
            "uA": [_swap("a1", "WHALE", mcap=2_000_000)],     # 被筛
            "uB": [_swap("b1", "SMOL", mcap=300_000)],        # 同一个币,推
        })
        assert len(notifier.sent) == 1
        assert "$SMOL" in notifier.sent[0]
        assert "2/2" in notifier.sent[0], f"被筛掉的买入没计入共识:\n{notifier.sent[0]}"
        with store.get_conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM user_token_stats WHERE user_id = 'uA'").fetchone()["n"]
        assert n == 1

    def test_跟单信号拿到完整的新事件_不受市值区间影响(self, db, monkeypatch):
        got: list[str] = []
        monkeypatch.setattr(Poller, "_check_copytrade",
                            lambda self, conn, new_events, dry_run=False:
                            got.extend(e.token_symbol for e in new_events))
        _mcap_env(monkeypatch, hi="500K", unknown="false")
        _add_ready("uA", "alice")
        _tick({"uA": _FOUR})
        assert sorted(got) == ["DUMP", "NOMC", "SMOL", "WHALE"], \
            f"跟单信号的输入被推送侧的市值区间动过了:{got}"

    def test_转入推送不受市值区间影响(self, db, monkeypatch):
        """/tin 转入没有市值(报文里没有 marketCap);「无市值不推」绝不能把它筛掉"""
        _tin_env(monkeypatch)
        _mcap_env(monkeypatch, lo="50K", hi="500K", unknown="false")
        _add_ready("uA", "PoorGoat_")
        _mark_tin("PoorGoat_")
        _, notifier = _tick_transfers([_deposit()])
        assert len(notifier.sent) == 1 and notifier.sent[0].startswith("📥"), \
            f"转入被买入的市值区间筛掉了:{notifier.sent}"

    def test_每轮只打一条INFO汇总(self, db, monkeypatch, infos):
        _mcap_env(monkeypatch, hi="500K", unknown="false")
        _add_ready("uA", "alice")
        _tick({"uA": [
            _swap("w1", "WHALE", mcap=2_000_000),
            _swap("w2", "WHALE", mcap=3_000_000),
            _swap("w3", "WHALE", mcap=4_000_000),
            _swap("n1", "NOMC", mcap=None),
        ]})
        logs = _skip_logs(infos)
        assert len(logs) == 1, f"一轮应当只有一条汇总:{logs}"
        assert "跳过 4 条买入推送(其中无市值 1 条)" in logs[0]
        assert "≤ $500.00K | 无市值不推" in logs[0]


# ============================================================
# 可观测:启动日志 / /status
# ============================================================
def _run_cli(monkeypatch, **kw) -> None:
    from src import cli
    from tests.test_cli import _stub_run

    s = FomoSettings(fomo_alpha_enabled=False, fomo_pump_enabled=False,
                     fomo_pump_callout_enabled=False, **kw)
    _stub_run(monkeypatch, s)
    monkeypatch.setattr(cli, "setup_logger", lambda *a, **k: None, raising=False)
    assert cli.cmd_run() == 0


class Test可观测:
    def test_开启时启动日志印出解析后的区间(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_buy_push_max_market_cap="500K")
        assert any("买入推送市值区间 ≤ $500.00K | 无市值照推" in m for m in infos), infos

    def test_两端都设时启动日志印出区间(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_buy_push_min_market_cap="50K",
                 fomo_buy_push_max_market_cap="1.5M", fomo_buy_push_unknown_market_cap=False)
        assert any("买入推送市值区间 $50.00K ~ $1.50M | 无市值不推" in m for m in infos), infos

    def test_关闭时启动日志一个字都不打(self, monkeypatch, infos):
        _run_cli(monkeypatch)
        assert not any("买入推送市值" in m or "MARKET_CAP" in m for m in infos), infos

    def test_只设无市值不推时启动喊一句不生效(self, monkeypatch, infos):
        _run_cli(monkeypatch, fomo_buy_push_unknown_market_cap=False)
        assert any("单独设置不生效" in m for m in infos), infos

    def test_status开启时显示区间_关闭时不显示(self, monkeypatch, tmp_path):
        from tests.test_bot import _bot

        _mcap_env(monkeypatch, lo="50K", hi="500K")
        b, _ = _bot(monkeypatch, tmp_path)
        assert "💎 买入推送市值 $50.00K ~ $500.00K | 无市值照推(卖出照推)" in b._cmd_status()

        _mcap_env(monkeypatch)
        b, _ = _bot(monkeypatch, tmp_path)
        assert "买入推送市值" not in b._cmd_status()
