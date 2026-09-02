"""
发射台(🚀)与持有人数(🧑‍🤝‍🧑)—— 两个"币本身的属性",与市值/币龄一族。

社媒(🔗)**不在本模块**:它躺在 dexscreener 那份已经在取的响应里(pair.info),
不值得为它开第二个请求(见 dexscreener._socials)。

============ 数据来自哪儿(全部匿名、免 key、只读)============
■ FOMO 的 filterTokens(发射台 + 持有人,跨链混批)
    POST https://prod-api.fomo.family/public/proxy/filterTokens
    headers: Content-Type: application/json
             X-Supported-Chains: 1,56,143,4663,8453,1399811149
    body:    ["<address>:<chainId>", …]      ← 顺序是 address:chainId
    ⚠️⚠️ **X-Supported-Chains 这个头是关键**:不带它,EVM 链返回
       HTTP 200 + responseObject: [](成功状态码 + 空数组,最阴的失败形态)。
       src/client.py:fetch_token_meta 上一版就是缺这个头,对 robinhood 链
       一直返回空 —— 本轮一并修好,并各有一条测试钉住这个头。
    ⚠️ 必须 curl_cffi impersonate="chrome",否则 Cloudflare 直接 HTTP 430。
    ⚠️ 非 /public/ 的同名路径匿名一律 431(要 Bearer)。**不要碰。**

■ Robinhood 链的 Blockscout(持有人精确值 + Pons 版本判定)
    GET https://robinhoodchain.blockscout.com/api/v2/tokens/{addr}        → holders_count
    GET https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}     → 创建交易
    GET https://robinhoodchain.blockscout.com/api/v2/transactions/{tx}    → to.hash(工厂)

============ ⚠️⚠️ 持有人数的三个坑(全是实测,不是防御性编程)============
1. **holders == 0 是「取不到」的哨兵,不是真的 0。** robinhood 上 31 个 0 逐个查
   Blockscout,28 个是错的(有个显示 0 的实际 4302 人);solana 的 USDC 也是
   FOMO=0 / 真值 524 万。⇒ 解析层就把 0 当 None,绝不让它流到渲染层。
   ⚠️ 这一条与本项目"判空一律 is None、0 是真实值"那条铁律**不矛盾**:
      铁律说的是"不要用真值判断吞掉真实的 0",而这里是**上游用 0 表示缺失** ——
      判据在解析层做一次、做干净,下游拿到的就只有 None 或真值。
2. **robinhood 上 FOMO 的 holders 对已经死掉的小币严重过期。** 552 个币对比
   FOMO vs Blockscout:中位差 0.308%、完全相等 37.0%、差 >50% 的占 4.3%,
   最离谱的 fone 是 Blockscout=36 / FOMO=2186(差 5972%)。**Blockscout 对**:
   fone 那 36 个地址的余额合计 = 总供应量的 100.0000%,数学上不可能有第 37 个人。
   ⇒ **robinhood 优先 Blockscout,FOMO 只作兜底**;其余三条链用 FOMO
   (已用 solana RPC / basescan / GeckoTerminal 独立验到 18/19 落在 1.5% 以内)。
3. **base.blockscout.com 的 holders_count 是错的**(Basecat:blockscout 2234 /
   basescan 23399 / FOMO 23423)。⇒ 别拿它当 base 的兜底。robinhood 那个实例是对的。

============ 发射台:不猜 ============
launchpadName 为空 → 整行不显示。**绝不用域名反推** —— 那条路已经被证伪:
CASHCAT 的官网是 cashcat.cc、AI 的是 artificialinu.com,都是项目自己的站,
跟发射台没关系。
误报率的硬证据:扫 560 个 robinhood 代币,从链上取每个币创建交易的工厂合约,
验"同一个工厂从不产生两个不同的 launchpadName" —— 违反数 0。

Pons V1/V2:FOMO 只给 "pons",不区分版本,而用户要的是「Pons V2」。
区分靠两次 Blockscout(创建交易 → 它的 to,即工厂合约),映射见 PONS_FACTORIES。
⚠️ 实测 15.7%(88/560)的代币拿不到创建交易的 to → 那部分退回显示 "Pons",**不猜**。

============ ⚠️⚠️ 限速(本项目有过 tick 从 5s 拖到 90s 的教训)============
filterTokens 实测:无间隔连打**第 10 次**触发 Cloudflare 429,retry-after 203 秒,
封禁期继续打不会延长;每 10 秒 1 次连续 22 次,0 个 429。
⇒ 本模块的闸门:
  · 一个 tick 把待推代币**攒成一个批次**、跨链混批、一次请求;>200 个才切第二批;
  · **任意两次请求间隔 >= _MIN_INTERVAL_SEC(10s),绝不并发** —— 闸是**进程级**的
    (poller 与 pumpfun 各有一个 lookup 实例,共用同一把闸;做成实例级等于把
     10 秒间隔悄悄变成 5 秒);
  · 拿不到闸(等超过 _GATE_MAX_WAIT_SEC)→ 这几行本轮消失,**绝不阻塞推送**;
  · 429 → 读 retry-after 进冷却,本 tick 这几行整体消失,不重试。
Blockscout 未观察到限速(6 并发 1680 次请求 0 个 429),但仍设每轮次数上限 +
墙钟闸门(与 namecn 那套同一个思路):真正要防的是"外部接口把 tick 拖慢"。

============ 缓存 ============
复用 name_glossary(kind/key/value/expires_at 的通用 KV,见 store):
  launchpad     成功 → **永久**(发射台是代币出生时定死的属性,560 个币的工厂→名字
                映射 0 违反);查无 → 7 天;失败 → 1 小时(三档 TTL 各自独立)
  pons 版本     成功 → **永久**;查无(拿不到创建 tx / 工厂不在表里)→ 7 天;失败 → 1 小时
  holders       **内存** 90 秒(实测上游自己的缓存就是这个量级)。刻意不落库:
                90 秒的东西写进 SQLite 只是给磁盘添活儿,进程重启后重查一次就有。
  社媒          不在本模块;它随 dexscreener 那份 6 小时内存缓存一起走(零新增请求)。

⚠️⚠️ **绝不因这些外呼报错影响推送主路径**:本模块所有对外方法都不抛异常,
   失败一律记 WARNING 并让对应的那一行消失。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from loguru import logger

# ⚠️⚠️ SUPPORTED_CHAINS 从 client 借,**不在这里再写一份数字串**:
#    一份表放两个地方早晚走岔(这个项目已经有过这条教训),而这个头写错的后果是
#    HTTP 200 + 空数组 —— 没有任何日志会说它错了。
from src.client import SUPPORTED_CHAINS
from src.config import get_settings
from src.models import NETWORK_CHAIN_ID, normalize_token_address
from src.store import get_conn

# ============================================================
# 端点与常量
# ============================================================
FILTER_TOKENS_URL = "https://prod-api.fomo.family/public/proxy/filterTokens"

BLOCKSCOUT_BASE = "https://robinhoodchain.blockscout.com/api/v2"

# 一次 filterTokens 最多带几个地址。实测 300 个一次全返回(1748ms)。
# 200 留了余量,也让"超过 200 要切第二批"这条分支在真实负载下走得到。
MAX_KEYS_PER_REQUEST = 200
# 一个 tick 最多切几批。⚠️ 第二批同样要过 10 秒的闸,拿不到就那些币的两行消失。
MAX_BATCHES_PER_ROUND = 2
# 任意两次 filterTokens 请求的最小间隔。实测:无间隔第 10 次就 429(冷却 203 秒),
# 每 10 秒 1 次连打 22 次 0 个 429。
_MIN_INTERVAL_SEC = 10.0
# 等闸最多等多久。⚠️ 这个数字直接加在 tick 的墙钟上,**必须小**:
#    poller 每 15 秒一个 tick,等 10 秒等于把 tick 拖长 2/3。等不到就这一轮不查。
_GATE_MAX_WAIT_SEC = 2.0
# 429 之后的兜底冷却(上游没给 retry-after 时用)。实测真实值是 203 秒。
_DEFAULT_COOLDOWN_SEC = 210.0
# retry-after 的上限:一个畸形的头不该把这个功能按住一整天。
_MAX_COOLDOWN_SEC = 900.0

_TIMEOUT_SEC = 12.0

# Blockscout 每轮的次数与墙钟闸门。
BLOCKSCOUT_HOLDERS_PER_ROUND = 12
# Pons 版本判定一次要**两个**请求,而且是永久缓存(一个币这辈子只判一次),
# 所以每轮只给 2 个额度 —— 慢慢把库里的 pons 币判完,不跟 tick 抢时间。
BLOCKSCOUT_PONS_PER_ROUND = 2
ROUND_WALL_CLOCK_SEC = 8.0

# 缓存 kind(name_glossary 的第一主键)。⚠️ 与 namecn 的 token_zh/company_zh 分开,
#    互不干扰;负缓存(value 为 NULL)的语义由本模块自己解释。
KIND_LAUNCHPAD = "launchpad"
KIND_PONS_VER = "pons_ver"

TTL_MISS_SEC = 7 * 86400          # 查过了、上游确实没有 → 7 天后再问一次
TTL_ERROR_SEC = 3600              # 网络/HTTP/JSON 失败 → 1 小时,别把一次抖动按住 7 天
HOLDERS_TTL_SEC = 90.0            # 内存缓存,见模块头

# ============================================================
# Pons 工厂合约 → 版本名
# ============================================================
# ⚠️⚠️ 这是**封闭枚举**,不是模式匹配:四个都是 robinhood 链上已验证的合约,地址固定。
#    表外的工厂 → 退回 "Pons"(不猜版本)。表本身就是"我们知道的全部"。
# ⚠️ 键是小写(链上地址大小写不定,比对前一律 lower)。
PONS_FACTORIES = {
    "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb": "Pons",      # PonsLaunchFactory  (V1)
    "0xe33e9e479df8802cb0866d5d05258bec4cf62948": "Pons V2",   # PonsV2LaunchAndBuy
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e": "Pons V2",   # PonsV2LaunchFactory
    "0x22e99278308b393ea1260859b181ad7e78f5eeed": "LONG",      # LongLauncher
}
# FOMO 给的 launchpadName 等于这个值时才去分版本(大小写不敏感比对)。
PONS_LAUNCHPAD_NAME = "pons"
# 分不出版本时的显示值。⚠️ **绝不猜 V2** —— 实测 15.7% 的币拿不到创建交易的 to。
PONS_FALLBACK = "Pons"

# 只有这条链走 Blockscout(持有人 + Pons 版本)。
BLOCKSCOUT_NETWORK = "robinhood"


# ============================================================
# 数据结构
# ============================================================
@dataclass(frozen=True)
class TokenExtra:
    """
    一个币的两个"出身属性"。两个字段**各自独立** —— 一个拿不到不影响另一个,
    更不影响整条推送(铁律 2:缺失整行消失)。
    """

    launchpad: str | None = None
    holders: int | None = None


EMPTY = TokenExtra()


# ============================================================
# 解析(纯函数,可脱网完整单测)
# ============================================================
def parse_holders(raw) -> int | None:
    """
    上游给的持有人数 → int;**0 一律当 None**(见模块头的坑 1),负数同理。

    ⚠️ 上游两种类型都出现过:Blockscout 给字符串 "1194"、FOMO 给数字 1227。
       两种都吃,吃不动就 None(绝不把字符串原样往渲染层塞)。
    ⚠️ bool 是 int 的子类,单独挡掉 —— True 会被 int() 变成 1。
    ⚠️⚠️ **只认纯 ASCII 数字串**(可带一个负号)。`int()` 自己收得比这宽得多:
       它吃全角数字(`１３８００１３８０００` → 13800138000)、吃前导 `+`
       (`+79001234567` → 79001234567)、吃下划线分隔(`1_000`)。
       这个槽位是上游可控的自由文本,而"一串数字"恰好是手机号 / QQ 号的形态 ——
       与其信任 int() 的宽容,不如在这里把形状收死。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        n = raw
    else:
        text = str(raw).strip()
        body = text[1:] if text[:1] == "-" else text
        if not body or not all("0" <= c <= "9" for c in body):
            return None
        n = int(text)
    return n if n > 0 else None


def parse_launchpad_name(item) -> str | None:
    """
    responseObject 的一项 → launchpadName;拿不到 None(那一行消失,**不猜**)。

    ⚠️ 实测 launchpad 有三种形态:带 launchpadName 的对象、**空对象 {}**、以及缺键。
       三种都要落到 None,不能只判 `is None`。
    """
    if not isinstance(item, dict):
        return None
    token = item.get("token")
    if not isinstance(token, dict):
        return None
    lp = token.get("launchpad")
    if not isinstance(lp, dict):
        return None
    name = lp.get("launchpadName")
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())
    return name or None


def parse_filter_tokens(payload) -> dict[tuple[str, str], TokenExtra]:
    """
    filterTokens 的响应 → {(内部链标识, 归一化地址): TokenExtra}。

    ⚠️ 响应带 token.networkId,跨链混批时靠它自己分开 —— 不依赖请求顺序
       (上游没承诺过顺序,而"按下标对齐"这种假设错了是静默错位)。
    ⚠️ 认不出链 / 认不出地址的项直接丢弃,不猜。
    """
    out: dict[tuple[str, str], TokenExtra] = {}
    items = payload if isinstance(payload, list) else []
    num_to_net = {num: net for net, num in NETWORK_CHAIN_ID.items()}
    for item in items:
        if not isinstance(item, dict):
            continue
        token = item.get("token") if isinstance(item.get("token"), dict) else {}
        try:
            net = num_to_net.get(int(token.get("networkId")))
        except (TypeError, ValueError):
            net = None
        key = normalize_token_address(token.get("address"))
        if net is None or key is None:
            continue
        out[(net, key)] = TokenExtra(launchpad=parse_launchpad_name(item),
                                     holders=parse_holders(item.get("holders")))
    return out


def parse_creation_tx(payload) -> str | None:
    """Blockscout /addresses/{addr} → 创建交易 hash;没有就 None。"""
    if not isinstance(payload, dict):
        return None
    tx = payload.get("creation_transaction_hash")
    tx = " ".join(str(tx or "").split())
    return tx or None


def parse_tx_to(payload) -> str | None:
    """Blockscout /transactions/{tx} → to.hash(合约创建走的那个工厂);没有就 None。"""
    if not isinstance(payload, dict):
        return None
    to = payload.get("to")
    if not isinstance(to, dict):
        return None
    h = " ".join(str(to.get("hash") or "").split())
    return h or None


def pons_version(factory) -> str | None:
    """
    工厂合约地址 → 版本名;**表外一律 None**(调用方退回 PONS_FALLBACK,不猜)。

    ⚠️ 封闭枚举比对,不做任何前缀/包含匹配 —— 这个项目上一轮的 BLOCKER 就是
       "手写模式匹配收本该枚举的东西"。
    """
    h = " ".join(str(factory or "").split()).lower()
    return PONS_FACTORIES.get(h)


# ============================================================
# 限速闸 —— **进程级**
# ============================================================
class _RateGate:
    """
    「任意两次 filterTokens 请求至少隔 _MIN_INTERVAL_SEC」。

    ⚠️⚠️ 做成模块级单例而不是每个 lookup 一个:poller 与 pumpfun 各持一个
       TokenExtraLookup,实例级的闸等于把 10 秒间隔悄悄变成 5 秒 ——
       而实测第 10 次连打就 429、冷却 203 秒。限流是**按 IP** 的,闸也必须按进程。
    ⚠️ 等不到就返回 False(那几行本轮消失),**绝不无限等** —— 它挂在 tick 的墙钟上。
    """

    def __init__(self, interval: float = _MIN_INTERVAL_SEC,
                 max_wait: float = _GATE_MAX_WAIT_SEC,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self._interval = float(interval)
        self._max_wait = float(max_wait)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0        # 下一次**允许**发请求的时刻

    def acquire(self) -> bool:
        """拿到闸 → True(并把下一次的时刻推后一个间隔);等超上限 → False。"""
        with self._lock:
            now = self._clock()
            wait = self._next_at - now
            if wait > self._max_wait:
                logger.debug("filterTokens 限速闸未就绪(还要等 {:.1f}s > {:.1f}s),本轮跳过",
                             wait, self._max_wait)
                return False
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next_at = now + self._interval
            return True

    def penalize(self, seconds: float) -> None:
        """429:把下一次允许的时刻整体推后。⚠️ 封禁期继续打不会延长,所以只推一次。"""
        s = max(0.0, min(float(seconds), _MAX_COOLDOWN_SEC))
        with self._lock:
            self._next_at = max(self._next_at, self._clock() + s)
        logger.warning("filterTokens 被限速(429),冷却 {:.0f}s —— 这段时间发射台/持有人两行消失", s)


# 进程级的那一把。⚠️ 测试要换掉它就注入自己的 gate(TokenExtraLookup(gate=...))。
_GATE = _RateGate()


def retry_after_seconds(headers, default: float = _DEFAULT_COOLDOWN_SEC) -> float:
    """429 的 retry-after → 秒。读不出来用默认值(实测真实值 203 秒)。"""
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            try:
                return max(1.0, min(float(str(v).strip()), _MAX_COOLDOWN_SEC))
            except (TypeError, ValueError):
                return default
    return default


# ============================================================
# HTTP 客户端 —— 只用公开免鉴权端点,异常一律自己吞掉
# ============================================================
class _CurlClient:
    """
    curl_cffi + impersonate="chrome" 的共同底座。

    ⚠️ 每线程一个 Session:libcurl 的 easy handle 不能被多线程同时使用
       (与 dexscreener.DexScreenerClient / pumpfun.PumpClient 同一条理由)。
    """

    def __init__(self, proxy: str | None = None) -> None:
        s = get_settings()
        self._proxy = s.fomo_proxy if proxy is None else proxy
        self._tl = threading.local()

    def _session(self):
        sess = getattr(self._tl, "session", None)
        if sess is None:
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


class FilterTokensClient(_CurlClient):
    """FOMO filterTokens。返回 (responseObject 列表, 429 冷却秒数);失败 (None, 0)。"""

    def fetch(self, keys: list[str]):
        """
        keys 形如 ["0xabc…:4663", "So111…:1399811149"](顺序是 address:chainId)。

        ⚠️⚠️ headers 里的 X-Supported-Chains **不许拿掉**:不带它,EVM 链返回
           200 + 空数组 —— 一个成功状态码 + 一个空结果,没有任何日志会说这是错的。
        """
        if not keys:
            return None, 0.0
        try:
            resp = self._session().post(
                FILTER_TOKENS_URL,
                json=list(keys),
                headers={
                    "Content-Type": "application/json",
                    "X-Supported-Chains": SUPPORTED_CHAINS,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("filterTokens 请求失败(本轮这几行不显示) | {} | {}", keys[0][:48], e)
            self.close()
            return None, 0.0
        if resp.status_code == 429:
            return None, retry_after_seconds(getattr(resp, "headers", None))
        if not (200 <= resp.status_code < 300):
            logger.warning("filterTokens 返回 HTTP {} | {}", resp.status_code, keys[0][:48])
            return None, 0.0
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("filterTokens 响应不是 JSON | {} | {}", keys[0][:48], e)
            return None, 0.0
        if isinstance(body, list):
            return body, 0.0
        if isinstance(body, dict):
            ro = body.get("responseObject")
            if isinstance(ro, list):
                return ro, 0.0
        logger.warning("filterTokens 响应结构不认识 | {}", type(body).__name__)
        return None, 0.0


class BlockscoutClient(_CurlClient):
    """Robinhood 链的 Blockscout。所有方法失败一律 None,不抛。"""

    def _get(self, path: str, tag: str):
        try:
            resp = self._session().get(f"{BLOCKSCOUT_BASE}{path}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Blockscout 请求失败 | {} | {}", tag, e)
            self.close()
            return None
        if not (200 <= resp.status_code < 300):
            # 少数代币确实 500(实测),这是常态不是异常 —— DEBUG 就够
            logger.debug("Blockscout 返回 HTTP {} | {}", resp.status_code, tag)
            return None
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("Blockscout 响应不是 JSON | {} | {}", tag, e)
            return None

    def token(self, addr: str):
        return self._get(f"/tokens/{addr}", f"tokens/{addr[:12]}")

    def address(self, addr: str):
        return self._get(f"/addresses/{addr}", f"addresses/{addr[:12]}")

    def transaction(self, tx: str):
        return self._get(f"/transactions/{tx}", f"tx/{tx[:12]}")


# ============================================================
# 带缓存 + 预算 + 限速的批量查询
# ============================================================
class TokenExtraLookup:
    """
    「这一批币的发射台与持有人分别是什么」。

    ⚠️ poller 与 pumpfun 各持一份(与 PoolQuoteLookup 同一条理由:共用会让
       "这一轮打了几个请求"变得不可预测),但**限速闸是共用的进程级单例**。
    ⚠️⚠️ 对外的两个方法(lookup / cached)都**不抛异常**:任何失败都只让那一行消失。
    """

    def __init__(self, filter_client=None, blockscout=None, conn_factory=None,
                 gate=None, holders_ttl: float = HOLDERS_TTL_SEC,
                 keys_per_request: int = MAX_KEYS_PER_REQUEST,
                 max_batches: int = MAX_BATCHES_PER_ROUND,
                 holders_budget: int = BLOCKSCOUT_HOLDERS_PER_ROUND,
                 pons_budget: int = BLOCKSCOUT_PONS_PER_ROUND,
                 wall_clock_sec: float = ROUND_WALL_CLOCK_SEC) -> None:
        self._filter = filter_client if filter_client is not None else FilterTokensClient()
        self._bs = blockscout if blockscout is not None else BlockscoutClient()
        self._conn_factory = conn_factory if conn_factory is not None else get_conn
        self._gate = gate if gate is not None else _GATE
        self._holders_ttl = float(holders_ttl)
        self._keys_per_request = max(1, int(keys_per_request))
        self._max_batches = max(1, int(max_batches))
        self._budget = {"holders": int(holders_budget), "pons": int(pons_budget)}
        self._wall = float(wall_clock_sec)
        # (net, addr) → (取到的时刻, 持有人数 或 None)
        self._holders: dict[tuple[str, str], tuple[float, int | None]] = {}
        self._used = {"holders": 0, "pons": 0}
        self._spent = 0.0

    def close(self) -> None:
        for c in (self._filter, self._bs):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def begin_round(self) -> None:
        """每 tick 开头调一次:次数与墙钟预算归零(与 namecn.NameGlossary 同一套)。"""
        self._used = {"holders": 0, "pons": 0}
        self._spent = 0.0

    # ---- 对外 --------------------------------------------------------------
    def lookup(self, pairs) -> dict[tuple[str, str], TokenExtra]:
        """
        [(链, 地址), …] → {(链, 归一化地址): TokenExtra}。查不到的键**不出现**。

        ⚠️ 整段包在 try 里:这个功能没有任何理由让一条推送发不出去。
        """
        try:
            return self._lookup(pairs)
        except Exception as e:  # noqa: BLE001
            logger.warning("发射台/持有人查询失败,本轮这两行不显示 | {}", e)
            return {}

    def cached(self, network_id, address) -> TokenExtra:
        """
        **只读缓存、绝不发请求**。给转入(/tin、转入聚合)推送用 ——
        与 dexscreener.PoolQuoteLookup.cached 同一条口径。
        """
        try:
            net = (network_id or "").strip()
            key = normalize_token_address(address)
            if key is None:
                return EMPTY
            lp = self._cached_launchpad(net, key)
            return TokenExtra(launchpad=lp, holders=self._cached_holders(net, key))
        except Exception as e:  # noqa: BLE001
            logger.warning("发射台/持有人读缓存失败 | {} | {}", network_id, e)
            return EMPTY

    # ---- 主流程 ------------------------------------------------------------
    def _lookup(self, pairs) -> dict[tuple[str, str], TokenExtra]:
        keys: list[tuple[str, str]] = []
        for net_raw, addr in pairs or ():
            net = (net_raw or "").strip()
            k = normalize_token_address(addr)
            if not net or k is None or net not in NETWORK_CHAIN_ID:
                continue                      # 认不出链就一个请求都不发
            if (net, k) not in keys:
                keys.append((net, k))
        if not keys:
            return {}

        launchpad: dict[tuple[str, str], str | None] = {}
        holders: dict[tuple[str, str], int | None] = {}
        need_fomo: list[tuple[str, str]] = []
        for nk in keys:
            net, _ = nk
            hit_lp, lp = self._glossary_get(KIND_LAUNCHPAD, f"{net}:{nk[1]}")
            if hit_lp:
                launchpad[nk] = lp
            h_hit, h = self._holders_cache_get(nk)
            if h_hit:
                holders[nk] = h
            # ⚠️ robinhood 的持有人走 Blockscout,**不因为它去打 filterTokens**;
            #    只有发射台没缓存时才需要。其余链两件事都在这一个请求里。
            need_holders = (not h_hit) and net != BLOCKSCOUT_NETWORK
            if not hit_lp or need_holders:
                need_fomo.append(nk)

        if need_fomo:
            self._fetch_fomo(need_fomo, launchpad, holders)

        # ---- robinhood:持有人用 Blockscout(FOMO 只作兜底)、pons 分版本 ----
        for nk in keys:
            if nk[0] != BLOCKSCOUT_NETWORK:
                continue
            if not self._holders_cache_get(nk)[0]:
                bs = self._blockscout_holders(nk)
                if bs is not None:
                    holders[nk] = bs
                    self._holders_cache_put(nk, bs)
                elif nk in holders:
                    # 兜底:Blockscout 拿不到时才用 FOMO 那个(已知会偏,见模块头坑 2)
                    self._holders_cache_put(nk, holders[nk])
            name = launchpad.get(nk)
            if name is not None and name.strip().lower() == PONS_LAUNCHPAD_NAME:
                launchpad[nk] = self._pons_version(nk[1])

        out: dict[tuple[str, str], TokenExtra] = {}
        for nk in keys:
            lp = launchpad.get(nk)
            h = holders.get(nk)
            if lp is not None or h is not None:
                out[nk] = TokenExtra(launchpad=lp, holders=h)
        return out

    def _fetch_fomo(self, need: list[tuple[str, str]], launchpad: dict, holders: dict) -> None:
        """攒成批次打 filterTokens。⚠️ 每批各过一次限速闸,拿不到就整批放弃。"""
        batches = [need[i:i + self._keys_per_request]
                   for i in range(0, len(need), self._keys_per_request)][:self._max_batches]
        if len(need) > self._keys_per_request * self._max_batches:
            logger.debug("filterTokens 本轮待查 {} 个,超过 {} 批的上限 —— 多出来的下一轮再说",
                         len(need), self._max_batches)
        for batch in batches:
            if not self._gate.acquire():
                return
            keys = [f"{addr}:{NETWORK_CHAIN_ID[net]}" for net, addr in batch]
            payload, cooldown = self._timed(self._filter.fetch, keys)
            if cooldown:
                self._gate.penalize(cooldown)
                return
            if payload is None:
                # 失败:**只写失败负缓存**,不写"查无"—— 两者 TTL 差 7 天
                for nk in batch:
                    self._glossary_put(KIND_LAUNCHPAD, f"{nk[0]}:{nk[1]}", None,
                                       "error", TTL_ERROR_SEC)
                continue
            found = parse_filter_tokens(payload)
            for nk in batch:
                extra = found.get(nk)
                if extra is None:
                    # 上游根本没回这个币 —— 与"回了但没有发射台"是两回事,按失败处理
                    self._glossary_put(KIND_LAUNCHPAD, f"{nk[0]}:{nk[1]}", None,
                                       "absent", TTL_ERROR_SEC)
                    continue
                launchpad[nk] = extra.launchpad
                self._glossary_put(
                    KIND_LAUNCHPAD, f"{nk[0]}:{nk[1]}", extra.launchpad,
                    "fomo" if extra.launchpad is not None else "miss",
                    # ⚠️ 成功 → **永久**(发射台是出生时定死的);查无 → 7 天
                    None if extra.launchpad is not None else TTL_MISS_SEC)
                if extra.holders is not None:
                    holders[nk] = extra.holders
                if nk[0] != BLOCKSCOUT_NETWORK:
                    # robinhood 的先不入缓存 —— 它要等 Blockscout 那一步(见 _lookup)
                    self._holders_cache_put(nk, extra.holders)

    # ---- Blockscout --------------------------------------------------------
    def _blockscout_holders(self, nk: tuple[str, str]) -> int | None:
        if not self._take("holders", nk[1]):
            return None
        payload = self._timed(self._bs.token, nk[1])
        if not isinstance(payload, dict):
            return None
        return parse_holders(payload.get("holders_count"))

    def _pons_version(self, addr: str) -> str:
        """
        robinhood 上 launchpadName == "pons" 的币 → "Pons" / "Pons V2"。

        ⚠️⚠️ 拿不到创建交易、或工厂不在封闭表里 → **退回 "Pons",绝不猜 V2**
           (实测 15.7% 的币拿不到创建交易的 to)。
        """
        hit, val = self._glossary_get(KIND_PONS_VER, addr)
        if hit:
            return val or PONS_FALLBACK
        if not self._take("pons", addr):
            return PONS_FALLBACK           # 预算用尽:这一轮先按 V1 显示,不写缓存
        tx = parse_creation_tx(self._timed(self._bs.address, addr))
        if tx is None:
            self._glossary_put(KIND_PONS_VER, addr, None, "no-creation-tx", TTL_MISS_SEC)
            return PONS_FALLBACK
        factory = parse_tx_to(self._timed(self._bs.transaction, tx))
        ver = pons_version(factory)
        if ver is None:
            self._glossary_put(KIND_PONS_VER, addr, None, "unknown-factory", TTL_MISS_SEC)
            return PONS_FALLBACK
        self._glossary_put(KIND_PONS_VER, addr, ver, "blockscout", None)   # 永久
        return ver

    # ---- 预算 --------------------------------------------------------------
    def _take(self, budget: str, tag: str) -> bool:
        """次数闸 + 墙钟闸,任一触发就这一轮不查(与 namecn._take 同一套)。"""
        if self._spent >= self._wall:
            logger.debug("发射台/持有人墙钟预算用尽({:.1f}s >= {:.1f}s),本轮跳过 | {} | {}",
                         self._spent, self._wall, budget, tag)
            return False
        if self._used[budget] >= self._budget[budget]:
            logger.debug("发射台/持有人次数预算用尽({}/{}),本轮跳过 | {} | {}",
                         self._used[budget], self._budget[budget], budget, tag)
            return False
        self._used[budget] += 1
        return True

    def _timed(self, fn, *args):
        """调一次外部接口并把耗时记进本轮墙钟账。⚠️ 失败也要记 —— 超时最费时间。"""
        t0 = time.monotonic()
        try:
            return fn(*args)
        finally:
            self._spent += time.monotonic() - t0

    # ---- 缓存 --------------------------------------------------------------
    def _holders_cache_get(self, nk) -> tuple[bool, int | None]:
        hit = self._holders.get(nk)
        if hit is None or time.time() - hit[0] >= self._holders_ttl:
            return False, None
        return True, hit[1]

    def _holders_cache_put(self, nk, value: int | None) -> None:
        self._holders[nk] = (time.time(), value)
        if len(self._holders) > 4000:
            for k, _ in sorted(self._holders.items(), key=lambda kv: kv[1][0])[:1000]:
                del self._holders[k]

    def _cached_holders(self, net: str, key: str) -> int | None:
        return self._holders_cache_get((net, key))[1]

    def _cached_launchpad(self, net: str, key: str) -> str | None:
        hit, val = self._glossary_get(KIND_LAUNCHPAD, f"{net}:{key}")
        if not hit or val is None:
            return None
        if net == BLOCKSCOUT_NETWORK and val.strip().lower() == PONS_LAUNCHPAD_NAME:
            # ⚠️ 只读缓存路径:分不出版本就退回 "Pons",**绝不为它发请求**
            v_hit, ver = self._glossary_get(KIND_PONS_VER, key)
            return (ver or PONS_FALLBACK) if v_hit else PONS_FALLBACK
        return val

    def _glossary_get(self, kind: str, key: str) -> tuple[bool, str | None]:
        """→ (有没有未过期的行, value)。读失败当没有(下一轮重查)。"""
        try:
            from src import store

            with self._conn_factory() as conn:
                row = store.glossary_get(conn, kind, key, time.time())
        except Exception as e:  # noqa: BLE001
            logger.warning("发射台缓存读取失败,当作未缓存 | {} | {}", kind, e)
            return False, None
        return (False, None) if row is None else (True, row["value"])

    def _glossary_put(self, kind: str, key: str, value: str | None, source: str,
                      ttl: float | None) -> None:
        try:
            from src import store

            with self._conn_factory() as conn:
                store.glossary_put(conn, kind, key, value, source,
                                   None if ttl is None else int(time.time() + ttl))
                store.glossary_prune(conn)
        except Exception as e:  # noqa: BLE001
            logger.warning("发射台缓存写入失败(下轮重查) | {} | {}", kind, e)
