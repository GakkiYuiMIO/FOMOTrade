"""
FOMO API 客户端 —— 双实现(设计文档 §2.1 端点表 / §2.2 鉴权 / §3.6 双 Client)

  HttpFomoClient       curl_cffi + Chrome TLS 指纹直连,轻量、快、内存占用小
  PlaywrightFomoClient 常驻 headless 浏览器,在 fomo.family 页面上下文里 fetch(兜底,吃 ~300MB)

⚠️ 已实测:**匿名请求会被 Cloudflare WAF 403**(返回 "Attention Required!" 的 HTML 页面)。
   带上合法 Bearer token 后能否放行**尚未验证** —— 这正是 `--probe` 要回答的第一个问题。
   因此 403 的错误信息里必须区分「Cloudflare 拦截」和「鉴权失败」:
   前者要改 FOMO_CLIENT_IMPL=playwright,后者要重新 --login,用户的处置完全不同。

⚠️ §十一 里的字段名/分页参数**一项都没实测过**。本文件的原则:
   1) 响应结构用 _as_list / _as_obj 多形态兜底,不写死 {"data": [...]}
   2) 任何一类数据拉取失败都只降级(置 None)、绝不炸掉整个 tick
   3) 拿不准的地方标 TODO(probe #N),N 对应设计文档 §十一 的编号
"""
from __future__ import annotations

import json
import random
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode

from loguru import logger

from src.auth import USER_AGENT, AuthError, TokenProvider, get_token_provider, load_session
from src.config import get_settings
from src.models import pick

BASE_URL = "https://prod-api.fomo.family"
FOMO_ORIGIN = "https://fomo.family"

# ---- 端点(逆向前端 bundle 得到,设计文档 §2.1) ----
EP_USER_BY_HANDLE = "/v2/users/userHandle/{handle}"
# 当前登录账号自己。⚠️ 是 current 不是 me:/v2/users/me 会被当成 userId 校验(400),
# /me、/users/me、/auth/me、/v2/profile 全是 404。
EP_CURRENT_USER = "/v2/users/current"
EP_USER = "/v2/users/{uid}"
EP_SWAPS = "/v2/users/{uid}/swaps"
EP_BALANCES = "/v2/users/{uid}/balances"
# 持仓单(含已平仓的)。orderBy 只接受 'closedAt' / 'realizedPnlUsd' 两个值
EP_TRADES = "/trades"
EP_FOLLOWING = "/v2/users/{uid}/followingPaginate"
# 榜单。period ∈ {24h, 7d, 30d, following};⚠️ limit 必传,不带直接 400
EP_LEADERBOARD = "/v2/leaderboard/{period}"
# ⚠️ 服务端硬上限 100,传 201 会直接 400 —— 实测出来的,不要改大
_FOLLOWING_PAGE = 100
# 某人的**全部**转账流水(与任意第三方之间的,不限于"我与他")。
# ⚠️ 这条注释更正了一个长期的错误结论。此前记的是"FOMO 不提供转账查询" ——
#    那句话只对 /v2/transfers/with/{uid} 成立:它的语义确实是「**我**与该用户之间的转账」
#    (对自己调返回 400 "Cannot fetch transfers with self"),拿不到别人与第三方的转账。
#    但 /v2/users/{uid}/transfers 是**另一个端点**,给的正是该用户与任意地址之间的全部
#    充提流水。2026-08-26 实测 91 人 × 100 条全部 200,字段见 poller._transfer_to_event。
#    这个区别很要命:整整一类信号(项目方/内部人把筹码分给名单里的人)曾经因此完全看不见。
EP_TRANSFERS = "/v2/users/{uid}/transfers"
# ⚠️ thesis 只能**按代币**查。实测 /feed/user/thesis → 404、/v2/users/{uid}/thesis → 不存在,
#    FOMO 根本没有"按用户查观点"的端点。所以观点的采集方式是:
#    遍历监控用户持仓里的币 → 按币拉 thesis → 按 userId 过滤出监控对象(见 poller._collect_thesis)。
EP_TOKEN_THESIS = "/feed/token/thesis"

# 某个币的**持有人榜**(/chips 的分子)。tokens 参数是 URL 编码后的 JSON 数组,
# 元素形如 {"address": "<CA>", "networkId": <数字链 ID>}。
# ⚠️⚠️ 服务端有两道**静默**过滤,不理解它们就会把"下界"当成"精确值"报给用户:
#   1) 条数硬钳 100:limit 传 100/101/500/1000 返回完全一样;offset / page / skip /
#      cursor 四种分页参数**全部被静默忽略**(实测四种参数下首条 tradeId 纹丝不动)——
#      不是参数名没猜对,是这个端点根本没有分页能力。别再去试分页。
#   2) 按持仓**市值**过滤,门槛约 $2:所以 totalHolders=113 的币可能只返 8 条。
#      (这个 $2 是从多个币末位 value 落在 $2.20–$2.72 推断出来的**强推断,不是实锤**。)
# 于是只有 len(topHolders) == totalHolders 时统计才是精确的,否则只能当**下界**用 ——
# 这个判据是 /chips 全部文案的支点,见 bot._chips_stats。
EP_TOP_HOLDERS = "/hodlers/top"
# ⚠️ 服务端硬钳,传更大的值没有任何效果(见上)。写成常量是为了让调用方把
#    "我们请求了多少"和"实际返回了多少"放在一起判断,而不是两处各写各的字面量。
TOP_HOLDERS_LIMIT = 100

# 代币元数据批量查询(/chips 的分母:总供应量)。body 是**字符串数组**
# ["<address>:<networkId>"],数据在 responseObject[0].token.info.totalSupply。
# ⚠️ 这条**不需要 Authorization**(实测不带任何令牌返回 200)。好处是实打实的:
#    分母这一步不消耗监控进程共用的那份登录态。
# ⚠️ 但"不要令牌"绝不等于"随便怎么调都行":必须过 Cloudflare 的 TLS 指纹检查。
#    实测 urllib 裸调 **HTTP 430**,curl_cffi + impersonate="chrome" 才 200。
#    下一个人看到"匿名可调"很容易顺手换成 httpx/urllib,那会直接 430。
EP_FILTER_TOKENS = "/public/proxy/filterTokens"

# 「我关注的人」的活动流。买/卖/观点三类混在一条流里,一次调用就知道名单里谁刚动过。
# 详见 _BaseFomoClient.get_activity_feed 的说明与实测数据。
EP_ACTIVITY_FEED = "/feed/tradingActivity"

# TODO(probe #15): 取值格式未实测。前端是 getChains() 的返回值,可能是逗号分隔 slug、
#                  也可能是 JSON 数组或链 ID。改这个值还可能影响响应里 networkId 的表示(同 probe #10)。
# ⚠️ 必须是**数字链 ID**,不是链名。实测(2026-08-11 抓 fomo.family 网页版真实请求头):
#     x-supported-chains: 1,56,143,4663,8453,1399811149
# 写成 "solana,base,bsc" 时服务端解析不了,**不报错、直接把结果全过滤成空数组** ——
# /trades /watchlist /v2/users/{id}/swaps /feed/tradingActivity 全部返回 0 条,
# 看起来像"这些端点没数据"或"权限不够",极难定位。踩过一次,别再改回链名。
#   1=Ethereum  56=BSC  143=Monad  4663=?  8453=Base  1399811149=Solana
SUPPORTED_CHAINS = "1,56,143,4663,8453,1399811149"

# TODO(probe #5): 分页参数名(limit/offset/cursor/before)与单页上限全是猜的
_PAGE_SIZE = 50
_MAX_PAGES = 20          # 设计文档 §8.3 的内部硬上限
# transfers 单页条数。⚠️ 稳态下每轮只取第一页、不翻页,所以这个数字的意义是
#    "一次要覆盖多久的余量"。实测名单中位数 1.67 条/人/小时,而采集降频到 5 分钟一轮
#    (poller._TRANSFERS_EVERY_N_TICKS),25 条相当于约 15 小时余量 —— 漏采只可能发生在
#    停机之后,而那由 _catchup_since 兜住。默认 100 条约 70KB,25 条约 1/4,白省的流量。
_TRANSFERS_PAGE = 25
# ⚠️ 超时不是"越宽容越好":它发生在线程池的一个 worker 里,一个卡死的请求会占住
#    整整一个并发名额。实测正常响应 p50 0.3-1.2s,25s 的余量只会把单轮拖到分钟级。
_TIMEOUT_SEC = 12.0
# 单次请求的总尝试次数上限,防止退避循环无限打转。
# ⚠️ 5 次线性退避最坏要 sleep 1.5+3+4.5+6 = 15s,而这 15s 全部计入单轮耗时 ——
#    日志里那些 79s 的 tick 就是这么来的。balances 现在有缓存兜底、失败的代价小得多,
#    3 次(最坏 sleep 4.5s)是重试价值与单轮耗时的更好平衡。
_MAX_ATTEMPTS = 3
_BACKOFF_SEC = 1.5
# 退避抖动幅度。⚠️ 没有抖动时,同时打出去的十几个请求会在同一毫秒一起重试 ——
#    真实日志里出现过 5 个 balances 在 22:38:17 同秒 504、又在同秒一起重试,
#    等于把瞬时压力原样重放一遍。
_JITTER = 0.35


def _backoff(attempt: int) -> float:
    """第 attempt 次失败后要等多久(带抖动)"""
    return _BACKOFF_SEC * attempt * random.uniform(1 - _JITTER, 1 + _JITTER)


# ============================================================
# 停机信号
# ============================================================
# ⚠️ 没有这个信号时,Ctrl+C 要等十几秒到几十秒才真的退出:
#    调度器停了,但线程池里十几个 worker 还卡在重试退避的 sleep 里、
#    或者卡在一个 12s 的 HTTP 超时上;ThreadPoolExecutor 的 atexit 钩子会 join 它们,
#    于是进程一直挂着,用户只能连按好几次 Ctrl+C。
#    这里给一个全局 Event:退避改成可打断的 wait(),排队中的请求直接放弃。
_STOP = threading.Event()


def request_stop() -> None:
    """通知所有在途请求尽快放弃(cli 收到 Ctrl+C 时调用)"""
    _STOP.set()


def stop_requested() -> bool:
    return _STOP.is_set()


def reset_stop() -> None:
    """单测用:Event 是模块级全局,不重置会污染后续用例"""
    _STOP.clear()


def sleep_or_stop(seconds: float) -> bool:
    """
    可打断的等待。返回 True 表示"别等了,要停机了"。

    ⚠️ 凡是**以秒计**的 sleep 都该走这里,不只是重试退避 ——
       poller 的推送节流同样会一次等好几十秒,用 time.sleep 就等于按了 Ctrl+C 也走不掉。
    """
    return _STOP.wait(seconds)


# ============================================================
# 瞬时故障计数(日志降噪)
# ============================================================
# ⚠️ 每次 5xx/429 重试都打一条 WARNING,一轮就是几十行刷屏 —— 而这些**已经被处理掉了**
#    (重试 + balances 有缓存兜底)。用户真正需要知道的是"这一轮有多少次、哪个端点",
#    不是每一次。所以逐条降到 DEBUG,由 poller 每轮汇总成一行。
#    上游抖动是这个 API 的常态(实测 /balances 从 p50 1.2s 劣化到单次 10s 过),
#    刷屏的后果是真正要紧的 ERROR 被淹掉。
_ERRS: dict[str, int] = {}
_ERRS_LOCK = threading.Lock()


def _note_transient(kind: str) -> None:
    with _ERRS_LOCK:
        _ERRS[kind] = _ERRS.get(kind, 0) + 1


def take_transient_errors() -> dict[str, int]:
    """取走并清空本轮的瞬时故障计数(poller 每轮末尾调一次)"""
    with _ERRS_LOCK:
        out = dict(_ERRS)
        _ERRS.clear()
        return out


# Cloudflare 拦截页的特征词。
# ⚠️ 绝不能只看 cf-ray 响应头:prod-api 整站都在 Cloudflare 后面,合法的 401 也带这个头。
#    必须是「HTML 页面 + 拦截文案」同时命中才算 WAF 拦截。
_CF_MARKERS = ("attention required", "cloudflare", "just a moment", "cf-error", "ray id")


class FomoAPIError(Exception):
    """FOMO API 调用失败。message 里必须说清是 Cloudflare 拦截还是鉴权失败。"""


class UserGoneError(FomoAPIError):
    """
    上游明确说这个用户不存在了(HTTP 404)。

    ⚠️ 与 FomoAPIError 分开是**必须的**:后者的语义是"这次没成功,下次再试",
       而这个的语义是"再试一万次也是这个结果"。混在一起的后果实测过 ——
       两个被删掉的账号被每 15 秒重试一次,连续 10 天约 11.5 万次注定失败的请求,
       日志里每轮刷一行"上游抖动",而真正的原因(账号没了)一次都没说出口。
    """


class NotSupportedError(Exception):
    """当前 client 实现不支持该能力(PlaywrightFomoClient 不支持 swaps 分页)"""


class _TransportError(Exception):
    """传输层失败(超时 / DNS / 浏览器崩溃)。内部用,不外泄 —— 对外一律包成 FomoAPIError。"""


@dataclass
class UserSnapshot:
    """
    一个用户本 tick 的四类原始数据。

    ⚠️ None 与 [] 语义**完全不同**:None = 这一项拉取失败,[] = 拉到了但确实没有。
       count_holders 靠这个区分来决定「仍持有」整段要不要消失(设计文档 §8.4),
       把失败当空列表会让共识副指标凭空少人。
    """

    user_id: str
    swaps: list[dict] | None = None
    transfers: list[dict] | None = None
    thesis: list[dict] | None = None
    balances: list[dict] | None = None
    # 持仓单(含已平仓)。已实现盈亏与"剩余 $0.00"只能从这里拿 ——
    # 清仓后 balances 里就没有那个币了
    trades: list[dict] | None = None


class FomoClient(Protocol):
    """两个实现的公共协议。上层 poller / bot 只依赖这个,换实现只改一行配置。"""

    def resolve_handle(self, handle: str) -> tuple[str, str, str]: ...
    def get_current_user(self) -> dict: ...
    def get_swaps(self, user_id: str, limit: int = 50) -> list[dict]: ...
    def get_transfers(self, user_id: str, limit: int = _TRANSFERS_PAGE) -> list[dict]: ...
    def iter_transfers(self, user_id: str, max_items: int) -> Iterator[dict]: ...
    def get_token_thesis(self, token_address: str, network_id, after_ms: int | None = None,
                         limit: int = 100) -> list[dict]: ...
    def get_top_holders(self, token_address: str, network_id) -> dict: ...
    def get_token_meta(self, token_address: str, network_id) -> dict: ...
    def get_balances(self, user_id: str) -> list[dict]: ...
    def get_trades(self, user_id: str) -> list[dict]: ...
    def get_activity_feed(self, limit: int = 100) -> list[dict]: ...
    def get_following(self, user_id: str, max_items: int = 300) -> list[dict]: ...
    def get_leaderboard(self, period: str = "24h", limit: int = 20) -> list[dict]: ...
    def iter_swap_buys(self, user_id: str, max_items: int) -> Iterator[dict]: ...
    def raw_get(self, path: str, params: dict | None = None) -> tuple[int, object, dict]: ...
    def fetch_snapshot(self, user_id: str) -> UserSnapshot: ...


# ============================================================
# 响应解析工具
# ============================================================
def _auth_headers(token: str) -> dict[str, str]:
    """公共请求头。前端 fomoFetch 只带前三个,其余是让请求看起来像浏览器发的。"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Supported-Chains": SUPPORTED_CHAINS,
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": FOMO_ORIGIN,
        "referer": FOMO_ORIGIN + "/",
        "user-agent": USER_AGENT,
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _parse_json(text: str, path: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise FomoAPIError(f"{path} 返回的不是 JSON: {text[:200]!r}") from e


# 列表可能藏在这些键下面。probe 确认真实结构后可以收窄,但保留兜底不会有坏处。
_LIST_KEYS = (
    "data", "items", "results", "records", "list", "rows",
    "swaps", "transfers", "balances", "theses", "thesis", "feed", "tokens",
)


def _unwrap(payload):
    """
    剥掉 FOMO 的统一响应信封。

    实测(2026-08-11)所有端点都是这个形状:
        {"success": true, "message": "...", "responseObject": <真正的数据>, "statusCode": 200}

    ⚠️ 这一步必须显式做。原来只靠 _LIST_KEYS 里的通用候选键去猜,猜不中 responseObject,
       于是 get_swaps() 恒返回 0 条,而同一个请求 raw_get() 明明能拿到 100 条 ——
       表现为"能连通、不报错、但永远没有新事件",比直接报错难查得多。
    """
    if isinstance(payload, dict) and "responseObject" in payload:
        return payload["responseObject"]
    return payload


def _as_list(payload, _depth: int = 0) -> list[dict]:
    """
    把响应压成 list[dict]。裸数组 / {"responseObject": {"swaps": [...]}} / {"data": [...]} 都吃。

    ⚠️ 解析不出来返回 [] 而不是抛异常 —— 上层拿到空列表只是「本轮没有新事件」,
       拿到异常则会把整个用户的这一类数据判为失败。
    """
    payload = _unwrap(payload)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and _depth < 3:
        for k in _LIST_KEYS:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = _as_list(v, _depth + 1)
                if inner:
                    return inner
        # 信封里只有一个数组时不必猜键名(如 {"swaps": [...], "hasNextPage": false})
        arrays = [v for v in payload.values() if isinstance(v, list)]
        if len(arrays) == 1:
            return [x for x in arrays[0] if isinstance(x, dict)]
    return []


def _as_obj(payload) -> dict:
    """把响应压成单个对象。{"responseObject": {...}} / {"data": {...}} / 裸对象都吃。"""
    payload = _unwrap(payload)
    if isinstance(payload, dict):
        for k in ("data", "user", "result", "item", "profile"):
            v = payload.get(k)
            if isinstance(v, dict):
                return v
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _as_network_number(network_id):
    """
    链 ID → JSON 里该写的形态。数字串写成**数字**,写不成数字的原样透传。

    ⚠️ 与 get_token_thesis 踩过的坑同源(传 "bsc" 直接 400):这些端点认的是 FOMO 原生的
       数字链 ID。这里只做"能转数字就转",不做别名映射 —— 别名表在 bot._NETWORK_RAW_ID,
       一份表放两个地方早晚会走岔;而原样透传能让服务端的报错说话,不被我们吞掉。
    """
    try:
        return int(str(network_id).strip())
    except (TypeError, ValueError):
        return network_id


def fetch_token_meta(token_address: str, network_id) -> dict:
    """
    匿名查代币元数据(/chips 的分母来源)。返回 responseObject 里对应这个币的那个对象,
    拿不到返回 {}。总供应量在 `.token.info.totalSupply`,用 total_supply_of() 取。

    ⚠️ 刻意**不走** _fetch_ok:那条路径带 Bearer、会重试、会在 401/403 时调
       tokens.invalidate()。本请求根本不需要令牌,让它去作废一份好端端的登录态
       是纯粹的伤害。失败就失败,调用方自己有本地兜底。
    ⚠️ 必须用 curl_cffi + impersonate="chrome":Cloudflare 认 TLS 指纹,
       urllib/httpx 裸调实测 HTTP 430(见 EP_FILTER_TOKENS)。
    ⚠️ 不重试:这是命令层的同步路径,用户在等一条回执;一次没拿到就走本地推算,
       比让他多等几秒好。
    """
    key = f"{token_address}:{_as_network_number(network_id)}"
    try:
        from curl_cffi import requests as cffi_requests

        settings = get_settings()
        resp = cffi_requests.post(
            BASE_URL + EP_FILTER_TOKENS,
            json=[key],
            impersonate="chrome",
            proxies=settings.proxies,
            timeout=_TIMEOUT_SEC,
            headers={
                "Content-Type": "application/json",
                "accept": "application/json, text/plain, */*",
                # ⚠️⚠️ **这个头不许拿掉。** 不带它,EVM 链(robinhood/base/bsc)返回
                #    **HTTP 200 + responseObject: []** —— 成功状态码配一个空数组,
                #    最阴的失败形态:没有任何错误码、没有任何日志会说它错了。
                #    上一版这里就是缺它,于是 /chips 对 robinhood 链的分母**一直**取不到,
                #    静默退化成本地推算。与 _headers() 里那一份同源(SUPPORTED_CHAINS)。
                "X-Supported-Chains": SUPPORTED_CHAINS,
                "origin": FOMO_ORIGIN,
                "referer": FOMO_ORIGIN + "/",
                "user-agent": USER_AGENT,
            },
        )
        if not (200 <= resp.status_code < 300):
            logger.warning("代币元数据查询 HTTP {} | {}", resp.status_code, key[:40])
            return {}
        ro = _unwrap(json.loads(resp.text or "null"))
    except Exception as e:  # noqa: BLE001
        logger.warning("代币元数据查询失败 | {} | {}", key[:40], e)
        return {}

    if isinstance(ro, list):
        ro = next((x for x in ro if isinstance(x, dict)), None)
    return ro if isinstance(ro, dict) else {}


def total_supply_of(meta: dict) -> float | None:
    """
    从 fetch_token_meta 的返回里取总供应量。取不到返回 None,**绝不返回 0** ——
    0 会被下游当成"供应量真的是 0"从而算出荒唐的占比,而 None 让那一行整行消失。
    """
    token = meta.get("token") if isinstance(meta, dict) else None
    info = token.get("info") if isinstance(token, dict) else None
    raw = info.get("totalSupply") if isinstance(info, dict) else None
    try:
        supply = float(raw)
    except (TypeError, ValueError):
        return None
    # 0 / 负数不是"供应量",是脏数据 —— 当没拿到处理,而不是让它去当除数
    return supply if supply > 0 else None


def _is_cloudflare_block(text: str) -> bool:
    """是不是 Cloudflare 的 WAF 拦截页(而不是 API 自己返回的 401 JSON)"""
    body = (text or "")[:4000].lower()
    # ⚠️ 不能硬性要求 <html:Cloudflare 也会返回 JSON 错误体、1020 纯文本、
    #    甚至空 body 的 403。硬卡 <html 会把这些漏判成"鉴权问题",
    #    让用户白折腾 --login。标志词本身已经足够特异,单独匹配即可。
    return any(m in body for m in _CF_MARKERS)


def _retry_after(headers: dict, default: float = 3.0) -> float:
    """
    429 的退避秒数。上限 60s —— 一个畸形的头不该把整个 tick 卡死。

    ⚠️ 必须带抖动。实测过一次真实的雷群:同一毫秒里 15 个请求
       (balances × 10、thesis × 5、trades × 1)一起 429,服务端给的 retry-after 又完全相同,
       于是它们又在同一毫秒一起重试 —— 等于把刚才那波压力原样重放一遍,
       而且每一轮都会再撞一次。抖动是打散雷群的唯一手段。
    """
    wait = default
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            try:
                wait = max(1.0, min(float(v), 60.0))
            except (TypeError, ValueError):
                wait = default
            break
    return wait * random.uniform(1 - _JITTER, 1 + _JITTER)


def _item_identity(item: dict) -> str:
    """翻页去重用的弱标识。没有稳定 id 时退化为整条报文的字符串形态。"""
    native = pick(item, "id", "_id", "swapId", "txHash", "transactionHash", "signature", "hash")
    return str(native) if native else json.dumps(item, sort_keys=True, default=str)[:512]


def _pick_id(item: dict) -> str | None:
    """
    取一条记录的原生 id,用作分页游标(lastSwapIdV2)。

    ⚠️ 只认真正的 id,**不接受 txHash 之类的兜底** —— 服务端拿它当游标查,
       给错了会静默返回第一页,又退化成"永远只有 50 条"。
    """
    v = pick(item, "id", "_id", "swapId")
    return str(v) if v else None


# ============================================================
# 公共实现:两个 client 只在「怎么发一个 GET」上不同
# ============================================================
class _BaseFomoClient:
    """端点拼装、重试策略、响应解析都在这里;子类只需实现 _request()。"""

    # iter_swap_buys 上一次翻页是否走到底。False = 分页参数疑似失效、基线只覆盖了第一页,
    # seeding 据此调整回执文案 —— 绝不能在只回填了 50 条时报"基线完成"
    _last_paging_ok: bool = True
    # http 实现天生可并发;playwright 每线程要开一整套浏览器,必须串行(见 poller._fetch_snapshots)
    supports_concurrency: bool = True

    def __init__(self, token_provider: TokenProvider | None = None) -> None:
        self._tokens = token_provider or get_token_provider()
        # get_thesis 探到的可用**模板**(不是拼好的路径 —— 拼好的带 uid,换个用户就错了)
        self._thesis_tpl: str | None = None

    # ---------- 子类实现 ----------
    def _request(self, path: str, params: dict | None = None) -> tuple[int, str, dict]:
        """发一个带鉴权的 GET,返回 (status_code, body_text, headers)。传输失败抛 _TransportError。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放资源(连接池 / 浏览器)。默认无操作。"""

    # ---------- 重试与错误分类 ----------
    def _fetch_ok(self, path: str, params: dict | None = None) -> str:
        """
        取一个 2xx 的响应体,否则抛 FomoAPIError。

        ⚠️ AuthError 直接上抛不拦截:登录态挂掉是全局问题,
           包成 FomoAPIError 会让 poller 以为只是某个接口抖动,继续空转刷日志,
           而设计文档 §3.5 要求的是「告警需要重新登录 + 停止轮询」。
        """
        if stop_requested():
            raise FomoAPIError(f"{path} 停机中,未发出")
        attempt = 0
        auth_retried = False
        # ⚠️ 记住"这一路上见过 401/403"。401 若正好落在**最后一次**尝试上,
        #    下面那个 `continue` 会直接把循环耗尽,走到函数末尾抛 FomoAPIError ——
        #    而 FomoAPIError 会被 _fetch_snapshots 的 `except Exception` 吞成"该项降级为 None",
        #    tick 照常返回、last_tick_at 照常前进、/status 显示一切正常。
        #    结果正是本 docstring 要避免的那种失效:一个看起来完全健康的、死掉的监控。
        #    _MAX_ATTEMPTS 从 5 降到 3 之后,凑齐"前面两次失败 + 最后一次 401"的门槛低了不少,
        #    而这个 API 一次抖动就能甩出一串 504/429。
        auth_status: int | None = None
        while attempt < _MAX_ATTEMPTS:
            attempt += 1
            try:
                status, text, headers = self._request(path, params)
            except _TransportError as e:
                _note_transient("传输失败")
                if attempt < _MAX_ATTEMPTS and not stop_requested():
                    wait = _backoff(attempt)
                    logger.debug("请求传输失败,{:.1f}s 后重试 | {} | {}", wait, path, e)
                    if sleep_or_stop(wait):
                        raise FomoAPIError(f"{path} 停机中,放弃重试") from e
                    continue
                raise FomoAPIError(f"{path} 传输失败: {e}") from e

            if 200 <= status < 300:
                return text

            if status == 429:
                _note_transient("限流 429")
                wait = _retry_after(headers)
                if attempt < _MAX_ATTEMPTS and not stop_requested():
                    logger.debug("FOMO 限流 429,{:.1f}s 后重试 | {}", wait, path)
                    if sleep_or_stop(wait):
                        raise FomoAPIError(f"{path} 停机中,放弃重试")
                    continue
                raise FomoAPIError(f"{path} 持续 429 限流")

            if status in (401, 403):
                auth_status = status
                # ⚠️ 这两支都必须抛 AuthError,**不能抛 FomoAPIError**。
                #    fetch_snapshot 只对 AuthError 显式上抛,FomoAPIError 会落进
                #    `except Exception` 把三个分项置 None → tick 正常返回 0 →
                #    last_tick_at 照常前进 → /status 显示"一切正常"。
                #    结果是一个看起来完全健康的、死掉的监控 —— 这正是本函数
                #    docstring 里说要避免的那种失效。
                if _is_cloudflare_block(text):
                    # 换 token 解决不了 WAF,重试只是浪费时间,直接抛出并告诉用户怎么办
                    raise AuthError(
                        f"{path} 被 Cloudflare WAF 拦截(HTTP {status})—— 不是鉴权问题。"
                        f"请把 .env 里的 FOMO_CLIENT_IMPL 改成 playwright 重试。"
                    )
                if not auth_retried:
                    auth_retried = True
                    logger.warning("HTTP {} 疑似 token 失效,续期后重试一次 | {}", status, path)
                    self._tokens.invalidate()
                    continue
                raise AuthError(
                    f"{path} 鉴权失败(HTTP {status},续期后仍失败,非 Cloudflare 拦截):"
                    f"{(text or '')[:200]} —— 请重新执行 --login"
                )

            if status >= 500:
                _note_transient(f"服务端 {status}")
            if status >= 500 and attempt < _MAX_ATTEMPTS and not stop_requested():
                wait = _backoff(attempt)
                logger.debug("FOMO 服务端错误 {},{:.1f}s 后重试 | {}", status, wait, path)
                if sleep_or_stop(wait):
                    raise FomoAPIError(f"{path} 停机中,放弃重试")
                continue

            if status == 404:
                # ⚠️ 不记 _note_transient:404 不是抖动,把它混进"上游抖动"的汇总里
                #    会让一个永久故障看起来像网络问题,从而永远没人去处理它。
                raise UserGoneError(f"{path} HTTP 404: {(text or '')[:200]}")
            raise FomoAPIError(f"{path} HTTP {status}: {(text or '')[:300]}")

        if auth_status is not None:
            # 重试次数耗尽,但这一路上出现过鉴权失败 —— 必须以 AuthError 收场(见循环前的说明)
            raise AuthError(
                f"{path} 鉴权失败(HTTP {auth_status},重试 {_MAX_ATTEMPTS} 次耗尽)"
                f" —— 请重新执行 --login"
            )
        raise FomoAPIError(f"{path} 重试 {_MAX_ATTEMPTS} 次仍失败")

    def _get(self, path: str, params: dict | None = None):
        return _parse_json(self._fetch_ok(path, params), path)

    # ---------- 端点 ----------
    def resolve_handle(self, handle: str) -> tuple[str, str, str]:
        """
        handle → (user_id, display_name, 规范大小写的 handle)。

        ⚠️ URL 里**不做 lower()**:store.normalize_handle 的小写化是为了 DB 主键唯一,
           而 FOMO 的这个查询端点是否大小写敏感未知,擅自转小写可能查不到人。
        """
        h = (handle or "").strip().lstrip("@").strip()
        if not h:
            raise FomoAPIError("handle 为空")
        payload = self._get(EP_USER_BY_HANDLE.format(handle=quote(h, safe="")))
        obj = _as_obj(payload)
        uid = pick(obj, "id", "userId", "user_id", "_id", "uid")
        if not uid:
            raise FomoAPIError(f"响应里找不到 userId | handle={h} | {json.dumps(obj, default=str)[:300]}")
        display = pick(obj, "displayName", "display_name", "name", "username", "userHandle", "handle") or h
        # ⚠️ 回传 API 侧的**规范大小写** handle,而不是用户在 /add 里敲的那个 ——
        #    handle 要显示给人看、还要拿去搜,@GakkiYuiTifa 和 @gakkiyuitifa 观感差很多。
        canonical = pick(obj, "userHandle", "handle", "username") or h
        return str(uid), str(display), str(canonical).lstrip("@")

    def get_swaps(self, user_id: str, limit: int = 50) -> list[dict]:
        # TODO(probe #1/#2/#3/#4): networkId 是否存在、唯一 id 字段名、时间单位、买卖方向表示
        return _as_list(self._get(EP_SWAPS.format(uid=quote(user_id, safe="")), {"limit": limit}))

    def get_transfers(self, user_id: str, limit: int = _TRANSFERS_PAGE) -> list[dict]:
        """
        某人的充提流水(与任意第三方之间的转账),按 createdAt **降序**。

        实测(2026-08-26)每条形如:
          {"id": uuid, "type": "DEPOSIT" | "WITHDRAWAL",
           "fromAddress": …, "toAddress": …, "isNativeToken": bool,
           "tokenAddress": …(原生币为 null), "networkId": 1399811149,
           "humanAmount": 12000000, "tokenAmountString": "12000000000000",
           "usdAmount": 2439.09, "createdAt": "2026-08-26T00:37:40.579Z",
           "tokenMetadata": {"symbol": "fih", "imageLargeUrl": …}}

        ⚠️ 报文里**没有** marketCap / txHash / 对手方的 userId ——
           市值只能从本 tick 的 balances 索引补,对手方只有钱包地址(见 poller)。
        ⚠️ 这里**不包含** FOMO 上买卖产生的腿。铁证:CryptoTalkMan 在 FOMO 上买过两笔
           $fih,而他这个币的 transfers 返回 0 条 —— 所以"同一笔交易被推两遍"不会发生。
        """
        path = EP_TRANSFERS.format(uid=quote(user_id, safe=""))
        return _as_list(self._get(path, {"limit": max(1, int(limit))}))

    def iter_transfers(self, user_id: str, max_items: int) -> Iterator[dict]:
        """
        分页迭代转账流水(冷启动补数用)。稳态采集只调 get_transfers 取第一页。

        ⚠️ 游标是 **lastTransferId**(上一页最后一条的 id),不是 offset ——
           与 swaps 的 lastSwapIdV2 同一套形式。实测两页 100+100 条 id 重叠 0 条、
           时间严格更老。这个 API 对不认识的参数一律**静默忽略**,所以传错参数名
           不会报错、只会一直返回第一页(swaps 上踩过这个坑,见 iter_swap_buys)。
        ⚠️ 保留"某页无新条目就停"的兜底:万一参数名哪天又变了,不停就是无限循环。
        """
        uid = quote(user_id, safe="")
        path = EP_TRANSFERS.format(uid=uid)
        seen: set[str] = set()
        yielded = 0
        last_id: str | None = None

        for page in range(_MAX_PAGES):
            if yielded >= max_items:
                return
            params: dict = {"limit": _PAGE_SIZE}
            if last_id:
                params["lastTransferId"] = last_id
            payload = self._get(path, params)
            items = _as_list(payload)
            if not items:
                return

            fresh = 0
            for it in items:
                key = _item_identity(it)
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                yield it
                yielded += 1
                if yielded >= max_items:
                    return

            if fresh == 0:
                logger.warning(
                    "transfers 第 {} 页无新数据(分页参数可能又变了),停止翻页 | user={}",
                    page + 1, user_id,
                )
                return
            ro = _unwrap(payload)
            if isinstance(ro, dict) and ro.get("hasNextPage") is False:
                return
            if len(items) < _PAGE_SIZE:
                return  # 不满一页 = 已到底
            nid = _pick_id(items[-1])
            if not nid:
                logger.warning("transfers 末条没有可用作游标的 id,停止翻页 | user={}", user_id)
                return
            last_id = nid

    def get_balances(self, user_id: str) -> list[dict]:
        return _as_list(self._get(EP_BALANCES.format(uid=quote(user_id, safe=""))))

    def get_leaderboard(self, period: str = "24h", limit: int = 20) -> list[dict]:
        """
        榜单。period ∈ {24h, 7d, 30d, following};following 是"我关注的人里的排名"。

        ⚠️ limit **必传**:不带直接 400。服务端上限 100,传更大也只给 100。
        字段:id / displayName / userHandle / totalPnL / pnl24h / pnl7d / pnl30d /
             totalVolume / numTrades / followers / totalHoldings / topHoldings[] / clan。
        ⚠️ 实测(2026-08-22):period="following" 一个请求返回 79 行,
           同时带全部四个盈亏字段;period="7d" 之类返回的是**全站前 100 榜**,
           只有 pnl7d 一项,且大半不是我们名单里的人 —— 采集名单盈亏只能用 following。
        """
        p = (period or "24h").strip().lower()
        path = EP_LEADERBOARD.format(period=quote(p, safe=""))
        return _as_list(self._get(path, {"limit": max(1, min(int(limit), 100))}))

    def get_following(self, user_id: str, max_items: int = 300) -> list[dict]:
        """
        某人关注的人。字段含 id / userHandle / displayName / swapCount / numTrades /
        private / isRestricted。

        ⚠️ limit 服务端上限是 **100**(传 200 直接 400
           "Number must be less than or equal to 100")。关注几百人的账号要翻页,
           游标是 lastId —— 与 transfers 的分页形式一致。
        ⚠️ 一旦某页没有任何**新** id 就立刻停:若 lastId 其实不生效,
           API 会一直返回第一页,不停就是无限循环。
        """
        path = EP_FOLLOWING.format(uid=quote(user_id, safe=""))
        out: list[dict] = []
        seen: set[str] = set()
        last_id: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict = {"limit": _FOLLOWING_PAGE}
            if last_id:
                params["lastId"] = last_id
            items = _as_list(self._get(path, params))
            fresh = [u for u in items if u.get("id") and str(u["id"]) not in seen]
            if not fresh:
                break
            for u in fresh:
                seen.add(str(u["id"]))
                out.append(u)
            if len(out) >= max_items or len(items) < _FOLLOWING_PAGE:
                break
            last_id = str(fresh[-1]["id"])
        return out[:max_items]

    def get_current_user(self) -> dict:
        """
        当前登录账号自己。返回 responseObject(含 id / userHandle / displayName / following …)。

        ⚠️ 这是判断「活动流看得见谁」的起点:活动流只覆盖**这个账号关注的人**,
           而人不会关注自己 —— 所以自己的交易和观点永远不出现在流里。
           实测:自己在 100 条流里出现 0 次,而自己的账号恰恰是用户最在意的那一个。
           详见 poller._refresh_feed_blind。
        ⚠️ 端点是 /v2/users/current。/v2/users/me 会被当成 userId 校验(400 "must be a uuid"),
           /me、/users/me、/auth/me、/v2/profile 全是 404。
        """
        ro = _unwrap(self._get(EP_CURRENT_USER))
        return ro if isinstance(ro, dict) else {}

    def get_activity_feed(self, limit: int = 100) -> list[dict]:
        """
        「我关注的人」的活动流 —— 一次调用就知道名单里谁刚有动作。

        实测(2026-08-11):单次 0.25~0.43s,100 条覆盖约一小时,
        且 100/100 条都属于监控名单、名单外用户 0 个 —— 这是**关注流不是全站流**。
        拿它当变更检测器,可以把「全员 69 次 swaps(3~5s)」压成「1 次 0.35s」。

        返回 responseObject.items,每条形如:
          swap_buy / swap_sell:
            {"type":"swap_buy", "id":…, "createdAt":…, "userId":…, "userHandle":…,
             "usdAmount":…, "marketCap":…, "price":…, "ticker":…,
             "tokenAddress":…, "networkId": 8453, "equity":…}
          thesis:
            {"type":"thesis", …, "comment":{…正文与代币…}, "authorTrade":{…持仓盈亏…}}

        ⚠️ swap 条目的形态与 /v2/users/{uid}/swaps **完全不同**(没有 in/out 两条腿),
           绝不能喂给 normalize_swaps。本项目只拿它当"谁动了"的信号,事件本身仍以
           swaps 端点为准 —— 少一处字段假设就少一处静默失效。
           thesis 条目则与 /feed/token/thesis 形态一致,可以直接复用 normalize_thesis。
        ⚠️ 它只覆盖**当前登录账号关注的人**。名单里若有没关注的人,他的动作不会出现在这里
           —— 所以 poller 必须保留一条全员轮转扫描兜底(见 _fetch_snapshots)。
        ⚠️ limit 上限 100(传 200 直接 400)。
        ⚠️ 这个端点限流很紧:实测 2.5s 内连打 10 次全部 429。
           每 tick 只调一次远在安全线内,但绝不能放进循环里调。
        """
        payload = self._get(EP_ACTIVITY_FEED, {"limit": max(1, min(int(limit), 100))})
        ro = _unwrap(payload)
        if isinstance(ro, dict):
            items = ro.get("items")
            return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        return [x for x in _as_list(ro) if isinstance(x, dict)]

    def get_trades(self, user_id: str) -> list[dict]:
        """
        拉某人的持仓单(activeTrades + closedTrades),扁平成一个列表返回。

        ⚠️ 这是「已实现盈亏」和「剩余持仓 $0.00」的**唯一**来源:
           清仓之后 balances 里就没有这个币了,只有 closedTrades 还留着
           realizedPnlUsd 与 humanTokenAmount=0。

        每条形如(注意真正的数据在嵌套的 trade 里):
          {"trade": {"tokenAddress":…, "networkId":…, "humanTokenAmount": 剩余数量,
                     "avgEntryPrice":…, "realizedPnlUsd":…, "unrealizedPnlUsd":…,
                     "totalCostBasis":…, "closedAt":…,
                     "tokenMetadata": {"symbol":…, "currentPrice":…}},
           "comment": {...}, "type": "spot"}
        """
        payload = self._get(EP_TRADES, {"userId": user_id, "orderBy": "closedAt"})
        ro = _unwrap(payload)
        if not isinstance(ro, dict):
            return []
        out: list[dict] = []
        for key in ("activeTrades", "closedTrades"):
            v = ro.get(key)
            if isinstance(v, list):
                out.extend(x for x in v if isinstance(x, dict))
        return out

    def get_token_thesis(self, token_address: str, network_id, after_ms: int | None = None,
                         limit: int = 100) -> list[dict]:
        """
        拉某个**代币**下的观点(thesis)。

        ⚠️ FOMO 没有"按用户查观点"的端点 —— 实测 /feed/user/thesis 是 404,
           /v2/users/{id}/thesis 也不存在。观点只能按币查,再按 userId 过滤出监控对象,
           这是 poller._collect_thesis 的职责。
        ⚠️ afterTime 单位是**毫秒**。实测传秒会被服务端忽略(返回全量),
           增量拉取就退化成每轮重复拉 100 条,靠 event_id 去重兜住但白费流量。

        每条记录形如:
          {"type":"thesis", "id":..., "createdAt":"2026-08-11T05:16:28.216Z",
           "userId":..., "userHandle":..., "comment":{"comment":"正文",
           "tokenAddress":..., "networkId":...}, "authorTrade":{...持仓与盈亏...}}
        """
        params: dict = {
            "tokenAddress": token_address,
            "networkId": network_id,
            "limit": limit,
            # ⚠️ threshold=0 **必须显式传**。它按发帖人的**持仓美元额**过滤,
            #    不传时服务端用一个非 0 的默认值,小仓位的观点会被静默丢掉。
            #    实测:同一个币不传 → 100 条里 0 条是目标用户的;
            #          传 threshold=0 → 同样 100 条里有 2 条是他的(持仓仅 $3.71)。
            #    网页版自己调的时候带的就是 threshold=0,我们漏了这一个参数,
            #    表现为"观点功能完全不工作但没有任何报错"。
            "threshold": 0,
        }
        if after_ms:
            params["afterTime"] = int(after_ms)
        return _as_list(self._get(EP_TOKEN_THESIS, params))

    def get_top_holders(self, token_address: str, network_id) -> dict:
        """
        某个币的持有人榜(/chips 的分子)。返回 responseObject 里对应这个币的那个对象:
          {"tokenAddress": …, "networkId": …, "totalHolders": 26, "topHolders": [ … ]}
        每个 topHolders 元素实测含:
          user{id, userHandle, displayName, address, evmAddress, …}, tradeId,
          humanAmount, value, price, costBasis, averageEntryPrice,
          averageHoldTimeSeconds, pnl, unrealizedPnl, realizedPnl, sumSwapOpen,
          isDev, comment, showComment

        ⚠️ **只发一个请求、不分页**。原因见 EP_TOP_HOLDERS 的注释:这个端点没有分页能力,
           limit 也钳死在 100。调用方必须自己拿 len(topHolders) 与 totalHolders 比,
           判断手上这份数据是全量还是截断 —— 那是 /chips「精确 / 下界」两套文案的唯一依据。
        ⚠️ 解析不出来返回 {} 而不是抛异常:上层据此显示"没查到",与猜错链是同一种表现。
        """
        tokens = json.dumps(
            [{"address": token_address, "networkId": _as_network_number(network_id)}],
            separators=(",", ":"),
        )
        payload = self._get(EP_TOP_HOLDERS, {"tokens": tokens, "limit": TOP_HOLDERS_LIMIT})
        ro = _unwrap(payload)
        # 请求里只放了一个币,响应就只有一个元素;仍按"可能是裸对象"兜底(与 _as_obj 同一条理由)
        if isinstance(ro, list):
            ro = next((x for x in ro if isinstance(x, dict)), {})
        return ro if isinstance(ro, dict) else {}

    def get_token_meta(self, token_address: str, network_id) -> dict:
        """
        代币元数据(/chips 的分母来源:`.token.info.totalSupply`)。拿不到返回 {}。

        ⚠️ 走的是匿名请求,不占用监控进程共用的登录态(见 EP_FILTER_TOKENS)。
           两个实现都继承这一份:它与 Bearer 鉴权、与 _fetch_ok 的重试/失效逻辑完全无关,
           尤其**不能**让一个 403 触发 tokens.invalidate() —— 那会把好端端的登录态作废掉。
        """
        return fetch_token_meta(token_address, network_id)

    def iter_swap_buys(self, user_id: str, max_items: int) -> Iterator[dict]:
        """
        分页迭代 swaps 原始条目,供 seeding 建历史基线用。

        ⚠️ 名字里的 buys 是历史叫法,这里**吐的是原始 swap 字典、不做买卖方向过滤** ——
           方向判定要用 models 的归一化规则,那是 poller.normalize_swaps 的职责,
           client 层不碰业务语义(跨模块契约就是这么定的)。
        ⚠️ 分页游标是 **lastSwapIdV2**(上一页最后一条的 id),不是 offset。
           实测证据:改之前用 offset,真实日志里每个活跃用户都是
           「第 2 页无新数据(分页参数可能不生效)」+「基线完成 · 扫描 50 条」,
           而真的只有 24 笔的用户没有这条警告 —— 完美对照。
           这个 API 对不认识的参数一律**静默忽略**,所以 offset 不报错、只是一直返回第一页。
           后果:fomo_backfill_max_items=500 形同虚设,基线只覆盖最近 50 笔,
           几个月前买过又清仓的币会被误判成 🌱 首次建仓 —— 而徽章落库即冻结、永不重算。
        ⚠️ 仍然保留"某页无新条目就停"的兜底:万一服务端哪天又改了参数名,
           不停就是无限循环 + 无限重复数据。

        返回:通过 generator 正常结束表示翻到底;
             若因分页参数疑似失效而提前停止,会把 self._last_paging_ok 置 False,
             供 seeding 决定回执文案(不能无条件报"基线完成")。
        """
        uid = quote(user_id, safe="")
        path = EP_SWAPS.format(uid=uid)
        seen: set[str] = set()
        yielded = 0
        last_id: str | None = None
        self._last_paging_ok = True

        for page in range(_MAX_PAGES):
            if yielded >= max_items:
                return
            params: dict = {"limit": _PAGE_SIZE}
            if last_id:
                params["lastSwapIdV2"] = last_id
            payload = self._get(path, params)
            items = _as_list(payload)
            if not items:
                return

            fresh = 0
            for it in items:
                key = _item_identity(it)
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                yield it
                yielded += 1
                if yielded >= max_items:
                    return

            if fresh == 0:
                logger.warning(
                    "swaps 第 {} 页无新数据(分页参数可能又变了),停止翻页 | user={}", page + 1, user_id
                )
                self._last_paging_ok = False
                return
            # 服务端明确告知还有没有下一页时以它为准
            ro = _unwrap(payload)
            if isinstance(ro, dict) and ro.get("hasNextPage") is False:
                return
            if len(items) < _PAGE_SIZE:
                return  # 不满一页 = 已到底
            nid = _pick_id(items[-1])
            if not nid:
                logger.warning("swaps 末条没有可用作游标的 id,停止翻页 | user={}", user_id)
                self._last_paging_ok = False
                return
            last_id = nid

    def fetch_snapshot(self, user_id: str) -> UserSnapshot:
        """
        按用户一次拉齐 swaps + balances。**每一类单独 try/except**,失败的置 None 并 WARN。

        ⚠️ 只拉这两类:
          - transfers **不在这里**:它是低频采集(约 5 分钟一轮,见
            poller._TRANSFERS_EVERY_N_TICKS),与"每轮全员拉"的这两类节奏完全不同,
            塞进来只会让它跟着涨到每轮 91 个请求。取数入口是 get_transfers。
          - thesis 没有按用户查的端点,改由 poller._collect_thesis 按代币采集后过滤。
        ⚠️ 绝不能让一类失败拖垮另一类:balances 挂了只是共识副指标降级,
           swaps 照常推送。
        """
        snap = UserSnapshot(user_id=user_id)
        for part in ("swaps", "balances", "trades"):
            try:
                setattr(snap, part, getattr(self, f"get_{part}")(user_id))
            except AuthError:
                raise  # 登录态问题是全局的,必须上抛让 poller 告警「需要重新登录」并停轮询
            except Exception as e:  # noqa: BLE001
                logger.warning("{} 拉取失败(该项降级为 None) | user={} | {}", part, user_id, e)
        return snap

    def raw_get(self, path: str, params: dict | None = None) -> tuple[int, object, dict]:
        """
        `--probe` 专用:原样返回 (status_code, json 或 text, 响应头),
        **不抛异常、不重试、不做任何解析降级**。

        status_code == 0 表示传输层就没成功(超时 / DNS / 浏览器崩溃)。

        ⚠️ 必须把响应头也带出来:probe #16(有无 X-RateLimit-*)要靠它判定,
           不然那一项永远只能标"无法验证",Phase 0 的 17 项就打不满勾。
        """
        try:
            status, text, headers = self._request(path, params)
        except AuthError as e:
            return 0, f"AuthError: {e}", {}
        except Exception as e:  # noqa: BLE001
            return 0, f"TransportError: {e}", {}
        try:
            return status, (json.loads(text) if text else None), headers
        except (json.JSONDecodeError, TypeError):
            return status, text, headers


# ============================================================
# 实现一:curl_cffi 直连
# ============================================================
class HttpFomoClient(_BaseFomoClient):
    """
    curl_cffi + impersonate="chrome":用真实 Chrome 的 TLS/HTTP2 指纹发请求。

    已实测匿名请求会被 Cloudflare 403(**且换 TLS 指纹无效**),说明规则是「无合法 Bearer 即拦」。
    带 token 后能否放行由 --probe 回答;不行就把 FOMO_CLIENT_IMPL 改成 playwright。
    """

    def __init__(self, token_provider: TokenProvider | None = None) -> None:
        super().__init__(token_provider)
        # ⚠️ libcurl 的 easy handle **不能被多线程同时使用**,共用一个 Session
        #    是概率性的崩溃/串包,不是理论问题。
        #    早期用全局锁解决,但那把所有请求串行化了 —— 名单到几十人时
        #    一轮要一分多钟(实测 1.01s/人),远超 20s 的轮询间隔,tick 会一直堆积。
        #    改成**每线程一个 Session**:线程之间天然隔离,锁可以整个去掉,
        #    poller 才能开线程池并发拉取。
        self._tl = threading.local()

    def _ensure_session(self):
        sess = getattr(self._tl, "session", None)
        if sess is None:
            # 延迟 import:curl_cffi 带原生库,顶层 import 会让 playwright 路径也被它的加载失败拖累
            from curl_cffi import requests as cffi_requests

            settings = get_settings()
            sess = cffi_requests.Session(
                impersonate="chrome",
                proxies=settings.proxies,
                timeout=_TIMEOUT_SEC,
            )
            self._tl.session = sess
            logger.debug("curl_cffi 会话已创建(线程 {}) | proxy={}",
                         threading.current_thread().name, settings.fomo_proxy or "无")
        return sess

    def _request(self, path: str, params: dict | None = None) -> tuple[int, str, dict]:
        token = self._tokens.get_access_token()
        url = BASE_URL + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._ensure_session().get(url, params=clean or None, headers=_auth_headers(token))
        except Exception as e:  # noqa: BLE001
            raise _TransportError(str(e)) from e
        return resp.status_code, resp.text or "", dict(resp.headers or {})

    def close(self) -> None:
        """只关当前线程的会话。工作线程退出时它自己那份由 GC 回收。"""
        sess = getattr(self._tl, "session", None)
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._tl.session = None


# ============================================================
# 实现二:Playwright 页面上下文内 fetch
# ============================================================
# 在页面里发 fetch:同源策略下 CORS 天然放行,Cloudflare 看到的是一个真实浏览器。
# 捕获异常返回 status:0,让 Python 侧统一走 _TransportError。
_FETCH_JS = """async ({url, headers}) => {
  try {
    const r = await fetch(url, {method: 'GET', headers, credentials: 'include'});
    const body = await r.text();
    const h = {};
    r.headers.forEach((v, k) => { h[k] = v; });
    return {status: r.status, body: body, headers: h};
  } catch (e) {
    return {status: 0, body: String(e), headers: {}};
  }
}"""


class PlaywrightFomoClient(_BaseFomoClient):
    """
    常驻 headless 浏览器,在 fomo.family 的页面上下文里 fetch。慢、吃 ~300MB,但与真实 App 行为一致。

    ⚠️ Playwright 的同步 API 绑定创建它的线程(内部用 greenlet),跨线程调用会挂死。
       poller 线程和 bot 线程都会用 client,所以整套浏览器按**线程私有**创建。
       代价:bot 线程第一次 resolve_handle 会再拉起一个浏览器(约 +300MB),
       所以第二次创建时会打 WARNING。这比"跨线程静默挂死"好排查得多。

    ⚠️ **绝不能并发**。poller 的线程池每 tick 建新线程,而本类按线程私有创建浏览器,
       线程退出时浏览器不会被回收(close() 只关当前线程那份,且正常路径下无人调用)——
       实测每 tick 泄漏 6 套 node driver + chromium,进程数单调递增直到机器 swap 卡死。
       supports_concurrency=False 让 _fetch_snapshots 对本实现强制串行。
    """

    supports_concurrency = False

    def __init__(self, token_provider: TokenProvider | None = None) -> None:
        super().__init__(token_provider)
        self._tl = threading.local()
        self._instances = 0
        self._instances_lock = threading.Lock()

    # ---------- 浏览器生命周期 ----------
    def _ensure_page(self):
        page = getattr(self._tl, "page", None)
        if page is not None:
            try:
                if not page.is_closed():
                    return page
            except Exception:  # noqa: BLE001
                pass
            self._teardown()

        from playwright.sync_api import sync_playwright

        settings = get_settings()
        launch_kwargs: dict = {"headless": True}
        if settings.fomo_proxy:
            launch_kwargs["proxy"] = {"server": settings.fomo_proxy}

        with self._instances_lock:
            self._instances += 1
            if self._instances > 1:
                logger.warning(
                    "第 {} 个浏览器实例(线程 {}):Playwright 同步 API 不能跨线程共享,"
                    "每个线程各起一套,内存会多占约 300MB",
                    self._instances, threading.current_thread().name,
                )

        pw = sync_playwright().start()
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        _seed_privy_session(context)
        page = context.new_page()
        try:
            page.goto(FOMO_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:  # noqa: BLE001
            # 首页没完全加载不影响 fetch —— 我们只需要一个 fomo.family 的页面上下文
            logger.warning("fomo.family 首页加载异常(继续使用该页面上下文): {}", e)

        self._tl.pw, self._tl.browser, self._tl.context, self._tl.page = pw, browser, context, page
        logger.info("PlaywrightFomoClient 就绪 | 线程={}", threading.current_thread().name)
        return page

    def _teardown(self) -> None:
        """关掉当前线程这一套。只在页面崩了或显式 close 时调用。"""
        for attr in ("context", "browser", "pw"):
            obj = getattr(self._tl, attr, None)
            if obj is None:
                continue
            try:
                # playwright 实例是 stop(),浏览器/上下文是 close()
                obj.stop() if attr == "pw" else obj.close()  # noqa: B018
            except Exception:  # noqa: BLE001
                pass
            setattr(self._tl, attr, None)
        self._tl.page = None

    # ---------- 请求 ----------
    def _request(self, path: str, params: dict | None = None) -> tuple[int, str, dict]:
        token = self._tokens.get_access_token()
        url = BASE_URL + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if clean:
            url += ("&" if "?" in url else "?") + urlencode(clean)

        page = self._ensure_page()
        try:
            res = page.evaluate(_FETCH_JS, {"url": url, "headers": _auth_headers(token)})
        except Exception as e:  # noqa: BLE001
            # 页面/浏览器挂了就整套重建,否则后面每次请求都会撞同一具尸体
            self._teardown()
            raise _TransportError(f"页面内 fetch 执行失败: {e}") from e

        res = res or {}
        status = int(res.get("status") or 0)
        body = res.get("body") or ""
        if status == 0:
            raise _TransportError(f"页面内 fetch 未拿到响应: {str(body)[:200]}")
        return status, body, dict(res.get("headers") or {})

    def iter_swap_buys(self, user_id: str, max_items: int) -> Iterator[dict]:
        """
        ⚠️ 明确不支持(设计文档 §3.6):在浏览器里翻 10 页 swaps 的成本远高于收益。
           调用方(seed_next_pending_user)捕获本异常后让 stats_ready 保持 0,
           功能 A/B 整体降级为不显示,**主推送完全不受影响**。
        """
        raise NotSupportedError("PlaywrightFomoClient 不支持 swaps 分页,功能 A/B 降级为不显示")

    def close(self) -> None:
        self._teardown()


def _seed_privy_session(context) -> None:
    """
    把 data/fomo_session.json 里的 Privy token 塞回浏览器,让页面本身也处于登录态。

    不塞也能工作(fetch 里显式带了 Authorization 头),塞的收益是:
    cookie domain 若是 .fomo.family,登录态 cookie 会一并发给 prod-api.fomo.family,
    Cloudflare 看到的就是一个完整的已登录浏览器 —— 而这个实现存在的唯一理由就是过 Cloudflare。

    ⚠️ TODO(probe): 键名与编码方式(是否 JSON 双重编码)未实测。
       整段包在 try 里:塞不进去只是少一层保险,绝不能因此让 client 起不来。
    """
    sess = load_session() or {}
    access, refresh = sess.get("access_token"), sess.get("refresh_token")
    if not access:
        return
    try:
        cookies = [{
            "name": "privy-token", "value": access, "domain": ".fomo.family",
            "path": "/", "secure": True, "sameSite": "Lax",
        }]
        if refresh:
            cookies.append({
                "name": "privy-refresh-token", "value": refresh, "domain": ".fomo.family",
                "path": "/", "secure": True, "sameSite": "Lax",
            })
        context.add_cookies(cookies)

        # localStorage 必须在页面创建前用 init script 写,页面脚本才读得到
        pairs = {"privy:token": access}
        if refresh:
            pairs["privy:refresh_token"] = refresh
        stmts = "".join(
            # 双重 dumps:privy 存的是 JSON 编码后的字符串(带引号),直接写裸值它读不出来
            f"localStorage.setItem({json.dumps(k)}, {json.dumps(json.dumps(v))});"
            for k, v in pairs.items()
        )
        context.add_init_script(f"try{{{stmts}}}catch(e){{}}")
        logger.debug("已把登录态注入浏览器上下文")
    except Exception as e:  # noqa: BLE001
        logger.warning("注入登录态失败(不影响 Bearer 鉴权): {}", e)


# ============================================================
# 工厂
# ============================================================
def build_client(token_provider: TokenProvider | None = None) -> FomoClient:
    """按 settings.fomo_client_impl 选实现。取值已在 FomoSettings 里校验过,这里不再重复兜底。"""
    impl = get_settings().fomo_client_impl
    if impl == "playwright":
        logger.info("使用 PlaywrightFomoClient(页面上下文内 fetch)")
        return PlaywrightFomoClient(token_provider)
    logger.info("使用 HttpFomoClient(curl_cffi + Chrome 指纹)")
    return HttpFomoClient(token_provider)
