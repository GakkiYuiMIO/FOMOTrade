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

# 行情多旧算冻住。与 bot._STALE_MCAP_MIN / store.SNAPSHOT_FRESH_MIN 是同一道线
STALE_MIN = 60


def esc(v) -> str:
    """⚠️ 昵称里带 '<' 并不罕见,不转义整个页面就乱了"""
    return html.escape(str(v)) if v is not None else ""


def money(v: float | None) -> str:
    if v is None:
        return ""
    return f"${v:,.2f}"


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


def stale_mark(updated_at: str | None) -> str:
    """行情太旧的标记。⚠️ 够新返回空串 —— 正常情况不该占屏"""
    if not updated_at:
        return "⚠️ 无行情"
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins = (datetime.now(UTC) - dt).total_seconds() / 60
    if mins <= STALE_MIN:
        return ""
    if mins < 60 * 24:
        return f"⚠️ 行情停在 {int(mins / 60)}h 前"
    return f"⚠️ 行情停在 {int(mins / 1440)}d 前"


def page(title: str, body: str, active: str = "") -> str:
    """整页骨架。⚠️ 不用 CDN —— 本机自用,断网也要能开"""
    nav = [("/", "看板"), ("/people", "人员"), ("/hot", "热门币"), ("/copy", "我的跟单")]
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
