"""
pump.fun 连通性提醒(src/pumpfun.py PumpHealth / _announce_health)。

事故背景:代理节点连不上 frontend-api-v3.pump.fun(TLS 握手被掐断),pump.fun 的买卖与观点推送
停了几个小时,用户是翻日志才发现的。现在:某个域名连续连不上 10 分钟 → TG 推一条;恢复后再推一条。

⚠️ 全部离线:时钟、会话、通知器都是假的。
"""
# ruff: noqa: N802
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import pumpfun as pf
from src.config import get_settings

FRONT = "https://frontend-api-v3.pump.fun/portfolio/abc?x=1"
SWAP = "https://swap-api.pump.fun/v1/coins/m/trades/batch"
SSL_ERR = ("Failed to perform, curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection "
           "to frontend-api-v3.pump.fun:443 . See https://curl.se/libcurl/errors.html#35")


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    """
    配置给死,不吃 .env:提醒里的补推窗口跟着 FOMO_PUMP_*_MAX_AGE_SEC 走,
    用户自己的 .env 改了它,不该让这套用例跟着漂。要别的值就调 cfg(KEY=值)。
    """
    def _apply(**kw):
        env = {"FOMO_PUMP_MIN_USD": "100", "FOMO_PUMP_MAX_MINTS": "8",
               "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200",
               "FOMO_PUMP_CALLOUT_MAX_AGE_SEC": "7200"}
        env.update({k: str(v) for k, v in kw.items()})
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    _apply()
    yield _apply
    get_settings.cache_clear()


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _health():
    clk = Clock()
    return pf.PumpHealth(clock=clk), clk


def _down(h, clk, url=FRONT, fails=3, span=pf.PUMP_OUTAGE_ALERT_SEC):
    """从现在起连着失败 fails 次,首尾相隔 span 秒(时钟停在最后一次)"""
    step = span / max(1, fails - 1)
    for i in range(fails):
        if i:
            clk.t += step
        h.fail(url, OSError(SSL_ERR))


def _kinds(items):
    return [(host, kind) for host, kind, _ in items]


class FakeNotifier:
    def __init__(self, ok=True, boom=False) -> None:
        self.sent: list[str] = []
        self.ok, self.boom = ok, boom

    def send(self, text, **kw):
        if self.boom:
            raise RuntimeError("TG 挂了")
        self.sent.append(text)
        return self.ok


class Test报错分类:
    @pytest.mark.parametrize(("err", "want"), [
        (SSL_ERR, "SSL 握手被掐断"),
        ("curl: (28) Operation timed out after 5001 milliseconds", "连接超时"),
        ("Connection Timed Out", "连接超时"),
        ("curl: (6) Could not resolve host: frontend-api-v3.pump.fun", "域名解析失败"),
        ("curl: (7) Failed to connect: Connection refused", "连接被拒"),
        ("对端断开", "网络错误"),
    ])
    def test_报错变成一句人话(self, err, want):
        assert pf._classify_net_error(OSError(err)) == want


class Test何时提醒:
    def test_不到10分钟不提醒(self):
        h, clk = _health()
        _down(h, clk, fails=20, span=pf.PUMP_OUTAGE_ALERT_SEC - 1)
        assert h.claim() == []

    def test_满10分钟且失败够3次_提醒一条(self):
        h, clk = _health()
        _down(h, clk)
        assert _kinds(h.claim()) == [("frontend-api-v3.pump.fun", "down")]

    def test_满10分钟但只失败2次_不提醒(self):
        """只试过一两次,说明不了是断网还是刚好赶上一下抖动"""
        h, clk = _health()
        _down(h, clk, fails=2)
        assert h.claim() == []

    def test_中途连上过一次就从头算(self):
        h, clk = _health()
        _down(h, clk, fails=3, span=500)
        h.ok(FRONT)
        clk.t += 100
        _down(h, clk, fails=3, span=pf.PUMP_OUTAGE_ALERT_SEC - 1)
        assert h.claim() == [], "连上过一次,10 分钟要从下一次失败重新算"

    def test_429也算连得上(self):
        """callout/list 天天 429 —— 那是限流不是断网。只要拿到响应就清零(由 _json 调 ok)"""
        h, clk = _health()
        c = pf.PumpClient(proxy="", health=h)
        for _ in range(3):
            c._tl.session = _BoomSession(OSError(SSL_ERR))
            c._json("t", "get", FRONT)
            clk.t += 300
        c._tl.session = _OkSession(status=429)
        assert c._json("t", "get", FRONT) is None
        assert h.claim() == []

    def test_各域名分开记(self):
        """frontend 断了、swap 好好的 —— swap 的成功不能把 frontend 的断网抹掉"""
        h, clk = _health()
        h.fail(FRONT, OSError(SSL_ERR))
        for _ in range(2):
            clk.t += pf.PUMP_OUTAGE_ALERT_SEC / 2
            h.ok(SWAP)
            h.fail(FRONT, OSError(SSL_ERR))
        assert _kinds(h.claim()) == [("frontend-api-v3.pump.fun", "down")]


class Test认领与回报:
    def test_一次中断只提醒一次(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        clk.t += 3600
        h.fail(FRONT, OSError(SSL_ERR))
        assert h.claim() == []

    def test_认领了还没回报_别的线程看不到(self):
        """两个巡检任务跑在不同线程:先认领再发,否则同一条提醒推两遍"""
        h, clk = _health()
        _down(h, clk)
        assert len(h.claim()) == 1
        assert h.claim() == []

    def test_没发出去_下一轮重发(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, False)
        assert _kinds(h.claim()) == [(host, "down")]


class Test恢复提醒:
    def test_提醒过的_恢复后推一条且只推一条(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        clk.t += 1200
        h.ok(FRONT)
        items = h.claim()
        assert _kinds(items) == [(host, "up")]
        h.done(host, "up", True)
        h.ok(FRONT)
        assert h.claim() == []

    def test_恢复提醒认领了还没回报_别的线程看不到(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        h.ok(FRONT)
        assert _kinds(h.claim()) == [(host, "up")]
        assert h.claim() == []

    def test_恢复提醒没发出去_下一轮重发(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        h.ok(FRONT)
        [(_, up, _)] = h.claim()
        h.done(host, up, False)
        assert _kinds(h.claim()) == [(host, "up")]

    def test_没提醒过的_恢复了也不推(self):
        """用户压根不知道断过,突然说恢复只会让人困惑"""
        h, clk = _health()
        _down(h, clk, span=60)
        h.ok(FRONT)
        assert h.claim() == []

    def test_断网提醒没发出去就恢复了_也不推恢复(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, False)
        h.ok(FRONT)
        assert h.claim() == []

    def test_断网提醒正在发时恢复了_发出去就补推恢复(self):
        """⚠️ 认领→发送之间另一个线程连上了:「连不上了」既然发出去了,「恢复了」不能丢"""
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        clk.t += 30
        h.ok(FRONT)
        h.done(host, kind, True)
        assert _kinds(h.claim()) == [(host, "up")]

    def test_断网提醒正在发时恢复了_没发出去就不推恢复(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.ok(FRONT)
        h.done(host, kind, False)
        assert h.claim() == []

    def test_恢复排在下一次断网前面(self):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        h.ok(FRONT)
        clk.t += 10
        _down(h, clk)
        assert _kinds(h.claim()) == [(host, "up"), (host, "down")]


class Test文案:
    def test_断网文案(self):
        h, clk = _health()
        _down(h, clk, fails=4)
        [(_, _, text)] = h.claim()
        assert text.startswith("⚠️ <b>pump.fun 连不上了</b>(已持续 10 分钟)")
        assert "frontend-api-v3.pump.fun · SSL 握手被掐断(连续 4 次)" in text
        assert "买卖推送和观点推送都停了" in text
        assert "换个节点" in text and "不用重启" in text
        # ⚠️ 原始报错不进 TG:里面有 URL、libcurl 的长串
        assert "curl" not in text and "https://" not in text
        assert text.endswith("恢复后会补推最近 2 小时内的成交和观点,更早的不补。")

    def test_swap域名的影响说明不一样(self):
        h, clk = _health()
        _down(h, clk, url=SWAP)
        [(host, _, text)] = h.claim()
        assert host == "swap-api.pump.fun"
        assert "观点推送不受影响" in text

    def _recovered(self, gap):
        h, clk = _health()
        _down(h, clk)
        [(host, kind, _)] = h.claim()
        h.done(host, kind, True)
        clk.t += gap - pf.PUMP_OUTAGE_ALERT_SEC
        h.ok(FRONT)
        [(_, _, text)] = h.claim()
        return text

    @pytest.mark.parametrize(("gap", "minutes", "tail"), [
        (1500, "中断约 25 分钟", "中断期间的成交和观点会陆续补推。"),
        (7199, "中断约 2 小时", "中断期间的成交和观点会陆续补推。"),
        (7200, "中断约 2 小时", "中断时间较长:只补推最近 2 小时内的成交和观点,更早的不补了。"),
        (3 * 3600, "中断约 3 小时", "中断时间较长:只补推最近 2 小时内的成交和观点,更早的不补了。"),
    ])
    def test_恢复文案(self, gap, minutes, tail):
        text = self._recovered(gap)
        assert text.startswith("✅ <b>pump.fun 已恢复连接</b>(frontend-api-v3.pump.fun · ")
        assert minutes in text and text.endswith(tail)

    def test_补推窗口跟着配置走(self, cfg):
        """⚠️ 不写死 2 小时:用户的 .env 改了窗口,提醒里说的补推范围也得跟着对"""
        cfg(FOMO_PUMP_TRADE_MAX_AGE_SEC=3600, FOMO_PUMP_CALLOUT_MAX_AGE_SEC=9000)
        h, clk = _health()
        _down(h, clk)
        [(_, _, text)] = h.claim()
        assert text.endswith("恢复后会补推最近 60 分钟内的成交、2.5 小时内的观点,更早的不补。")
        # 中断时长按**较短**的那个窗口判:超过它,就有一部分不补了
        assert self._recovered(3000).endswith("中断期间的成交和观点会陆续补推。")
        assert self._recovered(3600).endswith("只补推最近 60 分钟内的成交、2.5 小时内的观点,更早的不补了。")

    @pytest.mark.parametrize(("sec", "want"), [
        (10, "1 分钟"), (5400, "90 分钟"), (7140, "119 分钟"), (7200, "2 小时"),
        (9000, "2.5 小时"), (36000, "10 小时"),
    ])
    def test_时长格式(self, sec, want):
        assert pf._fmt_minutes(sec) == want


class _BoomSession:
    def __init__(self, exc):
        self.exc = exc

    def get(self, url, **kw):
        raise self.exc

    post = get

    def close(self):
        pass


class _OkSession:
    def __init__(self, status=200, payload=None):
        self.status, self.payload = status, payload if payload is not None else {}

    def get(self, url, **kw):
        return SimpleNamespace(status_code=self.status, headers={}, json=lambda: self.payload)

    post = get

    def close(self):
        pass


class Test请求层记账:
    def test_网络失败记一笔_连上清零(self):
        h, clk = _health()
        c = pf.PumpClient(proxy="", health=h)
        for _ in range(3):
            # ⚠️ 每次都要重新塞:_json 失败后会 close() 把会话置空,不塞就去建真的 curl 会话
            c._tl.session = _BoomSession(OSError(SSL_ERR))
            assert c._json("t", "get", FRONT) is None
            clk.t += 300
        st = h._hosts["frontend-api-v3.pump.fun"]
        assert st["fails"] == 3 and st["err"] == "SSL 握手被掐断"
        c._tl.session = _OkSession(payload={"a": 1})
        assert c._json("t", "get", FRONT) == {"a": 1}
        assert st["down_since"] is None and st["fails"] == 0

    def test_限速闸没放行_不算连不上(self):
        """一个字节都没发出去,说明不了网络通不通"""
        h, _ = _health()
        gate = SimpleNamespace(acquire=lambda: False)
        c = pf.PumpClient(proxy="", gate=gate, health=h)
        c._tl.session = _BoomSession(OSError(SSL_ERR))
        assert c._json("t", "get", FRONT) is None
        assert h._hosts == {}

    def test_默认用进程级那一份(self):
        """买卖巡检、观点巡检、/chips 的 client 各是一个实例,连通性必须记在同一份上"""
        assert pf.PumpClient(proxy="").health is pf._HEALTH
        assert pf.PumpClient(proxy="").health is pf.PumpClient(proxy="").health


class Test发送:
    def _ready(self):
        h, clk = _health()
        _down(h, clk)
        return h

    def test_发出去了(self):
        h, n = self._ready(), FakeNotifier()
        pf._announce_health(n, h)
        assert len(n.sent) == 1 and "连不上了" in n.sent[0]
        pf._announce_health(n, h)
        assert len(n.sent) == 1

    def test_发送返回False_下一轮重发(self):
        h, n = self._ready(), FakeNotifier(ok=False)
        pf._announce_health(n, h)
        pf._announce_health(n, h)
        assert len(n.sent) == 2

    def test_发送抛异常_不外抛_下一轮重发(self):
        h = self._ready()
        pf._announce_health(FakeNotifier(boom=True), h)
        n = FakeNotifier()
        pf._announce_health(n, h)
        assert len(n.sent) == 1

    def test_没有health就什么都不做(self):
        """测试里的假 client 没这个属性:不能去碰进程级那一份"""
        _down(pf._HEALTH, Clock(), fails=3, span=0)
        pf._HEALTH._clock = lambda: 1e12
        n = FakeNotifier()
        pf._announce_health(n, None)
        assert n.sent == []


class Test巡检接线:
    def _watchers(self, h, n, monkeypatch, boom=False):
        client = SimpleNamespace(health=h, close=lambda: None)

        def check(self):
            if boom:
                raise RuntimeError("巡检炸了")
            return 0
        monkeypatch.setattr(pf.PumpWatcher, "_check", check)
        monkeypatch.setattr(pf.PumpCalloutWatcher, "_check", check)
        return pf.PumpWatcher(n, client=client), pf.PumpCalloutWatcher(n, client=client)

    @pytest.mark.parametrize("boom", [False, True])
    def test_每轮结束发提醒_巡检炸了也发(self, monkeypatch, boom):
        h, clk = _health()
        _down(h, clk)
        n = FakeNotifier()
        trades, callouts = self._watchers(h, n, monkeypatch, boom=boom)
        assert trades.run_once() == 0
        assert len(n.sent) == 1

    def test_两个巡检共用一份_不重复推(self, monkeypatch):
        h, clk = _health()
        n = FakeNotifier()
        trades, callouts = self._watchers(h, n, monkeypatch)
        _down(h, clk)
        callouts.run_once()
        trades.run_once()
        callouts.run_once()
        assert len(n.sent) == 1
        h.ok(FRONT)
        trades.run_once()
        callouts.run_once()
        assert len(n.sent) == 2 and "已恢复" in n.sent[1]

    def test_观点巡检也会发(self, monkeypatch):
        h, clk = _health()
        n = FakeNotifier()
        _, callouts = self._watchers(h, n, monkeypatch)
        _down(h, clk)
        callouts.run_once()
        assert len(n.sent) == 1
