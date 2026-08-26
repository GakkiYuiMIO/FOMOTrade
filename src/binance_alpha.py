"""
币安 Alpha 新上架监控 —— 有新代币进入 Alpha 全量名单就推一条 Telegram。

⚠️⚠️ 这个信号**只做通知,永远不接跟单执行器**。不进 CopyConfig、不写 copytrade_signals。
   「币安上了个新币」与「名单里的人掏钱买了」是完全不同的含义,混进执行器就是拿它当买入信号。

为什么以「全量名单新增」为主干、板块只当标注:
  App 钱包页那个「市場焦點」板块是币安运营编排出来的,没有任何文档,改名/下线都不会通知谁;
  而 Alpha 全量名单是官方文档化的公开集合,稳定得多。板块哪天没了,推送退化成
  「Alpha 新上架 XXX」,少一行标注而已,功能不死。

为什么不复用 src/client.py:
  FomoClient 绑死 prod-api.fomo.family 且携带 Privy 登录态,它抛的 AuthError 会被
  cli._tick_job 捕获后 **sched.shutdown()** —— 币安接口抖一下绝不该有权力停掉整个监控。
  所以这里自建 httpx 客户端,且**所有异常一律自己吞掉**(见 run_once)。

用到的两个端点全部公开免登录,**不碰任何币安凭据、不触任何交易接口**。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import httpx
from loguru import logger

from src import store
from src.config import get_settings
from src.formatter import render_alpha_batch, render_alpha_listing
from src.models import normalize_network, normalize_token_address

# ============================================================
# 端点与常量
# ============================================================
# 主干:Alpha 全量名单(官方文档化,只需 Content-Type,免登录免 key)
ALPHA_LIST_URL = (
    "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
# 标注:板块成员榜(App 钱包页「市場焦點」那几栏的数据源)
SECTOR_RANK_URL = (
    "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse"
    "/unified/rank/list"
)
# 板块接口按链查询。Alpha 板块目前全在 BSC(实测 665 条名单里 chainId 清一色 56),
# 做成模块常量而不是配置项:多一个谁都不会改的旋钮只会增加配错的机会。
SECTOR_CHAIN_ID = "56"
# 单次板块查询要的条数。⚠️ 服务端会按这个值截断 tokens,所以**条数上限保护不能只看
#    len(tokens)**(见 _sector_members 里的说明)。
SECTOR_PAGE_SIZE = 100

# 水位线在 runtime_state 里的键。⚠️ 必须落库:只存内存的话进程一重启就退回冷启动,
# 重启期间上架的币会被静默播种吃掉,永远不推。
STATE_KEY = "binance_alpha_watermark_ms"

# 板块条数上限。⚠️ 这道闸不是防御性编程,是防一个**实测存在**的坑:
#    传一个不存在的 tabId(如 63)币安**不报错**,而是返回全量 3139 个代币。
#    哪天币安下线 tabId 61,映射表会瞬间把几千个币全标成「股票 Meme 幣」——
#    推送里那一行标注就成了纯粹的假信息。超限就整份丢弃并告警,绝不照单全收。
#    500 的来由:实测目标板块 21 个成员,一级板块最多也就百来个;而失效时的返回是
#    三千多。500 离两边都足够远。
SECTOR_MAX_MEMBERS = 500

# 单轮最多逐条推几个。实测上新频率极低(近 7 天 2 个、近 30 天 8 个),
# 正常永远碰不到这个上限;它防的是「币安某天把一批老币的 listingTime 集体重写成今天」
# 这种上游异常 —— 那会一次推出几百条,用户当场静音,这个功能就死了。
# 超限时改推一条汇总(见 formatter.render_alpha_batch),信息不丢、消息只有一条。
MAX_PUSH_PER_ROUND = 10

_TIMEOUT_SEC = 15.0
# 币安风控严。单轮就一次全量 + 每个板块一次,不重试 —— 5 分钟后自然还有下一轮,
# 而一个天级事件根本不差这 5 分钟。重试只会平白多打一次。
_HEADERS = {"Content-Type": "application/json"}


@dataclass(frozen=True)
class AlphaToken:
    """
    名单里的一行。只留推送用得上的字段。

    ⚠️ listing_time_ms 是唯一的必填项 —— 整个水位线机制都架在它上面,
       取不到值的行连「是不是新的」都判不了,直接丢弃(见 parse_tokens)。
    """

    listing_time_ms: int
    symbol: str | None = None
    name: str | None = None
    chain_id: str | None = None
    chain_name: str | None = None
    contract_address: str | None = None
    market_cap: str | None = None
    holders: str | None = None
    alpha_id: str | None = None

    @property
    def sector_key(self) -> tuple[str, str] | None:
        """板块映射的查表键。地址归一化两边必须用同一个函数,否则大小写一差就永远命不中。"""
        ca = normalize_token_address(self.contract_address)
        if not self.chain_id or not ca:
            return None
        return (str(self.chain_id).strip(), ca)


# ============================================================
# 解析
# ============================================================
def _as_int(v) -> int | None:
    """任意输入 → int。判空一律 is None:0 是有意义的真实值,不能被真值判断吞掉。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _as_text(v) -> str | None:
    """任意输入 → 非空字符串,空串/空白一律 None(缺失整行消失,绝不打 N/A)。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_tokens(payload) -> list[AlphaToken] | None:
    """
    响应体 → 代币列表。结构不对返回 None(与「拉取失败」同义),

    ⚠️ 返回 None 和返回 [] 语义不同:None = 这轮不知道名单长什么样(水位线绝不能动),
       [] = 确实一个都没有。把两者混起来会让一次结构变更把水位线推到无穷。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("code") != "000000" or payload.get("success") is not True:
        logger.warning("币安 Alpha 名单返回非成功码: code={} success={}",
                       payload.get("code"), payload.get("success"))
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        logger.warning("币安 Alpha 名单 data 不是数组: {}", type(rows).__name__)
        return None

    out: list[AlphaToken] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped += 1
            continue
        ms = _as_int(row.get("listingTime"))
        if ms is None or ms <= 0:
            # 没有上架时间就判不了新旧。实测 665/665 条都有值,真出现说明上游变了,要留痕
            dropped += 1
            continue
        out.append(AlphaToken(
            listing_time_ms=ms,
            symbol=_as_text(row.get("symbol")),
            name=_as_text(row.get("name")),
            chain_id=_as_text(row.get("chainId")),
            chain_name=_as_text(row.get("chainName")),
            contract_address=_as_text(row.get("contractAddress")),
            market_cap=_as_text(row.get("marketCap")),
            holders=_as_text(row.get("holders")),
            alpha_id=_as_text(row.get("alphaId")),
        ))
    if dropped:
        logger.warning("币安 Alpha 名单有 {} 行缺 listingTime,已跳过(总 {} 行)", dropped, len(rows))
    return out


def parse_sector(payload, *, rank_type: int, tab_id: int) -> list[tuple[str, str]] | None:
    """
    板块响应 → [(chainId, 归一化后的 CA), …]。失败或**判定该板块已失效**时返回 None。

    ⚠️ 条数上限必须看 total,不能只看 len(tokens):请求带了 size=100,服务端会把
       tokens 截到 100 条 —— 也就是说失效时返回的三千多条在 tokens 里**永远表现为 100**,
       只看 len 的话这道闸一辈子不会响。total 才是那个真实数字。
       两个都查:total 缺失或是脏值时还有 len 兜底。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("code") != "000000" or payload.get("success") is not True:
        logger.warning("板块 rankType={} tabId={} 返回非成功码: {}", rank_type, tab_id, payload.get("code"))
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("tokens")
    if not isinstance(rows, list):
        return None

    total = _as_int(data.get("total"))
    count = total if total is not None and total >= 0 else len(rows)
    if count > SECTOR_MAX_MEMBERS or len(rows) > SECTOR_MAX_MEMBERS:
        # 未知 tabId 币安不报错、直接返回全量 —— 照单全收会把几千个币标成这个板块
        logger.error(
            "板块 rankType={} tabId={} 返回 {} 个成员,超过 {} 的合理上限 —— "
            "判定该板块已失效(币安对未知 tabId 不报错、直接返回全量),本轮丢弃该板块映射",
            rank_type, tab_id, count, SECTOR_MAX_MEMBERS,
        )
        return None

    out: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain = _as_text(row.get("chainId"))
        ca = normalize_token_address(_as_text(row.get("contractAddress")))
        if chain and ca:
            out.append((chain, ca))
    return out


# ============================================================
# HTTP 客户端 —— 只用公开免登录端点,异常一律吞掉
# ============================================================
class BinanceAlphaClient:
    """
    ⚠️ 本类的每个 fetch_* **都不抛异常**,失败一律返回 None 并记日志。
       它跑在调度器的 worker 线程里,一个逃逸的异常最坏会被 APScheduler 记成
       job 崩溃 —— 而这个功能没有任何理由影响别的 job。
    """

    def __init__(self, proxy: str | None = None) -> None:
        s = get_settings()
        self._proxy = s.fomo_proxy if proxy is None else proxy
        self._lock = threading.Lock()
        self._shared: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        """复用连接池:一轮里全量 + 各板块共用同一条 TLS 连接,少几次握手。"""
        with self._lock:
            if self._shared is None:
                self._shared = httpx.Client(timeout=_TIMEOUT_SEC, proxy=self._proxy,
                                            headers=_HEADERS)
            return self._shared

    def close(self) -> None:
        with self._lock:
            c, self._shared = self._shared, None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_tokens(self) -> list[AlphaToken] | None:
        """Alpha 全量名单。任何失败(网络/超时/结构变更)一律 None。"""
        try:
            resp = self._http().get(ALPHA_LIST_URL)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("币安 Alpha 名单拉取失败(下一轮重试): {}", e)
            self.close()          # 连接可能已经废了,整池丢弃重建
            return None
        return parse_tokens(payload)

    def fetch_sector(self, rank_type: int, tab_id: int) -> list[tuple[str, str]] | None:
        """板块成员。失败或判定失效一律 None —— 调用方据此**跳过标注,但照常推主干**。"""
        body = {
            "rankType": rank_type, "period": 50, "chainId": SECTOR_CHAIN_ID,
            "tabId": tab_id, "size": SECTOR_PAGE_SIZE, "sortBy": 10,
        }
        try:
            resp = self._http().post(SECTOR_RANK_URL, json=body)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("板块 rankType={} tabId={} 拉取失败(不影响主干推送): {}",
                           rank_type, tab_id, e)
            self.close()
            return None
        return parse_sector(payload, rank_type=rank_type, tab_id=tab_id)


# ============================================================
# 水位线巡检
# ============================================================
class AlphaWatcher:
    """
    独立的第二个 APScheduler job。**不塞进 poller.tick()**,理由:
      1) tick 实测 5~18 秒,轮询间隔 27 秒,余量本来就只剩几秒;
      2) tick 里抛 AuthError 会 sched.shutdown() 停掉整个监控(cli.py:829-836)——
         币安接口抖一下绝不该有权力停掉 FOMO 推送链路;
      3) 节奏不匹配:FOMO 名单是 27 秒级,Alpha 上新是天级事件。
    """

    def __init__(self, notifier, client: BinanceAlphaClient | None = None,
                 sectors: list[tuple[int, int, str]] | None = None) -> None:
        s = get_settings()
        self._notifier = notifier
        self._client = client if client is not None else BinanceAlphaClient()
        self._sectors = s.alpha_sectors if sectors is None else sectors

    # ---- 对外唯一入口 ----------------------------------------------------
    def run_once(self) -> int:
        """
        调度器直接调这个,返回本轮真正发出去的消息条数(调度器不看返回值,给测试用)。

        ⚠️⚠️ **本方法绝不抛异常**。它是这个功能与调度器之间的唯一接触面:
           异常逃逸出去,轻则 APScheduler 打一堆 job 崩溃栈,重则被上层
           某个 except 当成"该停机了"—— 而这个功能的任何故障都不该影响别的 job。
        """
        try:
            return self._check()
        except Exception as e:  # noqa: BLE001
            logger.exception("币安 Alpha 巡检异常,下一轮继续: {}", e)
            return 0

    def close(self) -> None:
        """退出时释放连接池。⚠️ 同样不抛 —— 它跑在 cmd_run 的 finally 里。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 内部 ------------------------------------------------------------
    def _check(self) -> int:
        tokens = self._client.fetch_tokens()
        if tokens is None:
            # 拉取失败 → 水位线**一动不动**。动了就等于把这段时间的上新静默吃掉
            return 0
        if not tokens:
            logger.warning("币安 Alpha 名单为空,本轮跳过(水位线不动)")
            return 0

        newest = max(t.listing_time_ms for t in tokens)
        # ⚠️ 数据库连接**只在这一小段里持有**:下面的板块拉取和推送都要打网络,
        #    最坏十几秒。虽然 WAL 下读不挡写,但把一条连接横跨整段网络 IO 挂着,
        #    只会让 WAL 迟迟无法 checkpoint。开两次短连接的代价可以忽略。
        with store.get_conn() as conn:
            mark = _as_int(store.get_state(conn, STATE_KEY))
            if mark is None:
                # ⚠️⚠️ 冷启动**必须静默播种**:第一次跑时名单里 665 个币全都"比水位线新",
                #    照推会把 Telegram 当场打爆、用户直接静音,这个功能第一天就死了。
                self._save_mark(conn, newest)
                logger.info("币安 Alpha 冷启动:水位线播种为 {}(名单 {} 个代币),本轮一条都不推",
                            newest, len(tokens))
                return 0

        # ⚠️ 严格大于,不能用 >=。水位线的语义是「这个时刻(含)之前的都已经处理过」,
        #    用 >= 的话:名单里 listingTime 恰好等于水位线的那个币**每一轮都会被重推**,
        #    5 分钟一条直到用户静音 —— 而这正是上一次上架的那个币,必然存在。
        #    代价是理论上会漏推「晚到、且 listingTime 与水位线一模一样」的币;
        #    但币安是整份名单一起更新的,同一毫秒的几个币必然在同一次响应里一起出现、
        #    一起被判新,这个漏洞在现实中打不着。用 >= 换来的却是必然的重复推送。
        fresh = sorted((t for t in tokens if t.listing_time_ms > mark),
                       key=lambda t: t.listing_time_ms)
        if not fresh:
            return 0

        # 板块标注是**尽力而为**:这一步失败只是少一行标注,主干照推(见 _sector_map)
        sector_map = self._sector_map()
        logger.info("币安 Alpha 新上架 {} 个(水位线 {} → {})", len(fresh), mark, newest)
        sent = self._push(fresh, sector_map)
        # ⚠️ 先推后写:反过来的话推送崩在中间就永久漏掉了。
        # ⚠️ 推送**是否送达**不影响水位线前移 —— notifier 自己已经重试三次,
        #    还失败就记 error;不前移的话 TG 长期不可用时会每轮重来一遍,
        #    恢复的那一刻一次性全炸出来。
        with store.get_conn() as conn:
            self._save_mark(conn, max(mark, newest))
        return sent

    def _save_mark(self, conn, ms: int) -> None:
        """
        水位线落库。⚠️ 写事务必须短:另一个线程的 tick 正拿 BEGIN IMMEDIATE 写事件,
        这里多待一毫秒就是它多等一毫秒。
        """
        with store.tx(conn):
            store.set_state(conn, STATE_KEY, str(int(ms)))

    def _sector_map(self) -> dict[tuple[str, str], str]:
        """
        (chainId, CA) → 板块名。⚠️ 拉不到就是空表,**绝不让它影响主干** ——
        缺了只是推送里少一行标注(缺失整行消失,不打 N/A)。
        """
        out: dict[tuple[str, str], str] = {}
        for rank_type, tab_id, label in self._sectors:
            try:
                members = self._client.fetch_sector(rank_type, tab_id)
            except Exception as e:  # noqa: BLE001
                # client 契约上不抛,但它是可替换的依赖 —— 主干绝不能被它拖下水
                logger.warning("板块 rankType={} tabId={} 异常(不影响主干推送): {}",
                               rank_type, tab_id, e)
                continue
            if members is None:
                continue
            for key in members:
                # 先配置到的板块优先,保证同一个币的标注是确定的
                out.setdefault(key, label)
        return out

    def _push(self, fresh: list[AlphaToken], sector_map: dict[tuple[str, str], str]) -> int:
        if len(fresh) > MAX_PUSH_PER_ROUND:
            logger.error("本轮 Alpha 新上架 {} 个,超过逐条推送上限 {} —— 改推一条汇总。"
                         "这个量级不正常,多半是上游把一批老币的 listingTime 重写了",
                         len(fresh), MAX_PUSH_PER_ROUND)
            return 1 if self._notifier.send(render_alpha_batch(_render_args_batch(fresh))) else 0

        sent = 0
        for t in fresh:
            key = t.sector_key
            sector = sector_map.get(key) if key is not None else None
            text = render_alpha_listing(
                symbol=t.symbol,
                name=t.name,
                network_id=normalize_network(t.chain_id),
                chain_name=t.chain_name,
                contract_address=t.contract_address,
                listing_time_ms=t.listing_time_ms,
                market_cap=t.market_cap,
                holders=t.holders,
                sector=sector,
            )
            if self._notifier.send(text):
                sent += 1
            else:
                logger.error("币安 Alpha 上新推送失败 | symbol={} ca={}", t.symbol, t.contract_address)
        return sent


def _render_args_batch(fresh: list[AlphaToken]) -> list[tuple[str | None, int]]:
    """汇总消息只需要 (符号, 上架时间),不把整个 dataclass 泄进 formatter(铁律:纯函数不认业务类型)"""
    return [(t.symbol, t.listing_time_ms) for t in fresh]
