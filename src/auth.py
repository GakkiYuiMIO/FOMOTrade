"""
Privy 登录态 —— 交互式登录取 token + refresh token 自动续期

FOMO 的所有 API 都要求 `Authorization: Bearer <privy access token>`(设计文档 §2.2)。
本模块只做两件事:
  1) interactive_login(): 拉起**有头**浏览器,等用户自己登录完,把 token 抓出来存盘
  2) TokenProvider: 运行期内存缓存 access token,快过期时用 refresh token 自动续期

⚠️ 硬性安全要求:登录全程由用户在浏览器里自己完成,**程序绝不代填账号密码、不触碰凭据输入**。
⚠️ data/fomo_session.json 含 refresh token,泄漏等于账号被接管;已在 .gitignore 里。
   本模块任何 token 进日志前必须过 config.mask()。
⚠️ Privy 的 token 到底存在 localStorage 还是 cookie **没有实测过**,两条路径都实现了,
   哪条命中会打 INFO 日志。probe 阶段照着日志把候选列表收敛即可。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from functools import lru_cache
from pathlib import Path

import httpx
from loguru import logger

from src.config import PROFILE_DIR, SESSION_FILE, get_settings, mask
from src.models import pick

# ============================================================
# 常量(逆向 fomo.family 前端 bundle 得到,见设计文档 §2.2)
# ============================================================
PRIVY_APP_ID = "cm6h485o300n3zj9yl6vpedq7"
PRIVY_CLIENT_ID = "client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH"
PRIVY_SESSIONS_URL = "https://auth.privy.io/api/v1/sessions"

FOMO_ORIGIN = "https://fomo.family"

# 与 curl_cffi 的 impersonate="chrome" 大致对齐,避免 UA 与 TLS 指纹自相矛盾
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 过期前多久主动续期。Privy access token 约 1 小时,5 分钟余量足够覆盖一次 tick + 重试
REFRESH_MARGIN_SEC = 300
# JWT 里解不出 exp 时的保守有效期:宁可多续几次,也不能"永远不续"直接静默失效
_FALLBACK_TTL_SEC = 900

LOGIN_TIMEOUT_SEC = 300
_LOGIN_POLL_SEC = 2.0

# ============================================================
# 反自动化检测(给 Google / X 这类第三方登录用)
# ============================================================
# 现象:走 Google 登录时报 "Couldn't sign you in — This browser or app may not be secure"。
# Google 判定"被自动化控制"主要看三条,**三条必须同时抹掉,少一条照样被拦**:
#   1) 启动开关 --enable-automation(Playwright 默认会加,还会顶出"正受自动化控制"横幅)
#   2) navigator.webdriver === true
#   3) 浏览器本体:打包的 Chromium / headless-shell 版本号与指纹都对不上真实 Chrome
# 对应的三条对策就是下面的 _DROP_DEFAULT_ARGS / _STEALTH_JS / _LOGIN_CHANNELS。
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]
_DROP_DEFAULT_ARGS = ["--enable-automation"]
# init script 在每个 document 创建时**先于页面脚本**执行,所以检测代码读到的已经是改过的值
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

# 浏览器渠道优先级:系统真实 Chrome > Edge > Playwright 打包的 Chromium。
# 前两个是真浏览器,Google 认;最后一个只是兜底 ——
# 用它能完成 FOMO 自己的邮箱登录,但 Google OAuth 大概率仍会被拦。
_LOGIN_CHANNELS = ("chrome", "msedge", None)

# ---- Privy token 在浏览器里的候选存放位置(全部未实测) ----
# TODO(probe): 用 --login 跑一次,照日志里打印的实际键名把下面的候选列表收敛成一条
_LS_ACCESS_KEYS = ("privy:token", "privy:access_token", "privy:accessToken")
_LS_REFRESH_KEYS = ("privy:refresh_token", "privy:refreshToken")
_CK_ACCESS_KEYS = ("privy-token", "privy-access-token")
_CK_REFRESH_KEYS = ("privy-refresh-token", "privy-refresh")

# 只捞含 privy 的键,避免把整个 localStorage(可能有几 MB 的前端缓存)拖进 Python
_DUMP_PRIVY_LS_JS = """() => {
  const out = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.toLowerCase().includes('privy')) out[k] = localStorage.getItem(k);
    }
  } catch (e) { /* 跨域 iframe 读 localStorage 会抛,忽略 */ }
  return out;
}"""


class AuthError(Exception):
    """登录态不可用(未登录 / 续期失败)。上层收到它应当告警「需要重新登录」并停止轮询,不要空转刷日志。"""


class RetryableAuthError(AuthError):
    """
    续期**暂时**失败:网络抖动、代理断流、Privy 5xx。

    ⚠️ 与 AuthError 的区别是致命性,不是场景:
       AuthError → 发 TG 告警 + shutdown 调度器 + 进程退出(bot.ps1 无守护,退了就一直停着)
       RetryableAuthError → 只 warn,本轮降级,下一轮自然重试
    继承 AuthError 是为了让既有的 `except AuthError: raise` 上抛链保持不变 ——
    调用方要区分时用 isinstance 判子类,漏判最坏也只是退回旧行为(停机告警),不会静默。
    """


# ============================================================
# JWT / 会话文件
# ============================================================
def decode_jwt_exp(token: str | None) -> float | None:
    """
    从 JWT 中段解出 exp(unix 秒)。

    ⚠️ 刻意**不做签名校验**:我们读的是自己的 token,不是在验证别人递过来的 token,
       校验需要 Privy 的公钥且毫无收益。解析失败一律返回 None,由调用方走保守 TTL。
    """
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        # base64url 的 padding 被 JWT 规范去掉了,不补齐会直接 binascii.Error
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return float(exp) if exp else None
    except Exception as e:  # noqa: BLE001
        logger.debug("JWT exp 解析失败(不影响主流程): {}", e)
        return None


def load_session(path: Path | None = None) -> dict | None:
    """读取登录态文件。文件不存在或损坏都返回 None —— 调用方一律当作「未登录」处理。"""
    p = Path(path) if path else SESSION_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("登录态文件损坏,按未登录处理 | {} | {}", p, e)
        return None


def save_session(
    access_token: str | None,
    refresh_token: str | None,
    *,
    source: str,
    path: Path | None = None,
) -> dict:
    """
    写登录态文件。expires_at 从 JWT 解,解不出用保守 TTL。

    ⚠️ 续期后必须回写,否则进程重启会拿着一个已经作废的 refresh token 反复失败。
    """
    p = Path(path) if path else SESSION_FILE
    exp = decode_jwt_exp(access_token) or (time.time() + _FALLBACK_TTL_SEC)
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": exp,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "登录态已写入 {} | access={} refresh={} 过期于 {}",
        p, mask(access_token), mask(refresh_token),
        time.strftime("%H:%M:%S", time.localtime(exp)),
    )
    return data


# ============================================================
# TokenProvider
# ============================================================
class TokenProvider:
    """
    access token 的唯一出口。内存缓存 + 自动续期。

    ⚠️ poller 线程与 bot 线程会并发取 token,必须加锁 ——
       不加锁的话两个线程会同时发起续期,第二次续期用的是已被 Privy 作废的旧 refresh token,
       直接把登录态搞丢。用 RLock 是因为 get_access_token 内部还会走 _ensure_loaded。
    """

    def __init__(self, session_file: Path | None = None) -> None:
        self._path = Path(session_file) if session_file else SESSION_FILE
        self._lock = threading.RLock()
        self._loaded = False
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at: float = 0.0

    # ---------- 内部 ----------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        data = load_session(self._path) or {}
        self._access = data.get("access_token")
        self._refresh = data.get("refresh_token")
        exp = data.get("expires_at")
        try:
            self._expires_at = float(exp) if exp else (decode_jwt_exp(self._access) or 0.0)
        except (TypeError, ValueError):
            self._expires_at = decode_jwt_exp(self._access) or 0.0
        self._loaded = True
        if self._access or self._refresh:
            logger.debug("登录态已加载 | access={} refresh={}", mask(self._access), mask(self._refresh))

    def _do_refresh(self) -> None:
        """
        用 refresh token 换一对新 token。

        ⚠️ TODO(probe #14): 这个接口的请求/响应形状**完全没实测过** ——
           请求体字段名、响应里 access/refresh 的字段名、过期表现(401 还是 403)都是推测。
           因此续期失败时**必须把完整响应体打进日志**,否则用户第一次跑起来只会看到
           「续期失败」四个字,无从下手。日志会自动脱敏长字符串。
        """
        settings = get_settings()
        headers = {
            "privy-app-id": PRIVY_APP_ID,
            "privy-client-id": PRIVY_CLIENT_ID,
            "Content-Type": "application/json",
            "accept": "application/json",
            "origin": FOMO_ORIGIN,
            "referer": FOMO_ORIGIN + "/",
            "user-agent": USER_AGENT,
        }
        # ⚠️ 必须带上**当前(即将过期)的** access token。
        #    实测不带时 Privy 返回 400 {"error":"Missing access token",
        #    "code":"missing_or_invalid_token"} —— 光有 refresh token 不够。
        #    漏掉这一行的后果是:程序跑满一小时后续期必失败、轮询停摆,
        #    而前一小时一切正常,很容易被误认为"跑着跑着自己挂了"。
        if self._access:
            headers["Authorization"] = f"Bearer {self._access}"
        logger.info("access token 即将过期,发起续期 | refresh={}", mask(self._refresh))
        try:
            with httpx.Client(timeout=20.0, proxy=settings.fomo_proxy) as c:
                resp = c.post(PRIVY_SESSIONS_URL, json={"refresh_token": self._refresh}, headers=headers)
        except Exception as e:  # noqa: BLE001
            # ⚠️ 网络层失败**绝不能**当成"登录态失效"。
            #    代理抖一下、DNS 超时、Privy 502 —— 这些和"你被登出了"是两回事,
            #    但上层对 AuthError 的处置是"发 TG 告警 + shutdown 调度器 + 进程退出",
            #    而 bot.ps1 没有守护进程,退了就一直停着。
            #    用户会收到"请重新 --login",但 session 文件根本没坏,重登是白做的。
            raise RetryableAuthError(f"Privy 续期请求失败(网络层,可重试): {e}") from e

        if resp.status_code >= 500:
            # 5xx 是 Privy 自己的问题,同样可重试
            logger.warning("Privy 续期返回 {},判为可重试", resp.status_code)
            raise RetryableAuthError(f"Privy 续期失败 HTTP {resp.status_code}(服务端错误,可重试)")

        if resp.status_code >= 400:
            # 4xx 才是真的"这个 refresh token 不好使了" —— 完整 body 是唯一有用的诊断信息
            logger.error("Privy 续期失败 status={} body={}", resp.status_code, (resp.text or "")[:2000])
            raise AuthError(f"Privy 续期失败 HTTP {resp.status_code},请重新执行 --login")

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.error("Privy 续期响应不是 JSON | body={}", (resp.text or "")[:2000])
            raise AuthError("Privy 续期响应无法解析") from e

        # 字段名未实测,多键兜底(models.pick 的用途就是这个)
        access = pick(data, "token", "access_token", "accessToken", "privy_access_token")
        refresh = pick(data, "refresh_token", "refreshToken", "privy_refresh_token")
        if not access:
            logger.error("Privy 续期响应里找不到 access token | body={}", (resp.text or "")[:2000])
            raise AuthError("Privy 续期响应缺少 access token,字段名可能已变(见上面的完整 body)")

        self._access = str(access)
        # Privy 可能不轮换 refresh token(不返回就沿用旧的),这里不能把它清空
        self._refresh = str(refresh) if refresh else self._refresh
        self._expires_at = decode_jwt_exp(self._access) or (time.time() + _FALLBACK_TTL_SEC)
        save_session(self._access, self._refresh, source="refresh", path=self._path)

    # ---------- 对外 ----------
    @property
    def has_session(self) -> bool:
        """是否存在可用的登录态文件(不保证 token 还没过期)"""
        with self._lock:
            self._ensure_loaded()
            return bool(self._access or self._refresh)

    def get_access_token(self) -> str:
        """取一个当前可用的 access token;拿不到一律抛 AuthError。"""
        with self._lock:
            self._ensure_loaded()
            if not (self._access or self._refresh):
                raise AuthError("尚未登录:先跑 python -m src.cli --login")

            if self._access and time.time() < self._expires_at - REFRESH_MARGIN_SEC:
                return self._access

            if self._refresh:
                self._do_refresh()
                return self._access or ""

            # 没有 refresh token:能用一天算一天,过期就只能重新登录
            if self._access and time.time() < self._expires_at:
                logger.warning("会话里没有 refresh token,当前 token 过期后需要重新 --login")
                return self._access
            raise AuthError("access token 已过期且无 refresh token,请重新执行 --login")

    def invalidate(self) -> None:
        """
        标记当前 token 失效,下次取用时强制续期。client 收到 401/403 时调用。

        ⚠️ 没有 refresh token 时**故意不清掉 access token**:
           403 也可能是 Cloudflare 拦截而非鉴权失败,清掉只会让后续所有请求
           以「未登录」的名义失败,把真实原因盖掉。
        """
        with self._lock:
            self._ensure_loaded()
            if not self._refresh:
                logger.warning("token 被标记失效,但会话里没有 refresh token,无法自动续期 —— 请重新 --login")
                return
            self._expires_at = 0.0
            logger.info("access token 已标记失效,下次取用时强制续期")


@lru_cache(maxsize=1)
def get_token_provider() -> TokenProvider:
    """
    进程内单例。

    ⚠️ 必须是单例:每个 client 各造一个 TokenProvider 的话,内存里的过期时间各算各的,
       会出现「一个刚续完期,另一个拿着旧 refresh token 又续一次」的相互踩踏。
    """
    return TokenProvider()


# ============================================================
# 交互式登录(Playwright 有头浏览器)
# ============================================================
def _unquote(v) -> str | None:
    """localStorage 里的值通常是 JSON 编码的(带引号),取出来要剥一层"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            s = json.loads(s)
        except Exception:  # noqa: BLE001
            s = s[1:-1]
    s = (s or "").strip()
    return s or None


def _looks_like_jwt(v: str | None) -> bool:
    """三段式且够长才算 —— 避免把 'true' / 'null' 这类占位值当成 token 存下来"""
    return bool(v) and v.count(".") == 2 and len(v) > 60


def _first(d: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = _unquote(d.get(k))
        if v:
            return v
    return None


def _fuzzy_access(d: dict) -> str | None:
    """键名兜底:含 privy + token,排除 refresh / id_token,且值长得像 JWT"""
    for k, raw in d.items():
        kl = k.lower()
        if "privy" in kl and "token" in kl and "refresh" not in kl and "id_token" not in kl:
            v = _unquote(raw)
            if _looks_like_jwt(v):
                return v
    return None


def _fuzzy_refresh(d: dict) -> str | None:
    for k, raw in d.items():
        kl = k.lower()
        if "privy" in kl and "refresh" in kl:
            v = _unquote(raw)
            if v:
                return v
    return None


def _extract_tokens(context) -> tuple[str | None, str | None, str]:
    """
    从浏览器上下文里抓 Privy token,返回 (access, refresh, 命中来源)。

    两条路径都走一遍(localStorage 优先,cookie 兜底),因为具体存哪儿没实测过。
    OAuth 登录会开弹窗页,所以要遍历 context 下的所有 page。
    """
    ls_all: dict = {}
    for page in list(context.pages):
        try:
            if page.is_closed():
                continue
            ls_all.update(page.evaluate(_DUMP_PRIVY_LS_JS) or {})
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 localStorage 失败(页面可能正在跳转): {}", e)

    access = _first(ls_all, _LS_ACCESS_KEYS) or _fuzzy_access(ls_all)
    refresh = _first(ls_all, _LS_REFRESH_KEYS) or _fuzzy_refresh(ls_all)
    source = "localStorage" if (access or refresh) else ""
    if ls_all:
        logger.debug("localStorage 里的 privy 键: {}", sorted(ls_all.keys()))

    if not (access and refresh):
        try:
            ck = {c["name"]: c.get("value") for c in context.cookies()}
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 cookie 失败: {}", e)
            ck = {}
        privy_ck = {k: v for k, v in ck.items() if "privy" in k.lower()}
        if privy_ck:
            logger.debug("cookie 里的 privy 键: {}", sorted(privy_ck.keys()))
        ck_access = _first(ck, _CK_ACCESS_KEYS) or _fuzzy_access(privy_ck)
        ck_refresh = _first(ck, _CK_REFRESH_KEYS) or _fuzzy_refresh(privy_ck)
        if not access and ck_access:
            access, source = ck_access, (source + "+cookie" if source else "cookie")
        if not refresh and ck_refresh:
            refresh = ck_refresh
            source = source if "cookie" in source else (source + "+cookie" if source else "cookie")

    return access, refresh, source or "none"


def _open_login_context(p, settings, cdp_url: str | None):
    """
    打开一个尽量"像真人在用"的浏览器上下文,返回 (context, closer)。

    两种模式:
      - cdp_url 给了 → attach 到用户自己启动的 Chrome(最可靠,Google 完全看不出异常)
      - 否则 → 用持久化 profile 起系统真实 Chrome,并抹掉自动化特征
    """
    # ---- 模式一:attach 到用户自己开的 Chrome ----
    if cdp_url:
        logger.info("attach 到已运行的浏览器: {}", cdp_url)
        browser = p.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # attach 模式下代理由那个浏览器自己决定,这里不覆盖
        return ctx, browser.close

    # ---- 模式二:持久化 profile + 真实 Chrome 渠道 ----
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    base: dict = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": False,
        "locale": "en-US",
        "args": list(_STEALTH_ARGS),
        "ignore_default_args": list(_DROP_DEFAULT_ARGS),
        # 不覆盖 UA:真实 Chrome 自带的 UA 与它的版本、指纹是自洽的,
        # 手写一个 Chrome/131 反而会和实际内核版本对不上,更容易被识破
    }
    if settings.fomo_proxy:
        # Playwright 只认 {"server": ...} 这种形状,不吃 httpx 的 proxies 字典
        base["proxy"] = {"server": settings.fomo_proxy}
        logger.info("登录浏览器走代理: {}", settings.fomo_proxy)

    last_err = None
    for channel in _LOGIN_CHANNELS:
        kwargs = dict(base)
        if channel:
            kwargs["channel"] = channel
        try:
            ctx = p.chromium.launch_persistent_context(**kwargs)
            label = channel or "bundled-chromium"
            if channel is None:
                logger.warning(
                    "没找到系统 Chrome/Edge,退回 Playwright 打包的 Chromium —— "
                    "FOMO 自己的邮箱登录可以用,但 Google 第三方登录大概率仍会被拦。"
                    "建议装个 Chrome,或改用 --login --cdp 附着到你自己的浏览器"
                )
            else:
                logger.info("登录浏览器: {}(系统真实浏览器,已抹掉自动化特征)", label)
            return ctx, ctx.close
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.debug("启动 {} 失败: {}", channel or "chromium", e)
    raise RuntimeError(f"所有浏览器渠道都启动失败,最后错误: {last_err}")


def interactive_login(timeout_sec: int = LOGIN_TIMEOUT_SEC, cdp_url: str | None = None) -> bool:
    """
    拉起有头浏览器,等用户自己在 fomo.family 上完成登录,然后抓 Privy token 存盘。

    ⚠️ 程序全程不碰账号密码 —— 只在用户登录成功后读浏览器里已经存在的 token。
    ⚠️ playwright 在这里才 import:HttpFomoClient 路径根本用不到浏览器,
       顶层 import 会让没装 chromium 的机器连 --run 都起不来。

    cdp_url: 形如 "http://127.0.0.1:9222"。给了就 attach 到用户自己启动的 Chrome ——
             Google 对自动化浏览器的封锁在这个模式下完全不存在(那就是一个普通 Chrome)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("未安装 playwright,请先执行: pip install playwright && playwright install chromium")
        return False

    settings = get_settings()
    logger.info("正在打开浏览器,请在窗口里自己完成登录(程序不会代填任何账号密码)…")
    deadline = time.time() + timeout_sec
    access = refresh = None
    source = "none"

    with sync_playwright() as p:
        try:
            context, closer = _open_login_context(p, settings, cdp_url)
        except Exception as e:  # noqa: BLE001
            logger.error("浏览器启动失败: {}", e)
            return False

        # 页面脚本跑之前先把 navigator.webdriver 抹掉
        try:
            context.add_init_script(_STEALTH_JS)
        except Exception as e:  # noqa: BLE001
            logger.debug("注入 stealth 脚本失败(不致命): {}", e)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(FOMO_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:  # noqa: BLE001
            # 首页没加载出来不代表登录不了(可能只是某个资源超时),继续轮询,别在这里放弃
            logger.warning("首页加载异常(继续等待登录): {}", e)

        logger.info("等待登录完成…最多等 {} 秒。登录成功后本窗口会自动关闭。", timeout_sec)
        logger.info("提示:如果 Google 登录被拒(This browser or app may not be secure),"
                    "改用 FOMO 的邮箱验证码登录,或看 README 的 --cdp 方案")
        while time.time() < deadline:
            if not context.pages:  # 用户把窗口全关了
                logger.error("浏览器已被关闭,登录未完成")
                break
            access, refresh, source = _extract_tokens(context)
            # access 必须像 JWT 才算数:登录中途 Privy 可能先写一个空壳值
            if _looks_like_jwt(access):
                logger.info("已检测到登录态 | 来源={} access={}", source, mask(access))
                break
            access = None
            time.sleep(_LOGIN_POLL_SEC)

        try:
            closer()
        except Exception:  # noqa: BLE001
            pass

    if not access:
        logger.error(
            "未能抓到 Privy access token。可能原因:①登录超时 ②token 键名与候选列表不符 —— "
            "把 LOG_LEVEL 改成 DEBUG 再跑一次,日志里会打印实际的 privy 键名"
        )
        return False
    if not refresh:
        # 没有 refresh token 不是致命错误:access token 还能用约 1 小时,只是到期要重新登录
        logger.warning("只抓到 access token,没抓到 refresh token —— 过期后需要重新 --login")

    save_session(access, refresh, source=source)
    get_token_provider.cache_clear()  # 同进程内后续调用要拿到新会话

    # ⚠️ cdp 模式 attach 的是**用户自己的 Chrome**,PROFILE_DIR 从头到尾没被写过
    #    (见 _open_login_context 的 cdp 分支:它在 PROFILE_DIR.mkdir 之前就 return 了)。
    #    监控照常能跑(那只要 API token),但跟单的真实下单只认 PROFILE_DIR ——
    #    于是"登录成功了"和"能不能下单"在这里悄悄分了岔。必须说出来。
    if cdp_url and not (PROFILE_DIR / "Default" / "Network" / "Cookies").exists():
        logger.warning(
            "⚠️ 你走的是 --cdp,它只借用你自己的 Chrome,**没有写 {} **。"
            "监控不受影响;但跟单的真实下单用的是那个 profile,现在还是空的。"
            "要用跟单下单的话,请再跑一次**不带 --cdp** 的 --login。", PROFILE_DIR,
        )
    return True
