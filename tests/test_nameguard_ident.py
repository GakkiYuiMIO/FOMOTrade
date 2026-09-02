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
    # ======== 本轮 F5:_RE_DOMAIN_WITH_PATH 的**独占**样本 ========
    # ⚠️ 上一轮这条正则是零覆盖的(把它打成永不匹配,全量 1972 条 0 红):语料里每个
    #    带路径域名的 TLD 恰好都在 _IDENT_TLDS 里,被下面那条"TLD 是真 TLD"捎带拦了。
    #    下面三条的 TLD(zzz / gift / wtf)**都不在**那张表里,只有带路径那条拦得住。
    ("evil.zzz/claim", "带路径的域名,TLD 'zzz' 不在 ident 的 TLD 表里"),
    ("x.gift/free", "带路径的域名,TLD 'gift' 不在表里"),
    ("claim.wtf/now", "带路径的域名,TLD 'wtf' 不在表里"),
    # ======== 本轮 F1b:分隔符在 ident 侧**一律严禁**(它不套「」容器)========
    # ⚠️⚠️ 这一组与 test_nameguard_shape 的必拦基线是**同一批串**:
    #    名字侧有容器,所以其中几条在那边放行;ident 侧没有容器,这边一条都不许过。
    #    "同一个串两边判得不一样"从此是**写下来的规则**,不再是两个文件的矛盾。
    ("已清仓 · 亏损 99%", "U+00B7:直接和推送自己的 SEP 平级,能凭空造两个字段"),
    ("官方认证 · 已审计", "U+00B7"),
    ("Coin • Token", "U+2022 —— 这一条在名字侧(有容器)是放行的"),
    ("已清仓 ・ 亏损 99%", "U+30FB 片假名中点"),
    ("六子｜Funny Six", "U+FF5C 全角竖线 —— 生产库里的真实昵称,刻意的取舍"),
    ("血手人屠·厉飞雨", "U+00B7 —— 生产库里的真实昵称,刻意的取舍(理由见文件头)"),
    ("A|B", "U+007C 半角竖线"),
    ("A·B", "紧贴两侧也不许:ident 侧没有'两侧有没有空格'这条口径"),
    # ======== 本轮 F2:视觉容器字符在 ident 侧也整段丢弃 ========
    ("假「名字」", "`「`『』是视觉容器本身,ident 没有字符白名单,单列一条"),
    ("CUM」 · 已清仓", "只带一个右括号就能把容器提前关掉 —— 「」配平那条不变量"),
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
    "alice 👨‍💻", "[VIP] trader", "Ω" * 30,
    # 苏格兰旗 = 🏴 + 6 个 tag 字符 + 终止符。⚠️ 叠平那一步用的是**保留 emoji** 的版本,
    # 全删 Cf 会把它拆成一面光秃秃的黑旗(那是 E5 那条教训)。
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿 highlander",
]
# ⚠️⚠️ **本轮从必放行里移走的两条**(如实登记,这是刻意的取舍):
#      '血手人屠·厉飞雨'(U+00B7)与 '六子｜Funny Six'(U+FF5C)是生产库里的真实昵称,
#      本轮起被 safe_ident 拦下 —— 它们不套「」容器,与推送自己的 SEP 平级。
#      实测代价:208 个真实 handle/昵称里就这 2 条 = 0.96%(判据线是 2%)。
#      拦下之后展示名退回 user_id、再退回"未知用户"(既有的缺失规矩,不打占位符)。


@pytest.mark.parametrize(("raw", "why"), _IDENT_BLOCK, ids=[b[1] for b in _IDENT_BLOCK])
def test_必须拦住的形态(raw, why):
    assert safe_ident(raw) is None, f"{raw!r} 本该被拦({why})"


@pytest.mark.parametrize("raw", _IDENT_PASS)
def test_必须放行的真实符号与昵称(raw):
    assert safe_ident(raw) == raw, raw


def test_通过的要叠平成单行():
    """
    ⚠️⚠️ 本轮 F2 改的就是这条(上一版断言"原样返回,一个字符都不动")。
       上一版的理由是"清洗与限长是下游 _clip 的既有职责" —— 那句话对
       render_pump_trade / render_transfer_in_signal / render_alpha_listing 成立,
       对 **render() 不成立**:_display_name / _symbol_plain 只 _esc 不 _clip,
       于是 token_symbol='CUM\n💰 买入 $999,999.00' 在标题里凭空造出一整行伪造字段。
       把清洗责任推给下游 = 赌四条路都记得,而实测漏了一条。现在这道门自己叠平。
    ⚠️ 只叠平**空白与控制符**,不做别的清洗:转义与限长仍然是下游 _clip 的事
       (在这里顺手转义会让 `&` 被转两次,显示成一串乱码)。
    """
    assert safe_ident("CUM\n💰 买入 $999,999.00") == "CUM 💰 买入 $999,999.00"
    assert safe_ident("  CUM  ") == "CUM"
    assert safe_ident("A\r\n\tB") == "A B"
    assert safe_ident("A B") == "A B", "NBSP 也算空白"
    # 零宽字符删掉(它能把域名拆开躲过形态判断)
    assert safe_ident(f"ali{ZWSP}ce") == "alice"
    # ⚠️ 但**组合 emoji 的粘合剂必须留着**:ZWJ 与 tag 字符的类别同样是 Cf,
    #    全删会把 👨‍💻 拆成两个 emoji、把苏格兰旗拆成一面黑旗。
    assert safe_ident("alice 👨‍💻") == "alice 👨‍💻"


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
                "13800138000", "🔴🟢🟡", "已清仓 亏损 99%", "官方认证。已审计",
                "一三八零零一三八零零零", "Buy now safe airdrop visit my profile"):
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
