"""
币名 / 公司名的**中文译名**,与底池对手股票的**事实**(公司全名、交易所)。

============ 这三行长什么样 ============
    🌱 inyourwalls · 首次建仓 · $CUM · Cummingtonite        ← A. 标题尾巴:英文全名(dexscreener.token_name)
    📝 Cummingtonite = 镁铁闪石                              ← C. 中文名(本模块 token_zh)
    🌊 底池 · USAR · USA Rare Earth, Inc.                    ← 既有的底池行(dexscreener.notable)
    🏢 USAR = 美国稀土公司 · 纳斯达克(NasdaqGM)上市          ← B. 股票说明(本模块 stock_info)
A 的英文全名不是本模块的事(DexScreener 那份响应里自带),本模块只管**翻译**与**股票事实**。

============ 来源(全部免 key、免鉴权、只读)============
事实只从 Yahoo 来:`GET query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=1d&interval=1d`
    → chart.result[0].meta.longName / fullExchangeName / instrumentType;查无时 result 为 null。
翻译两级:
  (a) 维基百科跨语言链接 = **正式中文名**(优先)。en.wikipedia 的 langlinks 给中文标题
      (可能是繁体,如 "鎂鐵閃石"),再用 zh.wikipedia 的 parse?variant=zh-cn 转成简体
      ("镁铁闪石")。两跳算**一次**网络调用(预算见下)。
      ⚠️ 消歧义页(pageprops.disambiguation)**当作没有**:"Mercury"/"AI" 这种会乱指。
      ⚠️ 中文条目名与英文相同(SpaceX → SpaceX)= 正式中文名就是它,**不再问 Google**,
         那一行整行不出现(没信息量)。
  (b) Google 免费通道(兜底,**非官方**):translate.googleapis.com/translate_a/single?client=gtx。
      ⚠️ 它会把 Cummingtonite 译成"铜明石"(错的)—— 这就是维基必须优先的理由。
      ⚠️ 它随时可能失效。失效 = 中文那一段消失,其余照常,**绝不**因它报错影响推送。

============ 安全:币名是攻击者可控的(任何人都能给币起名)============
翻译是「输入什么翻什么」,所以**输入侧过滤**是主防线(translatable):名字含
0x 地址 / http / t.me / @ / 连续 ≥26 位 base58 串 / < / > 之一 → 不送翻译,C 行不出现。
输出侧再兜一层(clean_output):译文含同样形态、或长度 > 输入的 4 倍 → 丢弃并 WARNING
(带 symbol,不带原文)。渲染前的 html.escape 在 formatter,与这里是三道独立的墙。

============ 缓存与预算 ============
SQLite 表 name_glossary(store.glossary_get / glossary_put):成功**永久**(名字/公司名不会变);
"查过了没有"缓存 30 天;HTTP/超时失败缓存 1 小时(一次抖动不该按住一整天)。
每 tick 网络调用**硬上限**:翻译 5 次(维基两跳算 1、Google 算 1)、Yahoo 5 次;
缓存命中不计;同一 tick 同一 key 只查一次(_memo)。超出的那条推送这一轮就没那一段 ——
一次性推送,过了就过了,可接受。
⚠️⚠️ 本模块的任何失败都不许影响推送主路径:public 方法不抛,失败记 WARNING、该段消失。

============ 为什么不复用 src/client.py ============
与 dexscreener 同一条理由:FomoClient 绑死 FOMO 的登录态,它抛的 AuthError 会停掉整个监控。
这里自建 curl_cffi 客户端(impersonate="chrome",走 fomo_proxy,与 dexscreener/pumpfun 一致)。
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from dataclasses import dataclass

from loguru import logger

from src import store
from src.config import get_settings

# ============================================================
# 端点与常量
# ============================================================
WIKI_EN_API = "https://en.wikipedia.org/w/api.php"
WIKI_ZH_API = "https://zh.wikipedia.org/w/api.php"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
# 维基要求带**描述性** User-Agent,否则会被拒(浏览器 UA 也会被限流)。只对维基的请求带。
WIKI_USER_AGENT = "FOMO-monitor/1.0 (+github.com/GakkiYuiMIO/FOMOTrade)"

# 单次请求超时。这些接口都在推送发送循环里被同步调用,8 秒是上限不是目标。
_TIMEOUT_SEC = 8.0

# 缓存 kind
KIND_TOKEN_ZH = "token_zh"
KIND_COMPANY_ZH = "company_zh"
KIND_STOCK_FACT = "stock_fact"

# 负缓存 TTL(查过了、两级都没有):30 天。名字不会变,但维基条目会新建,一个月重问一次。
TTL_MISS_SEC = 30 * 86400
# 失败缓存 TTL(网络/HTTP/JSON 失败):1 小时。一次抖动不该把这一段按住一个月。
TTL_ERROR_SEC = 3600

# 每 tick 网络调用上限。翻译(维基两跳算 1 次、Google 算 1 次)与 Yahoo 各自一本账。
# 5:一个 tick 通常只推几条,热门币又几乎全走缓存;真的一轮来 20 个新币,后面的少一段就少一段。
ROUND_TRANSLATE_CALLS = 5
ROUND_YAHOO_CALLS = 5

# 送去翻译的名字最长几个字符。再长就是营销文案,翻出来也没人读,还白占预算。
_MAX_TRANSLATE_CHARS = 64
# 显示 B 行的 instrumentType。别的(CRYPTOCURRENCY / INDEX / MUTUALFUND …)不是"上市股票"。
STOCK_TYPES = frozenset({"EQUITY", "ETF"})

# 可疑成分:地址 / 链接 / 提及 / Solana 地址形态 / 标签。命中即不送翻译、译文命中即丢弃。
# ⚠️ base58 串 ≥26 位:Solana 地址是 32~44 位,26 是留了余量的下限;英文里没有 26 个字母
#    连写且不含 0/O/I/l 的常用词。
# ⚠️ 不能整体加 IGNORECASE:那会让 base58 那段的 [A-HJ-NP-Z] 连小写 l 也收进去,
#    "Supercalifragilisticexpialidocious" 这种普通长词就被当成地址了。只对 http / t.me 忽略大小写。
_SUSPICIOUS = re.compile(
    r"0x[0-9a-fA-F]{6,}|(?i:http|t\.me)|@|[1-9A-HJ-NP-Za-km-z]{26,}|[<>]")
# 中日韩文字(含假名、谚文):名字本身含 CJK 就不用翻。
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
# 股票代码形态。ticker 来自 DexScreener 的对手 symbol(第三方字符串),只放行这种形态。
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,11}")
# 公司名后缀。查维基时先剥掉:维基条目叫 "NVIDIA" 不叫 "NVIDIA Corporation"。
# ⚠️ 只收公司组织形式,**不收** "Holdings"/"Group" 这类实词 —— "Apple Holdings" 剥成 "Apple"
#    会指到水果。Class A Common Stock 是 DexScreener 侧见过的尾巴,顺手一起剥。
_CORP_SUFFIX = re.compile(
    r",?\s+(?:Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|Limited|Co\.?|PLC|LLC|L\.P\.|N\.V\.|S\.A\.|AG"
    r"|Class [A-C](?: Common Stock)?)\s*$", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]*>")


# ============================================================
# 数据结构
# ============================================================
@dataclass(frozen=True)
class StockFact:
    """Yahoo 给的事实。三个字段都可能缺(缺了那半句消失,绝不编)。"""

    long_name: str | None
    exchange: str | None
    instrument_type: str | None


@dataclass(frozen=True)
class StockInfo:
    """给渲染层的股票说明:🏢 {ticker} = {company_zh} · {exchange}上市。company_zh 翻不出是 None。"""

    ticker: str
    long_name: str | None
    exchange: str | None
    company_zh: str | None


class UnavailableError(Exception):
    """网络 / 超时 / 非 2xx / 不是 JSON —— 与「查无」(返回 None)必须分开:两者的缓存 TTL 不同。"""


# ============================================================
# 纯函数(可脱网完整单测)
# ============================================================
def _flat(s) -> str:
    """叠平空白。"""
    return " ".join(str(s or "").split())


def norm_key(s) -> str:
    """缓存键归一化:lower + strip + 叠平空白。"""
    return _flat(s).strip().lower()


def _bare(s) -> str:
    """比较用:叠平空白、去掉首尾的 $ 与空白、小写。"""
    return _flat(s).strip().strip("$").strip().lower()


def same_as_symbol(name, symbol) -> bool:
    """名字与 symbol 相同(忽略大小写、忽略 $ 与首尾空白)→ 标题不重复、也不送翻译。"""
    a, b = _bare(name), _bare(symbol)
    return bool(a) and a == b


def same_text(a, b) -> bool:
    return norm_key(a) == norm_key(b)


def is_suspicious(text) -> bool:
    return bool(_SUSPICIOUS.search(str(text or "")))


def translatable(name, symbol) -> bool:
    """
    这个名字能不能送去翻译(输入侧过滤,**主防线**)。以下一律不翻、C 行整行不出现:
    名字缺失 / 与 symbol 相同 / 含 CJK / 纯数字纯符号 / 长度 < 2 / 太长 / 含可疑成分。
    """
    n = _flat(name)
    if len(n) < 2 or len(n) > _MAX_TRANSLATE_CHARS:
        return False
    if same_as_symbol(n, symbol):
        return False
    if _CJK.search(n):
        return False
    if not any(ch.isalpha() for ch in n):
        return False
    return not is_suspicious(n)


def clean_output(zh, source_text, tag) -> str | None:
    """
    译文的输出侧过滤:含可疑成分、或长度 > 输入的 4 倍 → 丢弃并 WARNING(带 symbol,不带原文)。
    """
    z = _flat(zh)
    if not z:
        return None
    if is_suspicious(z) or len(z) > 4 * max(1, len(_flat(source_text))):
        logger.warning("译文被丢弃(含可疑成分或过长) | {}", tag)
        return None
    return z


def strip_tags(s) -> str:
    """维基 displaytitle 是带 <span> 的 HTML → 剥标签、还原实体、叠平空白。"""
    return _flat(html.unescape(_HTML_TAG.sub("", str(s or ""))))


def strip_corp_suffix(name) -> str:
    """
    "USA Rare Earth, Inc." → "USA Rare Earth";"NVIDIA Corporation" → "NVIDIA"。
    最多剥三层("X Corp. Class A" 这种)。剥空了返回空串,调用方回退用全名。
    """
    s = _flat(name)
    for _ in range(3):
        t = _CORP_SUFFIX.sub("", s).strip()
        if t == s:
            break
        s = t
    return s


def parse_wiki_langlink(payload) -> str | None:
    """
    en.wikipedia langlinks 响应 → 中文标题(可能是繁体);没有条目 / 消歧义页 / 没有中文链接 → None。

    ⚠️ 消歧义判断必须在读 langlinks **之前**:"Mercury" 的消歧义页自己也带一条 zh 链接
       ("Mercury"),先读链接就把它当成正式译名收下了。
    """
    if not isinstance(payload, dict):
        return None
    pages = (payload.get("query") or {}).get("pages")
    if not isinstance(pages, dict):
        return None
    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page:
            continue
        props = page.get("pageprops")
        if isinstance(props, dict) and "disambiguation" in props:
            continue
        for ll in page.get("langlinks") or ():
            if isinstance(ll, dict) and ll.get("lang") == "zh":
                t = _flat(ll.get("*"))
                if t:
                    return t
    return None


def parse_wiki_display_title(payload) -> str | None:
    """zh.wikipedia parse?variant=zh-cn 响应 → 简体标题(剥掉 <span>)。"""
    if not isinstance(payload, dict):
        return None
    dt = (payload.get("parse") or {}).get("displaytitle")
    t = strip_tags(dt) if isinstance(dt, str) else ""
    return t or None


def parse_google(payload) -> str | None:
    """translate_a/single 响应:json[0] 是 [[译文段, 原文段, …], …],把每段 [0] 拼起来。"""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return None
    parts = [seg[0] for seg in payload[0]
             if isinstance(seg, list) and seg and isinstance(seg[0], str)]
    t = _flat("".join(parts))
    return t or None


def parse_yahoo(payload) -> StockFact | None:
    """v8/finance/chart 响应 → StockFact;查无(result 为 null / 空)→ None。"""
    if not isinstance(payload, dict):
        return None
    result = (payload.get("chart") or {}).get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return None
    meta = result[0].get("meta")
    if not isinstance(meta, dict):
        return None
    return StockFact(
        long_name=_flat(meta.get("longName")) or None,
        exchange=_flat(meta.get("fullExchangeName")) or None,
        instrument_type=_flat(meta.get("instrumentType")).upper() or None,
    )


# ============================================================
# HTTP —— 只用公开免鉴权端点;传输层是可注入的缝(测试用夹具顶替)
# ============================================================
class CurlTransport:
    """
    每线程一个 curl_cffi Session(libcurl 的 easy handle 不能跨线程),impersonate="chrome",
    走 fomo_proxy —— 与 dexscreener / pumpfun / binance_alpha 的客户端同一套惯例。
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

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> tuple[int, object]:
        """
        → (HTTP 状态码, 解析出的 JSON)。网络失败 / 不是 JSON → 抛 Unavailable。
        ⚠️ 非 2xx **不在这里判**:Yahoo 查无是 404 + 合法 JSON,得由调用方按端点语义看。
        """
        try:
            resp = self._session().get(url, params=params, headers=headers)
        except Exception as e:  # noqa: BLE001
            self.close()          # 连接可能已经废了,整池丢弃重建
            raise UnavailableError(f"请求失败: {e}") from e
        try:
            return resp.status_code, resp.json()
        except Exception as e:  # noqa: BLE001
            raise UnavailableError(f"HTTP {resp.status_code} 响应不是 JSON") from e


class NameClient:
    """
    四个端点的薄封装。每个方法:查到 → 值;**查无** → None;网络/HTTP/JSON 失败 → 抛 Unavailable。
    ⚠️ 抛 UnavailableError 是刻意的:两种结果的缓存 TTL 不同(30 天 vs 1 小时),
       用 None 混在一起就是"一次超时把这一段按住一个月"。
    """

    def __init__(self, transport=None, proxy: str | None = None) -> None:
        self._t = transport if transport is not None else CurlTransport(proxy)

    def close(self) -> None:
        try:
            self._t.close()
        except Exception:  # noqa: BLE001
            pass

    def _ok(self, url, params, headers=None):
        status, body = self._t.get_json(url, params, headers)
        if not (200 <= status < 300):
            raise UnavailableError(f"HTTP {status} | {url}")
        return body

    def wiki_langlink(self, title: str) -> str | None:
        body = self._ok(WIKI_EN_API, {
            "action": "query", "prop": "langlinks|pageprops", "ppprop": "disambiguation",
            "titles": title, "lllang": "zh", "redirects": 1, "format": "json",
        }, {"User-Agent": WIKI_USER_AGENT})
        return parse_wiki_langlink(body)

    def wiki_display_title(self, zh_title: str) -> str | None:
        body = self._ok(WIKI_ZH_API, {
            "action": "parse", "page": zh_title, "prop": "displaytitle",
            "variant": "zh-cn", "redirects": 1, "format": "json",
        }, {"User-Agent": WIKI_USER_AGENT})
        return parse_wiki_display_title(body)

    def google_translate(self, text: str) -> str | None:
        body = self._ok(GOOGLE_TRANSLATE_URL, {
            "client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text,
        })
        return parse_google(body)

    def yahoo_chart(self, ticker: str) -> StockFact | None:
        status, body = self._t.get_json(YAHOO_CHART_URL.format(ticker=ticker),
                                        {"range": "1d", "interval": "1d"})
        # 查无 = 404 + {"chart": {"result": null, "error": {...}}}。别的非 2xx 才是失败。
        if isinstance(body, dict) and isinstance((body.get("chart") or {}).get("error"), dict):
            return None
        if not (200 <= status < 300):
            raise UnavailableError(f"HTTP {status} | yahoo {ticker}")
        return parse_yahoo(body)


# ============================================================
# 词汇表 —— 缓存 + 每 tick 预算 + 两级翻译
# ============================================================
_NO_BUDGET = object()


class NameGlossary:
    """
    ⚠️ 每个 watcher 各持一份(与 PoolQuoteLookup 同一条理由):预算是按 tick 算的,
       共用一份会让"这一轮打了几个请求"变得不可预测。
    ⚠️ 所有 public 方法**不抛**:缓存读写失败当没缓存,网络失败按 1 小时负缓存,该段消失。
    """

    def __init__(self, client: NameClient | None = None, conn_factory=None,
                 translate_calls: int = ROUND_TRANSLATE_CALLS,
                 yahoo_calls: int = ROUND_YAHOO_CALLS) -> None:
        self._client = client if client is not None else NameClient()
        # 缓存连接的来源。默认 store.get_conn(每次开一个短连接,与 pumpfun 的用法一致);
        # 测试注入一个复用内存库的上下文管理器。
        self._conn_factory = conn_factory if conn_factory is not None else store.get_conn
        self._limits = {"translate": int(translate_calls), "yahoo": int(yahoo_calls)}
        self._used = {"translate": 0, "yahoo": 0}
        self._memo: dict[tuple[str, str], object] = {}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def begin_round(self) -> None:
        """每 tick 开头调一次:预算归零、同 tick 的 memo 清空。"""
        self._used = {"translate": 0, "yahoo": 0}
        self._memo = {}

    # ---- 对外 --------------------------------------------------------------
    def token_zh(self, name, symbol, *, network: bool = True) -> str | None:
        """
        币的英文全名 → 中文名;不翻 / 翻不出 / 译文等于原文 → None(C 行整行不出现)。
        network=False 时**只读缓存**(转入推送用:不为它新开请求)。
        """
        try:
            n = _flat(name)
            if not translatable(n, symbol):
                return None
            return self._translate(KIND_TOKEN_ZH, n, n, tag=_flat(symbol) or "?", network=network)
        except Exception as e:  # noqa: BLE001
            logger.warning("币名翻译异常,该段不显示 | {} | {}", _flat(symbol), e)
            return None

    def stock_info(self, ticker) -> StockInfo | None:
        """
        底池对手(币股)的说明。事实只从 Yahoo 来;查无 / 不是 EQUITY·ETF → None(B 行不出现)。
        公司中文名翻不出时 company_zh 为 None,其余照给(B 行仍显示交易所)。
        """
        try:
            t = _bare(ticker).upper()
            if not _TICKER.fullmatch(t):
                return None
            fact = self._stock_fact(t)
            if fact is None or (fact.instrument_type or "") not in STOCK_TYPES:
                return None
            zh = None
            if fact.long_name and translatable(fact.long_name, t):
                zh = self._translate(KIND_COMPANY_ZH, fact.long_name,
                                     strip_corp_suffix(fact.long_name) or fact.long_name, tag=t)
            return StockInfo(ticker=t, long_name=fact.long_name, exchange=fact.exchange,
                             company_zh=zh)
        except Exception as e:  # noqa: BLE001
            logger.warning("股票说明查询异常,该行不显示 | {} | {}", _flat(ticker), e)
            return None

    # ---- 缓存 --------------------------------------------------------------
    def _cache_get(self, kind: str, key: str) -> tuple[bool, str | None]:
        """→ (有没有未过期的行, value)。读失败当没有。"""
        try:
            with self._conn_factory() as conn:
                row = store.glossary_get(conn, kind, key, time.time())
        except Exception as e:  # noqa: BLE001
            logger.warning("词汇表读取失败,当作未缓存 | {} | {}", kind, e)
            return False, None
        if row is None:
            return False, None
        return True, row["value"]

    def _cache_put(self, kind: str, key: str, value: str | None, source: str,
                   ttl: float | None) -> None:
        expires = None if ttl is None else int(time.time() + ttl)
        try:
            with self._conn_factory() as conn:
                store.glossary_put(conn, kind, key, value, source, expires)
        except Exception as e:  # noqa: BLE001
            logger.warning("词汇表写入失败(下轮重查) | {} | {}", kind, e)

    def _take(self, budget: str) -> bool:
        """扣一次预算;超了返回 False(这一段这一轮就没有)。"""
        if self._used[budget] >= self._limits[budget]:
            return False
        self._used[budget] += 1
        return True

    # ---- 翻译 --------------------------------------------------------------
    def _translate(self, kind: str, text: str, wiki_term: str, tag: str,
                   *, network: bool = True) -> str | None:
        key = norm_key(text)
        memo_key = (kind, key)
        if memo_key in self._memo:
            return self._memo[memo_key]  # type: ignore[return-value]
        found, value = self._cache_get(kind, key)
        if found:
            self._memo[memo_key] = value
            return value
        if not network:
            return None
        result = self._translate_net(kind, key, text, wiki_term, tag)
        if result is _NO_BUDGET:
            return None          # 不入 memo、不入缓存:下一 tick 重来
        self._memo[memo_key] = result
        return result  # type: ignore[return-value]

    def _translate_net(self, kind: str, key: str, text: str, wiki_term: str, tag: str):
        # ---- 一级:维基百科跨语言链接(两跳算一次)----
        if not self._take("translate"):
            return _NO_BUDGET
        try:
            zh_title = self._client.wiki_langlink(wiki_term)
            if isinstance(zh_title, str) and zh_title:
                # 繁→简。这一跳失败(None)就退回用 langlinks 给的标题 —— 仍是真的,只是可能繁体
                simplified = self._client.wiki_display_title(zh_title)
                zh = simplified if isinstance(simplified, str) and simplified else zh_title
                zh = clean_output(zh, text, tag)
                if zh is None:
                    self._cache_put(kind, key, None, "wiki-bad", TTL_MISS_SEC)
                    return None
                if same_text(zh, text):
                    # 正式中文名就是它自己(SpaceX)—— 确定的答案,永久缓存,不再问 Google
                    self._cache_put(kind, key, None, "wiki-same", None)
                    return None
                self._cache_put(kind, key, zh, "wiki", None)
                return zh
        except UnavailableError as e:
            logger.warning("维基查询失败,1 小时内不重试 | {} | {}", tag, e)
            self._cache_put(kind, key, None, "error", TTL_ERROR_SEC)
            return None
        # ---- 二级:Google 免费通道(非官方,随时可能失效)----
        if not self._take("translate"):
            return _NO_BUDGET
        try:
            zh = self._client.google_translate(text)
        except UnavailableError as e:
            logger.warning("Google 翻译失败,1 小时内不重试 | {} | {}", tag, e)
            self._cache_put(kind, key, None, "error", TTL_ERROR_SEC)
            return None
        if not isinstance(zh, str) or not zh:
            # 响应结构变了(非官方接口的常态)—— 按失败处理,别把"没有"缓存一个月
            logger.warning("Google 翻译响应无法解析,1 小时内不重试 | {}", tag)
            self._cache_put(kind, key, None, "error", TTL_ERROR_SEC)
            return None
        zh = clean_output(zh, text, tag)
        if zh is None or same_text(zh, text):
            self._cache_put(kind, key, None, "miss", TTL_MISS_SEC)
            return None
        self._cache_put(kind, key, zh, "google", None)
        return zh

    # ---- 股票事实 ------------------------------------------------------------
    def _stock_fact(self, ticker: str) -> StockFact | None:
        key = norm_key(ticker)
        memo_key = (KIND_STOCK_FACT, key)
        if memo_key in self._memo:
            return self._memo[memo_key]  # type: ignore[return-value]
        found, value = self._cache_get(KIND_STOCK_FACT, key)
        if found:
            fact = None if value is None else _fact_from_json(value)
            self._memo[memo_key] = fact
            return fact
        if not self._take("yahoo"):
            return None
        try:
            fact = self._client.yahoo_chart(ticker)
        except UnavailableError as e:
            logger.warning("Yahoo 查询失败,1 小时内不重试 | {} | {}", ticker, e)
            self._cache_put(KIND_STOCK_FACT, key, None, "error", TTL_ERROR_SEC)
            return None
        if fact is None:
            self._cache_put(KIND_STOCK_FACT, key, None, "miss", TTL_MISS_SEC)
        else:
            self._cache_put(KIND_STOCK_FACT, key, _fact_to_json(fact), "yahoo", None)
        self._memo[memo_key] = fact
        return fact


def _fact_to_json(f: StockFact) -> str:
    return json.dumps({"long_name": f.long_name, "exchange": f.exchange,
                       "instrument_type": f.instrument_type}, ensure_ascii=False)


def _fact_from_json(s: str) -> StockFact | None:
    try:
        d = json.loads(s)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return StockFact(long_name=_flat(d.get("long_name")) or None,
                     exchange=_flat(d.get("exchange")) or None,
                     instrument_type=_flat(d.get("instrument_type")).upper() or None)
