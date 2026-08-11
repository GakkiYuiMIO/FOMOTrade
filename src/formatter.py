"""
FomoEvent → Telegram HTML 消息渲染(设计文档 §10)

============ formatter 实现铁律(§10.4) ============
1. 标题行第一个字符必须是事件 emoji,且全局唯一 —— 快速滑动时唯一的扫描锚点
2. 任何可选字段缺失时「整行消失」,绝不打印 "N/A" / "--" / "0"
3. badge 三态,None 表示数据不足 —— 宁可漏标不可错标,None 一律渲染成 🟢
4. 所有来自 API 的文本(handle / thesis 正文)必须 html.escape,否则一个 '<' 就 400
5. 不使用空行分组:手机端空行照样占行高,全推场景一屏只放得下一条半消息

补充两条同样致命的:
6. CA 独占最后一行、纯 <code>、绝不截断、不加「CA:」前缀、不用 <a> 包裹 ——
   点 <code> 实体一键复制是中国网络下唯一 100% 可用的操作(§10.3)
7. 本模块是纯函数,不做任何 IO / DB 访问 —— 拿不到真实 API 数据的阶段,
   它是唯一能被完整单测的展示层

⚠️ 「缺失即整行消失」的判据一律是 `is None`,**绝不能用真值判断**:
   amount_usd=0.0 / holders=0 都是有意义的真实值(卖出清仓就靠 `📦 剩余 $0.00` 体现),
   用 `if not x` 会把它们连同 None 一起吞掉。
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation, localcontext

from loguru import logger

from src.models import (
    BADGE_FIRST,
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    FomoEvent,
)

# ============================================================
# 行首锚点(§10.1)—— 全局唯一,永不重复,永不被别的字符挤掉
# ============================================================
EMOJI_FIRST = "🌱"           # 首次建仓
EMOJI_ADD = "🟢"             # 加仓(也是 badge=None 时的兜底,铁律 3)
EMOJI_SELL = "🔴"            # 卖出
EMOJI_THESIS = "💭"          # 发表观点
EMOJI_TRANSFER_IN = "📥"     # 收到转入
EMOJI_TRANSFER_OUT = "📤"    # 转出

LABEL_FIRST = "首次建仓"
LABEL_ADD = "加仓"
LABEL_SELL = "卖出"
LABEL_THESIS = "发表观点"
LABEL_TRANSFER_IN = "收到转入"
LABEL_TRANSFER_OUT = "转出"
# 方向判不出来时的标题文案(§九 降级矩阵:「标题写『交易』,无徽章无共识」)
LABEL_UNKNOWN_SIDE = "交易"

# ============================================================
# 第二列以后的 emoji —— 永不占行首
# ============================================================
EMOJI_AMOUNT_IN = "💰"       # 买入 / 转账数量
EMOJI_AMOUNT_OUT = "💸"      # 卖出
EMOJI_HOLDING = "📦"
EMOJI_AVG_PRICE = "📊"
EMOJI_TRADE_COUNT = "🔄"
EMOJI_MARKET_CAP = "💎"
EMOJI_CONSENSUS = "👥"
EMOJI_NETWORK = "🧬"
EMOJI_COUNTERPARTY = "👤"
EMOJI_WARN = "⚠️"
EMOJI_PENDING = "⏳"

# ============================================================
# 固定文案
# ============================================================
SEP = " · "                                      # 标题行分隔符(U+00B7)
TEXT_TRANSFER_NOT_BUY = f"{EMOJI_WARN} 转账获得,非市场买入"
TEXT_INTERNAL_TRANSFER = f"{EMOJI_WARN} 名单内转账"
TEXT_BASELINE_PENDING = f"{EMOJI_PENDING} 基线建立中,首次/共识暂不可用"
TEXT_UNKNOWN_USER = "未知用户"

LABEL_HOLDING_BUY = "持仓"
LABEL_HOLDING_SELL = "剩余"      # 清仓自然体现为「📦 剩余 $0.00」,不设独立的清仓事件类型
LABEL_HOLDING_THESIS = "他持仓"  # 「他」字必须有,否则会被误读成 token 总量(§10.2 场景 E)
LABEL_AMOUNT_QTY = "数量"
LABEL_AMOUNT_USD = "金额"
LABEL_FROM = "来自"
LABEL_TO = "转给"
# 方向判不出时用的中性说法 —— 「来自」和「转给」都是在断言方向,断错就是彻底的错误信息
LABEL_COUNTERPARTY = "对手方"

# thesis 正文硬截断长度。TG 单条上限 4096,但一条几百字的观点在全推流里已经过长,
# 超出部分用 expandable 折叠(Bot API 7.4+;旧客户端退化成普通引用,仍可读)
THESIS_MAX_CHARS = 500

# ============================================================
# 链名展示
# ============================================================
# 键是 models.normalize_network() 的输出(全小写)。未命中时原样透传 ——
# ⚠️ 未命中值直接来自 API,必须 escape(见 _network_line)
NETWORK_DISPLAY = {
    "solana": "Solana",
    "base": "Base",
    "bsc": "BSC",
    "ethereum": "Ethereum",
    "arbitrum": "Arbitrum",
    "polygon": "Polygon",
}

# 观点正文里的连续空行:手机端空行照样占行高(铁律 5),用户原文里的空行同样要压掉
_BLANK_LINES = re.compile(r"\n\s*\n+")

# 金额缩写单位(市值行用)
_COMPACT_UNITS = (
    (Decimal("1e12"), "T"),
    (Decimal("1e9"), "B"),
    (Decimal("1e6"), "M"),
    (Decimal("1e3"), "K"),
)


# ============================================================
# 数值格式化 —— 全部返回 str | None,None 表示「这一行不要出现」
# ============================================================
def _to_decimal(v) -> Decimal | None:
    """
    任意输入 → Decimal。无法解析 / NaN / Inf 一律返回 None(调用方据此整行消失)。

    ⚠️ 必须走 Decimal 而不是 float:token 数量是 1e15 量级的字符串,
       float 在 17 位有效数字之后就开始丢精度,`1,200,000,000,000,001` 会显示成 ...000。
    ⚠️ API 字段语义未实测,任何脏值都可能出现(空串 / "N/A" / 带千分位的字符串),
       这里必须吃掉所有异常,绝不能让一条脏数据炸掉整条推送。
    """
    if v is None:
        return None
    if isinstance(v, bool):          # bool 是 int 的子类,当数量用一定是上游 bug
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("_", "")
        if not s:
            return None
    else:
        s = str(v)
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _fmt_usd(v) -> str | None:
    """普通金额:$2,500.00 / -$12.30 / $0.00(0 是真实值,照常显示)"""
    d = _to_decimal(v)
    if d is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 60                        # 默认 prec=28,大额 quantize 会 InvalidOperation
            q = d.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def _fmt_usd_compact(v) -> str | None:
    """缩写金额(市值行):$19.14M / $2.10B。不足 1000 时退化成普通金额"""
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            for base, suffix in _COMPACT_UNITS:
                if a >= base:
                    return f"{sign}${(a / base).quantize(Decimal('0.01')):,.2f}{suffix}"
    except (InvalidOperation, ValueError):
        return None
    return _fmt_usd(d)


def _fmt_price(v) -> str | None:
    """
    单价:$0.016 / $1,234.56 / $0.0000012345

    ⚠️ 不能复用 _fmt_usd —— memecoin 单价普遍在 1e-6 量级,
       两位小数会把每一个价格都渲染成 $0.00,那一行就成了纯噪音。
    """
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    if a >= 1 or a == 0:
        return f"{sign}${a:,.2f}"
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            # 小于 1 时保留 4 位有效数字:0.016 → 0.016,0.0000012345 → 0.000001234
            exponent = a.adjusted()              # 10 的幂次,0.016 → -2
            q = a.quantize(Decimal(1).scaleb(exponent - 3))
    except (InvalidOperation, ValueError):
        return None
    s = format(q, "f").rstrip("0").rstrip(".")
    return f"{sign}${s or '0'}"


def _fmt_qty(v) -> str | None:
    """
    token 数量:1,200,000 / 1,250,000.5 / 0.00000123

    ⚠️ 整数部分**永不截断、永不缩写** —— 数量是用户核对链上记录的依据,
       缩写成 1.2M 就核对不了了。小数部分才做截断。
    """
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            if a >= 1:
                q = a.quantize(Decimal("0.0001"))    # 大额只保留 4 位小数,整数位一位不动
            elif a == 0:
                q = a
            else:
                # 小于 1 保留 8 位有效数字,避免整串 0
                q = a.quantize(Decimal(1).scaleb(a.adjusted() - 7))
            s = format(q, "f")
    except (InvalidOperation, ValueError):
        return None
    int_part, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    # int() 走 Python 大整数,1e30 也不会丢精度;f"{:,}" 负责千分位
    out = f"{int(int_part or '0'):,}"
    return f"{sign}{out}.{frac}" if frac else f"{sign}{out}"


# ============================================================
# 单行渲染 —— 每个函数返回 str | None,None 一律被 render 丢弃
# ============================================================
def _esc(v) -> str:
    """
    所有来自 API 的文本必经之路(铁律 4)。

    ⚠️ handle / symbol / thesis 正文 / 对手方名 全是用户可控内容,
       一个裸 '<' 就让整条消息 400 Bad Request —— 这既是稳定性问题,
       更是一个可被投毒的攻击面(改个昵称就能让监控静默失效)。
    """
    return html.escape(str(v))


def _display_name(ev: FomoEvent) -> str:
    """展示名优先用 handle;都没有时退到 user_id,绝不留空标题"""
    name = (ev.handle or "").strip() or (ev.user_id or "").strip()
    return _esc(name) if name else TEXT_UNKNOWN_USER


def _symbol_plain(ev: FomoEvent) -> str | None:
    """去掉 API 可能自带的 $ 前缀,由模板统一补 —— 否则会出现 $$TOAD"""
    s = (ev.token_symbol or "").strip().lstrip("$").strip()
    return s or None


def _title_anchor(ev: FomoEvent) -> tuple[str, str]:
    """
    (行首 emoji, 标题文案)。

    ⚠️ 铁律 3:badge 为 None(数据不足)时一律 🟢 加仓,**绝不显示 🌱** ——
       一屏全 🌱 会让这个符号在用户心里当场作废,而且没有第二次机会。
    """
    et = ev.event_type
    if et == EVENT_THESIS:
        return EMOJI_THESIS, LABEL_THESIS
    if et in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT):
        # ⚠️ 这道门必须在下面两个分支之前。poller 在方向判不出时会把 direction
        #    兜底成 TRANSFER_IN 并置 side_unknown —— 若不拦,一笔实际是转出的记录
        #    会被渲染成「📥 收到转入」,这是彻底的错误信息,比不显示糟得多。
        #    锚点仍复用 📥,不新增第七个行首 emoji(铁律 1:行首 emoji 全局唯一且固定)。
        if ev.side_unknown:
            return EMOJI_TRANSFER_IN, LABEL_UNKNOWN_SIDE
        if et == EVENT_TRANSFER_IN:
            return EMOJI_TRANSFER_IN, LABEL_TRANSFER_IN
        return EMOJI_TRANSFER_OUT, LABEL_TRANSFER_OUT
    if et == EVENT_SELL:
        return EMOJI_SELL, (LABEL_UNKNOWN_SIDE if ev.side_unknown else LABEL_SELL)
    # BUY 与任何未知 event_type 都收敛到这里:兜底成 🟢,不新增第七个行首锚点
    if et != EVENT_BUY or ev.side_unknown:
        return EMOJI_ADD, LABEL_UNKNOWN_SIDE
    if ev.badge == BADGE_FIRST:
        return EMOJI_FIRST, LABEL_FIRST
    return EMOJI_ADD, LABEL_ADD


def _title_line(ev: FomoEvent) -> str:
    emoji, label = _title_anchor(ev)
    parts = [f"{emoji} <b>{_display_name(ev)}</b>", label]
    sym = _symbol_plain(ev)
    if sym is not None:
        parts.append(f"<b>${_esc(sym)}</b>")
    return SEP.join(parts)


def _thesis_line(ev: FomoEvent) -> str | None:
    """
    观点正文。<blockquote> 在 TG 里渲染成左侧竖线,与数据行视觉分层。

    ⚠️ 必须**先截断原文再 escape**:反过来的话会把 `&amp;` 从中间劈开,
       残缺实体同样 400。
    """
    text = (ev.thesis_text or "").strip()
    if not text:
        return None
    text = _BLANK_LINES.sub("\n", text)
    if len(text) > THESIS_MAX_CHARS:
        body = _esc(text[:THESIS_MAX_CHARS].rstrip()) + "…"
        return f"<blockquote expandable>{body}</blockquote>"
    return f"<blockquote>{_esc(text)}</blockquote>"


def _amount_line(ev: FomoEvent) -> str | None:
    """
    金额行。三种降级路径(§九 降级矩阵):
      USD 有   → 💰 买入 $2,500.00
      USD 缺   → 💰 买入 1,250,000 TOAD      (显示原生数量,功能 A/B 完全不受影响)
      两者皆缺 → 整行消失
    转账形态额外带上换算:💰 数量 1,200,000 ≈ $19,200.00
    """
    et = ev.event_type
    if et == EVENT_THESIS:
        return None                              # 观点没有金额
    usd = _fmt_usd(ev.amount_usd)
    qty = _fmt_qty(ev.token_amount)
    if usd is None and qty is None:
        return None

    is_transfer = et in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT)
    if is_transfer or ev.side_unknown:
        # 方向不明时也走这套中性文案,免得标题写「交易」而正文写「买入」自相矛盾
        emoji = EMOJI_AMOUNT_IN
        if qty is not None and usd is not None:
            return f"{emoji} {LABEL_AMOUNT_QTY} {qty} ≈ {usd}"
        if qty is not None:
            return f"{emoji} {LABEL_AMOUNT_QTY} {qty}"
        return f"{emoji} {LABEL_AMOUNT_USD} {usd}"

    emoji = EMOJI_AMOUNT_OUT if et == EVENT_SELL else EMOJI_AMOUNT_IN
    label = LABEL_SELL if et == EVENT_SELL else "买入"
    if usd is not None:
        return f"{emoji} {label} {usd}"
    sym = _symbol_plain(ev)
    return f"{emoji} {label} {qty} {_esc(sym)}" if sym else f"{emoji} {label} {qty}"


def _counterparty_line(ev: FomoEvent) -> str | None:
    """转账对手方。名单内转账必须标出来,否则用户无法辨别筹码是不是在名单内搬家(B-9)"""
    if ev.event_type not in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT):
        return None
    who = (ev.counterparty_handle or "").strip()
    if not who:
        return None
    # 方向判不出时用中性说法:「来自」/「转给」都在断言方向,断错就是彻底的错误信息
    if ev.side_unknown:
        label = LABEL_COUNTERPARTY
    else:
        label = LABEL_FROM if ev.event_type == EVENT_TRANSFER_IN else LABEL_TO
    line = f"{EMOJI_COUNTERPARTY} {label} {_esc(who)}"
    if ev.counterparty_is_watched:
        line += f" {TEXT_INTERNAL_TRANSFER}"
    return line


def _holding_line(ev: FomoEvent) -> str | None:
    """
    持仓行。⚠️ 判据必须是 `is None` —— holding_usd=0.0 是清仓的真实体现
    (`📦 剩余 $0.00`),不是缺失。
    """
    if ev.holding_usd is None:
        return None
    usd = _fmt_usd(ev.holding_usd)
    if usd is None:
        return None
    if ev.event_type == EVENT_THESIS:
        label = LABEL_HOLDING_THESIS
    elif ev.event_type in (EVENT_SELL, EVENT_TRANSFER_OUT):
        label = LABEL_HOLDING_SELL
    else:
        label = LABEL_HOLDING_BUY
    return f"{EMOJI_HOLDING} {label} {usd}"


def _avg_price_line(ev: FomoEvent) -> str | None:
    """⚠️ 只透传 API 字段,**绝不本地推算**(§10.2 场景 C):均价需要累计买入 token 数量,
    而 token 数量按设计存 TEXT、不做算术。显示一个错的均价比不显示更糟。"""
    p = _fmt_price(ev.avg_price)
    return f"{EMOJI_AVG_PRICE} 均价 {p}" if p is not None else None


def _trade_count_line(ev: FomoEvent) -> str | None:
    """
    ⚠️ 同样只透传(probe #8 未确认语义前 poller 根本不会填这个字段)。
       绝不能拿 user_token_stats.buy_count 顶替:拆单会让它 +N,给出的是错的次数。
    """
    n = ev.api_trade_count
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return f"{EMOJI_TRADE_COUNT} 第 {n} 次交易" if n > 0 else None


def _market_cap_line(ev: FomoEvent) -> str | None:
    mc = _fmt_usd_compact(ev.market_cap)
    return f"{EMOJI_MARKET_CAP} 市值 {mc}" if mc is not None else None


def _consensus_line(buyers: int | None, watchlist: int | None, holders: int | None) -> str | None:
    """
    功能 B:👥 名单内 3/12 人买过 · 2 人仍持有

    - buyers / watchlist 任一为 None → 整行消失(共识算不出来时绝不留半句话)
    - holders 为 None → 只有「· N 人仍持有」这一段消失,主指标不受影响(B-5)
    - holders=0 是真实值,照常显示 —— 卖出消息里「0 人仍持有」本身就是强信号
    ⚠️ 文案永远写「买过」而不是「刚刚买入」:分子分母会随 /add /del 跃迁,
       任何时效承诺都会在下一次跃迁时变成假话(B-7)。
    """
    if buyers is None or watchlist is None:
        return None
    try:
        b, w = int(buyers), int(watchlist)
    except (TypeError, ValueError):
        return None
    if w <= 0:
        return None                              # 0/0 没有任何信息量,不如不显示
    line = f"{EMOJI_CONSENSUS} 名单内 {b}/{w} 人买过"
    if holders is None:
        return line
    try:
        h = int(holders)
    except (TypeError, ValueError):
        return line
    return f"{line} · {h} 人仍持有"


def _network_line(ev: FomoEvent) -> str | None:
    net = (ev.network_id or "").strip()
    if not net:
        return None
    # 未命中映射表时原样透传 —— 该值直接来自 API,必须 escape
    return f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}"


def _ca_line(ev: FomoEvent) -> str | None:
    """
    CA 独占最后一行,纯 <code>(§10.3)。

    ⚠️ 绝不截断:截断了就复制不了,整条消息的实用价值归零,宁可换行。
    ⚠️ 绝不加「CA:」前缀:tap-to-copy 的命中区就是 code 实体覆盖的字符范围,
       整行都是 code 时点哪都能复制。
    ⚠️ 绝不用 <a href> 包裹:TG 内置浏览器在中国网络下大概率白屏,
       那等于把最可靠的操作换成了最不可靠的。
    """
    ca = (ev.token_address or "").strip()
    return f"<code>{_esc(ca)}</code>" if ca else None


# ============================================================
# 对外唯一入口
# ============================================================
def render(
    ev: FomoEvent,
    buyers: int | None = None,
    watchlist: int | None = None,
    holders: int | None = None,
    baseline_pending: bool = False,
) -> str:
    """
    渲染一条 Telegram HTML 消息。

    参数:
        ev               事件(badge 已在落库时判定并冻结,这里只读不判)
        buyers/watchlist 功能 B 主指标;任一为 None → 共识行整段消失
        holders          功能 B 副指标;None → 只掉「N 人仍持有」这一段
        baseline_pending 基线未就绪 → 末尾追加 ⏳ 尾行

    ⚠️ 本函数**不得抛异常**。它在 poller 的发送循环里被调用,
       一条脏数据把渲染炸掉会连带整个 tick 停摆 —— 宁可发一条降级消息。
    """
    try:
        return _render(ev, buyers, watchlist, holders, baseline_pending)
    except Exception as e:  # noqa: BLE001
        # 走到这里一定是本模块的 bug(所有字段级异常都已在下游吃掉),必须留痕
        logger.exception("消息渲染失败,降级为最简文本 | event_id={} | {}", getattr(ev, "event_id", "?"), e)
        return _fallback(ev)


def _render(
    ev: FomoEvent,
    buyers: int | None,
    watchlist: int | None,
    holders: int | None,
    baseline_pending: bool,
) -> str:
    # 行序固定,缺失的行整行消失。这个顺序逐条对齐设计文档 §10.2 的七个场景
    candidates = [
        _title_line(ev),
        _thesis_line(ev),
        _amount_line(ev),
        _counterparty_line(ev),
        # 「转账获得」只对**确定是转入**的记录成立:
        #   转出不存在「被误当成买入」的风险;方向判不出时更不能这么断言
        TEXT_TRANSFER_NOT_BUY
        if (ev.event_type == EVENT_TRANSFER_IN and not ev.side_unknown)
        else None,
        _holding_line(ev),
        _avg_price_line(ev),
        _trade_count_line(ev),
        _market_cap_line(ev),
        _consensus_line(buyers, watchlist, holders),
        _network_line(ev),
        _ca_line(ev),                            # CA 永远是数据部分的最后一行
        TEXT_BASELINE_PENDING if baseline_pending else None,
    ]
    # 不使用空行分组(铁律 5):这里 join 的是已经过滤掉 None 的行,不会产生空行
    return "\n".join(line for line in candidates if line)


def _fallback(ev: FomoEvent) -> str:
    """渲染彻底失败时的保底消息 —— 信息量最小,但绝不丢消息、绝不 400"""
    try:
        who = _display_name(ev)
        et = _esc(getattr(ev, "event_type", "?") or "?")
        return f"{EMOJI_WARN} <b>{who}</b>{SEP}{et}{SEP}消息渲染异常"
    except Exception:  # noqa: BLE001
        return f"{EMOJI_WARN} 消息渲染异常"
