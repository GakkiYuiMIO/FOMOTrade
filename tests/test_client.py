"""
client.py 重试与错误分类的单测。

⚠️ 这个文件盯的是同一类失效:**把"登录态挂了"误报成"某个接口抖了一下"**。
   _fetch_snapshots 只对 AuthError 显式上抛,FomoAPIError 会落进 `except Exception`
   把该项降级为 None —— 于是 tick 照常返回 0、last_tick_at 照常前进、
   /status 显示一切正常,而实际上再也拉不到任何数据。
   一个看起来完全健康的、死掉的监控,是本项目最危险的失效模式。
"""
# ruff: noqa: N802, N812
# 测试函数名刻意用中文(同 tests/ 其余文件);client 简写成 C 是为了让断言一眼看全。
from __future__ import annotations

import pytest

from src import client as C


def _fake_client(responder):
    """一个只实现 _request 的最小 client —— 不碰网络,只驱动 _fetch_ok 的重试分支"""

    class Fake(C._BaseFomoClient):
        def __init__(self):
            self._tokens = type("T", (), {
                "invalidate": lambda s: None,
                "get_access_token": lambda s: "t",
            })()
            self._thesis_tpl = None

        def _request(self, path, params=None):
            return responder()

    return Fake()


def test_401落在最后一次尝试上仍然抛AuthError(monkeypatch):
    """
    ⚠️ 401 若正好落在**最后一次**尝试上,那句 `continue` 会把循环耗尽,
       走到函数末尾抛 FomoAPIError —— 就是文件头说的那种静默失效。
       _MAX_ATTEMPTS 从 5 降到 3 之后,凑齐"前面全失败 + 最后一次 401"的门槛低了不少,
       而这个 API 一次抖动就能甩出一串 504/429。
    """
    calls = {"n": 0}

    def responder():
        calls["n"] += 1
        # 前面服务端错误,最后一次鉴权失败 —— 恰好把重试次数用光
        return (500, "boom", {}) if calls["n"] < C._MAX_ATTEMPTS else (401, "unauthorized", {})

    monkeypatch.setattr(C, "sleep_or_stop", lambda _s: False)   # 别真的退避
    with pytest.raises(C.AuthError):
        _fake_client(responder)._fetch_ok("/whatever")
    assert calls["n"] == C._MAX_ATTEMPTS


def test_纯服务端错误耗尽重试仍然是FomoAPIError(monkeypatch):
    """反向:没出现过 401 就不该谎报成鉴权失败,否则会误停轮询、让用户白跑一次 --login"""
    monkeypatch.setattr(C, "sleep_or_stop", lambda _s: False)   # 别真的退避
    with pytest.raises(C.FomoAPIError) as ei:
        _fake_client(lambda: (503, "unavailable", {}))._fetch_ok("/whatever")
    assert not isinstance(ei.value, C.AuthError)


def test_429退避带抖动():
    """
    实测过一次真实雷群:同一毫秒 15 个请求一起 429,服务端给的 retry-after 又完全相同,
    于是它们又在同一毫秒一起重试 —— 把刚才那波压力原样重放一遍。
    """
    waits = {C._retry_after({"retry-after": "5"}) for _ in range(20)}
    assert len(waits) > 1, "退避必须带抖动,不能是固定值"
    assert all(3.0 <= w <= 7.0 for w in waits), f"抖动幅度要合理,实际 {sorted(waits)[:3]}"


def test_429退避仍然夹上限():
    """畸形/极端的 retry-after 不该把整轮卡死"""
    assert C._retry_after({"retry-after": "99999"}) <= 60.0 * (1 + C._JITTER)
    assert C._retry_after({"retry-after": "garbage"}) > 0
