"""
数据结构与归一化纯函数

本文件是整个项目的共享契约:store / poller / formatter 都依赖这里的 FomoEvent 与常量。
所有函数都是**纯函数**(无 IO、无全局状态),因此可以在拿到真实 API 数据之前就完整单测。

⚠️ 归一化是本项目最容易踩坑的地方 —— 聚合键错一点,共识计数就整体算错。
   三条铁律写在各函数的 docstring 里,改动前务必先读。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# ============================================================
# 事件类型
# ============================================================
EVENT_BUY = "BUY"
EVENT_SELL = "SELL"
EVENT_THESIS = "THESIS"
EVENT_TRANSFER_IN = "TRANSFER_IN"
EVENT_TRANSFER_OUT = "TRANSFER_OUT"

ALL_EVENT_TYPES = (
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
)

# ============================================================
# 徽章(功能 A)
# ============================================================
BADGE_FIRST = "FIRST"   # 🌱 首次建仓
BADGE_ADD = "ADD"       # 🟢 加仓

# badge_reason 取值 —— 排查时靠它区分"真的判过"和"数据不足"
REASON_LOCAL_STATS = "local_stats"      # 正常判定
REASON_API_VETO = "api_veto"            # 被 API 交易次数字段否决(Q3,probe 确认后才启用)
REASON_NOT_BUY = "not_buy"
REASON_NO_SIDE = "no_side"
REASON_NO_TOKEN_KEY = "no_token_key"
REASON_QUOTE_TOKEN = "quote_token"
REASON_NO_BASELINE = "no_baseline"

# 这两个 reason 表示"事件确实是一笔可计数的市场买入",只有它们才写 user_token_stats
COUNTABLE_REASONS = (REASON_LOCAL_STATS, REASON_API_VETO)

# ============================================================
# 链标识归一化
# ============================================================
# ⚠️ probe #10 必须确认 swaps / balances / transfers 三处的实际取值是否一致,
#    不一致会让同一条链裂成两个聚合键,共识计数直接算错。
#    未命中映射表时原样返回小写值(而不是丢弃),这样至少同一来源内部是自洽的。
_NETWORK_ALIASES = {
    "solana": "solana", "sol": "solana", "1399811149": "solana",
    "base": "base", "8453": "base",
    "bsc": "bsc", "bnb": "bsc", "bnb-chain": "bsc",
    "binance-smart-chain": "bsc", "binance": "bsc", "56": "bsc",
    "ethereum": "ethereum", "eth": "ethereum", "1": "ethereum",
    "monad": "monad", "143": "monad",
    "robinhood": "robinhood", "4663": "robinhood",
    "hyperliquid": "hyperliquid", "1337": "hyperliquid",
}

# ============================================================
# 链的展示名与 URL slug(抓自 fomo.family 前端 chains 模块,与官方一致)
# ============================================================
# ⚠️ 键是**我们内部的归一化值**,不是原始 networkId ——
#    内部值一旦改动就会让 user_token_stats 的聚合键裂开,所以这两张表单独维护,
#    不要图省事把内部值直接改成 FOMO 的 slug(bsc vs bnb 就是不一致的一例)。
NETWORK_DISPLAY = {
    "solana": "Solana", "base": "Base", "monad": "Monad", "bsc": "BNB Chain",
    "ethereum": "Ethereum", "hyperliquid": "Hyperliquid", "robinhood": "Robinhood Chain",
}
# fomo.family 代币页的路径片段:https://fomo.family/tokens/{slug}/{address}
NETWORK_SLUG = {
    "solana": "solana", "base": "base", "monad": "monad", "bsc": "bnb",
    "ethereum": "ethereum", "hyperliquid": "hyperliquid", "robinhood": "robinhood",
}
# GMGN 的链片段:https://gmgn.ai/{slug}/token/{address}
# 只收录 GMGN 确实支持的链 —— 拼一个它不支持的链只会得到 404,
# **错的链接比没有链接更糟**(设计 §10.3),所以未收录的链直接不出这个链接。
GMGN_SLUG = {
    "solana": "sol", "base": "base", "bsc": "bsc", "ethereum": "eth",
}

# ============================================================
# 计价币白名单(功能 A/B 的排除规则)
# ============================================================
# ⚠️ 必须按 (network_id, address) 判定,**绝不能用 symbol** ——
#    链上假 USDC / 假 SOL 遍地,按 symbol 判会把真币误当计价币直接排除掉。
# ⚠️ probe #13 必须核对这些 CA 是否与 FOMO 返回的一致。
# 键统一用 normalize_token_address 的输出形态(EVM 小写 / Solana 原样 base58)。
QUOTE_TOKENS: frozenset[tuple[str, str]] = frozenset({
    # ---- Solana ----
    ("solana", "So11111111111111111111111111111111111111112"),   # WSOL
    ("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),   # USDC
    ("solana", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),   # USDT
    # ---- Monad(143) / Robinhood(4663):抓自前端 chains 模块 ----
    # ⚠️ Robinhood 链上的稳定币是 **USDG**,不是 USDC(前端 g() 函数按链返回不同符号)
    ("monad", "0x754704bc059f8c67012fed69bc8a327a5aafb603"),      # USDC
    ("monad", "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"),      # WETH
    ("robinhood", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),  # USDG
    ("robinhood", "0x0bd7d308f8e1639fab988df18a8011f41eacad73"),  # WETH
    # ---- Base (8453) ----
    ("base", "0x4200000000000000000000000000000000000006"),       # WETH
    ("base", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),       # USDC
    ("base", "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"),       # USDbC
    # ---- BSC ----
    ("bsc", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"),        # WBNB
    ("bsc", "0x55d398326f99059ff775485246999027b3197955"),        # USDT
    ("bsc", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),        # USDC
    # ---- Ethereum ----
    ("ethereum", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"),   # WETH
    ("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),   # USDC
    ("ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7"),   # USDT
})

# 各链常见的"原生币占位地址",与链无关,单独判
_NATIVE_SENTINELS = frozenset({
    "0x0000000000000000000000000000000000000000",
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "11111111111111111111111111111111",  # Solana System Program
})

# ============================================================
# 时间戳合理性区间
# ============================================================
# 解析结果必须落在这个区间内,越界一律拒绝该字段走"时间戳缺失"降级。
# 秒/毫秒判错会导致"永远拉不到新数据"的静默失效 —— 这是最危险的一类 bug,
# 靠区间断言兜底比靠位数猜测可靠得多。
_TS_MIN = datetime(2020, 1, 1, tzinfo=UTC)
_TS_MAX_SKEW = timedelta(days=1)


def now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串,全项目统一入口"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def iso_minutes_ago(minutes: int) -> str:
    """
    N 分钟前的 UTC ISO 字符串。

    ⚠️ 时间窗口查询在 SQLite 里是**字符串比较**,下界必须和 now_iso() 用同一个生产者,
       否则格式差一点结果就完全失真。
       踩过的坑:曾用 SQL 的 datetime('now','-10 minutes') 当下界,
       它产出空格分隔、无偏移量的 '2026-08-11 03:58:34',
       而 ingested_at 是 now_iso() 产出的 '2026-08-11T00:08:34+00:00' ——
       逐字符比到第 11 位,'T'(0x54) > ' '(0x20) 恒成立,
       于是「10 分钟窗口」静默退化成「同一 UTC 日的全部记录」。
       后果是失败消息被每 20 秒重试一整天,越积越多直到把正常推送饿死。
    """
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def known_networks() -> dict[str, str]:
    """
    链别名映射表的只读副本。

    给 --probe 的核对表用 —— 外部模块不该直接 import 私有的 _NETWORK_ALIASES,
    那样这里一改名调用方就 ImportError。
    """
    return dict(_NETWORK_ALIASES)


# ============================================================
# 归一化纯函数
# ============================================================
def normalize_network(raw) -> str | None:
    """
    链标识归一化。

    ⚠️ probe #10 必须确认 swaps / balances / transfers 三处取值是否一致,
       不一致会让同一条链裂成两个聚合键,共识计数直接算错。
    """
    if raw is None:
        return None
    k = str(raw).strip().lower()
    if not k:
        return None
    return _NETWORK_ALIASES.get(k, k)


def normalize_token_address(addr: str | None) -> str | None:
    """
    代币地址归一化。

    ⚠️ EVM hex 地址大小写不敏感,API 可能返回 checksum 混合大小写 → 必须 lower(),
       否则同一个币裂成两条记录,共识计数直接错。
    ⚠️ Solana 是 base58,**大小写敏感,绝不能 lower()**。

    判据用地址自身的编码形态(0x 开头 + 42 位),不用链名白名单 ——
    "这是不是 EVM 地址编码"是确定性的,"这是哪条 EVM 链"才是猜。
    用链白名单判会在遇到未知链(如 Arbitrum 42161)时漏掉 lower()。
    """
    a = (addr or "").strip()
    if not a:
        return None
    if a.startswith("0x") and len(a) == 42:
        return a.lower()
    return a


def is_quote_token(network_id: str | None, token_address: str | None) -> bool:
    """
    是否是计价币(SOL / ETH / BNB / USDC / USDT 等)。

    计价币不打徽章、不算共识、不进 stats,但事件照常落库照常推送。
    不排除的话 $SOL 的共识数会恒等于名单人数,功能 B 整体变噪音。
    """
    if not token_address:
        return False
    if token_address.lower() in _NATIVE_SENTINELS:
        return True
    if not network_id:
        return False
    return (network_id, token_address) in QUOTE_TOKENS


def to_iso(v) -> tuple[str | None, bool]:
    """
    时间戳统一入口,返回 (iso_or_None, is_fallback)。

    秒 / 毫秒 / 微秒 / ISO 字符串四种形态都可能出现(某些 Solana 侧接口返回微秒)。

    ⚠️ 位数判断不可靠,真正的兜底是**结果合理性断言**:
       解析结果必须落在 [2020-01-01, now+1d],越界一律返回 (None, True),
       由调用方走"时间戳缺失"降级。
       probe #3 必须用一笔已知时间的真实交易人工核对一次。
    """
    if v is None or v == "":
        return None, True

    # ---- ISO / 日期字符串 ----
    if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None, True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return _validated(dt)

    # ---- 数值:秒 / 毫秒 / 微秒 ----
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None, True
    if n <= 0:
        return None, True

    # 依次按三种量纲试,取第一个落在合理区间内的 ——
    # 位数猜测会在闰秒边界和极端值上出错,区间断言不会
    for divisor in (1, 1_000, 1_000_000):
        try:
            dt = datetime.fromtimestamp(n / divisor, tz=UTC)
        except (OverflowError, OSError, ValueError):
            continue
        iso, fallback = _validated(dt)
        if not fallback:
            return iso, False
    return None, True


def _validated(dt: datetime) -> tuple[str | None, bool]:
    """合理性断言:越界返回 (None, True),让调用方走降级"""
    upper = datetime.now(UTC) + _TS_MAX_SKEW
    if dt < _TS_MIN or dt > upper:
        return None, True
    return dt.astimezone(UTC).isoformat(timespec="seconds"), False


def make_event_id(
    kind: str,
    native_id: str | None,
    *,
    user_id: str,
    tx_hash: str | None = None,
    token_address: str | None = None,
    event_ts: str | None = None,
    amount: str | None = None,
    page_index: int = 0,
) -> str:
    """
    生成去重键。

    主路径:'{kind}:{原生 id}' —— API 给了稳定 id 就用它,最可靠。

    兜底:sha1(kind|user|tx_hash|token|ts|amount|页内序号)
    ⚠️ **必须掺入 tx_hash 和页内序号**。一笔大额买入会被 DEX 路由拆成多条
       等额、同秒、同币的 swap;不掺这两个分量的话四条会哈希成同一个 id,
       被 INSERT OR IGNORE 误去重,金额少报 75%。
    """
    if native_id:
        return f"{kind}:{native_id}"
    payload = "|".join([
        kind, user_id or "", tx_hash or "", token_address or "",
        event_ts or "", amount or "", str(page_index),
    ])
    return f"{kind}:h:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"


# ============================================================
# 事件数据结构
# ============================================================
@dataclass
class FomoEvent:
    """
    一条监控事件。

    字段分三类:
      1) 落库字段 —— 与 fomo_events 表一一对应
      2) 判定结果 —— badge / badge_reason,落库后永不重算
      3) 内存标志与展示字段 —— 不落库,只在本 tick 的判定和渲染中使用
    """

    # ---- 落库字段 ----
    event_id: str
    event_type: str
    user_id: str
    event_ts: str
    raw_json: str
    handle: str | None = None          # 展示名(displayName)
    user_handle: str | None = None     # @handle,与展示名一起显示
    network_id: str | None = None
    token_address: str | None = None
    token_symbol: str | None = None
    amount_usd: float | None = None
    token_amount: str | None = None      # 原始数量存字符串:memecoin 是 1e15 量级,float 丢精度
    price_usd: float | None = None
    tx_hash: str | None = None
    ingested_at: str = field(default_factory=now_iso)

    # ---- 判定结果(落库,永不重算) ----
    badge: str | None = None
    badge_reason: str | None = None

    # ---- 内存标志(不落库) ----
    ts_fallback: bool = False    # 时间戳是兜底来的 → 不写 first_buy_at
    side_unknown: bool = False   # 买卖方向判不出 → 不写 stats、不打徽章、不显示共识
    # 两侧都是计价币的兑换(USDT→USDC、SOL→USDC 等)。
    # 既不是建仓也不是离场,没有信号价值 —— **落库但不推送**(用户决定)。
    # 落库是为了保留"他当时是不是在备钱"的回溯能力。
    quote_only: bool = False
    api_trade_count: int | None = None   # Q3 否决票用,probe 确认语义前不参与判定

    # ---- 展示字段(不落库,API 透传;缺失则对应行整行消失) ----
    holding_usd: float | None = None
    avg_price: float | None = None
    market_cap: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    realized_pnl: float | None = None        # 已实现盈亏(卖出时才有意义)
    realized_pnl_pct: float | None = None
    thesis_text: str | None = None
    counterparty_handle: str | None = None
    counterparty_is_watched: bool = False
    # 对手方**钱包地址**(转入取 fromAddress、转出取 toAddress)。
    # ⚠️ 这是转入告警里唯一可证的硬证据,所以必须落库:报文里**没有 userId**
    #    (8404 条真实转账里 userId 键出现 0 次),"是不是同一个人在发"永远无从判断;
    #    而"是不是同一个钱包在发"是地址比对,确定性的。
    #    $fih 真实案例:5 分 23 秒内同一个 fromAddress 发给名单里三个人。
    counterparty_address: str | None = None
    # 代币合约创建时间(unix 秒)→ 消息里的「币龄」。
    # ⚠️ 落库:补发的消息也要能显示它,而 formatter 是纯函数、不查库(§10.4 铁律 7)
    token_created_at: int | None = None

    @property
    def token_key(self) -> tuple[str, str] | None:
        """
        功能 A/B 的聚合键。任一分量缺失都返回 None ——
        构造不出键就不打徽章、不算共识(但事件照常落库照常推送)。
        """
        if not self.network_id or not self.token_address:
            return None
        return (self.network_id, self.token_address)

    @property
    def is_quote(self) -> bool:
        return is_quote_token(self.network_id, self.token_address)

    @property
    def countable_buy(self) -> bool:
        """是否是一笔可计入 user_token_stats 的市场买入"""
        return (
            self.event_type == EVENT_BUY
            and not self.side_unknown
            and self.token_key is not None
            and not self.is_quote
        )

    def to_row(self) -> dict:
        """转成 fomo_events 的插入参数"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "handle": self.handle,
            "user_handle": self.user_handle,
            "network_id": self.network_id,
            "token_address": self.token_address,
            "token_symbol": self.token_symbol,
            "amount_usd": self.amount_usd,
            "token_amount": self.token_amount,
            "price_usd": self.price_usd,
            # 市值是时点值、事后无法重算 —— /hot 的"买入时市值 → 现在"倍数靠它
            "market_cap": self.market_cap,
            # 币龄基准。落库是为了补发的消息也能显示(formatter 不查库)
            "token_created_at": self.token_created_at,
            "tx_hash": self.tx_hash,
            "event_ts": self.event_ts,
            "ingested_at": self.ingested_at,
            "badge": self.badge,
            "badge_reason": self.badge_reason,
            # 发货地址。⚠️ 与 market_cap 同理:事后无法从别处补回来,
            #    而"同一个地址发给了几个人"正是转入告警里唯一站得住的证据
            "counterparty_address": self.counterparty_address,
            "raw_json": self.raw_json,
        }


def dump_raw(obj) -> str:
    """
    原始报文序列化。字段语义未实测,必须全量留存原始数据以便日后离线回填 ——
    改一次解析逻辑就得重拉一遍历史是不可接受的。
    """
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"_unserializable": str(obj)}, ensure_ascii=False)


def pick(d: dict, *keys, default=None):
    """
    多键取值 —— 逆向出来的字段名不一定准,按优先级依次尝试。
    例: pick(raw, "networkId", "chainId", "network")
    probe 确认真实字段名后,可以把候选列表收窄,但保留兜底不会有坏处。
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default
