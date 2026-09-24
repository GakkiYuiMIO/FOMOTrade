"""
/chips 前 10 名持有人的「投资组合」与「7 天盈亏」—— 照搬 FOMO 前端用户卡片(ProfileHoverCard)的算法。

============ 数据从哪来(2026-09-24 逆向 fomo.family 前端 bundle + 真实接口实测)============
卡片上这两个数**都不是服务端直接给的**,是前端拿两个接口现算的:

  投资组合 = AccountProvider.mc( GET /v2/users/{id}/balances )
  7 天盈亏 = portfolio.V(id, "7d").totalPnlUsd
           = [ Q(balances) + otherPnlV2 + livePerpPnl ]            ← 此刻的累计盈亏
             − GET /v2/userTokens/aggregatedSnapshotById
                   ?userId=&snapshotId=<7 天前向下取整到整点的 unix 秒>  .pnl

⚠️⚠️ 为什么不用现成的 GET /v2/users/{id}/leaderboard 里的 rank7d.pnl(一个请求就够):
   实测它与卡片**不是一个口径**:badabeepp 51,140 vs 48,015、JASON7822 -4,268 vs -5,757(差 35%)。
   用户拿去对照的是 FOMO 界面上那张卡,数对不上,读者只会认为是我们算错了。
⚠️ 服务端整点快照里也有 equity,但那是整点值、会滞后:JASON7822 快照 3,988 vs 现算 3,514(差 12%)。
   所以投资组合也只能现算。现算的 badabeepp = $180,722.60,截图里卡片是 $180,067.75
   (差 0.4%,截图与实测不是同一时刻,价格在动)。

============ 口径细节(逐条对应前端,改之前先去对 bundle)============
投资组合(mc):
  · 各条 EVM 链上的稳定币**整行剔除**(前端 chains.ma):它们由 otherEquity 统一计入,不剔就是重复计算
  · Solana 上的 USDC 是 FOMO 的"现金",按**面值**计(shiftedBalance 本身就是美元)
  · valuation 存在且 includeInEquity 为假 → 不计
  · valuation 存在且 useLivePrice 为假 → 价格按 0 计;否则用 tokenFilterResult.priceUSD
  · 最后加 otherEquity
累计盈亏(Q):
  · 现金那一行跳过
  · 未实现 = 持有量 × (现价 − 平均成本),只在 includeUnrealizedPnl 且"有价格"时计
    活跃单(activeTrade)的平均成本是**买入与转入的加权平均**(前端 util.Se),
    不是 avgEntryPrice —— 有转入的账户上两者不一样,这一处最容易抄错
  · 已实现 = activeTrade.realizedPnlUsd / userToken.currentRealizedPnlUsd,只在 includeRealizedPnl 时计
  · 再加 otherPnlV2 与 livePerpPnl

⚠️ **刻意**与前端不一致的两处(前端在这两种情况下会说假话,我们让那一段消失):
   · balances 响应里没有 balances 键:前端 mc 按 `?? []` 当空仓,卡片显示 $0 / 只剩 otherEquity;
   · 快照的 pnl 是 null:前端 `u - null = u`,把**全部历史**盈亏当成 7 天。
⚠️ 隐私:持有人标了 private 的**不去拉他的资产**(调用方在 bot 里挡)。实测 6 个币、480 个持有人里
   一个 private 都没有 —— 服务端看来本来就不把他们放进持有人榜,这一道是防御,代价为零。
⚠️ 这两个数只用于 /chips 的展示,不落库、不参与任何判定、更不接进跟单。
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from loguru import logger

# FOMO 的"现金":Solana 上的 USDC。算投资组合按面值、算盈亏时跳过(前端 chains.S)。
CASH_TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# 算投资组合时整行剔除的 (链, 地址)(前端 chains.ma)。它们由 otherEquity 统一计入。
# ⚠️ 顺带印证了 models.QUOTE_TOKENS 里 Arc 的 0x3600…0000 就是 USDC —— FOMO 自己也这么认。
_EQUITY_EXCLUDED = frozenset({
    (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),   # Base USDC
    (56, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),     # BSC USDC
    (143, "0x754704bc059f8c67012fed69bc8a327a5aafb603"),    # Monad USDC
    (1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),      # Ethereum USDC
    (5042, "0x3600000000000000000000000000000000000000"),   # Arc USDC
    (4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),   # Robinhood USDG
})

_HOUR_MS = 3600 * 1000
_WEEK_MS = 7 * 24 * _HOUR_MS

# 一次 /chips 最多并发几个请求。⚠️ 监控进程同时在打同一个服务端(实测 24 并发是拐点,
#    36 会被打回来),这里是命令路径上的附加流量,宁慢勿抢。
CARD_WORKERS = 4
# 整批的墙钟上限(秒)。超时的人那两段不显示,消息照发 —— 命令是串行处理的,
# 一条 /chips 卡太久会把后面排队的命令拖过 bot.STALE_COMMAND_SEC。
CARD_BUDGET_SEC = 15.0
# 同一个人 60 秒内再被问到就用缓存(连着查几个币时,大户往往是同一批人)。
CARD_TTL_SEC = 60.0
_CACHE_MAX = 500


def _num(v, default: float = 0.0) -> float:
    """JS 的 `Number(x ?? 0)`,但算不出来的一律当 default —— 绝不让 NaN 流进结果。"""
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _balances(resp) -> list[dict] | None:
    """balances 响应里的持仓数组;形状不对返回 None(= 这个人的两个数都算不出来)。"""
    if not isinstance(resp, dict) or not isinstance(resp.get("balances"), list):
        return None
    return [s for s in resp["balances"] if isinstance(s, dict)]


def _equity_excluded(user_token: dict) -> bool:
    """
    ⚠️ 链 id 只认**整数**:前端 chains.ma 是 `switch(networkId){case 8453: …}`,严格相等,
       字符串 "8453" 不会命中。我们多做一步 int() 就会剔掉前端不剔的那一行,数字对不上。
    """
    chain = user_token.get("networkId")
    if not isinstance(chain, int) or isinstance(chain, bool):
        return False
    return (chain, str(user_token.get("tokenAddress") or "").lower()) in _EQUITY_EXCLUDED


def portfolio_value(resp) -> float | None:
    """投资组合(美元)。前端 AccountProvider.mc 的逐条移植,口径见模块注释。拿不到返回 None。"""
    rows = _balances(resp)
    if rows is None:
        return None
    total = 0.0
    for s in rows:
        bal = _obj(s.get("balance"))
        if _equity_excluded(_obj(s.get("userToken"))):
            continue
        if bal.get("tokenAddress") == CASH_TOKEN:
            total += _num(bal.get("shiftedBalance"))
            continue
        val = s.get("valuation")
        if isinstance(val, dict) and not val.get("includeInEquity"):
            continue
        live = not (isinstance(val, dict) and not val.get("useLivePrice"))
        price = _num(_obj(s.get("tokenFilterResult")).get("priceUSD")) if live else 0.0
        total += _num(bal.get("shiftedBalance")) * price
    return total + _num(resp.get("otherEquity"))


def _avg_entry(trade: dict) -> float:
    """活跃单的平均成本 = 买入与转入按数量加权(前端 util.Se)。两边都是 0 时为 0。"""
    swap_qty, in_qty = _num(trade.get("sumSwapOpen")), _num(trade.get("sumTransferIn"))
    qty = swap_qty + in_qty
    if not qty:
        return 0.0
    return (_num(trade.get("avgEntryPrice")) * swap_qty
            + _num(trade.get("avgTransferInPrice")) * in_qty) / qty


def live_total_pnl(resp) -> float | None:
    """此刻的累计盈亏(美元)。前端 portfolio.Q + otherPnlV2 + livePerpPnl 的逐条移植。"""
    rows = _balances(resp)
    if rows is None:
        return None
    total = 0.0
    for t in rows:
        if _obj(t.get("balance")).get("tokenAddress") == CASH_TOKEN:
            continue
        val = t.get("valuation")
        has_val = isinstance(val, dict)
        inc_unrealized = bool(val.get("includeUnrealizedPnl")) if has_val else True
        inc_realized = bool(val.get("includeRealizedPnl")) if has_val else True
        frozen = has_val and not val.get("useLivePrice")
        tf = _obj(t.get("tokenFilterResult"))
        priced = frozen or bool(tf.get("priceUSD"))
        price = 0.0 if frozen else _num(tf.get("priceUSD"))
        trade = t.get("activeTrade")
        # ⚠️ 前端是 `if(t.activeTrade)`:JS 里空对象 {} 也是真 —— 走活跃单分支、贡献 0。
        #    写成 `and trade` 会把 {} 当成没有,改走 userToken 分支,数字就对不上了。
        if isinstance(trade, dict):
            unrealized = (_num(trade.get("humanTokenAmount")) * (price - _avg_entry(trade))
                          if inc_unrealized and priced else 0.0)
            realized = _num(trade.get("realizedPnlUsd")) if inc_realized else 0.0
        else:
            ut = _obj(t.get("userToken"))
            unrealized = (_num(ut.get("humanAmountRemaining"))
                          * (price - _num(ut.get("averageEntryPriceUsd")))
                          if inc_unrealized and priced else 0.0)
            realized = _num(ut.get("currentRealizedPnlUsd")) if inc_realized else 0.0
        total += unrealized + realized
    return total + _num(resp.get("otherPnlV2")) + _num(resp.get("livePerpPnl"))


def snapshot_id_7d(now_ms: float) -> int:
    """7 天前那个整点的快照 id(unix 秒)。前端 config.K:floor(t / 1h) * 1h / 1000。"""
    return int(math.floor((now_ms - _WEEK_MS) / _HOUR_MS) * _HOUR_MS // 1000)


def pnl_7d(live_pnl: float | None, snapshot) -> float | None:
    """
    7 天盈亏 = 此刻累计 − 7 天前那个整点的累计。

    ⚠️ 快照拿不到(新用户 7 天前还没有记录 / 请求失败)→ None,那一段消失。
       前端同样处理:`if(!D){m=null}`,卡片上那一格是空的,而不是把全部历史盈亏当成 7 天。
    """
    if live_pnl is None or not isinstance(snapshot, dict):
        return None
    past = snapshot.get("pnl")
    if past is None or isinstance(past, bool):
        return None
    try:
        past_f = float(past)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(past_f):
        return None
    return live_pnl - past_f


@dataclass(frozen=True)
class HolderCard:
    """一位持有人卡片上的两个数。各自独立:一个算不出来不影响另一个。"""

    portfolio_usd: float | None = None
    pnl_7d_usd: float | None = None
    # 两个请求都成功了。False = 7 天前的快照请求**失败**(不是「没有记录」):
    # 调用方把它算进「没取到」,这份结果也不进缓存。
    complete: bool = True


class HolderCardLookup:
    """
    user_id → HolderCard。**绝不抛异常**:任何失败都只让那个人的那两段不显示。

    ⚠️⚠️ 请求一律 auth_invalidate=False:这是命令路径上的附加请求,一个 401/403
       绝不能去作废监控进程共用的登录态(与 boardholders 同一条规矩)。
       fast_fail=True:不重试 —— 用户在等回执,一次没拿到就算了。
    ⚠️ 超时后还在跑的请求不去等,它们跑完会自己写进缓存,下一次 /chips 直接用上。
    ⚠️⚠️ 客户端声明了 supports_concurrency=False(playwright)就**在调用线程里串行查**,
       一个线程都不开 —— 见 _run_serial。
    """

    def __init__(self, client, *, workers: int = CARD_WORKERS, budget_sec: float = CARD_BUDGET_SEC,
                 ttl_sec: float = CARD_TTL_SEC, clock=time.monotonic, now_ms=None) -> None:
        self._client = client
        self._workers = max(1, int(workers))
        self._budget = float(budget_sec)
        self._ttl = float(ttl_sec)
        self._clock = clock
        self._now_ms = now_ms or (lambda: time.time() * 1000)
        self._cache: dict[str, tuple[float, HolderCard]] = {}
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None      # 第一次用时再建,常驻

    def supported(self) -> bool:
        """客户端有没有这两个接口。没有(离线桩)就整块不查,也不报"没取到"。"""
        return (self._client is not None and hasattr(self._client, "get_balances_raw")
                and hasattr(self._client, "get_pnl_snapshot"))

    def lookup(self, user_ids) -> dict[str, HolderCard]:
        out: dict[str, HolderCard] = {}
        if not self.supported():
            return out
        ids = list(dict.fromkeys(u for u in (user_ids or ()) if isinstance(u, str) and u))
        todo: list[str] = []
        now = self._clock()
        with self._lock:
            for uid in ids:
                hit = self._cache.get(uid)
                if hit is not None and now - hit[0] < self._ttl:
                    out[uid] = hit[1]
                else:
                    todo.append(uid)
        if not todo:
            return out
        snap_id = snapshot_id_7d(self._now_ms())
        try:
            if getattr(self._client, "supports_concurrency", True):
                self._run_pooled(todo, snap_id, out)
            else:
                self._run_serial(todo, snap_id, out)
        except Exception as e:  # noqa: BLE001 —— 例:解释器关闭时 submit 会抛 RuntimeError
            logger.warning("/chips 持有人卡片查询异常,已拿到的照常显示 | {}", e)
        return out

    def _run_pooled(self, todo: list[str], snap_id: int, out: dict) -> None:
        """
        http 实现:常驻线程池并发查。

        ⚠️ 线程池**常驻**、不是每次 /chips 新建:curl_cffi 的会话是按线程私有的
           (client._ensure_session),每次新线程就要重新 TLS 握手、重新连代理,
           白白吃掉那 15 秒预算。常驻也让总线程数封顶在 workers 个。
        ⚠️ 超时的只 cancel **还没开始**的;已经在跑的跑完会自己写进缓存,下一次直接用上。
        """
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._workers,
                                            thread_name_prefix="holdercard")
        futs = {self._pool.submit(self._fetch_one, uid, snap_id): uid for uid in todo}
        done, pending = wait(futs, timeout=self._budget)
        for f in pending:
            f.cancel()
        for f in done:
            try:
                card = f.result()
            except Exception:  # noqa: BLE001 —— _fetch_one 自己兜住了,这里是第二道
                card = None
            if card is not None:
                out[futs[f]] = card
        if pending:
            logger.info("/chips 持有人卡片:{} 人超过 {:.0f}s 没取到,本条不显示那两段",
                        len(pending), self._budget)

    def _run_serial(self, todo: list[str], snap_id: int, out: dict) -> None:
        """
        playwright 实现:**在调用线程里**逐个查,同样受墙钟预算约束。

        ⚠️⚠️ 绝不能开线程:PlaywrightFomoClient 按线程私有启动一整套浏览器,线程退出不回收
           (client.py 里记着那次事故:每 tick 泄漏 6 套 node + chromium,直到机器 swap 卡死)。
           它声明了 supports_concurrency=False,poller 与盈利榜都守这条,这里也守。
        """
        deadline = self._clock() + self._budget
        for i, uid in enumerate(todo):
            if self._clock() >= deadline:
                logger.info("/chips 持有人卡片(串行):{} 人超过 {:.0f}s 没查,本条不显示那两段",
                            len(todo) - i, self._budget)
                return
            card = self._fetch_one(uid, snap_id)
            if card is not None:
                out[uid] = card

    def _fetch_one(self, uid: str, snap_id: int) -> HolderCard | None:
        try:
            resp = self._client.get_balances_raw(uid, auth_invalidate=False, fast_fail=True)
        except Exception as e:  # noqa: BLE001
            logger.debug("持有人卡片 balances 取不到 | {} | {}", uid[:8], e)
            return None
        portfolio = portfolio_value(resp)
        live = live_total_pnl(resp)
        week = None
        complete = True
        if live is not None:
            try:
                snap = self._client.get_pnl_snapshot(uid, snap_id, auth_invalidate=False,
                                                     fast_fail=True)
            except Exception as e:  # noqa: BLE001
                # ⚠️ 请求**失败**与"这人 7 天前还没有记录"是两回事:前者要让读者知道
                #    (计进"没取到"),也不许缓存 —— 否则 60 秒内一直是缺的。
                logger.debug("持有人卡片 7 天前快照取不到 | {} | {}", uid[:8], e)
                snap, complete = None, False
            week = pnl_7d(live, snap)
        if portfolio is None and week is None:
            return None
        card = HolderCard(portfolio_usd=portfolio, pnl_7d_usd=week, complete=complete)
        if complete:
            with self._lock:
                self._cache[uid] = (self._clock(), card)
                if len(self._cache) > _CACHE_MAX:
                    for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:_CACHE_MAX // 5]:
                        del self._cache[k]
        return card
