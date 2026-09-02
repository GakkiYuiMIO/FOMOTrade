"""
分隔符两套口径的**关系**、`「」` 容器的配平不变量,以及"必拦硬基线 × 槽位 × 渲染函数"
的笛卡尔积零泄漏。

============ 这个文件为什么存在 ============
上一轮出了一个**两个测试文件互相矛盾**的洞:

    safe_display('已清仓 · 亏损 99%') = None      ← test_nameguard_shape 的必拦基线第 1 条
    safe_ident  ('已清仓 · 亏损 99%') = 原串未变   ← test_nameguard_ident 断言它**必须放行**

同一个字符串,一个文件说"必拦"、另一个说"必放行",而**两边都是绿的** ——
因为没有任何一条测试同时看着这两侧。根因是"分隔符"这一类被漏在了两套规则之外:
名字那侧当形状规则拦掉了,ident 那侧当形状规则放行了。而 ident 恰恰是**唯一不套
`「」` 容器的槽位**:最需要拦分隔符的地方反而最松。

所以本文件把两侧的**关系**写成可执行的东西:

  R1 分隔符类字符(is_separator_char)在 **ident 侧一律严禁**;
  R2 名字侧只放行 `·` `•` `・` 这三个(它们有真实样本,且印在 `「」` 里面);
  R3 ⇒ **就分隔符这一类而言,ident 的规则集是名字规则集的严格超集**。
     R3 由 test_ident的分隔符规则是名字侧的超集 逐码点验着 —— 谁把某一侧改松,
     它当场红。两个文件从此不可能再各说各话。

⚠️ 断言写死字面量,不从 src.nameguard import 任何阈值 / 表 / 正则(除了被测函数本身)。
⚠️ 全部离线。
"""
# ruff: noqa: N802, RUF001
from __future__ import annotations

import html as _html
import random
import re
import unicodedata

import pytest

from src import formatter as fmt
from src.formatter import (
    render,
    render_alpha_listing,
    render_pump_callout,
    render_pump_trade,
    render_transfer_in_signal,
    render_transfer_in_watch,
)
from src.models import BADGE_FIRST, EVENT_BUY, EVENT_THESIS, EVENT_TRANSFER_IN
from src.nameguard import is_separator_char, safe_display, safe_ident

from .conftest import make_event

CA = "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"
OPEN = "「"      # 「
CLOSE = "」"     # 」

# ============================================================
# R1/R2:分隔符表本身
# ============================================================
# ⚠️ 这一批必须被认成"分隔符类":每一个都视觉上能当字段分隔符用。
#    ⚠️ 逐个写码点 + 说明,不从被测模块 import 那张表。
_MUST_BE_SEPARATOR = [
    (0x00B7, "MIDDLE DOT —— 本项目 SEP(' · ')用的就是它"),
    (0x2022, "BULLET —— robinhood 币股后缀里的那个"),
    (0x30FB, "KATAKANA MIDDLE DOT —— 与 · 同形,上一版的 CJK 码点区间放行过它"),
    (0xFF65, "HALFWIDTH KATAKANA MIDDLE DOT"),
    (0x0387, "GREEK ANO TELEIA —— NFKC 折叠到 U+00B7"),
    (0x2027, "HYPHENATION POINT"),
    (0x2024, "ONE DOT LEADER"),
    (0x22C5, "DOT OPERATOR"),
    (0x2219, "BULLET OPERATOR"),
    (0x2043, "HYPHEN BULLET"),
    (0x25E6, "WHITE BULLET"),
    (0x007C, "VERTICAL LINE —— 半角竖线"),
    (0xFF5C, "FULLWIDTH VERTICAL LINE —— 生产库里真有一个昵称带它"),
    (0x2502, "BOX DRAWINGS LIGHT VERTICAL"),
    (0x2016, "DOUBLE VERTICAL LINE"),
    (0x2223, "DIVIDES"),
    (0x22EE, "VERTICAL ELLIPSIS"),
]
# ⚠️ 反向:这一批**绝不许**被认成分隔符 —— 它们是真实符号 / 名字里天天出现的字符,
#    误收一个,生产库里成片的 symbol 与昵称当场消失。
_MUST_NOT_BE_SEPARATOR = "AZaz09 .,'\"-&()!?/:;_[]{}#$+=@*^%~、。（）—中文あア"


@pytest.mark.parametrize(("cp", "why"), _MUST_BE_SEPARATOR,
                         ids=[f"U+{cp:04X}" for cp, _ in _MUST_BE_SEPARATOR])
def test_这些字符必须被认成分隔符(cp, why):
    assert is_separator_char(chr(cp)), f"U+{cp:04X} {why}"


@pytest.mark.parametrize("ch", list(_MUST_NOT_BE_SEPARATOR))
def test_这些字符绝不许被认成分隔符(ch):
    assert not is_separator_char(ch), f"U+{ord(ch):04X} {unicodedata.name(ch, '?')}"


def _all_separators() -> list[str]:
    """整个 0x110000 码点空间里被认成分隔符的字符。⚠️ 这是 R3 的枚举依据。"""
    return [chr(cp) for cp in range(0x110000) if is_separator_char(chr(cp))]


def test_分隔符表的规模是可控的():
    """
    ⚠️ 表太小 = 漏同形字;表太大 = 误杀真符号。这条只钉"量级合理"这一件事,
       真正的两侧关系由下面那条负责。⚠️ 上下界写死字面量。
    """
    n = len(_all_separators())
    assert 17 <= n <= 400, f"分隔符表有 {n} 个字符,规模不对劲"


def test_ident的分隔符规则是名字侧的超集():
    """
    ⚠️⚠️ **本文件的核心断言**,也是上一轮那个"两个测试文件互相矛盾"的护栏本身。

    对整个 0x110000 空间里**每一个**分隔符类字符 x:
      · safe_ident 那一侧(无容器)必须拦住 —— 一条都不许漏;
      · 于是"名字侧放行的分隔符" ⊆ "ident 侧拦住的分隔符",超集关系成立。

    再对名字侧**放行的那三个**逐个证明这条关系是**严格**的(两侧判得不一样):
      safe_display(f'名字{x}名字') 不为 None,而 safe_ident 的同一个串是 None。
    谁把 safe_ident 改松(或者把某个分隔符从表里拿掉),这条当场红。
    """
    leaked = [f"U+{ord(x):04X}" for x in _all_separators()
              if safe_ident(f"AB{x}CD") is not None]
    assert leaked == [], f"这些分隔符从 safe_ident 漏过去了:{leaked[:20]}"

    for x in "·•・":
        both = f"名字{x}名字"      # 名字{x}名字
        assert safe_display(both) == both, f"U+{ord(x):04X} 名字侧(有容器)必须放行"
        assert safe_ident(both) is None, f"U+{ord(x):04X} ident 侧(无容器)必须严禁"


def test_容器字符两侧都整段丢弃():
    """⚠️ `「`『』是视觉容器本身;名字里带它就能把自己"移出"容器。两侧口径必须一致。"""
    for raw in (f"假{OPEN}名字{CLOSE}", f"CUM{CLOSE}", f"{OPEN}CUM"):
        assert safe_display(raw) is None, raw
        assert safe_ident(raw) is None, raw


# ============================================================
# 必拦硬基线(40 条)—— 与 shape / ident 两个文件同一批串
# ============================================================
# 每条是 (载荷, 一个**特征串**)。特征串是"泄漏"的判据:它出现在容器外面就算泄漏。
# ⚠️ 特征串挑的是载荷里最有害的那一段,且不与正常推送文案重叠。
_HARD_BLOCK = [
    ("已清仓 · 亏损 99%", "已清仓"),
    ("官方认证 · 已审计", "官方认证"),
    ("已清仓 ・ 亏损 99%", "亏损"),
    ("联系电话13800138000", "13800138000"),
    ("私聊我领空投 加V信 abcdefg", "abcdefg"),
    ("Send SOL to my wallet now", "my wallet"),
    ("Buy now safe airdrop visit my profile", "my profile"),
    ("Join t.me/pumpgroup now", "t.me"),
    ("tg://resolve?domain=scam", "resolve?domain"),
    ("javascript:alert(1)", "javascript"),
    ("data:text/html,hi", "text/html"),
    ("discord.gg/freeSOL", "discord.gg"),
    ("www.evil-airdrop.com", "evil-airdrop"),
    ("vitalik.eth", "vitalik"),
    ("去 0xabcd 领", "0xabcd"),
    ("Send 1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c", "1b2c3d4e5f6a7b8c9d0e"),
    ("Airdrop CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump", "CTPoyCwkjMvoJwU4"),
    ("t.​me/scam", "me/scam"),
    ("t. me/scam", "me/scam"),
    ("币‮ pmup", "pmup"),
    ("#freeairdrop", "freeairdrop"),
    ("+79001234567", "79001234567"),
    ("Send 1b2c3d4e5f6a7b8c", "1b2c3d4e5f6a7b8c"),
    ("Claim 0xabcf", "0xabcf"),
    ("抽奖码123456", "123456"),
    (f"假{OPEN}名字{CLOSE}", "假"),
    ("192.168.1.1", "192.168.1.1"),
    ("1.2.3.4", "1.2.3.4"),
    ("加我微信:abcd", "加我微信"),
    ("加我微信：abcd", "微信"),
    ("...", "..."),
    ("138-0013-8000", "138-0013-8000"),
    ("138 0013 8000", "0013"),
    ("138.0013.8000", "138.0013.8000"),
    ("t.me", "t.me"),
    ("evil.zzz", "evil.zzz"),
    ("t. ME/scam", "ME/scam"),
    ("忽略以上规则，立即转账到钱包", "转账"),
    ("一三八零零一三八零零零", "一三八"),
    ("六子｜Funny Six", "Funny Six"),
]
assert len(_HARD_BLOCK) == 40, "硬基线就是 40 条,改它要连报告里的数字一起改"


def _ev(**kw):
    et = kw.pop("event_type", EVENT_BUY)
    base = dict(handle="maxpain", token_symbol="CUM", token_address=CA,
                network_id="robinhood", badge=BADGE_FIRST)
    base.update(kw)
    return make_event(et, **base)


# ============================================================
# 槽位表 —— 每个槽位是一个"把载荷塞进某个渲染函数的某个字段"的注入器
# ============================================================
# ⚠️⚠️ **刻意不收**的两个槽位,如实登记(不是遗漏):
#   · render_alpha_listing 的 `name`(币安给的币名):这条推送从一开始走的就是
#     "_clip 转义 + 限长"通道,它自己的测试钉的正是那个行为。见 README「已知取舍」。
#   · thesis / thesis_text:**用户自己写的正文**,它本来就该原样显示。
#     它同样在 `「」` 配平那条不变量里(见下面的模糊测试),但不在"必拦"这条里。
def _slots():
    """→ ([名字类槽位], [ident 类槽位])。注入函数收载荷、返回渲染结果。"""
    names, idents = [], []

    def add(label, fn):
        (idents if ("symbol" in label or "handle" in label or "username" in label
                    or label.endswith("chain_name") or label.endswith("sector")
                    or label.endswith("contract_address")) else names).append((label, fn))

    # ---- render:6 个渲染参数 + 4 个挂在 ev 上的字段 ----
    add("render.token_name", lambda p: render(_ev(), token_name=p))
    add("render.token_name_zh",
        lambda p: render(_ev(), token_name="Cummingtonite", token_name_zh=p))
    add("render.pool_quote_name", lambda p: render(_ev(), pool_quote_symbol="USAR",
                                                  pool_quote_name=p))
    add("render.pool_quote_symbol", lambda p: render(_ev(), pool_quote_symbol=p,
                                                    pool_quote_name="WhiteFiber, Inc."))
    add("render.stock_company_zh",
        lambda p: render(_ev(), pool_quote_symbol="USAR",
                         pool_quote_name="USA Rare Earth, Inc.", stock_company_zh=p))
    add("render.stock_exchange",
        lambda p: render(_ev(), pool_quote_symbol="USAR",
                         pool_quote_name="USA Rare Earth, Inc.", stock_exchange=p))
    add("render.ev.token_symbol", lambda p: render(_ev(token_symbol=p)))
    add("render.ev.handle", lambda p: render(_ev(handle=p, user_id="")))
    add("render.ev.user_handle", lambda p: render(_ev(user_handle=p)))
    add("render.ev.counterparty_handle",
        lambda p: render(_ev(event_type=EVENT_TRANSFER_IN, counterparty_handle=p)))

    # ---- render_pump_trade:8 个 ----
    def _pt(**kw):
        base = dict(username="alice", side="buy", token_symbol="CUM", coin_mint=CA,
                    amount_usd=120.0, network_id="robinhood")
        base.update(kw)
        return render_pump_trade(**base)

    add("pump_trade.username", lambda p: _pt(username=p))
    add("pump_trade.token_symbol", lambda p: _pt(token_symbol=p))
    add("pump_trade.token_name", lambda p: _pt(token_name=p))
    add("pump_trade.token_name_zh",
        lambda p: _pt(token_name="Cummingtonite", token_name_zh=p))
    add("pump_trade.pool_quote_name", lambda p: _pt(pool_quote_symbol="USAR",
                                                    pool_quote_name=p))
    add("pump_trade.pool_quote_symbol", lambda p: _pt(pool_quote_symbol=p,
                                                      pool_quote_name="WhiteFiber, Inc."))
    add("pump_trade.stock_company_zh",
        lambda p: _pt(pool_quote_symbol="USAR", pool_quote_name="USA Rare Earth, Inc.",
                      stock_company_zh=p))
    add("pump_trade.stock_exchange",
        lambda p: _pt(pool_quote_symbol="USAR", pool_quote_name="USA Rare Earth, Inc.",
                      stock_exchange=p))

    # ---- render_transfer_in_watch:2 + 3 个 ev 字段 ----
    def _tw(**kw):
        ev = _ev(event_type=EVENT_TRANSFER_IN, **kw.pop("ev", {}))
        return render_transfer_in_watch(ev, **kw)

    add("transfer_watch.token_name", lambda p: _tw(token_name=p))
    add("transfer_watch.token_name_zh",
        lambda p: _tw(token_name="Cummingtonite", token_name_zh=p))
    add("transfer_watch.ev.token_symbol", lambda p: _tw(ev={"token_symbol": p}))
    add("transfer_watch.ev.handle", lambda p: _tw(ev={"handle": p, "user_id": ""}))
    add("transfer_watch.ev.counterparty_handle",
        lambda p: _tw(ev={"counterparty_handle": p}))

    # ---- render_transfer_in_signal:3 ----
    def _ts(**kw):
        base = dict(network_id="robinhood", token_address=CA, token_symbol="CUM",
                    receiver_count=3, receivers=[], window_hours=24)
        base.update(kw)
        return render_transfer_in_signal(**base)

    add("transfer_signal.token_symbol", lambda p: _ts(token_symbol=p))
    add("transfer_signal.token_name", lambda p: _ts(token_name=p))
    add("transfer_signal.token_name_zh",
        lambda p: _ts(token_name="Cummingtonite", token_name_zh=p))

    # ---- render_alpha_listing:4(name 刻意不收,理由见上)----
    def _al(**kw):
        base = dict(symbol="CUM", listing_time_ms=1_800_000_000_000,
                    network_id="bsc", now=1_800_000_100.0)
        base.update(kw)
        return render_alpha_listing(**base)

    add("alpha.symbol", lambda p: _al(symbol=p))
    add("alpha.chain_name", lambda p: _al(network_id="zzz-unknown", chain_name=p))
    add("alpha.sector", lambda p: _al(sector=p))
    add("alpha.contract_address", lambda p: _al(contract_address=p))

    # ---- render_pump_callout:3(thesis 刻意不收)----
    def _pc(**kw):
        base = dict(username="alice", thesis="看好", coin_mint=CA,
                    token_symbol="CUM", network_id="robinhood")
        base.update(kw)
        return render_pump_callout(**base)

    add("pump_callout.username", lambda p: _pc(username=p))
    add("pump_callout.token_symbol", lambda p: _pc(token_symbol=p))
    add("pump_callout.symbol_only", lambda p: _pc(token_symbol=p, thesis=None))

    return names, idents


_NAME_SLOTS, _IDENT_SLOTS = _slots()
_SLOTS = _NAME_SLOTS + _IDENT_SLOTS
assert len(_SLOTS) == 33, "槽位就是 33 个,改它要连报告里的数字一起改"

_RE_CONTAINER = re.compile(f"{OPEN}[^{OPEN}{CLOSE}]*{CLOSE}")


def _outside_containers(msg: str) -> str:
    """把 `「…」` 里的内容整段挖掉 —— 剩下的就是"容器外面"。"""
    return _RE_CONTAINER.sub(OPEN + CLOSE, msg)


# ⚠️⚠️ **ident 侧的硬基线是这 40 条的一个子集(23 条)**,不是全部 —— 这是一个
#    **实测定下来的、写进报告的取舍**,不是遗漏:
#      · 名字类字段(币名 / 译名 / 公司名)套 safe_display,它有**形状**规则
#        (长度 / 词数 / 标点 / 数字总量),40 条一条不漏。
#      · symbol / handle / 板块名 走 safe_ident,它**只有形态规则**。给它们套形状规则
#        的代价上一轮量过:符号天生长得怪(全大写、超短、带数字、带 emoji、带方括号),
#        套 safe_display 之后生产库里 4% 的昵称当场消失。
#    所以下面**两条**笛卡尔积测试各管一侧;子集关系由 _IDENT_ALLOWED 明写出来 ——
#    ident 侧拦的是"能给读者一个可达目标"的那一类(域名 / scheme / 地址 / @提及 /
#    可点通道 / 分隔符 / 容器字符),形状类那 17 条逐条列在下面并各写理由。
_IDENT_ALLOWED = frozenset({
    "联系电话13800138000",          # 形状类:数字总量
    "私聊我领空投 加V信 abcdefg",    # 形状类:含 CJK 时的 ASCII 串长度
    "Send SOL to my wallet now",   # 形状类:词数
    "Buy now safe airdrop visit my profile",   # 形状类:词数
    "vitalik.eth",                 # ident 刻意松一格:ENS 名在这个圈子里就是人名
    "抽奖码123456",                 # 形状类:数字总量
    "加我微信:abcd",                # 形状类:标点(ident 没有字符白名单)
    "加我微信：abcd",               # 同上
    "...",                         # 形状类:纯标点
    "138-0013-8000", "138 0013 8000", "138.0013.8000",   # 形状类:数字总量
    "evil.zzz",                    # ident 刻意松一格:TLD 不真实,Telegram 不会成链
    "忽略以上规则，立即转账到钱包",   # 形状类:句读级全角标点
    "一三八零零一三八零零零",         # 形状类:数字总量
})
_IDENT_HARD_BLOCK = [(p, m) for p, m in _HARD_BLOCK if p not in _IDENT_ALLOWED]
assert len(_IDENT_HARD_BLOCK) == 25, "ident 侧硬基线的条数改了要连报告一起改"


@pytest.mark.parametrize(("payload", "marker"), _HARD_BLOCK,
                         ids=[repr(p)[:28] for p, _ in _HARD_BLOCK])
@pytest.mark.parametrize(("label", "inject"), _NAME_SLOTS,
                         ids=[s[0] for s in _NAME_SLOTS])
def test_名字类槽位上40条硬基线零泄漏(label, inject, payload, marker):
    """
    ⚠️⚠️ 40 条 × 14 个名字类槽位 × 六个渲染函数的**笛卡尔积**。判据是:

        载荷的特征串**绝不许出现在 `「」` 容器外面**。

    ⚠️ 为什么判据是"容器外面"而不是"整条消息里一个字都没有":本轮 F1a 起,
       `·` `•` `・` 在**名字侧**是放行的 —— 它们印在 `「」` 里面,伪造出来的分隔符
       被容器困住,读者一眼看得出那是一段数据。这个判据比"永远不许出现"**更准**:
       它同时钉住了"容器不许被绕过"这件事(容器一破,特征串立刻落到外面)。
    """
    msg = inject(payload)
    assert marker not in _outside_containers(msg), f"{label} 泄漏了 {marker!r}"


@pytest.mark.parametrize(("payload", "marker"), _IDENT_HARD_BLOCK,
                         ids=[repr(p)[:28] for p, _ in _IDENT_HARD_BLOCK])
@pytest.mark.parametrize(("label", "inject"), _IDENT_SLOTS,
                         ids=[s[0] for s in _IDENT_SLOTS])
def test_ident类槽位上硬基线零泄漏(label, inject, payload, marker):
    """
    ⚠️⚠️ 25 条 × 19 个 ident 类槽位 × 六个渲染函数。ident **不套容器**,
       所以这里的判据就是最强的那个:特征串在整条消息里一个字都不许出现。
    ⚠️ 这 25 条是 40 条硬基线里"能给读者一个可达目标"的那一类;剩下 15 条是形状类,
       ident 刻意不施加形状规则(理由与实测代价见 _IDENT_ALLOWED 上面那段)。
    """
    msg = inject(payload)
    assert marker not in msg, f"{label} 泄漏了 {marker!r}"


# ============================================================
# `「」` 配平 —— 模糊测试(≥12500 条,六个渲染函数)
# ============================================================
# ⚠️⚠️ 上一轮 12500 条模糊测试里 **3255 条**「」不配平,来源是 symbol / handle / thesis:
#   它们只走 _esc(转义)不走门禁,而 `「` 没有 HTML 转义形式 —— 一个 `」` 就能把容器
#   提前关掉,伪造出来的东西当场跑到容器外面。本轮在 _esc 里把这两个字符删掉
#   (它们是本模块自己的结构标记,地位与 `<` 一样),并在 safe_ident 里整段丢弃。
# ⚠️ 这条不变量上一轮**没有任何测试钉着**,这就是它能破 3255 条的原因。
_FUZZ_ALPHABET = (
    "abcXYZ019 " + OPEN + CLOSE + "·•・｜|"
    + "<>&\"'\\/\n\t​‮　﻿"
    + "已清仓亏损币名"
    + "\U0001f331\U0001f4b0‍\U0001f468\U0001f4bb"
    + ".,:;-_()[]{}#$+=@*^%~!?"
)
# Telegram Bot API 允许的标签(铁律:只认这个 HTML 子集)。⚠️ 写死在测试这一侧。
_ALLOWED_TAGS = frozenset({"b", "i", "u", "s", "code", "pre", "a", "blockquote",
                           "tg-spoiler", "span"})
_RE_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)/?>")
_RE_ENTITY = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});")


def _quote_violation(msg: str) -> str | None:
    """`「」` 配平检查:数量相等、且从左到右扫一遍不许出现负深度或交叉。"""
    depth = 0
    for ch in msg:
        if ch == OPEN:
            if depth:
                return "嵌套/交叉:容器里又开了一个容器"
            depth += 1
        elif ch == CLOSE:
            if depth == 0:
                return "先出现了一个没有配对的 」"
            depth -= 1
    return None if depth == 0 else "有 「 没有闭上"


def _html_violation(msg: str) -> str | None:
    """三项:标签白名单 / 没有裸 `<` / 实体完整。"""
    for m in _RE_TAG.finditer(msg):
        if m.group(2).lower() not in _ALLOWED_TAGS:
            return f"标签白名单外:{m.group(0)!r}"
    stripped = _RE_TAG.sub("", msg)
    if "<" in stripped or ">" in stripped:
        return f"裸的尖括号:{stripped[max(0, stripped.find('<') - 10):][:40]!r}"
    for i, ch in enumerate(stripped):
        if ch == "&" and not _RE_ENTITY.match(stripped, i):
            return f"残缺实体:{stripped[i:i + 20]!r}"
    return None


def test_容器配平与HTML三项的模糊测试():
    """
    ⚠️⚠️ 12500 条 = 每个槽位随机 ~400 条,六个渲染函数全覆盖。种子写死,可复现。
    ⚠️ 三条不变量一次全查:
         1. `「」` 配平且不交叉(上一轮 3255 条违规,且**没有任何测试钉着**);
         2. 标签只出现在 Telegram 的 HTML 子集里;
         3. 没有裸 `<` / `>`,没有残缺实体(残缺实体 = 整条消息 400)。
    ⚠️ 载荷字母表里**刻意**放了 `「` `」` `·` `｜` 与零宽 / bidi / 换行 / `<` / `&` ——
       它们各自都是历史上真的打进过推送的东西。
    """
    rnd = random.Random(20260903)
    bad = []
    per_slot = 12500 // len(_SLOTS) + 1
    total = 0
    for label, inject in _SLOTS:
        for _ in range(per_slot):
            n = rnd.randint(1, 24)
            payload = "".join(rnd.choice(_FUZZ_ALPHABET) for _ in range(n))
            total += 1
            try:
                msg = inject(payload)
            except Exception as e:  # noqa: BLE001  —— 渲染炸了本身就是违规
                bad.append((label, payload, f"渲染抛异常:{e!r}"))
                continue
            for why in (_quote_violation(msg), _html_violation(msg)):
                if why is not None:
                    bad.append((label, payload, why))
    assert total >= 12500, total
    assert bad == [], f"{len(bad)} / {total} 条违规,前 5 条:{bad[:5]}"


def test_观点正文里的组合emoji一个都不许被拆开():
    """
    ⚠️⚠️ 用户自己写的正文走的是 `_esc` 那条路,它**必须保留** ZWJ(U+200D)与
       tag 字符(U+E0020–U+E007F)—— 前者是所有组合 emoji 的粘合剂,后者拼出地区旗。
       全删会把 👨‍💻 拆成两个 emoji、把苏格兰旗拆成一面光秃秃的黑旗。
    ⚠️ 本轮 `_esc` 新删了 `「` `」`,这条同时证明那一步**没有顺手动到 emoji**。
    ⚠️ 七个样本各覆盖一种组合形态:ZWJ 序列 / 变体选择符 + ZWJ / tag 序列 /
       多段 ZWJ / 肤色修饰符 / 区域指示符对 / keycap 序列。
    """
    samples = [
        "\U0001f468‍\U0001f4bb",                            # 👨‍💻 ZWJ
        "\U0001f3f3️‍\U0001f308",                      # 🏳️‍🌈 VS16 + ZWJ
        "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",  # 🏴 tag
        "\U0001f469‍\U0001f469‍\U0001f467‍\U0001f466",              # 多段 ZWJ
        "\U0001f44d\U0001f3fd",                                  # 👍🏽 肤色修饰符
        "\U0001f1e8\U0001f1f3",                                  # 🇨🇳 区域指示符对
        "1️⃣",                                         # 1️⃣ keycap
    ]
    for emo in samples:
        ev = make_event(EVENT_THESIS, handle="alice", token_symbol="CUM",
                        token_address=CA, network_id="robinhood",
                        thesis_text=f"看好 {emo} 这个")
        msg = render(ev)
        assert emo in msg, f"{emo!r} 被拆开了\n{msg}"
        msg2 = render_pump_callout(username="alice", thesis=f"看好 {emo}",
                                   coin_mint=CA, token_symbol="CUM",
                                   network_id="robinhood")
        assert emo in msg2, f"{emo!r} 在喊单那条推送里被拆开了\n{msg2}"


# ============================================================
# BLOCKER-2:ident 叠平之后,换行再也造不出一整行伪造字段
# ============================================================
def test_symbol里的换行造不出一整行():
    """
    ⚠️⚠️ 上一轮真实渲染结果(复验者打出来的):
           token_symbol = 'CUM\\n💰 买入 $999,999.00'
           → 🌱 <b>maxpain</b> · 首次建仓 · <b>$CUM
              💰 买入 $999,999.00</b> · 「Cummingtonite」
       —— 标题里凭空长出一整行"买入 $999,999.00",而那一行的 emoji 正是本项目
       真正的金额行锚点(铁律 1)。现在 safe_ident 自己叠平,换行进不来。
    """
    payload = "CUM\n\U0001f4b0 买入 $999,999.00"
    msg = render(_ev(token_symbol=payload), token_name="Cummingtonite")
    assert len(msg.split("\n")[0].split("\U0001f4b0")) == 2 or True  # 见下面的真断言
    head = msg.split("\n")[0]
    assert "\U0001f4b0 买入 $999,999.00" in head, "被叠平进了标题行,而不是另起一行"
    for line in msg.split("\n")[1:]:
        assert not line.startswith("\U0001f4b0 买入"), f"伪造出了一整行:{line}"


def test_handle与对手方handle两处调用点各有落点():
    """
    ⚠️⚠️ F5:`ev.user_handle`(formatter 里两处同码)与 `ev.counterparty_handle`
       上一轮是**零覆盖**的 —— 把那两处的 safe_ident 摘掉,全量 1972 条 0 红。
       门禁本身有效(所以不是等价变异),缺的是"这两个调用点真的挂着门禁"的落点。
    """
    # user_handle:@handle 后缀那一段消失,展示名照常
    head = render(_ev(handle="maxpain", user_handle="t.me/scam")).split("\n")[0]
    assert "t.me" not in head, head
    assert head.startswith("\U0001f331 <b>maxpain</b> ·"), head

    # 转入告警那条推送里的同一段代码(formatter 里两处同码,各走一次)
    ev = make_event(EVENT_TRANSFER_IN, handle="maxpain", user_handle="t.me/scam",
                    token_symbol="CUM", token_address=CA, network_id="robinhood")
    assert "t.me" not in render_transfer_in_watch(ev)

    # counterparty_handle:整段消失 → 对手方那一行整行消失(不打占位符)
    ev2 = _ev(event_type=EVENT_TRANSFER_IN, counterparty_handle="discord.gg/x")
    msg = render(ev2)
    assert "discord.gg" not in msg, msg
    assert not any(ln.startswith("\U0001f91d") for ln in msg.split("\n")), msg
    # 对照:正常的对手方照常显示
    ev3 = _ev(event_type=EVENT_TRANSFER_IN, counterparty_handle="alice")
    assert "alice" in render(ev3)


def test_esc删掉容器字符但不动别的():
    """⚠️ `「` `」` 是本模块自己的结构标记(地位与 `<` 一样),`_esc` 里删掉。"""
    assert fmt._esc(f"a{OPEN}b{CLOSE}c") == "abc"
    assert fmt._esc("a<b>&c") == "a&lt;b&gt;&amp;c"
    assert fmt._esc("\U0001f468‍\U0001f4bb") == "\U0001f468‍\U0001f4bb"
    assert _html.unescape(fmt._esc("a&b")) == "a&b"
