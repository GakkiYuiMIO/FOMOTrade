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
import math
import re
import time
from decimal import Decimal, InvalidOperation, localcontext

from loguru import logger

from src.models import (
    BADGE_FIRST,
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    GMGN_SLUG,
    NETWORK_DISPLAY,
    NETWORK_SLUG,
    FomoEvent,
)

# ⚠️ 只借 MAX_MESSAGE_LEN 这一个常量(与 bot.py 同样的做法):
#    转入告警要自己算长度预算,而"上限是多少"必须与真正发消息的那一侧同源 ——
#    两处各写一个数字,改了一处另一处就变成一颗定时炸弹。
#    notifier 只依赖 config,不依赖本模块,所以这个 import 不会成环。
from src.notifier import MAX_MESSAGE_LEN

# ============================================================
# 行首锚点(§10.1)—— 全局唯一,永不重复,永不被别的字符挤掉
# ============================================================
EMOJI_FIRST = "🌱"           # 首次建仓
EMOJI_ADD = "🟢"             # 加仓(也是 badge=None 时的兜底,铁律 3)
EMOJI_SELL = "🔴"            # 卖出
EMOJI_THESIS = "💭"          # 发表观点
EMOJI_TRANSFER_IN = "📥"     # 收到转入
EMOJI_TRANSFER_OUT = "📤"    # 转出

# 特别关注标记。⚠️ 它跟在事件 emoji **后面**,不占行首(见 _title_line 里的说明)
STAR_MARK = "⭐"

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
EMOJI_TOKEN_AGE = "🕐"
EMOJI_CONSENSUS = "👥"
EMOJI_NETWORK = "🧬"
EMOJI_COUNTERPARTY = "👤"
EMOJI_WARN = "⚠️"
EMOJI_PENDING = "⏳"
EMOJI_PNL_UP = "📈"
EMOJI_PNL_DOWN = "📉"
EMOJI_LINK = "🔗"

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
LABEL_PNL_REALIZED = "已实现盈亏"
LABEL_PNL_UNREALIZED = "未实现盈亏"

# 代币页链接。⚠️ 链 slug 未收录时**整个链接不出** ——
# 错的链接比没有链接更糟(§10.3),拼一个平台不支持的链只会得到 404。
FOMO_TOKEN_URL = "https://fomo.family/tokens/{slug}/{ca}"
GMGN_TOKEN_URL = "https://gmgn.ai/{slug}/token/{ca}"

# thesis 正文硬截断长度。TG 单条上限 4096,但一条几百字的观点在全推流里已经过长,
# 超出部分用 expandable 折叠(Bot API 7.4+;旧客户端退化成普通引用,仍可读)
THESIS_MAX_CHARS = 500

# ============================================================
# 链名展示
# ============================================================
# 键是 models.normalize_network() 的输出(全小写)。未命中时原样透传 ——
# ⚠️ 未命中值直接来自 API,必须 escape(见 _network_line)
# 链的展示名与 slug 统一由 models 提供(与 fomo.family 官方映射一致),
# 这里不再维护第二份 —— 两份必然会不同步。

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
    """
    展示名。有 @handle 时一并显示:`血手人屠·厉飞雨 (@GakkiYuiTifa)`。

    展示名可以随时改、也可能重名,@handle 才是能拿去搜的那个标识 ——
    两个都给,扫消息时认人、要查证时有据可循。
    两者相同时不重复显示(有些人没设展示名,handle 会被直接当展示名用)。
    """
    name = (ev.handle or "").strip() or (ev.user_id or "").strip()
    return _esc(name) if name else TEXT_UNKNOWN_USER


def _handle_suffix(ev: FomoEvent) -> str:
    """`(@handle)` 后缀。与展示名相同时不重复显示(有人没设展示名,handle 会被当展示名用)"""
    h = (ev.user_handle or "").strip().lstrip("@")
    if not h:
        return ""
    name = (ev.handle or "").strip()
    if name and h.lower() == name.lower():
        return ""
    return f" (@{_esc(h)})"


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


def _title_line(ev: FomoEvent, starred: bool = False) -> str:
    emoji, label = _title_anchor(ev)
    # ⚠️ 星标只能放在**事件 emoji 之后**,绝不能顶到行首(铁律 1):
    #    行首那个字符是聊天列表预览里唯一的扫描锚点。被 ⭐ 顶掉之后,
    #    所有特别关注的消息在列表预览里长得一模一样,买入卖出当场分不出来。
    mark = f"{STAR_MARK} " if starred else ""
    # 只给展示名加粗:@handle 是辅助信息,一起加粗会把行首锚点的视觉重量冲散
    parts = [f"{emoji} {mark}<b>{_display_name(ev)}</b>{_handle_suffix(ev)}", label]
    sym = _symbol_plain(ev)
    if sym is not None:
        parts.append(_style_symbol(sym, starred))
    return SEP.join(parts)


def _style_symbol(sym: str, starred: bool) -> str:
    """
    币名的样式。特别关注的人要更醒目。

    ⚠️ Telegram 的 Bot API **不支持任意文字颜色** —— 允许的标签只有
       b / i / u / s / code / pre / a / blockquote / tg-spoiler,没有 font、没有 style。
       真能出颜色的只有两条:diff 代码块(加号行绿、减号行红)和彩色 emoji;
       而前者是等宽的块级元素,会把标题行整个拆开、还吃掉行内链接。
       所以这里用「加粗 + 方括号」做强调,颜色交给行首的 🌱/🟢/🔴 承担 ——
       那本来就是这条消息的颜色锚点。改样式只需要动这一个函数。
    """
    body = f"${_esc(sym)}"
    return f"<b>【{body}】</b>" if starred else f"<b>{body}</b>"


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


def fmt_token_age(created_at: int | float | None, now: float | None = None) -> str | None:
    """
    币龄:8M / 3H / 5D / 2MO / 1.4Y。拿不到就返回 None(整行消失,铁律 2)。

    ⚠️ 分钟用 M、月份用 MO —— 单独一个 M 在币圈语境里会被读成市值(market cap)。
    ⚠️ 未来时间戳返回 None 而不是负数:宁可不显示,也不能出现「币龄 -3H」。
    ⚠️ 天数以内不做小数(3.7H 没有意义),超过一年才给一位小数 ——
       "1.4Y" 比 "511D" 好读。
    """
    if created_at is None:
        return None
    try:
        age = (time.time() if now is None else now) - float(created_at)
    except (TypeError, ValueError):
        return None
    # ⚠️ 必须显式挡 NaN/Inf:`age < 0` 对 NaN 恒为 False,会一路落到最后一支
    #    渲染成 "nanY" —— 一条一眼假的信息比没有这一行糟得多(铁律 2)。
    if not math.isfinite(age) or age < 0:
        return None
    if age < 3600:
        return f"{max(int(age // 60), 1)}M"
    if age < 86400:
        return f"{int(age // 3600)}H"
    if age < 86400 * 30:
        return f"{int(age // 86400)}D"
    if age < 86400 * 365:
        return f"{int(age // (86400 * 30))}MO"
    return f"{age / (86400 * 365):.1f}Y"


def _token_age_line(ev: FomoEvent) -> str | None:
    """
    🕐 币龄 3H。新币是这个项目最关心的信号,而"多新"只有这一行能回答。

    ⚠️ 数据只来自 balances 里的 token.createdAt。名单里没人持有的币(比如刚清仓的)
       拿不到,那就整行消失 —— 绝不本地推算、也绝不用交易对创建时间凑数
       (后者对老币差几年,见 poller._token_created_at)。
    """
    age = fmt_token_age(ev.token_created_at)
    return f"{EMOJI_TOKEN_AGE} 币龄 {age}" if age else None


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


def _pnl_line(ev: FomoEvent) -> str | None:
    """
    盈亏行。卖出看**已实现**,其余看**未实现** —— 卖出那一刻真正落袋的是前者。

    ⚠️ 判据一律 `is None`:盈亏正好是 0 是有意义的真实值(刚开仓、或买卖打平)。
    """
    if ev.event_type in (EVENT_SELL, EVENT_TRANSFER_OUT):
        val, pct, label = ev.realized_pnl, ev.realized_pnl_pct, LABEL_PNL_REALIZED
    else:
        val, pct, label = ev.unrealized_pnl, ev.unrealized_pnl_pct, LABEL_PNL_UNREALIZED
    if val is None:
        return None
    usd = _fmt_usd(abs(val))
    if usd is None:
        return None
    emoji = EMOJI_PNL_DOWN if val < 0 else EMOJI_PNL_UP
    sign = "-" if val < 0 else "+"
    line = f"{emoji} {label} {sign}{usd}"
    if pct is not None:
        try:
            line += f" ({float(pct):+.2f}%)"
        except (TypeError, ValueError):
            pass
    return line


def _links_line(ev: FomoEvent) -> str | None:
    """
    快速跳转:FOMO 代币页 + GMGN。

    ⚠️ 必须排在 CA 行**之前** —— CA 独占最后一行是硬规则(§10.3),
       它是中国网络下唯一 100% 可用的操作(tap-to-copy),链接只是锦上添花。
    ⚠️ 链 slug 未收录时对应链接直接不出:GMGN 不支持 Monad / Robinhood,
       硬拼出来只会 404,而错的链接比没有链接更糟。
    """
    ca = (ev.token_address or "").strip()
    net = (ev.network_id or "").strip()
    if not ca or not net:
        return None
    parts = []
    fomo_slug = NETWORK_SLUG.get(net)
    if fomo_slug:
        parts.append(f'<a href="{FOMO_TOKEN_URL.format(slug=fomo_slug, ca=_esc(ca))}">FOMO</a>')
    gmgn_slug = GMGN_SLUG.get(net)
    if gmgn_slug:
        parts.append(f'<a href="{GMGN_TOKEN_URL.format(slug=gmgn_slug, ca=_esc(ca))}">GMGN</a>')
    return f"{EMOJI_LINK} {' · '.join(parts)}" if parts else None


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
    starred: bool = False,
) -> str:
    """
    渲染一条 Telegram HTML 消息。

    参数:
        ev               事件(badge 已在落库时判定并冻结,这里只读不判)
        buyers/watchlist 功能 B 主指标;任一为 None → 共识行整段消失
        holders          功能 B 副指标;None → 只掉「N 人仍持有」这一段
        baseline_pending 基线未就绪 → 末尾追加 ⏳ 尾行
        starred          特别关注 → 标题加 ⭐、币名加方括号。**纯展示**,
                         不影响徽章、共识、采集的任何判定

    ⚠️ 本函数**不得抛异常**。它在 poller 的发送循环里被调用,
       一条脏数据把渲染炸掉会连带整个 tick 停摆 —— 宁可发一条降级消息。
    """
    try:
        return _render(ev, buyers, watchlist, holders, baseline_pending, starred)
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
    starred: bool = False,
) -> str:
    # 行序固定,缺失的行整行消失。这个顺序逐条对齐设计文档 §10.2 的七个场景
    candidates = [
        _title_line(ev, starred),
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
        _pnl_line(ev),                           # 卖出看已实现,其余看未实现
        _trade_count_line(ev),
        _market_cap_line(ev),
        _token_age_line(ev),
        _consensus_line(buyers, watchlist, holders),
        _network_line(ev),
        _links_line(ev),                         # 链接在 CA 之前 —— CA 必须独占最后一行
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


# ============================================================
# 跟单信号
# ============================================================
def render_copy_signal(cand, d, cfg, status: str) -> str:
    """
    跟单信号消息。

    ⚠️ 必须把**入场市值**和**币龄**摆在最显眼的位置:这两个数决定这一单是"早"还是"追高",
       而信号本身("N 个人买了")对两者一无所知 —— 光看人数会把 $4.19M 的追高
       和 $41.9K 的埋伏读成同一件事。
    ⚠️ 纸上模式必须写明「未真实成交」。含糊的措辞会让人以为钱已经出去了。
    """
    sym = _esc((cand.token_symbol or "?").lstrip("$"))
    head = "🧪 <b>纸上跟单</b>" if status == "paper" else "🛒 <b>跟单信号</b>"
    lines = [f"{head} · <b>${sym}</b> · 👥 {cand.buyers} 人买过"]

    seg = []
    if cand.entry_mcap:
        seg.append(f"💎 入场市值 {_fmt_usd_compact(cand.entry_mcap)}")
    age = fmt_token_age(cand.token_created_at)
    if age:
        seg.append(f"{EMOJI_TOKEN_AGE} 币龄 {age}")
    if seg:
        lines.append(" · ".join(seg))

    lines.append(f"💰 跟单金额 ${cfg.amount_usd:,.2f}"
                 + ("(仅记账,<b>未真实成交</b>)" if status == "paper" else ""))
    net = (cand.network_id or "").strip()
    if net:
        lines.append(f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}")
    link = _links_line(_fake_ev(net, cand.token_address))
    if link:
        lines.append(link)
    lines.append(f"<code>{_esc(cand.token_address)}</code>")
    return "\n".join(lines)


def _fake_ev(net: str, ca: str):
    """_links_line 只用到这两个字段 —— 复用它,免得链接拼装出现第二份实现"""
    return FomoEvent(event_id="", event_type=EVENT_BUY, user_id="", event_ts="", raw_json="",
                     network_id=net or None, token_address=ca)


# ============================================================
# 转入告警:N 个名单成员「收到」了同一个币
# ============================================================
# 行首锚点。⚠️ 全局唯一(铁律 1),与买卖那六个、与跟单的 🧪/🛒 都不重样 ——
#    这条消息在聊天列表预览里必须一眼能跟别的区分开:它说的是"有人白拿到了筹码",
#    与"有人买入"含义相反,认错锚点就是把相反的信号读成同一件事。
EMOJI_DISTRIBUTION = "🚨"
# 单条消息的**转义后**字符预算,与 /ca 同一套规矩(见 bot.CA_MSG_BUDGET 的事故记录):
# notifier.send 超限时做的是盲切,切点落在 `&amp;` 中间就是残缺实体 → 整条 400,
# 用户什么都收不到。所以长度必须由渲染方按转义后的真实长度自己算,且只在整行边界停手。
TRANSFER_MSG_BUDGET = MAX_MESSAGE_LEN - 400
# handle / ticker 由陌生人和服务端决定,长度不受任何天然约束 —— 一个 3000 字符的
# ticker 就能把标题撑到顶破预算。先按**转义前**的字符数压一压再转义(顺序见 _clip)。
_SIG_HANDLE_CHARS = 24
_SIG_SYMBOL_CHARS = 16
# CA 也要收口:normalize_token_address 对非 0x/42 位的输入原样透传、不做长度校验,
# 而 CA 是"砍无可砍时也要贴上去"的那一行。Solana 44 位 / EVM 42 位,128 绰绰有余。
_SIG_CA_CHARS = 128


def _clip(s, limit: int) -> str:
    """
    任意短字段 → 单行、限长、**已转义**。

    ⚠️ 顺序必须是"叠平空白 → 截断 → 转义",与 bot._ca_clip 同一条理由:
       反过来会在截断点切断一个 `&amp;`,残缺实体照样让整条消息 400。
       叠平空白也是必需的:一个 handle 里塞几个换行就能把一行变成十行,
       绕开"按行算预算"这个前提。
    """
    flat = " ".join(str(s or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return _esc(flat)


def _fit_signal(lines: list[str], anchor: str) -> str:
    """
    整条信号消息的出口:按**整行边界**砍到预算内,锚点最后贴。

    出口不变式(无论入参是什么都成立):
      1. len(返回值) <= TRANSFER_MSG_BUDGET
      2. CA 锚点是最后一行

    ⚠️ 只按整行砍,绝不切进行内 —— 切在实体中间就是残缺实体、整条 400。
    ⚠️ 锚点最后贴,且它自己已经过 _clip 收口(≤ _SIG_CA_CHARS 转义后)——
       所以"砍到一行不剩 + 贴上锚点"这个最坏情况仍然在预算内。
    """
    kept = [ln for ln in lines if ln]
    while kept and len("\n".join([*kept, anchor])) > TRANSFER_MSG_BUDGET:
        kept.pop()
    return "\n".join([*kept, anchor])


def render_transfer_in_signal(
    *,
    network_id: str | None,
    token_address: str,
    token_symbol: str | None,
    receiver_count: int,
    receivers: list[dict],
    window_hours: int,
    buyers: list[str] | None = None,
) -> str:
    """
    「同一个币被 N 个名单成员『收到』」的告警。

    参数:
        receiver_count  窗口内合格收到者总人数(可能大于 len(receivers),下面会写"还有 N 人")
        receivers       [{"who": handle, "usd": 到账美元, "mcap": 收到时市值, "hits": 笔数}, …]
                        缺哪个键就少哪一格(铁律 2:缺失整格消失,绝不打 0 / N/A)
        buyers          名单里**真金白银买过**这个币的人。
                        ⚠️ None 与 [] 语义不同:None = 查不出来(那一行整行消失),
                           [] = 查过了、确实没人买过 —— 后者是有价值的信息,要显示。

    ⚠️ 这条消息的第一职责是**让人一眼看出这不是买入**。「收到」= 从外部钱包转进来、
       没花钱,语义上多半是项目方/内部人在分发筹码,与"他自己看好所以掏钱买"是相反的
       信号。所以标题里带「没花钱」、正文第二行再显式否定一次 —— 宁可啰嗦,
       也不能让人扫一眼当成"三个人在抢这个币"。
    ⚠️ 本函数是纯函数,不查库(铁律 7)。所有事实由 poller 取好传进来。
    """
    sym = _clip((token_symbol or "").lstrip("$"), _SIG_SYMBOL_CHARS)
    net = (network_id or "").strip()

    title = (f"{EMOJI_DISTRIBUTION} <b>筹码分发预警</b>{SEP}"
             f"<b>{receiver_count} 人「收到」同一个币</b>")
    if sym:
        title += f"{SEP}<b>${sym}</b>"
    lines = [
        title,
        f"{EMOJI_TRANSFER_IN} <b>不是买入</b> —— 是从外部钱包转进来的,他们一分钱没花",
    ]

    # ⚠️ 判空一律 is None(模块铁律):一个人也没报出金额时 total 是 None、那一段消失;
    #    而 $0.00 是有意义的真实值,不能被 `if total` 连同 None 一起吞掉。
    priced = [r["usd"] for r in receivers if r.get("usd") is not None]
    total_str = _fmt_usd(sum(priced)) if priced else None
    head = f"{EMOJI_CONSENSUS} 最近 {window_hours} 小时内 {receiver_count} 人收到"
    if total_str is not None:
        head += f"{SEP}合计 {total_str}"
    lines.append(head)

    for r in receivers:
        cells = [f"{EMOJI_COUNTERPARTY} {_clip(r.get('who') or TEXT_UNKNOWN_USER, _SIG_HANDLE_CHARS)}"]
        usd = _fmt_usd(r.get("usd"))
        if usd is not None:
            cells.append(usd)
        mc = _fmt_usd_compact(r.get("mcap"))
        if mc is not None:
            # 「收到时」这个说法只有在市值确实取自那个时刻才成立 ——
            # poller 用新鲜度闸门保证了这一点,取不到就是 None,这一格直接不出现
            cells.append(f"{EMOJI_MARKET_CAP} 收到时 {mc}")
        hits = r.get("hits")
        if hits is not None and hits > 1:
            cells.append(f"{EMOJI_TRADE_COUNT} {hits} 笔")
        lines.append(SEP.join(cells))
    omitted = receiver_count - len(receivers)
    if omitted > 0:
        lines.append(f"…还有 {omitted} 人未显示")

    # 有没有人真金白银买过 —— 有对比才有判断力
    if buyers is not None:
        if buyers:
            who = "、".join(_clip(b, _SIG_HANDLE_CHARS) for b in buyers)
            lines.append(f"{EMOJI_AMOUNT_IN} 名单里另有 {len(buyers)} 人"
                         f"<b>真金白银</b>买过:{who}")
        else:
            lines.append(f"{EMOJI_AMOUNT_IN} 名单里<b>还没有人</b>真金白银买过这个币")

    if net:
        lines.append(f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}")
    link = _links_line(_fake_ev(net, token_address))
    if link:
        lines.append(link)
    # CA 独占最后一行、纯 <code>(铁律 6)
    return _fit_signal(lines, f"<code>{_clip(token_address, _SIG_CA_CHARS)}</code>")
