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


# 允许的标点。⚠️ 全角标点(,。、;:!?"")**不在**里面:名字不带全角标点,
#    带全角标点的是句子 —— 而推送里不需要显示任何人写给读者的句子。
#    ⚠️ `#` `$` `+`(Telegram 可点通道)、`·` `•`(字段分隔符)、`「` `」`(视觉容器)
#       与 `;` 都**刻意不在**里面,理由见模块头。
#    ⚠️ 半角冒号 `:` 也**已经拿掉**:scheme 那条只认"冒号后紧跟非空白"的 ASCII 形态,
#       CJK 打头的 `加我微信:abcd` 从它下面漏过去(ASCII 串 4 位也够不着 ≥5 那条)。
#       实测 247 个真实币名 + 85 个 Yahoo longName 里带 `:` 的是 **0** 个,拿掉零成本。
_ALLOWED_PUNCT = frozenset(" .,'\"-&()!?/%~")

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
    if ch in _ALLOWED_PUNCT:
        return True
    if ch.isascii() and (ch.isalpha() or ch.isdigit()):
        return True
    return is_cjk_char(ch)


def _has_content(text: str) -> bool:
    """
    整段里至少要有**一个**字母 / 数字 / 中日韩文字。

    ⚠️ 少了这一条,一个纯标点的"名字"("..." / "---" / '""')能全程放行 ——
       每个字符都在标点白名单里、长度词数标点数全在上限内。它不是名字,
       印出来只会在标题里长出一段读不懂的东西。实测 247 个真实币名全部满足这一条。
    """
    return any(ch.isascii() and (ch.isalpha() or ch.isdigit()) or is_cjk_char(ch)
               for ch in text)


def _shape_ok(text: str, source=None) -> bool:
    """
    形状判断 —— 比字符白名单强一个量级的那一层。阈值与理由见上面的常量注释。

    source 是**原文**(只有译文那条路有):ASCII 串原样出现在原文里就不算"凭空多出",
    理由见 _RE_ASCII_RUN 上面那段。不传 = 与从前完全一致。
    """
    if len(text) > _MAX_CHARS:
        return False
    if len(text.split()) > _MAX_WORDS:
        return False
    if sum(1 for ch in text if ch in _ALLOWED_PUNCT and ch != " ") > _MAX_PUNCT:
        return False
    if sum(1 for ch in text if ch.isascii() and ch.isdigit()) > _MAX_DIGITS:
        return False
    if not _has_content(text):
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
# ⚠️ 通过 → 返回**原串**(不清洗):昵称里的 emoji、符号里的大小写都必须原样保留,
#    清洗与限长是下游 _clip 的既有职责,这道门只回答"给不给显示"。
_RE_DOMAIN_WITH_PATH = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}/")
_IDENT_TLDS = _SPLIT_DOMAIN_TLDS - {"eth", "sol"}
_IDENT_BAD_PATTERNS = (_RE_SCHEME, _RE_MENTION, _RE_EVM, _RE_BARE_HEX, _RE_BASE58, _RE_IPV4)


def safe_ident(s) -> str | None:
    """
    symbol / handle / 昵称的轻门禁:命中形态类规则 → None,否则**原样**返回。

    ⚠️ 实测丢弃率(2026-09-02,生产库只读):
       token_symbol 1/2434 = 0.04%(被丢的那条是 '@everyone');
       handle + 昵称 0/208 = 0.00%。
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
    return raw


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
