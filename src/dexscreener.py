"""
底池对手资产 —— 这个币最深的那个池子,对面摆的是什么。

============ 为什么值得单独做一个模块 ============
底池对着什么,决定这个币的命运绑在谁身上。绝大多数币对着原生币或稳定币
(WBNB / SOL / USDC / WETH),那是背景噪音;但 Robinhood 链上成规模地存在
「对手是代币化美股」的池子 —— 实测 $AI(0x2e8c…1e18)最深的池是
**AI / NVDA**(quoteToken.name = "NVIDIA • Robinhood Token",liq $4.55M)。
NVDA 一跌它就跟着跌,这与一个对着 BNB 的币是**两种风险**,而推送里原先一个字都没说。
同一现象在 Base 上也有(实测 $BLUECHIP 的对手是 NVDAc「NVIDIA Corporation」),
在 BSC 上也有(实测 $MarsCoin 的对手是 SPCXB「SpaceX」)。

============ ⚠️⚠️ 绝不要"优化"成链上扫描 ============
调研时先走的就是链上路线:扫 9000 个区块 / 7451 条日志找 pair 合约、读
`token0()/token1()`,结论是「$AI 的对手是 WETH($1.57M)」—— **那是系统性的错误答案**。
真正最深的 AI/NVDA 池是 **Uniswap V4 单例架构**:所有池子的资产都在同一个
PoolManager 合约里,`pairAddress` 是 **32 字节的 poolId 而不是合约地址**
(实测 0xcbdfea90…1ce27,66 个 hex 字符),对它调 `token0()` 必然 revert。
任何「找 pair 合约再读两侧」的扫描**看不见 V4 的池子**,而 Robinhood 链上
恰恰绝大多数是 V4。所以这里只走 DexScreener 的聚合数据,不读链。

============ 为什么不复用 src/client.py ============
FomoClient 绑死 prod-api.fomo.family 且携带 Privy 登录态,它抛的 AuthError 会被
cli._tick_job 捕获后 **sched.shutdown()** —— DexScreener 抖一下绝不该有权力
停掉整个监控。所以这里自建 curl_cffi 客户端,且**所有异常一律自己吞掉**。

本模块用到的端点免鉴权、免 key、只读,不碰 FOMO 的任何接口、任何凭据。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from loguru import logger

from src.config import get_settings
from src.models import is_quote_token, normalize_token_address

# ============================================================
# 端点与常量
# ============================================================
# GET /tokens/v1/{chainSlug}/{addr1,addr2,…} —— 一次最多 30 个地址,
# 每个 token 返回**一条**(该 token 流动性最深的那个池),不需要我们自己排序。
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{slug}/{addrs}"

# 一次请求最多带几个地址(上游硬上限)。超出的部分自动切片成多个请求。
MAX_ADDRS_PER_REQUEST = 30

_TIMEOUT_SEC = 15.0

# 缓存存活时间:6 小时。
# ⚠️ 敢设这么长的理由:一个币最深的池子对面是什么,是**部署时就定死**的属性 ——
#    $AI 对 NVDA、$BLUECHIP 对 NVDAc,这不是行情,是这个币的出身。真发生迁池
#    也是天级事件,而且过期值的危害很小(顶多把一个已经迁走的风险标签多挂几小时,
#    它不是数字、不会被读成"现在值多少钱")。
# ⚠️ 长 TTL 的**主职责**是把成本压到近似 0:同一个热门币一天会被推几十条,
#    6 小时的 TTL 让它一天最多问 4 次;而"同一轮同一个币只查一次"由
#    lookup() 里的批量去重直接保证,不依赖 TTL。
# ⚠️⚠️ **失败绝不入缓存**(见 lookup):缓存一次失败 = 让一次网络抖动把这一行
#    按住整整 6 小时,而且没有任何日志会说"这行是被缓存按住的"。
POOL_QUOTE_TTL_SEC = 6 * 3600.0

# 缓存条目上限。TTL 长达 6 小时,过期清理几乎不触发,而进程是长驻的 ——
# 不设上限的话这个 dict 只增不减(实测库里 robinhood 一条链就有 1267 个不同的币)。
_CACHE_MAX = 2000

# ============================================================
# 链 slug 映射 —— 本仓库内部链标识 → DexScreener 的 chainId
# ============================================================
# ⚠️⚠️ 每一条都是**实测**出来的,不是照着 network_id 猜的。核对方式:
#    GET https://api.dexscreener.com/tokens/v1/{slug}/{一个该链上的真实 CA}
#    返回非空数组、且 chainId 等于 slug,才算数。
# ⚠️⚠️ **绝不能复用 models.NETWORK_SLUG** —— 那张表是给 fomo.family 的 URL 用的,
#    里面 bsc 映射成 `bnb`。实测 `GET /tokens/v1/bnb/0xfe18…7777` 返回的是
#    **HTTP 200 + 空数组**,不是错误:拿错 slug 的后果是每一轮都白打一个请求、
#    每一条 BSC 推送都静默地少一行,而日志里一个字都不会有。
# ⚠️ 映射不到的链(ethereum / monad / hyperliquid,以及将来出现的新链)
#    **整行消失,绝不硬拼一个 slug 去试** —— 与 GMGN 链接那条同样的理由:
#    拼一个上游不认识的 slug 只会得到静默的空结果,白挨限流。
#    要加链就先按上面那条命令实测一次,再往这张表里补。
DEX_CHAIN_SLUG = {
    "solana": "solana",       # 实测 CATE/fone/CYBERLEEK → chainId=solana
    "bsc": "bsc",             # 实测 MarsCoin/XAUt/WBNB   → chainId=bsc
    "base": "base",           # 实测 BLUECHIP/Bots/WETH   → chainId=base
    "robinhood": "robinhood",  # 实测 AI/CASHCAT/PONS      → chainId=robinhood
}


# ============================================================
# 数据结构
# ============================================================
@dataclass(frozen=True)
class PoolQuote:
    """
    某个币最深的那个池子里,**对面**那个资产。

    address    对手资产的合约地址(已归一化:EVM 小写 / Solana 原样 base58)
    symbol     对手符号,如 "NVDA"
    name       对手全名,如 "NVIDIA • Robinhood Token"
    is_common  是不是常见计价资产(原生币 / 稳定币)。见 _is_common 的说明 ——
               常见的那些是背景噪音,不值得在推送里占一行。
    """

    address: str
    symbol: str | None
    name: str | None
    is_common: bool


# ============================================================
# 解析(纯函数,可脱网完整单测)
# ============================================================
def _side_key(pair, field: str) -> tuple[str | None, dict]:
    """取 pair 的某一侧,返回 (归一化地址, 该侧原始 dict)"""
    side = pair.get(field)
    if not isinstance(side, dict):
        return None, {}
    return normalize_token_address(side.get("address")), side


def _text(v) -> str | None:
    """第三方返回的短文本 → 去空白后的字符串;空一律 None(整行消失,不打 '--')"""
    s = " ".join(str(v or "").split())
    return s or None


def _liquidity_usd(pair) -> float:
    """池子深度,只用来在"同一个币返回了多条"时挑最深的那条。取不到当 0(排最后)。"""
    liq = pair.get("liquidity")
    if not isinstance(liq, dict):
        return 0.0
    try:
        return float(liq.get("usd"))
    except (TypeError, ValueError):
        return 0.0


def _is_common(network_id: str, address: str) -> bool:
    """
    对手是不是「常见计价资产」(原生币 / 稳定币)。

    ⚠️⚠️ 判据是 **(链, 地址)** 对,**绝不能用 symbol**:链上假 USDC / 假 SOL 遍地,
       按符号判会把一个自称 "USDC" 的骗子币当成计价资产**藏起来** ——
       而那恰恰是最该报出来的一条。这条与 models.QUOTE_TOKENS 的注释同一条教训。
    ⚠️⚠️ 地址必须先 normalize_token_address:DexScreener 返回的是 **checksum 大小写**
       (0xd0601CE157Db…),而 models.QUOTE_TOKENS 的键是小写。不归一化的话一条都匹配不上,
       四条链的原生币/稳定币会**全部**变成噪音行 —— 而且看起来"功能在工作"。
    ⚠️ 刻意直接复用 models.is_quote_token,**不另立一张表**:
       它已经按链分别收录了四条链各自的原生币与稳定币(Robinhood 的稳定币是 USDG
       不是 USDC 这种坑就在里面),并且内部还含 _NATIVE_SENTINELS ——
       实测 Robinhood 链上 Uniswap V4 的原生 ETH 就是用
       `0x0000000000000000000000000000000000000000` 表示的($STONKBROKER / $PACK 都是),
       少了这一条它们会各挂一行毫无信息量的「底池 · ETH」。
       另建一张"补充表"会立刻制造两份真相各自 drift(与 pumpfun._CHAIN_ID_TO_NETWORK
       的注释同一条教训)。
    ⚠️⚠️ **但这张表是承重的,改它不是"改显示"**。models.QUOTE_TOKENS 同时门禁着:
       FomoEvent.is_quote → countable_buy(user_token_stats 的共识统计)、
       poller 的 quote_only 分支(落库但**不推送**)、store 的多处徽章与计数判定。
       **为了让这一行少出现几次而往里加一个地址,会静默改掉「谁被推送」和
       「名单内几人买过」** —— 而且不会有任何测试或日志告诉你。
       要动它,先想清楚是不是真的想让那个资产在**全项目**都被当成计价币;
       只是嫌这一行吵,那就该在本模块加过滤,而不是改那张表。
    ⚠️ 这张表宁缺毋滥,因为两种错的**代价不对称**:
       漏收一个计价资产 = 多一行噪音(可容忍);误收一个真币 = 把真信号藏掉(不可容忍)。
    """
    return is_quote_token(network_id, address)


def parse_pool_quote(pair, network_id: str, our_address: str) -> PoolQuote | None:
    """
    一条 pair → 对手资产。判不出来一律 None(那一行整行消失,绝不猜)。

    ⚠️⚠️ **必须判断哪一侧的地址等于我们要查的 CA,取另一侧。**
       base/quote 的方向**不固定**:$AI 在 AI/NVDA 池里是 base,但在 CLANKER/AI、
       SIT/AGI 这类池里是 quote。无脑取 `quoteToken` 会在方向反过来的时候
       **把这个币自己当成它的对手报出来** —— 一句读起来很正常的假话。
    ⚠️⚠️ 地址比较必须**大小写不敏感**:DexScreener 返回 EVM checksum 形态
       (0x2E8c31162b855A2ffa90F6F8634643Ad6F111e18),我们库里存的是小写。
       两边都过 normalize_token_address 之后再比,否则每一条 EVM 记录都判成
       "两侧都不是我们的币" → 整个功能对 EVM 链静默失效。
       (Solana 是 base58、大小写敏感,normalize_token_address 对它原样透传。)
    """
    if not isinstance(pair, dict):
        return None
    # ⚠️ 入参也要归一化,**不能假设调用方已经归一化过**:少了这一句,
    #    传一个 checksum 形态的 CA 进来会静默地判成"两侧都不是我们的币"。
    our_key = normalize_token_address(our_address)
    if our_key is None:
        return None
    base_key, base = _side_key(pair, "baseToken")
    quote_key, quote = _side_key(pair, "quoteToken")
    if our_key == base_key:
        other_key, other = quote_key, quote
    elif our_key == quote_key:
        other_key, other = base_key, base
    else:
        # 这条 pair 两侧都不是我们问的那个币 —— 上游串了数据,丢弃并留痕
        return None
    if other_key is None:
        return None
    return PoolQuote(
        address=other_key,
        symbol=_text(other.get("symbol")),
        name=_text(other.get("name")),
        is_common=_is_common(network_id, other_key),
    )


def parse_pool_quotes(payload, network_id: str, wanted: set[str]) -> dict[str, PoolQuote]:
    """
    整个响应 → {我们的币(归一化地址): 对手资产}。

    wanted 是本次问过的地址集合(已归一化)。**不在里面的一律丢弃** ——
    与 pumpfun 那道"batch 返回了名单外的地址"闸同一条理由:上游多给的东西
    不该悄悄流进推送。

    ⚠️ 上游文档说每个 token 只返回一条(最深的池),但真出现多条时这里按
       liquidity.usd **取最深的那条**,而不是"取最后一条覆盖前面的" ——
       后者的结果取决于数组顺序,是不可预期的。
    """
    out: dict[str, PoolQuote] = {}
    best: dict[str, float] = {}
    if not isinstance(payload, list):
        return out
    for pair in payload:
        if not isinstance(pair, dict):
            continue
        base_key, _ = _side_key(pair, "baseToken")
        quote_key, _ = _side_key(pair, "quoteToken")
        for our_key in (base_key, quote_key):
            if our_key is None or our_key not in wanted:
                continue
            pq = parse_pool_quote(pair, network_id, our_key)
            if pq is None:
                continue
            liq = _liquidity_usd(pair)
            if our_key not in out or liq > best[our_key]:
                out[our_key] = pq
                best[our_key] = liq
    return out


# ============================================================
# HTTP 客户端 —— 只用公开免鉴权端点,异常一律吞掉
# ============================================================
class DexScreenerClient:
    """
    ⚠️ fetch_pairs **不抛异常**,失败一律返回 None 并记日志。
       它跑在调度器的 worker 线程里,一个逃逸的异常最坏会被 APScheduler 记成
       job 崩溃 —— 而这个功能没有任何理由影响别的 job。
    ⚠️ 每线程一个 Session:libcurl 的 easy handle **不能被多线程同时使用**
       (与 pumpfun.PumpClient / client.HttpFomoClient 同一条理由)。
    """

    def __init__(self, proxy: str | None = None) -> None:
        s = get_settings()
        self._proxy = s.fomo_proxy if proxy is None else proxy
        self._tl = threading.local()

    def _session(self):
        sess = getattr(self._tl, "session", None)
        if sess is None:
            # 延迟 import:curl_cffi 带原生库,顶层 import 会让不用它的路径也被拖累
            from curl_cffi import requests as cffi_requests

            proxies = None if not self._proxy else {"http": self._proxy, "https": self._proxy}
            sess = cffi_requests.Session(impersonate="chrome", proxies=proxies,
                                         timeout=_TIMEOUT_SEC)
            self._tl.session = sess
        return sess

    def close(self) -> None:
        sess = getattr(self._tl, "session", None)
        self._tl.session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_pairs(self, slug: str, addresses: list[str]):
        """
        一次批量查询 → 原始数组。任何失败(网络/超时/非 2xx/不是 JSON)一律 None。

        ⚠️ 返回 None(失败)与返回 [](成功但一个都没查到)必须区分开:
           前者不许入缓存,后者是一个确定的答案。
        """
        if not slug or not addresses:
            return None
        url = TOKENS_URL.format(slug=slug, addrs=",".join(addresses))
        try:
            resp = self._session().get(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("DexScreener {} 请求失败(下一轮重试): {}", slug, e)
            self.close()          # 连接可能已经废了,整池丢弃重建
            return None
        if not (200 <= resp.status_code < 300):
            logger.warning("DexScreener {} 返回 HTTP {}", slug, resp.status_code)
            return None
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("DexScreener {} 响应不是 JSON: {}", slug, e)
            return None


# ============================================================
# 带 TTL 缓存的批量查询
# ============================================================
class PoolQuoteLookup:
    """
    「这一批币的底池对手分别是什么」。

    ⚠️ 每个 watcher 各持一份(与 pumpfun 的 _coin_cache 同一条理由):
       共用一份会让"这一轮打了几个请求"变得不可预测。
    """

    def __init__(self, client: DexScreenerClient | None = None,
                 ttl: float = POOL_QUOTE_TTL_SEC) -> None:
        self._client = client if client is not None else DexScreenerClient()
        self._ttl = ttl
        # (内部链标识, 归一化地址) → (取到的时刻, 对手资产)
        self._cache: dict[tuple[str, str], tuple[float, PoolQuote]] = {}

    def close(self) -> None:
        """退出时释放连接池。⚠️ 不抛 —— 它跑在 finally 里。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def lookup(self, network_id: str | None, addresses) -> dict[str, PoolQuote]:
        """
        {归一化地址: 对手资产}。查不到的地址**不出现在返回值里**(调用方据此整行消失)。

        ⚠️⚠️ 链 slug 映射不到 → **一个请求都不发、直接返回空**。硬拼一个 slug
           只会换来 HTTP 200 + 空数组(实测),白挨一次限流还什么都拿不到。
        ⚠️ 同一轮同一个币只查一次:入参先归一化去重,再扣掉缓存命中的,
           剩下的才切片发请求。
        ⚠️⚠️ **失败绝不入缓存**:client 返回 None 时这一片的地址一个都不写缓存,
           下一轮重新问。缓存一次失败 = 一次网络抖动把这一行按住整个 TTL。
        ⚠️ 查得到但对手判不出来(上游结构变了)同样不入缓存 —— 那不是一个答案。
           查得到、但这个币压根没有池子(响应里没有它)也不入缓存:新币几分钟后
           就可能建池,而它顺路搭在同一个批量请求里,重问的边际成本是 0。
        """
        net = (network_id or "").strip()
        slug = DEX_CHAIN_SLUG.get(net)
        keys: list[str] = []
        for a in addresses or ():
            k = normalize_token_address(a)
            if k is not None and k not in keys:
                keys.append(k)
        if not slug or not keys:
            return {}

        now = time.time()
        out: dict[str, PoolQuote] = {}
        missing: list[str] = []
        for k in keys:
            hit = self._cache.get((net, k))
            if hit is not None and now - hit[0] < self._ttl:
                out[k] = hit[1]
            else:
                missing.append(k)

        for i in range(0, len(missing), MAX_ADDRS_PER_REQUEST):
            chunk = missing[i:i + MAX_ADDRS_PER_REQUEST]
            payload = self._client.fetch_pairs(slug, chunk)
            if payload is None:
                continue                       # 失败:一个都不入缓存,下一轮重来
            found = parse_pool_quotes(payload, net, set(chunk))
            for k, pq in found.items():
                out[k] = pq
                self._cache[(net, k)] = (now, pq)
        self._prune(now)
        return out

    def _prune(self, now: float) -> None:
        """先清过期,还超上限就按取到的时刻丢最旧的 —— TTL 长,不设上限它只增不减。"""
        for k, (ts, _v) in list(self._cache.items()):
            if now - ts >= self._ttl:
                del self._cache[k]
        if len(self._cache) > _CACHE_MAX:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[
                    :len(self._cache) - _CACHE_MAX]:
                del self._cache[k]


def notable(quotes: dict[str, PoolQuote], token_address) -> PoolQuote | None:
    """
    从 lookup 的结果里取出**值得占一行**的那个对手资产;没有就 None。

    ⚠️⚠️ 这里是「只在对手不是常见计价资产时才显示」这条策略的**唯一**落点。
       理由(设计选择,不是省事):
         1. 信噪比。实测 Solana 侧三个样本 100% 对 WSOL,Robinhood 侧六个样本里
            四个对 WETH/ETH/USDG —— 总是显示等于给绝大多数推送加一行零信息量的字,
            而推送长度是有预算的(_fit_signal 会按整行砍),噪音行会真的挤掉有用的行。
         2. 「异常才出现」本身就是信号。这一行一旦出现,读者不用读内容就知道
            "这个币绑在别的东西上了";而每条都有的话,它就退化成版式的一部分。
            这与项目里已有的做法一致(⚠️ 转账获得、⏳ 基线建立中 都是条件出现)。
         3. 与铁律 2「缺失整行消失」同构:没有可说的就不说,绝不打占位符。
    ⚠️ 入参地址同样要归一化后再查 —— 调用方手上的可能是 checksum 形态。
    """
    key = normalize_token_address(token_address)
    if key is None:
        return None
    pq = quotes.get(key)
    if pq is None or pq.is_common:
        return None
    return pq
