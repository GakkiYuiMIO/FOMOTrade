"""
网页版的 HTML 渲染。

⚠️ 本模块**不碰数据库**,只进数据、出字符串。
⚠️ 显示铁律(抄自 formatter.py,那里已经踩过一遍):
   1. 判空一律用 `is None`。0.0 / 0 是**有意义的真实值**,
      用 `if not x` 会把它们和缺失一起吞掉。
   2. 缺失就让那一格**空着**,不要打 "N/A" / "--" / "0"。
"""
from __future__ import annotations

import html
import math
from datetime import UTC, datetime

from src.formatter import fmt_token_age

# 行情多旧算冻住。与 bot._STALE_MCAP_MIN / store.SNAPSHOT_FRESH_MIN 是同一道线
STALE_MIN = 60


def esc(v) -> str:
    """⚠️ 昵称里带 '<' 并不罕见,不转义整个页面就乱了"""
    return html.escape(str(v)) if v is not None else ""


def money(v: float | None) -> str:
    # ⚠️ carry-over fix(Task 4 review):与 formatter.py 的 _fmt_usd 对齐 ——
    #    负号写在美元符号前面("-$12.30"),不是 "$-12.30"。
    #    Task 8 的跟单人均 PnL 列会出现大额负数(实测 -87,897 / -144,223),
    #    两种写法混用在同一页会非常显眼。
    if v is None:
        return ""
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def mult(v: float | None) -> str:
    if v is None or not math.isfinite(v):
        return ""
    return f"{v:.2f}x"


def pct(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v * 100:.0f}%"


def mcap(v: float | None) -> str:
    if v is None:
        return ""
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def token_label(symbol: str | None, ca: str) -> str:
    """⚠️ symbol 缺 31.3%。缺了显示合约前缀,不要显示空白"""
    s = (symbol or "").lstrip("$").strip()
    if s:
        return f"${s}"
    return (ca[:6] + "…") if ca else ""


def age_minutes(iso: str | None) -> float | None:
    """
    ISO 时间戳距现在过了多少分钟 —— stale_mark() 和 pages._is_frozen() 共用的
    解析逻辑(抽出来之前两处各自写了一遍完全一样的六行:解析 ISO、'Z'→'+00:00'、
    naive→UTC 补时区、算分钟差)。

    ⚠️ 本函数只负责"能不能解析、解析出来多久";缺失(None/空串)或解析失败一律
       返回 None,**不**在这里替调用方决定"缺失该怎么办"——stale_mark 和
       _is_frozen 对缺失/损坏的处理结果虽然都是"当作不新鲜",但具体动作不同
       (前者要挑不同的提示文案,后者只需要一个 bool),阈值比较也各自独立
       (STALE_MIN vs LEDGER_FREEZE_MIN),抽取范围到此为止,不要把阈值判断也搬进来。
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 60


def stale_mark(updated_at: str | None) -> str:
    """行情太旧的标记。⚠️ 够新返回空串 —— 正常情况不该占屏"""
    if not updated_at:
        return "⚠️ 无行情"
    mins = age_minutes(updated_at)
    if mins is None:
        # ⚠️ carry-over fix(Task 4 review):损坏的时间戳不能"失败即当新鲜"——
        #    那等于让一条脏数据看起来和真正的实时行情一样,而 stale 标记
        #    在跟单台账页恰恰是用户判断"这个倍数能不能信"的依据。
        return "⚠️ 时间戳异常"
    if mins <= STALE_MIN:
        return ""
    if mins < 60 * 24:
        return f"⚠️ 行情停在 {int(mins / 60)}h 前"
    return f"⚠️ 行情停在 {int(mins / 1440)}d 前"


def token_age(created_at: int | float | None) -> str:
    """
    币龄展示。⚠️ 直接复用 formatter.fmt_token_age —— 那是 Telegram 消息同一套
    算法(纯函数、不碰 DB),两处各写一份换算迟早会算出两个不一样的「币龄」。
    拿不到就是空串(铁律 2),透传 fmt_token_age 的 None,不在这里另判一次。
    """
    return fmt_token_age(created_at) or ""


def ath_bar(now_v: float | None, peak_v: float | None) -> str:
    """
    ATH 进度条:现价是峰值的百分之几。

    ⚠️ 判空用 is None —— peak_v=0 是脏数据但仍是「取到值」的真实值(见
       queries.follow_value 同类注释)。但 0 做分母算不出比例,这不是把 0
       当成缺失处理,是数学上除不了,退化成空条不崩不算错。
    """
    if now_v is None or peak_v is None or peak_v <= 0:
        return ""
    frac = max(0.0, min(1.0, now_v / peak_v))
    return (
        f'<div class=athbar><div class=athbar-fill style="width:{frac * 100:.1f}%"></div></div>'
        f'<div class=dim>现价是峰值的 {frac * 100:.0f}%</div>'
    )


def sparkline(values: list[float | None]) -> str:
    """
    迷你走势图(市值序列)。⚠️ token_price_history 刚上线,重启 + 跑够采样
    间隔前几乎全是空的 —— 少于 2 个有效点画不出趋势线,这里给一句安静的
    提示,绝不能拿假数据填,也不能留一个空 <svg> 看着像坏掉了。
    """
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return '<p class="note spark-empty">还没有足够的行情历史</p>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0  # 全部相同时避免除零,画一条水平线而不是报错
    w, h, pad = 120, 28, 2
    step = (w - 2 * pad) / (len(pts) - 1)
    coords = " ".join(
        f"{pad + i * step:.1f},{pad + (h - 2 * pad) * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(pts)
    )
    return f'<svg class=spark viewBox="0 0 {w} {h}" preserveAspectRatio="none"><polyline points="{coords}"/></svg>'


def page(title: str, body: str, active: str = "") -> str:
    """整页骨架。⚠️ 不用 CDN —— 本机自用,断网也要能开"""
    nav = [("/", "信号"), ("/board", "看板"), ("/people", "人员"),
           ("/hot", "热门币"), ("/copy", "我的跟单")]
    links = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{esc(label)}</a>'
        for href, label in nav
    )
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · FOMO 监控</title>"
        '<link rel=stylesheet href="/static/app.css"></head><body>'
        f"<nav>{links}</nav><main>{body}</main></body></html>"
    )
