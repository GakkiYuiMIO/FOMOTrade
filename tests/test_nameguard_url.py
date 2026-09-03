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
    # ⚠️⚠️ H5 之后**没有**顶级域白名单了(实测:那张表误杀 8.16% 的真实官网 ——
    #    662 条真实官网里 54 条被丢,其中 42 条纯粹是被这张表误杀的,涉及 29 个不同 TLD,
    #    包括 www.whitehouse.gov / youtu.be / linktr.ee 这种一点都不 exotic 的域;
    #    而表里本来就含 xyz/top/vip/cc/ws/gd/ly —— 对坏人基本不构成约束)。
    #    这一道现在只判**形态**:顶级域必须是纯 ASCII 字母且 >= 2 位。
    ("https://evil.z/", "website", "顶级域只有 1 位"),
    ("https://evil.123/", "website", "顶级域是数字"),
    ("https://evil.co-m/", "website", "顶级域带连字符"),
    ("https://x.co​m/a", "twitter", "零宽空格拆域名"),
    ("https://x.com/a b", "twitter", "URL 里有空格"),
    ("https://x.com/a\nb", "twitter", "URL 里有换行(能在消息里造出一整行伪造字段)"),
    # ⚠️⚠️ 这条**先**死在长度上限(第 2 道:313 > 200),根本走不到 host 那一道(第 5 道)。
    #    safe_url 的门禁顺序是 1 字符集 → 2 长度 → 3 scheme → 4 netloc → 5 host,
    #    上一轮的注释把这条说成"挡在域名段 63 字符上",是**说反了** —— 已实测更正:
    #    把 _MAX_URL_CHARS 调大之后它仍被拦,那时才轮到 _label_ok 兜底。
    #    真正**独占**钉住长度上限与 63 字符段长的用例各在文件末尾。
    ("https://" + "a" * 300 + ".com/", "website", "313 字符:先撞长度上限"),
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


# ============================================================
# ⚠️⚠️ 长度上限(H4)
# ============================================================
# 上面必拦表里那条 `"https://" + "a" * 300 + ".com/"` **不测长度**:
# 它的 host 只有一段 300 字符的标签,先被 `_label_ok` 的 63 字符段长挡下来了 ——
# 把长度上限从 200 改成 100000 跑全量,一条都不红。
# 长度上限是"超长 URL → notifier 盲切 → HTML 不配平 → Telegram 400 整条推送发不出去"
# 这条链路上的**唯一一道闸**,下面两条用**合法**的长 URL(每段 ≤63、TLD 在表里)把它夹住。
_LONG_PATH_HOST = "https://example.com/"          # 20 字符
_LEGAL_200 = _LONG_PATH_HOST + "a" * 180          # 正好 200
_LEGAL_201 = _LONG_PATH_HOST + "a" * 181          # 201


def test_合法但超长的URL被长度上限挡下():
    """
    ⚠️⚠️ 每一段都合法(host 是 example.com,路径只是普通 ASCII),
       只有**总长**越了线 —— 除了长度这一道,没有任何别的判定拦得住它。
    ⚠️ 断言写死 200 / 201,不 import _MAX_URL_CHARS。
    """
    assert len(_LEGAL_200) == 200 and len(_LEGAL_201) == 201
    assert safe_url(_LEGAL_200, "website") == _LEGAL_200, "200 字符本该放行"
    assert safe_url(_LEGAL_201, "website") is None, "201 字符本该被长度上限挡下"


def test_社媒那一侧的长度上限也在():
    """⚠️ 封闭 host 表挡不住长路径:t.me/<很长的一串> 每一段都合法。"""
    ok = "https://t.me/" + "b" * 187                # 13 + 187 = 200
    too_long = "https://t.me/" + "b" * 188          # 201
    assert len(ok) == 200 and len(too_long) == 201
    assert safe_url(ok, "telegram") == ok
    assert safe_url(too_long, "telegram") is None


def test_超长URL在社媒行的收口上也进不去():
    """⚠️ 端到端:safe_social_links 是渲染入口那道收口,长度上限必须在它后面也成立。"""
    assert safe_social_links([("website", _LEGAL_201)]) is None
    assert safe_social_links([("website", _LEGAL_201),
                              ("twitter", "https://x.com/ok")]) == \
        (("twitter", "https://x.com/ok"),)


# ============================================================
# ⚠️⚠️ 顶级域:只判形态,**没有白名单**(H5)
# ============================================================
# 依据(2026-09-03 实测):抽 1600 个生产库里被推送过的代币走 DexScreener,
# 拿到 662 条官网 / 496 个去重域名,旧版的顶级域白名单整条丢掉 54 条 = 8.16%,
# 其中 **42 条是纯粹被那张表误杀的** —— 29 个不同顶级域,包括 www.whitehouse.gov、
# youtu.be、linktr.ee、slate.foundation、archive.ph、striker.cat、burger.mom …
# 而那张表里本来就有 xyz/top/vip/cc/ws/gd/ly,对坏人基本不构成约束。
# 去掉之后同一份样本丢弃率 → 1.81%。
# ⚠️ 下面这些是**样本里真实出现过**的域名,写死字面量。
_REAL_TLD_PASS = [
    "https://www.whitehouse.gov/",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://linktr.ee/somecoin",
    "https://slate.foundation/",
    "https://archive.ph/abcde",
    "https://giwa.markets/",
    "https://striker.cat/",
    "https://burger.mom/",
    "https://stonk.rocks/",
    "https://unicorn.place/",
    "https://pons.company/",
    "https://agentos.services/",
    "https://nov.ag/",
    "https://glados.aperture.institute/",
    "https://xgirls.eth.limo/",
    "https://hoodmorn.ing/",
]


@pytest.mark.parametrize("url", _REAL_TLD_PASS)
def test_真实出现过的冷门顶级域不再被误杀(url):
    """⚠️ 把顶级域白名单加回来的那一刻,这一整组当场红。"""
    assert safe_url(url, "website") == url, url


def test_顶级域仍然要成形():
    """
    ⚠️⚠️ 去掉白名单**不等于**这一道没了。它还留着唯一真正干活的那部分:
       顶级域必须是纯 ASCII 字母且 >= 2 位 —— 挡的是 IP 字面量与尾随点,
       那两样才是"看起来是域名其实不是"的真实路径。
    """
    assert safe_url("https://1.2.3.4/x", "website") is None      # IP 字面量
    assert safe_url("https://192.168.0.1/", "website") is None
    assert safe_url("https://x.com./a", "website") is None       # 尾随点
    assert safe_url("https://evil.z/", "website") is None        # 顶级域 1 位
    assert safe_url("https://evil.123/", "website") is None      # 顶级域是数字
    assert safe_url("https://evil.co-m/", "website") is None     # 顶级域带连字符
    assert safe_url("https://xn--80ak6aa92e.com/", "website") is None   # punycode 仍然拦


def test_放开顶级域没有放开别的任何一道():
    """⚠️ 其余五道必须**原样**还在 —— 这条是 H5 那次改动的回归网。"""
    assert safe_url("http://ok-site.foundation/", "website") is None       # http 仍不放行
    assert safe_url("https://ok@evil.foundation/", "website") is None      # userinfo
    assert safe_url("https://ok.foundation:8443/", "website") is None      # 端口
    assert safe_url("https://ok.foundation/a b", "website") is None        # 空白
    assert safe_url('https://ok.foundation/"x', "website") is None         # 引号
    assert safe_url("https://a.b.c.d.e.f.g.foundation/", "website") is None  # 段数超上限
    assert safe_url("https://ok.foundation/" + "a" * 200, "website") is None  # 超长
    # 社媒各类的封闭 host 表**没有**受影响
    assert safe_url("https://ok.foundation/x", "twitter") is None


def test_域名段63字符上限自己有独占的钉子():
    """
    ⚠️⚠️ 这条是补上一轮的漏:当时**唯一**自称覆盖 63 字符段长的样本
       (`"https://" + "a"*300 + ".com/"`,313 字符)其实先死在长度上限那一道,
       于是把 _label_ok 的 63 改成 6300,全量 pytest **0 红** —— 那道门是裸的。
    ⚠️ 这里的样本**刻意做短**(77 字符,远在 200 的长度上限之内),
       所以它只可能死在段长那一道上。
    """
    ok63 = "https://" + "a" * 63 + ".com/"       # 76 字符,单段正好 63 → 放行
    bad64 = "https://" + "a" * 64 + ".com/"      # 77 字符,单段 64 → 只可能死在段长
    assert len(ok63) < 200 and len(bad64) < 200
    assert safe_url(ok63, "website") == ok63
    assert safe_url(bad64, "website") is None


def test_社媒条数不设上限是因为类别本身封顶():
    """
    ⚠️⚠️ 上一版有个 `_MAX_SOCIAL_LINKS = 6` 的"上限",实际是**死常量** ——
       结果按 SOCIAL_KINDS 逐类去重,同类只取第一个,所以条数天然 <= 类别数(6),
       那个 [:6] 切片永远切不掉任何东西(实测把它改成 100,全量 pytest 0 红)。
       已删掉。这条测试钉住真正在起作用的那个不变量:**上游塞再多也只出 <= 6 条**。
    ⚠️ 死常量 + 空转测试是最糟的组合 —— 它让人以为有一道门,而那道门是画上去的。
    """
    raw = []
    for i in range(50):                       # 每类塞 50 条,共 300 条
        raw.append(("twitter", f"https://x.com/a{i}"))
        raw.append(("telegram", f"https://t.me/b{i}"))
        raw.append(("discord", f"https://discord.gg/c{i}"))
        raw.append(("reddit", f"https://www.reddit.com/r/d{i}"))
        raw.append(("github", f"https://github.com/e{i}"))
        raw.append(("website", f"https://site{i}.com/"))
    got = safe_social_links(raw)
    assert got is not None
    assert len(got) == 6, got                 # 六类各一条,不多不少
    assert [k for k, _ in got] == ["website", "twitter", "telegram",
                                   "discord", "reddit", "github"]
    # 同类只取第一个:取到的是 i=0 那条,不是 i=49
    assert dict(got)["twitter"] == "https://x.com/a0"
    assert dict(got)["website"] == "https://site0.com/"
