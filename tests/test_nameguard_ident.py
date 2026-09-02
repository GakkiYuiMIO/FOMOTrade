"""
symbol / handle 的**轻门禁**(src/nameguard.safe_ident)与它在推送里的落点。

============ 这个文件为什么存在 ============
上一轮把 symbol / handle 登记成"已审查、只走 _clip",于是门禁在一头掐掉的出口
在旁边被原样打开 —— 三个复验者亲手打出来的真实渲染结果:

    render(token_symbol='t.me/pumpgrp')      → 🌱 <b>maxpain</b> · 首次建仓 · <b>$t.me/pumpgrp</b>
    render(pool_quote_symbol='discord.gg/x') → 🌊 底池 · discord.gg/x · 「USA Rare Earth, Inc.」
    ev.handle = 't.me/scam'                  → 🌱 <b>t.me/scam</b> · 首次建仓 · <b>$X</b>

symbol 与 handle 同样是**攻击者可控**的:谁都能给自己发的币起符号、给自己起用户名。

============ 为什么是"轻"门禁而不是 safe_display ============
符号天生长得怪:全大写、带数字、超短、带 emoji、带方括号。套完整的形状规则
(长度 / 词数 / 标点数)会把正经符号与昵称大面积误伤。所以只留**形态**那半:
scheme / 域名 / @提及 / 0x 地址 / 裸 hex / base58 / IPv4 / bidi 控制符。

============ 实测丢弃率(2026-09-02,只读生产库)============
token_symbol   1 / 2434 = 0.04%   —— 被丢的那一条是 '@everyone'
handle + 昵称  0 / 208  = 0.00%
⚠️ 域名那条对 ident 比 safe_display **松一格**:只拦"带路径的域名"与"TLD 是真 TLD"
   的域名。理由是实测里 'eric.eth' / 'Dylan.Eth' / 'sol.engineer' / 'Bull.Path 🫵😹' /
   'Hungr.AI' / 'Mr.CZ Punks' 全是真实存在的昵称与符号(ENS 名在这个圈子里就是人名),
   而它们在 Telegram 里既不会被自动变成链接、也没有任何目标可达。
   按 safe_display 那条口径,handle 的丢弃率是 1.92%(4/208),超了 1% 的线。

⚠️ 断言写死字面量,不从被测模块 import 任何表 / 正则 / 阈值。全部离线。
"""
# ruff: noqa: N802, RUF001
from __future__ import annotations

import pytest

from src.formatter import render, render_pump_trade, render_transfer_in_signal
from src.models import BADGE_FIRST, EVENT_BUY
from src.nameguard import safe_ident

from .conftest import make_event

CA = "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"
ZWSP = "​"
RLO = "‮"

# ============================================================
# 硬基线:必须拦住(每一条都是"读者能拿到一个目标"的形态)
# ============================================================
_IDENT_BLOCK = [
    ("t.me/pumpgrp", "带路径的域名 —— 复验者拿它当 token_symbol 打进了标题"),
    ("t.me/scam", "带路径的域名 —— 复验者拿它当 handle 打进了标题"),
    ("discord.gg/x", "带路径的域名 —— 复验者拿它当 pool_quote_symbol 打进了 🌊 行"),
    ("t.me", "TLD 'me' 是真 TLD"),
    ("www.evil-airdrop.com", "TLD 'com'"),
    ("discord.gg", "TLD 'gg'"),
    ("pump.fun", "TLD 'fun'"),
    ("bit.ly", "TLD 'ly'"),
    ("tme-scam.io", "TLD 'io'"),
    ("t. me/scam", "NBSP 拆开之后仍是带路径的域名"),
    ("tg://resolve?domain=scam", "scheme"),
    ("javascript:alert(1)", "scheme(单冒号)"),
    ("@everyone", "@提及 —— 实测 2434 个真实符号里唯一被丢的那条"),
    ("0xabcd1234", "0x 地址"),
    ("0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18", "完整 EVM 地址"),
    ("CTPoyCwkjMvoJwU4xvZZqoD8xJvHnLDfmVGGgQnRpump", "base58 ≥26"),
    ("7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18", "裸 hex ≥16"),
    ("192.168.1.1", "IPv4"),
    (f"CU{RLO}M", "bidi 控制符:来过就整段丢弃(与币名那条同一口径)"),
]

# ============================================================
# 硬基线:必须放行(全部来自生产库的真实 symbol / handle / 昵称)
# ============================================================
_IDENT_PASS = [
    "CUM", "USAR", "NVDA", "$WIF", "1000X", "PEACH64", "Demo3x", "GOGL",
    "AI", "X", "牛来", "喵喵币",
    # 下面这一批是**真实的**、按 safe_display 那条口径会被误杀的:
    "Mr.CZ Punks", "WAR.DOCX", "Hungr.AI",
    "eric.eth", "Dylan.Eth", "sol.engineer", "Bull.Path 🫵😹",
    # 昵称里的 emoji / 组合 emoji / 方括号都必须原样留着
    "血手人屠·厉飞雨", "alice 👨‍💻", "[VIP] trader", "Ω" * 30,
]


@pytest.mark.parametrize(("raw", "why"), _IDENT_BLOCK, ids=[b[1] for b in _IDENT_BLOCK])
def test_必须拦住的形态(raw, why):
    assert safe_ident(raw) is None, f"{raw!r} 本该被拦({why})"


@pytest.mark.parametrize("raw", _IDENT_PASS)
def test_必须放行的真实符号与昵称(raw):
    assert safe_ident(raw) == raw, raw


def test_通过的原样返回不做任何清洗():
    """
    ⚠️ 这道门只回答"给不给显示"。清洗(删控制符)与限长是下游 _clip 的既有职责 ——
       在这里顺手清洗会让两边各清一次,而 `&` 转义两次就会显示成一串乱码。
    """
    assert safe_ident(f"ali{ZWSP}ce") == f"ali{ZWSP}ce"
    assert safe_ident("  CUM  ") == "  CUM  "


def test_空与缺失为None():
    for s in (None, "", "   ", ZWSP):
        assert safe_ident(s) is None, repr(s)


def test_不施加形状规则():
    """
    ⚠️⚠️ 这条是"轻"字的可执行形式:下面每一条都过不了 safe_display 的形状白名单
       (长度 / 词数 / 标点数 / 数字总量 / 表情),但它们是**正常的符号与昵称**。
       谁把 safe_ident 换成 safe_display,这条当场红,而生产库里 4% 的昵称会消失。
    """
    # ⚠️ 长的那条用 "OIl" 三个字母:它们**不在** base58 字母表里,也不是 hex ——
    #    否则测到的是地址形态那条(那条对 ident 仍然生效),不是"没有长度上限"。
    for raw in ("OIl" * 20, "one two three four five six seven", "!!!???...,,,",
                "13800138000", "🔴🟢🟡", "已清仓 · 亏损 99%"):
        assert safe_ident(raw) == raw, raw


# ============================================================
# 落点:同一行上的 symbol / handle 真的进不了推送
# ============================================================
def _ev(**kw):
    base = dict(handle="maxpain", token_symbol="CUM", token_address=CA,
                network_id="robinhood", badge=BADGE_FIRST)
    base.update(kw)
    return make_event(EVENT_BUY, **base)


def test_坏symbol让标题少那一段而不是印出来():
    """⚠️ 命中 → 按既有"字段缺失"规矩:标题没有 $符号 那一段,绝不打占位符。"""
    msg = render(_ev(token_symbol="t.me/pumpgrp"), token_name="Cummingtonite")
    head = msg.split("\n")[0]
    assert "t.me" not in head, head
    assert head == "🌱 <b>maxpain</b> · 首次建仓 · 「Cummingtonite」", head


def test_坏handle让展示名退回未知用户():
    """⚠️ handle 不合格 → 退回 user_id,再退回"未知用户"(既有的缺失规矩)。"""
    ev = _ev(handle="t.me/scam", user_id="")
    head = render(ev).split("\n")[0]
    assert "t.me" not in head, head
    assert head.startswith("🌱 <b>未知用户</b>"), head


def test_坏对手符号让底池行只剩全名而股票行整行消失():
    """
    ⚠️ 🌊 行:符号那一段消失,公司全名照常(两段各自独立)。
    ⚠️ 🏢 行:它的第一段就是符号,符号没了整行消失 —— 绝不退化成打一个地址。
    """
    msg = render(_ev(), pool_quote_symbol="discord.gg/x",
                 pool_quote_name="USA Rare Earth, Inc.",
                 stock_company_zh="美国稀土公司", stock_exchange="NasdaqGM")
    lines = msg.split("\n")
    assert "discord.gg" not in msg, msg
    assert "🌊 底池 · 「USA Rare Earth, Inc.」" in lines, lines
    assert not any(ln.startswith("🏢") for ln in lines), lines


def test_pump用户名与转入告警的符号也过同一道门():
    """⚠️ 四个渲染函数走的是同一张表,这里各抽一条证明落点没漏。"""
    msg = render_pump_trade(username="t.me/scam", side="buy", token_symbol="t.me/pumpgrp",
                            coin_mint=CA, amount_usd=120.0, network_id="robinhood",
                            token_name="Cummingtonite")
    assert "t.me" not in msg, msg

    msg2 = render_transfer_in_signal(network_id="robinhood", token_address=CA,
                                     token_symbol="www.evil-airdrop.com",
                                     receiver_count=3, receivers=[], window_hours=24,
                                     token_name="Cummingtonite")
    assert "evil-airdrop" not in msg2, msg2


def test_正常的符号与昵称一个都不许丢():
    """⚠️ 对照组:轻门禁不能把正经推送打残。"""
    head = render(_ev(handle="eric.eth", token_symbol="1000X"),
                  token_name="Cummingtonite").split("\n")[0]
    assert head == "🌱 <b>eric.eth</b> · 首次建仓 · <b>$1000X</b> · 「Cummingtonite」", head
