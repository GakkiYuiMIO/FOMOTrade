"""
外部 URL 门禁(nameguard.safe_url / safe_social_links)—— 社媒那一行的唯一入口。

============ 这个文件为什么存在 ============
本轮是本项目**第一次**把外部字符串放进 `<a href>`。前面所有门禁守的都是
"印出来的字",这一道守的是"点下去会去哪儿" —— 失败后果完全不是一个量级:
前者最坏是读者读到一句假话,后者是读者**被带到攻击者的站点**,
而链接文字还是我们自己写的「官网」「Twitter」,天然带着我们的背书。

⚠️ 断言全部写死字面量:不从 src.nameguard import 任何表、任何阈值、任何正则。
   改门禁 = 改这些断言,而不能反过来。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src.nameguard import safe_social_links, safe_url
from tests.test_nameguard_shape import _MUST_BLOCK

# ============================================================
# 放行:真实样本(2026-09-03 真网络录的)
# ============================================================
_REAL_PASS = [
    ("https://x.com/cumonrh", "twitter"),
    ("https://x.com/cashcat_token", "twitter"),
    ("https://x.com/artificiallyinu", "twitter"),
    ("https://t.me/cashcat_robinhood", "telegram"),
    ("https://t.me/catecoin_telegram", "telegram"),
    ("https://www.reddit.com/r/cashcattoken/s/L8GOOuy9Ch", "reddit"),
    ("https://cashcat.cc/", "website"),
    ("https://artificialinu.com/", "website"),
    ("https://cate.meme/", "website"),
    ("https://app.long.xyz/tokens/0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18", "website"),
    ("https://x.com/i/communities/2004330768022004131", "twitter"),
]


@pytest.mark.parametrize(("url", "kind"), _REAL_PASS)
def test_真实社媒链接原样放行(url, kind):
    """⚠️ 这些是真网络录下来的,门禁收紧到把它们打掉时这里当场红。"""
    assert safe_url(url, kind) == url, url


# ============================================================
# ⚠️⚠️ 红队:针对 URL 的必拦样本
# ============================================================
# 每一条各钉住一条独立的规则 —— 去掉任何一道判定,至少有一条会红。
_URL_MUST_BLOCK = [
    ("javascript:alert(1)", "website", "scheme 不是 https"),
    ("JavaScript:alert(1)", "website", "scheme 大小写变体"),
    ("data:text/html;base64,PHNjcmlwdD4=", "website", "data: 伪协议"),
    ("//evil.com/x", "website", "协议相对 URL(浏览器会自己补 https)"),
    ("http://x.com/a", "twitter", "http 不放行 —— 明文降级面"),
    ("https://x.com@evil.com/", "twitter", "userinfo:host 其实是 evil.com"),
    ("https://x.com:pass@evil.com/", "twitter", "带密码的 userinfo"),
    # ⚠️ 官网那一类**放开 host**,所以 userinfo 在这一侧没有"封闭 host 表"兜底 ——
    #    它只能靠 netloc 的 @ 判定 + 逐段 LDH 校验。这条是那两道规则的独占样本。
    ("https://evil.com@attacker.com/", "website", "userinfo(官网那一类没有 host 表兜底)"),
    ("https://a_b.com/", "website", "下划线不是合法域名字符(LDH)"),
    ("https://x.com.evil.com/a", "twitter", "后缀伪装:封闭表必须是**等值**比对"),
    ("https://evilx.com/a", "twitter", "前缀伪装"),
    ("https://notx.com/a", "twitter", "host 不在 twitter 的封闭表里"),
    ("https://t.me.evil.com/x", "telegram", "同上,telegram 侧"),
    ("https://discord.gg.evil.com/x", "discord", "同上,discord 侧"),
    ('https://x.com/a" onmouseover="alert(1)', "twitter", "双引号闭合 href 属性"),
    ("https://x.com/a'>x", "twitter", "单引号 + 尖括号"),
    ("https://x.com/<script>", "twitter", "尖括号"),
    ("https://xn--80ak6aa92e.com/", "website", "punycode:同形字域名的最后一条路"),
    ("https://х.com/", "twitter", "西里尔 х —— 非 ASCII 在字符集那一步就死"),
    ("https://1.2.3.4/x", "website", "IP 字面量(顶级域不是字母)"),
    ("https://[::1]/x", "website", "IPv6 字面量"),
    ("https://x.com:8080/a", "twitter", "带端口"),
    ("https://x.com./a", "twitter", "尾随点(DNS 上等价于 x.com,但不等值)"),
    ("https://evil.zzz/", "website", "顶级域不在封闭表里"),
    ("https://x.co​m/a", "twitter", "零宽空格拆域名"),
    ("https://x.com/a b", "twitter", "URL 里有空格"),
    ("https://x.com/a\nb", "twitter", "URL 里有换行(能在消息里造出一整行伪造字段)"),
    ("https://" + "a" * 300 + ".com/", "website", "超长 URL(顶爆消息预算)"),
    ("https://x.com/%zz", "twitter", "半截百分号编码"),
    ("https://", "website", "只有 scheme"),
    ("https://com/", "website", "只有一段 host"),
    ("https://-evil.com/", "website", "域名段以 - 开头"),
    ("https://x.com\\@evil.com/", "twitter", "反斜杠(部分解析器当 /)"),
    ("", "website", "空串"),
    (None, "website", "None"),
    ("https://x.com/a", "twitter ", "kind 带空格 —— 不在封闭类别表里"),
    ("https://x.com/a", "medium", "未知类别一律不放行"),
    ("https://x.com/a", "", "空类别"),
]


@pytest.mark.parametrize(("url", "kind", "rule"), _URL_MUST_BLOCK,
                         ids=[r[2] for r in _URL_MUST_BLOCK])
def test_恶意URL必须整条丢弃(url, kind, rule):
    assert safe_url(url, kind) is None, f"{url!r}/{kind!r} 本该被拦住({rule})"


@pytest.mark.parametrize(("raw", "rule"), _MUST_BLOCK, ids=[r[1] for r in _MUST_BLOCK])
def test_名字侧的必拦硬基线打进URL槽位也零泄漏(raw, rule):
    """
    ⚠️⚠️ 把 main 上已有的那张必拦表(safe_display 的硬基线)整张打进 URL 的两个槽位。
       它们本来是"名字"形态的攻击串,但一个门禁只要有一条路径把它原样吐出来,
       这条推送就还是脏的。
    """
    assert safe_url(raw, "website") is None, raw
    assert safe_url(raw, "twitter") is None, raw
    assert safe_social_links([("website", raw), ("twitter", raw)]) is None, raw


def test_除官网外每一类都必须有封闭host表():
    """
    ⚠️⚠️ 结构不变量:`safe_url` 里"没有 host 表就放开任意 host"这条兜底是给「官网」用的。
       谁往 SOCIAL_KINDS 里加一个新类别却忘了给它配 host 表,那一类就**静默变成"任意 host"** ——
       一个看不见的降级。这条把它抓出来。
    ⚠️ 类别清单写死在这一侧,不 import SOCIAL_KINDS。
    """
    for kind in ("twitter", "telegram", "discord", "reddit", "github"):
        assert safe_url("https://not-their-domain-at-all.com/x", kind) is None, kind
    # 对照:官网那一类**刻意**放开(见 nameguard 里那段取舍)
    assert safe_url("https://not-their-domain-at-all.com/x", "website") is not None


def test_host与scheme归一成小写但路径大小写原样():
    """⚠️ t.me/AbC != t.me/abc —— 路径的大小写是有意义的,不许动。"""
    assert safe_url("HTTPS://X.COM/AbC", "twitter") == "https://x.com/AbC"


# ============================================================
# safe_social_links:同类只取第一个 / 未知类别跳过 / 顺序固定
# ============================================================
def test_同类只取第一个():
    """⚠️ 有的币把同一个 type 填了好几条。"第一个"以上游顺序为准,不许随机挑。"""
    got = safe_social_links([("twitter", "https://x.com/first"),
                             ("twitter", "https://x.com/second")])
    assert got == (("twitter", "https://x.com/first"),)


def test_单条不合格只丢那一条():
    """⚠️ 三行各自独立的同一条口径:一个坏链接不该把整行带走。"""
    got = safe_social_links([("website", "javascript:alert(1)"),
                             ("twitter", "https://x.com/ok"),
                             ("telegram", "https://t.me/ok")])
    assert got == (("twitter", "https://x.com/ok"), ("telegram", "https://t.me/ok"))


def test_全都不合格整行消失():
    assert safe_social_links([("website", "javascript:x"),
                              ("twitter", "https://evil.com/x")]) is None
    assert safe_social_links([]) is None
    assert safe_social_links(None) is None


def test_未知类别跳过而不是显示原文():
    """
    ⚠️⚠️ 未知 type **不显示原文**。链接文字必须是我们自己的常量,
       "显示原文"等于把上游的自由文本当链接文字印出去 —— 那正是这一轮要堵的口子。
    """
    assert safe_social_links([("medium", "https://medium.com/@x")]) is None
    assert safe_social_links([("已清仓 · 亏损 99%", "https://x.com/a")]) is None


def test_输出顺序固定不随上游数组顺序漂移():
    """⚠️ 同一个币两次推送里链接顺序不一样,会让人以为数据变了。"""
    a = safe_social_links([("telegram", "https://t.me/x"), ("twitter", "https://x.com/y"),
                           ("website", "https://a.com/")])
    b = safe_social_links([("website", "https://a.com/"), ("twitter", "https://x.com/y"),
                           ("telegram", "https://t.me/x")])
    assert a == b == (("website", "https://a.com/"), ("twitter", "https://x.com/y"),
                      ("telegram", "https://t.me/x"))


def test_上游的label永远不出现在返回值里():
    """
    ⚠️⚠️ DexScreener 的 `websites[].label` 是发币的人填的自由文本。门禁返回的是
       **类别**,链接文字由 formatter 用自己的常量渲染 —— 攻击者写什么都到不了消息里。
    """
    got = safe_social_links([("website", "https://a.com/")])
    assert got == (("website", "https://a.com/"),)
    assert "官方客服" not in str(got)


def test_结构畸形不抛异常():
    """⚠️ 上游给什么都不许炸:这条路径挂在推送主路径上。"""
    assert safe_social_links(["not a pair"]) is None
    assert safe_social_links([("only-one",)]) is None
    assert safe_social_links([None, 42, ("twitter", "https://x.com/a")]) == \
        (("twitter", "https://x.com/a"),)
    assert safe_social_links(123) is None


def test_条数封顶():
    """⚠️ 上游可以塞任意多条;不封顶就能把一行顶爆消息预算。"""
    got = safe_social_links([
        ("website", "https://a.com/"), ("twitter", "https://x.com/a"),
        ("telegram", "https://t.me/a"), ("discord", "https://discord.gg/a"),
        ("reddit", "https://reddit.com/r/a"), ("github", "https://github.com/a"),
    ])
    assert len(got) == 6                        # 六个类别各一条 = 上限
