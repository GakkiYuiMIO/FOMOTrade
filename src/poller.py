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

from loguru import logger

from src import store
from src.auth import AuthError
from src.client import NotSupportedError
from src.config import get_settings
from src.formatter import render
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
# 20s 一轮 × 25 个 = 一分钟覆盖 75 个币,对几十人的名单足够。
_THESIS_TOKENS_PER_TICK = 25
# afterTime 回看窗口(秒)。够覆盖轮转一圈的时间即可 —— 拉太久白费流量,
# 拉太短会在轮转间隙漏掉观点。精确去重由 event_id + 游标负责,这里只是压 payload。
_THESIS_LOOKBACK_SEC = 3600


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

            # --- 2) 采集 ---
            snapshots = self._fetch_snapshots(users)
            # 先建代币/持仓索引:归一化时要用它补 symbol、市值、持仓、均价
            self._build_token_index(snapshots)

            # --- 3) 归一化 + 游标过滤 ---
            events = self._collect_events(conn, users, snapshots)
            # 观点单独走一条路:它按代币查,不按用户查(见 _collect_thesis)
            try:
                events.extend(self._collect_thesis(conn, users))
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

        return len(new_events)

    def _refresh_watched_index(self, users) -> None:
        self._watched_ids = {u["user_id"] for u in users}
        self._watched_handles = {h for h in (_row_get(u, "handle") for u in users) if h}

    def _fetch_snapshots(self, users) -> dict:
        """
        逐个用户拉快照。单个用户失败置 None ——
        一个人的网络抖动不能让整个名单的推送停摆(count_holders 会据此整段降级)。
        """
        snapshots: dict = {}
        for u in users:
            uid = u["user_id"]
            try:
                snapshots[uid] = self.client.fetch_snapshot(uid)
            except AuthError:
                # ⚠️ 必须上抛,绝不能被下面的裸 except 吞掉。
                #    登录态失效是"整个管道都废了",不是"某个用户拉取失败" ——
                #    吞掉的后果是程序每 20 秒空转一次只刷 ERROR 日志,
                #    而 §3.5 要求的那条「🔐 登录态失效」TG 告警永远发不出去,
                #    用户会一直以为监控还活着。
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("拉取快照失败 user={} handle={} err={}", uid, _row_get(u, "handle"), e)
                snapshots[uid] = None
        return snapshots

    def _build_token_index(self, snapshots: dict) -> None:
        """
        从本 tick 的 balances 建两张索引,供消息渲染补字段:

            _token_meta[(net, ca)]      → symbol / 市值 / 现价      (全局共享)
            _positions[(uid, net, ca)]  → 持仓额 / 均价 / 未实现盈亏 (按人)

        ⚠️ swap 记录本身**不含** symbol、市值、持仓、均价 —— 消息里那几行全靠这张索引。
           而 balances 每个 tick 本来就要拉(count_holders 依赖它),所以零额外 API 开销。
        ⚠️ 索引里没有的币,对应行整行消失,绝不本地推算(§10.4 铁律 2)。
           清仓后 balances 里就没这个币了,所以卖出消息的持仓行消失是**正确行为**。
        """
        meta: dict[tuple, dict] = {}
        pos: dict[tuple, dict] = {}
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
                # setdefault:同一个币多人持有时以先到的为准,值都一样,不必反复覆盖
                m.setdefault("symbol", _clean_symbol(_pick_str(tok, "symbol")))
                m.setdefault("market_cap", _f(tfr.get("marketCap")))
                m.setdefault("price_usd", price)
                # 拉 thesis 时要用**原始**数字 networkId(1399811149),
                # 不能用归一化后的 "solana" —— 那是我们内部的聚合键,API 不认
                m.setdefault("network_raw", tok.get("networkId"))

                ut = b.get("userToken") if isinstance(b.get("userToken"), dict) else None
                if not ut:
                    continue
                cost = _f(ut.get("currentCostBasisUsd"))
                holding = _balance_usd(b)
                pnl = (holding - cost) if (holding is not None and cost is not None) else None
                pos[(uid, net, ca)] = {
                    "holding_usd": holding,
                    "avg_price": _f(ut.get("averageEntryPriceUsd")),
                    "pnl": pnl,
                    "pnl_pct": (pnl / cost * 100) if (pnl is not None and cost) else None,
                }
        self._token_meta = meta
        self._positions = pos
        logger.debug("代币索引 {} 个 · 持仓索引 {} 条", len(meta), len(pos))

    def _collect_thesis(self, conn, users) -> list[FomoEvent]:
        """
        观点采集:遍历监控用户持仓里的币 → 按币拉 thesis → 按 userId 过滤出监控对象。

        ⚠️ FOMO **没有**"按用户查观点"的端点(/feed/user/thesis 是 404),只能这么绕。
           直接代价是调用量 = 持仓币数,所以做了三件事压住它:
             1) 跨用户去重 —— 多人持有同一个币只拉一次(_token_meta 天然按币聚合)
             2) 每 tick 最多拉 _THESIS_TOKENS_PER_TICK 个,**轮转覆盖**,
                币多时延迟变长但调用量恒定,不会随名单规模爆炸
             3) 带 afterTime 增量拉(单位**毫秒**,传秒会被服务端忽略)
        ⚠️ 只能看到"监控用户当前持仓的币"下的观点。他对已清仓的币发的观点看不到 ——
           这是本方案的已知盲区,写在这里免得日后当成 bug 查。
        """
        tokens = [k for k, v in self._token_meta.items() if v.get("network_raw") is not None]
        if not tokens or not self._watched_ids:
            return []
        tokens.sort()                      # 固定顺序,轮转才有意义
        n = len(tokens)
        take = min(_THESIS_TOKENS_PER_TICK, n)
        start = self._thesis_rr % n
        batch = [tokens[(start + i) % n] for i in range(take)]
        self._thesis_rr = (start + take) % n

        after_ms = int((time.time() - _THESIS_LOOKBACK_SEC) * 1000)
        by_user: dict[str, list[dict]] = {}
        failed = 0
        for net, ca in batch:
            raw_net = (self._token_meta.get((net, ca)) or {}).get("network_raw")
            try:
                items = self.client.get_token_thesis(ca, raw_net, after_ms=after_ms)
            except AuthError:
                raise                       # 登录态问题是全局的,必须上抛
            except Exception as e:          # noqa: BLE001
                failed += 1
                logger.debug("thesis 拉取失败 token={} err={}", ca[:16], e)
                continue
            for it in items:
                if isinstance(it, dict) and it.get("userId") in self._watched_ids:
                    by_user.setdefault(it["userId"], []).append(it)

        if failed:
            logger.warning("thesis 本轮 {}/{} 个代币拉取失败", failed, len(batch))
        if not by_user:
            return []

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
                    # None = 该项拉取失败(区别于空列表)。swaps 挂了不影响其余三类继续推
                    logger.warning("{} 的 {} 本 tick 拉取失败,本类跳过", uid, kind)
                    continue
                try:
                    evs = fn(u, items)
                except Exception as e:  # noqa: BLE001
                    logger.error("归一化 {} 整批失败 user={} err={}", kind, uid, e)
                    continue
                events.extend(_drop_before_cursor(evs, store.get_cursor(conn, uid, kind)))
        return events

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
        interval = self.settings.fomo_send_interval_sec
        first = True
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
                )
            except Exception as e:  # noqa: BLE001
                # 渲染炸了只丢这一条,后面的照发。sent 保持 0,下一 tick 会再试
                logger.error("渲染失败,跳过该条 | {} | {}", ev.event_id, e)
                continue

            if dry_run:
                logger.info("[dry-run] {}", text.replace("\n", " ⏎ "))
                continue

            # 串行 + 固定间隔,规避 TG 同 chat 约 20 msg/min 的限流。第一条不等
            if not first:
                time.sleep(interval)
            first = False

            try:
                ok = self.notifier.send(text)
            except Exception as e:  # noqa: BLE001
                logger.error("推送异常 | {} | {}", ev.event_id, e)
                continue
            if ok:
                # ⚠️ 只能在 TG 确认收到之后才置 sent=1。"发之前就标已发"会在崩溃时永久丢消息
                store.mark_sent(conn, ev.event_id, buyers, watchlist)

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
            logger.info("{} 基线完成 · 扫描 {} 条 · {} 个代币", handle, scanned, total)
            if not dry_run:
                # ⚠️ handle 来自 API 透传的 display_name,是用户可控文本 —— 必须转义。
                #    昵称里一个裸 '<' 就让这条回执 400(§10.4 铁律 4)。
                self.notifier.send(
                    f"✅ <b>{html.escape(str(handle))}</b> 基线完成 · {total} 个代币"
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
            unrealized_pnl=_f(pick(raw, *_K_PNL_USD)) or posn.get("pnl"),
            unrealized_pnl_pct=_f(pick(raw, *_K_PNL_PCT)) or posn.get("pnl_pct"),
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
