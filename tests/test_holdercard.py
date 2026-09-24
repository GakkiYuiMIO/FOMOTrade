"""
/chips 前 10 名的投资组合与 7 天盈亏(src/holdercard.py)。

⚠️ 公式是 FOMO 前端用户卡片的逐条移植,这里按前端的**每一个分支**各钉一条 ——
   抄错一个分支,数字就和 FOMO 界面对不上,而那正是用户拿来对照的东西。
⚠️ 数字一律写死字面量;全部离线。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

import threading
import time

import pytest

from src import holdercard as hc

SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _row(*, addr="TOKEN", chain=1399811149, shifted=0.0, price=None, valuation=None,
         active=None, remaining=0.0, avg=0.0, realized=0.0) -> dict:
    """balances 里的一行,形状照抄实测:balance / userToken / tokenFilterResult / valuation / activeTrade"""
    r = {"balance": {"tokenAddress": addr, "shiftedBalance": shifted},
         "userToken": {"networkId": chain, "tokenAddress": addr,
                       "humanAmountRemaining": remaining, "averageEntryPriceUsd": avg,
                       "currentRealizedPnlUsd": realized},
         "tokenFilterResult": {"priceUSD": price}}
    if valuation is not None:
        r["valuation"] = valuation
    if active is not None:
        r["activeTrade"] = active
    return r


def _resp(*rows, other_equity=None, other_pnl=None, perp=None) -> dict:
    d: dict = {"balances": list(rows)}
    if other_equity is not None:
        d["otherEquity"] = other_equity
    if other_pnl is not None:
        d["otherPnlV2"] = other_pnl
    if perp is not None:
        d["livePerpPnl"] = perp
    return d


# ============================================================
# 投资组合(前端 AccountProvider.mc)
# ============================================================
class Test投资组合:
    def test_数量乘现价再加otherEquity(self):
        assert hc.portfolio_value(_resp(_row(shifted=100, price="2"), other_equity=50)) == 250.0

    def test_Solana上的USDC是现金_按面值计_不乘价格(self):
        assert hc.portfolio_value(_resp(_row(addr=SOL_USDC, shifted=1234.5, price=None))) == 1234.5

    @pytest.mark.parametrize(("chain", "addr"), [
        (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),   # Base USDC
        (56, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),     # BSC USDC
        (143, "0x754704bc059f8c67012fed69bc8a327a5aafb603"),    # Monad USDC
        (1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),      # Ethereum USDC
        (5042, "0x3600000000000000000000000000000000000000"),   # Arc USDC
        (4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),   # Robinhood USDG
    ])
    def test_各链稳定币整行剔除_由otherEquity统一计入(self, chain, addr):
        """⚠️ 不剔就是重复计算。地址大小写不敏感(上游可能给 checksum 形态)"""
        rows = _resp(_row(addr=addr.upper().replace("0X", "0x"), chain=chain, shifted=1000, price="1"))
        assert hc.portfolio_value(rows) == 0.0

    def test_同一个地址在别的链上不剔(self):
        """判据是 (链, 地址),不是地址 —— Base 的 USDC 地址放到 BSC 上就是一个普通币"""
        rows = _resp(_row(addr="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", chain=56,
                          shifted=10, price="1"))
        assert hc.portfolio_value(rows) == 10.0

    def test_valuation标了不计入权益的不计(self):
        v = {"includeInEquity": False, "useLivePrice": True}
        assert hc.portfolio_value(_resp(_row(shifted=100, price="2", valuation=v))) == 0.0

    def test_valuation不用实时价的按0计(self):
        v = {"includeInEquity": True, "useLivePrice": False}
        assert hc.portfolio_value(_resp(_row(shifted=100, price="2", valuation=v))) == 0.0

    def test_valuation两项都开_按实时价计(self):
        v = {"includeInEquity": True, "useLivePrice": True}
        assert hc.portfolio_value(_resp(_row(shifted=100, price="2", valuation=v))) == 200.0

    def test_价格非数或缺失按0计_不让NaN流出来(self):
        rows = _resp(_row(shifted=100, price="nan"), _row(shifted=100, price=None),
                     _row(shifted=3, price="4"))
        assert hc.portfolio_value(rows) == 12.0

    @pytest.mark.parametrize("resp", [None, {}, {"balances": None}, {"balances": "x"}, []])
    def test_形状不对返回None而不是0(self, resp):
        """⚠️ None 让那一段消失;返回 0 就是在说"这人一分钱都没有"(假事实)"""
        assert hc.portfolio_value(resp) is None

    def test_空持仓是真实的0加otherEquity(self):
        assert hc.portfolio_value(_resp(other_equity=7)) == 7.0


# ============================================================
# 累计盈亏(前端 portfolio.Q + otherPnlV2 + livePerpPnl)
# ============================================================
class Test累计盈亏:
    def test_没有活跃单时按userToken算(self):
        # 10 枚 × (现价 3 − 均价 1) + 已实现 5 = 25
        assert hc.live_total_pnl(_resp(_row(price="3", remaining=10, avg=1, realized=5))) == 25.0

    def test_活跃单的成本是买入与转入的加权平均_不是avgEntryPrice(self):
        """
        ⚠️⚠️ 这是最容易抄错的一处(前端 util.Se)。买 10 枚 @1、转入 10 枚 @3 → 平均成本 2;
           20 枚 × (4 − 2) + 已实现 7 = 47。只看 avgEntryPrice 会算成 20 × (4 − 1) + 7 = 67。
        """
        at = {"humanTokenAmount": 20, "avgEntryPrice": 1, "sumSwapOpen": 10,
              "avgTransferInPrice": 3, "sumTransferIn": 10, "realizedPnlUsd": 7}
        assert hc.live_total_pnl(_resp(_row(price="4", active=at))) == 47.0

    def test_活跃单买入转入都是0时成本按0(self):
        at = {"humanTokenAmount": 5, "realizedPnlUsd": 0}
        assert hc.live_total_pnl(_resp(_row(price="2", active=at))) == 10.0

    def test_valuation关掉未实现_只剩已实现(self):
        v = {"includeUnrealizedPnl": False, "includeRealizedPnl": True, "useLivePrice": True}
        assert hc.live_total_pnl(_resp(_row(price="3", remaining=10, avg=1, realized=5,
                                            valuation=v))) == 5.0

    def test_valuation关掉已实现_只剩未实现(self):
        v = {"includeUnrealizedPnl": True, "includeRealizedPnl": False, "useLivePrice": True}
        assert hc.live_total_pnl(_resp(_row(price="3", remaining=10, avg=1, realized=5,
                                            valuation=v))) == 20.0

    def test_valuation不用实时价时现价按0_未实现是负的成本(self):
        """前端就是这么算的(i=0 且仍算"有价格"):10 × (0 − 1) + 5 = -5"""
        v = {"includeUnrealizedPnl": True, "includeRealizedPnl": True, "useLivePrice": False}
        assert hc.live_total_pnl(_resp(_row(price="3", remaining=10, avg=1, realized=5,
                                            valuation=v))) == -5.0

    def test_没有价格时不算未实现_已实现照算(self):
        assert hc.live_total_pnl(_resp(_row(price=None, remaining=10, avg=1, realized=5))) == 5.0

    def test_现金那一行跳过(self):
        assert hc.live_total_pnl(_resp(_row(addr=SOL_USDC, price="1", remaining=1000,
                                            avg=0, realized=99))) == 0.0

    def test_加上otherPnlV2与livePerpPnl(self):
        assert hc.live_total_pnl(_resp(other_pnl=100, perp=-30)) == 70.0

    def test_形状不对返回None(self):
        assert hc.live_total_pnl({"nope": 1}) is None


# ============================================================
# 7 天盈亏
# ============================================================
class Test七天盈亏:
    def test_快照id是7天前向下取整到整点的unix秒(self):
        """2026-09-24 12:34:56 UTC → 7 天前 = 09-17 12:34:56 → 向下取整 = 09-17 12:00:00"""
        assert hc.snapshot_id_7d(1790253296000) == 1789646400

    def test_此刻累计减7天前的累计(self):
        assert hc.pnl_7d(100.0, {"pnl": 30}) == 70.0

    def test_7天前的累计是0是真实值(self):
        assert hc.pnl_7d(100.0, {"pnl": 0}) == 100.0

    @pytest.mark.parametrize("snap", [None, {}, {"pnl": None}, {"pnl": "abc"}, {"pnl": True},
                                      {"pnl": float("nan")}, "x"])
    def test_快照拿不到就是None_绝不把全部历史当成7天(self, snap):
        assert hc.pnl_7d(100.0, snap) is None

    def test_此刻累计拿不到就是None(self):
        assert hc.pnl_7d(None, {"pnl": 30}) is None


# ============================================================
# 查询器:并发 / 时限 / 缓存 / 失败降级
# ============================================================
class _FakeClient:
    def __init__(self, data=None, *, slow=None, fail_bal=(), fail_snap=(), delay=0.0,
                 snap=None, concurrent=True):
        self.data = data or {}
        self.snap = {"pnl": 1.0} if snap is None else snap
        self.supports_concurrency = concurrent
        self.threads: list[int] = []
        self.slow = slow or {}
        self.fail_bal, self.fail_snap = set(fail_bal), set(fail_snap)
        self.delay = delay
        self.calls: list[tuple] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def _enter(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self):
        with self.lock:
            self.active -= 1

    def get_balances_raw(self, uid, *, auth_invalidate=True, fast_fail=False):
        self._enter()
        try:
            with self.lock:
                self.calls.append(("bal", uid, auth_invalidate, fast_fail))
                self.threads.append(threading.get_ident())
            time.sleep(self.slow.get(uid, self.delay))
            if uid in self.fail_bal:
                raise RuntimeError("上游炸了")
            return self.data.get(uid, _resp(_row(shifted=10, price="1", remaining=10, avg=0.5)))
        finally:
            self._leave()

    def get_pnl_snapshot(self, uid, snapshot_id, *, auth_invalidate=True, fast_fail=False):
        with self.lock:
            self.calls.append(("snap", uid, snapshot_id, auth_invalidate, fast_fail))
        if uid in self.fail_snap:
            raise RuntimeError("快照炸了")
        return self.snap


NOW_MS = 1790253296000


def _lookup(client, **kw):
    kw.setdefault("now_ms", lambda: NOW_MS)
    return hc.HolderCardLookup(client, **kw)


class Test查询器:
    def test_两个数都算出来_请求不作废登录态也不重试(self):
        c = _FakeClient()
        got = _lookup(c).lookup(["u1"])
        # 投资组合 10 × 1 = 10;累计 10 × (1 − 0.5) = 5,减 7 天前 1 → 4
        assert got == {"u1": hc.HolderCard(portfolio_usd=10.0, pnl_7d_usd=4.0)}
        assert ("bal", "u1", False, True) in c.calls
        assert ("snap", "u1", 1789646400, False, True) in c.calls

    def test_客户端没有这两个接口就一个都不查(self):
        class Bare:
            pass
        lk = _lookup(Bare())
        assert lk.supported() is False
        assert lk.lookup(["u1"]) == {}

    def test_没有客户端(self):
        assert _lookup(None).lookup(["u1"]) == {}

    def test_balances挂了那个人不出现(self):
        c = _FakeClient(fail_bal={"u2"})
        got = _lookup(c).lookup(["u1", "u2"])
        assert set(got) == {"u1"}

    def test_快照请求失败_投资组合照有_但标成不完整且不缓存(self):
        """
        ⚠️ 请求**失败**与「这人 7 天前还没有记录」是两回事:前者要计进「没取到」、
           也不许缓存(否则 60 秒内一直缺);后者是真实的空,照常缓存。
        """
        c = _FakeClient(fail_snap={"u1"})
        lk = _lookup(c)
        assert lk.lookup(["u1"]) == {"u1": hc.HolderCard(portfolio_usd=10.0, pnl_7d_usd=None,
                                                          complete=False)}
        n = len(c.calls)
        lk.lookup(["u1"])
        assert len(c.calls) > n, "失败的结果进了缓存,60 秒内不会重试"

    def test_快照是空的_是没有记录不是失败_照常缓存(self):
        c = _FakeClient(snap={})
        lk = _lookup(c)
        assert lk.lookup(["u1"]) == {"u1": hc.HolderCard(portfolio_usd=10.0, pnl_7d_usd=None)}
        n = len(c.calls)
        lk.lookup(["u1"])
        assert len(c.calls) == n

    def test_balances挂了就不再发快照请求(self):
        c = _FakeClient(fail_bal={"u1"})
        _lookup(c).lookup(["u1"])
        assert not any(x[0] == "snap" for x in c.calls), c.calls

    def test_响应形状不对的人不出现(self):
        c = _FakeClient({"u1": {"weird": True}})
        assert _lookup(c).lookup(["u1"]) == {}

    def test_超时的人不等_其余照常返回(self):
        c = _FakeClient(slow={"slow": 1.0})
        t0 = time.monotonic()
        got = _lookup(c, budget_sec=0.3).lookup(["a", "slow", "b"])
        assert set(got) == {"a", "b"}
        assert time.monotonic() - t0 < 0.9, "超时之后还在等慢的那个人"

    def test_超时的人跑完会写进缓存_下一次直接用(self):
        c = _FakeClient(slow={"slow": 0.5})
        lk = _lookup(c, budget_sec=0.1)
        assert "slow" not in lk.lookup(["slow"])
        time.sleep(0.8)
        n = len(c.calls)
        assert "slow" in lk.lookup(["slow"])
        assert len(c.calls) == n, "晚到的结果没进缓存,又打了一遍请求"

    def test_TTL内走缓存_过期重取(self):
        now = [1000.0]
        c = _FakeClient()
        lk = _lookup(c, ttl_sec=60, clock=lambda: now[0])
        lk.lookup(["u1"])
        n = len(c.calls)
        now[0] += 59
        lk.lookup(["u1"])
        assert len(c.calls) == n
        now[0] += 2
        lk.lookup(["u1"])
        assert len(c.calls) > n

    def test_同一个人只查一次(self):
        c = _FakeClient()
        _lookup(c).lookup(["u1", "u1", "", None])
        assert [x for x in c.calls if x[0] == "bal"] == [("bal", "u1", False, True)]

    def test_并发不超过上限(self):
        c = _FakeClient(delay=0.05)
        _lookup(c, workers=3).lookup([f"u{i}" for i in range(10)])
        assert 1 <= c.max_active <= 3, c.max_active


class Test与前端对齐的边缘分支:
    def test_activeTrade是空对象时走活跃单分支_贡献0(self):
        """前端 `if(t.activeTrade)`:JS 里 {} 是真。改走 userToken 分支就会多算 10 × 2 + 5 = 25"""
        assert hc.live_total_pnl(_resp(_row(price="3", remaining=10, avg=1, realized=5,
                                            active={}))) == 0.0

    def test_链id是字符串时不剔_与前端严格相等一致(self):
        """前端 `switch(networkId){case 8453:…}`:字符串 "8453" 不命中,这一行照常计入"""
        rows = _resp(_row(addr="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", chain="8453",
                          shifted=100, price="1"))
        assert hc.portfolio_value(rows) == 100.0


class Test串行与常驻线程池:
    def test_不支持并发的客户端_全在调用线程里查_一个线程都不开(self):
        """
        ⚠️⚠️ playwright 按线程私有启动一整套浏览器、线程退出不回收 ——
           每开一个线程就是漏一套 chromium(client.py 里记着那次 swap 卡死的事故)。
        """
        c = _FakeClient(concurrent=False)
        got = _lookup(c).lookup(["a", "b", "c"])
        assert set(got) == {"a", "b", "c"}
        assert set(c.threads) == {threading.get_ident()}, "开了新线程"

    def test_不支持并发时同样受墙钟预算约束(self):
        now = [0.0]

        class Slow(_FakeClient):
            def get_balances_raw(self, uid, **kw):
                now[0] += 10.0                    # 每查一个人,时钟走 10 秒
                return super().get_balances_raw(uid, **kw)
        c = Slow(concurrent=False)
        got = _lookup(c, budget_sec=15, clock=lambda: now[0]).lookup(["a", "b", "c"])
        assert set(got) == {"a", "b"}, "过了预算还在一个一个查"

    def test_线程池常驻_两次查询共用同一批线程(self):
        c = _FakeClient(delay=0.02)
        lk = _lookup(c, workers=2, ttl_sec=0)
        lk.lookup(["a", "b", "c", "d"])
        lk.lookup(["e", "f", "g", "h"])
        assert len(set(c.threads)) <= 2, "每次 /chips 都新开线程(每个新线程都要重新 TLS 握手)"
