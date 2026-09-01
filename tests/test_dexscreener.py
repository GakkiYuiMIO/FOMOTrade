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

# ---- 用户报的那个 bug:$Rabbit 的底池是 WYFI,不是 ETH -------------------
CA_RABBIT = "0xcd1cca2b3d0a11b295c42fe765ea8f895c2d0901"
CA_WYFI = "0x9e7abd3c9139d14e4c86dce0e455aab7a0c2fb3e"
# $QUANT(Quantums):最深池对着 SPY —— ETF 也算币股
CA_QUANT = "0x29ffc36bcc7d857f325b435533041c3dd9fa26e5"
CA_SPY_RH = "0x117cc2133c37b721f49de2a7a74833232b3b4c0c"
# Robinhood 链的稳定币是 USDG(不是 USDC)
CA_USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

# dexscreener_latest_batch8.json 里问过的那 8 个 robinhood 地址(原样、原顺序)
BATCH8 = (
    "0x98096d17e191b3da1d5f99a6d7b3584351b11e18",  # BONER
    "0xd18528b39da6464b3662c331a52181ecb15b1e18",  # SAYLORMOON
    "0xfb5b5778d45ae47f15323fb59b666c655174a79c",  # HOODon
    "0x85747822cef10bd5ac453d35aa6a4f21d8571e18",  # TIM
    "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",  # NVDA
    "0x0423bed328942cb8bf79726b986893e1eb863cba",  # URMOM
    "0x690b0a11c83708941d6ecbb9934f47dce821a63e",  # RobinDex ← 一条池都没轮到
    "0x1d11f0496982706c5e14a514d4e79f2e6bde4516",  # DJT
)
CA_ROBINDEX = "0x690b0a11c83708941d6ecbb9934f47dce821a63e"

# 判据后缀,写死字面量(U+2022 BULLET,前后各一个空格)
_RH_MARK = " • Robinhood Token"


def _load(name: str):
    """夹具 → pair 数组。/latest/dex/tokens 的是 {"pairs": [...]},老端点的是裸数组。"""
    d = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return d["pairs"] if isinstance(d, dict) else d


def _pair(base_addr, quote_addr, *, base_sym="AI", quote_sym="NVDA",
          base_name="Artificial Inu", quote_name="NVIDIA • Robinhood Token",
          liq=1000.0, chain="robinhood") -> dict:
    """造一条 pair。默认是 AI/NVDA 的形状。"""
    return {
        "chainId": chain,
        "baseToken": {"address": base_addr, "name": base_name, "symbol": base_sym},
        "quoteToken": {"address": quote_addr, "name": quote_name, "symbol": quote_sym},
        "liquidity": {"usd": liq},
    }


class FakeDex:
    """
    离线的 DexScreener 客户端。

    ⚠️ 契约与真 client 一致:fetch_pairs **不抛异常**、失败返回 None、
       "查到了但一个池都没有"返回 []。`boom` 是**故意违约**,用来验证上层顶得住。

    两种喂法:
      responses  按调用顺序弹(弹空之后一律 None = 请求失败)
      by_addr    {地址: pair 数组};一次请求带几个地址就把它们的 pair 拼起来 ——
                 这才是真端点的形状(多地址响应里几个币的池子混在同一个数组里)
    """

    def __init__(self, responses=None, *, boom: Exception | None = None,
                 by_addr: dict | None = None) -> None:
        self._responses = list(responses or [])
        self._by_addr = dict(by_addr or {})
        self._use_by_addr = by_addr is not None
        self.boom = boom
        self.calls: list[tuple[str, ...]] = []
        self.closed = 0

    def fetch_pairs(self, addresses):
        self.calls.append(tuple(addresses))
        if self.boom is not None:
            raise self.boom
        if self._use_by_addr:
            out = []
            for a in addresses:
                out.extend(self._by_addr.get(a) or [])
            return out
        return self._responses.pop(0) if self._responses else None

    def close(self) -> None:
        self.closed += 1


# ============================================================
# ⚠️⚠️ 本次修复的主角:必须取**流动性最深**的那个池
# ============================================================
class Test取最深的池:
    def test_Rabbit的底池是WYFI而不是ETH(self):
        """
        ⚠️⚠️ 用户报的那个 bug。旧实现用 `/tokens/v1` 批量端点,注释里写着
           「每个 token 返回最深的那一条」—— 实测它给的是 ETH($31,069),
           而真正最深的是 WYFI($466,663),差 15 倍。ETH 是计价资产会被藏掉,
           于是这一行**整条消失**,用户什么都看不到。

        夹具是那次真实响应原样存下来的:30 条 pair,ETH 那条恰好排在**第一个**。
        所以"取第一条"和"取最深"在这份数据上结论完全不同 —— 这正是它值钱的地方。
        """
        pairs = _load("dexscreener_latest_rabbit.json")
        assert pairs[0]["quoteToken"]["symbol"] == "ETH", "夹具变了:第一条不再是 ETH"

        got = dx.parse_pool_quotes(pairs, "robinhood", {CA_RABBIT})
        assert got[CA_RABBIT].symbol == "WYFI", "取的不是最深的那个池"
        assert got[CA_RABBIT].address == CA_WYFI

    def test_最深的判据是流动性而不是数组顺序(self):
        """
        ⚠️ 响应里的顺序**不是**按深度排的(实测每一批都有逆序点)。
           把 `liq > best` 换成"后来的覆盖先来的"或"先来的赢",在真实数据上
           都会挑到别的池子。
        """
        pairs = _load("dexscreener_latest_rabbit.json")
        deepest = max(pairs, key=lambda p: p["liquidity"]["usd"])
        assert deepest["quoteToken"]["symbol"] == "WYFI"
        a = dx.parse_pool_quotes(pairs, "robinhood", {CA_RABBIT})[CA_RABBIT]
        b = dx.parse_pool_quotes(list(reversed(pairs)), "robinhood", {CA_RABBIT})[CA_RABBIT]
        assert a.address == b.address == CA_WYFI, "答案跟着数组顺序变了"

    def test_取最深之后仍然要判是哪一侧(self):
        """
        ⚠️⚠️ 两件事要一起成立:**先挑最深的那条,再在那条里判哪一侧是我们的币**。
           另一位验证者在 120 个真实币里实测到 20 次"被查的币在 quoteToken 侧"。
           少了侧判,最深的那条会把**这个币自己**当成它的对手报出来。
        """
        deep = _pair("0x" + "c" * 40, CA_AI_CHECKSUM, base_sym="CLANKER",
                     base_name="Clanker", quote_sym="AI", quote_name="Artificial Inu",
                     liq=999.0)
        shallow = _pair(CA_AI_CHECKSUM, CA_NVDA_RH, liq=1.0)
        pq = dx.parse_pool_quotes([shallow, deep], "robinhood", {CA_AI})[CA_AI]
        assert pq.address != CA_AI, "把这个币自己当成了它的对手"
        assert pq.symbol == "CLANKER"

    def test_深度取不到时排最后(self):
        """「深度未知」没有资格盖过一个已知的深池。"""
        known = _pair(CA_AI_CHECKSUM, "0x" + "b" * 40, quote_sym="DEEP",
                      quote_name="d", liq=5.0)
        unknown = _pair(CA_AI_CHECKSUM, "0x" + "a" * 40, quote_sym="NOLIQ",
                        quote_name="n")
        unknown["liquidity"] = None
        for order in ([known, unknown], [unknown, known]):
            assert dx.parse_pool_quotes(order, "robinhood", {CA_AI})[CA_AI].symbol == "DEEP"


# ============================================================
# ⚠️⚠️ 截断:整个响应最多 30 条 pair,**不是每个币 30 条**
# ============================================================
class Test截断:
    def test_八个地址一次问只覆盖到七个币(self):
        """
        实测数据(2026-09-01):传 8 个 robinhood 地址 → 回 **30 条** pair →
        其中一个币(RobinDex)**一条池都没轮到**。
        """
        pairs = _load("dexscreener_latest_batch8.json")
        assert len(pairs) == 30, "夹具变了:不再是顶格的 30 条"
        covered = dx.covered_keys(pairs, "robinhood", set(BATCH8))
        assert len(covered) == 7
        assert CA_ROBINDEX not in covered, "没覆盖到的那个币被当成覆盖到了"

    def test_顶到上限就算被截了(self):
        """⚠️ 判 `>=` 不判 `==`:上游哪天把上限调到 50,`== 30` 会永远判成"没截"。"""
        assert dx.is_truncated(_load("dexscreener_latest_batch8.json")) is True
        assert dx.is_truncated([{}] * 30) is True
        assert dx.is_truncated([{}] * 31) is True
        assert dx.is_truncated([{}] * 29) is False
        assert dx.is_truncated([]) is False

    def test_没顶到上限的响应不算被截(self):
        """$QUANT 实测回 27 条 —— 27 那条同时证明 30 是**上限**不是固定页大小。"""
        pairs = _load("dexscreener_latest_quant.json")
        assert len(pairs) == 27
        assert dx.is_truncated(pairs) is False

    def test_多地址响应被截时整片作废逐个补查(self):
        """
        ⚠️⚠️ 这是本模块的安全带。被截的多地址响应里,**谁的最深池都可能已经被切掉**
           (顺序不按深度排,30 条要被几个币分),所以"这个币被覆盖到了"完全
           不等于"它的答案是对的"。

        这里让批量响应给出批量能给的答案,单地址响应给出更深的那个 ——
        闸门失灵的话拿到的就是批量那份:读起来完全正常,错得毫无痕迹。
        """
        a, b = BATCH8[0], BATCH8[1]
        batch = _load("dexscreener_latest_batch8.json")
        assert len(batch) == 30
        single = {
            a: [_pair(a, CA_WYFI, base_sym="BONER", base_name="Boner Coin",
                      quote_sym="WYFI", quote_name="WhiteFiber, Inc." + _RH_MARK,
                      liq=9_000_000.0)],
            b: [_pair(b, CA_NVDA_RH, base_sym="SAYLORMOON", base_name="SAYLORMOON",
                      liq=8_000_000.0)],
        }

        class Seq(FakeDex):
            def fetch_pairs(self, addresses):
                self.calls.append(tuple(addresses))
                if len(addresses) > 1:
                    return batch
                return single.get(addresses[0], [])

        fake = Seq()
        got = dx.PoolQuoteLookup(client=fake, addrs_per_request=8).lookup(
            "robinhood", list(BATCH8))

        assert len(fake.calls[0]) == 8, "第一片本来就该是 8 个地址"
        assert [len(c) for c in fake.calls[1:]] == [1] * 8, "被截之后没有逐个补查"
        assert got[a].symbol == "WYFI", "批量那份被截的答案漏进来了"
        assert got[b].symbol == "NVDA"

    def test_没被截的多地址响应直接采信(self):
        """没顶到上限 = 上游把这一片的池子全给了,不必再逐个补查一遍。"""
        a, b = BATCH8[0], BATCH8[1]
        small = [_pair(a, CA_NVDA_RH, base_sym="BONER", base_name="Boner", liq=5.0),
                 _pair(b, CA_NVDA_RH, base_sym="SAYLORMOON", base_name="SM", liq=6.0)]
        fake = FakeDex([small])
        got = dx.PoolQuoteLookup(client=fake, addrs_per_request=8).lookup(
            "robinhood", [a, b])
        assert len(fake.calls) == 1, "没被截却还去逐个补查了"
        assert set(got) == {a, b}

    def test_单地址仍被截时收下已有的最深池(self):
        """
        上游给不了更多了(实测 Rabbit / AI / CASHCAT 单独问也正好 30 条)。
        这时**必须收下**,否则 $Rabbit 这种最该显示的币反而永远不显示。
        """
        fake = FakeDex(by_addr={CA_RABBIT: _load("dexscreener_latest_rabbit.json")})
        got = dx.PoolQuoteLookup(client=fake).lookup("robinhood", [CA_RABBIT])
        assert fake.calls == [(CA_RABBIT,)], "单地址被截时不该再补查"
        assert got[CA_RABBIT].symbol == "WYFI"

    def test_默认一次只带一个地址(self):
        """
        ⚠️ 生产默认 1:多地址请求里 30 条额度要被几个币分,而顺序不按深度排 ——
           省下的请求换来的是一批"看起来正常的错答案"。
        """
        fake = FakeDex(by_addr={})
        dx.PoolQuoteLookup(client=fake).lookup(
            "robinhood", [CA_AI, CA_CASHCAT, CA_STONKBROKER])
        assert [len(c) for c in fake.calls] == [1, 1, 1]
        assert [c[0] for c in fake.calls] == [CA_AI, CA_CASHCAT, CA_STONKBROKER]


# ============================================================
# ⚠️⚠️ 链过滤:/latest/dex/tokens 不带链,响应是跨链的
# ============================================================
class Test链过滤:
    def test_别的链的pair一律丢掉(self):
        """
        ⚠️⚠️ 实测同一个 EVM 地址会一次回 ethereum / bsc / pulsechain 三条链的 pair。
           不按 chainId 过滤 = 可能拿**另一条链**的池子当本链最深的池:
           金额、符号全都对得上,只有链错了,肉眼看不出来。
        """
        mine = _pair(CA_AI_CHECKSUM, CA_NVDA_RH, liq=10.0, chain="robinhood")
        alien = _pair(CA_AI_CHECKSUM, "0x" + "e" * 40, quote_sym="ALIEN",
                      quote_name="Alien", liq=9_999_999.0, chain="pulsechain")
        got = dx.parse_pool_quotes([alien, mine], "robinhood", {CA_AI})
        assert got[CA_AI].symbol == "NVDA", "把别的链的池子当成了最深的池"

    def test_四条链的chainId都是实测过的值(self):
        """
        ⚠️⚠️ 写错一个字母的后果:该链每一条 pair 都被过滤掉 → 每条推送静默少一行,
           日志里一个字都没有。**绝不能**复用 models.NETWORK_SLUG(那里 bsc 是 `bnb`)。
        """
        for net, chain_id in (("solana", "solana"), ("bsc", "bsc"),
                              ("base", "base"), ("robinhood", "robinhood")):
            p = _pair("0x" + "1" * 40, "0x" + "2" * 40, chain=chain_id)
            assert dx.parse_pool_quotes([p], net, {"0x" + "1" * 40}), \
                f"{net} 的 chainId 不是实测过的那个"

    def test_没映射的链一条都认不出来(self):
        p = _pair("0x" + "1" * 40, "0x" + "2" * 40, chain="ethereum")
        assert dx.parse_pool_quotes([p], "ethereum", {"0x" + "1" * 40}) == {}
        assert dx.covered_keys([p], "ethereum", {"0x" + "1" * 40}) == set()


# ============================================================
# ⚠️⚠️ 币股判据:该显示的显示,不是股票的绝不说成股票
# ============================================================
class Test币股判据:
    def _one(self, quote_name, *, net="robinhood", quote_sym="ZZZ"):
        p = _pair("0x" + "1" * 40, "0x" + "9" * 40, quote_sym=quote_sym,
                  quote_name=quote_name, chain=net)
        return dx.parse_pool_quotes([p], net, {"0x" + "1" * 40})["0x" + "1" * 40]

    def test_个股要显示并带公司名(self):
        pq = self._one("NVIDIA" + _RH_MARK, quote_sym="NVDA")
        assert pq.is_stock is True
        assert pq.issuer == "NVIDIA"

    def test_ETF也算(self):
        """用户口径:这不是纯美股,是**币股** —— ETF、杠杆产品、未上市公司都算。"""
        pq = self._one("SPDR S&P 500 ETF Trust" + _RH_MARK, quote_sym="SPY")
        assert pq.is_stock is True
        assert pq.issuer == "SPDR S&P 500 ETF Trust"

    def test_未上市公司也算(self):
        pq = self._one("Space Exploration Technologies Corp. Class A Common Stock" + _RH_MARK,
                       quote_sym="SPCX")
        assert pq.is_stock is True
        assert pq.issuer.startswith("Space Exploration Technologies")

    def test_加密原生资产绝不能被判成股票(self):
        """
        ⚠️⚠️ 把 WETH / ETH / USDG 说成"底池是股票"是**假事实**。
           实测这三个在 Robinhood 链上都**不带**判据后缀 —— 判据靠的就是这一点。
        """
        for sym, name in (("WETH", "WETH"), ("ETH", "Ether"), ("USDG", "Global Dollar")):
            pq = self._one(name, quote_sym=sym)
            assert pq.is_stock is False, f"{sym} 被判成了股票"
            assert pq.issuer is None

    def test_碰瓷公司名的memecoin不算(self):
        """
        ⚠️⚠️ 同一条链上真实存在 `GME · GameStop`(memecoin,0xc2362aff…)、
           `GPRO · GoPro Inc`、`TIM · Tim Apple`。判据要是"名字里像公司名就算",
           这三个全会被说成股票。
        """
        for sym, name in (("GME", "GameStop"), ("GPRO", "GoPro Inc"),
                          ("TIM", "Tim Apple"), ("NVDAc", "NVIDIA Corporation"),
                          ("SAYLORMOON", "SAYLORMOON")):
            assert self._one(name, quote_sym=sym).is_stock is False, f"{name} 被误收"

    def test_名字里有Robinhood但不是这个后缀的不算(self):
        """
        ⚠️ 必须 endswith 不是 in:`Robinhood Markets (Ondo Tokenized)` 是别家(Ondo)
           的产品,只是名字里有 "Robinhood" 三个字。
        """
        assert self._one("Robinhood Markets (Ondo Tokenized)", quote_sym="HOODon").is_stock \
            is False

    def test_后缀不在结尾不算(self):
        assert self._one("NVIDIA" + _RH_MARK + " v2").is_stock is False

    def test_别的链一律判不出来(self):
        """
        ⚠️⚠️ base / bsc / solana 上**没有找到可靠标记**(见 STOCK_NAME_MARKERS 的调研
           记录),那几条链一律不显示。宁可漏,不可编。
        """
        for net, sym, name in (("base", "NVDAc", "NVIDIA Corporation"),
                               ("bsc", "SPCXB", "SpaceX"),
                               ("solana", "SPYx", "SP500 xStock")):
            assert self._one(name, net=net, quote_sym=sym).is_stock is False, \
                f"{net} 上没有判据却判出来了"

    def test_别的链连Robinhood后缀也不认(self):
        """判据是**按链**定义的,不是全局的字符串规则。"""
        assert self._one("NVIDIA" + _RH_MARK, net="base", quote_sym="NVDA").is_stock is False

    def test_公司名剥不出来时只留符号(self):
        """
        ⚠️ 缺了就少那半句,绝不编一个名字,也绝不打占位符。
        ⚠️ 名字先过"叠平空白",所以一个只剩后缀的名字长这样(前导空格已经没了)——
           判据要是写成带前导空格的 " • Robinhood Token",这条路永远走不到。
        """
        pq = self._one("• Robinhood Token", quote_sym="XX")
        assert pq.is_stock is True
        assert pq.issuer is None

    def test_公司名里的空白被叠平(self):
        pq = self._one("  White\tFiber,   Inc.  " + _RH_MARK, quote_sym="WYFI")
        assert pq.issuer == "White Fiber, Inc."

    def test_原始name仍然留着(self):
        """排查时要看得到上游原文,所以 name 不被就地改掉。"""
        assert self._one("NVIDIA" + _RH_MARK, quote_sym="NVDA").name == \
            "NVIDIA • Robinhood Token"


# ============================================================
# notable:唯一的「要不要占一行」判定点
# ============================================================
class Test要不要占一行:
    def _quotes(self, fixture: str, net: str, wanted: set[str]):
        return dx.parse_pool_quotes(_load(fixture), net, wanted)

    def test_Rabbit渲染出WYFI和公司名(self):
        """可证伪的验收标准 ①。"""
        q = self._quotes("dexscreener_latest_rabbit.json", "robinhood", {CA_RABBIT})
        pq = dx.notable(q, CA_RABBIT)
        assert pq is not None
        assert pq.symbol == "WYFI"
        assert pq.name == "WhiteFiber, Inc.", "公司名没提出来 / 判据后缀没剥掉"

    def test_对手是SPY的币要显示_ETF也算(self):
        """可证伪的验收标准 ②。"""
        q = self._quotes("dexscreener_latest_quant.json", "robinhood", {CA_QUANT})
        pq = dx.notable(q, CA_QUANT)
        assert pq is not None
        assert pq.symbol == "SPY"
        assert pq.name == "SPDR S&P 500 ETF Trust"
        assert pq.address == CA_SPY_RH

    def test_AI渲染出NVDA(self):
        """可证伪的验收标准 ③。"""
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_AI})
        pq = dx.notable(q, CA_AI)
        assert pq is not None and pq.symbol == "NVDA"
        assert pq.name == "NVIDIA"

    def test_对手是robinhood的WETH要藏起来(self):
        """可证伪的验收标准 ④。"""
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_CASHCAT})
        assert q[CA_CASHCAT].symbol == "WETH"
        assert dx.notable(q, CA_CASHCAT) is None

    def test_对手是原生币占位地址要藏起来(self):
        """
        ⚠️⚠️ 实测 Robinhood 链上 Uniswap V4 的原生 ETH 就是
           `0x0000000000000000000000000000000000000000`。
        """
        q = self._quotes("dexscreener_robinhood.json", "robinhood", {CA_STONKBROKER})
        assert q[CA_STONKBROKER].address == "0x" + "0" * 40
        assert dx.notable(q, CA_STONKBROKER) is None

    def test_对手是USDG要藏起来(self):
        p = _pair(CA_AI_CHECKSUM, CA_USDG, quote_sym="USDG", quote_name="Global Dollar")
        q = dx.parse_pool_quotes([p], "robinhood", {CA_AI})
        assert dx.notable(q, CA_AI) is None

    def test_对手是WSOL要藏起来(self):
        q = self._quotes("dexscreener_solana.json", "solana", {CA_CATE})
        assert q[CA_CATE].symbol == "SOL", "夹具里对手确实是 WSOL"
        assert dx.notable(q, CA_CATE) is None

    def test_对手是另一个memecoin不显示(self):
        """用户口径:「不是股票底池的可以不带」。"""
        p = _pair("0x" + "1" * 40, "0x" + "2" * 40, quote_sym="AI",
                  quote_name="Artificial Inu")
        q = dx.parse_pool_quotes([p], "robinhood", {"0x" + "1" * 40})
        assert q["0x" + "1" * 40].is_common is False, "它确实不是计价币"
        assert dx.notable(q, "0x" + "1" * 40) is None, "memecoin 对手不该占一行"

    def test_计价币这道门单独存在(self):
        """
        ⚠️ 假如上游哪天把稳定币也发成 `USDG • Robinhood Token`,币股那道门会放行,
           这时挡住它的就是 is_common 这道门。
        """
        p = _pair(CA_AI_CHECKSUM, CA_USDG, quote_sym="USDG",
                  quote_name="Global Dollar" + _RH_MARK)
        q = dx.parse_pool_quotes([p], "robinhood", {CA_AI})
        assert q[CA_AI].is_stock is True, "前提:它确实过了币股那道门"
        assert dx.notable(q, CA_AI) is None, "计价币那道门没挡住"

    def test_按地址判而不是按symbol_假USDC必须显示(self):
        """
        ⚠️⚠️ 链上假 USDC 遍地。按符号判会把一个自称 "USDC" 的骗子币**藏起来** ——
           而那恰恰是最该报出来的一条。
        """
        pair = _pair(CA_AI_CHECKSUM, "0x" + "9" * 40, quote_sym="USDC",
                     quote_name="USD Coin" + _RH_MARK)
        q = dx.parse_pool_quotes([pair], "robinhood", {CA_AI})
        assert q[CA_AI].is_common is False, "按 symbol 判把假 USDC 当成计价币了"
        assert dx.notable(q, CA_AI) is not None

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
           不归一化就一条都匹配不上 —— 四条链的原生币/稳定币会**全部**逃过这道门。
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
        """
        q = dx.parse_pool_quotes(
            [_pair(CA_AI_CHECKSUM, "0x" + "9" * 40, quote_sym="ZZZ",
                   quote_name="Zed" + _RH_MARK)], "robinhood", {CA_AI})
        assert set(q) == {CA_AI}, "结果的键必须是归一化后的形态"
        assert dx.notable(q, CA_AI_CHECKSUM) is not None, "checksum 形态的 CA 查不到"
        assert dx.notable(q, "  " + CA_AI + "  ") is not None, "带空白的 CA 查不到"

    def test_查不到的币notable给None(self):
        assert dx.notable({}, CA_AI) is None
        assert dx.notable({}, None) is None


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
        pair = _pair("0x" + "c" * 40, CA_AI_CHECKSUM,
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
           整个功能对 EVM 链**静默失效**。
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
# 发不发请求
# ============================================================
class Test请求闸门:
    def test_没有币股判据的链一个请求都不发(self):
        """
        ⚠️⚠️ 那条链上这一行**永远显示不出来**(notable 一律给 None),
           为一个必然不显示的东西打一轮请求是纯浪费。
           加判据的那天请求会自动跟着回来 —— 两件事共用同一张表,不会 drift。
        """
        for net in ("solana", "bsc", "base"):
            fake = FakeDex([[_pair(CA_AI_CHECKSUM, CA_NVDA_RH)]])
            assert dx.PoolQuoteLookup(client=fake).lookup(net, [CA_AI]) == {}
            assert fake.calls == [], f"{net} 上白打了请求"

    def test_映射不到的链一个请求都不发(self):
        fake = FakeDex([[_pair(CA_AI_CHECKSUM, CA_NVDA_RH)]])
        assert dx.PoolQuoteLookup(client=fake).lookup("ethereum", [CA_AI]) == {}
        assert fake.calls == [], "对没映射的链发了请求"

    def test_链为空同样不发请求(self):
        fake = FakeDex()
        assert dx.PoolQuoteLookup(client=fake).lookup(None, [CA_AI]) == {}
        assert dx.PoolQuoteLookup(client=fake).lookup("", [CA_AI]) == {}
        assert fake.calls == []

    def test_同一轮同一个币只查一次(self):
        """
        大小写不同、重复出现,都必须折成同一个地址。

        ⚠️ 这里的"不同大小写"用的是 **checksum 形态** —— 那是 EVM 世界里真实存在的
           唯一变体,也是 normalize_token_address 认的那种。
        """
        fake = FakeDex(by_addr={CA_AI: _load("dexscreener_robinhood.json")})
        dx.PoolQuoteLookup(client=fake).lookup(
            "robinhood", [CA_AI, CA_AI_CHECKSUM, CA_AI, CA_AI_CHECKSUM])
        assert fake.calls == [(CA_AI,)], "同一个币被问了不止一次"

    def test_空地址列表不发请求(self):
        fake = FakeDex()
        assert dx.PoolQuoteLookup(client=fake).lookup("robinhood", []) == {}
        assert dx.PoolQuoteLookup(client=fake).lookup("robinhood", [None, "", "  "]) == {}
        assert fake.calls == []

    def test_没问过的地址一律丢弃(self):
        """上游多给的东西不该悄悄流进推送。"""
        got = dx.parse_pool_quotes(_load("dexscreener_robinhood.json"),
                                   "robinhood", {CA_AI})
        assert set(got) == {CA_AI}


class Test缓存:
    def test_第二次不再发请求(self):
        fake = FakeDex(by_addr={CA_AI: _load("dexscreener_robinhood.json")})
        lk = dx.PoolQuoteLookup(client=fake)
        first = lk.lookup("robinhood", [CA_AI])
        second = lk.lookup("robinhood", [CA_AI])
        assert len(fake.calls) == 1, "缓存没生效"
        assert first[CA_AI].symbol == "NVDA"
        assert second[CA_AI].symbol == "NVDA"

    def test_TTL过期后重新查(self):
        fake = FakeDex(by_addr={CA_AI: _load("dexscreener_robinhood.json")})
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
        """200 + 空数组(比如新币还没建池)同样重问 —— 它几分钟后就可能建池。"""
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
            lk._cache[("robinhood", f"0x{i:040x}")] = (now, dx.PoolQuote("x", "X", "X", False))
        lk.lookup("robinhood", ["0x" + "e" * 40])
        assert len(lk._cache) <= 2000


# ============================================================
# HTTP 客户端:端点与响应解包
# ============================================================
class _Resp:
    def __init__(self, status, body, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class _UrlProbe(dx.DexScreenerClient):
    """
    只为看"请求打到哪个 URL"。

    ⚠️ 故意不调用父类 __init__ —— 那会去读配置。这里只想钉端点。
    ⚠️⚠️ conftest 有一道兜底防线,把 DexScreenerClient.fetch_pairs 整个桩成"请求失败"
       (不许任何一条用例真的打网络)。而这里要测的**正是那个真方法**,
       所以在子类上把它绑回来 —— 这一句在 import 时求值,那会儿桩还没打上去。
    """

    fetch_pairs = dx.DexScreenerClient.fetch_pairs

    def __init__(self, resp=None, raises=None):
        self.urls: list[str] = []
        self._resp = resp
        self._raises = raises
        self.closed = 0

    def _session(self):
        return self

    def get(self, url):
        self.urls.append(url)
        if self._raises is not None:
            raise self._raises
        return self._resp

    def close(self):
        self.closed += 1


class Test端点:
    def test_打的是latest_dex_tokens而不是tokens_v1(self):
        """
        ⚠️⚠️ 换端点是本次修复的根:`/tokens/v1/{chain}/{addrs}` 每个币只回**一条**,
           而那一条**不是最深的**($Rabbit 实测回 ETH $31,069,真最深是 WYFI $466,663)。
           URL 写死字面量 —— 退回旧端点时,离线测试里只有这一条能发现。
        """
        c = _UrlProbe(_Resp(200, {"pairs": []}))
        c.fetch_pairs([CA_RABBIT])
        assert c.urls == [
            "https://api.dexscreener.com/latest/dex/tokens/"
            "0xcd1cca2b3d0a11b295c42fe765ea8f895c2d0901"]

    def test_多个地址用逗号拼(self):
        c = _UrlProbe(_Resp(200, {"pairs": []}))
        c.fetch_pairs([CA_AI, CA_CASHCAT])
        assert c.urls == [
            "https://api.dexscreener.com/latest/dex/tokens/"
            "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18,"
            "0x020bfc650a365f8bb26819deaabf3e21291018b4"]
        assert "/tokens/v1/" not in c.urls[0], "退回批量端点了"

    def test_解包pairs字段(self):
        p = _pair(CA_AI_CHECKSUM, CA_NVDA_RH)
        assert _UrlProbe(_Resp(200, {"pairs": [p]})).fetch_pairs([CA_AI]) == [p]

    def test_查不到时pairs是null要当成空数组(self):
        """
        ⚠️ 上游查不到给的是 `{"pairs": null}` 不是 `[]`。
           不统一成 [] 的话它会和"请求失败"(None)混成一件事 ——
           一次网络抖动就会被当成"这个币确实没有池子"。
        """
        assert _UrlProbe(_Resp(200, {"pairs": None})).fetch_pairs([CA_AI]) == []

    def test_失败一律None而不是空数组(self):
        """⚠️ None(失败)与 [](确定的空答案)必须分开 —— 只有前者不许入缓存。"""
        assert _UrlProbe(_Resp(429, {})).fetch_pairs([CA_AI]) is None
        assert _UrlProbe(_Resp(200, None, bad_json=True)).fetch_pairs([CA_AI]) is None
        assert _UrlProbe(_Resp(200, "oops")).fetch_pairs([CA_AI]) is None
        assert _UrlProbe(raises=OSError("boom")).fetch_pairs([CA_AI]) is None

    def test_网络异常时把连接池丢掉重建(self):
        """连接可能已经废了,留着它下一轮还是废的。"""
        c = _UrlProbe(raises=OSError("boom"))
        c.fetch_pairs([CA_AI])
        assert c.closed == 1

    def test_没有地址时不发请求(self):
        c = _UrlProbe(_Resp(200, {"pairs": []}))
        assert c.fetch_pairs([]) is None
        assert c.urls == []


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
            assert dx.covered_keys(bad, "robinhood", {CA_AI}) == set()
            assert dx.is_truncated(bad) is False

    def test_pair缺字段不炸(self):
        assert dx.parse_pool_quote({}, "robinhood", CA_AI) is None
        assert dx.parse_pool_quote({"baseToken": "x"}, "robinhood", CA_AI) is None
        assert dx.parse_pool_quote(None, "robinhood", CA_AI) is None

    def test_close转发给客户端(self):
        fake = FakeDex()
        dx.PoolQuoteLookup(client=fake).close()
        assert fake.closed == 1
