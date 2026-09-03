"""
🚀 发射台 / 🧑‍🤝‍🧑 持有人 / 🔗 社媒 —— 三行的渲染。

⚠️ 断言写死字面量(整行逐字比对):不从 src.formatter import 任何 emoji / 文案常量。
   改一个字 = 改这里,而不能反过来。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src.formatter import render, render_pump_trade, render_transfer_in_watch
from tests.conftest import make_event
from tests.test_nameguard_shape import _MUST_BLOCK

SOC = (("website", "https://cashcat.cc/"),
       ("twitter", "https://x.com/cashcat_token"),
       ("telegram", "https://t.me/cashcat_robinhood"))


def _lines(**kw):
    return render(make_event(), **kw).split("\n")


# ============================================================
# 三行长什么样
# ============================================================
def test_发射台那一行逐字():
    """
    ⚠️ **不套 `「」`**:门禁是封闭枚举(nameguard.safe_launchpad),返回值只可能是
       表里那几个规范写法 —— 与 🏢 行的交易所名同一条理由。形态与用户样例一致。
    """
    assert "🚀 发射台 · LONG" in _lines(launchpad="LONG")
    assert "🚀 发射台 · Pump.fun" in _lines(launchpad="Pump.fun")
    assert "🚀 发射台 · Pons V2" in _lines(launchpad="Pons V2")


def test_发射台的大小写不受上游摆布():
    """⚠️ 返回的是**表里的规范写法**,与 safe_exchange 同一条口径。"""
    assert "🚀 发射台 · Pump.fun" in _lines(launchpad="PUMP.FUN")
    assert "🚀 发射台 · Pons" in _lines(launchpad="pons")


def test_发射台是封闭枚举表外的一律不显示():
    """
    ⚠️⚠️ 代价明写:上游出一个新发射台时这一行不显示,直到有人把它加进
       nameguard._LAUNCHPADS。这是"宁可缺失整行,绝不印错"的既有取舍。
    ⚠️ 尤其是这几个 —— 它们长得**就像**真发射台,但表里没有:
    """
    for bad in ("BrandNewPad", "Pump.fun2", "Pump .fun", "pumpfun", "LONGER",
                "t.me", "evil.com", "Pons V3"):
        assert "发射台" not in render(make_event(), launchpad=bad), bad


# ⚠️⚠️ 2026-09-03 第二轮(H1)实测:上一版那张封闭表只枚举了 **四条链**
#    (robinhood/solana/bsc/base),而 models.NETWORK_CHAIN_ID 里有**六条** ——
#    ethereum 与 monad 同样会走 filterTokens。六条链 9810 个去重代币重跑一遍,
#    全集是 44 个名字,上一版漏了下面这 19 个。其中 **Nad.Fun 是 monad 上有发射台的
#    币的 100%** —— 也就是说 monad 链的 🚀 那一行在上一版里**从来没有显示过**。
# ⚠️ 名字与出现次数写死在这一侧,不 import _LAUNCHPADS:
#    这张表是"实测结论",不是"代码说什么就是什么"。
# ⚠️⚠️ 必须钉**全部** 45 条,不能只钉新增的那 19 条 —— 复验实测:只钉新增的那一版,
#    把 Flap(901 次命中)、MeteoraDBC(589)、Clanker V4(80)从 _LAUNCHPADS 里删掉,
#    全量 pytest **0 红**。45 条里当时有 22 条没有任何测试守着,
#    而命中次数最多的几个恰好都在没守着的那一半里。
# 每项 =(上游原值, 我们的规范写法, 主要出现的链)。
_ALL_LAUNCHPADS = [
    ("Pump.fun", "Pump.fun", "solana"), ("pons", "Pons", "robinhood"),
    ("Flap", "Flap", "bsc"), ("MeteoraDBC", "MeteoraDBC", "solana"),
    ("StonkFun", "StonkFun", "solana"), ("Four.meme", "Four.meme", "bsc"),
    ("LONG", "LONG", "robinhood"), ("Bankr", "Bankr", "base"),
    ("Clanker V4", "Clanker V4", "base"), ("UniswapCCA", "UniswapCCA", "robinhood"),
    ("o1.exchange", "o1.exchange", "base"), ("BAGS", "BAGS", "solana"),
    ("Virtuals", "Virtuals", "robinhood"), ("Bonk", "Bonk", "solana"),
    ("LaunchLab", "LaunchLab", "solana"), ("Pump Mayhem", "Pump Mayhem", "solana"),
    ("Nad.Fun", "Nad.Fun", "monad"), ("Printr", "Printr", "solana"),
    ("Flaunch", "Flaunch", "base"), ("Sushi Launch", "Sushi Launch", "robinhood"),
    ("EasyA Kickstart", "EasyA Kickstart", "solana"),
    ("Zora Creator", "Zora Creator", "base"), ("Feel.cash", "Feel.cash", "base"),
    ("bow.fun", "bow.fun", "robinhood"), ("Heaven", "Heaven", "solana"),
    ("Baseapp", "Baseapp", "base"), ("Four.meme Fair", "Four.meme Fair", "bsc"),
    ("Trench", "Trench", "robinhood"), ("tren.ch", "tren.ch", "solana"),
    ("Moonshot", "Moonshot", "solana"),
    ("Meteora Alpha Vault", "Meteora Alpha Vault", "solana"),
    ("Liquid", "Liquid", "base"), ("Livo", "Livo", "ethereum"),
    ("Jupiter Studio", "Jupiter Studio", "solana"),
    ("AMERICA.fun", "AMERICA.fun", "solana"), ("Zora", "Zora", "base"),
    ("Baseapp Creator", "Baseapp Creator", "base"), ("hood.fun", "hood.fun", "robinhood"),
    ("Moonit", "Moonit", "solana"), ("DubDub", "DubDub", "solana"),
    ("Believe", "Believe", "solana"), ("Blowfish", "Blowfish", "solana"),
    ("Vertigo", "Vertigo", "solana"), ("Metaplex", "Metaplex", "solana"),
    # 本仓库自己产出的值(tokeninfo 按创建工厂把 pons 分成 V1/V2)
    ("Pons V2", "Pons V2", "robinhood"),
]


def test_封闭表的条数写死在测试这一侧():
    """
    ⚠️ 45 这个数字是**实测结论**(六条链 9810~9882 个去重代币,fixer 与两个复验者
       各自独立枚举都得 44 个上游名,加上我们自产的 Pons V2)。写死在这一侧,
       是为了让"有人往 _LAUNCHPADS 里加了一条却没有对应实测依据"当场变红:
       加表项必须同时把它加进上面那张实测表。
    """
    assert len(_ALL_LAUNCHPADS) == 45
    assert len({d for _, d, _ in _ALL_LAUNCHPADS}) == 45


@pytest.mark.parametrize(("raw", "shown", "chain"), _ALL_LAUNCHPADS,
                         ids=[d for _, d, _ in _ALL_LAUNCHPADS])
def test_六条链全量枚举出来的发射台名一个不漏(raw, shown, chain):
    """⚠️ 大小写也钉住:显示的是**我们的规范写法**,不是上游原串的任意变体。"""
    assert f"🚀 发射台 · {shown}" in _lines(launchpad=raw), (raw, shown, chain)
    assert f"🚀 发射台 · {shown}" in _lines(launchpad=raw.upper()), (raw, shown, chain)
    assert f"🚀 发射台 · {shown}" in _lines(launchpad=raw.lower()), (raw, shown, chain)


def test_表外的发射台名打的是WARNING不是DEBUG():
    """
    ⚠️⚠️ 这条日志是"上游出了新发射台、这张封闭表该更新了"的**唯一**发现途径。
       上一版打的是 DEBUG,而默认日志级别看不到 DEBUG —— 于是 monad 上的 Nad.Fun
       整整一版都不显示,没有任何人知道。**静默失效**是这个项目反复吃过的亏。
    ⚠️ 用 loguru 自己的 sink 抓(它不走 caplog);级别写死 "WARNING"。
    """
    from loguru import logger

    got = []
    sink = logger.add(lambda m: got.append((m.record["level"].name, m.record["message"])),
                      level="WARNING")
    try:
        assert "发射台" not in render(make_event(), launchpad="BrandNewPad")
    finally:
        logger.remove(sink)
    assert any(lvl == "WARNING" and "不在封闭表里" in msg for lvl, msg in got), got


def test_持有人那一行逐字并带千分位():
    assert "🧑‍🤝‍🧑 持有人 1,194" in _lines(token_holders=1194)
    assert "🧑‍🤝‍🧑 持有人 33,633" in _lines(token_holders=33633)


def test_持有人的emoji不是共识那个():
    """
    ⚠️⚠️ 👥 已经被「名单内 3/108 人买过」占了(用户亲自定的口径)。
       两条含义完全不同的行共用一个 emoji,扫一眼会被读成同一件事。
    """
    line = [ln for ln in _lines(token_holders=1194) if "持有人" in ln][0]
    assert line.startswith("🧑‍🤝‍🧑 ")
    assert not line.startswith("👥")


def test_社媒那一行用我们自己的文字():
    """
    ⚠️⚠️ 链接文字是**我们的常量**,永远不用上游给的 label(那是发币的人填的自由文本,
       而链接文字带着我们的背书)。
    """
    assert ('🔗 <a href="https://cashcat.cc/">网站</a> · '
            '<a href="https://x.com/cashcat_token">Twitter</a> · '
            '<a href="https://t.me/cashcat_robinhood">Telegram</a>') in _lines(token_socials=SOC)


def test_六类社媒的文字():
    got = _lines(token_socials=(("website", "https://a.com/"), ("twitter", "https://x.com/a"),
                                ("telegram", "https://t.me/a"), ("discord", "https://discord.gg/a"),
                                ("reddit", "https://reddit.com/r/a"),
                                ("github", "https://github.com/a")))
    line = [ln for ln in got if ln.startswith("🔗") and "网站" in ln][0]
    assert "网站" in line and "Twitter" in line and "Telegram" in line
    assert "Discord" in line and "Reddit" in line and "GitHub" in line


# ============================================================
# ⚠️⚠️ 三行**各自独立**
# ============================================================
@pytest.mark.parametrize(("kw", "gone", "kept"), [
    ({"launchpad": None, "token_holders": 1194, "token_socials": SOC}, "发射台", ("持有人", "网站")),
    ({"launchpad": "LONG", "token_holders": None, "token_socials": SOC}, "持有人", ("发射台", "网站")),
    ({"launchpad": "LONG", "token_holders": 1194, "token_socials": None}, "网站", ("发射台", "持有人")),
])
def test_任一拿不到只掉那一行(kw, gone, kept):
    text = render(make_event(), **kw)
    assert gone not in text
    for k in kept:
        assert k in text, k


def test_三个全没有时整条推送照发():
    """⚠️ 这三行没有一个是推送的前置条件。"""
    text = render(make_event(), launchpad=None, token_holders=None, token_socials=None)
    assert "🚀" not in text and "持有人" not in text
    assert text.startswith("🌱") or text.startswith("🟢")
    assert "<code>" in text                       # CA 行还在


# ============================================================
# 缺失的判据
# ============================================================
def test_持有人为0不显示():
    """
    ⚠️⚠️ 0 在取值层就被归成 None 了(上游拿 0 当"取不到"的哨兵,见 tokeninfo)。
       这里是第二道:一条紧挨着真实成交的消息里,「持有人 0」这句话本身就说不出口。
    """
    assert "持有人" not in render(make_event(), token_holders=0)
    assert "持有人" not in render(make_event(), token_holders=-3)


def test_持有人不是数字时整行消失():
    for bad in ("一千", "", [], {}, True, 3.7e400):
        assert "持有人" not in render(make_event(), token_holders=bad), bad


def test_持有人有上界():
    """
    ⚠️⚠️ 这个槽位收的是上游给的数,而"一串 10~11 位数字"正是手机号 / QQ 号的形态。
       印成「持有人 13,800,138,000」既是假事实、又是一条可拨可加的目标。
       实测全库最大的是 base 的 USDC(10,967,609),10 亿留了近百倍余量。
    """
    assert "持有人" not in render(make_event(), token_holders=13800138000)
    assert "持有人" not in render(make_event(), token_holders=1_000_000_001)
    assert "持有人 1,000,000,000" in render(make_event(), token_holders=1_000_000_000)
    assert "持有人 10,967,609" in render(make_event(), token_holders=10967609)


def test_持有人是浮点数时按整数显示():
    assert "🧑‍🤝‍🧑 持有人 1,194" in _lines(token_holders=1194.0)


def test_发射台为空串或空白时整行消失():
    for bad in ("", "   ", None):
        assert "发射台" not in render(make_event(), launchpad=bad), repr(bad)


def test_社媒全空时整行消失():
    for bad in (None, (), [], "不是列表"):
        text = render(make_event(), token_socials=bad)
        assert "网站" not in text and "Twitter" not in text, repr(bad)


# ============================================================
# 行序
# ============================================================
def test_行序_发射台与持有人在市值之后底池之前():
    """⚠️ 位置与用户给的样例一致:它们与市值/币龄同一族(都在回答"这个币本身是什么样的")。"""
    ev = make_event(market_cap=123456.0)
    lines = render(ev, launchpad="LONG", token_holders=1194,
                   pool_quote_symbol="USAR", pool_quote_name="USA Rare Earth, Inc.").split("\n")
    idx = {"mc": None, "lp": None, "ho": None, "pool": None}
    for i, ln in enumerate(lines):
        if ln.startswith("💎"):
            idx["mc"] = i
        elif ln.startswith("🚀"):
            idx["lp"] = i
        elif "持有人" in ln:
            idx["ho"] = i
        elif ln.startswith("🌊"):
            idx["pool"] = i
    assert None not in idx.values(), lines
    assert idx["mc"] < idx["lp"] < idx["ho"] < idx["pool"], lines


def test_行序_社媒在平台链接之前而平台链接在CA之前():
    """⚠️ CA 独占最后一行是硬规则(§10.3),社媒绝不能挤到它后面。"""
    lines = render(make_event(), token_socials=SOC).split("\n")
    soc = next(i for i, ln in enumerate(lines) if "网站" in ln)
    fomo = next(i for i, ln in enumerate(lines) if ">FOMO<" in ln)
    assert soc < fomo < len(lines) - 1
    assert lines[-1].startswith("<code>")


# ============================================================
# ⚠️⚠️ 红队:必拦硬基线打进三个槽位
# ============================================================
@pytest.mark.parametrize(("raw", "rule"), _MUST_BLOCK, ids=[r[1] for r in _MUST_BLOCK])
def test_必拦硬基线打进发射台槽位零泄漏(raw, rule):
    text = render(make_event(), launchpad=raw)
    assert "🚀" not in text, f"{raw!r} 漏进了发射台行({rule})"
    assert raw.strip() not in text or not raw.strip()


@pytest.mark.parametrize(("raw", "rule"), _MUST_BLOCK, ids=[r[1] for r in _MUST_BLOCK])
def test_必拦硬基线打进持有人槽位零泄漏(raw, rule):
    text = render(make_event(), token_holders=raw)
    assert "持有人" not in text, f"{raw!r} 漏进了持有人行({rule})"


@pytest.mark.parametrize(("raw", "rule"), _MUST_BLOCK, ids=[r[1] for r in _MUST_BLOCK])
def test_必拦硬基线打进社媒槽位零泄漏(raw, rule):
    base = render(make_event())
    text = render(make_event(),
                  token_socials=[("website", raw), ("twitter", raw), (raw, raw)])
    assert "网站" not in text and "Twitter" not in text, f"{raw!r} 漏进了社媒行({rule})"
    # ⚠️ 链接总数不许比"没有社媒"那条多 —— 多一个就是多一个可点出口
    assert text.count("<a href") == base.count("<a href"), f"{raw!r} 多长出一个链接({rule})"


_EVIL_URLS = [
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "//evil.com/x",
    "https://x.com@evil.com/",
    'https://x.com/a" onmouseover="alert(1)',
    "https://xn--80ak6aa92e.com/",
    "https://x.com/a\nb",
    "https://" + "a" * 300 + ".com/",
    "https://1.2.3.4/",
    # ⚠️ H5 之后**没有**顶级域白名单了(evil.zzz 现在放行,理由见 nameguard 那段实测)。
    #    这一道只判形态,所以红队样本换成"顶级域根本不成形"的三种。
    "https://evil.z/",
    "https://evil.123/",
    "https://evil.co-m/",
]


@pytest.mark.parametrize("url", _EVIL_URLS)
def test_恶意URL在渲染入口就被拦掉(url):
    """⚠️ 门禁在**渲染入口**统一做(formatter.URL_FIELDS),不是靠调用方记得先过一遍。"""
    base = render(make_event())
    for kind in ("website", "twitter"):
        text = render(make_event(), token_socials=[(kind, url)])
        assert "网站" not in text and "Twitter" not in text, (kind, url)
        assert text.count("<a href") == base.count("<a href"), (kind, url)


def test_社媒类的host是等值比对而网站那一类刻意放开():
    """
    ⚠️⚠️ `https://x.com.evil.com/` 在 twitter 那一类**必须**被拦(封闭 host 表是
       **等值**比对,不是后缀比对);但它在「网站」那一类是**放行**的 ——
       项目自己的站 host 本质上不可枚举,那是刻意的取舍(见 nameguard 里
       "「网站」那一类为什么放开 host" 那段,含代价)。这条把两侧都钉住,免得后来人
       看到"website 放行了 evil.com"以为是漏洞、顺手把这一类整块删掉。
    ⚠️ 也正因为放开,链接文字才必须是**不带背书**的「网站」而不是「官网」(H5)。
    """
    assert "Twitter" not in render(make_event(),
                                   token_socials=[("twitter", "https://x.com.evil.com/")])
    assert ">网站</a>" in render(make_event(),
                                token_socials=[("website", "https://x.com.evil.com/")])


def test_URL里的与号必须转义():
    """
    ⚠️⚠️ `&` 是 URL 查询串的合法字符(门禁放行),但它在 HTML 属性里是实体的起头。
       铁律 4:所有来自 API 的文本必须 escape —— 一个裸 `&` 就可能让整条消息 400。
       门禁保证了 `"` 与 `<` 进不来,escape 保证了 `&` 也是安全的;两道各自独立。
    """
    text = render(make_event(), token_socials=[("twitter", "https://x.com/a?b=1&c=2")])
    assert '<a href="https://x.com/a?b=1&amp;c=2">Twitter</a>' in text
    assert "?b=1&c=2" not in text


def test_上游的label永远不出现在消息里():
    """
    ⚠️⚠️ DexScreener 的 `websites[].label` 是发币的人填的。这里把一个"看着像官方"的
       label 塞进来,它一个字都不该出现 —— 因为我们**根本不读**那个字段。
    """
    text = render(make_event(), token_socials=(("website", "https://cashcat.cc/"),))
    assert "官方客服" not in text
    assert ">网站<" in text


def test_门禁炸了也只是这几行消失():
    class Boom:
        def __iter__(self):
            raise RuntimeError("上游给了个会炸的对象")

    text = render(make_event(), token_socials=Boom(), launchpad="LONG", token_holders=1194)
    assert "🚀 发射台 · LONG" in text and "持有人 1,194" in text
    assert "网站" not in text


# ============================================================
# 另外三条渲染路径
# ============================================================
def test_pump成交推送也带这三行():
    text = render_pump_trade(username="alice", side="buy", token_symbol="CATE",
                             coin_mint="Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump",
                             amount_usd=100.0, network_id="solana",
                             launchpad="Pump.fun", token_holders=118265, token_socials=SOC)
    assert "🚀 发射台 · Pump.fun" in text
    assert "🧑‍🤝‍🧑 持有人 118,265" in text
    assert ">网站</a>" in text
    assert text.split("\n")[-1].startswith("<code>")


def test_转入逐条推送也带这三行():
    from src.models import EVENT_TRANSFER_IN

    text = render_transfer_in_watch(make_event(EVENT_TRANSFER_IN),
                                    launchpad="LONG", token_holders=1194, token_socials=SOC)
    assert "🚀 发射台 · LONG" in text
    assert "持有人 1,194" in text
    assert ">Telegram</a>" in text


def test_分发预警也带这三行():
    from src.formatter import render_transfer_in_signal

    text = render_transfer_in_signal(
        network_id="robinhood", token_address="0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18",
        token_symbol="CUM", receiver_count=3,
        receivers=[{"who": "a", "usd": 1.0}], window_hours=6,
        launchpad="LONG", token_holders=1194, token_socials=SOC)
    assert "🚀 发射台 · LONG" in text
    assert "持有人 1,194" in text
    assert ">网站</a>" in text
    assert text.split("\n")[-1].startswith("<code>")
