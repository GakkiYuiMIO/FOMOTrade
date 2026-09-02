"""
推送里所有**外部文本**(币名、译名、公司名、交易所名)的展示门禁。

============ 为什么不是"再补几个字符进黑/白名单" ============
连续两轮、五个验证者各自独立打出新洞(`·` 伪造字段、纯中文联系方式、手机号、
加 V 信、裸 hex 地址…)。根因不是"过滤规则不够全",而是:

  **不可信文本与可信结构挤在同一行、共用同一个分隔符。**

推送的标题是 `🌱 alice · 首次建仓 · $CUM · {币名}` —— 币名完全攻击者可控。
只要币名里能出现 `·`,他就能凭空长出两个字段:`已清仓 · 亏损 99%`。
就算把 `·` 也禁掉,用空格、全角标点、纯中文照样能伪造。所以这里做三件事:

  1. **形状白名单**(本模块 safe_display):币名/译名/公司名这类东西**有形状** ——
     长度、词数、标点数、数字串、分隔符。不满足形状 → **整段丢弃**。
     形状比字符集强一个量级:`Send SOL to my wallet now` 每个字符都在字符白名单里,
     但它是 6 个词的一句话,不是名字。
  2. **形态收口**(safe_exchange):交易所名不是自由文本,用严格正则收。
  3. **视觉隔离**(在 formatter:所有不可信文本套一层 `「」`)——
     伪造的分隔符明显落在容器**内部**,`🌱 alice · 首次建仓 · $CUM · 「已清仓 · 亏损 99%」`
     一眼看得出后半段是数据不是本文。这一层是 `·` 伪造的**真正解**。

============ safe_display 的六步 ============
1. 删掉所有 Unicode Cf(格式控制)字符 —— 零宽空格 U+200B、RTL 覆盖 U+202E 之类。
   ⚠️ 顺序是"先删再判":先删才能让 `t.<U+200B>me` 还原成 `t.me` 被后面抓到。
   ⚠️ 这条路**连 U+200D(ZWJ)一起删** —— 币名里的 emoji 组合没有正当用途,
      而 ZWJ 能拆开域名。用户自己写的观点正文走的是另一条路(formatter._esc),
      那条**必须**保留 ZWJ,否则 👨‍💻 会被拆成两个 emoji。两条路分开,各自有测试。
1.5 bidi 控制符(RTL 覆盖那一类)**整段丢弃**,不是删掉了事(见 _BIDI_CONTROLS)。
2. 叠平**所有**空白:NBSP U+00A0、表意空格 U+3000、行分隔符 U+2028… 一并归一。
   少了这一层,`t.<NBSP>me` 在下一步隐身。⚠️ 靠的是无参数 `str.split()`(见 flatten)。
3. 必拦形态(命中任一 → 整段丢弃):scheme 头、域名形态、@提及、0x 地址、
   裸 hex 长串、base58 长串。域名**额外在"抽掉全部空白"的形态上再判一次** ——
   `t. me/scam` 读者一眼仍读得出,必须抓住;那一道额外要求顶级域真实存在
   (见 _SPLIT_DOMAIN_TLDS),否则 "U.S.A. Token" 这类缩写名字会被误杀。
4. 字符白名单:CJK + ASCII 字母 + ASCII 数字 + 空格 + 一小撮标点。集合外一律丢弃。
5. 形状:长度 / 词数 / 标点数 / 连续数字 / 含 CJK 时的 ASCII 串长度。
6. 通过 → 返回**清洗过**的那段文本(调用方必须用它,不能再用原串)。

============ 为什么是"整段丢弃"而不是"剔掉坏的那部分" ============
剔一半会拼出一个**似是而非的假名字**("加群 t.me/scam" 剔成 "加群 scam"),
读者看不出它被动过,比整段不显示更糟。缺失整行消失是本项目的既有铁律(铁律 2)。

============ 刻意的取舍(会误伤,是有意的)============
· **表情符号一律不放行。** 本项目的行首 emoji 是聊天列表预览里**唯一的扫描锚点**
  (铁律 1):一个叫 "🔴 已清仓 · 骗你的" 的币,名字印进标题就能伪造一条不存在的卖出行。
· **非 ASCII 拉丁字母(é / ü)与西里尔、希腊字母一律不放行。** 同形字
  (西里尔 `а` 与拉丁 `a` 长得一模一样)正是白名单最经典的绕过手法。
· **`#` `$` `+` 不在白名单里。** 它们各是一条**可达通道**:Telegram 会把
  `#freeairdrop` 渲染成可点 hashtag、`$SCAM` 渲染成 cashtag、`+79001234567`
  在移动端渲染成可拨号链接 —— 攻击者不需要域名也能拿到一个可点击的出口。
· **`·` `•` `|` 不在白名单里。** 它们是本项目的字段分隔符(formatter.SEP),
  放行等于把"造一个假字段"的能力直接交出去。这一条**不许松**。
· **`「` `」` 不在白名单里。** 它们是视觉容器本身;名字里带它就能把自己"移出"容器。
  ⚠️ 不许"转义后放行" —— 转义破坏不了 HTML,但破坏了容器的视觉不变量。

⚠️ 本模块是**纯函数、零依赖**。收口点在 formatter(渲染入口统一过一遍,
   见 formatter.UNTRUSTED_FIELDS);namecn 送翻译前 / 收译文时另外各调一次,
   理由是"省请求"与"别把脏译文写进永久缓存",与显示那道**互相独立**。
"""
from __future__ import annotations

import re
import unicodedata

# ---- 第 1 步:格式控制字符 -------------------------------------------------
# 零宽连接符。emoji 组合(👨‍💻 = 👨 + ZWJ + 💻)全靠它粘合。
ZWJ = "‍"

# ---- 第 2 步:双向文本控制符 → **整段丢弃**(不是删掉了事)-------------------
# ⚠️⚠️ 这一条与别的 Cf 处理**刻意不同**。零宽空格是"把字拆开躲过形态判断",
#    删掉之后剩下的串就是攻击者本来想显示的东西,继续往下判是对的;
#    但 bidi 控制符(LRM/RLM/LRE/RLE/PDF/LRO/RLO/isolate 那一套)唯一的用途是
#    **让显示顺序不等于字符顺序** —— `币<U+202E> pmup` 读者看到的是 `币 pump`。
#    删掉之后剩下的 `币 pmup` 是一个**作者从没写过**的名字,照样印出去等于把
#    "剔掉坏的那部分再显示"做了一遍(见模块头"整段丢弃"一节),那正是本模块拒绝做的事。
#    ⚠️ 零误伤:2026-09-02 实测 248 个真实币名(四条链、DexScreener baseToken.name),
#       含任何 Cf 字符的是 **0** 个。
_BIDI_CONTROLS = frozenset(
    "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"
)

# ---- 第 3 步:必拦形态 ----------------------------------------------------
# ⚠️ 不整体加 IGNORECASE:base58 那条的 [A-HJ-NP-Z] 一旦忽略大小写就会把小写 l 收进去,
#    "Supercalifragilisticexpialidocious" 这种普通长词会被当成地址。

# scheme 形态。⚠️ 只拦 `://` 是不够的:`javascript:alert(1)` / `data:text/html,hi`
#    / `tg:resolve` / `mailto:a@b` 都只有**单个**冒号,数据层拦得住、渲染层却放行过。
#    这里要求冒号**紧跟非空白**(scheme 后面总是立刻接目标),于是
#    "Web3: The Movie"(冒号后是空格)这种正常名字不受影响。
#    这条同时覆盖了老的 `://`(`tg://x` 里 `tg:` 后面紧跟 `/`)。
_RE_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:\S")
# 域名形态:t.me / evil.com / vitalik.eth / discord.gg。
# ⚠️ 点后面必须**紧跟** 2 个以上字母才算,于是:
#    "Inc." "Corp."(点在词尾、后面是空格或结尾)不匹配;
#    "U.S.A."(点之间只有单字母)不匹配;"Web3.0"(点后是数字)不匹配。
_RE_DOMAIN = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}")
# @提及。@ 本来就不在白名单里,这条只是把意图写明白。
_RE_MENTION = re.compile(r"@[A-Za-z]")
# EVM 地址。⚠️ 4 位起,不是 6 位:实测 "去 0xabcd 领" 这种短地址一样是钓鱼话术。
_RE_EVM = re.compile(r"0[xX][0-9a-fA-F]{4,}")
# **不带 0x 前缀**的裸 hex 长串。0x 那条要字面量 0x、base58 那条的字符集不含 '0',
# 于是 "Send 7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18" 曾经从两条规则中间漏过去。
# 16 位:实测真实名字(见 tests 的真实样本表)不会出现 16 位全 hex 的串。
_RE_BARE_HEX = re.compile(r"[0-9a-fA-F]{16,}")
# Solana 地址形态:base58 字母表(无 0 O I l)连续 ≥26 位。地址是 32~44 位,26 留了余量。
_RE_BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{26,}")

# 在**叠平后的文本**上判的规则。
_BAD_PATTERNS = (_RE_SCHEME, _RE_DOMAIN, _RE_MENTION, _RE_EVM, _RE_BARE_HEX, _RE_BASE58)

# ---- 第 3.5 步:被空白拆开的域名 -------------------------------------------
# `t. me/scam`(NBSP / 表意空格 / 换行拆出来的)读者一眼仍读得出,必须抓住 ——
# 但**不能**直接把 _RE_DOMAIN 套在"抽掉全部空白"的形态上:那样会误杀一大批真名字,
# 因为英文里"点 + 空格"是缩写与句点的常态:
#     "U.S.A. Token"                          → 抽空白成 "U.S.A.Token" → 匹配 "A.Token"
#     "Space Exploration Technologies Corp. Class A" → 匹配 "Corp.ClassA"
# 两条都是真实存在的正常名字(前者在硬基线里,后者是实测样本)。
# ⚠️⚠️ 所以这一道**额外要求顶级域是真实存在的那一小撮**:
#    一个被空白拆开的"域名"只有在读起来像**真域名**时才骗得到人,而这需要一个真 TLD。
#    "A.Token" / "Corp.ClassA" / "St.Louis" 的尾巴都不是 TLD,读者也不会去访问它们。
#    这一层只管"空白拆开"这一种绕过;没被拆开的域名由上面的 _RE_DOMAIN 原样拦住,
#    与 TLD 是什么无关(`www.evil-airdrop.com` / `vitalik.eth` 都走那条)。
# ⚠️ 表里是**加密钓鱼实际用的**那些 + 几个通用大户。宁可这张表短:
#    漏一个冷门 TLD 只意味着"被空白拆开 **且** TLD 冷门"这一种组合逃过第二道;
#    表长了则开始误杀真名字("Alpha.Studio" 这种)。
_SPLIT_DOMAIN_TLDS = frozenset({
    "com", "net", "org", "io", "co", "me", "gg", "xyz", "app", "fun",
    "link", "live", "vip", "top", "cc", "ru", "cn", "info", "biz", "site",
    "online", "club", "pro", "tv", "ws", "to", "sh", "gd", "ly", "am",
    "fm", "at", "st", "eth", "sol", "bio", "one", "pw", "su", "space",
    "store", "tech", "world", "team", "chat", "cash", "finance", "money",
})
# 与 _RE_DOMAIN 同形,只是把顶级域单独抓出来供上面那张表判。
_RE_SPLIT_DOMAIN = re.compile(r"[A-Za-z0-9-]+\.([A-Za-z]{2,})")


def _looks_like_split_domain(compact: str) -> bool:
    """抽掉空白之后是否长出了一个**真域名**(顶级域在白名单里)。理由见上面。"""
    return any(m.group(1).lower() in _SPLIT_DOMAIN_TLDS
               for m in _RE_SPLIT_DOMAIN.finditer(compact))

# ---- 第 4 步:字符白名单 --------------------------------------------------
# 中日韩(含假名、谚文)。与 namecn._CJK 同一份范围。
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
# 允许的标点。⚠️ 全角标点(,。、;:!?"")**不在**里面:名字不带全角标点,
#    带全角标点的是句子 —— 而推送里不需要显示任何人写给读者的句子。
#    ⚠️ `#` `$` `+`(Telegram 可点通道)、`·` `•`(字段分隔符)、`「` `」`(视觉容器)
#       与 `;` 都**刻意不在**里面,理由见模块头。
_ALLOWED_PUNCT = frozenset(" .,'\"-&()!?/%:~")

# ---- 第 5 步:形状 --------------------------------------------------------
# ⚠️⚠️ 下面四个阈值是**实测**定的,不是拍脑袋:先把 13 个真实样本
#    (Cummingtonite / Cash Cat / dogwifhat / Artificial Inu / Quantum White Fiber Rabbit /
#     USA Rare Earth, Inc. / WhiteFiber, Inc. / NVIDIA Corporation / SPDR S&P 500 ETF Trust /
#     Space Exploration Technologies Corp. / 镁铁闪石 / 美国稀土公司 / 英伟达)
#    写成参数化回归基线(tests/test_nameguard_shape.py),再让阈值恰好全放行。
#    **放行真实样本优先于拦住边缘攻击样本** —— 除了 `·` 与 6 位数字这两条,它们不许松。
#
# 长度上限 40:最长的真实样本 "Space Exploration Technologies Corp." 是 36 字符,留一点余量。
#   ⚠️ 这不是显示限长(那是 formatter 的 _TOKEN_NAME_CHARS=32 / _TOKEN_ZH_CHARS=24),
#      这是"还算不算一个名字"的上限:再长就是营销文案。
_MAX_CHARS = 40
# 长度下限 1:**刻意不设 2** —— 单字的中文译名("猫" / "犬")是真实存在的正常结果,
#   而一个字的串本身没有任何攻击面(装不下域名、地址、联系方式)。
#   "送不送去翻译"那道门另有 len>=2 的判断(namecn.translatable),那是省请求,不是安全。
_MIN_CHARS = 1
# 词数上限 5:"SPDR S&P 500 ETF Trust" 正好 5 词(这是实测把上限从 4 抬到 5 的那个样本);
#   "Quantum White Fiber Rabbit" 4 词。而 "Send SOL to my wallet now" 6 词、
#   "Buy now 100% safe visit my profile" 7 词 —— 一句话总比一个名字长。
_MAX_WORDS = 5
# 标点上限 3:2026-09-02 实测 248 个真实币名的标点数分布是 {0: 235, 1: 11, 2: 2},
#   实测上界只有 2("USA Rare Earth, Inc." / "WhiteFiber, Inc.":`,` 与 `.`);
#   抬到 3 是为了放行**缩写式**的名字 —— "U.S.A. Token" 光点就有 3 个,
#   而它是一个正常名字(2 上限时它被误杀,这是本轮实测发现的唯一一条真误杀)。
#   代价评估:抬这一格不会多放行任何一条攻击基线(伪造字段靠的是 `·`/全角标点,
#   那两类根本不在字符白名单里;话术类靠词数与长数字拦),所以是零成本。
_MAX_PUNCT = 3
# 连续数字上限 5 位:手机号(13800138000)、QQ 号(3355778899)一律 ≥6 位;
#   而 "1000X"(4 位)、"SPDR S&P 500"(3 位)是正常的。**这一条不许松。**
_RE_LONG_DIGITS = re.compile(r"[0-9]{6,}")
# 含 CJK 时允许的 ASCII 字母串上限 4 位。中文译名不会夹带长英文单词;
#   4 位是为了放行 "SPDR" / "ETF" / "AI" 这类代号(与 has_new_ascii_word 的 ≥4 同源判断)。
#   这一条专治 "私聊我领空投 加V信 abcdefg" —— 3 个词、0 标点、0 长数字,
#   靠长度/词数/标点全拦不住,但 "abcdefg" 是 7 位。
_RE_ASCII_RUN = re.compile(r"[A-Za-z]{5,}")

# ---- 交易所名(不是自由文本,按已知形态收)----------------------------------
# fullExchangeName 是交易所代码,形态固定:NasdaqGM / NYSE / NYSEArca / NYSE American / AMEX。
# 字母开头,只许字母数字空格点横杠,总长 ≤20。不匹配 → 那一段不显示
# (🏢 整行仍可显示 ticker + 公司名)。比套通用门禁更贴合这个字段。
_RE_EXCHANGE = re.compile(r"[A-Za-z][A-Za-z0-9 .\-]{0,19}")

# 译文里"凭空长出来的英文单词"的长度下限。3 位以下(ETF / AI / DNA)是正常的中文行文,
# 4 位起才像是被塞进来的东西。
_ASCII_WORD = re.compile(r"[A-Za-z]{4,}")


def strip_format_controls(s) -> str:
    """
    删掉**所有** Unicode Cf(格式控制)字符:零宽空格、零宽连接符、RTL 覆盖、字节序标记…

    ⚠️ 这是**不可信名字**那条路用的版本,ZWJ 也删 —— 币名里的 emoji 组合没有正当用途,
       而 ZWJ 能把域名拆开躲过形态判断。用户自己写的正文走 strip_controls_keep_emoji。
    """
    return "".join(ch for ch in str(s or "") if unicodedata.category(ch) != "Cf")


def strip_controls_keep_emoji(s) -> str:
    """
    删 Cf,但**保留 U+200D(ZWJ)**。

    ⚠️⚠️ 这条是给**用户自己写的内容**(观点正文 thesis_text、昵称、symbol)用的。
       U+200D 的 unicodedata.category 正是 'Cf',而它是所有组合 emoji 的粘合剂:
       全删会把 👨‍💻 拆成 "👨💻"、把 🏳️‍🌈 拆成白旗 + 彩虹 —— 那是对既有行为的破坏。
    ⚠️ U+FE0F(变体选择符)的类别是 'Mn' 不是 'Cf',本来就不会被删,这里一并说明,
       免得下一个人以为它没被考虑过。
    ⚠️ U+202E(RTL 覆盖)之类仍然删:它能让后面的文字反向显示,是纯粹的视觉欺骗。
    """
    return "".join(ch for ch in str(s or "")
                   if ch == ZWJ or unicodedata.category(ch) != "Cf")


def flatten(s) -> str:
    """
    删 Cf → 叠平**所有**空白。**不可信文本的所有判断**都在这个结果上做。

    ⚠️⚠️ "空白替身"(NBSP U+00A0、表意空格 U+3000、行分隔符 U+2028…)必须一并归一:
       少了这一层,`t.<NBSP>me` 在域名规则面前隐身,而 Telegram 渲染出来读者一眼
       仍读得出 `t. me`。
    ⚠️ 这里**不需要**手写一张 Z* 替换表 —— 无参数的 `str.split()` 按 Unicode 判空白,
       Zs/Zl/Zp **全体**(以及 \\t \\r \\n)都在内。这条前提由
       tests/test_nameguard_shape.py::test_每一个Unicode空白都算空白 逐码点验着;
       ⚠️ 但它只对**无参数**的 split 成立:写成 `split(" ")` 这一层当场失效。
    """
    return " ".join(strip_format_controls(s).split())


def _char_ok(ch: str) -> bool:
    if ch in _ALLOWED_PUNCT:
        return True
    if ch.isascii() and (ch.isalpha() or ch.isdigit()):
        return True
    return bool(_CJK.match(ch))


def _shape_ok(text: str) -> bool:
    """形状判断 —— 比字符白名单强一个量级的那一层。阈值与理由见上面的常量注释。"""
    if not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
        return False
    if len(text.split()) > _MAX_WORDS:
        return False
    if sum(1 for ch in text if ch in _ALLOWED_PUNCT and ch != " ") > _MAX_PUNCT:
        return False
    if _RE_LONG_DIGITS.search(text):
        return False
    if _CJK.search(text) and _RE_ASCII_RUN.search(text):
        return False
    return True


def safe_display(s) -> str | None:
    """
    → 可以放进推送的那段文本;不合格 **整段丢弃**(返回 None,调用方让那一行消失)。

    ⚠️ 返回值是**清洗过**的(Cf 已删、空白已归一叠平),调用方必须用它,不能再用原串。
    ⚠️ 幂等:safe_display(safe_display(x)) == safe_display(x) —— formatter 在入口统一
       过一遍之后,下游单行渲染函数再过一遍不会改变结果(那是刻意保留的第二道)。
    """
    # ⚠️ 这一条判的是**原串**,必须在 flatten(会把 Cf 删掉)之前:
    #    删完就看不出它来过了,而"它来过"本身就是丢弃的理由(见 _BIDI_CONTROLS)。
    if any(ch in _BIDI_CONTROLS for ch in str(s or "")):
        return None
    text = flatten(s)
    if not text:
        return None
    for pat in _BAD_PATTERNS:
        if pat.search(text):
            return None
    if _looks_like_split_domain(text.replace(" ", "")):
        return None
    if not all(_char_ok(ch) for ch in text):
        return None
    if not _shape_ok(text):
        return None
    return text


def safe_exchange(s) -> str | None:
    """
    交易所名(Yahoo 的 fullExchangeName)的门禁 —— 严格正则,不匹配 → None。

    ⚠️⚠️ 这个字段曾经**全程零门禁**:同一份 Yahoo 响应里 longName / company_zh 都套了
       门禁,唯独 fullExchangeName 漏了,于是被篡改的代理能把
       `立即访问 t.me/free-airdrop` 原样送进 🏢 行。
    ⚠️ 它不是自由文本,所以不套通用的形状白名单,而是按已知形态收:
       字母开头 + 字母/数字/空格/点/横杠,总长 ≤20("NYSE American" 13、"NasdaqGM" 8)。
    ⚠️ bidi 控制符与 safe_display 同一条口径:来过就整段丢弃,理由见 _BIDI_CONTROLS。
    """
    if any(ch in _BIDI_CONTROLS for ch in str(s or "")):
        return None
    text = flatten(s)
    if not text:
        return None
    return text if _RE_EXCHANGE.fullmatch(text) else None


def has_new_ascii_word(zh, source_text) -> bool:
    """
    译文里出现了**原文中没有的** ASCII 字母串(≥4 位)吗?

    正常的中文译名不会凭空长出英文单词。凭空长出来的只有两种可能:
    代理篡改了响应,或者维基把条目重定向到了另一个英文名
    (SPCX 的 "Space Exploration Technologies Corp." → 中文维基条目就叫 "SpaceX",
     那是**英文**,塞进"中文名"槽位里是错的)。
    ⚠️ 原文里有的不受影响:"SpaceX" → "SpaceX" 判为假。
    """
    src = flatten(source_text).lower()
    return any(w.lower() not in src for w in _ASCII_WORD.findall(flatten(zh)))
