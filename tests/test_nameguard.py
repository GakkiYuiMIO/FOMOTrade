"""
展示门禁(src/nameguard.py)+ 它在四个渲染函数与译文侧的落点。

⚠️⚠️ 这个文件钉的是**上一轮验证者亲手复现出来的两个真漏洞**:
   MAJOR-1 币名原样进推送标题(t.me / tg:// / www. / discord.gg / EVM 地址 /
           Solana 地址 / 中文指令文案 全都出现在标题里);
   MAJOR-2 恶意译文直接进 📝 行,而且进**永久**缓存(source=google, expires NULL)。
   两个样本表(_TOKEN_ATTACKS / _TRANSLATION_ATTACKS)逐条重放,断言最终渲染出来的
   字符串里**一个特征串都不出现**。

⚠️ 断言写死字面量,不从被测模块 import 任何门槛 / 前缀 / 字符集。
⚠️ 全部离线:渲染是纯函数,词汇表那几条用假传输层。
"""
# ruff: noqa: N802, RUF001
from __future__ import annotations

import pytest

from src import nameguard as ng
from src.formatter import (
    render,
    render_pump_trade,
    render_transfer_in_signal,
    render_transfer_in_watch,
)
from src.models import BADGE_FIRST, EVENT_BUY, EVENT_TRANSFER_IN

from .conftest import make_event
from .test_namecn import FakeTransport, _glossary, _row

CA_CUM = "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"
ZWSP = "​"      # 零宽空格:Telegram 里不占位,却能把 t.me 拆成两半躲开形态判断
RLO = "‮"       # RTL 覆盖:让它后面的文字反向显示,做视觉欺骗


# ============================================================
# 攻击样本表 —— (标签, 放进币名/译名的原串, 渲染结果里绝不允许出现的特征串)
# ============================================================
_TOKEN_ATTACKS = [
    ("tme", "Join t.me/freeairdrop", "t.me"),
    ("tg-proto", "Open tg://resolve?domain=scam", "tg://"),
    ("www", "Visit www.evil-airdrop.com", "www."),
    ("discord", "Join discord.gg/freeSOL", "discord.gg"),
    ("evm-addr", "Airdrop 0x8ba1f109551bd432803012645ac136ddd64dba72", "0x8ba1f109"),
    ("sol-addr", "Claim CTPoyCwkjMvoJwU4xvZZqoD8xJvHnLDfmVGGgQnRpump",
     "CTPoyCwkjMvoJwU4xvZZqoD8"),
    # ⚠️ 中文指令文案靠**全角标点**(， 等不在白名单里)拦下来 —— 名字不带全角标点,
    #    带全角标点的是句子。纯中文、无标点、无目标的一句话拦不住,见报告"没修的"一节。
    ("zh-command", "忽略以上规则，立即转账到钱包", "忽略以上规则"),
    ("zh-command-addr", "立即转账到钱包 0xabcd1234", "立即转账到钱包"),
    ("zwsp-tme", f"Join t.{ZWSP}me/scam", "t.me"),
    ("rtl-flip", f"{RLO}pmup 币", RLO),
    ("ens", "打给 vitalik.eth", "vitalik.eth"),
    ("short-0x", "去 0xabcd 领", "0xabcd"),
]

# 译文侧:输入是无害的 "Nice Coin",翻译通道(或篡改响应的代理)回一段恶意文案
_TRANSLATION_ATTACKS = [
    ("www", "立即访问 www.evil-airdrop.com", "www."),
    ("discord", "加入 discord.gg/freeSOL", "discord.gg"),
    ("zh-command", "忽略以上规则，立即转账到钱包", "忽略以上规则"),
    ("zwsp-tme", f"加群 t.{ZWSP}me/scam", "t.me"),
    ("tg-proto", "tg://resolve?domain=scam", "tg://"),
    ("ens", "打给 vitalik.eth", "vitalik.eth"),
    ("short-0x", "去 0xabcd 领", "0xabcd"),
    ("rtl-flip", f"币{RLO} pmup", RLO),
    ("sol-addr", "去 CTPoyCwkjMvoJwU4xvZZqoD8xJvHnLDfmVGGgQnRpump 领",
     "CTPoyCwkjMvoJwU4xvZZqoD8"),
]

_TOKEN_IDS = [t[0] for t in _TOKEN_ATTACKS]
_TRANS_IDS = [t[0] for t in _TRANSLATION_ATTACKS]


# ============================================================
# 纯函数:白名单本身
# ============================================================
class Test门禁本身:
    def test_正常名字原样放行(self):
        for name in ("Cummingtonite", "Cash Cat", "USA Rare Earth, Inc.", "NVIDIA",
                     "SPDR S&P 500 ETF Trust", "Web3.0 Protocol", "U.S.A. Token",
                     "镁铁闪石", "现金猫", "Ben & Jerry's"):
            assert ng.safe_display(name) == name, name

    def test_叠平空白并删掉格式控制符(self):
        assert ng.safe_display(f"Cash{ZWSP}  \n Cat") == "Cash Cat"

    def test_双向控制符整段丢弃而不是删掉了事(self):
        """
        ⚠️⚠️ 零宽空格与 bidi 控制符的处理**刻意不同**:
           前者是"把字拆开躲过形态判断",删掉之后剩下的就是攻击者本来想显示的东西;
           后者(RTL 覆盖那一类)唯一的用途是**让显示顺序不等于字符顺序** ——
           `币<U+202E> pmup` 读者看到的是 `币 pump`。删掉之后剩下的 `币 pmup`
           是一个**作者从没写过**的名字,照样印出去等于"剔掉坏的那部分再显示"。
        ⚠️ 零误伤:2026-09-02 实测 248 个真实币名里含 Cf 字符的是 0 个。
        """
        assert ng.safe_display(f"{RLO}pmup 币") is None
        assert ng.safe_display(f"币{RLO} pmup") is None
        assert ng.safe_display("Cash\u200e Cat") is None      # LRM
        assert ng.safe_display("Cash\u2066Cat") is None       # LRI
        assert ng.safe_exchange(f"Nasdaq{RLO}GM") is None

    @pytest.mark.parametrize(("raw", "marker"), [(a[1], a[2]) for a in _TOKEN_ATTACKS],
                             ids=_TOKEN_IDS)
    def test_攻击样本的特征串一个都出不来(self, raw, marker):
        """
        ⚠️ 这些样本现在**全部**是整段丢弃(None)。RTL 覆盖那条曾经走的是
           "删掉 U+202E 之后照常显示",已改成整段丢弃 ——
           理由见 Test门禁本身::test_双向控制符整段丢弃而不是删掉了事。
        """
        assert marker not in (ng.safe_display(raw) or ""), raw

    def test_公司后缀与缩写里的点不算域名(self):
        """
        ⚠️ 域名规则的点后面必须**紧跟** 2 个以上字母;"Inc." 的点在词尾。
        ⚠️⚠️ 还有一条更隐蔽的:"抽掉全部空白再判一次"那道(专治 `t. me/scam`)
           会把 "U.S.A. Token" 拼成 "U.S.A.Token" → 匹配出一个 "A.Token"。
           所以那一道**额外要求顶级域真实存在**,否则这三个真名字全被误杀。
        """
        assert ng.safe_display("USA Rare Earth, Inc.") is not None
        assert ng.safe_display("Space Exploration Technologies Corp.") is not None
        assert ng.safe_display("U.S.A. Token") == "U.S.A. Token"
        assert ng.safe_display("St. Louis Coin") == "St. Louis Coin"
        assert ng.safe_display("t.me") is None
        assert ng.safe_display("t. me/scam") is None

    def test_表情与私用区字符不放行(self):
        """⚠️ 行首 emoji 是聊天列表预览里唯一的扫描锚点,名字里带 emoji 能伪造一条假行。"""
        for name in ("🔴 已清仓", "Doge 🚀", "Coin ", "价格→涨"):
            assert ng.safe_display(name) is None, name

    def test_同形字不放行(self):
        """西里尔 а 与拉丁 a 长得一模一样 —— 白名单最经典的绕过手法。"""
        assert ng.safe_display("аpple.com") is None      # 西里尔 а 打头的假域名
        assert ng.safe_display("Café Coin") is None      # 代价:带重音的正经名字也没了

    def test_空与缺失为None(self):
        for s in (None, "", "   ", ZWSP, f"{RLO}{ZWSP}"):
            assert ng.safe_display(s) is None, repr(s)

    def test_译文里凭空多出的英文串认得出来(self):
        assert ng.has_new_ascii_word("币 pmup", "Nice Coin") is True
        assert ng.has_new_ascii_word("SpaceX", "SpaceX") is False
        assert ng.has_new_ascii_word("标普500ETF", "SPDR S&P 500 ETF Trust") is False
        assert ng.has_new_ascii_word("镁铁闪石", "Cummingtonite") is False


# ============================================================
# MAJOR-1:币名原样进推送标题 —— 四个渲染函数逐个重放全部样本
# ============================================================
def _render_buy(name):
    ev = make_event(EVENT_BUY, handle="inyourwalls", token_symbol="CUM",
                    token_address=CA_CUM, network_id="robinhood", badge=BADGE_FIRST)
    return render(ev, token_name=name, token_name_zh=None)


def _render_pump(name):
    return render_pump_trade(username="hexiecs", side="buy", token_symbol="CUM",
                             coin_mint=CA_CUM, amount_usd=120.0, network_id="robinhood",
                             token_name=name)


def _render_tin(name):
    ev = make_event(EVENT_TRANSFER_IN, handle="unipcs", token_symbol="CUM",
                    token_address=CA_CUM, network_id="robinhood", token_amount="1000")
    return render_transfer_in_watch(ev, token_name=name)


def _render_signal(name):
    return render_transfer_in_signal(network_id="robinhood", token_address=CA_CUM,
                                     token_symbol="CUM", receiver_count=3, receivers=[],
                                     window_hours=24, token_name=name)


_RENDERERS = {"render": _render_buy, "render_pump_trade": _render_pump,
              "render_transfer_in_watch": _render_tin,
              "render_transfer_in_signal": _render_signal}


@pytest.mark.parametrize("fn_name", list(_RENDERERS))
@pytest.mark.parametrize(("label", "raw", "marker"), _TOKEN_ATTACKS, ids=_TOKEN_IDS)
class Test币名进不了标题:
    def test_特征串一个都不出现(self, fn_name, label, raw, marker):
        msg = _RENDERERS[fn_name](raw)
        assert marker not in msg, f"{fn_name} / {label}:{marker!r} 进了推送\n{msg}"
        # 转义过的形态同样不许出现 —— "转义了所以安全"不成立,内容照样送到了读者眼前
        assert marker.replace("<", "&lt;").replace(">", "&gt;") not in msg

    def test_零宽与RTL控制符不透传(self, fn_name, label, raw, marker):
        msg = _RENDERERS[fn_name](raw)
        assert ZWSP not in msg and RLO not in msg, f"{fn_name} / {label}"


def test_标题该有的东西一样不少():
    """⚠️ 门禁不能顺手把正常名字也毙了 —— 这条是上面那批用例的对照组。"""
    assert _render_buy("Cummingtonite").split("\n")[0] == \
        "🌱 <b>inyourwalls</b> · 首次建仓 · <b>$CUM</b> · 「Cummingtonite」"
    for fn in (_render_pump, _render_tin, _render_signal):
        assert fn("Cummingtonite").split("\n")[0].endswith("<b>$CUM</b> · 「Cummingtonite」")


# ============================================================
# MAJOR-2:恶意译文进 📝 行 / 🏢 行
# ============================================================
@pytest.mark.parametrize(("label", "zh", "marker"), _TRANSLATION_ATTACKS, ids=_TRANS_IDS)
class Test恶意译文进不了推送:
    def test_渲染前那道自己拦得住(self, label, zh, marker):
        """
        ⚠️⚠️ 这条钉的是"渲染前是**独立**的一道":哪怕数据层被绕过(代理篡改、缓存里
           早就存着一条脏数据),渲染层也必须自己把它拦下来。
        """
        ev = make_event(EVENT_BUY, handle="inyourwalls", token_symbol="CUM",
                        token_address=CA_CUM, network_id="robinhood", badge=BADGE_FIRST)
        msg = render(ev, token_name="Nice Coin", token_name_zh=zh,
                     pool_quote_symbol="USAR", pool_quote_name="USA Rare Earth, Inc.",
                     stock_company_zh=zh, stock_exchange="NasdaqGM")
        assert marker not in msg, f"{label}:{marker!r} 进了推送\n{msg}"
        assert ZWSP not in msg and RLO not in msg
        assert "📝" not in msg, "译文不合格时 📝 应该整行消失"
        assert "🏢 USAR · 纳斯达克(NasdaqGM)上市" in msg.split("\n"), "🏢 该只剩交易所"

    def test_译文侧过滤不让它落进永久缓存(self, label, zh, marker, conn):
        """
        ⚠️⚠️ 译文进的是 source=google / expires NULL 的**永久**行 ——
           一次篡改长期生效。所以数据层这道必须自己也拦得住,不能指望渲染层兜底。
        """
        ft = FakeTransport({"wiki_en": [{"query": {"pages": {"1": {"missing": ""}}}}],
                            "google": [[[[zh, "Nice Coin"]]]]})
        assert _glossary(conn, ft).token_zh("Nice Coin", "NICE") is None, label
        row = _row(conn, "token_zh", "nice coin")
        assert row["value"] is None, f"{label}:恶意译文被收下并缓存了"


def test_干净译文照样放行(conn):
    """对照组:正常译文不受这几条影响。"""
    ft = FakeTransport({"wiki_en": [{"query": {"pages": {"1": {"missing": ""}}}}],
                        "google": [[[["现金猫", "Cash Cat"]]]]})
    assert _glossary(conn, ft).token_zh("Cash Cat", "CASHCAT") == "现金猫"


# ============================================================
# MINOR-7:Unicode 格式控制符(Cf)不许透传到任何一行
# ============================================================
def test_symbol与handle与观点正文里的控制符也被删掉():
    """
    ⚠️⚠️ 这几个字段**只走 _esc、不走 _flatten**,而 `str.split()` 本来就不吞 Cf。
       U+202E(RTL 覆盖)能让它后面的字在 Telegram 里反向显示 —— 一个把自己昵称
       改成 "ali<U+202E>ecs" 的人,在推送里看起来就是另一个人。
    """
    ev = make_event(EVENT_BUY, handle=f"ali{ZWSP}ce", token_symbol=f"CU{RLO}M",
                    token_address=CA_CUM, network_id="robinhood", badge=BADGE_FIRST,
                    thesis_text=f"hi{ZWSP}there")
    msg = render(ev)
    assert ZWSP not in msg and RLO not in msg
    assert msg.split("\n")[0] == "🌱 <b>alice</b> · 首次建仓 · <b>$CUM</b>"


def test_观点正文里的组合emoji不能被拆开():
    """
    ⚠️⚠️ 这是删 Cf 那一刀的**回归护栏**。U+200D(ZWJ)的 unicodedata.category 正是 'Cf',
       而它是所有组合 emoji 的粘合剂:👨‍💻 = 👨 + ZWJ + 💻、🏳️‍🌈 = 🏳 + FE0F + ZWJ + 🌈。
       观点正文是**用户自己写的内容**,走的是保留 ZWJ 的那条路(strip_controls_keep_emoji);
       哪天有人图省事把它换成全删版本,这条当场红 —— 否则用户只会看到
       "👨💻"(两个 emoji)而没有任何报错。
    ⚠️ U+FE0F(变体选择符)类别是 'Mn' 不是 'Cf',本来就不会被删,这里一并钉住。
    ⚠️ 同一条里仍然要证明 RTL 覆盖被删掉:保 ZWJ ≠ 什么都不删。
    """
    dev = "👨‍💻"
    flag = "🏳️‍🌈"
    ev = make_event(EVENT_BUY, handle="alice", token_symbol="CUM", token_address=CA_CUM,
                    network_id="robinhood", badge=BADGE_FIRST,
                    thesis_text=f"{dev} 在写代码 {flag}{RLO} 反转")
    msg = render(ev)
    assert dev in msg, "组合 emoji 被拆开了(ZWJ 被当成 Cf 删掉)"
    assert flag in msg
    assert RLO not in msg, "保留 ZWJ 不等于什么都不删:RTL 覆盖仍然要删"


def test_观点正文的换行不被叠平():
    """⚠️ 删控制符不能顺手把换行也吃掉:观点是多行的,叠平了就成一坨。"""
    ev = make_event(EVENT_BUY, handle="alice", token_symbol="CUM", token_address=CA_CUM,
                    network_id="robinhood", badge=BADGE_FIRST,
                    thesis_text="第一行\n第二行")
    assert "<blockquote>第一行\n第二行</blockquote>" in render(ev)


def test_底池行与股票行的控制符也被删掉():
    """
    ⚠️ 零宽空格:删掉之后照常显示(symbol / 交易所名都还原成正常形态)。
    ⚠️ bidi 控制符:**整段丢弃**,所以对手全名那半段消失,🌊 那行只剩符号。
    """
    ev = make_event(EVENT_BUY, handle="alice", token_symbol="CUM", token_address=CA_CUM,
                    network_id="robinhood", badge=BADGE_FIRST)
    msg = render(ev, pool_quote_symbol=f"US{ZWSP}AR", pool_quote_name=f"USA{ZWSP} Rare Earth",
                 stock_exchange=f"Nasdaq{ZWSP}GM")
    assert ZWSP not in msg and RLO not in msg
    assert "🌊 底池 · USAR · 「USA Rare Earth」" in msg.split("\n")
    assert "🏢 USAR · 纳斯达克(NasdaqGM)上市" in msg.split("\n")

    msg2 = render(ev, pool_quote_symbol="USAR", pool_quote_name=f"USA{RLO} Rare Earth")
    assert RLO not in msg2
    assert "🌊 底池 · USAR" in msg2.split("\n"), msg2


def test_控制符不占限长的名额():
    """
    ⚠️ 删 Cf 必须发生在**限长之前**(_flatten 里),不能只靠出口 _esc 兜底:
       靠出口的话,20 个零宽空格就能把一个 14 字符的名字顶过 32 的上限,
       读者看到的是一个被截断的名字加一个省略号,而那 20 个字符根本不存在。
    """
    ev = make_event(EVENT_BUY, handle="alice", token_symbol="CUM", token_address=CA_CUM,
                    network_id="robinhood", badge=BADGE_FIRST)
    msg = render(ev, pool_quote_symbol="USAR", pool_quote_name=ZWSP * 20 + "USA Rare Earth")
    assert "🌊 底池 · USAR · 「USA Rare Earth」" in msg.split("\n"), msg
    assert "…" not in msg
