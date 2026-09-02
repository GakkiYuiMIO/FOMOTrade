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
· **分隔符按"有没有容器"分两套口径**(见下面一节),不再一刀切。
· **`「` `」` 在名字侧与 ident 侧**都**直接丢弃。** 它们是视觉容器本身;
  名字里带它就能把自己"移出"容器。
  ⚠️ 不许"转义后放行" —— 转义破坏不了 HTML,但破坏了容器的视觉不变量。

============ 分隔符:名字侧放行、ident 侧严禁(本轮的核心修正)============
上一轮把"分隔符"这一类漏在了两套规则之外,于是同一个字符串 `已清仓 · 亏损 99%`
在一个测试文件里是"必拦"、在另一个里被断言"必放行" —— safe_display 把它当形状规则
拦掉了,safe_ident 把它当形状规则放行了。而 **ident 恰恰是唯一不套「」容器的槽位**:
最需要拦分隔符的地方反而最松。

真正的判据不是"这个字符危不危险",而是**它印在容器里还是容器外**:

  · 币名 / 译名 / 公司名 一律印在 `「」`**里面** → 伪造出来的分隔符被容器困住,
    读者一眼看得出那是一段数据 → `·`(U+00B7)`•`(U+2022)`・`(U+30FB)**放行**。
    ⇒ 顺带修好一大批真误杀:`Costco • Robinhood Token`(币股原名)、
      `DOG•GO•TO•THE•MOON`、`尼基塔·比尔`、`南希·佩洛西`(音译人名的间隔号)。
  · symbol / handle / 昵称 **不套容器**,直接和推送自己的 SEP(' · ')平级 →
    任何分隔符类字符 **一律严禁**(safe_ident,判据见 is_separator_char)。
    ⇒ 就分隔符这一类而言,**ident 的规则集是名字规则集的严格超集**。
    这条关系写成了可执行测试(tests/test_nameguard_sep.py),两份规则不可能再互相矛盾。

  ⚠️ 名字侧仍留一条**形状**规则:含中日韩文字时,分隔符两侧不许有空格(见
     _cjk_spaced_separator)。理由是实测 —— 中文排版里音译人名的间隔号**紧贴两侧**
     (`南希·佩洛西`),而 `已清仓 · 亏损 99%` / `官方认证 · 已审计` 带空格,
     是在**模仿 SEP 的形态**。纯 ASCII 那侧不能加这条:`Costco • Robinhood Token`
     就是一个带空格的**真币股原名**,拦掉它等于把整批币股名打回上一轮的状态。

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
# ⚠️⚠️ **tag 序列**那段码点(U+E0020–U+E007F)的 unicodedata.category 同样是 'Cf'。
#    地区旗 = 🏴 + 6 个 tag 字符 + 终止符 U+E007F:
#      🏴󠁧󠁢󠁳󠁣󠁴󠁿 苏格兰 / 🏴󠁧󠁢󠁥󠁮󠁧󠁿 英格兰 / 🏴󠁧󠁢󠁷󠁬󠁳󠁿 威尔士。
#    只保 ZWJ 不保这一段,苏格兰旗被拆成一面**光秃秃的黑旗** 🏴 —— 用户自己写的正文里
#    的旗子当场变成另一个东西,没有任何报错。这是相对 main 的行为退化,补在这里。
#    ⚠️ 它只对**用户自己写的内容**那条路(strip_controls_keep_emoji)成立;
#       不可信名字那条路(strip_format_controls)连 tag 字符一起删,两条路各有测试。
TAG_MIN = 0xE0020
TAG_MAX = 0xE007F

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
# ⚠️⚠️⚠️ 中日韩的判定**绝不许写成码点区间**。上一版写的是
#     `[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]`,
#     第一段 U+3040–U+30FF 里混着 **61 个非字母码点**,其中 U+30FB
#     (KATAKANA MIDDLE DOT,类别 Po)视觉上就是一个全角中点,与本项目的字段分隔符
#     SEP(' · ')**同形** —— 于是"`·` 不许松"那条实际上已经破了:攻击者用 U+30FB
#     就能原样重造 `已清仓 ・ 亏损 99%`。同一段里还混进了 U+309B/U+309C(Sk 浊音符)、
#     U+30A0(Pd 连字号)等。
#     手写码点区间**必然**混进标点 —— 这本来就是一个**类别查询**问题,就用类别查询解:
#
#       unicodedata.category(ch) == 'Lo'(其他字母)+ 字符名前缀限定在汉字/假名/谚文,
#       外加两个**必需**的 Lm(修饰字母):长音符 ー 与叠字符 々。
#
#     两者都不属于 P*/S*/Z*/C*,所以不可能再混进任何标点、空白或控制符。
#     这条前提由 tests/test_nameguard_shape.py::test_CJK白名单里没有任何标点空白控制符
#     **逐码点**(整个 0x110000 空间)验着 —— 那条断言就是防 U+30FB 再混进来的护栏。
_CJK_LO_PREFIXES = (
    "CJK UNIFIED IDEOGRAPH",
    "CJK COMPATIBILITY IDEOGRAPH",
    "HIRAGANA LETTER",
    "KATAKANA LETTER",
    "HANGUL SYLLABLE",
    "HANGUL LETTER",
)
# 这两个是 Lm(修饰字母)不是 Lo,但少了它们真名字就写不出来:
#   ー = KATAKANA-HIRAGANA PROLONGED SOUND MARK(コーヒー)
#   々 = IDEOGRAPHIC ITERATION MARK(佐々木)
# ⚠️ 逐个按**字符名**列,不按码点区间 —— 区间正是上面那个洞的成因。
_CJK_LM_NAMES = frozenset({
    "KATAKANA-HIRAGANA PROLONGED SOUND MARK",
    "IDEOGRAPHIC ITERATION MARK",
})


def _char_name(ch: str) -> str:
    """字符的 Unicode 名字;没有名字(私用区 / 未分配 / 控制符)返回空串。"""
    try:
        return unicodedata.name(ch)
    except ValueError:
        return ""


def is_cjk_char(ch: str) -> bool:
    """这个字符算不算"中日韩文字"。⚠️ 判据是 Unicode 类别 + 字符名,**不是码点区间**。"""
    cat = unicodedata.category(ch)
    if cat == "Lo":
        return _char_name(ch).startswith(_CJK_LO_PREFIXES)
    if cat == "Lm":
        return _char_name(ch) in _CJK_LM_NAMES
    return False


def has_cjk(s) -> bool:
    """整串里有没有中日韩文字。⚠️ namecn 判"要不要送去翻译"复用这一个,不另立一份。"""
    return any(is_cjk_char(ch) for ch in str(s or ""))


# ---- 分隔符类字符:**纯字符名查询**,不是手写码点表,也不再有类别闸门 -----------
# ⚠️⚠️ 与 E2(CJK 判定)同一条教训:手写一张 `·•|` 的表**必然**漏同形字 ——
#    U+30FB(片假名中点)、U+FF5C(全角竖线)、U+0387(希腊分隔号)、U+2219(点运算符)、
#    U+2502(制表竖线)…… 每一个都能原样重造 `已清仓 ・ 亏损 99%`。
#    所以这里用**查询**:Unicode **字符名**里含下面这几个词(中点、圆点、竖线、
#    分词符那几族)。这把"视觉上可作分隔"表达成了可枚举、可逐码点自测的东西。
# ⚠️⚠️⚠️ **类别闸门(`category(ch)[0] in "PS"`)本轮删掉**。上一版写着它、
#    并在注释里说"字母天然排除" —— 那句话是**错的**。Unicode 把一批
#    **视觉上就是中点 / 竖线**的字符放在**字母类**里,它们从闸门下面整批漏过去:
#        U+A78F Lo LATIN LETTER SINOLOGICAL DOT  ꞏ   ← 字面就是中文的间隔号
#        U+1427 Lo CANADIAN SYLLABICS FINAL MIDDLE DOT  ᐧ
#        U+18DF Lo CANADIAN SYLLABICS FINAL RAISED DOT  ᣟ
#        U+01C0 Lo LATIN LETTER DENTAL CLICK  ǀ   / U+01C1 LATERAL CLICK  ǁ
#        U+02C8 Lm MODIFIER LETTER VERTICAL LINE  ˈ
#    实测(2026-09-03)`safe_ident('AB{x}CD')` 对以上全部**原样放行**。
#    闸门删掉之后命中数 249 → 321,**一个旧命中都没丢**(逐码点比对过)。
# ⚠️⚠️ 删掉闸门之后唯一需要补的是一条**反向**规则(见 is_separator_char 里那两行):
#    **字母**的名字里出现 `X WITH Y` 时,那是"带修饰的字母本身"而不是那个修饰 ——
#        Ŀ / ŀ  LATIN {CAPITAL,SMALL} LETTER L WITH MIDDLE DOT(加泰罗尼亚语的 l·l)
#        Ҝ / ҹ  CYRILLIC … LETTER … WITH VERTICAL STROKE
#        ڂ ݟ ݫ  ARABIC LETTER … WITH TWO DOTS VERTICALLY ABOVE
#        𐼐 𐼗   OLD SOGDIAN LETTER … WITH VERTICAL TAIL
#    它们读起来仍是一个字母,不是一个分隔符,收进来就是误伤真实文本。
#    ⚠️ 这条**只对字母类(category 以 L 开头)生效**:符号名字里的 WITH 描述的是
#       符号自身的构造(`⍿` VERTICAL LINE WITH MIDDLE DOT、`⸠` LEFT VERTICAL BAR
#       WITH QUILL 都是货真价实的竖线同形字),对符号也排除会**收窄 50 个旧命中**
#       —— 实测过,那是往回开洞。
# ⚠️⚠️ 这里**曾经**还有一条"NFKC 折叠到 `·` `•` `・` `|` 四个基准字符之一"的规则
#    与那张四字符基准表 —— 两者都是**死规则**:逐码点跑完 0x110000,
#    "只有 NFKC 抓得到、字符名抓不到"的码点是 **0** 个(把 NFKC 那条打掉全量测试 0 红,
#    实测过)。原因是全角/半角同形字的 Unicode 名字里本来就带着基准字符的名字
#    (U+FF5C 叫 FULLWIDTH VERTICAL LINE、U+FF65 叫 HALFWIDTH KATAKANA MIDDLE DOT)。
#    死规则 + 空转测试是最糟的组合,一并删掉。
# ⚠️ 下面每一个 marker 都有**独占样本**钉在 tests/test_nameguard_sep.py 的
#    _MUST_BE_SEPARATOR 里:删掉任意一个,那条当场红。
# ⚠️⚠️ `IDEOGRAPHIC FULL STOP` 这个 marker **刻意只收半角的那一个**
#    (U+FF61 ｡,写成 "HALFWIDTH IDEOGRAPHIC FULL STOP"):半角句点是一个小圆点,
#    与 `·` 同形;而全角的 `。`(U+3002)是一个明显的大圈,读者不会把它读成字段分隔,
#    并且它被 tests/test_nameguard_sep.py 的 _MUST_NOT_BE_SEPARATOR 明确钉成"不是分隔符"。
_SEP_NAME_MARKERS = (
    "MIDDLE DOT",          # U+00B7 · / U+30FB ・ / U+16EB ᛫ / U+1427 ᐧ(Lo!)
    "BULLET",              # U+2022 • / U+2043 ⁃ / U+2219 ∙ / U+25E6 ◦
    "VERTICAL",            # U+007C | / U+2502 │ / U+FF5C ｜ / U+2016 ‖ / U+02C8 ˈ(Lm!)
    "DOT OPERATOR",        # U+22C5 ⋅
    "ONE DOT LEADER",      # U+2024 ․
    "TWO DOT LEADER",      # U+2025 ‥
    "HYPHENATION POINT",   # U+2027 ‧
    "DIVIDES",             # U+2223 ∣
    "ANO TELEIA",          # U+0387 ·(希腊分隔号)
    "RAISED DOT",          # U+2E33 ⸳ / U+18DF ᣟ(Lo!)
    "RING POINT",          # U+2E30 ⸰
    "SPOT",                # U+2981 ⦁(Z NOTATION SPOT)
    "WORD DIVIDER",        # U+1039F 𐎟 / U+103D0 𐏐 / U+12470 𒑰
    "WORD SEPARATOR",      # U+10101 𐄁 / U+1091F 𐤟 / U+1123A 𑈺 / U+2E31 ⸱
    "TRICOLON",            # U+205D ⁝
    "DOT PUNCTUATION",     # U+205A ⁚ / U+2056 ⁖ / U+2058 ⁘ / U+2059 ⁙
    "MODIFIER LETTER COLON",          # U+A789 ꞉(Sk,与 `:` 同形)
    "DOUBLE HYPHEN",       # U+2E40 ⹀ / U+30A0 ゠(Pd)
    "SINOLOGICAL DOT",     # U+A78F ꞏ(Lo!字面就是中文间隔号)
    "CLICK",               # U+01C0 ǀ / U+01C1 ǁ / U+01C2 ǂ / U+01C3 ǃ(全是 Lo!)
    "HALFWIDTH IDEOGRAPHIC FULL STOP",  # U+FF61 ｡(只收半角,理由见上)
)


def is_separator_char(ch: str) -> bool:
    """
    这个字符视觉上能不能当**字段分隔符**用。⚠️ ident 侧一律严禁,理由见模块头。

    ⚠️ 判据是 Unicode **字符名**,**不是**手写码点表(手写表必然漏同形字),
       也**不再有类别闸门** —— 一批中点 / 竖线同形字被 Unicode 归在字母类里,
       闸门把它们整批放行了(见上面那段)。
    """
    name = _char_name(ch)
    if not name:
        return False
    # ⚠️ 字母名字里的 `X WITH Y` = 带修饰的字母本身(Ŀ 是 L 加一点),不是那一点。
    #    只对字母生效:对符号也排除会收窄 50 个旧命中,理由见上面那段。
    if unicodedata.category(ch)[0] == "L" and " WITH " in name:
        return False
    return any(m in name for m in _SEP_NAME_MARKERS)


# 名字侧(有 `「」` 容器)放行的那三个分隔符。⚠️ **只有这三个** ——
#   它们是实测真名字里真实出现的("Costco • Robinhood Token" / "尼基塔·比尔" /
#   日文名里的 U+30FB),别的分隔符没有任何真实样本,留在集合外。
# ⚠️ 它们**不计入** _MAX_PUNCT,自己有一格上限 _MAX_SEP:
#    "DOG•GO•TO•THE•MOON"(4 个 •)是真实存在的币名,挤在标点那格里会被误杀。
_SEP_PUNCT = frozenset("·•・")
# 分隔符个数上限 5:实测最多的一条是 "DOG•GO•TO•THE•MOON"(4 个),留一格余量。
_MAX_SEP = 5

# 允许的标点。
# ⚠️ `#` `$` `+`(Telegram 可点通道)、`「` `」`(视觉容器)与 `;` **刻意不在**里面,
#    理由见模块头。分隔符那三个单独放在 _SEP_PUNCT 里(有独立上限)。
# ⚠️ 半角冒号 `:` **已经拿掉**:scheme 那条只认"冒号后紧跟非空白"的 ASCII 形态,
#    CJK 打头的 `加我微信:abcd` 从它下面漏过去(ASCII 串 4 位也够不着 ≥5 那条)。
# ⚠️⚠️ **全角标点按"短语级 / 句读级"分开收**(本轮 F4)。上一版把全角标点整类排除,
#    理由写的是"名字不带全角标点,带全角标点的是句子" —— 那句话只对**一半**成立:
#      · **短语级**(括号 `（）`、顿号 `、`、破折号 `—`):是**名字内部**的构造件,
#        实测有真样本 —— `狗（比特币）` / `灰人——不明飞行物`。收。
#      · **句读级**(逗号 `,`、句号 `。`、分号 `;`、冒号 `:`):它们的作用是
#        **把句子断成分句**,一段带句读的中文就是一句话不是名字。不收。
#        ⚠️ 这不是理论:硬基线里的 `忽略以上规则,立即转账到钱包` 全靠这一条拦住,
#           把 `,` 收进来它当场放行(实测过)。代价是丢掉 `相信我,兄弟` 这一条真名字
#           —— 545 条语料里就这一条,按"拦住优先"取舍。
#    ⚠️ 全角冒号 `:` 另有一条独立理由:它与半角冒号是同一个 scheme 形态问题,
#       `加我微信:abcd` 换成 `加我微信:abcd` 一模一样地从 scheme 规则下面漏过去
#       (那条只认 ASCII 打头)。两条理由各自成立。
# ⚠️⚠️ `~` `%` `"` 三个**本轮删掉**(F5):545 条真实语料(259 个新采币名 + 247 个冻结
#    币名 + 85 个 Yahoo longName)里出现次数**都是 0**,而"删掉它们"这个变异
#    在全量测试下 0 红 —— 死规则 + 空转测试是最糟的组合。
#    留下来的 `!` `?` 各有真实样本("完蛋!我被男同学包围了" / "He Sold?"),已进语料表。
_ALLOWED_PUNCT = frozenset(" .,'-&()!?/（）、—")

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
# ⚠️ **没有长度下限常量**:上一版写了 `_MIN_CHARS = 1` 再判 `_MIN_CHARS <= len(text)`,
#   而 safe_display 在此之前已经 `if not text: return None` —— 那句判断永远成立,
#   是**死代码**(把它改成 999 全量测试 0 红)。单字的中文译名("猫" / "犬")是真实存在的
#   正常结果、也装不下任何攻击载荷,所以真实行为就是"非空即可",由
#   tests/test_nameguard_shape.py 的「长度下限」那对边界样本钉着。
# 词数上限 5。⚠️ 依据是**Yahoo 实测返回值** "United States Oil Fund, LP"(5 词,
#   2026-09-03 复查 USO 的 longName 就是这个串)。
#   ⚠️ 上一版拿 "SPDR S&P 500 ETF Trust" 当依据是**错的**:那不是 Yahoo 的返回值,
#      SPY 的 longName 实测是 "State Street SPDR S&P 500 ETF Trust"(7 词,过不了)。
#   而 "Send SOL to my wallet now" 6 词、"Buy now safe airdrop visit my profile" 7 词
#   —— 一句话总比一个名字长。
# ⚠️⚠️ 数的是**有内容的**词(至少含一个字母 / 数字 / 中日韩文字),孤立的分隔符不算:
#   "Circle Internet Group • Robinhood Token" 是 5 词不是 6 词 —— 它是一个真币股的
#   **上游原名**(🌊 那行排查时看的就是它),中间那个 `•` 不该白吃掉一格词数预算。
_MAX_WORDS = 5
# 标点上限 3:2026-09-02 实测 248 个真实币名的标点数分布是 {0: 235, 1: 11, 2: 2},
#   实测上界只有 2("USA Rare Earth, Inc." / "WhiteFiber, Inc.":`,` 与 `.`);
#   抬到 3 是为了放行**缩写式**的名字 —— "U.S.A. Token" 光点就有 3 个,
#   而它是一个正常名字(2 上限时它被误杀,这是本轮实测发现的唯一一条真误杀)。
#   代价评估:抬这一格不会多放行任何一条攻击基线(话术类靠词数与长数字拦)。
# ⚠️ 分隔符那三个(`·` `•` `・`)**不计入这一格**,它们在 _MAX_SEP 里另算。
_MAX_PUNCT = 3
# ⚠️⚠️ 数字规则判的是**整段里的数字总个数**,不是"连续几位"。
#   上一版写的是 `[0-9]{6,}`(连续 ≥6 位),而 `-` `.` 空格都在字符白名单里、
#   标点上限 3、词数上限 5 —— 于是手机号只要插两个分隔符就平凡绕过:
#       '138-0013-8000' / '138 0013 8000' / '138.0013.8000'  全部原样放行,
#   渲染出来就是 `🌱 maxpain · 首次建仓 · $CUM · 「138-0013-8000」`。
#   "连续 N 位"这种规则**必然**被分隔符绕开,所以改成总量。
#   ⚠️ 总量规则**严格强于**连续规则(连续 n 位 ⇒ 总量至少 n),原来那条因此删掉,
#      不留一条被完全覆盖的死规则(死代码 + 空转测试是最糟的组合)。
# 上限 5:2026-09-02 实测 247 个真实币名的**数字总量**分布是
#   {0: 214, 1: 20, 2: 8, 3: 3, 4: 2},实测上界 4('NASDAQ 6900');
#   85 个 Yahoo longName 的上界也是 4('iShares Russell 2000 ETF')。
#   留一格余量取 5,于是 '1000X'(4)、'Web3'(1)、'SPDR S&P 500 ETF Trust'(3)放行,
#   而手机号(11)、QQ 号(10)、'138-0013-8000'(11)一律拦下。**这一条不许松。**
# ⚠️⚠️ 数的**不只是 ASCII 数字**(本轮 F6):上一版写的是 `ch.isascii() and ch.isdigit()`,
#   于是全角数字 '１３８００１３８０００' 与中文数字 '一三八〇〇一三八〇〇〇' 平凡绕过 ——
#   两者在 Telegram 里读者一眼仍读得出是一个手机号。改判"数值字符"(见 _is_digit_like):
#   `str.isdigit()`(覆盖 ASCII + 全角 + 各文种数字)加上**有数值的汉字**(一二三…十百千万)。
#   ⚠️ 代价实测为 0:545 条真实语料里含数值汉字最多的一条是 '八重'(1 个),离 5 很远。
_MAX_DIGITS = 5
# 含 CJK 时允许的 ASCII 字母串上限 4 位。中文译名不会夹带长英文单词;
#   4 位是为了放行 "SPDR" / "ETF" / "AI" 这类代号(与 has_new_ascii_word 的 ≥4 同源判断)。
#   这一条专治 "私聊我领空投 加V信 abcdefg" —— 3 个词、0 标点、0 长数字,
#   靠长度/词数/标点全拦不住,但 "abcdefg" 是 7 位。
# ⚠️⚠️ 但它必须**继承 has_new_ascii_word 的语义**:所谓可疑,是"凭空**多出**原文里
#   没有的英文串"。上一版这条是独立判的、看不见原文,于是把一批正常译文全毙了:
#       'Tesla xStock' → '特斯拉 xStock'、'Exxon Mobil xStock' → '埃克森美孚 xStock'
#   —— xStock 全家族 100% 丢译名(真网络实测 40 条里占 3 条)。
#   现在 safe_display 收一个可选的 source:ASCII 串**原样出现在原文里**就不算凭空多出。
#   ⚠️ 不传 source(币名、公司名那条路,本来就没有"原文")时行为与从前完全一致。
_RE_ASCII_RUN = re.compile(r"[A-Za-z]{5,}")
# IPv4 形态。⚠️ 数字总量那条拦得住 '192.168.1.1'(8 位),但拦不住 '1.2.3.4'(4 位),
#   而后者照样是一个"读者一眼认得出是地址"的东西。单列一条,零成本
#   (实测 247 个币名 + 85 个 longName 里没有任何一条长这样)。
_RE_IPV4 = re.compile(r"(?<![0-9])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9])")

# ---- 交易所名:**封闭枚举**,不是模式匹配 ------------------------------------
# ⚠️⚠️⚠️ 这里曾经写的是正则 `[A-Za-z][A-Za-z0-9 .\-]{0,19}` —— 字符集里有 `.` 和 `-`,
#    于是**所有 ≤20 字符的裸域名整段 fullmatch**:
#        't.me' / 'www.evil-airdrop.com' / 'vitalik.eth' / 'discord.gg' /
#        'pump.fun' / 'bit.ly' / 'tme-scam.io'  全部放行,
#    而 stock_exchange 是收口表里**唯一**走这道门(而不是 safe_display)的字段 ——
#    域名 / scheme / 地址那六条规则一条都不生效,并且该字段进**永久缓存**。
#    我自己手写的这条正则,放行了它本来要挡的东西。
#
#    根因是把一个**封闭集合**当成了模式匹配问题。交易所就那么几个,直接枚举:
#    **不在表里 → 那一段不显示**(🏢 整行仍可显示 ticker 与公司名)。
#    这样 't.me' 不是"被拦住",而是"根本不在允许集合里" —— 没有绕过面。
#
# ============ 表怎么来的(2026-09-02 实测)============
# 拿 87 个真实 ticker 去 Yahoo(v8/finance/chart 的 meta.fullExchangeName),85 个有值,
# 实际取值只有 12 种:
#     NasdaqGS(20) NYSE(28) NasdaqGM(4) NasdaqCM(2) NYSEArca(11) NYSE American(7)
#     Cboe US(4) OTC Markets OTCPK(2) OTC Markets OTCQX(1) OTC Markets OTCID(1)
#     CCC(1,BTC-USD)SNP(1,^GSPC)
# 后两个不是股票交易所(instrumentType 是 CRYPTOCURRENCY / INDEX,数据层的 STOCK_TYPES
# 本来就把它们挡在 🏢 行之外),**不进表**。
# 表里另外收了 4 个 Yahoo 历史上/别的端点上用过的写法:
#     AMEX / NYSEAmerican(NYSE American 的旧写法与无空格写法)、
#     NasdaqNMS / BATS(Cboe US 的旧名)——它们是同一批交易所的别名,收进来零风险。
# ⚠️ 比对**不区分大小写**(Yahoo 偶尔大小写不一致),但显示时用表里的规范写法,
#    这样 🏢 那一行的写法永远是我们自己定的,连大小写都不受上游摆布。
# ⚠️ 表外的值 = 那一段不显示。日后遇到新交易所,补这张表(并补测试),不许改成模式匹配。
_EXCHANGES = (
    "NasdaqGS", "NasdaqGM", "NasdaqCM", "NasdaqNMS",
    "NYSE", "NYSEArca", "NYSE American", "NYSEAmerican", "AMEX",
    "Cboe US", "BATS",
    "OTC Markets OTCPK", "OTC Markets OTCQX", "OTC Markets OTCID",
)
_EXCHANGE_BY_KEY = {name.lower(): name for name in _EXCHANGES}

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
    ⚠️⚠️ **tag 字符(U+E0020–U+E007F)也保留** —— 它们的类别同样是 'Cf',
       而地区旗(🏴󠁧󠁢󠁳󠁣󠁴󠁿 / 🏴󠁧󠁢󠁥󠁮󠁧󠁿 / 🏴󠁧󠁢󠁷󠁬󠁳󠁿)靠它们拼出来。上一轮只保了 ZWJ,
       于是苏格兰旗被拆成光秃秃的黑旗 🏴 —— 见 TAG_MIN 那段。
    ⚠️ U+202E(RTL 覆盖)之类仍然删:它能让后面的文字反向显示,是纯粹的视觉欺骗。
    """
    return "".join(ch for ch in str(s or "")
                   if ch == ZWJ or TAG_MIN <= ord(ch) <= TAG_MAX
                   or unicodedata.category(ch) != "Cf")


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
    if ch in _ALLOWED_PUNCT or ch in _SEP_PUNCT:
        return True
    if ch.isascii() and (ch.isalpha() or ch.isdigit()):
        return True
    return is_cjk_char(ch)


def _has_content(text: str) -> bool:
    """
    整段里至少要有**一个**字母 / 数字 / 中日韩文字。

    ⚠️ 少了这一条,一个纯标点的"名字"("..." / "---" / '・')能全程放行 ——
       每个字符都在标点白名单里、长度词数标点数全在上限内。它不是名字,
       印出来只会在标题里长出一段读不懂的东西。实测 247 个真实币名全部满足这一条。
    ⚠️ 它同时是**词数**那条的判据:孤立的分隔符不算一个词(见 _content_words)。
    """
    return any(ch.isascii() and (ch.isalpha() or ch.isdigit()) or is_cjk_char(ch)
               for ch in text)


def _content_words(text: str) -> int:
    """
    有内容的词数。⚠️ 孤立的分隔符**不算词**:"Circle Internet Group • Robinhood Token"
       是 5 词不是 6 词 —— 它是一个真币股的上游原名,`•` 不该吃掉一格词数预算。
    """
    return sum(1 for w in text.split() if _has_content(w))


def _is_digit_like(ch: str) -> bool:
    """
    这个字符算不算"一位数字"。⚠️ 不只是 ASCII —— 理由见 _MAX_DIGITS 上面那段。

    `str.isdigit()` 覆盖 ASCII 与全角(０-９)以及各文种的数字;
    再加上**有数值的汉字**(一二三四五六七八九十百千万),中文数字写的手机号才拦得住。
    """
    if ch.isdigit():
        return True
    return is_cjk_char(ch) and unicodedata.numeric(ch, None) is not None


def _cjk_spaced_separator(text: str) -> bool:
    """
    含中日韩文字时,分隔符两侧带空格(或顶在首尾)→ 视为**模仿 SEP 的形态**。

    ⚠️⚠️ 这是名字侧唯一保留的分隔符规则,理由见模块头那一节:
       中文排版里音译人名的间隔号**紧贴两侧**(`南希·佩洛西` / `尼基塔·比尔`),
       而 `已清仓 · 亏损 99%` / `官方认证 · 已审计` / `已清仓 ・ 亏损 99%`
       全都带空格 —— 那不是名字的写法,是在造一个假字段。
    ⚠️ 纯 ASCII 那侧**刻意不加**这条:`Costco • Robinhood Token` 是带空格的真名字。
    """
    if not has_cjk(text):
        return False
    n = len(text)
    for i, ch in enumerate(text):
        if ch not in _SEP_PUNCT:
            continue
        if i == 0 or i == n - 1 or text[i - 1] == " " or text[i + 1] == " ":
            return True
    return False


def _shape_ok(text: str, source=None) -> bool:
    """
    形状判断 —— 比字符白名单强一个量级的那一层。阈值与理由见上面的常量注释。

    source 是**原文**(只有译文那条路有):ASCII 串原样出现在原文里就不算"凭空多出",
    理由见 _RE_ASCII_RUN 上面那段。不传 = 与从前完全一致。
    """
    if len(text) > _MAX_CHARS:
        return False
    if _content_words(text) > _MAX_WORDS:
        return False
    if sum(1 for ch in text if ch in _ALLOWED_PUNCT and ch != " ") > _MAX_PUNCT:
        return False
    if sum(1 for ch in text if ch in _SEP_PUNCT) > _MAX_SEP:
        return False
    if sum(1 for ch in text if _is_digit_like(ch)) > _MAX_DIGITS:
        return False
    if not _has_content(text):
        return False
    if _cjk_spaced_separator(text):
        return False
    if has_cjk(text):
        src = flatten(source).lower()
        if any(w.lower() not in src for w in _RE_ASCII_RUN.findall(text)):
            return False
    return True


def safe_display(s, source=None) -> str | None:
    """
    → 可以放进推送的那段文本;不合格 **整段丢弃**(返回 None,调用方让那一行消失)。

    ⚠️ 返回值是**清洗过**的(Cf 已删、空白已归一叠平),调用方必须用它,不能再用原串。
    ⚠️ 幂等:safe_display(safe_display(x)) == safe_display(x) —— formatter 在入口统一
       过一遍之后,下游单行渲染函数再过一遍不会改变结果(那是刻意保留的第二道)。
    ⚠️ source 只给**译文**用(原文是什么):见 _RE_ASCII_RUN。不传 = 老行为。
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
    if _RE_IPV4.search(text):
        return None
    if _looks_like_split_domain(text.replace(" ", "")):
        return None
    if not all(_char_ok(ch) for ch in text):
        return None
    if not _shape_ok(text, source):
        return None
    return text


# ---- symbol / handle 的**轻**门禁(只判形态,不判形状)-----------------------
# ⚠️⚠️ symbol 与 handle 同样是攻击者可控的(谁都能给自己发的币起符号、给自己起用户名),
#    而它们和被门禁保护的币名**印在同一行**:
#        render(token_symbol='t.me/pumpgrp')  → 🌱 maxpain · 首次建仓 · $t.me/pumpgrp
#        render(pool_quote_symbol='discord.gg/x') → 🌊 底池 · discord.gg/x · 「USA Rare Earth」
#        ev.handle = 't.me/scam'              → 🌱 t.me/scam · 首次建仓 · $X
#    —— 门禁在这一头掐掉的出口,在旁边被原样打开了。
# ⚠️⚠️ 但**绝不能**给它们套 safe_display:符号天生长得怪(全大写、带数字、超短、带 emoji),
#    长度/词数/标点那套**形状**规则会把正经符号与昵称大面积误伤。所以这里只留**形态**那半:
#    scheme / 域名 / @提及 / 0x 地址 / 裸 hex / base58 / IPv4 / bidi 控制符。
# ⚠️ 域名那条对 ident **刻意比 safe_display 松一格**:只拦"带路径的域名"与
#    "顶级域是真 TLD 的域名",不拦所有 `X.Y` 形态。理由是实测:
#    `eric.eth` / `Dylan.Eth` / `sol.engineer` / `Bull.Path` / `Hungr.AI` / `Mr.CZ Punks`
#    都是真实存在的昵称与符号(ENS 名在这个圈子里就是人名),而它们在 Telegram 里
#    不会被自动变成链接、也没有任何目标可达;`t.me/scam`(带路径)与
#    `www.evil-airdrop.com`(真 TLD)则两者都占。
#    ⚠️ `.eth` / `.sol` 从 ident 的 TLD 表里**去掉**,正因为它们是这个圈子的人名后缀。
# ⚠️ 命中 → 返回 None,调用方按既有"字段缺失"规矩办(那一段或那一行消失,绝不打占位符)。
# ⚠️⚠️ **分隔符与容器字符在这一侧一律严禁**(本轮 F1b/F2):ident 不套 `「」`,
#    它与推送自己的 SEP(' · ')平级 —— `token_symbol='已清仓 · 亏损 99%'` 上一轮
#    原样进了标题。就分隔符这一类而言,ident 的规则集是名字规则集的**严格超集**。
# ⚠️⚠️ 通过 → 返回**叠平后**的串(本轮 F2 改)。上一版返回原串,理由写的是
#    "清洗与限长是下游 _clip 的既有职责" —— 那句话对 render_pump_trade /
#    render_transfer_in_signal / render_alpha_listing 成立,对 **render() 不成立**:
#    _display_name / _symbol_plain 只 _esc 不 _clip,于是
#        token_symbol = 'CUM\n💰 买入 $999,999.00'
#    在标题里**凭空造出一整行伪造字段**。把清洗责任推给下游 = 赌四条路都记得,
#    而实测漏了一条。现在这道门自己叠平,四条路都不用记。
#    ⚠️ 叠平用的是**保留 emoji 的**那个版本(strip_controls_keep_emoji):
#       昵称里的 👨‍💻 / 🏴󠁧󠁢󠁳󠁣󠁴󠁿 靠 ZWJ 与 tag 字符粘合,全删会把它们拆成另一个东西。
#       判断仍在**全删版本**(flatten)上做 —— 零宽空格不许用来拆域名。
_RE_DOMAIN_WITH_PATH = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}/")
_IDENT_TLDS = _SPLIT_DOMAIN_TLDS - {"eth", "sol"}
# Telegram 的**可点通道**在 ident 侧也要堵:
#   · `#` 会被渲染成可点 hashtag。实测 4064 个真实 symbol + 208 个 handle 里带 `#` 的
#     是 **0** 个,整个字符禁掉零成本。
#   · `+` 后面紧跟一串数字会在移动端被渲染成**可拨号**链接。这里只禁"电话形态"
#     不禁字符本身:实测里 'GTA+' 是一个真实符号(1/4064),禁整个字符会误杀它。
_RE_TEL = re.compile(r"\+[0-9]{7,}")
_IDENT_BAD_PATTERNS = (_RE_SCHEME, _RE_MENTION, _RE_EVM, _RE_BARE_HEX, _RE_BASE58,
                       _RE_IPV4, _RE_TEL)
# ⚠️ 与 QUOTE_CHARS 一起在字符层面直接丢弃的那几个(见 safe_ident)。
_IDENT_BAD_CHARS = frozenset("#")
# 视觉容器本身。⚠️ 名字侧靠字符白名单挡(它俩不在里面),ident 侧没有白名单,单列一条。
QUOTE_CHARS = frozenset("「」")
# ---- ident 侧的**唯一一条数量规则**:数字总量 -------------------------------
# ⚠️⚠️ 上一版 ident 侧**只有形态规则、一条数字规则都没有**,于是
#     safe_ident('联系电话13800138000') 原样放行,
#     render(token_symbol='联系电话13800138000') 把手机号直接印进标题行。
#     形态那几条一条都碰不到它:没有域名、没有 scheme、没有 @、不是 0x/hex/base58、
#     不是 IPv4、`+电话` 那条要求有 `+`。
# ⚠️ 判"数字总量"而不是"连续几位":连续规则被一个 `-` 就绕开
#     ('138-0013-8000'),这与名字侧 _MAX_DIGITS 是同一条教训。
# ⚠️ 数字的判据与名字侧**同源**,复用 _is_digit_like:全角数字与中文数字一并计入,
#     否则 '一三八零零一三八零零零' 平凡绕过。
# ⚠️⚠️ 阈值 7(> 7 才丢),**不与名字侧的 5 相同** —— 符号天生带数字,
#     实测(2026-09-03,生产库只读)在过完其余 ident 规则的语料上:
#       4090 个真实 token_symbol 的数字总量分布 {0:3915, 1:94, 2:33, 3:25, 4:19, 5:3, 10:1}
#         → 阈值 7 丢 1 条('1000000000'),0.024%;
#       205 个真实 handle/昵称的分布 {0:165,1:16,2:10,3:6,4:4,6:2,7:1,13:1}
#         → 阈值 7 丢 1 条('HJ8688878987581'),0.488%。
#     取 5(与名字侧同数)时 handle 要丢 4 条 = 1.95%,连 '397397' 这种纯数字昵称
#     一起丢 —— 贴着 2% 的红线,没有余量。取 7 的判据是**目标可达性**:
#     手机号 11 位、QQ 号 9~10 位、微信号里的数字段也在这个量级;7 位以下的数字串
#     够不着一个"可拨可加"的目标,拦它只有代价没有收益。
_MAX_IDENT_DIGITS = 7


def safe_ident(s) -> str | None:
    """
    symbol / handle / 昵称的轻门禁:命中形态 / 分隔符 / 容器字符 / 数字总量超限 → None,
    否则返回**叠平后**的串(单行、空白归一、去首尾;emoji 原样保留)。

    ⚠️ 实测丢弃率(2026-09-03,生产库只读,含本轮 G1 分隔符扩表与 G4 数字规则):
       token_symbol 4/4093 = 0.10%;handle + 昵称 3/207 = 1.45%。
       被丢的逐条见报告 —— 都是含分隔符同形字 / 15 位数字的昵称,刻意的取舍。
    ⚠️ 分隔符 / 容器字符 / Telegram 可点通道那三条在真实 symbol 上的代价仍是 **0**。
    """
    raw = str(s or "")
    if any(ch in _BIDI_CONTROLS for ch in raw):
        return None
    text = flatten(raw)
    if not text:
        return None
    for pat in _IDENT_BAD_PATTERNS:
        if pat.search(text):
            return None
    compact = text.replace(" ", "")
    if _RE_DOMAIN_WITH_PATH.search(compact):
        return None
    if any(m.group(1).lower() in _IDENT_TLDS for m in _RE_SPLIT_DOMAIN.finditer(compact)):
        return None
    if any(ch in QUOTE_CHARS or ch in _IDENT_BAD_CHARS or is_separator_char(ch)
           for ch in text):
        return None
    if sum(1 for ch in text if _is_digit_like(ch)) > _MAX_IDENT_DIGITS:
        return None
    return " ".join(strip_controls_keep_emoji(raw).split())


# ---- 合约地址的门禁:**形状封闭**,不是模式匹配 ------------------------------
# ⚠️⚠️ 地址这一类字段不能走 safe_ident —— 那道的裸 hex / base58 / 0x 三条规则
#    本来就是拿来拦地址的,把它套在"这里就该是一个地址"的槽位上等于全丢。
#    但它同样是**上游给的任意字符串**(币安 Alpha 的 contractAddress),
#    而它会被拼进 fomo.family / gmgn 的 URL、再印成 `<code>` 锚点。
# ⚠️ 判据是一个**封闭形状**:一段 ASCII 字母数字,后面可以跟若干个 `::段`。
#    · EVM `0x8ac7…`(42)与 Solana base58(32–44)是前半段;
#    · **Sui 的类型标签**是 `0xADDR::module::TYPE`(实测币安 Alpha 真的会给这种),
#      所以 `::` 必须收 —— 但只收**成对**的冒号,单个冒号一律不许:
#      单冒号正是 scheme 的形态(`javascript:alert` / `tg:resolve`),
#      而这个字段会被印成 `<code>` 锚点、还会被拼进代币页 URL。
#    · `../../x`、带引号、带空格、带 `?a=b` 的一律不在 —— 那些不是地址。
_RE_ADDRESS = re.compile(r"[0-9A-Za-z]{1,80}(?:::[0-9A-Za-z_]{1,40}){0,4}\Z")
# 地址总长上限。EVM 42 / Solana 44 / Sui 类型标签实测 68 —— 128 绰绰有余。
_MAX_ADDRESS_CHARS = 128


def safe_address(s) -> str | None:
    """
    合约地址的形状门禁:上面那个封闭形状 + 长度 ≤128;不合格 → None(那一行消失)。

    ⚠️ 它**不能**走 safe_ident:那道的 0x / 裸 hex / base58 三条规则本来就是拿来
       拦地址的,套在"这里就该是一个地址"的槽位上等于把每一条都丢掉。
    """
    text = flatten(s)
    if not text or len(text) > _MAX_ADDRESS_CHARS or not _RE_ADDRESS.match(text):
        return None
    return text


def safe_exchange(s) -> str | None:
    """
    交易所名(Yahoo 的 fullExchangeName)的门禁 —— 严格正则,不匹配 → None。

    ⚠️⚠️ 这个字段曾经**全程零门禁**:同一份 Yahoo 响应里 longName / company_zh 都套了
       门禁,唯独 fullExchangeName 漏了,于是被篡改的代理能把
       `立即访问 t.me/free-airdrop` 原样送进 🏢 行。
    ⚠️⚠️ 它是**封闭枚举**不是模式匹配(见 _EXCHANGES 上面那一大段):
       上一版的正则字符集含 `.` 与 `-`,把所有 ≤20 字符的裸域名整段放行了。
    ⚠️ 返回的是**表里的规范写法**,不是上游原串:大小写也不受上游摆布。
    ⚠️ bidi 控制符与 safe_display 同一条口径:来过就整段丢弃,理由见 _BIDI_CONTROLS。
    """
    if any(ch in _BIDI_CONTROLS for ch in str(s or "")):
        return None
    text = flatten(s)
    if not text:
        return None
    return _EXCHANGE_BY_KEY.get(text.lower())


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


# ============================================================
# 外部 URL 的门禁(safe_url / safe_social_links)—— 社媒那一行的唯一入口
# ============================================================
# ⚠️⚠️ 这是本项目**第一个**把外部字符串放进 `<a href>` 的地方。前面所有门禁
#    (safe_display / safe_ident / safe_address / safe_exchange)守的都是"印出来的字",
#    而这里守的是"点下去会去哪儿" —— 两者的失败后果完全不是一个量级:
#    前者最坏是读者读到一句假话,后者是读者**被带到攻击者的站点**,
#    而且链接文字还是我们自己写的「官网」「Twitter」,天然带着我们的背书。
#
# ============ 为什么不是"写个 URL 正则" ============
# 上一轮本项目的 BLOCKER 就是手写正则收交易所名(字符集里带 `.` 与 `-`),
# 结果把 t.me / discord.gg 整段放行。URL 比那个还糟 —— 它有 userinfo(@)、
# 端口、IDN、百分号编码、反斜杠、大小写、尾随点这一堆历史包袱,每一个都是一条
# "看起来是 x.com 其实不是"的路径:
#     https://x.com@evil.com/     → host 其实是 evil.com(userinfo 骗过肉眼)
#     https://x.com.evil.com/     → 后缀匹配骗过"以 x.com 结尾"
#     https://х.com/              → 西里尔 х,同形字
#     //evil.com                  → 协议相对,浏览器补 https
#     javascript:alert(1)         → 根本不是 http 家族
# 所以这里的做法是:**先把 URL 拆开,再对每一段做封闭判定**,任何一步不认识
# 就整条丢弃(绝不"清洗一下再放行" —— 清洗过的 URL 仍然是攻击者选的目标)。
#
# ============ 六道判定 ============
#   1. 字符集:只许 ASCII 可打印(0x21–0x7E),且 `"` `'` `<` `>` 反斜杠 反引号 一律不许。
#      → 空白/控制符/零宽/非 ASCII(IDN 同形字)全部在这一步死掉;
#        `"` 与 `<` 是 href 属性的闭合字符,来了就整条丢弃(不靠转义兜底)。
#   2. 长度 <= _MAX_URL_CHARS。
#   3. scheme 必须**逐字**是 "https://" 开头(大小写不敏感)。
#      → javascript: / data: / tg: / http: / 协议相对 //evil.com 全在这一步死掉。
#      ⚠️ 连 http 都不放行:社媒/官网今天全站 HTTPS,放行 http 只是白送一个降级面。
#   4. netloc:出现 `@`(userinfo)/ `:`(端口)/ `[` `]`(IPv6)一律整条丢弃。
#      → https://x.com@evil.com 在这一步死掉。
#   5. host:逐段 LDH 校验(小写字母数字与 `-`,不许首尾 `-`,每段 <=63,总长 <=253),
#      至少两段,**顶级域必须在 _URL_TLDS 这张封闭表里**,且不许 xn-- 开头(punycode)。
#      → 纯数字 IP、`x.com.` 尾点、西里尔域名全在这一步死掉。
#   6. 社媒类(kind != "website"):host 必须**逐字等于** _SOCIAL_HOSTS[kind] 里的某一项。
#      → https://x.com.evil.com 在这一步死掉(等值比对,不是后缀比对)。
#
# ============ 「官网」那一类为什么放开 host(取舍与代价)============
# 官网的 host **本质上不可枚举**:每个项目一个域名,这正是"官网"的定义。
# 三个选项:
#   (a) 整条不显示 —— 用户明确点名要"网站",直接砍掉等于没做这件事;
#   (b) 只显示文字不给链接 —— 但 URL 本身是攻击者可控的自由文本,把它当文字印出来
#       反而更糟(那是 safe_display 明令拦掉的域名形态);
#   (c) 放开 host,但把**其余五道**全部收死,并且链接文字用我们自己的常量。
# 选 (c)。代价是明确的、写在这里:**一个能发币的人可以让「官网」这两个字指向他选的
# 任意 https 站点**。缓解只有三条,都不消除风险:
#   · 链接文字永远是我们的常量「官网」,URL 本身不出现在消息里 —— 攻击者拿不到
#     "文字说 A、其实去 B" 这个额外的欺骗层(文字本来就只说"官网");
#   · 顶级域封闭表把一批一次性/薅羊毛 TLD 挡在外面;
#   · 同一条信息在 fomo.family 的代币页上本来就以同样的形式对同一批读者展示 ——
#     我们没有新增一个上游没有的通道。
# ⚠️ 这条取舍**必须**留在这里:后来人看到 "website 放行任意 host" 时,
#    第一反应会是"这不是漏了一道门吗",而它是被权衡过的。
_MAX_URL_CHARS = 200
_URL_SCHEME = "https://"
# href 属性的闭合字符与几个历史上被用来绕过解析的字符。⚠️ 出现即**整条丢弃**,
# 不做转义放行:转义只解决 HTML 层,解决不了"这条 URL 是被构造出来骗解析器的"。
_URL_FORBIDDEN_CHARS = frozenset("\"'<>\\`")
# 顶级域封闭表。⚠️ 与 _SPLIT_DOMAIN_TLDS **刻意不共用**:那张表是"看着像域名就拦"的
#    黑名单语义,这张是"只有表里的才放行"的白名单语义 —— 一张表两种语义,
#    改一处必伤另一处(这个项目已经有过"一份表放两个地方走岔"的教训)。
# ⚠️ 选表依据:2026-09-03 生产库四条链去重代币走 DexScreener 实测(见报告),
#    覆盖真实出现过的全部顶级域,外加几个常见的通用域。表外的官网 → 那一段消失。
_URL_TLDS = frozenset({
    "com", "net", "org", "io", "co", "app", "xyz", "fun", "gg", "ai", "so",
    "dev", "art", "money", "finance", "cash", "exchange", "capital", "fund",
    "tech", "digital", "live", "life", "world", "space", "online", "site",
    "club", "team", "chat", "social", "network", "systems", "tools", "wtf",
    "lol", "meme", "pizza", "ninja", "wiki", "news", "blog", "press", "media",
    "info", "pro", "biz", "cc", "tv", "me", "us", "uk", "de", "fr", "jp",
    "kr", "in", "id", "sg", "hk", "tw", "cn", "au", "ca", "ch", "nl", "se",
    "no", "fi", "it", "es", "pt", "br", "mx", "ar", "za", "ru", "eu", "vip",
    "top", "one", "zone", "store", "shop", "market", "trade", "global",
    "group", "family", "house", "studio", "agency", "works", "run", "sh",
    "gd", "ly", "to", "am", "fm", "st", "at", "is", "im", "re", "cx", "ws",
})
# 社媒各类的 host **封闭表**。⚠️ 等值比对,不是后缀比对 —— 后缀比对放行
#    x.com.evil.com,那是这一类判定最经典的洞。
# ⚠️ 每加一个 host 都要问一句"这个域名今天真的是它家的吗":discordapp.com 是
#    Discord 的旧域名(仍在重定向),telegram.me 是 Telegram 的旧域名,两者收录;
#    bit.ly 这类短链**一律不收** —— 短链的目标不可见,收它等于把整张表作废。
_SOCIAL_HOSTS = {
    "twitter": frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com",
                          "mobile.twitter.com"}),
    "telegram": frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"}),
    "discord": frozenset({"discord.gg", "www.discord.gg", "discord.com",
                          "www.discord.com", "discordapp.com"}),
    "reddit": frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"}),
    "github": frozenset({"github.com", "www.github.com"}),
}
# 我们**认识**的社媒类别。⚠️ 顺序就是展示顺序(官网在最前,与用户给的样例一致)。
# ⚠️⚠️ 表外的 type(medium / tiktok / youtube …)**一律跳过**,不显示原文:
#    链接文字必须是我们自己的常量(见 formatter.SOCIAL_LABELS),而"显示原文"
#    等于把上游的自由文本当链接文字印出去 —— 那正是这一轮要堵的口子
#    (websites[].label 同理,永远不用)。代价是少显示几类社媒,认了。
SOCIAL_KINDS = ("website", "twitter", "telegram", "discord", "reddit", "github")
# 一条消息里最多列几个社媒链接。⚠️ 上游可以塞任意多条,不设上限就能把一行顶爆预算。
_MAX_SOCIAL_LINKS = 6


def _label_ok(lab: str) -> bool:
    """单个域名段:只许小写 ASCII 字母 / 数字 / `-`,不许首尾 `-`,不许 punycode。"""
    if not lab or len(lab) > 63 or lab.startswith("-") or lab.endswith("-"):
        return False
    if lab.startswith("xn--"):
        return False                     # punycode:同形字的最后一条路
    return all((c.isascii() and (c.islower() or c.isdigit())) or c == "-" for c in lab)


def _host_ok(host: str) -> bool:
    """host 的封闭校验:逐段 LDH、2~6 段、顶级域在白名单里。"""
    if not host or len(host) > 253 or host.startswith(".") or host.endswith("."):
        return False
    labels = host.split(".")
    if not 2 <= len(labels) <= 6:
        return False
    if not all(_label_ok(lab) for lab in labels):
        return False
    return labels[-1] in _URL_TLDS


def _percent_ok(text: str) -> bool:
    """百分号只许以 %XX(两位十六进制)出现。⚠️ 半截的 % 是解析器分歧的经典来源。"""
    i = text.find("%")
    while i >= 0:
        seg = text[i + 1:i + 3]
        if len(seg) != 2 or not all(c in "0123456789abcdefABCDEF" for c in seg):
            return False
        i = text.find("%", i + 1)
    return True


def safe_url(url, kind: str) -> str | None:
    """
    外部 URL → 可以放进 `<a href>` 的那一份;任何一步不认识 → None(那一段消失)。

    kind 必须在 SOCIAL_KINDS 里;"website" 放开 host(理由见上面那段取舍),
    其余各类的 host 必须**逐字**在 _SOCIAL_HOSTS[kind] 里。

    ⚠️ 返回值的 scheme 与 host 已归一成小写,path 及其后原样保留 ——
       host 大小写不受上游摆布,而 path 的大小写是有意义的(t.me/AbC != t.me/abc)。
    """
    if kind not in SOCIAL_KINDS:
        return None
    raw = str(url or "")
    # 1. 字符集:ASCII 可打印 + 禁用字符表
    if not raw or any(not (0x21 <= ord(c) <= 0x7E) or c in _URL_FORBIDDEN_CHARS for c in raw):
        return None
    # 2. 长度
    if len(raw) > _MAX_URL_CHARS:
        return None
    # 3. scheme 必须逐字是 https://
    if raw[:len(_URL_SCHEME)].lower() != _URL_SCHEME:
        return None
    rest = raw[len(_URL_SCHEME):]
    if not rest:
        return None
    # 4. netloc 切出来(第一个 / ? # 之前),userinfo / 端口 / IPv6 一律丢
    cut = len(rest)
    for ch in "/?#":
        pos = rest.find(ch)
        if pos >= 0:
            cut = min(cut, pos)
    netloc, tail = rest[:cut], rest[cut:]
    if any(c in netloc for c in "@:[]"):
        return None
    # 5. host
    host = netloc.lower()
    if not _host_ok(host):
        return None
    # 6. 社媒类:host 必须在封闭表里(等值,不是后缀)
    allowed = _SOCIAL_HOSTS.get(kind)
    if allowed is not None and host not in allowed:
        return None
    if not _percent_ok(tail):
        return None
    return _URL_SCHEME + host + tail


def safe_social_links(items) -> tuple[tuple[str, str], ...] | None:
    """
    上游给的 [(类别, URL), …] → 过完门禁的 ((类别, URL), …);一条都不剩 → None。

    ⚠️ **同类只取第一个**(有的币把同一个 type 填了好几条)。"第一个"以上游给的
       顺序为准 —— 我们没有任何依据说第二条比第一条更正确,而"随机挑一条"
       在排查时是灾难。
    ⚠️ 返回的是**类别**不是文字:链接文字由 formatter 用自己的常量渲染
       (SOCIAL_LABELS)。上游的 websites[].label **永远不用** —— 它是攻击者
       可控的自由文本,而链接文字带着我们的背书。
    ⚠️ 输出顺序按 SOCIAL_KINDS 固定,不随上游数组顺序漂移:同一个币两次推送里
       链接顺序不一样会让人以为数据变了。
    ⚠️ 单条不合格只丢那一条,不影响其余(三行各自独立那条规矩的同一条口径)。
    """
    got: dict[str, str] = {}
    try:
        seq = list(items or ())
    except TypeError:
        return None
    for item in seq:
        try:
            kind, url = item
        except (TypeError, ValueError):
            continue
        k = str(kind or "").strip().lower()
        if k not in SOCIAL_KINDS or k in got:
            continue          # 未知类别跳过、同类只取第一个
        clean = safe_url(url, k)
        if clean is not None:
            got[k] = clean
    out = tuple((k, got[k]) for k in SOCIAL_KINDS if k in got)[:_MAX_SOCIAL_LINKS]
    return out or None


# ============================================================
# 发射台名的门禁(safe_launchpad)—— **封闭枚举**,与 safe_exchange 同一套路数
# ============================================================
# ⚠️⚠️ 为什么**不能**用 safe_display:实测(2026-09-03,生产库四条链 4305 个去重代币
#    全量走 filterTokens)真实存在的发射台名里,最常见的几个恰好是**域名形态**:
#      Pump.fun 1139 · o1.exchange 44 · Four.meme 27 · Feel.cash 5 · bow.fun 1 · tren.ch 3
#    safe_display 的"域名形态整段丢弃"那条会把它们全部毙掉 —— 1219 / 3422 = **35.6%**
#    的命中被打掉,而且打掉的是 Solana 上唯一重要的那一个(Pump.fun 占该链 83%)。
#    这不是"门禁太严",是**门选错了**:那道门是给"名字"用的,而发射台名不是名字,
#    它是一个**平台标识**,和交易所名一样 —— 世界上就那么几个,可以逐个数出来。
#
# ⚠️⚠️ 本项目上一轮的 BLOCKER 就是"手写正则去收本该枚举的东西"(交易所名的
#    `[A-Za-z][A-Za-z0-9 .\-]{0,19}` 把 t.me / discord.gg 全放行了)。发射台名
#    如果放开成"允许域名形态的短串",等于把那个洞原样重开一遍:
#    `t.me/scam` 与 `Pump.fun` 在任何模式匹配眼里都是同一个形状。
#    **凡是能枚举的一律不许手写模式匹配。**
#
# ============ 这张表的依据(全部实测)============
# 2026-09-03,生产库只读取出四条链**全部**去重代币(robinhood 1960 / solana 1528 /
# bsc 657 / base 160,共 4305 个),分 22 批走 filterTokens(每批 200、间隔 10 秒,
# 0 个 429),得到的 launchpadName 全集就是下面这 25 个,一个不多一个不少。
# 覆盖率:robinhood 72.0% · solana 89.5% · bsc 86.4% · base 47.5%
# (与调研数据 74.0 / 89.7 / 86.5 / 47.2 逐条对得上)。
#
# ============ 代价(明写)============
# **上游出现一个新发射台时,🚀 那一行不显示,直到有人把它加进这张表。**
# 这正是本项目"宁可缺失整行,绝不印错"的既有取舍(与 safe_exchange、
# 与 dexscreener.STOCK_NAME_MARKERS 同一条)。为了让"有人加进来"这件事真的发生,
# formatter._launchpad_line 在遇到表外的名字时打一条 DEBUG —— 日志里看得到。
#
# ⚠️ 键是**小写**的上游原值,值是**我们的规范写法**(大小写不受上游摆布)。
# ⚠️ "Pons" / "Pons V2" 是本仓库自己产出的值(robinhood 上的 pons 币要按创建工厂
#    分版本,见 tokeninfo.PONS_FACTORIES),所以两者都在表里。
_LAUNCHPADS = {
    # ---- robinhood ----
    "pons": "Pons",                       # 1235 个;robinhood 上会被 tokeninfo 分成 V1/V2
    "pons v2": "Pons V2",                 # 本仓库自己产出的值
    "long": "LONG",                       # 109
    "flap": "Flap",                       # 27(bsc 上 540、base 上 1)
    "uniswapcca": "UniswapCCA",           # 17
    "virtuals": "Virtuals",               # 10
    "bankr": "Bankr",                     # 6(base 上 20)
    "feel.cash": "Feel.cash",             # 3(base 上 2)⚠️ 域名形态,safe_display 会毙掉
    "sushi launch": "Sushi Launch",       # 2
    "trench": "Trench",                   # 2
    "bow.fun": "bow.fun",                 # 1  ⚠️ 域名形态
    # ---- solana ----
    "pump.fun": "Pump.fun",               # 1139 ⚠️ 域名形态,而且是全库最常见的一个
    "stonkfun": "StonkFun",               # 126
    "meteoradbc": "MeteoraDBC",           # 75
    "bonk": "Bonk",                       # 10
    "launchlab": "LaunchLab",             # 5
    "tren.ch": "tren.ch",                 # 3  ⚠️ 域名形态
    "easya kickstart": "EasyA Kickstart",  # 2
    "bags": "BAGS",                       # 2
    "meteora alpha vault": "Meteora Alpha Vault",  # 2
    "metaplex": "Metaplex",               # 1
    "printr": "Printr",                   # 1
    "jupiter studio": "Jupiter Studio",   # 1
    # ---- bsc ----
    "four.meme": "Four.meme",             # 27 ⚠️ 域名形态
    # ---- base ----
    "o1.exchange": "o1.exchange",         # 44 ⚠️ 域名形态
    "clanker v4": "Clanker V4",           # 5
}
# 对外只读:formatter 用它判"要不要打那条"名字不在表里"的 DEBUG",测试用它做枚举断言。
LAUNCHPAD_NAMES = frozenset(_LAUNCHPADS.values())


def safe_launchpad(s) -> str | None:
    """
    发射台名的门禁 —— **封闭枚举**,表外一律 None(那一行整行消失,绝不猜)。

    ⚠️ 返回**表里的规范写法**,不是上游原串:大小写也不受上游摆布。
    ⚠️ bidi 控制符与 safe_display / safe_exchange 同一条口径:来过就整段丢弃。
    ⚠️ 因为返回值只可能是表里那 26 个之一,它**不可能**含分隔符、`「」`、域名路径、
       scheme、@提及 —— 所以渲染时**不套 `「」` 容器**(与 safe_exchange 同一条理由:
       它已经不是自由文本了),这也正好对上用户样例里的 `🚀 发射台 · LONG`。
    """
    if any(ch in _BIDI_CONTROLS for ch in str(s or "")):
        return None
    text = flatten(s)
    if not text:
        return None
    return _LAUNCHPADS.get(text.lower())
