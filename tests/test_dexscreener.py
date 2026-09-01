"""
底池对手资产(DexScreener)的测试。

⚠️ 全部走**离线夹具**(tests/fixtures/dexscreener_*.json,是 2026-09-01 的真实响应
   原样存下来的),一条用例都不打网络 —— 每跑一次测试就打一次公开接口
   是在拿用户的出口 IP 冒险。
⚠️ 断言里的上限/slug/地址一律**写死字面量**,绝不从被测模块 import 过来 ——
   那种断言等价于 `x == x`,把常量改坏了它照样绿(本项目踩过)。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。ruff 的 N802 只认 ASCII 小写。
from __future__ import annotations

import json
from pathlib import Path

from src import dexscreener as dx

FIXTURES = Path(__file__).parent / "fixtures"

# ---- 真实地址,全部写死字面量 --------------------------------------------
# $AI(Robinhood 链)。⚠️ 库里存的是**小写**,而 DexScreener 返回 checksum 大小写
CA_AI = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
CA_AI_CHECKSUM = "0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18"
# 它的底池对手:代币化的英伟达股票
CA_NVDA_RH = "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
# $CASHCAT:对手是 Robinhood 链上的 WETH(常见计价资产 → 不该显示)
CA_CASHCAT = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
# $STONKBROKER:对手是 Uniswap V4 的**原生 ETH 占位地址**(同样不该显示)
CA_STONKBROKER = "0xe934e36a439c94017b64a3fece66af12099abf50"
# $CATE(Solana):对手是 WSOL
CA_CATE = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"
CA_WSOL = "So11111111111111111111111111111111111111112"
# BSC 的 WBNB —— checksum 形态,用来钉"计价币判定也必须大小写不敏感"
CA_WBNB_CHECKSUM = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _pair(base_addr, quote_addr, *, base_sym="AI", quote_sym="NVDA",
          base_name="Artificial Inu", quote_name="NVIDIA • Robinhood Token",
          liq=1000.0) -> dict:
    """造一条 pair。默认是 AI/NVDA 的形状。"""
    return {
        "chainId": "robinhood",
        "baseToken": {"address": base_addr, "name": base_name, "symbol": base_sym},
        "quoteToken": {"address": quote_addr, "name": quote_name, "symbol": quote_sym},
        "liquidity": {"usd": liq},
    }


class FakeDex:
    """
    离线的 DexScreener 客户端。

    ⚠️ 契约与真 client 一致:fetch_pairs **不抛异常**、失败返回 None、
       "查到了但一个都没有"返回 []。`boom` 是**故意违约**,用来验证上层顶得住。
    """

    def __init__(self, responses=None, *, boom: Exception | None = None) -> None:
        # 每次调用按顺序弹一个;弹空之后一律 None(= 请求失败)
        self._responses = list(responses or [])
        self.boom = boom
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.closed = 0

    def fetch_pairs(self, slug, addresses):
        self.calls.append((slug, tuple(addresses)))
        if self.boom is not None:
            raise self.boom
        return self._responses.pop(0) if self._responses else None

    def close(self) -> None:
        self.closed += 1


# ============================================================
# 坑 1:base/quote 方向不固定 —— 必须判断哪一侧是我们的币
# ============================================================
class Test取哪一侧:
    def test_我们的币在base侧时取quote(self):
        """真实响应:$AI 是 base,对手 NVDA 在 quote 侧。"""
        pq = dx.parse_pool_quote(_load("dexscreener_robinhood.json")[0],
                                 "robinhood", CA_AI)
        assert pq is not None
        assert pq.symbol == "NVDA"
        assert pq.name == "NVIDIA • Robinhood Token"
        assert pq.address == CA_NVDA_RH

    def test_我们的币在quote侧时取base(self):
        """
        ⚠️⚠️ 这条是「无脑取 quoteToken」那个 bug 的唯一防线。

        同一个 $AI 在 CLANKER/AI 这类池子里是 **quote**。不判断哪一侧是我们的币,
        取回来的就是 $AI 自己 —— 一句读起来完全正常的假话:
        「$AI 的底池对手是 $AI」。
        """
        pair = _pair(CA_AI_CHECKSUM.replace(CA_AI_CHECKSUM, "0x" + "c" * 40), CA_AI_CHECKSUM,
                     base_sym="CLANKER", base_name="Clanker", quote_sym="AI",
                     quote_name="Artificial Inu")
        pq = dx.parse_pool_quote(pair, "robinhood", CA_AI)
        assert pq is not None
        assert pq.address != CA_AI, "取回了这个币自己 —— 说明没判哪一侧是我们的币"
        assert pq.symbol == "CLANKER"
        assert pq.address == "0x" + "c" * 40

    def test_两侧都不是我们的币时丢弃(self):
        """上游串了数据。**绝不猜**,那一行整行消失。"""
        pair = _pair("0x" + "a" * 40, "0x" + "b" * 40)
        assert dx.parse_pool_quote(pair, "robinhood", CA_AI) is None

    def test_对手侧没有地址时丢弃(self):
        pair = _pair(CA_AI_CHECKSUM, None)
        assert dx.parse_pool_quote(pair, "robinhood", CA_AI) is None


# ============================================================
# 坑 1b:地址比较必须大小写不敏感
# ============================================================
class Test地址大小写:
    def test_库里小写而上游checksum照样认出来(self):
        """
        ⚠️⚠️ DexScreener 返回 `0x2E8c3116…`,我们库里是 `0x2e8c3116…`。
           大小写敏感地比 = 每一条 EVM 记录都判成"两侧都不是我们的币",
           整个功能对四条 EVM 链里的三条**静默失效**。
        """
        pair = _pair(CA_AI_CHECKSUM, "0x" + "F" * 40, quote_sym="ZZZ", quote_name="Zed")
        pq = dx.parse_pool_quote(pair, "robinhood", CA_AI)
        assert pq is not None and pq.symbol == "ZZZ"

    def test_我们手上是checksum而上游小写也认得出来(self):
        pair = _pair(CA_AI, "0x" + "f" * 40, quote_sym="ZZZ", quote_name="Zed")
        pq = dx.parse_pool_quote(pair, "robinhood", CA_AI_CHECKSUM)
        assert pq is not None and pq.symbol == "ZZZ"

    def test_对手地址归一化成小写(self):
        """下游要拿它去比 QUOTE_TOKENS(小写键),不归一化就一条都匹配不上。"""
        pair = _pair(CA_AI_CHECKSUM, "0x" + "F" * 40, quote_sym="ZZZ", quote_name="Zed")
        pq = dx.parse_pool_quote(pair, "robinhood", CA_AI)
        assert pq.address == "0x" + "f" * 40

    def test_Solana的base58绝不能被lower(self):
        """⚠️ Solana 地址大小写敏感,lower() 会把它改成另一个地址。"""
        pq = dx.parse_pool_quote(_load("dexscreener_solana.json")[0], "solana", CA_CATE)
        assert pq is not None
        assert pq.address == CA_WSOL, "WSOL 的 base58 被改坏了"

    def test_解析整个响应时也大小写不敏感(self):
        """wanted 集合里放小写,而响应里是 checksum —— 必须匹配得上。"""
        got = dx.parse_pool_quotes(_load("dexscreener_robinhood.json"),
                                   "robinhood", {CA_AI, CA_CASHCAT, CA_STONKBROKER})
        assert set(got) == {CA_AI, CA_CASHCAT, CA_STONKBROKER}


# ============================================================
# 坑 3:链 slug 映射
# ============================================================
class Test链slug:
    def test_四条链用的都是实测过的slug(self):
        """
        ⚠️⚠️ 字面量写死。**绝不能**复用 models.NETWORK_SLUG —— 那里 bsc 是 `bnb`,
           而实测 `GET /tokens/v1/bnb/0xfe18…7777` 返回的是
           **HTTP 200 + 空数组**(不是错误):拿错 slug 的后果是每轮白打一个请求、
           每条 BSC 推送静默少一行,日志里一个字都没有。
        """
        for net, slug in (("solana", "solana"), ("bsc", "bsc"),
                          ("base", "base"), ("robinhood", "robinhood")):
            fake = FakeDex([[]])
            dx.PoolQuoteLookup(client=fake).lookup(net, ["0x" + "a" * 40])
            assert fake.calls[0][0] == slug, f"{net} 的 slug 不是实测过的那个"

    def test_映射不到的链一个请求都不发(self):
        """
        ⚠️⚠️ 硬拼一个 slug 去试 = 白挨一次限流还什么都拿不到。
           映射不到就**整行消失**(与 GMGN 链接同一条规矩)。
        """
        fake = FakeDex([[_pair(CA_AI_CHECKSUM, CA_NVDA_RH)]])
        got = dx.PoolQuoteLookup(client=fake).lookup("ethereum", [CA_AI])
        assert got == {}
        assert fake.calls == [], "对没映射的链发了请求"

    def test_链为空同样不发请求(self):
        fake = FakeDex()
        assert dx.PoolQuoteLookup(client=fake).lookup(None, [CA_AI]) == {}
        assert dx.PoolQuoteLookup(client=fake).lookup("", [CA_AI]) == {}
        assert fake.calls == []


# ============================================================
# 常见计价资产:该藏的藏、该显示的显示
# ============================================================
class Test常见计价资产:
    def _quotes(self, fixture: str, net: str, wanted: set[str]):
        return dx.parse_pool_quotes(_load(fixture), net, wanted)

    def test_对手是NVDA要显示(self):
        """可证伪的验收标准:$AI 必须渲染出 NVDA。"""
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_AI})
        pq = dx.notable(q, CA_AI)
        assert pq is not None and pq.symbol == "NVDA"

    def test_对手是WSOL要藏起来(self):
        q = self._quotes("dexscreener_solana.json", "solana", {CA_CATE})
        assert q[CA_CATE].symbol == "SOL", "夹具里对手确实是 WSOL"
        assert dx.notable(q, CA_CATE) is None

    def test_对手是robinhood的WETH要藏起来(self):
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_CASHCAT})
        assert q[CA_CASHCAT].symbol == "WETH"
        assert dx.notable(q, CA_CASHCAT) is None

    def test_对手是原生币占位地址要藏起来(self):
        """
        ⚠️⚠️ 实测 Robinhood 链上 Uniswap V4 的原生 ETH 就是
           `0x0000000000000000000000000000000000000000`。判据里少了这一条,
           $STONKBROKER / $PACK 会各挂一行毫无信息量的「底池 · ETH」。
        """
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_STONKBROKER})
        assert q[CA_STONKBROKER].address == "0x" + "0" * 40
        assert dx.notable(q, CA_STONKBROKER) is None

    def test_按地址判而不是按symbol_假USDC必须显示(self):
        """
        ⚠️⚠️ 链上假 USDC 遍地。按符号判会把一个自称 "USDC" 的骗子币**藏起来** ——
           而那恰恰是最该报出来的一条。
        """
        pair = _pair(CA_AI_CHECKSUM, "0x" + "9" * 40, quote_sym="USDC", quote_name="USD Coin")
        q = dx.parse_pool_quotes([pair], "robinhood", {CA_AI})
        pq = dx.notable(q, CA_AI)
        assert pq is not None, "按 symbol 判把假 USDC 藏掉了"
        assert pq.address == "0x" + "9" * 40

    def test_真计价币即使符号乱写也要藏(self):
        """反过来:地址是真 WBNB,符号被上游写成别的 —— 照样该藏。"""
        pair = {"chainId": "bsc",
                "baseToken": {"address": "0x" + "1" * 40, "name": "X", "symbol": "X"},
                "quoteToken": {"address": CA_WBNB_CHECKSUM, "name": "??", "symbol": "??"},
                "liquidity": {"usd": 1.0}}
        q = dx.parse_pool_quotes([pair], "bsc", {"0x" + "1" * 40})
        assert dx.notable(q, "0x" + "1" * 40) is None

    def test_计价币判定也必须大小写不敏感(self):
        """
        ⚠️⚠️ QUOTE_TOKENS 的键是小写,DexScreener 给的是 checksum。
           不归一化就一条都匹配不上 —— 四条链的原生币/稳定币会**全部**变成噪音行,
           而且看起来"功能在正常工作"。
        """
        pair = {"chainId": "bsc",
                "baseToken": {"address": "0x" + "1" * 40, "name": "X", "symbol": "X"},
                "quoteToken": {"address": CA_WBNB_CHECKSUM, "name": "Wrapped BNB",
                               "symbol": "WBNB"},
                "liquidity": {"usd": 1.0}}
        pq = dx.parse_pool_quotes([pair], "bsc", {"0x" + "1" * 40})["0x" + "1" * 40]
        assert pq.is_common is True, "checksum 形态的 WBNB 没被认出来是计价币"

    def test_同一个地址换条链就不是计价币(self):
        """判据是 (链, 地址) 对 —— 不是光看地址。"""
        pair = {"chainId": "base",
                "baseToken": {"address": "0x" + "1" * 40, "name": "X", "symbol": "X"},
                "quoteToken": {"address": CA_WBNB_CHECKSUM, "name": "Wrapped BNB",
                               "symbol": "WBNB"},
                "liquidity": {"usd": 1.0}}
        pq = dx.parse_pool_quotes([pair], "base", {"0x" + "1" * 40})["0x" + "1" * 40]
        assert pq.is_common is False

    def test_notable接受未归一化的地址(self):
        """
        ⚠️ notable 的入参是**调用方手上的原始 CA**,不保证已归一化 ——
           它自己不归一化的话,一个 checksum 形态的 CA 会静默查不到,那一行凭空消失。
           (今天两个调用方传进来的都恰好已经是小写,所以这条**只有直接单测**能钉住;
            没有它,把 notable 里那句 normalize 删掉全套测试照样绿。)
        """
        q = dx.parse_pool_quotes(
            [_pair(CA_AI_CHECKSUM, "0x" + "9" * 40, quote_sym="ZZZ", quote_name="Zed")],
            "robinhood", {CA_AI})
        assert set(q) == {CA_AI}, "结果的键必须是归一化后的形态"
        assert dx.notable(q, CA_AI_CHECKSUM) is not None, "checksum 形态的 CA 查不到"
        assert dx.notable(q, "  " + CA_AI + "  ") is not None, "带空白的 CA 查不到"

    def test_查不到的币notable给None(self):
        assert dx.notable({}, CA_AI) is None
        assert dx.notable({}, None) is None


# ============================================================
# 批量与缓存
# ============================================================
class Test批量:
    def test_一轮多个币合并成一个请求(self):
        fake = FakeDex([_load("dexscreener_robinhood.json")])
        got = dx.PoolQuoteLookup(client=fake).lookup(
            "robinhood", [CA_AI, CA_CASHCAT, CA_STONKBROKER])
        assert len(fake.calls) == 1, "三个币打了不止一个请求"
        assert len(fake.calls[0][1]) == 3
        assert len(got) == 3

    def test_同一轮同一个币只查一次(self):
        """
        大小写不同、重复出现,都必须折成同一个地址。

        ⚠️ 这里的"不同大小写"用的是 **checksum 形态**(0x 前缀小写、hex 混合大小写)
           —— 那是 EVM 世界里真实存在的唯一变体,也是 normalize_token_address 认的那种。
           整串 upper()(连 `0X` 前缀一起)不是真实形态,不在这条不变式的射程内。
        """
        fake = FakeDex([_load("dexscreener_robinhood.json")])
        dx.PoolQuoteLookup(client=fake).lookup(
            "robinhood", [CA_AI, CA_AI_CHECKSUM, CA_AI, CA_AI_CHECKSUM])
        assert fake.calls[0][1] == (CA_AI,), "同一个币被问了不止一次"

    def test_超过30个地址切成多片(self):
        """⚠️ 30 是上游硬上限,字面量写死。"""
        fake = FakeDex([[], []])
        addrs = [f"0x{i:040x}" for i in range(35)]
        dx.PoolQuoteLookup(client=fake).lookup("base", addrs)
        assert len(fake.calls) == 2
        assert len(fake.calls[0][1]) == 30
        assert len(fake.calls[1][1]) == 5

    def test_空地址列表不发请求(self):
        fake = FakeDex()
        assert dx.PoolQuoteLookup(client=fake).lookup("base", []) == {}
        assert dx.PoolQuoteLookup(client=fake).lookup("base", [None, "", "  "]) == {}
        assert fake.calls == []

    def test_同一个币返回多条时取最深的那个池(self):
        """上游文档说只返回一条,真给多条时按 liquidity.usd 取最深 —— 不是"取最后一条"。"""
        shallow = _pair(CA_AI_CHECKSUM, "0x" + "a" * 40, quote_sym="SHALLOW",
                        quote_name="s", liq=10.0)
        deep = _pair(CA_AI_CHECKSUM, "0x" + "b" * 40, quote_sym="DEEP",
                     quote_name="d", liq=999.0)
        got = dx.parse_pool_quotes([deep, shallow], "robinhood", {CA_AI})
        assert got[CA_AI].symbol == "DEEP"

    def test_没问过的地址一律丢弃(self):
        """上游多给的东西不该悄悄流进推送。"""
        got = dx.parse_pool_quotes(_load("dexscreener_robinhood.json"),
                                   "robinhood", {CA_AI})
        assert set(got) == {CA_AI}


class Test缓存:
    def test_第二次不再发请求(self):
        fake = FakeDex([_load("dexscreener_robinhood.json")])
        lk = dx.PoolQuoteLookup(client=fake)
        first = lk.lookup("robinhood", [CA_AI])
        second = lk.lookup("robinhood", [CA_AI])
        assert len(fake.calls) == 1, "缓存没生效"
        assert first[CA_AI].symbol == "NVDA"
        assert second[CA_AI].symbol == "NVDA"

    def test_TTL过期后重新查(self):
        fake = FakeDex([_load("dexscreener_robinhood.json"),
                        _load("dexscreener_robinhood.json")])
        lk = dx.PoolQuoteLookup(client=fake, ttl=0.0)
        lk.lookup("robinhood", [CA_AI])
        lk.lookup("robinhood", [CA_AI])
        assert len(fake.calls) == 2

    def test_失败绝不入缓存(self):
        """
        ⚠️⚠️ 缓存一次失败 = 让一次网络抖动把这一行按住整整一个 TTL,
           而且没有任何日志会说"这行是被缓存按住的"。
        """
        fake = FakeDex([None, _load("dexscreener_robinhood.json")])
        lk = dx.PoolQuoteLookup(client=fake)
        assert lk.lookup("robinhood", [CA_AI]) == {}
        got = lk.lookup("robinhood", [CA_AI])
        assert len(fake.calls) == 2, "上一次失败被缓存住了"
        assert got[CA_AI].symbol == "NVDA"

    def test_查得到但没有这个币时不入缓存(self):
        """200 + 空数组(比如新币还没建池)同样重问 —— 它顺路搭在批量请求里,边际成本 0。"""
        fake = FakeDex([[], _load("dexscreener_robinhood.json")])
        lk = dx.PoolQuoteLookup(client=fake)
        assert lk.lookup("robinhood", [CA_AI]) == {}
        assert lk.lookup("robinhood", [CA_AI])[CA_AI].symbol == "NVDA"
        assert len(fake.calls) == 2

    def test_默认TTL至少一小时(self):
        """⚠️ 门槛写死字面量:底池对手是"出身"不是行情,TTL 短了纯属白挨限流。"""
        assert dx.POOL_QUOTE_TTL_SEC >= 3600

    def test_缓存不会无限长大(self):
        """TTL 长达 6 小时,过期清理几乎不触发 —— 没有上限它就只增不减。"""
        lk = dx.PoolQuoteLookup(client=FakeDex([[]]))
        import time as _t
        now = _t.time()
        for i in range(2500):
            lk._cache[("base", f"0x{i:040x}")] = (now, dx.PoolQuote("x", "X", "X", False))
        lk.lookup("base", ["0x" + "e" * 40])
        assert len(lk._cache) <= 2000


# ============================================================
# 故障隔离:这个功能没有任何理由影响别的东西
# ============================================================
class Test故障隔离:
    def test_客户端抛异常时lookup照样往上抛给调用方兜住(self):
        """
        ⚠️ 真 client 契约是"不抛";假 client 故意违约。lookup 不吞它 ——
           poller / pumpfun 那一层各自包了 try(那里才知道"降级成什么")。
        """
        lk = dx.PoolQuoteLookup(client=FakeDex(boom=RuntimeError("boom")))
        try:
            lk.lookup("robinhood", [CA_AI])
        except RuntimeError:
            pass
        else:
            raise AssertionError("异常被吞在了不该吞的层")

    def test_响应不是数组时安静地返回空(self):
        for bad in (None, {}, "oops", 123, [None, 42, "x"]):
            assert dx.parse_pool_quotes(bad, "robinhood", {CA_AI}) == {}

    def test_pair缺字段不炸(self):
        assert dx.parse_pool_quote({}, "robinhood", CA_AI) is None
        assert dx.parse_pool_quote({"baseToken": "x"}, "robinhood", CA_AI) is None
        assert dx.parse_pool_quote(None, "robinhood", CA_AI) is None

    def test_close转发给客户端(self):
        fake = FakeDex()
        dx.PoolQuoteLookup(client=fake).close()
        assert fake.closed == 1
