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


# ============================================================
# transfers:/v2/users/{uid}/transfers
# ============================================================
# ⚠️ 下面这份报文是 2026-08-26 从真实接口抓下来的原件($fih 分发给 PoorGoat_ 的那一笔),
#    一个字段都没删改。测试这一层的意义就在于"我们对真实结构的假设"能不能被守住 ——
#    换成手编的简化 JSON,恰恰把最容易出错的那部分(信封层级、tokenMetadata 嵌套)
#    测没了。
_REAL_TRANSFER_ENVELOPE = {
    "success": True,
    "message": "Transfers found",
    "responseObject": {
        "transfers": [{
            "id": "1604366a-1ca6-47c1-b817-4fcc52b58584",
            "toAddress": "7xYXu3gtFzbDa59fmCZnzQSo81frz9MNDBtVuJ8AfxTK",
            "fromAddress": "8FtY7n1ad4LvXqyw8FojCjc7aPLVyTgXXyMJPL2cZx72",
            "isNativeToken": False,
            "tokenAddress": "547tWxWhym8U7Y7DvhGJktpkcs5eHeywvSYnhwvdpump",
            "networkId": 1399811149,
            "humanAmount": 12000000,
            "tokenAmount": 12000000000000,
            "tokenAmountString": "12000000000000",
            "usdAmount": 2439.09,
            "type": "DEPOSIT",
            "createdAt": "2026-08-26T00:37:40.579Z",
            "fromTradeId": None,
            "toTradeId": "7d4252d4-8ccd-44c3-926e-d8a7a8278ed2",
            "isReferral": None,
            "isCrossmint": False,
            "tokenMetadata": {"imageLargeUrl": "https://…png", "symbol": "fih"},
        }],
        "hasNextPage": False,
    },
    "statusCode": 200,
}


def _recording_client(pages):
    """把 _get 换掉:记录每次请求的 (path, params),按顺序吐 pages 里的响应"""

    class Fake(C._BaseFomoClient):
        def __init__(self):
            self.seen = []
            self._i = 0

        def _get(self, path, params=None):
            self.seen.append((path, dict(params or {})))
            page = pages[min(self._i, len(pages) - 1)]
            self._i += 1
            return page

    return Fake()


def test_transfers_真实信封能解出条目():
    """
    ⚠️ 真实响应是 {"responseObject": {"transfers": [...], "hasNextPage": …}} 双层信封。
       解不开的话 get_transfers 恒返回 0 条 —— 而这种失效**不报错**:
       看起来"连得上、没异常、就是没数据",与"这个人确实没转账"长得一模一样。
    """
    c = _recording_client([_REAL_TRANSFER_ENVELOPE])
    items = c.get_transfers("uid-1")
    assert len(items) == 1
    assert items[0]["id"] == "1604366a-1ca6-47c1-b817-4fcc52b58584"
    path, params = c.seen[0]
    assert path == "/v2/users/uid-1/transfers", "端点写错会 404,而 404 会被降级吞掉"
    assert params["limit"] == 25, "默认单页条数变了就会改变漏采余量,见 _TRANSFERS_PAGE"


def test_transfers_翻页游标是lastTransferId():
    """
    ⚠️ 这个 API 对**不认识的参数一律静默忽略**(swaps 上用 offset 踩过一次):
       游标参数名写错不会报错,只会一直返回第一页 → 翻页变成无限重复第一页。
       所以这条测试盯的不是"能不能翻页",而是"第二页请求里到底带了什么参数"。
    """
    full = {"responseObject": {
        "transfers": [{"id": f"id-{i}"} for i in range(C._PAGE_SIZE)],
        "hasNextPage": True}}
    tail = {"responseObject": {"transfers": [{"id": "id-last"}], "hasNextPage": False}}
    c = _recording_client([full, tail])
    got = list(c.iter_transfers("uid-1", max_items=999))
    assert len(got) == C._PAGE_SIZE + 1
    assert "lastTransferId" not in c.seen[0][1], "第一页不该带游标"
    assert c.seen[1][1]["lastTransferId"] == f"id-{C._PAGE_SIZE - 1}", \
        "第二页的游标必须是上一页**最后一条**的 id"


def test_transfers_同一页反复返回时必须停手():
    """
    兜底:万一哪天参数名又变了,服务端会一直吐第一页。不停就是无限循环 + 无限重复数据。
    """
    same = {"responseObject": {
        "transfers": [{"id": f"id-{i}"} for i in range(C._PAGE_SIZE)],
        "hasNextPage": True}}
    c = _recording_client([same])
    got = list(c.iter_transfers("uid-1", max_items=10_000))
    assert len(got) == C._PAGE_SIZE, "重复页必须被识别出来并停手,而不是一路翻到 _MAX_PAGES"
