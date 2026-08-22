"""
轮询编排 —— 主循环 tick() / 历史基线 seeding / 共识副指标 count_holders

⚠️ 本文件唯一的结构性约束(设计文档 §3.4 / §8.1,不可动摇):
   **不允许边落库边推送。**
   必须是两个独立的 for 循环 ——
     第一循环(单事务):所有事件 → judge_badge → insert_event → upsert_stats
     第二循环(事务外):所有新事件 + 补发事件 → count_consensus → count_holders → render → send
   否则同一秒买同一个币的两个人会看到 1 和 2 两个不同的共识数,功能 B 当场失去可信度。
   代价是推送延迟最坏 +一个 tick(20s),这一点设计上明确接受。

⚠️ 本文件是全项目 API 字段假设最密集的地方。§11 的每一个字段名都是从前端 bundle
   逆向推测的,**一项都没实测过**。因此这里的铁律是:
     1) 所有取值走 models.pick() 多键兜底,绝不写死单个键名
     2) 任何字段缺失都有明确降级路径,单条记录解析失败只跳过这一条
     3) 拿不准的地方标 # TODO(probe #N),N 对应设计文档 §11 的编号
"""
from __future__ import annotations

import html
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

from loguru import logger

from src import copyworker, store
from src.auth import AuthError
from src.client import (
    NotSupportedError,
    UserGoneError,
    UserSnapshot,
    sleep_or_stop,
    stop_requested,
    take_transient_errors,
)
from src.config import get_settings
from src.copytrade import Candidate, decide
from src.formatter import render, render_copy_signal
from src.models import (
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    REASON_NO_SIDE,
    FomoEvent,
    dump_raw,
    is_quote_token,
    iso_minutes_ago,
    make_event_id,
    normalize_network,
    normalize_token_address,
    now_iso,
    pick,
    to_iso,
)

# ============================================================
# 候选字段名 —— 全部来自逆向推测,probe 确认后可以收窄但保留兜底不会有坏处
# ============================================================
# TODO(probe #1): 确认 swaps 的链字段名与形态(数值 8453 还是字符串 "base")。
#                 这是唯一的硬阻断项 —— 缺失则功能 A/B 整体不可用。
# TODO(probe #10): 确认 swaps / balances / transfers 三处的取值是否一致,
#                  不一致会让同一条链裂成两个聚合键,共识计数直接算错。
_K_NETWORK = ("networkId", "chainId", "network", "chain", "networkID", "chainName")

# TODO(probe #1/#6/#12): 三个端点的代币地址字段名可能各不相同
_K_TOKEN_ADDR = ("tokenAddress", "address", "contractAddress", "mint", "ca", "tokenMint", "tokenId")
_K_TOKEN_SYMBOL = ("symbol", "tokenSymbol", "ticker", "tokenTicker", "name", "tokenName")

# TODO(probe #2): 稳定唯一 id 字段名。没有稳定 id 时才走 make_event_id 的兜底 hash
_K_NATIVE_ID = ("id", "_id", "swapId", "eventId", "uuid", "tradeId", "transferId", "thesisId")
# TODO(probe #2): txHash 是兜底 hash 的必要分量 —— 等额拆单靠它才不会被误去重
_K_TX_HASH = ("txHash", "transactionHash", "signature", "hash", "txSignature", "tx", "txId")

# TODO(probe #3): 时间字段名与**单位**(秒/毫秒/微秒/ISO)。
#                 单位判错会导致"永远拉不到新数据"的静默失效,是最危险的一类 bug,
#                 必须用一笔已知时间的真实交易人工核对一次。models.to_iso 有区间断言兜底。
_K_TIMESTAMP = (
    "timestamp", "blockTime", "createdAt", "created_at", "time", "tradedAt",
    "executedAt", "date", "blockTimestamp", "updatedAt", "postedAt",
)

# TODO(probe #4): 买卖方向字段。拿不到时退化为"从计价币位置推断"
_K_SIDE = ("side", "type", "direction", "action", "tradeType", "swapType", "kind")

# TODO(probe #4): 双侧记录(tokenIn/tokenOut)还是单侧记录 —— 决定一条 swap 产出几条事件
_K_LEG_IN = ("tokenIn", "fromToken", "inputToken", "soldToken", "tokenSold", "sellToken", "inToken")
_K_LEG_OUT = ("tokenOut", "toToken", "outputToken", "boughtToken", "tokenBought", "buyToken", "outToken")
_P_LEG_IN = ("tokenIn", "fromToken", "inputToken", "in", "from", "sell", "sold")
_P_LEG_OUT = ("tokenOut", "toToken", "outputToken", "out", "to", "buy", "bought")

_K_AMOUNT_USD = ("amountUsd", "usdValue", "valueUsd", "usdAmount", "totalUsd", "usd", "amountUSD", "volumeUsd")
# 实测:swap 记录的 USD 金额是分侧的。取标的那一侧 —— 买入看 out、卖出看 in。
# 跨链 swap 两侧数值有细微差(手续费/滑点),取错侧显示的就不是这笔的真实成交额。
_USD_OUT_FIRST = ("humanUsdAmountOut", "humanUsdAmountIn")
_USD_IN_FIRST = ("humanUsdAmountIn", "humanUsdAmountOut")
_K_TOKEN_AMOUNT = ("amount", "tokenAmount", "uiAmount", "quantity", "qty", "rawAmount", "amountRaw", "balance")
_K_PRICE_USD = ("priceUsd", "price", "tokenPrice", "usdPrice", "priceInUsd", "unitPrice")

# TODO(probe #6): balances 的持仓美元值字段;缺失时 dust 判定退化为"数量 > 0"
_K_BALANCE_USD = ("usdValue", "valueUsd", "amountUsd", "balanceUsd", "totalUsd", "usd", "positionUsd")

# ---- 展示字段(缺失则对应行整行消失,绝不本地推算) ----
_K_HOLDING_USD = ("holdingUsd", "positionUsd", "balanceUsd", "holdingValueUsd", "currentValueUsd", "remainingUsd")
# TODO(probe #9): 有没有 per-token 均价/成本价字段。本地无法推算(token 数量按设计存 TEXT 不做算术)
_K_AVG_PRICE = ("avgPrice", "averagePrice", "avgCostUsd", "costBasis", "avgBuyPrice", "averageEntryPrice")
_K_MARKET_CAP = ("marketCap", "marketCapUsd", "mcap", "fdv", "fullyDilutedValuation")
# TODO(probe #8): "交易 N 次"字段的语义(该用户对该币 / 全网、含不含卖出)。
#                 语义未确认前它只透传到 formatter 展示,**绝不参与 judge_badge**
#                 —— 若实际是"全网交易次数",启用否决票会让 🌱 永不出现、功能 A 静默全废
_K_TRADE_COUNT = ("tradeCount", "txCount", "tradesCount", "numTrades", "buyCount", "tradeNumber")
_K_PNL_USD = ("unrealizedPnl", "unrealizedPnlUsd", "pnlUsd", "pnl")
_K_PNL_PCT = ("unrealizedPnlPct", "pnlPct", "pnlPercent", "roi", "roiPct")

# TODO(probe #11): transfers 的方向字段、对手方标识,以及**是否包含 swap 产生的 transfer**
#                  (若包含,同一笔 tx 会推两条消息)
_K_TRANSFER_DIR = ("direction", "type", "side", "transferType", "flow", "action")
_K_FROM_UID = ("fromUserId", "senderId", "fromId", "sourceUserId", "from_user_id")
_K_TO_UID = ("toUserId", "receiverId", "toId", "targetUserId", "to_user_id")
_K_COUNTERPARTY = ("counterparty", "counterpartyUser", "otherUser", "peer", "fromUser", "toUser", "user")
_K_HANDLE = ("userHandle", "handle", "username", "displayName", "name")

# TODO(probe #12): thesis 是否同时返回 tokenAddress **和** networkId。
#                  缺 networkId 则观点事件无法显示共识(照常落库照常推送)
_K_THESIS_TEXT = ("thesis", "text", "content", "body", "message", "description", "note", "comment")

# 方向词典 —— 各种可能的取值都收进来,拿不准的一律落到 side_unknown
_SIDE_BUY = frozenset({"buy", "b", "bought", "in", "buys", "long", "open", "add", "swap_in", "purchase"})
_SIDE_SELL = frozenset({"sell", "s", "sold", "out", "sells", "short", "close", "reduce", "swap_out"})
_DIR_IN = frozenset({"in", "input", "receive", "received", "incoming", "deposit", "credit", "to", "inbound"})
_DIR_OUT = frozenset({"out", "output", "send", "sent", "outgoing", "withdraw", "withdrawal", "debit",
                      "from", "outbound", "transfer_out"})

# ---- 时间戳批级健康检查阈值(见 Poller._guard_ts) ----
# 样本太小不足以判定"整体失效",只 WARN 不丢批 —— 拿 1 条失败就丢整批太容易误杀
_TS_GUARD_MIN_BATCH = 5
# 一批里超过这个比例的记录时间戳解析失败 → 整批丢弃。
# 定 0.5 而不是 1.0:字段名整体变更时通常是全军覆没,但混合新旧格式的过渡期会是部分失败,
# 那种情况下游标同样已经不可信了
_TS_GUARD_DROP_RATIO = 0.5

# ---- 观点采集(见 Poller._collect_thesis) ----
# 每 tick 扫多少个代币。调用量恒定不随名单规模增长:币多时只是轮转一圈更慢。
# ⚠️ 有了活动流之后这条路**降级成兜底**了:已关注的人发观点,活动流下一轮就直接推,
#    根本不用等轮转扫到。这里只负责捞"名单里没关注的人"。
#    所以从 25 降到 12 —— 少 13 个请求/轮,正好把峰值并发压回安全线内(见 _THESIS_WORKERS)。
_THESIS_TOKENS_PER_TICK = 12
# afterTime 回看窗口(秒)。够覆盖轮转一圈的时间即可 —— 拉太久白费流量,
# 拉太短会在轮转间隙漏掉观点。精确去重由 event_id + 游标负责,这里只是压 payload。
_THESIS_LOOKBACK_SEC = 3600

# ---- 增量采集(见 _fetch_snapshots)----
# 每 tick 额外刷新多少个「本轮没有交易」的用户的 balances。
# 有交易的人一律当场刷新、不占这个名额;轮转只是用来纠正两类我们看不见的漂移
# (链上转账 + 价格跌破 dust 线)。69 人 / 8 个 ≈ 9 轮扫一圈。
_BALANCE_ROTATE_PER_TICK = 8
# 冷启动预热:每轮最多补多少个"从没拉到过 balances"的人。
# ⚠️ 不设这个上限的话,进程一启动就会把全名单的 balances 一次性打出去 ——
#    /balances 是最重的端点(单个 100~400KB、p50 1.22s),70 个并发直接把源站打到
#    **504 Gateway Timeout**(不是限流,是 origin 超时)。用户日志里满屏的
#    "FOMO 服务端错误 504 | …/balances" 就是这么来的,而且重试也是 504,
#    结果是缓存反而填不满。摊到几轮里填,每轮几个请求,源站扛得住、缓存也真的填上了。
# ⚠️ 代价:预热完成前 count_holders 返回 None,「N 人仍持有」整段消失
#    (这是既定的降级语义)。70 人 / 每轮 10 个 ≈ 7 轮,10s 一轮就是一分多钟。
_BALANCE_WARMUP_PER_TICK = 10
# 活动流一次取多少条。实测 100 条覆盖约一小时;上限就是 100(传 200 直接 400)。
_FEED_LIMIT = 100
# 隔多少轮调一次活动流。
# ⚠️ 这个端点有**独立且严格**的 Cloudflare 限流,和 /v2/users/* 完全不是一个量级:
#      /v2/users/{id}/swaps —— 27 req/s 打 40 个,0 个 429
#      /feed/tradingActivity —— 每 10s 一次跑两分钟就开始持续 429(retry-after: 0,重试也没用)
#    所以它只能当"低频补充":捞那些**发在老仓位上**的观点(刚买就发的那种由
#    _thesis_priority 覆盖)。挂了也无所谓 —— 买卖判定完全不依赖它。
#    6 轮 × 10s = 每分钟一次,实测安全。
_FEED_EVERY_N_TICKS = 6
# 名单盈亏采集的降频。15s × 20 = 5 分钟。
# ⚠️ 与 _FEED_EVERY_N_TICKS = 6 错开,避免两个低频任务撞在同一轮
_PNL_EVERY_N_TICKS = 20
# 观点优先扫描的保留名额:**刚动过**的币优先扫。
# ⚠️ 观点几乎总是发在刚买的币上 —— 实测用户 10:05 买入、10:05 发观点。
#    不留这个名额的话,只能等轮转扫到(约 400 个币 / 每轮 12 个 ≈ 6 分钟),
#    用户的实际观感就是"观点没被监控到"(实测 10:05 发、10:09 才到)。
_THESIS_PRIORITY_SLOTS = 6
# 优先名额里一个币保留多久(秒)。买入后发观点通常在几分钟内
_THESIS_PRIORITY_TTL_SEC = 1800
# 观点扫描的并发上限。这一段是纯 IO,串行 25 个币实测 4.1s,并发 8 降到 2.2s。
# ⚠️ 但它与 fomo_fetch_workers **同时在跑**(观点在后台线程,快照在主线程池),
#    真实峰值并发是两者相加。实测 12+8=20 会真的撞上限流:
#    同一毫秒 15 个请求一起 429(balances×10 + thesis×5 + trades×1)。
#    降到 4 之后峰值 16,配合 _THESIS_TOKENS_PER_TICK 从 25 减到 12,这一段仍在 1s 上下。
_THESIS_WORKERS = 4

# ---- 推送节流(见 _throttle_send)----
# Telegram 对单个 chat 的软限是 ~1 条/秒并允许小幅突发。
# 固定 sleep 太保守:一轮 6 条按 3.5s 要多等 17.5s,而这 17.5s 全部算在 tick 耗时里。
# 改成令牌桶 —— 桶里的令牌可以立刻发完,之后才退化成匀速,稳态速率仍受 send_interval 约束。
_SEND_BURST = 5

# 币龄的合理下界(2015-01-01)。比这更早的一律当脏数据 —— 以太坊主网才 2015 年上线
_TOKEN_AGE_MIN_TS = 1420070400

# runtime_state 里记录上一轮时间的键。用来识别"关机了一晚上"这类长间断
_LAST_TICK_KEY = "last_tick_at"
# 跟单日报最后报到哪一天(UTC 日期串)。跨日的第一轮据此补报前一天
_COPY_SUMMARY_KEY = "copy_summary_day"

# 隔多少轮重试一次"上游 404"的账号。15s 一轮 → 240 轮约等于 1 小时。
# ⚠️ 别调小:这条的意义是"别让误判变成永久失明",不是"尽快恢复"。
_MISSING_RECHECK_TICKS = 240


def _num_fmt(v) -> str:
    """汇总里的紧凑金额:12.3K / 1.24M。一行要塞下币名、人数、金额"""
    try:
        a = abs(float(v or 0))
    except (TypeError, ValueError):
        return "0"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{a / div:,.2f}{unit}"
    return f"{a:,.0f}"


# ============================================================
# 取值小工具
# ============================================================
def _s(v) -> str | None:
    """转字符串,空串归一成 None(下游用 `if not x` 判缺失)"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _f(v) -> float | None:
    """
    转 float,失败一律 None 而不是抛异常。

    ⚠️ API 可能返回 "$1,234.56" 这种带符号的字符串,也可能返回 NaN/Infinity ——
       NaN 写进 SQLite 不报错,但之后所有比较都是 False,是极难排查的一类脏数据。
    """
    if v is None or v is True or v is False:
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _pick_str(d, *keys) -> str | None:
    """
    多键取字符串。命中的值是 dict/list 时返回 None ——
    逆向出来的键名可能对应嵌套对象(如 token: {...}),直接当字符串用会在
    normalize_token_address 里 .strip() 崩掉,把整批数据带走。
    """
    v = pick(d, *keys)
    if isinstance(v, (dict, list)):
        return None
    return _s(v)


def _row_get(row, key, default=None):
    """sqlite3.Row 没有 .get();缺列时它抛 IndexError 而不是 KeyError"""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _drop_before_cursor(events: list[FomoEvent], cursor: str | None) -> list[FomoEvent]:
    """
    冷启动保护:游标之前的历史事件永不进入推送(设计文档 §7 —— 这是唯一的抑制机制)。

    ⚠️ 刻意**不推进游标**。推进会与 A-8「后拿到更早的买入」直接冲突:
       一旦把游标推到本批最大 event_ts,乱序到达的更早事件就被永久丢弃,
       first_buy_at 再也修不回来。重复拉取的成本由 event_id 主键 + INSERT OR IGNORE 兜住,
       几十条记录的重复归一化是微秒级,不值得为它引入一个会丢数据的优化。

    ⚠️ 这里用字符串比较日期:now_iso() 与 to_iso() 都输出
       "YYYY-MM-DDTHH:MM:SS+00:00" 同一形态的 UTC ISO,字典序即时序。
       任何一方改了 timespec 或时区偏移,这个比较会静默失效 —— 改动前先确认两处一致。
    """
    if not cursor:
        return events
    return [e for e in events if e.event_ts > cursor]


# ============================================================
# balances 解析(count_holders 与 seeding 共用)
# ============================================================
def _balance_key(b: dict) -> tuple[str | None, str | None]:
    """
    从一条 balances 记录里取出 (network_id, token_address) 聚合键。

    ⚠️ 实测(2026-08-11)真实结构顶层只有四个键,代币标识**一个都不在顶层**:
          {"balance": {"tokenAddress": ..., "tokenId": "<addr>:<networkId>"},
           "tokenFilterResult": {"token": {"networkId": ..., "symbol": ...}},
           "userToken": {...}, "activeTrade": {...}}
       原来只按扁平/｛token:…｝两种猜测取键,对真实结构恒返回 (None, None) ——
       后果是 count_holders 一个都数不到、seeding 的持仓回填整个失效
       (而后者正是堵"回填窗口外老仓位被误标 🌱"的那道防线)。
       所以下面**先按实测路径取**,取不到再退回原来的通用猜测。
    """
    if not isinstance(b, dict):
        return None, None

    # ---- 实测路径 ----
    bal = b.get("balance") if isinstance(b.get("balance"), dict) else {}
    tfr = b.get("tokenFilterResult") if isinstance(b.get("tokenFilterResult"), dict) else {}
    tok = tfr.get("token") if isinstance(tfr.get("token"), dict) else {}
    ut = b.get("userToken") if isinstance(b.get("userToken"), dict) else {}

    ca = normalize_token_address(
        _pick_str(bal, "tokenAddress") or _pick_str(tok, "address") or _pick_str(ut, "tokenAddress")
    )
    net = normalize_network(
        _pick_str(tok, "networkId") or _pick_str(ut, "networkId") or _pick_str(tfr, "networkId")
    )
    # tokenId 是 "<address>:<networkId>" 的复合键,前两者都缺时用它兜底
    if not (ca and net):
        tid = _pick_str(bal, "tokenId") or _pick_str(tok, "id")
        if tid and ":" in tid:
            a, _, n = tid.rpartition(":")
            ca = ca or normalize_token_address(a)
            net = net or normalize_network(n)
    if ca and net:
        return net, ca

    # ---- 兜底:原来的通用猜测(结构再变时还有一线机会) ----
    nested = b.get("token") if isinstance(b.get("token"), dict) else None
    if nested is None and isinstance(b.get("tokenInfo"), dict):
        nested = b["tokenInfo"]
    src = nested or b
    net = normalize_network(_pick_str(src, *_K_NETWORK) or _pick_str(b, *_K_NETWORK))
    ca = normalize_token_address(_pick_str(src, *_K_TOKEN_ADDR) or _pick_str(b, *_K_TOKEN_ADDR))
    return net, ca


def _token_created_at(tok: dict) -> int | None:
    """
    代币合约的创建时间(unix 秒)。用来算「币龄」。

    ⚠️ 只认合理区间 [2015-01-01, 现在+1天]:这个值直接决定消息里那行「币龄」,
       而一个 0 或者毫秒级的时间戳会渲染成"币龄 56Y"这种一眼假的东西,
       比不显示糟得多。区间外一律当没有(整行消失,§10.4 铁律 2)。
    """
    v = _i(tok.get("createdAt"))
    if v is None:
        return None
    return v if _TOKEN_AGE_MIN_TS <= v <= time.time() + 86400 else None


def _fill(d: dict, key: str, value) -> None:
    """
    只在**当前值为空**时写入。

    ⚠️ 不能用 dict.setdefault:它只判断"键在不在",不判断值是不是 None。
       trades 分支先跑,若某个币的 tokenMetadata 是空的(真实响应里不罕见),
       就会把 symbol / price_usd / network_raw 写成 None 并占住这个键 ——
       之后 balances 明明有这些值也补不进去。
       后果:消息里没有 $SYMBOL、市值行消失,而且 network_raw=None 会让
       这个币被 _collect_thesis 整个跳过(观点永远抓不到)。
    """
    if d.get(key) is None and value is not None:
        d[key] = value


def _balance_usd(b: dict) -> float | None:
    """
    这条持仓值多少美元。

    ⚠️ 实测响应里**没有现成的 usdValue 字段**,只能自己乘:
         humanAmountRemaining(userToken) × priceUSD(tokenFilterResult)
       这是 dust 判定与「📦 持仓」行的唯一来源。
       注意这属于"把两个 API 字段相乘",不是"本地推算业务量"——
       设计里禁止的是拿 buy_count 反推交易次数那种,两者性质不同。
    """
    if not isinstance(b, dict):
        return None
    direct = _f(pick(b, *_K_BALANCE_USD))
    if direct is not None:
        return direct
    bal = b.get("balance") if isinstance(b.get("balance"), dict) else {}
    ut = b.get("userToken") if isinstance(b.get("userToken"), dict) else {}
    tfr = b.get("tokenFilterResult") if isinstance(b.get("tokenFilterResult"), dict) else {}
    qty = _f(ut.get("humanAmountRemaining")) or _f(bal.get("shiftedBalance"))
    price = _f(tfr.get("priceUSD"))
    if qty is not None and price is not None:
        return qty * price
    return None


def _balance_is_held(b: dict) -> bool:
    """这条持仓是否算"仍持有"(dust 以下不算)。算不出金额时退化为"数量 > 0"。"""
    usd = _balance_usd(b)
    if usd is not None:
        return usd >= store.HOLDING_MIN_USD
    bal = b.get("balance") if isinstance(b.get("balance"), dict) else {}
    nested = b.get("token") if isinstance(b.get("token"), dict) else {}
    amt = (_f(bal.get("shiftedBalance")) or _f(pick(b, *_K_TOKEN_AMOUNT))
           or _f(pick(nested, *_K_TOKEN_AMOUNT)))
    return bool(amt and amt > 0)


def _holds(balances: list[dict] | None, key: tuple[str, str]) -> bool:
    """名单内某人当前是否持有该币。同一个币出现多行时(多钱包)累加美元值再比阈值"""
    if not balances:
        return False
    total_usd = 0.0
    matched = False
    for b in balances:
        if _balance_key(b) != key:
            continue
        matched = True
        usd = _f(pick(b, *_K_BALANCE_USD))
        if usd is None:
            # 没有美元值就退化成"数量 > 0",单行命中即算持有
            if _balance_is_held(b):
                return True
        else:
            total_usd += usd
    return matched and total_usd >= store.HOLDING_MIN_USD


def count_holders(snapshots: dict, ready_ids: list[str], ev: FomoEvent) -> int | None:
    """
    功能 B 副指标:本 tick 内存 balances 里仍持有该币的人数。**零持久化**。

    ⚠️ 只要有任一 active & ready 用户本 tick 的 balances 缺失,直接返回 None(整段消失)。
       部分覆盖会让同一个币的数字在 3 和 1 之间来回跳,比不显示糟得多 ——
       "能抖的数字不能当主指标",连副指标也不能抖。

    snapshots: {user_id: UserSnapshot | None},balances 为 None 表示该项拉取失败
               (区别于空列表 = 拉到了但没有持仓)。
    """
    key = ev.token_key
    if key is None:
        return None

    for uid in ready_ids:
        snap = snapshots.get(uid)
        if snap is None or getattr(snap, "balances", None) is None:
            return None

    n = sum(1 for uid in ready_ids if _holds(snapshots[uid].balances, key))

    # 索引延迟保护:balances 快照通常晚于 swaps 索引,买入者本人可能还没出现在自己的持仓里。
    # 一条"他刚买入"的消息配"0 人仍持有"会直接毁掉这个数字的可信度。
    if (
        ev.event_type == EVENT_BUY
        and ev.user_id in ready_ids
        and not _holds(snapshots[ev.user_id].balances, key)
    ):
        n += 1
    return n


# ============================================================
# 补发:DB 行 → FomoEvent
# ============================================================
def _event_from_row(row) -> FomoEvent | None:
    """
    把 fomo_events 的一行还原成 FomoEvent,供"落库成功但没发出去"的补发路径使用。

    ⚠️ 展示字段(持仓/市值/均价)不落库,补发时必然缺失 —— 按"缺失即整行消失"降级,
       这是可接受的:补发本来就是异常路径。
    ⚠️ 但 thesis 正文与转账对手方是**消息的主体内容**,缺了会发出一条空壳消息,
       所以从 raw_json 里重新解析这两项(raw_json 全量留存正是为了这类回填)。
    """
    try:
        ev = FomoEvent(
            event_id=row["event_id"],
            event_type=row["event_type"],
            user_id=row["user_id"],
            event_ts=row["event_ts"],
            raw_json=row["raw_json"],
            handle=_row_get(row, "handle"),
            user_handle=_row_get(row, "user_handle"),
            network_id=_row_get(row, "network_id"),
            token_address=_row_get(row, "token_address"),
            token_symbol=_row_get(row, "token_symbol"),
            amount_usd=_row_get(row, "amount_usd"),
            token_amount=_row_get(row, "token_amount"),
            price_usd=_row_get(row, "price_usd"),
            tx_hash=_row_get(row, "tx_hash"),
            ingested_at=_row_get(row, "ingested_at", now_iso()),
            badge=_row_get(row, "badge"),
            badge_reason=_row_get(row, "badge_reason"),
            # 币龄是恒定值(不像市值会过期),落了库补发时就能照常显示
            token_created_at=_row_get(row, "token_created_at"),
            # side_unknown 不落库,但 badge_reason 已经把它记下来了 —— 补发时必须还原,
            # 否则一条方向不明的事件会被 formatter 当成正常买入渲染
            side_unknown=_row_get(row, "badge_reason") == REASON_NO_SIDE,
        )
    except (KeyError, IndexError) as e:
        logger.warning("补发行还原失败,跳过: {}", e)
        return None

    try:
        raw = json.loads(ev.raw_json)
        if isinstance(raw, dict):
            if ev.event_type == EVENT_THESIS:
                ev.thesis_text = _pick_str(raw, *_K_THESIS_TEXT)
            elif ev.event_type in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT):
                ev.counterparty_handle = _counterparty_handle(raw)
    except Exception as e:  # noqa: BLE001
        logger.debug("补发行 raw_json 回填失败(不影响主体): {}", e)
    return ev


def _counterparty_handle(raw: dict) -> str | None:
    """转账对手方展示名。可能是扁平字段,也可能挂在嵌套的 user 对象里"""
    for k in _K_COUNTERPARTY:
        v = raw.get(k)
        if isinstance(v, dict):
            h = _pick_str(v, *_K_HANDLE)
            if h:
                return h
    return _pick_str(raw, "counterpartyHandle", "fromHandle", "toHandle", "peerHandle")


# ============================================================
# Poller
# ============================================================
class Poller:
    """
    轮询编排器。client / notifier 由外部注入(cli.py 组装),便于 --dry-run 与单测替换。
    """

    def __init__(self, client, notifier) -> None:
        self.client = client
        self.notifier = notifier
        self.settings = get_settings()
        # 名单内转账标注(B-9)用:每 tick 刷新一次,避免 normalize_* 里再开 DB 连接
        self._watched_ids: set[str] = set()
        self._watched_handles: set[str] = set()
        # 每 tick 从 balances 重建(见 _build_token_index)
        self._token_meta: dict[tuple, dict] = {}    # (net, ca)        → symbol/市值/现价
        self._positions: dict[tuple, dict] = {}     # (uid, net, ca)   → 持仓/均价/盈亏
        self._thesis_rr = 0                         # 观点轮转扫描的游标(见 _collect_thesis)
        # 本轮是不是"停机后的第一轮"。见 tick() 里的说明
        self._catchup_since: str | None = None
        # ---- 增量采集状态(见 _fetch_snapshots)----
        self._swap_seen: dict[str, set[str]] = {}   # uid → 上一轮见过的 swap id 集合
        self._bal_cache: dict[str, list[dict]] = {}  # uid → 最近一次成功拉到的 balances
        # 上一轮有新交易的人 → 下一轮必须重拉 balances(见 _fetch_snapshots 里的说明)
        self._bal_dirty: set[str] = set()
        self._bal_rr = 0                            # balances 轮转刷新的游标
        self._warm_rr = 0                           # 冷启动预热的轮转游标
        self._feed_seen: set[str] = set()           # 上次活动流里见过的条目 id
        self._feed_thesis: list[dict] = []          # 活动流里捡到的观点,交给 _collect_thesis
        self._tick_no = 0                           # 轮次计数,用来给活动流降频(见 _poll_feed)
        # 刚动过的币 → 观点优先扫描。{(net, ca): 过期 monotonic 时刻}
        self._thesis_priority: dict[tuple, float] = {}
        # 本轮真的去拉过 swaps 的人。⚠️ 用来区分"拉取失败"和"本轮压根没排到他",
        #    否则 _collect_events 会对没排到的五十几个人每轮刷一条 WARNING
        self._swaps_attempted: set[str] = set()
        # 观点网络采集的后台线程。跨 tick 复用,懒建(见 _start_thesis)
        self._thesis_pool: ThreadPoolExecutor | None = None
        # 无人值守买入的执行队列。懒建 —— 没开自动的人不该多一条线程
        self._copy_worker: copyworker.CopyWorker | None = None
        # ---- 推送令牌桶(见 _throttle_send)。跨 tick 保持,不是每轮重置 ----
        self._send_tokens = float(_SEND_BURST)
        self._send_last = time.monotonic()

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------
    def tick(self, dry_run: bool = False) -> int:
        """
        一轮完整轮询,返回本 tick 新落库的事件数。

        ⚠️ 结构性约束:落库(第一循环)与推送(第二循环)必须完全分离。
           详见文件头注释 —— 这是整个功能 B 可信度的地基,任何"顺手在循环里发一下"
           的改动都会当场毁掉它。
        """
        # --- 1) 每 tick 最多为一个新用户建立历史基线 ---
        self.seed_next_pending_user(dry_run=dry_run)

        with store.get_conn() as conn:
            users = store.list_active_users(conn)
            if not users:
                logger.debug("监控名单为空,本 tick 跳过")
                return 0
            self._refresh_watched_index(users)
            # ⚠️ 采集名单要排除掉上游已经 404 的账号,但**共识计数与展示仍用完整名单**:
            #    他们历史上的买入是真实发生过的事,不能因为账号后来没了就抹掉。
            #    没有这道过滤时实测:2 个被删的账号被每 15 秒重试一次、连续 10 天。
            users = store.fetchable_users(conn)
            if not users:
                logger.warning("名单里的人上游全部 404,本 tick 无可采集")
                return 0
            # 识别"中间停过机"。必须在采集之前判,采集之后 last_tick_at 就被刷新了
            self._catchup_since = self._detect_gap(conn)
            with suppress(Exception):
                self._recheck_missing(conn)

            # --- 2) 采集 ---
            # ⚠️ 观点的轮转扫描与用户快照采集**互不依赖**,串行跑等于白等一个屏障:
            #    实测两段各自的尾延迟都有 2~4s,叠起来就是一轮里最贵的两截。
            #    这里让观点先在后台跑起来,与快照采集重叠。
            #    代价:批次是拿**上一轮**的 _token_meta 选的 —— 它只决定"这轮扫哪些币",
            #    不参与任何事件的判定,晚一轮完全无害(首轮 meta 为空则本轮不扫,
            #    活动流那条路照常工作)。
            batch = self._pick_thesis_batch()
            take_thesis = self._start_thesis(batch)
            snapshots = self._fetch_snapshots(users)
            # 先建代币/持仓索引:归一化时要用它补 symbol、市值、持仓、均价
            self._build_token_index(snapshots)
            # 行情落库,供 /hot 算倍数(失败不影响推送)
            self._save_token_snapshots(conn)

            # --- 3) 归一化 + 游标过滤 ---
            events = self._collect_events(conn, users, snapshots)
            # 观点单独走一条路:活动流直取 + 按代币轮转兜底(见 _collect_thesis)
            try:
                events.extend(self._collect_thesis(conn, users, batch, take_thesis()))
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                # 观点挂了不能影响买卖推送 —— 后者才是主链路
                logger.error("观点采集整体失败(买卖不受影响): {}", e)

            # ⚠️ 必须按事件时间升序。乱序时同一个币的第二笔买入可能先被判定,
            #    真正的第一笔反而拿到 ADD —— 徽章落库即冻结,永不重算,打错就是永久的。
            events.sort(key=lambda e: e.event_ts)

            # --- 4) 第一循环:单事务内全部落库 ---
            new_events = self._persist(conn, events)

            # --- 5) 第二循环:事务外统一渲染 + 串行发送 ---
            self._dispatch(conn, snapshots, new_events, dry_run=dry_run)

            # 活动流看不见的人刚碰过的币 → 下一轮优先扫它的观点(观点几乎总是发在刚买的币上)
            self._note_thesis_priority(new_events)

            # --- 6) 跟单信号。⚠️ 放在最后、整段包 try:它是附加功能,
            #        任何问题都不该影响主推送(推送才是这个项目的本体)
            try:
                self._check_copytrade(conn, new_events, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                logger.error("跟单信号判定失败(不影响推送): {}", e)
            try:
                self._maybe_copy_summary(conn, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                logger.warning("跟单日报失败(不影响推送): {}", e)

            try:
                self._maybe_poll_pnl(conn)
            except Exception as e:  # noqa: BLE001
                logger.warning("名单盈亏采集失败(不影响推送): {}", e)

            # 记录本轮时间,供下次识别间断。放在最后:中途异常时不刷新,
            # 下一轮仍会认出这段间断,不会把积压当成正常增量逐条推出去
            if not dry_run:
                try:
                    with store.tx(conn):
                        store.set_state(conn, _LAST_TICK_KEY, now_iso())
                except Exception as e:  # noqa: BLE001
                    logger.warning("记录 last_tick_at 失败(不影响推送): {}", e)

        return len(new_events)

    def _detect_gap(self, conn) -> str | None:
        """
        距上一轮太久 → 返回上一轮的时间(进入"停机汇总"模式),否则 None。

        场景:用户拿自己的电脑跑,晚上关机。第二天开机时积压着一整夜的事件 ——
        实测 68 人名单约 857 条/天,停 8 小时就是近 300 条。
        以 3.5s/条的节流要发一个多小时,而且那时候的信息早就过期了。

        处置:这些事件**照常入库**(共识计数、首次建仓判定、/hot 榜单都读库,
        数据完整性不受影响),只是不逐条推送,改发一条汇总。
        """
        threshold = self.settings.fomo_catchup_threshold_min
        if threshold <= 0:
            return None
        last = store.get_state(conn, _LAST_TICK_KEY)
        if not last:
            # 首次运行:没有上一轮可比。此时游标也是新设的,本来就不会有积压
            return None
        try:
            gap_min = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() / 60
        except ValueError:
            return None
        if gap_min < threshold:
            return None
        logger.info("检测到中断 {:.0f} 分钟,本轮走汇总模式(事件照常入库,不逐条推送)", gap_min)
        return last

    def _refresh_watched_index(self, users) -> None:
        self._watched_ids = {u["user_id"] for u in users}
        self._watched_handles = {h for h in (_row_get(u, "handle") for u in users) if h}

    def _fetch_snapshots(self, users) -> dict:
        """
        增量采集 —— 把单轮从 ~35s 压到个位数秒的关键。

        实测单端点延迟(70 人名单,含代理):
            swaps p50 0.27s · balances p50 1.22s · trades p50 0.72s
        全员三个端点都拉 = 70 × 2.2 ≈ 154 请求秒,6 线程要 31s,已经超过轮询间隔。
        省的是后两个,**不是 swaps**:

          - swaps:全员每轮拉。实测 70 人 12 线程只要 1.2~3.6s,
            而 /v2/users/* 的限流很宽松(27 req/s 打 40 个、0 个 429)。
            ⚠️ 曾经用活动流当"谁动了"的探针来省掉这一步,结果得不偿失:
               只省 1~2s,却换来 /feed/tradingActivity 的 Cloudflare 限流(见 _poll_feed)、
               "活动流照不到的人"的覆盖盲区,以及一整套 feed_hot/兜底轮转的复杂度。
               现在退回"全员每轮拉":最简单、无盲区、每个人的延迟都是一个轮询间隔。
          - trades:只给**本轮有新事件的人**拉。它只用于渲染「已实现盈亏 / 剩余持仓 / 均价」,
            这几行只出现在那个人的那条消息里,别人拉回来完全用不上。
          - balances:有两个用途 —— count_holders 要全员覆盖、_token_meta 要市值。
            而持仓只在这人**交易**时才变(转账 FOMO 没有可用端点,本来就看不见),
            所以「有交易的当场刷 + 上轮交易过的补刷 + 其余人轮转慢刷 + 剩下的读缓存」,
            既保住全员覆盖,又把请求量从 70 降到十几个。

        ⚠️ 返回值形态与改造前**完全一致**:每个人的 balances 都有值(本轮没拉的读缓存),
           所以 count_holders / _build_token_index 一行都不用改。缓存只在本函数内部生效,
           刻意不外泄成新的跨模块契约。
        ⚠️ 单个用户失败仍置 None —— 一个人的网络抖动不能让整个名单的推送停摆。
        """
        uids = [u["user_id"] for u in users]
        handles = {u["user_id"]: _row_get(u, "handle") for u in users}
        # 低频捞一批观点(只影响观点补充,买卖判定不依赖它)。
        # ⚠️ 计数器在**调用之后**才加:否则第一轮算出的是 1 % N != 0,
        #    活动流在启动那一轮反而被跳过,而那正是最该捞一次的时候。
        self._poll_feed(uids)
        self._tick_no += 1
        # ⚠️ playwright 实现必须串行:它按线程私有创建整套浏览器,而线程池每 tick 建新线程,
        #    线程退出时浏览器不回收 —— 实测每 tick 泄漏 6 套 chromium,进程数单调递增到卡死。
        if not getattr(self.client, "supports_concurrency", True):
            workers = 1
        else:
            workers = max(1, self.settings.fomo_fetch_workers)

        auth_err: list[AuthError] = []
        failed: list[str] = []
        gone: list[str] = []

        def call(kind: str, uid: str):
            # 停机中就别再发了:线程池里可能还排着几十个请求,一个个跑完
            # 会让 Ctrl+C 等上十几秒(见 client.request_stop 的说明)
            if stop_requested():
                return None
            try:
                return getattr(self.client, f"get_{kind}")(uid)
            except AuthError as e:
                auth_err.append(e)
                return None
            except UserGoneError as e:
                # ⚠️ 这不是抖动,是"这个人没了"。不进 failed(那会汇总成"上游抖动"),
                #    单独收集起来,由 tick 末尾标记 + 只告警一次。
                gone.append(uid)
                logger.debug("{} 上游 404 | user={} handle={} | {}",
                             kind, uid, handles.get(uid), e)
                return None
            except Exception as e:  # noqa: BLE001
                # ⚠️ 逐条 WARNING 会在上游抖动时刷屏几十行,把真正要紧的 ERROR 淹掉。
                #    这些失败**已经被处理**(balances 有缓存兜底、swaps 下一轮重来),
                #    汇总成一行就够(见本函数末尾)。
                failed.append(kind)
                logger.debug("{} 拉取失败(该项降级) | user={} handle={} | {}",
                             kind, uid, handles.get(uid), e)
                return None

        def run(jobs: list[tuple[str, str]]) -> list:
            """
            ⚠️ 调度单元是**一个请求**而不是"一个用户"。按用户调度时同一个人的
               swaps/balances/trades 只能串行,慢的那个端点会把整条线程占住 ——
               实测 69 人 × 3 端点:按用户 12 线程 17.8s,按请求 12 线程 15.1s、24 线程 10.8s。
            """
            if not jobs:
                return []
            if workers == 1 or len(jobs) == 1:
                return [call(*j) for j in jobs]
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs)),
                                    thread_name_prefix="fomo-fetch") as ex:
                return list(ex.map(lambda j: call(*j), jobs))

        # ⚠️ cold 必须在 _hot_users 之前取:那一步会把 _swap_seen 填满。
        #    冷启动没有差集基准,_hot_users 会把**所有人**判成 hot —— 那不是"所有人刚交易过"。
        cold = not self._swap_seen
        # ---- 全员 swaps。没有例外、没有轮转、没有盲区 ----
        need_swaps = uids
        self._swaps_attempted = set(need_swaps)

        # ---- 一个池子里同时打两类请求 ----
        # ⚠️ 关键在于**不要为了等 swaps 的判定结果而多设一道屏障**:每道屏障都要付一次
        #    尾延迟(实测单个请求最慢能到 3~5s)。所以"上一轮就知道该刷的人"直接跟 swaps
        #    一起发出去,只有本轮新发现有交易的才走后面那个小补漏池(通常 0~3 个)。
        # ⚠️ _bal_dirty 不能省:有新交易的人,他的 balances 与 swaps 是**同一个池子里并发发出**的,
        #    拿回来的必然是服务端还没索引到这笔交易的旧持仓。这份最不准的快照一旦进缓存,
        #    下一轮他既不 hot 也多半轮不到轮转,错误状态就被冻结整整一个轮转周期(约 9 轮)。
        #    表现:名单集体抢同一个新币的那一两分钟里,「N 人仍持有」系统性偏小 ——
        #    而那正是这个数字最该准的时刻。改造前每轮全员重拉,下一轮就自愈了。
        #    count_holders 里给买入方向写的那个 +1 补偿,说明这个索引延迟是本项目已确认的事实。
        # 冷启动预热要限量,否则一启动就把全名单的 balances 一次打出去,源站直接 504
        # (见 _BALANCE_WARMUP_PER_TICK 的说明)
        # ⚠️ 轮转取,不是每轮都取头几个:上游抖动时队头那几个会一直失败,
        #    固定取头部等于让后面的人永远排不上,缓存永远填不满。
        pending = [u for u in uids if u not in self._bal_cache]
        if pending:
            k = min(_BALANCE_WARMUP_PER_TICK, len(pending))
            start = self._warm_rr % len(pending)
            self._warm_rr = start + k
            warmup = [pending[(start + i) % len(pending)] for i in range(k)]
        else:
            warmup = []
        need_bal = sorted(self._bal_dirty
                          | self._rotate_balances(uids, self._bal_dirty)
                          | set(warmup))
        need_trd: list[str] = []
        res = run([("swaps", u) for u in need_swaps]
                  + [("balances", u) for u in need_bal]
                  + [("trades", u) for u in need_trd])
        i, j = len(need_swaps), len(need_swaps) + len(need_bal)
        swaps = dict(zip(need_swaps, res[:i], strict=True))
        bal_now = dict(zip(need_bal, res[i:j], strict=True))
        trades = dict(zip(need_trd, res[j:], strict=True))
        if auth_err:
            # 登录态挂了后面全是白打,直接上抛
            raise auth_err[0]

        # ---- 补漏池:本轮新发现有交易的人,补他们的 balances / trades ----
        # ⚠️ 冷启动那轮的 hot 是"全员"(没有差集基准),不代表全员刚交易过。
        #    照它去补 trades 会在**启动瞬间**多打 70 个请求 —— 冷启动本来就是单轮请求量的峰值
        #    (全员 swaps + 全员 balances),再叠一份 trades 就是 210 个,实测足以打爆限流窗口。
        hot = self._hot_users(swaps)
        acted: set[str] = set() if cold else hot
        extra_bal = sorted(acted - set(need_bal))
        extra_trd = sorted(acted - set(need_trd))
        if extra_bal or extra_trd:
            res2 = run([("balances", u) for u in extra_bal] + [("trades", u) for u in extra_trd])
            bal_now.update(zip(extra_bal, res2[:len(extra_bal)], strict=True))
            trades.update(zip(extra_trd, res2[len(extra_bal):], strict=True))
        # 本轮有新交易的人,他们的 balances 必然还没反映这笔交易(见上面 need_bal 处的说明),
        # 挂上脏标记让下一轮无条件重拉一次 —— 稳态下每轮也就多几个请求。
        self._bal_dirty = acted
        logger.debug("采集 | 名单 {} 人 · swaps {} 人 · 有新动作 {} 人 · balances {} 人 · 补漏 {} 人",
                     len(uids), len(need_swaps), len(hot),
                     len(need_bal) + len(extra_bal), len(extra_bal))
        if gone:
            self._handle_gone_users(sorted(set(gone)), handles)
        self._log_transient(failed, len(uids) - len(self._bal_cache))

        # ---- 组装。本轮没拉的读缓存,保证 count_holders 拿到全员覆盖 ----
        snapshots: dict = {}
        for uid in uids:
            b = bal_now.get(uid)
            if b is not None:
                self._bal_cache[uid] = b
            else:
                # ⚠️ 只有 None 才回落缓存。[] 是"拉到了、确实没有持仓",是有效值,
                #    用 `or` 写会把它当假值吞掉,那人就永远停在旧持仓上。
                b = self._bal_cache.get(uid)
            snap = UserSnapshot(user_id=uid)
            snap.swaps = swaps.get(uid)
            snap.balances = b
            snap.trades = trades.get(uid)
            snapshots[uid] = snap

        # 已经移出名单的人不再占内存(69 人的 balances 原始体积约 8MB)
        for dropped in set(self._bal_cache) - set(uids):
            self._bal_cache.pop(dropped, None)
            self._swap_seen.pop(dropped, None)

        if auth_err:
            # ⚠️ 必须上抛,绝不能吞掉。登录态失效是"整个管道都废了",
            #    不是"某个用户拉取失败" —— 吞掉的后果是程序每轮空转一次只刷 ERROR 日志,
            #    而 §3.5 要求的那条「🔐 登录态失效」TG 告警永远发不出去,
            #    用户会一直以为监控还活着。
            raise auth_err[0]
        return snapshots

    def _handle_gone_users(self, uids: list[str], handles: dict) -> None:
        """
        上游 404 的账号:落标记、只告警一次、从此不再拉他。

        ⚠️ **不自动移出名单**。账号可能只是改了名或临时不可见,而 /del 会把
           基线一起删掉 —— 那是不可逆的。这里只停止拉取并告诉用户,删不删由他决定。
        ⚠️ 告警必须只发一次:这个状态会持续存在(实测持续了 10 天),
           每轮发一条的话,TG 会被刷爆,而刷爆等同于没有告警。
        """
        fresh = []
        with store.get_conn() as conn:
            for uid in uids:
                if store.mark_user_missing(conn, uid):
                    fresh.append(uid)
        if not fresh:
            return
        names = ", ".join(f"@{handles.get(u) or u[:8]}" for u in fresh)
        logger.warning("上游 404,已停止拉取(不再重试) | {}", names)
        with suppress(Exception):
            self.notifier.send(
                f"\U0001F47B <b>{len(fresh)} 个账号在 FOMO 上已不存在</b>\n"
                f"{html.escape(names)}\n"
                "已停止拉取他们的数据(之前每 15 秒重试一次,一直失败)。\n"
                "他们的历史记录仍计入共识;确认不要了就 <code>/del &lt;handle&gt;</code>。"
            )

    def _recheck_missing(self, conn) -> None:
        """
        隔一阵子重试一次被标记的账号 —— 改名/临时不可见的情况会自己恢复。

        ⚠️ 频率要低。这一条的全部意义是"别让一次误判变成永久失明",
           不是"尽快恢复" —— 恢复晚一小时没有任何代价。
        """
        if self._tick_no % _MISSING_RECHECK_TICKS:
            return
        for row in store.missing_users(conn):
            uid = row["user_id"]
            try:
                self.client.get_balances(uid)
            except Exception:  # noqa: BLE001, S112
                continue
            if store.clear_user_missing(conn, uid):
                logger.info("账号又能拉到了,恢复采集 | @{}", row["handle"])
                with suppress(Exception):
                    self.notifier.send(
                        f"✅ <b>@{html.escape(row['handle'] or uid[:8])} 又能拉到了</b>,已恢复采集")

    def _log_transient(self, failed: list[str], not_warm: int) -> None:
        """
        把本轮的上游抖动汇总成**一行**。

        ⚠️ 逐条打 WARNING 的话,上游一抖就是几十行刷屏(用户实测启动时满屏
           "FOMO 服务端错误 504 | …/balances"),真正要紧的 ERROR 会被淹掉。
           而这些失败本身都已经被处理:balances 有缓存兜底、swaps 下一轮重来。
           所以逐条降到 DEBUG,这里只报"多少次、什么类型、还差几个人没预热"。
        """
        errs = take_transient_errors()
        if not errs and not failed:
            return
        parts = [f"{k}×{v}" for k, v in sorted(errs.items())]
        if failed:
            per_kind: dict[str, int] = {}
            for k in failed:
                per_kind[k] = per_kind.get(k, 0) + 1
            parts.append("最终降级 " + "/".join(f"{k}×{v}" for k, v in sorted(per_kind.items())))
        tail = f" · 还有 {not_warm} 人的持仓未预热" if not_warm > 0 else ""
        logger.warning("上游抖动 | {}{}(已重试/降级,不影响推送)", " · ".join(parts), tail)

    def _hot_users(self, swaps: dict) -> set[str]:
        """
        本轮出现了「上一轮没见过的 swap id」的用户 —— 只有这些人需要 trades 和新 balances。

        ⚠️ 比的是**整页 id 集合**,不是"最新一条变没变"。服务端偶尔会把一笔更早的 swap
           补进列表(跨链单两条腿到达时间不同),只看头一条会漏掉它,那条消息的
           「剩余持仓 / 已实现盈亏」就会整行消失。
        ⚠️ 拉取失败(None)的人**不算 hot,也不更新记忆** —— 没数据就产不出事件,
           拉 trades 没有意义;而记忆保持不动,下一轮拉成功时照样能正确 diff 出新条目。
        ⚠️ 冷启动(_swap_seen 为空)时所有人都算 hot:那一轮要把持仓缓存填满,
           count_holders 才有全员覆盖。代价是启动后第一轮慢一次,之后每轮都快。
        """
        hot: set[str] = set()
        for uid, items in swaps.items():
            if items is None:
                continue
            ids = {i for i in (_pick_str(x, *_K_NATIVE_ID) for x in items if isinstance(x, dict)) if i}
            prev = self._swap_seen.get(uid)
            if prev is None or (ids - prev):
                hot.add(uid)
            self._swap_seen[uid] = ids
        return hot

    def _poll_feed(self, uids: list[str]) -> None:
        """
        低频拉一次关注流,只为了**捡观点**,捡到的放进 self._feed_thesis。

        ⚠️ 它曾经是"谁刚动过"的变更检测器,用来省掉全员 swaps。已经废弃这个用法:
           实测全员 swaps(70 人 12 线程)只要 1.2~3.6s,而 /v2/users/* 的限流很宽松
           (27 req/s 打 40 个、0 个 429);活动流只省下这 1~2s,却带来三个真问题 ——
             1) 这个端点有**独立且严格**的限流:每 10s 调一次跑两分钟就持续 429
                (retry-after: 0,重试也没用),而它一失败就退回全员扫描,
                请求量翻几倍 → 限流更严重 → 越滚越大;
             2) 它只覆盖**当前账号关注的人**,自己和没关注的人永远不在流里
                (实测自己在 100 条里出现 0 次),这些人只能靠兜底轮转,延迟 30~70s;
             3) 为了绕开 1) 和 2) 堆出来的 feed_blind / 兜底轮转 / 降级分支,
                复杂度远超它省下的那 1~2s。
           现在它只做一件事:捞那些**发在老仓位上**的观点。
           刚买就发的那种由 _thesis_priority 覆盖,不依赖这里。
        ⚠️ 流里的 swap 条目形态与 swaps 端点完全不同(没有 in/out 两条腿),
           永远不要拿它直接造事件 —— 那等于凭空多一套会静默失效的字段假设。
           观点条目是例外:它与 /feed/token/thesis 同形,normalize_thesis 直接吃得下,
           且 event_id 用的是同一个原生 id,两条路撞上也会被 INSERT OR IGNORE 去重。
        ⚠️ 失败只是少捞一批观点,买卖判定完全不依赖它 —— 所以这里静默降级,不告警。
        """
        self._feed_thesis = []
        if self._tick_no % _FEED_EVERY_N_TICKS:
            return
        watched = set(uids)
        try:
            items = self.client.get_activity_feed(limit=_FEED_LIMIT)
        except AuthError:
            raise                       # 登录态是全局问题,必须上抛
        except NotSupportedError:
            return                      # playwright 实现没有这个能力
        except Exception as e:          # noqa: BLE001
            logger.debug("活动流拉取失败(只影响观点补充,买卖不受影响) | {}", e)
            return

        current: set[str] = set()
        for it in items:
            fid = _pick_str(it, *_K_NATIVE_ID)
            if not fid or _pick_str(it, "userId") not in watched:
                continue
            current.add(fid)
            if fid not in self._feed_seen and it.get("type") == "thesis":
                self._feed_thesis.append(it)
        # ⚠️ 记忆只留**这次流里还在的** id:流是滚动窗口,滚出去的条目再也不会回来,
        #    一直累积就是无界增长。滚出去又意外重现的代价只是多推一次(会被主键去重挡住)。
        self._feed_seen = current

    def _note_thesis_priority(self, events: list[FomoEvent]) -> None:
        """
        把**刚动过的币**记进观点优先扫描名额。

        ⚠️ 观点几乎总是发在刚买的币上(实测:用户 10:05 买入、10:05 发观点)。
           而按币轮转一圈要几分钟(约 400 个币 / 每轮 12 个),
           用户的实际观感就是"观点没被监控到"—— 实测 10:05 发、10:09 才收到。
           留几个优先名额给刚碰过的币,观点就能在下一轮被抓到。
        """
        deadline = time.monotonic() + _THESIS_PRIORITY_TTL_SEC
        for ev in events:
            if ev.token_key is not None:
                self._thesis_priority[ev.token_key] = deadline
        now = time.monotonic()
        for k in [k for k, exp in self._thesis_priority.items() if exp <= now]:
            del self._thesis_priority[k]

    def _rotate_balances(self, uids: list[str], hot: set[str]) -> set[str]:
        """
        轮转刷新一小批「本轮没交易」的人的 balances。

        为什么在"有交易就当场刷"之外还需要轮转:还有两条我们看不见的路会改变
        「是否仍持有」—— 链上转账(FOMO 没有可用端点)和价格跌破 $1 dust 线。
        轮转让这类漂移最迟在一圈之内被纠正,代价只有每轮几个请求。

        ⚠️ 游标走在**完整名单**上,不是"去掉 hot 之后的池子"上:池子成员每轮都在变,
           在变长度的列表上取模不构成扫描,会有人长期轮不到。
        """
        if not uids:
            return set()
        n = min(_BALANCE_ROTATE_PER_TICK, len(uids))
        start = self._bal_rr % len(uids)
        self._bal_rr = (start + n) % len(uids)
        return {uids[(start + i) % len(uids)] for i in range(n)} - hot

    def _build_token_index(self, snapshots: dict) -> None:
        """
        从本 tick 的 trades + balances 建两张索引,供消息渲染补字段:

            _token_meta[(net, ca)]      → symbol / 市值 / 现价                  (全局共享)
            _positions[(uid, net, ca)]  → 持仓额 / 均价 / 未实现盈亏 / 已实现盈亏 (按人)

        ⚠️ swap 记录本身**不含** symbol、市值、持仓、均价、盈亏 —— 消息里那几行全靠这张索引。
        ⚠️ 两个来源各有不可替代的部分,所以都要吃:
             trades    → 已实现盈亏、剩余持仓(**含已平仓的单**),balances 里没有
             balances  → 市值(marketCap),trades 里没有
           顺序是 trades 先、balances 后,后者只用 setdefault 补空,不覆盖前者。
        ⚠️ 两边都没有的币,对应行整行消失,绝不本地推算(§10.4 铁律 2)。
        """
        meta: dict[tuple, dict] = {}
        pos: dict[tuple, dict] = {}

        # ---- 先吃 trades:它含**已平仓**的单子,是「已实现盈亏」和「剩余 $0.00」的唯一来源 ----
        # (清仓之后 balances 里就没这个币了,只有 closedTrades 还留着记录)
        for uid, snap in snapshots.items():
            for row in (getattr(snap, "trades", None) or []):
                t = row.get("trade") if isinstance(row, dict) else None
                if not isinstance(t, dict):
                    continue
                net = normalize_network(t.get("networkId"))
                ca = normalize_token_address(_pick_str(t, "tokenAddress"))
                if not net or not ca:
                    continue
                tm = t.get("tokenMetadata") if isinstance(t.get("tokenMetadata"), dict) else {}
                price = _f(tm.get("currentPrice"))
                qty = _f(t.get("humanTokenAmount"))
                cost = _f(t.get("totalCostBasis"))
                realized = _f(t.get("realizedPnlUsd"))
                m = meta.setdefault((net, ca), {})
                _fill(m, "symbol", _clean_symbol(_pick_str(tm, "symbol")))
                _fill(m, "price_usd", price)
                _fill(m, "network_raw", t.get("networkId"))

                # ⚠️ 同一个币可能同时有一条活跃单和多条已平仓单(买→清→再买)。
                #    无脑覆盖的话,后写的已平仓单(剩余量 0)会把活跃单的真实持仓抹成 $0.00 ——
                #    实测就踩了这个:明明还持有 $3.79,消息里显示「持仓 $0.00」。
                #    分工:**活跃单**给持仓/均价/未实现盈亏,**已平仓单**只给已实现盈亏。
                p = pos.setdefault((uid, net, ca), {})
                is_open = t.get("closedAt") is None
                unreal = _f(t.get("unrealizedPnlUsd"))
                snapshot = {
                    # 剩余数量 × 现价。清仓时 humanTokenAmount=0 → 恰好渲染成「剩余 $0.00」
                    "holding_usd": (qty * price) if (qty is not None and price is not None) else None,
                    "avg_price": _f(t.get("avgEntryPrice")),
                    "pnl": unreal,
                    "pnl_pct": (unreal / cost * 100) if (unreal is not None and cost) else None,
                }
                if is_open:
                    # 活跃单是当前真实持仓,无条件覆盖
                    p.update(snapshot)
                    p["_open"] = True
                elif not p.get("_open") and not p.get("_written"):
                    # 没有活跃单时才用已平仓单兜底(它给出的正是「剩余 $0.00」)。
                    # ⚠️ 哨兵必须是独立的布尔量,**不能拿 holding_usd is None 兼职** ——
                    #    holding_usd = qty × price,而 tokenMetadata 缺 currentPrice 时它本身
                    #    就是 None(真实响应里不罕见)。哨兵落不下闩,后面每条已平仓单都会
                    #    整体覆盖 avg_price/pnl,而 closedTrades 是**倒序**返回的,
                    #    于是"最早的胜出" —— 卖出消息会显示半年前那笔仓位的成本价,
                    #    还跟同一条消息里的盈亏自相矛盾。
                    p.update(snapshot)
                    p["_written"] = True
                # 已实现盈亏取**最近一次**平仓的那笔 —— closedTrades 按 closedAt 倒序返回,
                # 所以只认第一条命中的,后面更早的不覆盖
                if realized is not None and p.get("realized_pnl") is None:
                    p["realized_pnl"] = realized
                    p["realized_pnl_pct"] = (realized / cost * 100) if cost else None

        # ---- 再吃 balances:市值只有它有;已被 trades 写过的键用 setdefault 保护 ----
        for uid, snap in snapshots.items():
            for b in (getattr(snap, "balances", None) or []):
                if not isinstance(b, dict):
                    continue
                net, ca = _balance_key(b)
                if not net or not ca:
                    continue
                tfr = b.get("tokenFilterResult") if isinstance(b.get("tokenFilterResult"), dict) else {}
                tok = tfr.get("token") if isinstance(tfr.get("token"), dict) else {}
                price = _f(tfr.get("priceUSD"))
                m = meta.setdefault((net, ca), {})
                # 只补空、不覆盖:同一个币多人持有时以先到的为准,值都一样
                _fill(m, "symbol", _clean_symbol(_pick_str(tok, "symbol")))
                _fill(m, "market_cap", _f(tfr.get("marketCap")))
                _fill(m, "price_usd", price)
                # 拉 thesis 时要用**原始**数字 networkId(1399811149),
                # 不能用归一化后的 "solana" —— 那是我们内部的聚合键,API 不认
                _fill(m, "network_raw", tok.get("networkId"))
                # 币龄。⚠️ 取 token.createdAt(**合约创建**),不是 tokenFilterResult.createdAt
                #    —— 后者是**交易对/池子**的创建时间,两者对新币几乎一样,对老币差得离谱:
                #    实测 USDC 的 token.createdAt 是 2020-10-13(对),
                #    而 tfr.createdAt 是 2024-03-20(那只是某个池子建的时间)。
                #    拿错字段的话,一个五年的老币会显示成"币龄 5D"。
                _fill(m, "created_at", _token_created_at(tok))

                ut = b.get("userToken") if isinstance(b.get("userToken"), dict) else None
                if not ut:
                    continue
                cost = _f(ut.get("currentCostBasisUsd"))
                holding = _balance_usd(b)
                pnl = (holding - cost) if (holding is not None and cost is not None) else None
                # ⚠️ 只补 trades 没给出的字段,**不能整体覆盖** ——
                #    trades 那份带已实现盈亏,且覆盖已平仓的单,信息严格更全。
                p = pos.setdefault((uid, net, ca), {})
                for k, v in (
                    ("holding_usd", holding),
                    ("avg_price", _f(ut.get("averageEntryPriceUsd"))),
                    ("pnl", pnl),
                    ("pnl_pct", (pnl / cost * 100) if (pnl is not None and cost) else None),
                ):
                    if p.get(k) is None:
                        p[k] = v
        self._token_meta = meta
        self._positions = pos
        logger.debug("代币索引 {} 个 · 持仓索引 {} 条", len(meta), len(pos))

    def _save_token_snapshots(self, conn) -> None:
        """
        把本 tick 的行情落库,供 /hot 算"买入时市值 → 现在市值"的倍数。

        ⚠️ 不落库的话 /hot 执行时得现拉几十个币的行情,一条命令要等十几秒。
           这里是顺手写几十行,几乎零成本。
        ⚠️ 只覆盖名单里还有人持有的币 —— 清仓后不再更新,
           updated_at 就是它最后已知的时间,/hot 会据此标注数据是不是旧的。
        """
        # ⚠️ 只收**有市值**的行。市值才是 /hot 算倍数的依据,而 updated_at 的语义是
        #    "这个市值有多新"。带着 market_cap=None 的行(比如已清仓的币,
        #    closedTrades 里仍有 currentPrice)照样写进来的话,会把时间戳一路刷新,
        #    /hot 的「行情已过期」提示就永远触发不了。
        rows = [
            (net, ca, m.get("symbol"), m.get("price_usd"), m.get("market_cap"))
            for (net, ca), m in self._token_meta.items()
            if m.get("market_cap") is not None
        ]
        if not rows:
            return
        try:
            with store.tx(conn):
                store.upsert_token_snapshots(conn, rows)
        except Exception as e:  # noqa: BLE001
            # 行情快照只影响 /hot 的展示,绝不能因为它失败而中断本轮推送
            logger.warning("行情快照落库失败(不影响推送): {}", e)

    def _pick_thesis_batch(self) -> list[tuple]:
        """
        选出本轮要扫的代币并前进轮转游标,返回 [(net, ca, network_raw), …]。

        ⚠️ 读的是**上一轮**的 _token_meta —— 本函数在 _build_token_index 之前调用,
           为的是让网络请求能和快照采集并行。它只决定"这轮扫哪些币",
           不参与任何事件判定,晚一轮完全无害;首轮 meta 为空则本轮不扫。
        ⚠️ network_raw 必须在这里就**取出来带走**,不能让后台线程回头再查
           self._token_meta —— 那个字段会被 _build_token_index 整体换掉(第 941 行是重新赋值),
           后台线程正好在中间读到新字典时,老字典里才有的币会取到 None,
           带着 networkId=None 发出去就是一个 400,那个币的观点这轮静默丢掉。
        """
        tokens = sorted(  # 固定顺序,轮转才有意义
            (k for k, v in self._token_meta.items() if v.get("network_raw") is not None)
        )
        if not tokens:
            return []
        meta = self._token_meta          # 定住这一版,循环里不再重新解引用

        # ---- 优先名额:活动流看不见的人刚动过的币 ----
        # ⚠️ 这些人的观点不会出现在活动流里,只能按币查。而轮转一圈要几分钟 ——
        #    用户实测「10:05 发观点、10:09 才收到」正是这么来的。
        now = time.monotonic()
        picked = [k for k, exp in sorted(self._thesis_priority.items())
                  if exp > now and k in meta][:_THESIS_PRIORITY_SLOTS]

        # ---- 其余名额按轮转填满 ----
        n = len(tokens)
        take = min(_THESIS_TOKENS_PER_TICK - len(picked), n)
        if take > 0:
            start = self._thesis_rr % n
            self._thesis_rr = (start + take) % n
            seen = set(picked)
            for i in range(take):
                k = tokens[(start + i) % n]
                if k not in seen:
                    seen.add(k)
                    picked.append(k)
        return [(*k, meta[k]["network_raw"]) for k in picked]

    def close(self) -> None:
        """
        释放常驻资源。cli 退出时调用。

        ⚠️ 不关的话,ThreadPoolExecutor 的 atexit 钩子会在解释器退出时 join 这条线程,
           而它可能正卡在一个 HTTP 超时里 —— 表现就是 Ctrl+C 之后进程还挂着不走
           (用户日志里那段 `_python_exit → t.join() → KeyboardInterrupt` 就是它)。
        """
        pool, self._thesis_pool = self._thesis_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        # ⚠️ 只叫停、**不等**在途的那一单:它可能还有一分钟才跑完,
        #    而 Ctrl+C 之后进程挂着不走是用户已经反馈过一次的问题。
        #    代价是那一单停在 auto_executing —— 由启动对账去认领,
        #    绝不能在这里替它写 failed:点击可能已经发生,钱可能已经出去了。
        worker, self._copy_worker = self._copy_worker, None
        if worker is not None:
            worker.close()

    def _start_thesis(self, batch: list[tuple]):
        """
        启动观点的网络采集,返回一个"去取结果"的可调用对象(阻塞到拿到为止)。

        ⚠️ 不可并发的实现(playwright)必须**留在主线程**同步跑完,绝不能丢进后台线程。
           它按 threading.local 私有创建整套 playwright + chromium,而线程退出时不回收 ——
           每 tick 一条新线程就是每 tick 泄漏一套浏览器(约 300MB),12s 一轮的话
           几分钟就能把内存吃光。这与 _fetch_snapshots 里那道守卫是同一个理由、同一个故障,
           只是换了个入口进来。
        ⚠️ 线程池**跨 tick 复用**,不是每轮新建:每轮建一条新线程等于每轮重做
           TLS 握手(curl 会话是 threading.local 的),而且对 playwright 就是上面那个泄漏。
           batch 为空时连池子都不建 —— 冷启动第一轮和空名单都属于这种情况。
        """
        if not batch:
            return list                       # 调用它返回 [],等价于"没扫任何币"
        if not getattr(self.client, "supports_concurrency", True):
            raw = self._fetch_thesis_raw(batch)
            return lambda: raw
        if self._thesis_pool is None:
            self._thesis_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="fomo-thesis-bg")
        return self._thesis_pool.submit(self._fetch_thesis_raw, batch).result

    def _fetch_thesis_raw(self, batch: list[tuple]) -> list | BaseException:
        """
        纯网络:把一批代币的观点拉回来。**不碰 DB、不碰 conn** ——
        它跑在后台线程里,而 sqlite 连接不跨线程。

        返回与 batch 等长的列表(单个失败置 None);登录态失效时返回那个异常本身,
        由主线程 raise —— 后台线程里抛出去没人接得住。
        """
        if not batch:
            return []
        after_ms = int((time.time() - _THESIS_LOOKBACK_SEC) * 1000)
        auth_err: list[AuthError] = []

        def fetch_one(key):
            # ⚠️ network_raw 由 _pick_thesis_batch 一并带过来,这里**不碰 self._token_meta** ——
            #    主线程正在重建它,后台线程回头去查就是一个数据竞争(见 _pick_thesis_batch)。
            _net, ca_, raw_net = key
            try:
                return self.client.get_token_thesis(ca_, raw_net, after_ms=after_ms)
            except AuthError as e:
                auth_err.append(e)          # 登录态问题是全局的,收集后交给主线程上抛
                return None
            except Exception as e:          # noqa: BLE001
                logger.debug("thesis 拉取失败 token={} err={}", ca_[:16], e)
                return None

        # ⚠️ 这批请求彼此无依赖,串行纯属浪费:实测串行 25 个 4.1s、并发 8 只要 2.2s。
        #    playwright 实现按线程建整套浏览器,必须退回串行(同 _fetch_snapshots)。
        if not getattr(self.client, "supports_concurrency", True) or len(batch) == 1:
            results = [fetch_one(k) for k in batch]
        else:
            with ThreadPoolExecutor(max_workers=min(_THESIS_WORKERS, len(batch)),
                                    thread_name_prefix="fomo-thesis") as ex:
                results = list(ex.map(fetch_one, batch))
        return auth_err[0] if auth_err else results

    def _collect_thesis(self, conn, users, batch: list[tuple],
                        results: list | BaseException) -> list[FomoEvent]:
        """
        观点采集。两条路合流:

          A) 活动流(_poll_feed 顺手捡的)—— **零额外调用、下一轮就能推**。
             流里的观点条目与 /feed/token/thesis 同形,直接喂 normalize_thesis。
          B) 按代币轮转扫描 —— 兜底。活动流只覆盖"当前登录账号关注的人",
             名单里没关注的人只能靠这条路捞回来。

        为什么 B 这么绕:FOMO **没有**"按用户查观点"的端点(/feed/user/thesis 是 404),
        只能遍历监控用户持仓里的币 → 按币拉 → 按 userId 过滤。代价是调用量 = 持仓币数,
        所以压了三道:
          1) 跨用户去重 —— 多人持有同一个币只拉一次(_token_meta 天然按币聚合)
          2) 每 tick 最多拉 _THESIS_TOKENS_PER_TICK 个,**轮转覆盖**,
             币多时延迟变长但调用量恒定,不会随名单规模爆炸
          3) 带 afterTime 增量拉(单位**毫秒**,传秒会被服务端忽略)

        ⚠️ 两条路都用原生 id 生成 event_id,同一条观点两边都抓到也会被
           INSERT OR IGNORE 去重,不会推两遍。
        ⚠️ B 只能看到"监控用户当前持仓的币"下的观点。他对已清仓的币发的观点看不到 ——
           这是已知盲区(A 不受此限),写在这里免得日后当成 bug 查。
        """
        # ⚠️ 名单直接从入参 users 取,不读 self._watched_ids ——
        #    后者由 tick() 里的 _refresh_watched_index 设置,本函数收了 users 却依赖
        #    另一处设的实例字段,是隐藏耦合:换个调用顺序就静默返回空,不报错。
        watched = {u["user_id"] for u in users}
        if not watched:
            return []

        by_user: dict[str, list[dict]] = {}
        # ---- A) 活动流顺手捡到的观点。零额外调用,先收进来 ----
        for it in self._feed_thesis:
            if it.get("userId") in watched:
                by_user.setdefault(it["userId"], []).append(it)

        # ---- B) 按代币轮转扫描的结果(网络部分已在后台线程跑完,见 _fetch_thesis_raw)----
        if isinstance(results, BaseException):
            raise results

        failed = sum(1 for r in results if r is None)
        for items in results:
            if items is None:
                continue
            for it in items:
                # 按币查会把该币下**所有人**的观点都拉回来(热门币一次 100 条),
                # 不按 userId 过滤就会把陌生人的观点当成监控对象推给用户
                if isinstance(it, dict) and it.get("userId") in watched:
                    by_user.setdefault(it["userId"], []).append(it)

        if failed:
            logger.warning("thesis 本轮 {}/{} 个代币拉取失败", failed, len(batch))
        if not by_user:
            return []
        if self._feed_thesis:
            logger.debug("thesis | 活动流直取 {} 条 · 轮转扫描 {} 个代币",
                         len(self._feed_thesis), len(batch))

        out: list[FomoEvent] = []
        rows = {u["user_id"]: u for u in users}
        for uid, items in by_user.items():
            u = rows.get(uid)
            if u is None:
                continue
            try:
                evs = self.normalize_thesis(u, items)
            except Exception as e:          # noqa: BLE001
                logger.error("归一化 thesis 整批失败 user={} err={}", uid, e)
                continue
            out.extend(_drop_before_cursor(evs, store.get_cursor(conn, uid, "thesis")))
        logger.debug("thesis 扫描 {} 个代币 → 命中 {} 条", len(batch), len(out))
        return out

    def _collect_events(self, conn, users, snapshots: dict) -> list[FomoEvent]:
        events: list[FomoEvent] = []
        for u in users:
            uid = u["user_id"]
            snap = snapshots.get(uid)
            if snap is None:
                continue
            # ⚠️ 这里只处理 swaps。
            #    - transfers 已砍掉:/v2/transfers/with/{uid} 是「**我**与该用户之间的转账」
            #      (对自己调返回 400 "Cannot fetch transfers with self"),
            #      拿不到别人与第三方的转账,FOMO 也没有别的入口。
            #    - thesis 没有按用户查的端点,走 _collect_thesis 按代币采集后过滤。
            for kind, items, fn in (
                ("swaps", getattr(snap, "swaps", None), self.normalize_swaps),
            ):
                if items is None:
                    # None 有两种来源,必须分开看:
                    #   本轮排到他了却拿回 None = 真的拉取失败,要 WARN;
                    #   本轮压根没排到他(活动流说他没动、也没轮到兜底扫描)= 正常,静默跳过。
                    #   不分开的话,稳态下每轮会对五十几个人各刷一条 WARNING。
                    if uid not in self._swaps_attempted:
                        continue
                    logger.warning("{} 的 {} 本 tick 拉取失败,本类跳过", uid, kind)
                    continue
                try:
                    evs = fn(u, items)
                except Exception as e:  # noqa: BLE001
                    logger.error("归一化 {} 整批失败 user={} err={}", kind, uid, e)
                    continue
                events.extend(_drop_before_cursor(evs, store.get_cursor(conn, uid, kind)))
        return events

    def _dispatch_catchup(self, conn, new_events: list[FomoEvent], dry_run: bool) -> None:
        """
        停机汇总:把积压事件统一标记为已处理,只发一条概览。

        ⚠️ 必须 mark_sent,不能只是"跳过发送" —— 否则 sent 永远是 0,
           补发队列会把它们一条条捞出来重推,等于绕开了整个汇总机制。
        ⚠️ 汇总失败也要标记:宁可漏一条汇总,也不能让几百条积压在下一轮喷出来。
        """
        since = self._catchup_since or now_iso()
        try:
            with store.tx(conn):
                for ev in new_events:
                    store.mark_sent(conn, ev.event_id, None, None)
        except Exception as e:  # noqa: BLE001
            logger.error("汇总模式标记已发送失败: {}", e)

        try:
            text = self._build_catchup_digest(conn, since, new_events)
        except Exception as e:  # noqa: BLE001
            logger.warning("汇总消息渲染失败,降级为最简文本: {}", e)
            text = (f"🌙 <b>停机期间汇总</b>\n"
                    f"积压 {len(new_events)} 条事件已入库,未逐条推送。\n"
                    f"看买入榜:/hot 今日")
        if dry_run:
            logger.info("[dry-run] {}", text.replace("\n", " ⏎ "))
            return
        self.notifier.send(text)

    def _build_catchup_digest(self, conn, since: str, new_events: list[FomoEvent]) -> str:
        """用 /hot 同一套聚合做停机概览 —— 关心的本来就是"这段时间大家在买什么"""
        gap_min = 0.0
        try:
            gap_min = (datetime.now(UTC) - datetime.fromisoformat(since)).total_seconds() / 60
        except ValueError:
            pass
        gap = (f"{gap_min / 60:.1f} 小时" if gap_min >= 60 else f"{gap_min:.0f} 分钟")

        # ⚠️ 按**本轮实际入库的那批**计数,不查 DB 时间窗:
        #    游标可能比 since 更早(停机前就落后了),按窗口查会报出与
        #    "已全部入库 N 条"对不上的数字,用户第一反应是"是不是漏了"。
        counts: dict[str, int] = {}
        for ev in new_events:
            counts[ev.event_type] = counts.get(ev.event_type, 0) + 1
        buys, sells = counts.get(EVENT_BUY, 0), counts.get(EVENT_SELL, 0)
        thesis = counts.get(EVENT_THESIS, 0)

        lines = [
            f"🌙 <b>停机期间汇总 · {gap}</b>",
            f"共 {len(new_events)} 条:{buys} 买 · {sells} 卖"
            + (f" · {thesis} 条观点" if thesis else ""),
            "已全部入库,不逐条推送(避免几百条过期消息刷屏)。",
        ]
        hot = store.hot_tokens(conn, since, limit=5)
        if hot:
            # 同名不同链的币会在榜里并列出现($sami 同时有 Base 和 Solana 版本),
            # 只有重名时才补链名 —— 不重名时加上纯属噪音
            seen: dict[str, int] = {}
            for r in hot:
                s = str(r["symbol"] or "?")
                seen[s] = seen.get(s, 0) + 1
            lines.append("\n<b>买入最集中的:</b>")
            for i, r in enumerate(hot, 1):
                sym = html.escape(str(r["symbol"] or "?"))
                suffix = ""
                if seen.get(str(r["symbol"] or "?"), 0) > 1:
                    suffix = f" ({html.escape(str(r['network_id'] or '?'))})"
                lines.append(f"{i}. <b>${sym}</b>{suffix} · 👥 {r['buyers']} 人 · "
                             f"${_num_fmt(r['total_usd'])}")
        lines.append("\n完整榜单:/hot 今日  ·  恢复正常推送 ✅")
        return "\n".join(lines)

    def _persist(self, conn, events: list[FomoEvent]) -> list[FomoEvent]:
        """
        第一循环:单事务内 judge_badge → insert_event → upsert_stats。

        ⚠️ judge_badge 必须在 upsert_stats **之前**,否则 buy_count 已经 +1,永远判不出 FIRST。
        ⚠️ upsert_stats 只在 insert_event 返回 True 时调用,否则重复轮询会把 buy_count 一路虚增。
        """
        if not events:
            return []
        new_events: list[FomoEvent] = []
        try:
            with store.tx(conn):
                for ev in events:
                    badge, reason = store.judge_badge(conn, ev)
                    ev.badge, ev.badge_reason = badge, reason
                    if store.insert_event(conn, ev):
                        if store.should_count(ev, reason):
                            store.upsert_stats(conn, ev)
                        if ev.quote_only:
                            # 稳定币互换:落库但不推送。
                            # ⚠️ 必须在这里就把 sent 置 1 —— 只是"跳过发送"的话,
                            #    sent 永远是 0,补发队列会每 20 秒把它捞出来重试一次、
                            #    连续捞 10 分钟,比推出去还费。
                            store.mark_sent(conn, ev.event_id, None, None)
                            continue
                        new_events.append(ev)
        except Exception as e:  # noqa: BLE001
            # 事务已回滚,库里什么都没写 —— 必须同时丢弃 new_events,
            # 否则会推送一批"库里不存在"的消息,且 mark_sent 找不到行、下轮再推一次
            logger.error("落库事务失败,本 tick 全部回滚: {}", e)
            return []
        if new_events:
            logger.info("本 tick 新事件 {} 条", len(new_events))
        return new_events

    def _dispatch(self, conn, snapshots: dict, new_events: list[FomoEvent], dry_run: bool) -> None:
        """
        第二循环:此时 stats 已是一致快照,所有消息共用同一个共识时点值。
        """
        # 停机后的第一轮:积压的事件已经全部入库(共识计数、首次建仓判定、
        # /hot 榜单都读库,数据完整性不受影响),这里只发一条汇总,不逐条推。
        if self._catchup_since:
            self._dispatch_catchup(conn, new_events, dry_run=dry_run)
            return

        pending = list(new_events)
        seen = {e.event_id for e in new_events}
        # C-2 补发:落库成功但推送失败/进程崩溃的事件。只捞 10 分钟内的 ——
        # 更早的补出去已经没有交易价值,反而制造困惑
        try:
            for row in store.load_unsent_recent(conn, minutes=10):
                if row["event_id"] in seen:
                    continue
                ev = _event_from_row(row)
                if ev is not None:
                    pending.append(ev)
        except Exception as e:  # noqa: BLE001
            logger.warning("补发队列加载失败(不影响本 tick 新事件): {}", e)

        if not pending:
            return

        ready = store.ready_user_ids(conn)
        # ⚠️ 星标是纯展示。查不出来就当没有 —— 绝不能让它挡住任何一条推送
        try:
            starred = store.starred_user_ids(conn)
        except Exception as e:  # noqa: BLE001
            logger.warning("特别关注名单读取失败,本轮不打星标 | {}", e)
            starred = set()
        for ev in pending:
            buyers = watchlist = holders = None
            baseline_pending = False
            # ⚠️ 共识计算整段包在 try 里,失败一律降级为 None。
            #    绝不能出现"因为算不出共识数所以整条推送失败"。
            try:
                baseline_pending = not store.is_stats_ready(conn, ev.user_id)
                buyers, watchlist = store.count_consensus(conn, ev)
                # buyers 为 None 时共识行整段消失,holders 也就没有存在的意义了
                if buyers is not None:
                    holders = count_holders(snapshots, ready, ev)
            except Exception as e:  # noqa: BLE001
                logger.warning("共识计算失败,降级为不显示 | {} | {}", ev.event_id, e)
                buyers = watchlist = holders = None

            try:
                text = render(
                    ev,
                    buyers=buyers,
                    watchlist=watchlist,
                    holders=holders,
                    baseline_pending=baseline_pending,
                    starred=ev.user_id in starred,
                )
            except Exception as e:  # noqa: BLE001
                # 渲染炸了只丢这一条,后面的照发。sent 保持 0,下一 tick 会再试
                logger.error("渲染失败,跳过该条 | {} | {}", ev.event_id, e)
                continue

            if dry_run:
                logger.info("[dry-run] {}", text.replace("\n", " ⏎ "))
                continue

            # 串行发送 + 令牌桶限速,规避 TG 同 chat 的限流
            self._throttle_send()

            try:
                ok = self.notifier.send(text)
            except Exception as e:  # noqa: BLE001
                logger.error("推送异常 | {} | {}", ev.event_id, e)
                continue
            if ok:
                # ⚠️ 只能在 TG 确认收到之后才置 sent=1。"发之前就标已发"会在崩溃时永久丢消息
                store.mark_sent(conn, ev.event_id, buyers, watchlist)

    def _check_copytrade(self, conn, new_events: list[FomoEvent], dry_run: bool) -> None:
        """
        跟单信号:本轮有新买入的币,够不够"N 个名单成员买过"的门槛。

        ⚠️ 只看**本轮有新买入**的币,不是全表扫描:信号的语义是"刚刚又多了一个人买",
           全表扫会在配置放宽的那一刻把历史上所有够格的币一次性全建仓。
        ⚠️ 判定逻辑全在 copytrade.decide()(纯函数、可单测),这里只负责取事实和记账。
        ⚠️ 真实下单**永远**由用户点 TG 按钮触发,这里最多把状态记成 pending 并推一条待确认。
        ⚠️ **停机补数那一轮整段跳过** —— 见下面 _catchup_since 那道守卫。
        """
        cfg = store.load_copy_config(conn)
        if not cfg.enabled:
            return

        # ⚠️ 停机补数轮:new_events 是**整段积压**,不是"刚刚发生的事"。
        #    推送侧早就承认了这一点并降级成一条汇总(见 _dispatch 里的同款守卫,
        #    理由写在 _detect_gap:"那时候的信息早就过期了")。跟单侧更不能放行 ——
        #    窗口是 24h,积压里大把币满足"≥N 人买过",而 entry_mcap 取的是**现在**的
        #    市值。结果就是拿今天拉升后的价,去买昨晚那批已经走完的信号。
        #    ($Plumber 就是这么个例子:第 2 个人进是 31.62x,第 5 个人进是 0.74x。)
        # ⚠️ 别指望"冷启动那轮 balances 没拉全、多数币拿不到币龄"来兜底 ——
        #    那是巧合不是防线:max_age_hours 一设成 None 就没了。
        if self._catchup_since:
            logger.info("停机补数轮,跟单判定整段跳过(积压信号已过期)")
            return

        keys = {e.token_key for e in new_events if e.event_type == EVENT_BUY and e.token_key}
        if not keys:
            return

        # 本轮买入事件自带的成交市值 —— 这是**最新鲜**的入场价来源。
        # ⚠️ 它就是"名单里那个人刚刚是在什么价位买的",比 balances 快照更贴近
        #    我们跟进去时的实际价位。实测覆盖率 84%,高于任何其他来源。
        ev_mcap: dict[tuple, float] = {}
        for e in new_events:
            if e.event_type == EVENT_BUY and e.token_key and e.market_cap is not None:
                ev_mcap[e.token_key] = e.market_cap   # 同一轮多笔时取最后一笔

        since = iso_minutes_ago(cfg.window_hours * 60)
        # ⚠️ **按信号强弱排,不是按合约地址排。**
        #    原来是 sorted(keys),排序键是 (network_id, token_address) 的字典序 ——
        #    与信号质量、时间先后毫无关系。有人盯着的时候这只影响按钮顺序;
        #    自动之后额度是有限的,这个顺序**决定当天的钱买了哪几个币**,
        #    而"按链名 + CA 首字母花钱"显然不是任何人想要的。
        #    买家数降序;同样多的按最早那笔买入的时间升序(先动的先跟)。
        # ⚠️ 这一步在 per-token try 的**外面**,所以它自己必须逐个兜底 ——
        #    否则一个币查询出错就又能掀掉整轮,把 A5 那道防线从背后绕过去。
        #    (这正是被 test_单个币处理失败不影响本轮其余币 抓到的一次回归。)
        #    失败按 0 计:0 一定小于 min_buyers,也就是"这个币本轮不跟" —— 安全的那一侧。
        buyers = {}
        for k in keys:
            try:
                buyers[k] = store.count_recent_buyers(conn, k[0], k[1], since, cfg.starred_only)
            except Exception as e:  # noqa: BLE001
                logger.exception("买家数查询失败,本轮跳过这个币 | {} {} | {}", k[0], k[1][:10], e)
                buyers[k] = 0
        first_ts = {}
        for e in new_events:
            if e.event_type == EVENT_BUY and e.token_key:
                k = e.token_key
                if k not in first_ts or e.event_ts < first_ts[k]:
                    first_ts[k] = e.event_ts
        ordered = sorted(keys, key=lambda k: (-buyers[k], first_ts.get(k, ""), k))

        # ⚠️ 两个上限的分子。查一次、循环内自增 —— 每个币都重查一遍库不但浪费,
        #    也挡不住同一轮内的累计(record 是逐个提交的,重查反而看起来"对")。
        used = {"n": store.copy_taken_today(conn), "usd": store.copy_spent_today(conn)}
        for net, ca in ordered:
            # ⚠️ 每个币独立兜底。循环体里马上要接真实下单,而执行器有十几处 raise
            #    (profile 被占、会话过期、页面改版、报价超时 —— 全是常态)。
            #    没有这道 try,一个币出事会掀掉**本轮剩下所有币**的判定,
            #    而外层那句"跟单信号判定失败(不影响推送)"会把它伪装成无害。
            try:
                self._copy_one(conn, net, ca, cfg, since, used,
                               ev_mcap.get((net, ca)), buyers[(net, ca)], dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                logger.exception("跟单单币处理失败,跳过这个币继续 | {} {} | {}", net, ca[:10], e)

    def _copy_one(self, conn, net: str, ca: str, cfg, since: str, used: dict,
                  event_mcap: float | None, buyers: int, *, dry_run: bool) -> None:
        """
        判定并记账**一个**币。异常由调用方按币兜底,见 _check_copytrade。

        used 是本轮共享的当日用量 {n: 笔数, usd: 金额},命中后就地自增 ——
        ⚠️ 不自增的话,一个 tick 里命中 15 个币会 15 单全过,当日上限形同虚设。
        """
        meta = self._token_meta.get((net, ca)) or {}
        # 入场市值按**新鲜度**取,不是按哪张表方便:
        #   1) 本轮买入事件自带的成交市值 —— 名单里那个人刚刚就是在这个价位买的
        #   2) 本轮 balances 的快照
        #   3) 够新的 token_snapshot(≤60min)
        # ⚠️ 原来的写法是 token_snapshot **优先**,正好反了。而 token_snapshot
        #    只覆盖"名单里还有人持有"的币,清仓后就冻住不动 —— 拿几小时前的低市值
        #    当"现在的价",会让 max_entry_mcap 放行本该拦掉的币,
        #    还会在真实仓位上凭空记出一笔纸面盈利。
        entry_mcap = event_mcap
        if entry_mcap is None:
            entry_mcap = meta.get("market_cap")
        if entry_mcap is None:
            entry_mcap = store.fresh_snapshot_mcap(conn, net, ca)
        cand = Candidate(
            network_id=net,
            token_address=ca,
            token_symbol=meta.get("symbol"),
            buyers=buyers,          # 排序时已经查过,不再查第二遍
            entry_mcap=entry_mcap,
            token_created_at=meta.get("created_at"),
            already_taken=False,     # 由 record_copy_signal 的主键冲突兜底,见下
            taken_today=used["n"],
            spent_today=used["usd"],
        )
        d = decide(cand, cfg)
        if not d.take:
            logger.debug("跟单跳过 | {} {} | {}", cand.token_symbol, ca[:10], d.reason)
            return

        # 三种落地方式,状态各不相同:
        #   纸上   → paper        不花钱,不推按钮
        #   人工   → pending      推一条带 [确认买入] 的消息,等人点
        #   无人值守 → auto_queued  直接进买入队列,没人点
        # ⚠️ 无人值守**不走 pending**:那样会同时存在"排队中"和"可点确认"两个入口,
        #    人点一次、队列再跑一次 —— 而两条路的终态会互相覆盖。
        auto = cfg.auto_execute and not cfg.paper_only
        status = "paper" if cfg.paper_only else (copyworker.ST_QUEUED if auto else "pending")
        # ⚠️ 主键冲突 = 这个币已经跟过。用 INSERT OR IGNORE 的返回值判断,
        #    而不是先查一次 —— 先查后插在两个 tick 撞上时会重复建仓。
        if not store.record_copy_signal(
            conn, network_id=net, token_address=ca, token_symbol=cand.token_symbol,
            buyers=cand.buyers, entry_mcap=cand.entry_mcap, age_sec=d.age_sec,
            amount_usd=cfg.amount_usd, status=status,
        ):
            return
        used["n"] += 1
        if status in store.SPENDING_STATUSES:
            used["usd"] += cfg.amount_usd
        logger.info("跟单信号 | {} · {} 人买过 · 入场市值 {} · {}",
                    cand.token_symbol, cand.buyers, cand.entry_mcap, status)
        if dry_run:
            return
        if auto:
            self._enqueue_buy(conn, cand, cfg)
        else:
            self._send_copy_signal(cand, d, cfg, status)

    def _maybe_copy_summary(self, conn, *, dry_run: bool) -> None:
        """
        跨 UTC 日的第一轮,推一条前一天的跟单对账。

        ⚠️ 挂在 tick 上而不是另起一个定时任务:重启、关机过夜都不会漏 ——
           只要下次跑起来发现"上次报的还是更早那天",就补上。
        ⚠️ 无人值守最需要的就是这条:没人点按钮,也就没人知道今天到底花了多少。
        """
        if dry_run:
            return
        today = now_iso()[:10]
        last = store.get_state(conn, _COPY_SUMMARY_KEY)
        if last == today:
            return
        # 首次运行只记下今天,不去补一条空的昨天
        if last is not None and last < today:
            s = store.copy_day_summary(conn, last)
            if s["total"]:
                self.notifier.send(self._render_copy_summary(s))
        with store.tx(conn):
            store.set_state(conn, _COPY_SUMMARY_KEY, today)

    def _maybe_poll_pnl(self, conn) -> None:
        """
        每 20 轮拉一次名单成员盈亏,一个请求拿全 80 人。

        ⚠️ 失败静默降级:这只是网页上的一列展示,买卖判定完全不依赖它。
        """
        if self._tick_no % _PNL_EVERY_N_TICKS:
            return
        try:
            # ⚠️ limit 必传,不带直接 400;服务端上限 100。
            # ⚠️ 必须用 "following":实测只有它同时返回 totalPnL/pnl24h/pnl7d/pnl30d,
            #    而 "7d" 之类返回的是**全站前 100 榜**,里面大半不是我们名单的人。
            board = self.client.get_leaderboard("following", limit=100)
        except NotSupportedError:
            return
        rows = []
        for it in board or []:
            uid = _pick_str(it, "id", "userId")
            if not uid:
                continue
            rows.append({
                "user_id": uid,
                "total_pnl": _f(it.get("totalPnL")),
                "pnl_24h": _f(it.get("pnl24h")),
                "pnl_7d": _f(it.get("pnl7d")),
                "pnl_30d": _f(it.get("pnl30d")),
                "total_holdings": _f(it.get("totalHoldings")),
                "num_trades": _i(it.get("numTrades")),
            })
        if rows:
            store.save_user_pnl(conn, rows)
            logger.debug("名单盈亏已更新 {} 人", len(rows))

    @staticmethod
    def _render_copy_summary(s: dict) -> str:
        label = {"paper": "🧪 纸上", "filled": "✅ 已成交", "failed": "❌ 未成交",
                 "rejected": "🚫 已忽略", "expired": "⌛ 已作废",
                 "pending": "⏳ 待确认", "unknown": "❔ 结果未知",
                 "auto_queued": "📥 排队中", "auto_executing": "🔄 执行中"}
        lines = [f"📒 <b>跟单日报</b> · {s['day']}(UTC)",
                 f"共 {s['total']} 单 · 真实出账 {s['spent_usd']:,.2f} 美元"]
        for st, v in sorted(s["by_status"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"· {label.get(st, st)} {v['n']} 单")
        if s["unclear"]:
            # ⚠️ 单独一行、放最后 —— 这是唯一需要你**动手去核对**的一格
            lines.append(f"\n⚠️ 有 <b>{s['unclear']}</b> 单结果不确定,请到 APP 核对持仓")
        return "\n".join(lines)

    def _enqueue_buy(self, conn, cand: Candidate, cfg) -> None:
        """
        把这一单丢进买入队列。⚠️ **不在这里执行** —— 见 copyworker 模块头:
        一笔买入要几十秒,而这里跑在 15s 一轮的 tick 主线程上。
        """
        if self._copy_worker is None:
            self._copy_worker = copyworker.CopyWorker(self.notifier)
        job = copyworker.BuyJob(
            network_id=cand.network_id, token_address=cand.token_address,
            symbol=(cand.token_symbol or "?").lstrip("$"),
            amount_usd=cfg.amount_usd, dry_run=cfg.dry_run_execute,
            queued_at=time.monotonic(),
        )
        if self._copy_worker.submit(job):
            return
        # ⚠️ 没接下就必须当场记 failed 并说出来。留在 auto_queued 的话,
        #    这个币因为主键冲突再也不会被跟 —— 而没有任何人知道它丢了。
        store.set_copy_status(conn, cand.network_id, cand.token_address, "failed",
                              "买入队列满,未提交", expect=copyworker.ST_QUEUED)
        self.notifier.send(
            f"⌛ <b>未提交</b> · 自动跟单 · ${html.escape(cand.token_symbol or '?')}\n"
            f"买入队列已满(前面还有单子在跑),这一单直接放弃 —— 晚几分钟买进去是另一笔交易"
        )

    def _send_copy_signal(self, cand: Candidate, d, cfg, status: str) -> None:
        """
        推一条跟单信号。pending 状态附确认按钮,纸上模式不附。

        ⚠️ callback_data 上限 64 字节,而 Solana 的 CA 就有 44 个字符 ——
           这里用 `网络:CA前12位` 当键(bot 侧用同样的表达式反查)。
           12 位前缀在一个几百币的库里碰撞概率可以忽略,而且反查还带网络限定。
        ⚠️ 发送失败不能上抛 —— 台账已经记了,推送只是通知。
        """
        buttons = None
        if status == "pending":
            key = f"{cand.network_id}:{cand.token_address[:12]}"
            buttons = [(f"✅ 买入 ${cfg.amount_usd:,.0f}", f"buy:{key}"),
                       ("🚫 忽略", f"skip:{key}")]
        try:
            self.notifier.send(render_copy_signal(cand, d, cfg, status), buttons=buttons)
        except Exception as e:  # noqa: BLE001
            logger.error("跟单信号推送失败(台账已记录): {}", e)

    def _throttle_send(self) -> None:
        """
        推送限速:令牌桶。桶里有令牌就立刻发,没有就等攒出一个。

        ⚠️ 桶**跨 tick 保持**,不是每轮重置 —— 否则每轮都能突发 _SEND_BURST 条,
           连续几轮爆量时实际速率会超过 TG 的软限,反而挨 429。
        ⚠️ 这段 sleep 发生在 tick 内部,直接计入单轮耗时。原来固定 sleep(3.5s)
           的写法下,一轮 6 条要凭空多花 17.5s —— 那正是日志里
           「新事件 6 条 · 耗时 67s」比「新事件 1 条 · 耗时 35s」多出来的那一截。
        """
        rate = self.settings.fomo_send_interval_sec
        if rate <= 0:
            return
        now = time.monotonic()
        self._send_tokens = min(float(_SEND_BURST),
                                self._send_tokens + (now - self._send_last) / rate)
        self._send_last = now
        if self._send_tokens >= 1.0:
            self._send_tokens -= 1.0
            return
        # ⚠️ 可打断:积压几十条时这里会连着等好几十秒,Ctrl+C 得能立刻停下
        if sleep_or_stop((1.0 - self._send_tokens) * rate):
            self._send_tokens = 0.0
            return
        self._send_tokens = 0.0
        self._send_last = time.monotonic()

    # --------------------------------------------------------
    # 历史基线(seeding)
    # --------------------------------------------------------
    def seed_next_pending_user(self, dry_run: bool = False) -> None:
        """
        为一个 stats_ready=0 的用户建立功能 A/B 的基线。每 tick 最多一个 ——
        批量 /add 10 人 = 10 个 tick 内全部就绪,期间照常推送、只是不打徽章。

        ⚠️ **只写 user_token_stats,绝不写 fomo_events**(Q2 决策)。
           "不推历史"由 fomo_cursors 单独保证,不引入第二套抑制机制 ——
           多套抑制机制的优先级极易写错,写错一次就是把三年历史全推给用户。
        ⚠️ dry_run 必须透传到这里。这是本函数唯一会发 TG 的地方,
           漏传会让 --dry-run 名不副实(帮助文案写的是"只打印不推送")。
        """
        with store.get_conn() as conn:
            u = store.pick_one_pending_user(conn)
        if not u:
            return

        uid = u["user_id"]
        handle = _row_get(u, "display_name") or _row_get(u, "handle") or uid
        max_items = self.settings.fomo_backfill_max_items
        logger.info("开始为 {} 建立历史基线(回填上限 {} 条)", handle, max_items)

        try:
            # 1) 分页拉历史买入,内存里聚合成 (net, ca) -> [笔数, 最早时间]
            agg: dict[tuple[str, str], list] = {}
            scanned = 0
            for raw in self.client.iter_swap_buys(uid, max_items=max_items):
                scanned += 1
                # 逐条归一化(而不是攒齐 500 条再一次性处理):内存占用恒定,
                # 且这里产生的 event_id 根本不落库,重复与否无所谓 —— 只取聚合键与时间
                for ev in self.normalize_swaps(u, [raw], guard=False):
                    # countable_buy 一次性挡掉:非买入 / 方向不明 / 无聚合键 / 计价币
                    if not ev.countable_buy:
                        continue
                    net, ca = ev.token_key
                    # 兜底时间戳(= 拉取时刻)绝不能当作历史买入时间写进 first_buy_at,
                    # 否则一个三年前的老仓位会显示成"今天首次买入"
                    ts = None if ev.ts_fallback else ev.event_ts
                    slot = agg.get((net, ca))
                    if slot is None:
                        agg[(net, ca)] = [1, ts]
                    else:
                        slot[0] += 1
                        if ts and (slot[1] is None or ts < slot[1]):
                            slot[1] = ts

            # 2) balances 全量:兜住回填窗口外的老仓位(A-2)。
            #    这一步失败必须让整个 seeding 失败重试 —— 少了它,窗口外的老仓位
            #    会在下次加仓时被误标 🌱,而徽章落库即冻结、错了就是永久的
            balances = self.client.get_balances(uid) or []

            with store.get_conn() as conn, store.tx(conn):
                for (net, ca), (cnt, first_ts) in agg.items():
                    store.upsert_seed(conn, uid, net, ca, buy_count=cnt, first_buy_at=first_ts)
                for b in balances:
                    if not isinstance(b, dict):
                        continue
                    net, ca = _balance_key(b)
                    if not net or not ca or is_quote_token(net, ca):
                        continue
                    if not _balance_is_held(b):
                        continue
                    # 当前持有 = 至少买过一次(时间未知)。INSERT OR IGNORE 保证不覆盖上一步的真实笔数
                    store.seed_holding(conn, uid, net, ca)
                store.mark_stats_ready(conn, uid)

            with store.get_conn() as conn:
                total = store.stats_row_count(conn, uid)
            # 分页疑似失效时**绝不能报"基线完成"** —— 那是明确的成功确认,
            # 而实际只回填了第一页。用户看到"完成"就不会再查,
            # 之后每一次误标的 🌱 都无从解释(徽章落库即冻结、永不重算)。
            paged_ok = getattr(self.client, "_last_paging_ok", True)
            logger.info("{} 基线{} · 扫描 {} 条 · {} 个代币",
                        handle, "完成" if paged_ok else "部分完成(分页受限)", scanned, total)
            if not dry_run:
                # ⚠️ handle 来自 API 透传的 display_name,是用户可控文本 —— 必须转义。
                #    昵称里一个裸 '<' 就让这条回执 400(§10.4 铁律 4)。
                name = html.escape(str(handle))
                if paged_ok:
                    self.notifier.send(f"✅ <b>{name}</b> 基线完成 · {total} 个代币")
                else:
                    self.notifier.send(
                        f"⚠️ <b>{name}</b> 基线**部分完成** · {total} 个代币\n"
                        f"只回填了最近 {scanned} 笔交易(分页受限),更早的仓位靠当前持仓兜底。\n"
                        f"极早期买过又清仓的币可能被误标为「首次建仓」。"
                    )

        except AuthError:
            # 登录态失效要上抛让调度器停轮询,不能在这里降级成一句 warning
            raise
        except NotSupportedError:
            # PlaywrightFomoClient 翻不了页 → 基线不可信 → stats_ready 保持 0
            # → 功能 A/B 整体降级为不显示,**主推送完全不受影响**
            logger.warning("client 不支持 swaps 分页,{} 的功能 A/B 将保持降级(stats_ready=0)", uid)
        except Exception as e:  # noqa: BLE001
            logger.warning("基线建立失败 user={} err={},下一 tick 重试", uid, e)

    # --------------------------------------------------------
    # 归一化:批级健康检查
    # --------------------------------------------------------
    def _guard_ts(self, kind: str, user_row, events: list[FomoEvent]) -> list[FomoEvent]:
        """
        时间戳批级健康检查 —— 三个 normalize_* 的统一出口。

        ⚠️ 这是本项目最危险的一条静默失效链,必须有可观测性:
             probe #3 未实测 → 真实时间字段名不在 _K_TIMESTAMP 候选里(或单位判错被区间断言拒)
             → 每条记录 event_ts 都兜底成 now
             → 全部越过 _drop_before_cursor 的 event_ts > cursor 判据
             → 首个 tick 把每人上百条历史一次性轰进 TG
           而在加这道检查之前,整条链上一句日志都没有。

        刻意**不引入第二套抑制机制**(设计文档明令禁止):
        这里只做两件事 —— 占比过高时丢弃该批并 ERROR,其余情况 WARN 一次。
        丢弃是安全的:游标不前进,下一 tick 会重新拉到同一批。
        """
        if not events:
            return events
        n_fb = sum(1 for e in events if e.ts_fallback)
        if n_fb == 0:
            return events
        uid = user_row["user_id"]
        if len(events) >= _TS_GUARD_MIN_BATCH and n_fb / len(events) > _TS_GUARD_DROP_RATIO:
            logger.error(
                "{} 批 {}/{} 条时间戳解析失败,整批丢弃 —— "
                "极可能是 API 时间字段名或单位变了(probe #3)。"
                "此时游标形同虚设,继续下去会把历史全量推给用户 | user={}",
                kind, n_fb, len(events), uid,
            )
            return []
        logger.warning("{} 批 {}/{} 条时间戳兜底 | user={}", kind, n_fb, len(events), uid)
        return events

    # --------------------------------------------------------
    # 归一化:swaps
    # --------------------------------------------------------
    def normalize_swaps(self, user_row, items: list[dict], *, guard: bool = True) -> list[FomoEvent]:
        """
        swaps 原始记录 → FomoEvent 列表。一条 swap 可能产出 1 条或 2 条事件(见 _swap_to_events)。

        ⚠️ 单条记录解析失败只跳过这一条,绝不让整批炸掉 ——
           字段假设一项都没实测过,一条畸形记录不能带走整个用户的推送。
        """
        out: list[FomoEvent] = []
        # ⚠️ 兜底 event_id 的"页内序号"分量:必须按 (kind, txHash, token, ts, amount)
        #    分组计数,**不能直接用列表下标**。下标会随新记录插到列表头部而整体平移,
        #    同一笔 swap 在下一个 tick 拿到不同的 hash → 同一条消息被推两次。
        #    按分组计数只在"完全等价的等额拆单"内部递增,跨 tick 稳定。
        dup: dict[tuple, int] = {}
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            try:
                out.extend(self._swap_to_events(user_row, raw, dup))
            except Exception as e:  # noqa: BLE001
                logger.warning("swap 解析失败,跳过该条 | user={} err={} raw={}",
                               user_row["user_id"], e, dump_raw(raw)[:300])
        # seeding 逐条调用时 guard=False:一条一批统计不出什么,
        # 500 条各刷一次 WARN 反而把日志淹了 —— 而且 seeding 不写事件表,没有历史轰炸风险
        return self._guard_ts("swaps", user_row, out) if guard else out

    def _swap_to_events(self, user_row, raw: dict, dup: dict) -> list[FomoEvent]:
        """
        方向判定的优先级(设计文档 §6 的建模澄清):
          1) 两侧代币都解析出来了 → 用**计价币位置**结构性判定:
             计价币 → 非计价币 = 买入;反之 = 卖出;两侧都非计价币 = 卖 A + 买 B 两条事件
          2) 只解析出一侧 → 用它所处的位置判(收到的一侧 = 买入,付出的一侧 = 卖出)
          3) 一侧都没有 → 用显式 side 字段
          4) 都拿不到 → side_unknown=True:照常落库照常推送,但不写 stats、不打徽章、不显示共识
        """
        leg_in = _extract_leg(raw, _K_LEG_IN, _P_LEG_IN)
        leg_out = _extract_leg(raw, _K_LEG_OUT, _P_LEG_OUT)
        in_net, in_ca, in_sym = _leg_token(raw, leg_in)
        out_net, out_ca, out_sym = _leg_token(raw, leg_out)

        # 只有拿到地址的一侧才算"解析出来了" —— 没有地址就判不了是不是计价币,
        # 硬当成非计价币会凭空造出一条卖出消息
        has_in = bool(in_ca)
        has_out = bool(out_ca)

        if has_in and has_out:
            in_quote = is_quote_token(in_net, in_ca)
            out_quote = is_quote_token(out_net, out_ca)
            if in_quote and not out_quote:
                return [self._make_swap_event(user_row, raw, dup, EVENT_BUY, leg_out,
                                              out_net, out_ca, out_sym)]
            if out_quote and not in_quote:
                return [self._make_swap_event(user_row, raw, dup, EVENT_SELL, leg_in,
                                              in_net, in_ca, in_sym)]
            if not in_quote and not out_quote:
                # 币币互换:两侧都是标的,产出两条事件(卖 A + 买 B)
                return [
                    self._make_swap_event(user_row, raw, dup, EVENT_SELL, leg_in,
                                          in_net, in_ca, in_sym),
                    self._make_swap_event(user_row, raw, dup, EVENT_BUY, leg_out,
                                          out_net, out_ca, out_sym),
                ]
            # 两侧皆计价币(USDT→USDC、SOL→USDC 等):产出一条并落库,但**不推送**。
            # 稳定币互换既不是建仓也不是离场,"🟢 加仓 $USDC"这行字零信号价值、纯占屏;
            # 落库是为了保留"他当时是不是在备钱"的回溯能力。
            ev = self._make_swap_event(user_row, raw, dup, EVENT_BUY, leg_out,
                                       out_net, out_ca, out_sym)
            ev.quote_only = True
            return [ev]

        if has_out:
            return [self._make_swap_event(user_row, raw, dup, EVENT_BUY, leg_out,
                                          out_net, out_ca, out_sym)]
        if has_in:
            return [self._make_swap_event(user_row, raw, dup, EVENT_SELL, leg_in,
                                          in_net, in_ca, in_sym)]

        # 单侧扁平记录:代币字段直接挂在记录上,方向只能靠显式字段
        net = normalize_network(_pick_str(raw, *_K_NETWORK))
        ca = normalize_token_address(_pick_str(raw, *_K_TOKEN_ADDR))
        sym = _pick_str(raw, *_K_TOKEN_SYMBOL)
        # 地址和 symbol 都拿不到 = 这条记录里没有任何可展示的标的,等同于解析失败。
        # 这不是"过滤"(全推原则针对的是能识别的事件),而是不发一条空壳消息 ——
        # 一条既没有币名也没有地址的"交易"对用户零价值,只会污染扫描色带
        if not ca and not sym:
            logger.warning("swap 无任何代币标识,跳过该条 | user={} raw={}",
                           user_row["user_id"], dump_raw(raw)[:300])
            return []
        side = _side_of(_pick_str(raw, *_K_SIDE))
        if side is None:
            logger.warning("swap 方向判不出,按 side_unknown 降级 | user={} raw={}",
                           user_row["user_id"], dump_raw(raw)[:300])
        return [self._make_swap_event(user_row, raw, dup, side or EVENT_BUY, None,
                                      net, ca, sym, side_unknown=side is None)]

    def _make_swap_event(self, user_row, raw: dict, dup: dict, event_type: str,
                         leg: dict | None, net: str | None, ca: str | None, sym: str | None,
                         side_unknown: bool = False) -> FomoEvent:
        src = leg or raw
        event_ts, ts_fallback = _resolve_ts(raw, leg)
        tx_hash = _pick_str(raw, *_K_TX_HASH)
        amount = _s(pick(src, *_K_TOKEN_AMOUNT)) or _s(pick(raw, *_K_TOKEN_AMOUNT))

        # 见 normalize_swaps 里对 dup 的说明:跨 tick 稳定的"等额拆单序号"
        dkey = (event_type, tx_hash or "", ca or "", event_ts, amount or "")
        seq = dup.get(dkey, 0)
        dup[dkey] = seq + 1

        # swap 记录不含 symbol / 市值 / 持仓 / 均价 —— 从本 tick 的 balances 索引里补。
        # 索引里没有(如已清仓的币)时保持 None,formatter 会让对应行整行消失。
        meta = self._token_meta.get((net, ca)) or {}
        posn = self._positions.get((user_row["user_id"], net, ca)) or {}

        native_id = _pick_str(raw, *_K_NATIVE_ID)
        # 一条双侧 swap 产出两条事件时,原生 id 相同 —— make_event_id 会加 kind 前缀
        # ("BUY:xxx" / "SELL:xxx")天然区分开,不需要额外后缀
        return FomoEvent(
            event_id=make_event_id(
                event_type, native_id,
                user_id=user_row["user_id"], tx_hash=tx_hash, token_address=ca,
                event_ts=event_ts, amount=amount, page_index=seq,
            ),
            event_type=event_type,
            user_id=user_row["user_id"],
            event_ts=event_ts,
            raw_json=dump_raw(raw),
            handle=_row_get(user_row, "display_name") or _row_get(user_row, "handle"),
            user_handle=_row_get(user_row, "handle"),
            network_id=net,
            token_address=ca,
            token_symbol=_clean_symbol(sym) or meta.get("symbol"),
            # ⚠️ 实测记录里 USD 金额是分侧的:humanUsdAmountIn / humanUsdAmountOut。
            #    要取**标的那一侧**的值:买入时标的在 out 侧,卖出时在 in 侧。
            #    跨链 swap 两侧数值会有细微差(手续费/滑点),取错侧显示的就不是这笔的成交额。
            amount_usd=(
                _f(pick(src, *_K_AMOUNT_USD))
                or _f(pick(raw, *(_USD_OUT_FIRST if event_type == EVENT_BUY else _USD_IN_FIRST)))
                or _f(pick(raw, *_K_AMOUNT_USD))
            ),
            token_amount=amount,
            price_usd=(_f(pick(src, *_K_PRICE_USD)) or _f(pick(raw, *_K_PRICE_USD))
                       or meta.get("price_usd")),
            tx_hash=tx_hash,
            ts_fallback=ts_fallback,
            side_unknown=side_unknown,
            api_trade_count=_i(pick(raw, *_K_TRADE_COUNT)),
            # 下面四项 swap 记录里一个都没有,全部来自 balances 索引(见 _build_token_index)。
            # 索引里也没有(如已清仓的币)时保持 None → formatter 让对应行整行消失。
            holding_usd=_f(pick(raw, *_K_HOLDING_USD)) or posn.get("holding_usd"),
            avg_price=(_f(pick(raw, *_K_AVG_PRICE)) or _f(pick(src, *_K_AVG_PRICE))
                       or posn.get("avg_price")),
            market_cap=(_f(pick(src, *_K_MARKET_CAP)) or _f(pick(raw, *_K_MARKET_CAP))
                        or meta.get("market_cap")),
            token_created_at=meta.get("created_at"),
            unrealized_pnl=_f(pick(raw, *_K_PNL_USD)) or posn.get("pnl"),
            unrealized_pnl_pct=_f(pick(raw, *_K_PNL_PCT)) or posn.get("pnl_pct"),
            # 已实现盈亏只对卖出有意义,来自 /trades(balances 里没有)
            realized_pnl=posn.get("realized_pnl"),
            realized_pnl_pct=posn.get("realized_pnl_pct"),
        )

    # --------------------------------------------------------
    # 归一化:transfers
    # --------------------------------------------------------
    def normalize_transfers(self, user_row, items: list[dict]) -> list[FomoEvent]:
        """
        转入 / 转出。

        ⚠️ TRANSFER_IN/OUT **绝不改 buy_count**(B-8):空投、领奖、内部划转都会造假买入,
           徽章会打在一笔没花钱的仓位上。这一点由 store.should_count 强制(只认 BUY),
           这里不需要也不允许做任何"看起来像买入"的推断。
        """
        out: list[FomoEvent] = []
        dup: dict[tuple, int] = {}
        uid = user_row["user_id"]
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            try:
                ev = self._transfer_to_event(user_row, raw, dup)
                if ev is not None:
                    out.append(ev)
            except Exception as e:  # noqa: BLE001
                logger.warning("transfer 解析失败,跳过该条 | user={} err={} raw={}",
                               uid, e, dump_raw(raw)[:300])
        return self._guard_ts("transfers", user_row, out)

    def _transfer_to_event(self, user_row, raw: dict, dup: dict) -> FomoEvent | None:
        uid = user_row["user_id"]
        nested = raw.get("token") if isinstance(raw.get("token"), dict) else None
        src = nested or raw
        net = normalize_network(_pick_str(src, *_K_NETWORK) or _pick_str(raw, *_K_NETWORK))
        ca = normalize_token_address(_pick_str(src, *_K_TOKEN_ADDR) or _pick_str(raw, *_K_TOKEN_ADDR))
        sym = _pick_str(src, *_K_TOKEN_SYMBOL) or _pick_str(raw, *_K_TOKEN_SYMBOL)
        # 没有任何代币标识 = 无从渲染,等同于解析失败(理由同 _swap_to_events)
        if not ca and not sym:
            logger.warning("transfer 无任何代币标识,跳过该条 | user={} raw={}", uid, dump_raw(raw)[:300])
            return None

        # 方向:优先显式字段;拿不到就比对收发双方的 userId 是不是本人
        direction = _direction_of(_pick_str(raw, *_K_TRANSFER_DIR))
        side_unknown = False
        if direction is None:
            from_uid = _pick_str(raw, *_K_FROM_UID)
            to_uid = _pick_str(raw, *_K_TO_UID)
            if to_uid and to_uid == uid:
                direction = EVENT_TRANSFER_IN
            elif from_uid and from_uid == uid:
                direction = EVENT_TRANSFER_OUT
        if direction is None:
            # ⚠️ 方向判不出时不能猜:把转出渲染成"收到转入"是彻底的错误信息。
            #    退化为 side_unknown,由 formatter 走中性文案(§9 降级矩阵:标题写"交易")
            side_unknown = True
            direction = EVENT_TRANSFER_IN
            logger.warning("transfer 方向判不出,按 side_unknown 降级 | user={} raw={}",
                           uid, dump_raw(raw)[:300])

        event_ts, ts_fallback = _resolve_ts(raw, nested)
        tx_hash = _pick_str(raw, *_K_TX_HASH)
        amount = _s(pick(raw, *_K_TOKEN_AMOUNT)) or _s(pick(src, *_K_TOKEN_AMOUNT))
        dkey = (direction, tx_hash or "", ca or "", event_ts, amount or "")
        seq = dup.get(dkey, 0)
        dup[dkey] = seq + 1

        cp_handle = _counterparty_handle(raw)
        cp_id = _pick_str(raw, *_K_FROM_UID) if direction == EVENT_TRANSFER_IN else _pick_str(raw, *_K_TO_UID)
        # B-9:名单内部转账必须标出来,否则用户无法辨别筹码是不是在名单内搬家
        cp_watched = bool(
            (cp_id and cp_id in self._watched_ids)
            or (cp_handle and cp_handle.strip().lstrip("@").lower() in self._watched_handles)
        )

        return FomoEvent(
            event_id=make_event_id(
                direction, _pick_str(raw, *_K_NATIVE_ID),
                user_id=uid, tx_hash=tx_hash, token_address=ca,
                event_ts=event_ts, amount=amount, page_index=seq,
            ),
            event_type=direction,
            user_id=uid,
            event_ts=event_ts,
            raw_json=dump_raw(raw),
            handle=_row_get(user_row, "display_name") or _row_get(user_row, "handle"),
            user_handle=_row_get(user_row, "handle"),
            network_id=net,
            token_address=ca,
            token_symbol=_clean_symbol(sym),
            amount_usd=_f(pick(raw, *_K_AMOUNT_USD)) or _f(pick(src, *_K_AMOUNT_USD)),
            token_amount=amount,
            price_usd=_f(pick(raw, *_K_PRICE_USD)) or _f(pick(src, *_K_PRICE_USD)),
            tx_hash=tx_hash,
            ts_fallback=ts_fallback,
            side_unknown=side_unknown,
            holding_usd=_f(pick(raw, *_K_HOLDING_USD)),
            market_cap=_f(pick(src, *_K_MARKET_CAP)) or _f(pick(raw, *_K_MARKET_CAP)),
            counterparty_handle=cp_handle,
            counterparty_is_watched=cp_watched,
        )

    # --------------------------------------------------------
    # 归一化:thesis(FOMO 内部把"观点"叫 thesis)
    # --------------------------------------------------------
    def normalize_thesis(self, user_row, items: list[dict]) -> list[FomoEvent]:
        out: list[FomoEvent] = []
        dup: dict[tuple, int] = {}
        uid = user_row["user_id"]
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            try:
                ev = self._thesis_to_event(user_row, raw, dup)
                if ev is not None:
                    out.append(ev)
            except Exception as e:  # noqa: BLE001
                logger.warning("thesis 解析失败,跳过该条 | user={} err={} raw={}",
                               uid, e, dump_raw(raw)[:300])
        return self._guard_ts("thesis", user_row, out)

    def _thesis_to_event(self, user_row, raw: dict, dup: dict) -> FomoEvent | None:
        uid = user_row["user_id"]
        # ⚠️ 实测结构:正文与代币标识都在嵌套的 comment 里,持仓与盈亏在 authorTrade 里。
        #      {"id":…, "createdAt":…, "userId":…,
        #       "comment": {"comment":"正文", "tokenAddress":…, "networkId":…},
        #       "authorTrade": {"usdValue":…, "unrealizedPnlUsd":…, "percentageUnrealizedPnl":…}}
        #    顶层 _K_THESIS_TEXT 里的 "comment" 命中的是**字典**而不是正文字符串,
        #    所以这里必须先显式下钻,不能只靠通用候选键。
        cmt = raw.get("comment") if isinstance(raw.get("comment"), dict) else None
        trade = raw.get("authorTrade") if isinstance(raw.get("authorTrade"), dict) else {}
        nested = cmt or (raw.get("token") if isinstance(raw.get("token"), dict) else None)
        src = nested or raw
        net = normalize_network(_pick_str(src, *_K_NETWORK) or _pick_str(raw, *_K_NETWORK))
        ca = normalize_token_address(_pick_str(src, *_K_TOKEN_ADDR) or _pick_str(raw, *_K_TOKEN_ADDR))
        sym = _pick_str(src, *_K_TOKEN_SYMBOL) or _pick_str(raw, *_K_TOKEN_SYMBOL)
        # 正文:先取 comment.comment,再退回顶层通用候选
        text = (_pick_str(cmt or {}, "comment", "text", "content", "body")
                or _pick_str(raw, *_K_THESIS_TEXT))
        # 观点的主体就是正文。正文和代币都拿不到时这条消息是纯空壳,不如不发
        if not text and not ca and not sym:
            logger.warning("thesis 无正文也无代币标识,跳过该条 | user={} raw={}", uid, dump_raw(raw)[:300])
            return None

        event_ts, ts_fallback = _resolve_ts(raw, nested)
        # thesis 没有 txHash,兜底 hash 用正文前 64 字符参与 —— 同一个币同一时刻的两条不同观点
        # 靠它才不会被误去重
        digest = (text or "")[:64]
        dkey = (ca or "", event_ts, digest)
        seq = dup.get(dkey, 0)
        dup[dkey] = seq + 1

        return FomoEvent(
            event_id=make_event_id(
                EVENT_THESIS, _pick_str(raw, *_K_NATIVE_ID),
                user_id=uid, tx_hash=None, token_address=ca,
                event_ts=event_ts, amount=digest, page_index=seq,
            ),
            event_type=EVENT_THESIS,
            user_id=uid,
            event_ts=event_ts,
            raw_json=dump_raw(raw),
            handle=_row_get(user_row, "display_name") or _row_get(user_row, "handle"),
            user_handle=_row_get(user_row, "handle"),
            network_id=net,
            token_address=ca,
            token_symbol=_clean_symbol(sym) or (self._token_meta.get((net, ca)) or {}).get("symbol"),
            ts_fallback=ts_fallback,
            # 持仓与盈亏来自 authorTrade(实测字段名),拿不到再退回 balances 索引
            holding_usd=(_f(trade.get("usdValue")) or _f(pick(raw, *_K_HOLDING_USD))
                         or (self._positions.get((uid, net, ca)) or {}).get("holding_usd")),
            unrealized_pnl=_f(trade.get("unrealizedPnlUsd")),
            unrealized_pnl_pct=_f(trade.get("percentageUnrealizedPnl")),
            market_cap=(_f(pick(src, *_K_MARKET_CAP)) or _f(pick(raw, *_K_MARKET_CAP))
                        or (self._token_meta.get((net, ca)) or {}).get("market_cap")),
            token_created_at=(self._token_meta.get((net, ca)) or {}).get("created_at"),
            token_amount=_s(trade.get("humanTokenAmount")),
            thesis_text=text,
        )

    # --------------------------------------------------------
    # 调度
    # --------------------------------------------------------
    # ⚠️ 这里**刻意不提供 run_forever**。调度器只有一份,在 cli.cmd_run 里 ——
    #    那份额外做了两件本类不该管的事:
    #      1) AuthError 时发 TG 告警并 shutdown 调度器(设计 §3.5)
    #      2) 回填 last_tick_at,/status 读的就是它
    #    曾经两边各写过一套,参数还不一致(misfire_grace_time 一个 None 一个 interval),
    #    而 Poller 那份是死代码 —— 谁哪天改用它,/status 的"最近 tick"会静默不再前进。
    #    调度只留一个入口,不要再在这里加第二份。


# ============================================================
# swaps 字段解析辅助
# ============================================================
def _extract_leg(raw: dict, nested_keys: tuple[str, ...], flat_prefixes: tuple[str, ...]) -> dict | None:
    """
    取出 swap 的一侧(in / out)。

    TODO(probe #4): 两种形态都可能:嵌套对象 {"tokenIn": {...}} 或平铺
                    {"tokenInAddress": ..., "tokenInSymbol": ...}。probe 确认后可删掉另一支。
    """
    for k in nested_keys:
        v = raw.get(k)
        if isinstance(v, dict) and v:
            return v
    for p in flat_prefixes:
        leg: dict = {}
        # 顺序即优先级,先命中的不被后面覆盖(setdefault)。
        # ⚠️ 前两项是 2026-08-11 实测确认的真实字段名:
        #      inTokenAddress / outTokenAddress、inHumanAmount / outHumanAmount
        #    原来只拼 p+"Address"(= inAddress),真实记录里没有这个键,
        #    于是两侧都取不到 → 走单侧降级分支 → 方向判不出 → 所有买卖都成了 side_unknown。
        for suffix, target in (
            ("TokenAddress", "address"), ("Address", "address"), ("Mint", "mint"),
            ("TokenSymbol", "symbol"), ("Symbol", "symbol"),
            ("HumanAmount", "amount"), ("Amount", "amount"),
            ("NetworkId", "networkId"), ("ChainId", "chainId"),
            ("AmountUsd", "amountUsd"), ("Price", "price"),
        ):
            val = raw.get(p + suffix)
            if val is not None and not isinstance(val, (dict, list)):
                leg.setdefault(target, val)
        if leg.get("address"):
            # 只有拿到地址才算解析出这一侧 —— 光有 amount 判不了是不是计价币
            return leg
    return None


def _leg_token(raw: dict, leg: dict | None) -> tuple[str | None, str | None, str | None]:
    """
    从一侧里取 (network, address, symbol)。

    链字段通常挂在记录级而不是 leg 级,所以 leg 取不到时回落到 raw ——
    ⚠️ 但地址**绝不能**回落到 raw:那会让 in / out 两侧拿到同一个地址,
       直接产出两条同币的买卖事件。
    """
    if leg is None:
        return None, None, None
    net = normalize_network(_pick_str(leg, *_K_NETWORK) or _pick_str(raw, *_K_NETWORK))
    ca = normalize_token_address(_pick_str(leg, *_K_TOKEN_ADDR))
    sym = _pick_str(leg, *_K_TOKEN_SYMBOL)
    return net, ca, sym


def _side_of(v: str | None) -> str | None:
    """显式方向字段 → BUY / SELL;认不出返回 None(调用方走 side_unknown 降级)"""
    if not v:
        return None
    k = v.strip().lower().replace("-", "_")
    if k in _SIDE_BUY:
        return EVENT_BUY
    if k in _SIDE_SELL:
        return EVENT_SELL
    return None


def _direction_of(v: str | None) -> str | None:
    """转账方向字段 → TRANSFER_IN / TRANSFER_OUT;认不出返回 None"""
    if not v:
        return None
    k = v.strip().lower().replace("-", "_")
    if k in _DIR_IN:
        return EVENT_TRANSFER_IN
    if k in _DIR_OUT:
        return EVENT_TRANSFER_OUT
    return None


def _resolve_ts(raw: dict, nested: dict | None = None) -> tuple[str, bool]:
    """
    时间戳统一入口,返回 (iso, is_fallback)。

    ⚠️ event_ts 是 NOT NULL 且是时序比较的唯一基准,拿不到时必须用当前时刻兜底,
       同时置 ts_fallback —— 兜底值绝不能写进 first_buy_at,否则老仓位会显示成"今天首次买入"。
    """
    for source in (raw, nested or {}):
        for key in _K_TIMESTAMP:
            if key not in source or source[key] is None:
                continue
            iso, fallback = to_iso(source[key])
            if iso and not fallback:
                return iso, False
    return now_iso(), True


def _clean_symbol(sym: str | None) -> str | None:
    """去掉 API 可能自带的 $ 前缀 —— formatter 会自己加,不去掉会渲染成 $$TOAD"""
    if not sym:
        return None
    return sym.strip().lstrip("$").strip() or None
