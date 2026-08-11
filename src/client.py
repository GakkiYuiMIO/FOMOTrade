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
import threading
import time
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
EP_USER = "/v2/users/{uid}"
EP_SWAPS = "/v2/users/{uid}/swaps"
EP_BALANCES = "/v2/users/{uid}/balances"
EP_TRANSFERS = "/v2/transfers/with/{uid}"
# ⚠️ 唯一确认过的 thesis 端点 /feed/token/thesis 是**按代币**查的,不是按用户。
#    "某用户发过哪些观点"的端点没能从 bundle 里逆向出来,只能按候选依次探测。
#    TODO(probe #12): 确认真实端点后把下面收敛成一条,并确认是否同时返回 tokenAddress + networkId
EP_THESIS_CANDIDATES = (
    "/v2/users/{uid}/thesis",
    "/v2/users/{uid}/theses",
    "/feed/user/thesis",
)

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
_TIMEOUT_SEC = 25.0
_MAX_ATTEMPTS = 5        # 单次请求的总尝试次数上限,防止退避循环无限打转
_BACKOFF_SEC = 1.5

# Cloudflare 拦截页的特征词。
# ⚠️ 绝不能只看 cf-ray 响应头:prod-api 整站都在 Cloudflare 后面,合法的 401 也带这个头。
#    必须是「HTML 页面 + 拦截文案」同时命中才算 WAF 拦截。
_CF_MARKERS = ("attention required", "cloudflare", "just a moment", "cf-error", "ray id")


class FomoAPIError(Exception):
    """FOMO API 调用失败。message 里必须说清是 Cloudflare 拦截还是鉴权失败。"""


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


class FomoClient(Protocol):
    """两个实现的公共协议。上层 poller / bot 只依赖这个,换实现只改一行配置。"""

    def resolve_handle(self, handle: str) -> tuple[str, str]: ...
    def get_swaps(self, user_id: str, limit: int = 50) -> list[dict]: ...
    def get_transfers(self, user_id: str, limit: int = 50) -> list[dict]: ...
    def get_thesis(self, user_id: str, limit: int = 50) -> list[dict]: ...
    def get_balances(self, user_id: str) -> list[dict]: ...
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


def _is_cloudflare_block(text: str) -> bool:
    """是不是 Cloudflare 的 WAF 拦截页(而不是 API 自己返回的 401 JSON)"""
    body = (text or "")[:4000].lower()
    if "<html" not in body and "<!doctype" not in body:
        return False
    return any(m in body for m in _CF_MARKERS)


def _retry_after(headers: dict, default: float = 3.0) -> float:
    """429 的退避秒数。上限 60s —— 一个畸形的头不该把整个 tick 卡死。"""
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            try:
                return max(1.0, min(float(v), 60.0))
            except (TypeError, ValueError):
                return default
    return default


def _item_identity(item: dict) -> str:
    """翻页去重用的弱标识。没有稳定 id 时退化为整条报文的字符串形态。"""
    native = pick(item, "id", "_id", "swapId", "txHash", "transactionHash", "signature", "hash")
    return str(native) if native else json.dumps(item, sort_keys=True, default=str)[:512]


def _next_cursor(payload) -> str | None:
    """游标式分页的下一页标记(如果 API 是这种风格)"""
    if not isinstance(payload, dict):
        return None
    c = pick(payload, "nextCursor", "next_cursor", "cursor", "nextPage", "next")
    return str(c) if c else None


# ============================================================
# 公共实现:两个 client 只在「怎么发一个 GET」上不同
# ============================================================
class _BaseFomoClient:
    """端点拼装、重试策略、响应解析都在这里;子类只需实现 _request()。"""

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
        attempt = 0
        auth_retried = False
        while attempt < _MAX_ATTEMPTS:
            attempt += 1
            try:
                status, text, headers = self._request(path, params)
            except _TransportError as e:
                if attempt < _MAX_ATTEMPTS:
                    logger.warning("请求传输失败,{:.1f}s 后重试 | {} | {}", _BACKOFF_SEC * attempt, path, e)
                    time.sleep(_BACKOFF_SEC * attempt)
                    continue
                raise FomoAPIError(f"{path} 传输失败: {e}") from e

            if 200 <= status < 300:
                return text

            if status == 429:
                wait = _retry_after(headers)
                if attempt < _MAX_ATTEMPTS:
                    logger.warning("FOMO 限流 429,{:.1f}s 后重试 | {}", wait, path)
                    time.sleep(wait)
                    continue
                raise FomoAPIError(f"{path} 持续 429 限流")

            if status in (401, 403):
                if _is_cloudflare_block(text):
                    # 换 token 解决不了 WAF,重试只是浪费时间,直接抛出并告诉用户怎么办
                    raise FomoAPIError(
                        f"{path} 被 Cloudflare WAF 拦截(HTTP {status})—— 不是鉴权问题。"
                        f"请把 .env 里的 FOMO_CLIENT_IMPL 改成 playwright 重试。"
                    )
                if not auth_retried:
                    auth_retried = True
                    logger.warning("HTTP {} 疑似 token 失效,续期后重试一次 | {}", status, path)
                    self._tokens.invalidate()
                    continue
                raise FomoAPIError(
                    f"{path} 鉴权失败(HTTP {status},续期后仍失败,非 Cloudflare 拦截):"
                    f"{(text or '')[:200]} —— 请重新执行 --login"
                )

            if status >= 500 and attempt < _MAX_ATTEMPTS:
                logger.warning("FOMO 服务端错误 {},{:.1f}s 后重试 | {}", status, _BACKOFF_SEC * attempt, path)
                time.sleep(_BACKOFF_SEC * attempt)
                continue

            raise FomoAPIError(f"{path} HTTP {status}: {(text or '')[:300]}")

        raise FomoAPIError(f"{path} 重试 {_MAX_ATTEMPTS} 次仍失败")

    def _get(self, path: str, params: dict | None = None):
        return _parse_json(self._fetch_ok(path, params), path)

    # ---------- 端点 ----------
    def resolve_handle(self, handle: str) -> tuple[str, str]:
        """
        handle → (user_id, display_name)。

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
        return str(uid), str(display)

    def get_swaps(self, user_id: str, limit: int = 50) -> list[dict]:
        # TODO(probe #1/#2/#3/#4): networkId 是否存在、唯一 id 字段名、时间单位、买卖方向表示
        return _as_list(self._get(EP_SWAPS.format(uid=quote(user_id, safe="")), {"limit": limit}))

    def get_transfers(self, user_id: str, limit: int = 50) -> list[dict]:
        # TODO(probe #11): direction 字段、对手方地址、是否包含 swap 自身产生的 transfer
        return _as_list(self._get(EP_TRANSFERS.format(uid=quote(user_id, safe="")), {"limit": limit}))

    def get_balances(self, user_id: str) -> list[dict]:
        # TODO(probe #6): tokenAddress/networkId/usdValue 是否齐全、是否一次返回全部链
        return _as_list(self._get(EP_BALANCES.format(uid=quote(user_id, safe=""))))

    def get_thesis(self, user_id: str, limit: int = 50) -> list[dict]:
        """
        按用户拉观点。

        ⚠️ 端点是猜的(见 EP_THESIS_CANDIDATES 的注释)。依次探测候选路径,
           第一个返回 2xx 的模板会缓存到进程内,后续不再重复试错。
           全部候选都失败 → 抛 FomoAPIError → fetch_snapshot 把 thesis 置 None,
           买卖/转账三类照常推送(设计文档 §九 降级矩阵)。
        """
        uid = quote(user_id, safe="")
        templates = [self._thesis_tpl] if self._thesis_tpl else list(EP_THESIS_CANDIDATES)
        last_err: Exception | None = None
        for tpl in templates:
            try:
                payload = self._get(tpl.format(uid=uid), {"userId": user_id, "limit": limit})
            except FomoAPIError as e:
                last_err = e
                logger.debug("thesis 候选端点不可用: {} | {}", tpl, e)
                continue
            if self._thesis_tpl != tpl:
                logger.info("thesis 端点探测命中: {}", tpl)
                self._thesis_tpl = tpl
            return _as_list(payload)
        raise FomoAPIError(f"thesis 端点全部候选均失败,最后一个错误: {last_err}")

    def iter_swap_buys(self, user_id: str, max_items: int) -> Iterator[dict]:
        """
        分页迭代 swaps 原始条目,供 seeding 建历史基线用。

        ⚠️ 名字里的 buys 是历史叫法,这里**吐的是原始 swap 字典、不做买卖方向过滤** ——
           方向判定要用 models 的归一化规则,那是 poller.normalize_swaps 的职责,
           client 层不碰业务语义(跨模块契约就是这么定的)。
        ⚠️ TODO(probe #5): 分页参数名与单页上限未实测。这里先试 cursor、再退回 offset;
           **一旦某页没有任何新条目就立刻停止** —— 若 offset 参数其实不生效,
           API 会一直返回第一页,不停的话就是无限循环 + 无限重复数据。
        """
        uid = quote(user_id, safe="")
        path = EP_SWAPS.format(uid=uid)
        seen: set[str] = set()
        yielded = 0
        offset = 0
        cursor: str | None = None

        for page in range(_MAX_PAGES):
            if yielded >= max_items:
                return
            params: dict = {"limit": _PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            elif offset:
                params["offset"] = offset
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
                logger.warning("swaps 第 {} 页无新数据(分页参数可能不生效),停止翻页 | user={}", page + 1, user_id)
                return
            cursor = _next_cursor(payload)
            offset += len(items)
            if not cursor and len(items) < _PAGE_SIZE:
                return  # 不满一页 = 已到底

    def fetch_snapshot(self, user_id: str) -> UserSnapshot:
        """
        一次拉齐四类数据。**每一类单独 try/except**,失败的置 None 并 WARN。

        ⚠️ 绝不能让一类失败拖垮整个用户:thesis 端点是猜的、大概率一开始就 404,
           若不隔离,买卖/转账推送会被一个猜错的端点整体带走。
        """
        snap = UserSnapshot(user_id=user_id)
        for part in ("swaps", "transfers", "thesis", "balances"):
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
        self._session = None
        # ⚠️ libcurl 的 easy handle 不能被多线程同时使用。poller 线程轮询、bot 线程 /add 时
        #    resolve_handle,两者会撞上 —— 不加锁是概率性的崩溃/串包,不是理论问题。
        self._lock = threading.Lock()

    def _ensure_session(self):
        if self._session is None:
            # 延迟 import:curl_cffi 带原生库,顶层 import 会让 playwright 路径也被它的加载失败拖累
            from curl_cffi import requests as cffi_requests

            settings = get_settings()
            self._session = cffi_requests.Session(
                impersonate="chrome",
                proxies=settings.proxies,
                timeout=_TIMEOUT_SEC,
            )
            logger.debug("curl_cffi 会话已创建 | proxy={}", settings.fomo_proxy or "无")
        return self._session

    def _request(self, path: str, params: dict | None = None) -> tuple[int, str, dict]:
        token = self._tokens.get_access_token()
        url = BASE_URL + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        with self._lock:
            try:
                resp = self._ensure_session().get(url, params=clean or None, headers=_auth_headers(token))
            except Exception as e:  # noqa: BLE001
                raise _TransportError(str(e)) from e
        return resp.status_code, resp.text or "", dict(resp.headers or {})

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None


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
    """

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
