"""
⚠️ 这些规则是从 formatter.py 已经防住的坑抄过来的。
   网页上重新挖开就白做了。
"""
# ruff: noqa: N802
from __future__ import annotations

from src.web import render


def test_缺失显示为空而不是零或NA():
    """
    ⚠️ formatter.py 的铁律:判空用 is None。
       amount_usd=0.0 是**有意义的真实值**(清仓就靠「剩余 $0.00」体现),
       用 if not x 会把它和 None 一起吞掉。
    """
    assert render.money(None) == ""
    assert render.money(0.0) == "$0.00", "0 是真实值,必须显示"
    assert render.mult(None) == ""
    assert render.mult(0.0) == "0.00x"


def test_倍数保留两位():
    assert render.mult(1.5) == "1.50x"
    assert render.mult(30.559) == "30.56x"


def test_市值用紧凑写法():
    assert render.mcap(1_234_567) == "$1.23M"
    assert render.mcap(45_800) == "$45.8K"
    assert render.mcap(None) == ""


def test_行情超一小时标stale():
    """⚠️ token_snapshot 对清仓的币会冻住,实测 29.6% 的快照已超 3 天"""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    assert render.stale_mark(old) != ""
    assert render.stale_mark(fresh) == ""
    assert render.stale_mark(None) != "", "完全没有行情也要标出来"


def test_转义防止昵称里的尖括号打乱页面():
    assert "&lt;script&gt;" in render.esc("<script>")


def test_币名缺失时退回合约地址前缀():
    """⚠️ symbol 缺 31.3%,不能显示空白"""
    assert render.token_label(None, "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump") == "GCa9TZ…"
    assert render.token_label("TOAD", "GCa9TZ") == "$TOAD"


def test_导航栏仅当前页高亮():
    """plan 的测试块没覆盖这条,但 nav 高亮逻辑容易在重构时悄悄错位。"""
    html_out = render.page("标题", "<p>body</p>", active="/people")
    assert '<a href="/people" class="on">' in html_out
    for href in ("/", "/hot", "/copy"):
        assert f'<a href="{href}" class="on">' not in html_out
        assert f'<a href="{href}" class="">' in html_out
