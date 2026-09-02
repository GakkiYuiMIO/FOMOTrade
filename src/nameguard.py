"""
推送里所有**外部文本**(币名、译名、公司名)的展示门禁 —— 白名单,不是黑名单。

============ 为什么必须是白名单 ============
币名来自 DexScreener 的 `baseToken.name`:**任何人**都能给自己发的币起任意名字。
译名来自维基 / Google 的响应,而所有外呼都走 fomo_proxy —— 代理能篡改响应,
Google 那条通道本身还是非官方的。两者都是**攻击者可控**的输入。

黑名单("列举坏东西")在这种输入上不成立,这有实测证据:同一份黑名单里
`t.me` 是拦得住的,可 `t.<U+200B>me`(中间插一个零宽空格)当场绕过 ——
而 Telegram 渲染时零宽空格不占位,读者看到的仍然是 `t.me`。
再补一条 `​` 进黑名单,攻击者换 U+200C / U+2060 / U+FEFF 就又过了。
所以这里反过来:**只放行认得出的东西,其余整段丢弃**。

============ 三步 ============
1. 删掉所有 Unicode Cf(格式控制)字符 —— 零宽空格 U+200B、RTL 覆盖 U+202E 之类。
   ⚠️ 顺序是"先删再判":先删才能让 `t.<U+200B>me` 还原成 `t.me` 被第 2 步抓到。
   ⚠️ U+202E 还能在 Telegram 里把后面的文字反向显示("币<U+202E>pmup" 看起来是
      "币pump"),这是纯粹的视觉欺骗,删掉即可。
2. 必拦形态(命中任一 → 整段丢弃):协议头、域名形态、@提及、0x 地址、base58 长串。
3. 字符白名单:CJK + ASCII 字母 + ASCII 数字 + 空格 + 一小撮标点。集合外一律丢弃。

============ 为什么是"整段丢弃"而不是"剔掉坏的那部分" ============
剔一半会拼出一个**似是而非的假名字**("加群 t.me/scam" 剔成 "加群 scam"),
读者看不出它被动过,比整段不显示更糟。缺失整行消失是本项目的既有铁律(铁律 2)。

============ 两个刻意的取舍(会误伤,是有意的)============
· **表情符号一律不放行。** 很多正经 memecoin 名字带表情,代价是它们的名字尾巴消失。
  但本项目的行首 emoji 是聊天列表预览里**唯一的扫描锚点**(铁律 1):一个叫
  "🔴 已清仓 · 骗你的" 的币,名字印进标题就能在预览里伪造一条不存在的卖出行。
  一个装饰性表情的价值,换不了这个。
· **非 ASCII 拉丁字母(é / ü)与西里尔、希腊字母一律不放行。** 同形字
  (西里尔 `а` 与拉丁 `a` 长得一模一样)正是白名单最经典的绕过手法 ——
  `аpple.com` 用西里尔 а 写就躲开了第 2 步的域名正则。
  代价:一个真叫 "Café Coin" 的币没有名字尾巴。可接受。

⚠️ 本模块是**纯函数、零依赖**:formatter(渲染前)与 namecn(送翻译前)各自独立调一次。
   渲染前那道**不能**因为"送翻译前已经查过了"就跳过 —— 译文是另一个来源。
"""
from __future__ import annotations

import re
import unicodedata

# ---- 第 2 步:必拦形态 ----------------------------------------------------
# ⚠️ 不整体加 IGNORECASE:base58 那条的 [A-HJ-NP-Z] 一旦忽略大小写就会把小写 l 收进去,
#    "Supercalifragilisticexpialidocious" 这种普通长词会被当成地址。
_BAD_PATTERNS = (
    # 协议头:http:// https:// tg:// ftp:// …
    re.compile(r"://"),
    # 域名形态:t.me / evil.com / vitalik.eth / discord.gg。
    # ⚠️ 点后面必须**紧跟** 2 个以上字母才算,于是:
    #    "Inc." "Corp."(点在词尾、后面是空格或结尾)不匹配;
    #    "U.S.A."(点之间只有单字母)不匹配;"Web3.0"(点后是数字)不匹配。
    re.compile(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}"),
    # @提及。@ 后面不跟字母的(邮箱式 "a@1"、单独一个 @)也一起拦掉更省事,
    # 但 @ 本来就不在白名单里,这条只是把意图写明白。
    re.compile(r"@[A-Za-z]"),
    # EVM 地址。⚠️ 4 位起,不是 6 位:实测 "去 0xabcd 领" 这种短地址一样是钓鱼话术。
    re.compile(r"0[xX][0-9a-fA-F]{4,}"),
    # Solana 地址形态:base58 字母表(无 0 O I l)连续 ≥26 位。
    # Solana 地址是 32~44 位,26 是留了余量的下限。
    re.compile(r"[1-9A-HJ-NP-Za-km-z]{26,}"),
)

# ---- 第 3 步:字符白名单 --------------------------------------------------
# 中日韩(含假名、谚文)。与 namecn._CJK 同一份范围。
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
# 允许的标点。⚠️ 全角标点(,。、;:!?"")**不在**里面:名字不带全角标点,
#    带全角标点的是句子 —— 而推送里不需要显示任何人写给读者的句子。
_ALLOWED_PUNCT = frozenset(" .,'\"-&()!?:;/+#$%·•~")

# 译文里"凭空长出来的英文单词"的长度下限。3 位以下(ETF / AI / DNA)是正常的中文行文,
# 4 位起才像是被塞进来的东西。
_ASCII_WORD = re.compile(r"[A-Za-z]{4,}")


def strip_format_controls(s) -> str:
    """删掉所有 Unicode Cf(格式控制)字符:零宽空格、RTL 覆盖、字节序标记…"""
    return "".join(ch for ch in str(s or "") if unicodedata.category(ch) != "Cf")


def flatten(s) -> str:
    """删 Cf → 叠平空白。**所有**判断都在这个结果上做。"""
    return " ".join(strip_format_controls(s).split())


def _char_ok(ch: str) -> bool:
    if ch in _ALLOWED_PUNCT:
        return True
    if ch.isascii() and (ch.isalpha() or ch.isdigit()):
        return True
    return bool(_CJK.match(ch))


def safe_display(s) -> str | None:
    """
    → 可以放进推送的那段文本;不合格 **整段丢弃**(返回 None,调用方让那一行消失)。

    ⚠️ 返回值是**清洗过**的(Cf 已删、空白已叠平),调用方必须用它,不能再用原串。
    """
    text = flatten(s)
    if not text:
        return None
    for pat in _BAD_PATTERNS:
        if pat.search(text):
            return None
    if not all(_char_ok(ch) for ch in text):
        return None
    return text


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
