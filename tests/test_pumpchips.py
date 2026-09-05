"""
/chips 回执里 💊 pump.fun 那半边的单测。

⚠️ 全部走**离线夹具**(tests/fixtures/pump_mint_positions_*.json /
   pump_coin_v3_*.json,是 2026-09-05 从真实响应原样存下来的),一条用例都不打网络。
⚠️ 断言里的人数、占比、措辞一律**写死字面量**,绝不从 src.pumpchips / src.bot
   import 阈值、正则或函数来断言自己 —— 那种断言等价于 `x == x`,
   把阈值改坏了它照样绿(本项目栽过很多次)。
⚠️ 这一块盯的第一重点与 🏦 FOMO 那半边完全一致:**诚实**。
   `totalCount` 是平台自报的人数,而明细**枚举不全**是常态
   (实测 totalCount=107 的币翻完只有 1 条明细),所以
   "持仓 X%" 与 "持仓 ≥X%"、"无人持有" 与 "前 N 名内无人"
   必须是一眼可辨的两套话,而且判据本身不能被改坏。
"""
# ruff: noqa: N802
from __future__ import annotations

import ast
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# 真实的 pump userId(UUID)。⚠️ 名单匹配的键就是它,不是用户名
UID_1000X = "3791aefa-662c-4fd4-9aac-60db5ca185a2"
UID_FLIP = "76129303-d90d-45f9-ae69-8c749505f90d"
UID_BRC20 = "5d823f4c-2c72-4dac-9814-9e20486db3cd"
UID_HEXIE = "046999d1-1609-4654-b8ed-06827aa50ae9"
UID_SIX = "3e2dae68-b1a5-44c1-b809-bf2cd01a97b3"
UID_STRANGER = "00000000-0000-0000-0000-00000000dead"

CA_CAP = "0x2f219c706e052dc25372a0c59dcc2afe0cab12f3"     # Robinhood Chain 的 $CAP
CA_SOL = "C7vNK395Dq4Es8Yxd1g1A8o3Wb9u2bxoQH1Y6FSypump"


def fx(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _pos(uid, *, name="stranger", held=1000.0, pnl=1.0):
    """一行 mint-positions。形状照抄实测响应的键名"""
    return {"coinMint": CA_CAP, "chainId": 4663, "userId": uid, "userName": name,
            "walletAddress": "5f1AoBaqeBZ3sQhNVQp7xYANb7ykj4xzYBh8eW5RYyFE",
            "isVerified": True, "xUsername": "x", "accountKind": "person",
            "amountHeld": held, "pnlUsd": 1.0, "pnlPercentage": pnl,
            "realizedPnlUsd": 0, "costBasisUsd": 1.0, "amountBoughtUsd": 1.0}


def _page(total, rows):
    return {"positions": rows, "totalCount": total}


# 总供应量 1e9 的 coins-v3 响应(EVM:1e27 最小单位 / 18 位小数)
def _coin(supply="1000000000000000000000000000", decimals=18):
    return {"mint": CA_CAP, "symbol": "CAP", "total_supply_str": supply,
            "base_decimals": decimals}


class FakePump:
    """
    够 pumpchips.fetch_chips 用的最小 pump 客户端。

    pages 是 {页码: 响应体};没登记的页返回空页(与真实服务端翻过头的行为一致)。
    """

    def __init__(self, pages, coin=None, *, exc=None, coin_exc=None,
                 delay=0.0, coin_delay=0.0):
        self.pages = pages
        self.coin = coin
        self.exc = exc
        self.coin_exc = coin_exc
        self.delay = delay
        self.coin_delay = coin_delay
        self.calls: list[tuple] = []
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def fetch_mint_positions(self, mint, page=0, page_size=50):
        with self._lock:
            self.calls.append(("positions", mint, page, page_size))
            self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.pages.get(page, {"positions": [], "totalCount":
                                     self.pages.get(0, {}).get("totalCount")})

    def fetch_coin_payload(self, mint):
        with self._lock:
            self.calls.append(("coin", mint, None, None))
        if self.coin_delay:
            time.sleep(self.coin_delay)
        if self.coin_exc is not None:
            raise self.coin_exc
        return self.coin


# ============================================================
# 1. 解析:四条链的真实响应 + 空列表 + 400 / 404
# ============================================================
class Test解析:
    @pytest.mark.parametrize(("name", "total", "rows", "uid", "held"), [
        ("robinhood", 430, 50, UID_1000X, 14584546.193562904),
        ("solana", 25, 24, UID_FLIP, 39152766.605289),
        ("bsc", 7, 7, UID_1000X, 0.001),
        ("base", 333, 44, UID_BRC20, 4497303.378383578),
    ])
    def test_四条链的真实响应都解析得出(self, name, total, rows, uid, held):
        """⚠️ 人数与行数**写死实测值**:它们不相等正是本功能全部文案的支点"""
        from src.pumpchips import parse_mint_positions

        got = parse_mint_positions(fx(f"pump_mint_positions_{name}"))
        assert got is not None
        got_total, got_rows = got
        assert got_total == total
        assert len(got_rows) == rows
        one = next(r for r in got_rows if r.user_id == uid)
        assert one.amount_held == held

    def test_totalCount与拿得到的明细条数常常不等(self):
        """
        ⚠️⚠️ 本功能最重要的一条实测事实,单列一条钉住它。
           robinhood 的 $CAP 自报 430 人,而这一页只给 50 条;solana 那个自报 25 人、
           一页只给 24 条(**总数比一页上限还小,却仍然给不满**)。
           谁要是把"这一页不满 50 就是最后一页"当终止条件,或者把 totalCount
           当成"我们能枚举出来的人数",这条当场红。
        """
        from src.pumpchips import parse_mint_positions

        assert parse_mint_positions(fx("pump_mint_positions_solana")) [0] == 25
        assert len(parse_mint_positions(fx("pump_mint_positions_solana"))[1]) == 24

    def test_空明细页解析成零行而不是失败(self):
        """
        ⚠️ 实测 `{"positions":[],"totalCount":107}`:平台自报 107 人,
           第 2 页起一条明细都不给。它与"请求失败"是两件事 —— 前者要显示人数,
           后者整块消失。
        """
        from src.pumpchips import parse_mint_positions

        payload = fx("pump_mint_positions_empty_page")
        assert payload == {"positions": [], "totalCount": 107}
        assert parse_mint_positions(payload) == (107, [])

    def test_人数为零是真实值不是失败(self):
        from src.pumpchips import parse_mint_positions

        assert parse_mint_positions(fx("pump_mint_positions_zero")) == (0, [])

    @pytest.mark.parametrize("name", ["pump_mint_positions_400", "pump_mint_positions_404"])
    def test_错误响应体解析成None(self, name):
        """⚠️ NestJS 的错误体是一个 dict,里面既没有 totalCount 也没有 positions"""
        from src.pumpchips import parse_mint_positions

        payload = fx(name)
        assert payload["statusCode"] in (400, 404)
        assert parse_mint_positions(payload) is None

    @pytest.mark.parametrize("bad", [None, [], "x", 3, {"positions": []}])
    def test_结构不对一律None(self, bad):
        from src.pumpchips import parse_mint_positions

        assert parse_mint_positions(bad) is None

    def test_缺userId的行被丢掉(self):
        from src.pumpchips import parse_mint_positions

        got = parse_mint_positions(_page(2, [_pos(UID_1000X), {"amountHeld": 1.0}]))
        assert [r.user_id for r in got[1]] == [UID_1000X]


class Test分母:
    def test_真实响应算得出总供应量(self):
        """⚠️ 1e27 最小单位 / 1e18 = 1e9 枚。不除 decimals 会小十八个数量级"""
        from src.pumpchips import parse_supply

        assert parse_supply(fx("pump_coin_v3_robinhood")) == 1e9

    def test_solana的六位小数也对(self):
        from src.pumpchips import parse_supply

        assert parse_supply(fx("pump_coin_v3_solana")) == 1e9

    @pytest.mark.parametrize("name", ["pump_coin_v3_null", "pump_coin_v3_404"])
    def test_非pump上架的币返回字面null(self, name):
        """⚠️ 实测 HTTP 200、响应体就是四个字节的 `null` —— 它只是被 pump 用户
           持有的外部币,pump 自己没有它的元数据"""
        from src.pumpchips import parse_supply

        assert fx(name) is None
        assert parse_supply(fx(name)) is None

    @pytest.mark.parametrize("payload", [
        {"total_supply_str": "0", "base_decimals": 18},
        {"total_supply_str": "-1", "base_decimals": 18},
        {"total_supply_str": "1e27"},
        {"base_decimals": 18},
        {"total_supply_str": "1000", "base_decimals": 99},
        {"total_supply_str": "abc", "base_decimals": 18},
    ])
    def test_脏分母一律None(self, payload):
        from src.pumpchips import parse_supply

        assert parse_supply(payload) is None

    def test_小数位必须真的被用上(self):
        """
        ⚠️ M-占比分母:把 base_decimals 忽略掉(直接拿 total_supply_str 当供应量)
           会让分母大 1e18 倍。这条用两个**只有 decimals 不同**的响应钉住它。
        """
        from src.pumpchips import parse_supply

        assert parse_supply({"total_supply_str": "1000000", "base_decimals": 0}) == 1e6
        assert parse_supply({"total_supply_str": "1000000", "base_decimals": 6}) == 1.0


# ============================================================
# 2. 取值:翻页、去重、降级、预算
# ============================================================
class Test取值:
    def test_按totalCount决定翻几页而不是看这一页满不满(self):
        """
        ⚠️⚠️ M-翻页终止条件:Addog 实测 page0 只有 46 行、page1 还有 45 行。
           拿"不满一页就收工"当终止条件会漏掉一多半人。
           这里 total=120 → ceil(120/50) = 3 页,page0 只给 10 行也必须继续翻。
        """
        from src.pumpchips import fetch_chips

        pages = {0: _page(120, [_pos(f"u{i}") for i in range(10)]),
                 1: _page(120, [_pos(f"v{i}") for i in range(50)]),
                 2: _page(120, [_pos(f"w{i}") for i in range(20)])}
        c = FakePump(pages, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=4)
        assert ch.covered == 80
        assert sorted(p for k, _, p, _ in c.calls if k == "positions") == [0, 1, 2]

    def test_page是0起算的(self):
        """⚠️ M-page:改成 1-indexed 的话第一页会变成 page=1,而且会多翻一页"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(60, [_pos("u1")]), 1: _page(60, [_pos("u2")])}, _coin())
        fetch_chips(c, CA_CAP, {}, workers=2)
        assert sorted(p for k, _, p, _ in c.calls if k == "positions") == [0, 1]

    def test_翻页请求的pageSize不超过50(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(200, [_pos("u1")])}, _coin())
        fetch_chips(c, CA_CAP, {}, workers=4)
        assert {ps for k, _, _, ps in c.calls if k == "positions"} == {50}

    def test_同一个人在两页里出现只算一次(self):
        """⚠️ 翻页期间数据会动,同一个人被算两遍就是把占比凭空放大"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(60, [_pos("dup", held=100.0)]),
                      1: _page(60, [_pos("dup", held=100.0)])}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=2)
        assert ch.covered == 1
        assert ch.plat_pct == pytest.approx(100.0 / 1e9 * 100.0)

    def test_人数超过阈值就降级成只看第一页(self):
        """
        ⚠️⚠️ M-阈值:阈值拿掉的话这里会翻 ceil(4000/50)=80 页。
           这条钉的是"降级真的发生了",措辞那条在 Test文案 里。
        """
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(4000, [_pos(f"u{i}") for i in range(50)])}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=4, full_scan_max=3000)
        assert [p for k, _, p, _ in c.calls if k == "positions"] == [0]
        assert ch.full_scan is False
        assert ch.exact is False
        assert ch.covered == 50

    def test_没超过阈值就全量翻(self):
        from src.pumpchips import fetch_chips

        c = FakePump({p: _page(100, [_pos(f"u{p}-{i}") for i in range(50)])
                      for p in range(2)}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=4, full_scan_max=3000)
        assert ch.full_scan is True
        assert ch.exact is True
        assert ch.covered == 100

    def test_墙钟预算到点就用手上的部分(self):
        """
        ⚠️⚠️ M-墙钟:预算拿掉的话这条会等满 5 × 0.4 秒。
           /chips 是同步命令,用户在等 —— 宁可给一份说清楚是部分结果的数据。
        """
        from src.pumpchips import fetch_chips

        pages = {p: _page(300, [_pos(f"u{p}")]) for p in range(6)}
        c = FakePump(pages, _coin(), delay=0.4)
        t0 = time.monotonic()
        ch = fetch_chips(c, CA_CAP, {}, workers=1, budget_sec=0.5)
        elapsed = time.monotonic() - t0
        assert ch.partial is True
        assert ch.full_scan is False
        assert elapsed < 2.0, f"预算没生效,实际等了 {elapsed:.2f}s"

    def test_预算为零时一页都不多翻(self):
        from src.pumpchips import fetch_chips

        c = FakePump({p: _page(300, [_pos(f"u{p}")]) for p in range(6)}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=2, budget_sec=0.0)
        assert ch.partial is True
        assert [p for k, _, p, _ in c.calls if k == "positions"] == [0]

    def test_第一页拿不到整块消失(self):
        from src.pumpchips import fetch_chips

        assert fetch_chips(FakePump({0: None}, _coin()), CA_CAP, {}) is None
        assert fetch_chips(FakePump({}, _coin(), exc=RuntimeError("boom")), CA_CAP, {}) is None
        assert fetch_chips(FakePump({0: fx("pump_mint_positions_400")}, _coin()),
                           CA_CAP, {}) is None

    def test_某一页挂了只丢那一页(self):
        """⚠️ 少几个人只是占比更低;整块消失是过度反应"""
        from src.pumpchips import fetch_chips

        class _Flaky(FakePump):
            def fetch_mint_positions(self, mint, page=0, page_size=50):
                if page == 1:
                    raise RuntimeError("这一页挂了")
                return super().fetch_mint_positions(mint, page, page_size)

        c = _Flaky({0: _page(150, [_pos("a")]), 2: _page(150, [_pos("c")])}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=3)
        assert ch.covered == 2
        assert ch.total == 150

    def test_分母挂了人数照样在(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos("a", held=5.0)])}, None,
                     coin_exc=RuntimeError("分母挂了"))
        ch = fetch_chips(c, CA_CAP, {})
        assert ch.total == 1
        assert ch.covered == 1
        assert ch.plat_pct is None

    def test_服务端自报的总数比明细还少时以明细为准且不声称精确(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos("a"), _pos("b")])}, _coin())
        ch = fetch_chips(c, CA_CAP, {})
        assert ch.total == 2
        assert ch.covered == 2
        assert ch.exact is False, "总数都自相矛盾了,更没有资格说这就是全部"

    def test_一个请求都没白发(self):
        """⚠️ 请求数写死:第一页 1 + 分母 1 + 第 1..2 页 2 = 4"""
        from src.pumpchips import fetch_chips

        c = FakePump({p: _page(120, [_pos(f"u{p}")]) for p in range(3)}, _coin())
        ch = fetch_chips(c, CA_CAP, {}, workers=3)
        assert ch.requests == 4
        assert len(c.calls) == 4


class Test名单匹配:
    def test_按userId匹配而不是按用户名(self):
        """
        ⚠️⚠️ M-userId匹配:改成按 userName 匹配的话,这条里
           那个**用户名叫 1000XCryptoD 的陌生人**会被算进名单,
           而真正的成员(用户名已经改掉)会被漏掉。用户名本人随时可以改,
           谁都能把自己的用户名改成名单里某个人的名字。
        """
        from src.pumpchips import fetch_chips

        rows = [_pos(UID_STRANGER, name="1000XCryptoD", held=999.0),
                _pos(UID_1000X, name="改过名了", held=5.0)]
        c = FakePump({0: _page(2, rows)}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"})
        assert [m["user_id"] for m in ch.matched] == [UID_1000X]
        assert ch.matched[0]["amount"] == 5.0

    def test_已清仓的人不算持有(self):
        """
        ⚠️⚠️ M-清仓:`amountHeld == 0` 是**已清仓**,不是"还持有 0 枚"。
           当成持有的话「你的名单 · 1 人持有」会指着一个早就卖光的人。
        """
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, held=0)])}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"})
        assert ch.matched == []
        assert ch.watch_pct is None

    def test_数量未知的人也不算持有(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [{"userId": UID_1000X, "userName": "x"}])}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"})
        assert ch.matched == []

    def test_展示名优先用本地名单里的那个(self):
        """⚠️ 同一个人在同一条回执的两半边必须是同一个名字"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, name="接口给的名字")])}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "本地存的名字"})
        assert ch.matched[0]["name"] == "本地存的名字"

    def test_本地没存名字时退回接口给的(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, name="接口给的名字")])}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: ""})
        assert ch.matched[0]["name"] == "接口给的名字"

    def test_名单占比与平台占比同源同口径(self):
        from src.pumpchips import fetch_chips

        rows = [_pos(UID_1000X, held=2e8), _pos(UID_STRANGER, held=1e8)]
        c = FakePump({0: _page(2, rows)}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "a"})
        assert ch.plat_pct == pytest.approx(30.0)
        assert ch.watch_pct == pytest.approx(20.0)

    def test_真实夹具上的命中(self):
        """⚠️ 数字写死实测值:$CAP 前 50 名里名单命中 1000XCryptoD 一人"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: fx("pump_mint_positions_robinhood")}, fx("pump_coin_v3_robinhood"))
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"}, full_scan_max=100)
        assert ch.total == 430
        assert ch.covered == 50
        assert ch.exact is False
        assert [m["name"] for m in ch.matched] == ["1000XCryptoD"]
        assert ch.matched[0]["amount"] == 14584546.193562904
        assert ch.watch_pct == pytest.approx(1.4584546193562903)

    def test_base那条链上命中三个人且按持仓降序(self):
        from src.pumpchips import fetch_chips

        members = {UID_BRC20: "brc20niubi", UID_HEXIE: "hexiecs", UID_SIX: "six666888eight"}
        c = FakePump({0: fx("pump_mint_positions_base")}, _coin())
        ch = fetch_chips(c, CA_CAP, members, full_scan_max=100)
        assert [m["name"] for m in ch.matched] == ["brc20niubi", "hexiecs", "six666888eight"]


# ============================================================
# 3. HTTP 层:参数不许改坏
# ============================================================
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[tuple] = []

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self.resp

    def close(self):
        pass


def _client(resp):
    from src.pumpfun import PumpClient

    c = PumpClient(proxy="")
    c._tl.session = _FakeSession(resp)
    return c, c._tl.session


class Test端点参数:
    def test_固定参数逐字对上(self):
        """
        ⚠️⚠️ 每一个值都来自服务端的 400 校验回包,不是猜的:
           sortBy ∈ {LATEST, TOP, LOWEST_ENTRY};pageSize 硬上限 50;page 0-indexed。
        """
        c, sess = _client(_Resp(200, _page(0, [])))
        c.fetch_mint_positions(CA_CAP, 3)
        _, url, kw = sess.calls[0]
        assert url == f"https://frontend-api-v3.pump.fun/mint-positions/{CA_CAP}"
        assert kw["params"] == {"sortBy": "TOP", "pageSize": 50,
                                "updatesLimit": 0, "page": 3}

    def test_绝不传withThesis(self):
        """
        ⚠️⚠️ M-withThesis:`withThesis=true` 只返回**写了 callout 的**持有人,
           大部分币直接返回空列表 —— 一传就会把"这个币没人持有"这个假事实报出去。
        """
        c, sess = _client(_Resp(200, _page(0, [])))
        c.fetch_mint_positions(CA_CAP, 0)
        _, url, kw = sess.calls[0]
        assert "withThesis" not in kw["params"]
        assert "withThesis" not in url

    def test_50这个上限是服务端说的不是我们猜的(self):
        """
        ⚠️⚠️ 阈值必须有出处。这条钉住那份**真实的 400 回包** ——
           传 pageSize=51 时服务端原话就是 "pageSize must not be greater than 50"。
           谁想把上限调宽,先看这里:调宽 = 整页 400 = 明细一条都拿不到。
        """
        payload = fx("pump_mint_positions_pagesize51")
        assert payload["statusCode"] == 400
        assert payload["message"] == ["pageSize must not be greater than 50"]
        assert "pageSize=51" in payload["path"]

    @pytest.mark.parametrize("asked", [51, 100, 999])
    def test_pageSize在发出去之前就被夹到50(self, asked):
        """⚠️ M-pageSize:服务端 51 就整页 400,而"传大一点少发几个请求"是个太自然的想法"""
        c, sess = _client(_Resp(200, _page(0, [])))
        c.fetch_mint_positions(CA_CAP, 0, asked)
        assert sess.calls[0][2]["params"]["pageSize"] == 50

    @pytest.mark.parametrize("bad", [
        "../../following-positions/alerts", "a/b", "a.b", "a?b=1", "a#b", "a b", "",
        "x" * 81, "0x2f21%2f..",
    ])
    def test_非法mint一个字节都不发出去(self, bad):
        """⚠️ mint 来自 Telegram 消息;`…/mint-positions/../../following-positions/alerts`
           会打到那个**需要登录**的端点上"""
        c, sess = _client(_Resp(200, _page(0, [])))
        assert c.fetch_mint_positions(bad, 0) is None
        assert c.fetch_coin_payload(bad) is None
        assert sess.calls == []

    def test_非2xx一律None(self):
        for status in (400, 401, 404, 429, 500):
            c, _ = _client(_Resp(status, {"statusCode": status}))
            assert c.fetch_mint_positions(CA_CAP, 0) is None

    def test_每线程一个Session(self):
        """
        ⚠️⚠️ libcurl 的 easy handle **不能被多线程同时使用**,共用一个是概率性的
           崩溃/串包。翻页是并发的,这条钉住 PumpClient 在每个线程各建一个 Session。
        """
        from src.pumpfun import PumpClient

        c = PumpClient(proxy="")
        made: list[int] = []

        class _Sess:
            def __init__(self):
                made.append(id(self))

            def get(self, url, **kw):
                time.sleep(0.02)
                return _Resp(200, _page(0, []))

            def close(self):
                pass

        c._session = lambda: _install()          # noqa: E731

        def _install():
            s = getattr(c._tl, "session", None)
            if s is None:
                s = _Sess()
                c._tl.session = s
            return s

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda p: c.fetch_mint_positions(CA_CAP, p), range(8)))
        assert len(set(made)) == len(made), "同一个 Session 被建了两次以上?"
        assert len(made) >= 2, "并发下应当有多个线程各建一个 Session"

    def test_线程池里每个线程各建一个真实Session的形态(self):
        """⚠️ 上一条验的是"不共用",这条验的是 threading.local 这个机制本身还在"""
        from src.pumpfun import PumpClient

        c = PumpClient(proxy="")
        seen: set = set()
        lock = threading.Lock()

        def work(_):
            s = getattr(c._tl, "session", None)
            if s is None:
                s = object()
                c._tl.session = s
            time.sleep(0.02)
            with lock:
                seen.add(id(getattr(c._tl, "session")))

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(work, range(8)))
        assert len(seen) >= 2

def _live_strings(path: str) -> list[str]:
    """
    模块里**真正会被执行到**的字符串字面量(去掉 docstring)。

    ⚠️ 只看字符串字面量是有依据的:一个 HTTP 端点只可能经由字符串拼出来。
       注释与 docstring 里当然会提到这些名字(那正是在写"为什么不碰它们"),
       拿整份文本做子串判断会把说明文档判成违规。
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))                 and body and isinstance(body[0], ast.Expr)                 and isinstance(body[0].value, ast.Constant)                 and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


class Test绝不碰链上持有人数:
    def test_源码里没有任何一处能拼出那个端点(self):
        """
        ⚠️⚠️ M-链上数:`/token-holders/{mint}/count` 的 `holderCount` 是**链上地址数**,
           与 `/mint-positions.totalCount`(平台托管持仓人数)差一个数量级
           (实测 Hr8CpESJ:链上 178 / 平台 167;65Nt7Tdis:链上 8561 / 平台 2035),
           而且链上那个**只有 Solana 有**,bsc/base/robinhood/eth 全部 404。
           拿链上数冒充平台数就是推错信息 —— 两个数相加或相除更是无中生有。
           这条把它钉死在源码层面:任何一处**字符串字面量**里出现这两个名字都当场红。
        """
        for name in ("src/pumpchips.py", "src/bot.py", "src/pumpfun.py"):
            for lit in _live_strings(name):
                assert "token-holders" not in lit, (name, lit)
                assert "holderCount" not in lit, (name, lit)

    def test_也不碰任何要登录的端点(self):
        """⚠️ `/followed-holders/{mint}` 是 401(要登录),`/following-positions/alerts`
           同理。这个功能全程匿名、不使用任何凭据。"""
        for name in ("src/pumpchips.py", "src/bot.py", "src/pumpfun.py"):
            for lit in _live_strings(name):
                assert "followed-holders" not in lit, (name, lit)
                assert "following-positions" not in lit, (name, lit)


class Test分母自相矛盾:
    """
    ⚠️⚠️ 2026-09-05 真网络实测打出来的洞:`$PUMP`
       (pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn)的 coins-v3 给
       total_supply_str=399462335624000000 / base_decimals=6 → 399.46B,
       而**光是前 49 名**的 amountHeld 合计就有 588.75B,算出来是「持仓 147.4%」。
       我们统计到的是全部持有人的**一个子集**,它绝不可能超过总供应量 ——
       超了就说明分母不是我们以为的那个数。
    ⚠️ 一条一眼假的信息比没有这一行糟得多:宁可不报占比。
    """

    def test_持仓比供应量还多时两个占比一起消失(self):
        from src.pumpchips import fetch_chips

        rows = [_pos(UID_1000X, held=6e8), _pos("x", held=6e8)]   # 合计 1.2e9 > 1e9
        c = FakePump({0: _page(2, rows)}, _coin())
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"})
        assert ch.bad_supply is True
        assert ch.plat_pct is None
        assert ch.watch_pct is None, "两个占比共用同一个分母,不能只掉一个"
        assert ch.total == 2, "人数不受影响"
        assert len(ch.matched) == 1, "名单命中不受影响"

    def test_恰好等于供应量仍然算可信(self):
        """⚠️ 一个人持有全部供应量是真实可能的(刚发出来的币),那不是矛盾"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, held=1e9)])}, _coin())
        ch = fetch_chips(c, CA_CAP, {})
        assert ch.bad_supply is False
        assert ch.plat_pct == pytest.approx(100.0)

    def test_没拿到分母时不算矛盾(self):
        """⚠️ "没拿到"与"对不上"是两件事,文案不同"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, held=1.0)])}, None)
        ch = fetch_chips(c, CA_CAP, {})
        assert ch.bad_supply is False
        assert ch.plat_pct is None

    @pytest.mark.parametrize(("amount", "supply", "ok"), [
        (1.0, 2.0, True), (2.0, 2.0, True), (2.000001, 2.0, False),
        (None, 2.0, False), (1.0, None, False), (1.0, 0.0, False), (1.0, -1.0, False),
    ])
    def test_判据本身(self, amount, supply, ok):
        from src.pumpchips import supply_usable

        assert supply_usable(amount, supply) is ok

    def test_一条明细都没有时判不了分母不算矛盾(self):
        """
        ⚠️⚠️ 真网络实测打出来的:SHFL(平台 0 人、明细恒空)的 supply 拿得到、
           分子是 None,少了这一半守卫会把它误判成"分母对不上"。
           **判不了**与**对不上**是两件事。
        """
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(116, [])}, _coin())
        ch = fetch_chips(c, CA_CAP, {})
        assert ch.bad_supply is False
        assert ch.covered == 0
        assert ch.total == 116


class Test分母超预算:
    """
    ⚠️⚠️ 变异测试打出来的空白:分母那一路撞墙钟预算时,`fut_supply.result(timeout=…)`
       会抛 TimeoutError,而那条 except 分支**一条用例都没走过** —— 把它改成
       `return None`(= 三段合成一个 try:分母超时就让 💊 整块消失)时全量 4103 条**全绿**。
       现在这两条把它钉住。
    """

    def test_分母超预算只掉占比整块照出(self):
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, held=5e7)])}, _coin(), coin_delay=0.4)
        ch = fetch_chips(c, CA_CAP, {UID_1000X: "1000XCryptoD"},
                         workers=2, budget_sec=0.05)
        assert ch is not None, "分母超时把整块带走了 —— 三段就不再是各自独立的"
        assert ch.total == 1 and ch.covered == 1
        assert ch.plat_pct is None and ch.watch_pct is None
        assert [m["name"] for m in ch.matched] == ["1000XCryptoD"], "名单命中不依赖分母"

    def test_分母超预算时也不许被当成分母自相矛盾(self):
        """⚠️ 「没在预算内拿到」与「拿到了但对不上」是两件事,文案不同"""
        from src.pumpchips import fetch_chips

        c = FakePump({0: _page(1, [_pos(UID_1000X, held=5e7)])}, _coin(), coin_delay=0.4)
        ch = fetch_chips(c, CA_CAP, {}, workers=2, budget_sec=0.05)
        assert ch.bad_supply is False
