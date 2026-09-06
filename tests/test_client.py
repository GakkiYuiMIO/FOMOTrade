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

import json

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


# ============================================================
# /chips 的两个端点:/hodlers/top(带鉴权)与 /public/proxy/filterTokens(匿名)
# ============================================================
# ⚠️⚠️ 这一段盯的是**请求本身长什么样**,不是响应能不能解析。
#    原因很直白:端点路径写错、请求体键名写错、链 ID 忘了转成数字、limit 漏传、
#    TLS 指纹忘了设 —— 这五种错误全都**不会在本地报任何错**,只会在生产上
#    真发一条 /chips 命令时才现形(404 / 400 / 空榜 / HTTP 430)。
#    所以下面每一条断言都写死字面量,故意不从 src.client import 任何常量:
#    从被测模块取门槛就成了同义反复,常量改错时断言跟着一起改错。
_CHIPS_CA = "BoAQaykj3LtkM2Brevc7cQcRAzpqcsP47nJ2rkyopump"
# Solana 在 FOMO 侧的**数字**链 ID。这是外部事实,写死
_SOLANA_NET_ID = 1399811149

_TOP_HOLDERS_ENVELOPE = {
    "success": True,
    "responseObject": [{
        "tokenAddress": _CHIPS_CA,
        "networkId": _SOLANA_NET_ID,
        "totalHolders": 2,
        "topHolders": [
            {"user": {"id": "u-1", "userHandle": "alice"},
             "humanAmount": 12_000_000, "value": 240.5},
            {"user": {"id": "u-2", "userHandle": "bob"},
             "humanAmount": 3_000_000, "value": 60.1},
        ],
    }],
    "statusCode": 200,
}

_FILTER_TOKENS_ENVELOPE = {
    "success": True,
    "responseObject": [{
        "token": {"info": {"symbol": "FOREST", "totalSupply": "999999999.123"}},
    }],
    "statusCode": 200,
}


def _wired_http_client(monkeypatch, body: str, *, status: int = 200):
    """
    **真的** HttpFomoClient,只把 curl_cffi 的 Session 换成录音机。

    ⚠️ 换的是 curl_cffi.requests.Session 本身而不是 client 的某个方法:
       _request 怎么拼 URL、怎么带鉴权头、Session 用什么指纹创建,全都要真的跑一遍,
       否则测的还是桩、不是代码。
    """
    import curl_cffi.requests as cffi

    box: dict = {"gets": []}

    class _Resp:
        status_code = status
        text = body
        headers: dict = {}

    class _RecordingSession:
        def __init__(self, **kw):
            box["session_kwargs"] = kw

        def get(self, url, params=None, headers=None):
            box["gets"].append({"url": url,
                                "params": dict(params or {}),
                                "headers": dict(headers or {})})
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(cffi, "Session", _RecordingSession)
    tokens = type("T", (), {"get_access_token": lambda s: "tkn-xyz",
                            "invalidate": lambda s: None})()
    return C.HttpFomoClient(tokens), box


def _wired_post(monkeypatch, body: str, *, status: int = 200):
    """匿名那一路只用 curl_cffi.requests.post,换掉它就能把整个请求录下来"""
    import curl_cffi.requests as cffi

    box: dict = {}

    class _Resp:
        status_code = status
        text = body

    def _post(url, **kw):
        box["url"] = url
        box.update(kw)
        return _Resp()

    monkeypatch.setattr(cffi, "post", _post)
    return box


def test_持有人榜打到的是hodlers_top(monkeypatch):
    """⚠️ 路径写错 = 生产上 404,而 404 会被 _chips_error 翻成一句"接口异常",查不到根因"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    c.get_top_holders(_CHIPS_CA, _SOLANA_NET_ID)

    assert len(box["gets"]) == 1, "只该发一个请求 —— 这个端点没有分页能力"
    assert box["gets"][0]["url"] == "https://prod-api.fomo.family/hodlers/top", box["gets"][0]


def test_持有人榜请求体的键名是address与networkId(monkeypatch):
    """
    ⚠️ 这个 API 对不认识的参数**一律静默忽略**(transfers 的翻页游标上踩过一次):
       键名写成 addr / net 不会报错,只会返回一份空榜 —— 而空榜与"这个币还没人买"
       长得一模一样,于是 /chips 会平静地告诉用户"没查到持有人"。
    """
    c, box = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    c.get_top_holders(_CHIPS_CA, _SOLANA_NET_ID)

    tokens = json.loads(box["gets"][0]["params"]["tokens"])
    assert tokens == [{"address": _CHIPS_CA, "networkId": _SOLANA_NET_ID}], tokens


def test_持有人榜带上了条数上限(monkeypatch):
    """⚠️ limit 漏传时服务端给多少条完全由它说了算,而条数正是"精确/下界"的判据之一"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    c.get_top_holders(_CHIPS_CA, _SOLANA_NET_ID)

    assert box["gets"][0]["params"]["limit"] == 100, box["gets"][0]["params"]


def test_链ID在请求体里是数字不是链名(monkeypatch):
    """
    ⚠️ 与 get_token_thesis 同源的坑:传 "solana" / "bsc" 这种本地别名,服务端直接 400。
       JSON 里必须是**数字** 1399811149,连字符串形态的 "1399811149" 都不行。
    """
    c, box = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    c.get_top_holders(_CHIPS_CA, "1399811149")      # 调用方给的是字符串(bot 侧的映射表就是字符串)

    net = json.loads(box["gets"][0]["params"]["tokens"])[0]["networkId"]
    assert net == 1399811149, f"链 ID 没转成数字: {net!r}"
    assert isinstance(net, int), f"链 ID 必须是 JSON 数字而不是字符串: {net!r}"


def test_持有人榜用的是chrome指纹(monkeypatch):
    """⚠️ Cloudflare 认 TLS 指纹。裸请求实测 HTTP 430 —— 这不是可选优化,是能不能通的前提"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    c.get_top_holders(_CHIPS_CA, _SOLANA_NET_ID)

    assert box["session_kwargs"].get("impersonate") == "chrome", box["session_kwargs"]


def test_持有人榜解出条目与总数(monkeypatch):
    """信封是 {"responseObject": [ {...} ]},剥不开就恒返回 {} —— 又是一种不报错的失效"""
    c, _ = _wired_http_client(monkeypatch, json.dumps(_TOP_HOLDERS_ENVELOPE))

    got = c.get_top_holders(_CHIPS_CA, _SOLANA_NET_ID)

    assert got["totalHolders"] == 2
    assert [h["user"]["id"] for h in got["topHolders"]] == ["u-1", "u-2"]


def test_元数据打到的是filterTokens(monkeypatch):
    box = _wired_post(monkeypatch, json.dumps(_FILTER_TOKENS_ENVELOPE))

    C.fetch_token_meta(_CHIPS_CA, _SOLANA_NET_ID)

    assert box["url"] == "https://prod-api.fomo.family/public/proxy/filterTokens", box["url"]


def test_元数据请求体是地址冒号数字链ID的字符串数组(monkeypatch):
    """
    ⚠️ 这个端点的 body 形状与持有人榜完全不同:**字符串数组** ["<地址>:<数字链ID>"],
       不是对象数组。写成对象数组不会报错,只会拿不到 totalSupply → 占比整段消失。
    """
    box = _wired_post(monkeypatch, json.dumps(_FILTER_TOKENS_ENVELOPE))

    C.fetch_token_meta(_CHIPS_CA, "1399811149")

    assert box["json"] == [f"{_CHIPS_CA}:1399811149"], box["json"]


def test_元数据请求绝不带鉴权头(monkeypatch):
    """
    ⚠️⚠️ 这是这条路径的**关键性质**,不是顺带的优化:它匿名可调,因此不占用
       监控进程共用的那份登录态。一旦带上 Bearer,401/403 就会牵连到那份登录态
       (更糟的是可能触发 invalidate),把一个"顺手查个供应量"变成把监控搞掉线。
    """
    box = _wired_post(monkeypatch, json.dumps(_FILTER_TOKENS_ENVELOPE))

    C.fetch_token_meta(_CHIPS_CA, _SOLANA_NET_ID)

    keys = [k.lower() for k in (box.get("headers") or {})]
    assert "authorization" not in keys, f"匿名端点带上了鉴权头: {keys}"


def test_元数据请求必须带X_Supported_Chains头(monkeypatch):
    """
    ⚠️⚠️ 这个头**不是可选的优化**。真网络实测(2026-09-03):

        带头   → HTTP 200,responseObject 有 1 条(CUM,robinhood/4663)
        不带头 → HTTP 200,responseObject = []      ← 成功状态码 + 空数组

    也就是说少了它,EVM 链(robinhood / base / bsc)的元数据**永远查不到**,
    而且没有任何错误码、没有任何日志会说它错了 —— /chips 的分母静默退化成
    本地推算,没有人会发现。上一版正是缺这个头。
    ⚠️ 断言写死字面量,不从被测模块 import SUPPORTED_CHAINS ——
       把常量改成空串时这条必须红。
    """
    box = _wired_post(monkeypatch, json.dumps(_FILTER_TOKENS_ENVELOPE))

    C.fetch_token_meta(_CHIPS_CA, _SOLANA_NET_ID)

    headers = {k.lower(): v for k, v in (box.get("headers") or {}).items()}
    assert headers.get("x-supported-chains") == "1,56,143,4663,8453,1399811149", headers


def test_元数据请求用的是chrome指纹(monkeypatch):
    """⚠️ 同上:匿名 ≠ 随便调。urllib 裸调实测 HTTP 430,指纹是必需品"""
    box = _wired_post(monkeypatch, json.dumps(_FILTER_TOKENS_ENVELOPE))

    C.fetch_token_meta(_CHIPS_CA, _SOLANA_NET_ID)

    assert box.get("impersonate") == "chrome", box.keys()


def test_元数据非2xx时降级为空而不是抛(monkeypatch):
    """分母是独立数据源,它挂掉只该让占比消失,绝不能把已经拿到的持有人数一起拖走"""
    box = _wired_post(monkeypatch, "nope", status=500)

    assert C.fetch_token_meta(_CHIPS_CA, _SOLANA_NET_ID) == {}
    assert box["url"].endswith("/public/proxy/filterTokens")


def test_总供应量解析与脏值兜底():
    """⚠️ 0 / 负数 / 缺失都返回 None:0 当分母算出的占比是荒唐值,而 None 让那一行整行消失"""
    assert C.total_supply_of({"token": {"info": {"totalSupply": "999999999.123"}}}) == \
        pytest.approx(999999999.123)
    assert C.total_supply_of({}) is None
    assert C.total_supply_of({"token": {"info": {"totalSupply": "0"}}}) is None
    assert C.total_supply_of({"token": {"info": {"totalSupply": "-1"}}}) is None
    assert C.total_supply_of({"token": {"info": {"totalSupply": "abc"}}}) is None


# ============================================================
# 榜单的 limit 上限(2026-09-05 实测)
# ============================================================
# ⚠️⚠️ 这几条钉住一件事:服务端真实上限是 **150**,不是 100。
#    上一版 get_leaderboard 里写的是 `min(int(limit), 100)`,注释说"服务端硬上限 100"
#    —— 那句话是 **_FOLLOWING_PAGE 的**(/followingPaginate 传 200 确实 400),
#    被错抄到了排行榜上。代价:榜上第 101~150 名永远看不见,而且**没有任何报错**。
#    实测:limit=150 → 150 行;151/160/175/199/500/1000 一律静默返回 150。
_BOARD_ENVELOPE = {"responseObject": [{"id": "u1", "userHandle": "a", "pnl24h": 1.0}]}


def test_榜单一次能拉到一百五十行(monkeypatch):
    """⚠️ 改回 min(limit, 100) 这条当场红。"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_BOARD_ENVELOPE))

    c.get_leaderboard("24h", 150)

    assert box["gets"][0]["params"]["limit"] == 150, box["gets"][0]["params"]
    assert box["gets"][0]["url"] == "https://prod-api.fomo.family/v2/leaderboard/24h"


def test_榜单超过一百五十的照样夹到一百五十(monkeypatch):
    """⚠️ 服务端对 >150 是**静默**返回 150(不报错),我们自己也夹在同一个数上。"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_BOARD_ENVELOPE))

    for n in (151, 500, 1000):
        c.get_leaderboard("24h", n)

    assert [g["params"]["limit"] for g in box["gets"]] == [150, 150, 150]


def test_榜单绝不再夹回一百(monkeypatch):
    """⚠️⚠️ 独占钉子:任何把上限改回 100(或任何 <150 的值)的改动,这条必红。"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_BOARD_ENVELOPE))

    c.get_leaderboard("24h", 120)
    c.get_leaderboard("24h", 101)

    assert [g["params"]["limit"] for g in box["gets"]] == [120, 101]


def test_关注列表分页上限与榜单上限不是同一个数():
    """⚠️ 两条限制曾经被混为一谈。/followingPaginate 是 100,排行榜是 150。"""
    assert C._FOLLOWING_PAGE == 100
    assert C.LEADERBOARD_MAX_LIMIT == 150


def test_榜单小于一的夹到一(monkeypatch):
    """⚠️ limit **必传**,不带直接 400;传 0/负数同理。"""
    c, box = _wired_http_client(monkeypatch, json.dumps(_BOARD_ENVELOPE))

    c.get_leaderboard("24h", 0)

    assert box["gets"][0]["params"]["limit"] == 1


# ============================================================
# auth_invalidate=False —— 锦上添花型调用方绝不处置登录态
# ============================================================
def _counting_client(status: int):
    """记下 tokens.invalidate() 被调了几次。"""
    box = {"invalidated": 0, "requests": 0}

    class Fake(C._BaseFomoClient):
        def __init__(self):
            self._tokens = type("T", (), {
                "invalidate": lambda s: box.__setitem__("invalidated",
                                                        box["invalidated"] + 1),
                "get_access_token": lambda s: "t",
            })()
            self._thesis_tpl = None

        def _request(self, path, params=None):
            box["requests"] += 1
            return (status, '{"message":"unauthorized"}', {})

    return Fake(), box


@pytest.mark.parametrize("status", [401, 403])
def test_关掉处置开关后401不作废登录态(status):
    """
    ⚠️⚠️ 这一块(🏅 盈利榜持有人)是推送里可有可无的一行,而登录态是**全进程共用**的。
       让一个可有可无的请求去 invalidate() 好端端的 access token(甚至在没有
       refresh token 时打出"请重新 --login"),是拿主路径的命赌一行装饰。
       真过期时主路径自己会 401、自己续期,这一块下一轮就自愈了。
    """
    c, box = _counting_client(status)

    with pytest.raises(C.AuthError):
        c._fetch_ok("/v2/leaderboard/24h", {"limit": 150}, auth_invalidate=False)

    assert box["invalidated"] == 0, "锦上添花型调用方绝不许处置登录态"
    assert box["requests"] == 1, "而且不重试 —— 401 重试一次也是白挨一次"


@pytest.mark.parametrize("status", [401, 403])
def test_默认仍然照旧续期一次(status):
    """⚠️ 反向:主路径的行为一字不变(续期后重试一次,仍失败才抛)。"""
    c, box = _counting_client(status)

    with pytest.raises(C.AuthError):
        c._fetch_ok("/v2/users/x/swaps")

    assert box["invalidated"] == 1
    assert box["requests"] == 2


def test_关掉处置开关不影响Cloudflare那条分支():
    """⚠️ Cloudflare 拦截仍要单独说清 —— 用户的处置完全不同(改 FOMO_CLIENT_IMPL)。"""
    box = {"invalidated": 0}

    class Fake(C._BaseFomoClient):
        def __init__(self):
            self._tokens = type("T", (), {
                "invalidate": lambda s: box.__setitem__("invalidated", 1),
                "get_access_token": lambda s: "t",
            })()
            self._thesis_tpl = None

        def _request(self, path, params=None):
            return (403, "<html>Attention Required! | Cloudflare</html>", {})

    with pytest.raises(C.AuthError) as ei:
        Fake()._fetch_ok("/hodlers/top", auth_invalidate=False)
    assert "Cloudflare" in str(ei.value)
    assert box["invalidated"] == 0
