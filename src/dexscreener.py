"""
底池对手资产 —— 这个币最深的那个池子,对面摆的是什么(**只在对手是币股时才报**)。

============ 为什么值得单独做一个模块 ============
底池对着什么,决定这个币的命运绑在谁身上。绝大多数币对着原生币或稳定币
(WBNB / SOL / USDC / WETH),那是背景噪音;但 Robinhood 链上成规模地存在
「对手是代币化股票」的池子 —— 实测 $AI(0x2e8c…1e18)最深的池是
**AI / NVDA**(quoteToken.name = "NVIDIA • Robinhood Token",liq $4.49M)。
NVDA 一跌它就跟着跌,这与一个对着 BNB 的币是**两种风险**,而推送里原先一个字都没说。

============ ⚠️⚠️ 教训:「验了条数、没验深度」 ============
最早这里用的是 `GET /tokens/v1/{chain}/{addrs}` 批量端点,注释里写着
「每个 token 返回**一条**(该 token 流动性最深的那个池)」。**前半句对,后半句是编的**:
当初调研只数了"每个币回几条",没有把它的结果跟真实池子列表比过深度,
而抽样的 6 个币恰好都撞对了,于是实现与验证**两轮同时漏掉**。

线上真实后果($Rabbit,0xcd1cca2b…0901,2026-09-01 实测):
    /tokens/v1  → ETH   liq $31,069     ← 端点给的
    真正最深    → WYFI  liq $466,663    ← 差 15 倍,而且是唯一有信息量的那条
用户看到的是「底池那一行根本没出现」—— ETH 是计价资产,被正确地藏掉了,
于是一个错得离谱的答案表现成"这个功能没话说",连一条日志都不会有。

**可迁移的那半句:「上游会替我们排序/挑选」是一个假设,验它必须拿它的答案
  去跟全量列表比深度;数"每个币回几条"验不出任何东西。**
现在改用 `GET /latest/dex/tokens/{addrs}`(返回该币的多个池)自己按
`liquidity.usd` 取最深 —— 排序权握在自己手里,不再依赖上游的任何承诺。

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
from dataclasses import dataclass, replace

from loguru import logger

from src.config import get_settings
from src.models import is_quote_token, normalize_token_address

# ============================================================
# 端点与常量
# ============================================================
# GET /latest/dex/tokens/{addr1,addr2,…} → {"pairs": [...]},含这些地址的**多个**池子。
# ⚠️ 这个端点**不带链**:同一个 EVM 地址在别的链上也可能有池子,响应会一起给。
#    实测 0x4206931337dc273a630d328da6441786bfad668f 一次就同时回了
#    ethereum / bsc / pulsechain 三条链的 pair。所以解析时**必须按 chainId 过滤**
#    (见 _chain_matches)—— 少了这一步会拿另一条链的池子当本链最深的池。
LATEST_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addrs}"

# ⚠️⚠️ **整个响应**最多 30 条 pair —— 不是"每个币 30 条"。实测(2026-09-01):
#      传 8 个 robinhood 地址 → 回 30 条 → 只覆盖到 7 个币,另一个一条池都没轮到;
#      四条链各试两批,批批都是 30 条。**传 1 个地址同样会顶到 30 条**
#      (Rabbit / BONER / AI / CASHCAT 都是 30,QUANT 是 27 —— 27 那条证明 30 是
#       上限不是固定页大小)。
# ⚠️⚠️ 响应里的顺序**不是**按 liquidity 排的(实测:每一批都存在逆序点),
#      所以"被截掉的是最浅的那些"这件事**不能从顺序上推**。这就是
#      MAX_ADDRS_PER_REQUEST 只敢用 1 的全部理由(见下)。
LATEST_PAIRS_CAP = 30

# 上游允许一次带多少个地址(URL 侧上限)。
_ADDRS_URL_LIMIT = 30

# 实际一次请求带几个地址。**是 1**,理由:
#   多地址请求里 30 条的额度要被几个币分,而顺序不按深度排 ——
#   于是"这个币被覆盖到了"完全**不等于**"它最深的池子在里面"。
#   实测一批 8 个地址时,某个热门币独占 18 条、其余的各分到 1 条,
#   那 1 条几乎必然不是它最深的。用批量省下的请求,买来的是一批**看起来正常的错答案**,
#   与上面那个 $Rabbit 的 bug 是同一种。
# ⚠️ 改大它不会静默出错:lookup 里那道闸会把"顶到上限的多地址响应"整片作废、
#    逐个地址补查(见 lookup)。但那等于先白打一个请求,所以默认就用 1。
MAX_ADDRS_PER_REQUEST = 1

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
# ⚠️⚠️ 换了端点之后这张表的职责也换了:/latest/dex/tokens 不带链,
#    它是**响应过滤器**——「这条 pair 的 chainId 等于本链的 slug 才算数」。
#    写错的后果一模一样(该链每条推送静默少一行、日志里一个字都没有),
#    所以每一条仍然是**实测**出来的:响应里 chainId 真的等于这个值。
# ⚠️⚠️ **绝不能复用 models.NETWORK_SLUG** —— 那张表是给 fomo.family 的 URL 用的,
#    里面 bsc 映射成 `bnb`,而 DexScreener 的 chainId 是 `bsc`。
# ⚠️ 映射不到的链(ethereum / monad / hyperliquid,以及将来出现的新链)
#    **整行消失,绝不硬拼一个 slug 去试**。
# ⚠️ 四条链都留着,而 lookup 只会给 STOCK_NAME_MARKERS 里有判据的链发请求 ——
#    留着是为了将来给某条链补上判据时,不必把 chainId 再实测一遍。
DEX_CHAIN_SLUG = {
    "solana": "solana",       # 实测 CATE/fone/CYBERLEEK → chainId=solana
    "bsc": "bsc",             # 实测 MarsCoin/XAUt/WBNB   → chainId=bsc
    "base": "base",           # 实测 BLUECHIP/Bots/WETH   → chainId=base
    "robinhood": "robinhood",  # 实测 AI/CASHCAT/PONS      → chainId=robinhood
}

# ============================================================
# 币股判据 —— 「对手是不是代币化的股票类资产」
# ============================================================
# 只有对手是**币股**时这一行才出现(个股 / ETF / 杠杆产品 / 未上市公司都算),
# 对手是另一个 memecoin 的不出现 —— 用户口径。
#
# ⚠️⚠️ 判据只认**这张表里明写的链**,别的链一律判成"不是"(那条链整行不出现)。
#    两种错的代价不对称:漏一个股票 = 少一行;把不是股票的说成股票 = **假事实**。
#
# ---- 调研结果(2026-09-01,247 个不同的真实代币,四条链) ----
# robinhood:`name` 以 " • Robinhood Token" 结尾 = 代币化的现实世界资产。**分得很干净**:
#   命中 15 个(NVDA/WYFI/HIMS/AAPL/GME/MSTR/SPCX/DJT/COST/QUBT/MU/TSM/MSFT/COIN/SNDK,
#   个股 / ETF「SPDR S&P 500 ETF Trust」/ 未上市「Space Exploration Technologies Corp.」都在内),
#   同链另外 53 个代币一个都不带这个后缀 —— 包括几个**专门碰瓷**的 memecoin:
#   `GME · GameStop`(0xc2362aff…)、`GPRO · GoPro Inc`、`TIM · Tim Apple`、`SAYLORMOON`。
#   ⚠️ 必须 endswith 不是 in:`HOODon · Robinhood Markets (Ondo Tokenized)` 名字里有
#      "Robinhood" 但不是这个后缀,它是另一家(Ondo)的产品。
# base / bsc / solana:**没找到可靠标记,故意留空**。证据(全是实测):
#   · base 的币股是 `NVDAc · NVIDIA Corporation` / `AAPLc · Apple Inc.` / `GOOGLc · Alphabet Inc.`,
#     地址前缀 0xb2000000000000000000… —— 但那是**发射台的靓号前缀**,同前缀的
#     `Basecat` / `RAWR` / `BLUECHIP` / `dogue` / `MEOW` 全是 memecoin。前缀判不了。
#     名字里的 "Corporation / Inc." 更判不了(`GPRO · GoPro Inc` 就是 memecoin)。
#   · bsc 的币股是 `SPCXB · SpaceX` / `NVDAB · NVIDIA Corp` / `SPYB · SPY` / `GMEB · GameStop`,
#     符号都以 B 结尾 —— 但 `BTCB · BTCB Token` 也以 B 结尾,而它不是股票。名字侧
#     `SpaceX` / `SPY` / `GameStop` 是**任何人都能取的普通词**,没有任何后缀可依。
#   · solana 的 xStocks 形态很齐(`SPYx · SP500 xStock` / `AAPLx · Apple xStock` /
#     `MCDx · McDonald's xStock` / `CRCLx · Circle xStock`,地址都以 Xs 开头),
#     但样本只有 4 个,不足以证明"没有别的东西也叫 … xStock"。**宁可漏掉**。
# ⚠️ 已知会漏(如实记):robinhood 上的杠杆产品 `NVDAx3L · NVDA 3x Long`
#    (实测是 $DEMOTHREE 最深池的对手)**没有**这个后缀,因此不显示。
#    247 个样本里这种形态只出现 1 次,凭 1 个样本立规矩就是在赌;
#    要收它,得先拿到一批同形态的真实样本。
STOCK_NAME_MARKERS = {
    # 实测原文是 "NVIDIA • Robinhood Token"(U+2022 BULLET,前后各一个空格)。
    # ⚠️ 这里**不带前面那个空格**:名字先过 _text 叠平空白,一个只剩后缀的名字
    #    (" • Robinhood Token")会被叠成 "• Robinhood Token" —— 带空格的判据
    #    对它就不成立了,于是"是股票但提不出公司名"那条路**永远走不到**,
    #    "缺了就只显示符号"这条规矩会变成一句没人执行得到的话。
    "robinhood": "• Robinhood Token",
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
    name       上游给的原始全名,如 "NVIDIA • Robinhood Token"(排查时看的就是它)
    is_common  是不是常见计价资产(原生币 / 稳定币)。见 _is_common 的说明 ——
               常见的那些是背景噪音,不值得在推送里占一行。
    is_stock   是不是**币股**(代币化的股票类资产)。见 STOCK_NAME_MARKERS ——
               只有它为真这一行才会出现。
    issuer     从 name 里剥掉判据后缀之后的公司/产品名,如 "WhiteFiber, Inc."。
               ⚠️ 剥不出来是 None(名字就是一个光秃秃的后缀),那就**只显示符号**;
                  绝不回退去编一个名字,也绝不打占位符。

    ⚠️ 两个新字段带默认值是为了让 `PoolQuote(addr, sym, name, is_common)` 这种
       四参数构造继续成立(测试里造样本用)。
    """

    address: str
    symbol: str | None
    name: str | None
    is_common: bool
    is_stock: bool = False
    issuer: str | None = None


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
    """
    池子深度。**这一路的排序权全在这个函数上** —— 同一个币的多条 pair 里挑哪一条,
    只看它。取不到当 0(排最后),因为"深度未知"没有资格盖过一个已知的深池。

    ⚠️ 绝不能用 volume / fdv / txns 之类的字段代替:那些是活跃度,
       而"这个币绑在谁身上"问的是钱压在哪儿。
    """
    liq = pair.get("liquidity")
    if not isinstance(liq, dict):
        return 0.0
    try:
        return float(liq.get("usd"))
    except (TypeError, ValueError):
        return 0.0


def _chain_matches(pair, network_id) -> bool:
    """
    这条 pair 是不是本链的。

    ⚠️⚠️ /latest/dex/tokens **不带链**,同一个 EVM 地址在别的链上的池子会一起回来
       (实测 0x4206…668f 一次同时回了 ethereum / bsc / pulsechain 三条链)。
       少了这道过滤,"最深的池"可能取自**另一条链** —— 一个金额、符号全都对得上、
       只有链错了的答案,肉眼看不出来。
    ⚠️ 映射不到 slug 的链一律 False:宁可整行消失,也不能拿一条不知道哪来的 pair 顶上。
    """
    slug = DEX_CHAIN_SLUG.get((network_id or "").strip())
    return slug is not None and pair.get("chainId") == slug


def _classify_stock(network_id, name) -> tuple[bool, str | None]:
    """
    对手是不是币股 → (是否, 公司/产品名)。

    ⚠️⚠️ 判据按**链**分别定义(STOCK_NAME_MARKERS),没有判据的链一律 (False, None)。
       "名字里带 Corporation 就算"这种跨链通用规则是**猜**:实测 Robinhood 链上
       `GPRO · GoPro Inc`、`TIM · Tim Apple` 都是 memecoin,按那种规则会被说成股票。
    ⚠️ 公司名是**剥掉后缀之后剩下的部分**,不是另外拼的。剥完是空 → None,
       调用方就只显示符号(缺了就少那半句,绝不编)。
    """
    marker = STOCK_NAME_MARKERS.get((network_id or "").strip())
    if not marker:
        return False, None
    s = _text(name)
    # ⚠️ 必须 endswith:`Robinhood Markets (Ondo Tokenized)` 名字里有 "Robinhood",
    #    但它是别家的产品,用 `in` 判会把它误收进来。
    if s is None or not s.endswith(marker):
        return False, None
    return True, _text(s[:-len(marker)])


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
    # ⚠️⚠️ 先过链:响应是跨链的,不过滤会把别的链的池子当成本链最深的池(见 _chain_matches)
    if not _chain_matches(pair, network_id):
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
    raw_name = _text(other.get("name"))
    is_stock, issuer = _classify_stock(network_id, raw_name)
    return PoolQuote(
        address=other_key,
        symbol=_text(other.get("symbol")),
        name=raw_name,
        is_common=_is_common(network_id, other_key),
        is_stock=is_stock,
        issuer=issuer,
    )


def parse_pool_quotes(payload, network_id: str, wanted: set[str]) -> dict[str, PoolQuote]:
    """
    整个响应 → {我们的币(归一化地址): **最深那个池子**的对手资产}。

    wanted 是本次问过的地址集合(已归一化)。**不在里面的一律丢弃** ——
    与 pumpfun 那道"batch 返回了名单外的地址"闸同一条理由:上游多给的东西
    不该悄悄流进推送。

    ⚠️⚠️ 这里的 `liq > best[our_key]` 是整个功能的**核心不变式**:
       响应里同一个币有几十条 pair,顺序**不是**按深度排的(实测每一批都有逆序点)。
       退回"取第一条"或"取最后一条"都等于按数组顺序随机挑一个池子 ——
       $Rabbit 就会重新变回「底池 · ETH($31k)」而不是「WYFI($467k)」。
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


def is_truncated(payload) -> bool:
    """
    这份响应是不是**顶到了 30 条的上限**(= 后面还有池子没给我们)。

    ⚠️⚠️ 这是本模块唯一能看见"上游把结果切了"的信号,而且是必须的:
       条数顶格 + 顺序不按深度排 ⇒ **任何一个币最深的池子都可能被切掉**。
       实测传 8 个地址回 30 条只覆盖到 4~7 个币,另外几个一条池都没轮到;
       被覆盖到的那几个也未必拿到自己最深的那条。
    ⚠️ 判 `>=` 不判 `==`:上游哪天把上限调到 50,`== 30` 会一声不响地永远判成"没截"。
    """
    return isinstance(payload, list) and len(payload) >= LATEST_PAIRS_CAP


def covered_keys(payload, network_id: str, wanted: set[str]) -> set[str]:
    """
    这份响应里**本链**真的出现过的地址(wanted 的子集)。

    ⚠️ 与 parse_pool_quotes 分开:一个币"出现过"但对手解析不出来,与"压根没出现",
       在补查决策上是两回事 —— 前者重问也是白问,后者值得重问。
    """
    out: set[str] = set()
    if not isinstance(payload, list):
        return out
    for pair in payload:
        if not isinstance(pair, dict) or not _chain_matches(pair, network_id):
            continue
        for field in ("baseToken", "quoteToken"):
            key, _ = _side_key(pair, field)
            if key is not None and key in wanted:
                out.add(key)
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

    def fetch_pairs(self, addresses: list[str]):
        """
        一次查询 → pair 数组。任何失败(网络/超时/非 2xx/不是 JSON)一律 None。

        ⚠️ 返回 None(失败)与返回 [](成功但一个池都没有)必须区分开:
           前者不许入缓存,后者是一个确定的答案。
        ⚠️ 这个端点**不带链**,所以这里也不收 slug —— 过滤在解析层按 chainId 做
           (见 _chain_matches)。把链塞进 URL 是上一版 /tokens/v1 的形态,别退回去。
        ⚠️ 上游把数组包在 {"pairs": [...]} 里,且**查不到时 pairs 是 null 不是 []**;
           这里统一成 [],免得每个调用方各判一次 None。
        """
        if not addresses:
            return None
        url = LATEST_TOKENS_URL.format(addrs=",".join(addresses))
        tag = addresses[0]
        try:
            resp = self._session().get(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("DexScreener 请求失败(下一轮重试) | {} | {}", tag, e)
            self.close()          # 连接可能已经废了,整池丢弃重建
            return None
        if not (200 <= resp.status_code < 300):
            logger.warning("DexScreener 返回 HTTP {} | {}", resp.status_code, tag)
            return None
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("DexScreener 响应不是 JSON | {} | {}", tag, e)
            return None
        if isinstance(body, list):        # 上游别的端点是裸数组,顺手兼容
            return body
        if not isinstance(body, dict):
            logger.warning("DexScreener 响应结构不认识 | {} | {}", tag, type(body).__name__)
            return None
        pairs = body.get("pairs")
        return pairs if isinstance(pairs, list) else []


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
                 ttl: float = POOL_QUOTE_TTL_SEC,
                 addrs_per_request: int = MAX_ADDRS_PER_REQUEST) -> None:
        self._client = client if client is not None else DexScreenerClient()
        self._ttl = ttl
        # 一次请求带几个地址。默认 1(理由见 MAX_ADDRS_PER_REQUEST)。
        # ⚠️ 之所以做成可注入而不是写死:它决定了"截断了要不要作废重查"那条分支
        #    走不走得到,而那条分支正是这个功能的安全带 —— 写死 1 等于把安全带
        #    焊在不可能拉动的位置上,没人能证明它还在。
        self._addrs_per_request = max(1, min(int(addrs_per_request), _ADDRS_URL_LIMIT))
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

        ⚠️⚠️ 这条链没有币股判据(STOCK_NAME_MARKERS)或映射不到 chainId →
           **一个请求都不发、直接返回空**。理由不是省事:那条链上这一行
           **永远显示不出来**(notable 会一律判 None),为一个必然不显示的东西
           打一轮请求是纯浪费。加判据的那天,请求会自动跟着回来 ——
           两件事共用同一张表,不会 drift。
        ⚠️ 同一轮同一个币只查一次:入参先归一化去重,再扣掉缓存命中的,
           剩下的才发请求。
        ⚠️⚠️ **失败绝不入缓存**:client 返回 None 时这一片的地址一个都不写缓存,
           下一轮重新问。缓存一次失败 = 一次网络抖动把这一行按住整个 TTL。
        ⚠️ 查不到(响应里没有这个币)同样不入缓存:新币几分钟后就可能建池。

        ---- 截断这道闸 ----
        ⚠️⚠️ 响应顶到 30 条 = 上游把结果切了,而**切的顺序不是按深度**。
           所以一份顶格的**多地址**响应里,谁的最深池都可能已经被切掉 ——
           这时整片作废、逐个地址补查(单地址能拿到的深度上限最高)。
           不作废的话拿到的是一批"覆盖到了、但答案是浅池"的结果:
           读起来完全正常,错得毫无痕迹,与 $Rabbit 那个 bug 同一种。
        ⚠️ 单地址请求仍然顶格时(实测 Rabbit / AI / CASHCAT 都正好 30),
           上游给不了更多了 —— 取已有的最深那条并**记一条日志**。
           证据侧:实测 SPY 的 30 条最浅是 $3,779、而 CATE 的 30 条最浅是 $0,
           说明这 30 条**很像**是按深度选出来的 top30(不然 SPY 那种大票的
           dust 池不可能一条都没混进来);但这只是旁证不是保证,所以留日志。
        """
        net = (network_id or "").strip()
        keys: list[str] = []
        for a in addresses or ():
            k = normalize_token_address(a)
            if k is not None and k not in keys:
                keys.append(k)
        if not keys or net not in DEX_CHAIN_SLUG or not STOCK_NAME_MARKERS.get(net):
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

        # 第一轮按配置的片大小;被截断而作废的地址,第二轮**逐个**补查。
        # 逐个仍被截就只能收下(上游的天花板),不再有第三轮。
        pending = missing
        for size in (self._addrs_per_request, 1):
            retry: list[str] = []
            for i in range(0, len(pending), size):
                chunk = pending[i:i + size]
                payload = self._client.fetch_pairs(chunk)
                if payload is None:
                    continue                   # 失败:一个都不入缓存,下一轮重来
                if is_truncated(payload) and len(chunk) > 1:
                    logger.debug("DexScreener 响应被截断,这片 {} 个地址逐个补查 | {}",
                                 len(chunk), net)
                    retry.extend(chunk)
                    continue
                if is_truncated(payload):
                    logger.debug("DexScreener 单地址响应仍顶到 {} 条上限,取已有的最深池 | {}",
                                 LATEST_PAIRS_CAP, chunk[0])
                found = parse_pool_quotes(payload, net, set(chunk))
                for k, pq in found.items():
                    out[k] = pq
                    self._cache[(net, k)] = (now, pq)
            pending = retry
            if not pending or size == 1:
                break
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

    ⚠️⚠️ 这里是「只在对手是**币股**时才显示」这条策略的**唯一**落点。
       两道门,顺序无所谓但缺一不可:
         1. is_stock —— 对手得是代币化的股票类资产(个股 / ETF / 杠杆产品 /
            未上市公司都算)。对手是另一个 memecoin 的不显示:$AI 对 $BONER
            这种"两个 meme 互相对着"没有任何风险信息,是纯噪音(用户口径)。
         2. not is_common —— 计价资产一律藏。这道门在今天**看起来**是多余的
            (USDG「Global Dollar」不带 Robinhood Token 后缀,过不了第一道门),
            但它挡的是"某天上游把稳定币也发成 `USDG • Robinhood Token`"——
            那时第一道门会放行,而「底池 · USDG」是纯版面噪音。留着,便宜。
    ⚠️⚠️ 返回的是**给展示用的形态**:name 字段里换成了 issuer(公司/产品名,
       "WhiteFiber, Inc."),不是上游原文("WhiteFiber, Inc. • Robinhood Token")。
       后缀是**判据**不是信息 —— 它对每一条命中的记录都一样,印在推送里只占地方。
       原始 name 仍在 lookup 的返回值里,排查时看得到。
       ⚠️ issuer 剥不出来时这里就是 None,渲染层据此**只出符号**(缺了少半句,绝不编)。
    ⚠️ 入参地址同样要归一化后再查 —— 调用方手上的可能是 checksum 形态。
    """
    key = normalize_token_address(token_address)
    if key is None:
        return None
    pq = quotes.get(key)
    if pq is None or pq.is_common or not pq.is_stock:
        return None
    return replace(pq, name=pq.issuer)
