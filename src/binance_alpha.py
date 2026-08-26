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
import time
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
# 板块接口按链查询,固定问 BSC —— 要标注的那个板块就在 BSC。
# 做成模块常量而不是配置项:多一个谁都不会改的旋钮只会增加配错的机会。
# ⚠️⚠️ 这里**不能**被读成「名单也全是 BSC」。此处原先写着「实测 665 条名单里 chainId
#    清一色 56」,**那是错的**:一次真实调用的实际分布是
#    56:490 / CT_501:70 / 8453:42 / 1:38 / CT_784:13 / 42161:4 / 146:4 / CT_195:3 / 59144:1,
#    BSC 只占 490/665。错注释比没注释更糟 —— 信了它就会拿 chainId 当纯数字去映射链,
#    于是 70 个 Solana 币(chainId 是 `CT_501`,不是数字)当场丢掉整行链接。
#    链的解析一律走 resolve_network(优先 chainName),不要回到猜 chainId 的老路。
SECTOR_CHAIN_ID = "56"
# 单次板块查询要的条数。⚠️ 服务端会按这个值截断 tokens,所以**条数上限保护不能只看
#    len(tokens)**(见 _sector_members 里的说明)。
SECTOR_PAGE_SIZE = 100

# 水位线在 runtime_state 里的键。⚠️ 必须落库:只存内存的话进程一重启就退回冷启动,
# 重启期间上架的币会被静默播种吃掉,永远不推。
STATE_KEY = "binance_alpha_watermark_ms"

# 台账是否已经初始化过。⚠️ 必须是独立的一位,不能靠「台账表是不是空的」来猜:
#    清理策略会把掉出宽限窗口的行删掉,表空掉是**正常状态**;
#    把它当成"没初始化过"会让一个迟到的同毫秒代币被当作历史存量二次播种、再次丢失。
LEDGER_KEY = "binance_alpha_ledger_ready"
LEDGER_READY = "1"

# 候选窗口的宽限量。候选 = listingTime > 水位线 - 这个量,且不在已推台账里。
# ⚠️⚠️ 为什么不能只用 `listingTime > 水位线`(见 store 里 binance_alpha_pushed 的注释):
#    实测 665 条名单里 32 组代币共享同一个 listingTime、覆盖 100 个币(15%),最大一组 10 个。
#    同组的币分两轮进名单时,后到的那个不满足严格大于,**永久静默丢失、零日志**。
#    也不能改成 `>=` —— 那是拿静默漏推换必然重复推送。去重交给台账主键,
#    窗口只负责决定「多晚的迟到还认」。
# ⚠️ 7 天的来由:代币从进名单到被我们看见最多差一个巡检间隔(5 分钟),7 天比它宽出三个
#    数量级;而上新是天级事件(近 30 天 8 个),7 天的台账稳态只有几十行,清理毫无压力。
#    再放长也不会更安全:真正兜底的是台账主键,不是窗口。
GRACE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000

# 水位线能接受的「未来」余量。
# ⚠️⚠️ 上界必须夹。没有它的话,名单里混进一行 listingTime=99999999999999(公元 5138 年)
#    就会把水位线顶到那里,此后**再也没有任何代币比水位线新** —— 功能永久静默死亡,
#    没有日志、没有告警,用户只会以为币安很久没上新了。
# ⚠️ 又不能一刀切把未来时间戳全判脏:名单里确实会出现尚未到上架时刻的币
#    (formatter 那个「即将上架」分支就是为它写的),预挂牌是正常业务。
#    7 天:比实测的预告提前量(几小时)宽出两个数量级,离脏值又远得看不见。
FUTURE_SKEW_MS = 7 * 24 * 60 * 60 * 1000

# 币安 chainName → 本仓库内部链标识。
# ⚠️⚠️ 为什么按 chainName 而不是 chainId:币安的 chainId **不是纯数字** ——
#    实测名单里有 CT_501(Solana)/ CT_784(Sui)/ CT_195(TRON) 这种前缀式 ID,
#    拿它当数字猜必然漏,而 Solana 恰恰是本仓库完整支持、最该出链接的一条链。
#    chainName 是接口自己给的人类可读链名(实测 665 条全部有值),稳定且自解释。
# ⚠️ 只收录**本仓库确实有 slug 的链**(models.NETWORK_SLUG / GMGN_SLUG)。
#    没收录的链(Sonic / Arbitrum / Linea / TRON / Sui)会退回原始 chainId:
#    链接那一行整行消失,**其余照常推**(缺失整行消失,绝不硬拼一个 404 链接)。
# ⚠️ 这张表是**权威**,不是"锦上添花的快捷方式":把某条链从表里删掉,它的链接行就真的
#    消失(有测试钉着)。所以别在下游补一层"通用别名表兜底"—— 那会让这张表的每一条
#    都被遮蔽成死重量、两份真相各自 drift,删掉一条也没人发现。要加链就改这里。
_CHAIN_NAME_TO_NETWORK = {
    "bsc": "bsc",
    "base": "base",
    "ethereum": "ethereum",
    "solana": "solana",
}

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
        """
        板块映射的查表键。地址归一化两边必须用同一个函数,否则大小写一差就永远命不中。

        ⚠️ 这里用的是**币安原始 chainId**(不是 resolve_network 的输出):
           板块响应与名单响应都出自币安,两边在同一个 ID 空间里,不该绕道我们的内部标识。
        """
        ca = normalize_token_address(self.contract_address)
        if not self.chain_id or not ca:
            return None
        return (str(self.chain_id).strip(), ca)

    @property
    def network_id(self) -> str | None:
        """本仓库内部的链标识(查展示名与 FOMO/GMGN 链接用)。见 resolve_network。"""
        return resolve_network(self.chain_name, self.chain_id)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """
        已推送台账的主键,口径与 copytrade_signals / transfer_in_signals 一致:
        (链标识, 归一化 CA)。

        ⚠️ **绝不返回 None**:缺 CA 的行照样是新上架、照样要推,但它同样必须被台账挡住,
           否则宽限窗口内每 5 分钟重推一次。所以缺失时逐级退到 alphaId、再退到
           「上架时刻|符号」—— 总能拼出一个在轮次之间稳定的身份。
           (实测 665 条名单里这些字段一条都不缺,这一级兜的是上游哪天变了。)
        """
        net = self.network_id or _as_text(self.chain_id) or ""
        ca = (normalize_token_address(self.contract_address)
              or self.alpha_id
              or f"{self.listing_time_ms}|{self.symbol or ''}")
        return (net, ca)


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


def resolve_network(chain_name, chain_id) -> str | None:
    """
    币安的 (chainName, chainId) → 本仓库内部链标识。

    ⚠️⚠️ **优先 chainName**。币安的 chainId 不是纯数字:实测名单里 70 个 Solana 币的
       chainId 是 `CT_501`,normalize_network 会把它原样透传成 `ct_501` ——
       既不在 NETWORK_SLUG 也不在 GMGN_SLUG,于是「🔗 FOMO · GMGN」那一行整行消失。
       而 Solana 是本仓库**完整支持**的链(两张 slug 表里都有),白丢了 70 个币的链接。
    ⚠️ 显式表是**唯一权威**:表里有就是有,没有就降级到 chainId 走 models 的通用别名表
       (数字 chainId 那几条它本来就收录着,币安哪天改个链名也不至于连 BSC 都认不出)。
       两级都没命中就原样透传成 `ct_784` 这样的原始 ID —— 那正是"我们不认识这条链"的
       如实表达(normalize_network 对未知链本来就这么做)。后果是链接那一行整行消失,
       但展示名、市值、持有人、CA **照常推**。绝不为了凑一行而拼一个必然 404 的链接。
    ⚠️ 想让一条新链出链接,改这张表 + models 的两张 slug 表,别在这里加"聪明"的推断。
    """
    key = " ".join(str(chain_name or "").split()).lower()
    net = _CHAIN_NAME_TO_NETWORK.get(key)
    if net is not None:
        return net
    return normalize_network(chain_id) or normalize_network(chain_name)


def _sane_newest(tokens: list[AlphaToken], now_ms: int | None = None) -> int | None:
    """
    水位线该前移到哪 —— 名单里**上界之内**的最大上架时间。全部越界时返回 None。

    ⚠️⚠️ 直接 max() 是个能让功能永久静默死亡的写法:一行 listingTime=99999999999999
       就把水位线顶到公元 5138 年,此后再也没有代币比它新,没有日志、没有告警。
       越界的行记 error 并**不参与水位线**;它本身仍然会照常进候选、照常推一次
       (那一行确实是名单里的新东西,该让用户看见,而不是我们替他判它是脏数据)。
    """
    limit = (int(time.time() * 1000) if now_ms is None else int(now_ms)) + FUTURE_SKEW_MS
    sane = [t.listing_time_ms for t in tokens if t.listing_time_ms <= limit]
    if len(sane) != len(tokens):
        logger.error(
            "币安 Alpha 名单有 {} 行上架时间越过合理上界(最大 {} > 上界 {})—— "
            "水位线**不跟它走**,只取上界内的最大值。跟了的话此后再没有代币比水位线新,"
            "这个功能会永久静默失效",
            len(tokens) - len(sane), max(t.listing_time_ms for t in tokens), limit,
        )
    return max(sane) if sane else None


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

        newest = _sane_newest(tokens)
        if newest is None:
            # 整份名单的上架时间全都越界 —— 这不是"没有新币",是名单不可信。
            # 水位线一动不动,下一轮重来(_sane_newest 已经记了 error)。
            return 0

        # ⚠️ 数据库连接**只在这一小段里持有**:下面的板块拉取和推送都要打网络,
        #    最坏十几秒。虽然 WAL 下读不挡写,但把一条连接横跨整段网络 IO 挂着,
        #    只会让 WAL 迟迟无法 checkpoint。开两次短连接的代价可以忽略。
        with store.get_conn() as conn:
            mark = _as_int(store.get_state(conn, STATE_KEY))
            if store.get_state(conn, LEDGER_KEY) != LEDGER_READY:
                mark = self._seed(conn, tokens, newest if mark is None else mark)
            cutoff = mark - GRACE_WINDOW_MS
            done = store.alpha_pushed_since(conn, cutoff)

        # ⚠️⚠️ 候选是「宽限窗口内 + 不在已推台账里」,**不是** `> 水位线`。
        #    单靠水位线的话,同一毫秒的代币分两轮进名单时后到的那个会永久静默丢失
        #    (实测 32 组共享 listingTime,覆盖名单的 15%,最大一组 10 个);
        #    改成 `>=` 又会让等于水位线的币每轮重推。去重交给台账主键,
        #    窗口只决定「多晚的迟到还认」。详见 GRACE_WINDOW_MS 与 store 里的表注释。
        fresh = sorted((t for t in tokens
                        if t.listing_time_ms > cutoff and t.dedupe_key not in done),
                       key=lambda t: t.listing_time_ms)
        if not fresh:
            # 没有该推的,但名单里出现了更新的上架时间(比如一个已推过的币被重新挂牌):
            # 水位线照样前移,顺手把掉出窗口的台账行清掉,否则那张表会一直长。
            if newest > mark:
                self._commit([], max(mark, newest))
            return 0

        # 板块标注是**尽力而为**:这一步失败只是少一行标注,主干照推(见 _sector_map)
        sector_map = self._sector_map()
        logger.info("币安 Alpha 新上架 {} 个(水位线 {} → {},候选窗口下界 {})",
                    len(fresh), mark, max(mark, newest), cutoff)
        sent, pushed = self._push(fresh, sector_map)
        # ⚠️ 先推后记台账:反过来的话推送崩在中间,那个币会被当成"已经推过"永久漏掉。
        # ⚠️ 水位线**照常前移**(与是否送达无关),但台账**只记真正发出去的那些** ——
        #    发失败的留在窗口里,下一轮自然重来;notifier 已经重试三次,再失败多半是
        #    TG 整体不可用,那时重来一遍才是对的,而不是把这个币判死。
        self._commit(pushed, max(mark, newest))
        return sent

    def _seed(self, conn, tokens: list[AlphaToken], base_ms: int) -> int:
        """
        台账初始化,返回本轮该用的水位线。**一条都不推。**

        两种情况走同一条路,区别只在 base_ms 取哪个:
          · 全新安装(水位线也没有)→ base = 名单里最新的上架时间。
            ⚠️⚠️ 这一步是整个功能的生死线:不播种的话 665 个币全都"没推过",
               第一轮就把 Telegram 打爆,用户当场静音,功能第一天就死。
          · 老库升级(水位线已有、台账还没有)→ base = 现有水位线。
            水位线的语义就是「这个时刻(含)之前的都已经处理过」,照它播种既不会重推
            历史上已经推过的币,也不会耽误 base 之上那些 —— 它们在同一轮里照常被推。

        ⚠️ 只播种**宽限窗口之内**的:窗口之外的币永远不会再成为候选,记了也是白记,
           下一次清理还得把它删掉。
        """
        cutoff = base_ms - GRACE_WINDOW_MS
        seed = [t for t in tokens if cutoff < t.listing_time_ms <= base_ms]
        with store.tx(conn):
            store.record_alpha_pushed(conn, [_ledger_row(t) for t in seed])
            store.set_state(conn, STATE_KEY, str(int(base_ms)))
            store.set_state(conn, LEDGER_KEY, LEDGER_READY)
        logger.info("币安 Alpha 台账初始化:水位线 {}(名单 {} 个代币,窗口内 {} 个记为已处理),"
                    "本轮一条都不推", base_ms, len(tokens), len(seed))
        return base_ms

    def _commit(self, pushed: list[AlphaToken], mark_ms: int) -> None:
        """
        记台账 + 前移水位线 + 清理过期台账,**同一个事务**。

        ⚠️ 三件事必须原子:只前移水位线而没记台账,那批币会掉进宽限窗口被重推;
           只记台账而没前移水位线,下一轮白算一遍。
        ⚠️ 写事务必须短:另一个线程的 tick 正拿 BEGIN IMMEDIATE 写事件,
           这里多待一毫秒就是它多等一毫秒 —— 所以网络 IO 全在事务之外做完了。
        """
        with store.get_conn() as conn, store.tx(conn):
            store.record_alpha_pushed(conn, [_ledger_row(t) for t in pushed])
            store.set_state(conn, STATE_KEY, str(int(mark_ms)))
            # 清理策略:掉出宽限窗口的行永远不会再成为候选,留着只会让表无限长大。
            # 与水位线同一个事务 —— 阈值就是新水位线对应的窗口下界,不会误删还在窗口里的。
            store.prune_alpha_pushed(conn, int(mark_ms) - GRACE_WINDOW_MS)

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

    def _push(self, fresh: list[AlphaToken],
              sector_map: dict[tuple[str, str], str]) -> tuple[int, list[AlphaToken]]:
        """
        返回 (真正发出去的消息条数, 该记进台账的代币)。

        ⚠️ 两个数字不是一回事:汇总那条消息只有 1 条,却覆盖了全部 fresh。
        ⚠️ 发失败的**不记台账** —— 让它留在宽限窗口里,下一轮重来。
           记了就等于把这个币判死:一次 TG 400 或网络抖动就是永久漏推。
        """
        if len(fresh) > MAX_PUSH_PER_ROUND:
            logger.error("本轮 Alpha 新上架 {} 个,超过逐条推送上限 {} —— 改推一条汇总。"
                         "这个量级不正常,多半是上游把一批老币的 listingTime 重写了",
                         len(fresh), MAX_PUSH_PER_ROUND)
            if self._notifier.send(render_alpha_batch(_render_args_batch(fresh))):
                return 1, list(fresh)     # 汇总发出去了 = 这一批都算说过了
            return 0, []

        sent = 0
        pushed: list[AlphaToken] = []
        for t in fresh:
            key = t.sector_key
            sector = sector_map.get(key) if key is not None else None
            text = render_alpha_listing(
                symbol=t.symbol,
                name=t.name,
                # ⚠️ 必须走 resolve_network(优先 chainName),绝不能拿 chainId 直接归一化:
                #    Solana 的 chainId 是 `CT_501`,那么做会让 70 个币丢掉整行链接。
                network_id=t.network_id,
                chain_name=t.chain_name,
                contract_address=t.contract_address,
                listing_time_ms=t.listing_time_ms,
                market_cap=t.market_cap,
                holders=t.holders,
                sector=sector,
            )
            if self._notifier.send(text):
                sent += 1
                pushed.append(t)
            else:
                logger.error("币安 Alpha 上新推送失败(下一轮重试) | symbol={} ca={}",
                             t.symbol, t.contract_address)
        return sent, pushed


def _ledger_row(t: AlphaToken) -> tuple[str, str, str | None, int]:
    """AlphaToken → 已推送台账的一行。store 层不认业务类型,只收元组。"""
    net, ca = t.dedupe_key
    return (net, ca, t.symbol, t.listing_time_ms)


def _render_args_batch(fresh: list[AlphaToken]) -> list[tuple[str | None, int]]:
    """汇总消息只需要 (符号, 上架时间),不把整个 dataclass 泄进 formatter(铁律:纯函数不认业务类型)"""
    return [(t.symbol, t.listing_time_ms) for t in fresh]
