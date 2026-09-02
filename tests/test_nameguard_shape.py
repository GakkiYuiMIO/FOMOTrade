"""
形状白名单(src/nameguard.safe_display)的**实测**回归基线。

============ 样本怎么来的 ============
2026-09-02:从生产库 data/fomo.db **只读**取真实的 (network_id, token_address),
四条链(solana / bsc / base / robinhood)各取 62 个不同的币,逐个走
`GET https://api.dexscreener.com/latest/dex/tokens/{addr}`(串行 + 0.45s 间隔,
按 chainId 与 baseToken.address 过滤),取 baseToken.name ——
拿到 **248 个不同的真实币名**。下面的表就是那批样本(去掉 1 条含侮辱性词汇的,
它能过门禁、与阈值无关),原样冻结。

============ 实测结果(定阈值的依据)============
丢弃 7 / 247 = **2.8%**(目标 ≤ 10%)。被丢弃的 7 条见 _REAL_DROPPED,
逐条都是"看起来确实可疑"或"刻意的取舍",没有一条是普通名字被形状打掉。

分布(全部 247 条,叠平之后):
  · 标点数 {0: 234, 1: 11, 2: 2}      → 上限 3(见下)
  · 词数   {1: 140, 2: 63, 3: 31, 4: 7, 5: 3, 6: 3}
  · 长度   最长 52(那条是带 " • Robinhood Token" 后缀的),其余 ≤ 36

============ 阈值与理由(⚠️ 改阈值 = 改这个文件,别反过来)============
_MAX_CHARS = 40   实测最长的"普通"名字 36("Space Exploration Technologies Corp.")。
_MIN_CHARS = 1    单字中文译名("猫")是正常结果,而 1 个字装不下任何攻击载荷。
_MAX_WORDS = 5    实测最多 5 词;"Send SOL to my wallet now" 6 词、
                  "Buy now 100% safe visit my profile" 7 词 —— 一句话总比一个名字长。
                  ⚠️ 实测里有 2 条 6 词的真名字("One Dollar Is All You Need" /
                     "one life, its worth an attempt")因此被误杀 —— 这是**冲突样本**:
                     放行它们就等于放行 "Send SOL to my wallet now"。按"拦住优先"取舍。
_MAX_PUNCT = 3    实测上界只有 2;抬到 3 是为了放行缩写式名字 "U.S.A. Token"(3 个点)。
                  抬这一格不会多放行任何一条攻击基线。
连续数字 ≥6 → 丢  手机号 / QQ 号一律 ≥6 位。**不许松。**
`·` `•` `#` `$` `+` `「` `」` 不在字符白名单里。**`·` 那条不许松**
                  (它是本项目的字段分隔符,放行等于把"造假字段"的能力交出去)。

⚠️ 断言写死字面量,**不从 src.nameguard import 任何阈值 / 正则** —— 那等于让被测模块
   给自己判卷。这个文件里的每一个数字都是上面那批实测样本量出来的。
"""
# ruff: noqa: N802, RUF001
from __future__ import annotations

import pytest

from src.nameguard import safe_display

# ============================================================
# 硬基线 1:必须放行(已知的真实名字,一个都不许丢)
# ============================================================
_MUST_PASS = [
    "Cummingtonite", "Cash Cat", "dogwifhat", "Artificial Inu",
    "Quantum White Fiber Rabbit", "USA Rare Earth, Inc.", "WhiteFiber, Inc.",
    "NVIDIA Corporation", "SPDR S&P 500 ETF Trust",
    "Space Exploration Technologies Corp.", "U.S.A. Token",
    "镁铁闪石", "美国稀土公司", "英伟达", "Tether Gold",
]

# ============================================================
# 硬基线 2:必须拦住(一个都不许放行)
# ============================================================
# ⚠️ 每条后面标的是**主要**由哪一层拦下的 —— 门禁是多层的,不止一层会命中。
_MUST_BLOCK = [
    ("已清仓 · 亏损 99%", "字符白名单:`·` 是字段分隔符"),
    ("官方认证 · 已审计", "字符白名单:`·`"),
    ("联系电话13800138000", "连续数字 ≥6"),
    ("私聊我领空投 加V信 abcdefg", "含 CJK 时不许有 ≥5 位 ASCII 串"),
    ("Send SOL to my wallet now", "词数 6 > 5"),
    ("Buy now 100% safe visit my profile", "词数 7 > 5"),
    ("Join t.me/pumpgroup now", "形态:域名"),
    ("tg://resolve?domain=scam", "形态:scheme"),
    ("javascript:alert(1)", "形态:scheme(单冒号)"),
    ("data:text/html,hi", "形态:scheme(单冒号)"),
    ("discord.gg/freeSOL", "形态:域名"),
    ("www.evil-airdrop.com", "形态:域名"),
    ("vitalik.eth", "形态:域名"),
    ("去 0xabcd 领", "形态:0x 地址(4 位起)"),
    ("Send 7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18", "形态:裸 hex ≥16"),
    ("Claim at 0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18", "形态:0x 地址"),
    ("Airdrop CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump", "形态:base58 ≥26"),
    ("t.\u200bme/scam", "零宽空格先删再判 → 域名"),
    ("t.\u00a0me/scam", "NBSP 归一 → 抽掉空白后是真域名 t.me"),
    ("币\u202e pmup", "bidi 控制符:整段丢弃,不是删掉了事"),
    ("#freeairdrop", "字符白名单:`#` 是 Telegram 的可点 hashtag 通道"),
    ("+79001234567", "字符白名单:`+`(可拨号)+ 连续数字 ≥6"),
    # ⚠️ 下面这几条各自钉住一个**刚好踩线**的阈值:上面那些样本都远超阈值,
    #    阈值放宽一格它们照样被拦,于是"放宽一格"变成了改不红的等价变异。
    ("Send 7a6a3b93cb3ffead", "形态:裸 hex 16 位刚好踩线(前面那条 40 位,放到 32 也拦得住)"),
    # ⚠️ "去 0xabcd 领" 那条其实是被"含 CJK 时不许有 ≥5 位 ASCII 串"拦下的("xabcd" 5 位),
    #    所以它证明不了 0x 规则的 4 位下限。这条纯英文的才证明得了。
    ("Claim 0xabcd", "形态:0x 地址 4 位刚好踩线"),
    ("抽奖码123456", "连续数字 6 位刚好踩线"),
    ("假「名字」", "字符白名单:`「`『』是视觉容器本身,名字里带它就能把自己移出容器"),
    ("Coin • Token", "字符白名单:`•` 与 `·` 同属字段分隔符"),
]

# ============================================================
# 实测样本(2026-09-02,四条链 248 个真实币名,去掉 1 条侮辱性的)
# ============================================================
# ⚠️ 这张表是**冻结的测量结果**,不是许愿单:动阈值之前先看这里会红几条。
_REAL_PASS = [
    '100 USDC and a meme',
    'A Society For AI Agents',
    'AmberVibe',
    'Apple Inc.',
    'Apple Juice',
    'Attention To Money',
    'BALD',
    'Base Juice',
    'Base Waldo',
    'Basemate',
    'basenoun',
    'BaseStonk',
    'Bitbank',
    'BLUE CHIP',
    'Build on Base',
    'Burger Money',
    'CEEcil',
    'Chainspin',
    'Chiwawa',
    'Cluster Protocol',
    'Coinbase Wrapped DOGE',
    'Coinye West',
    'defi-native',
    'DiamondPepe69',
    'doji',
    'Free Bots',
    'FRIEND',
    "Froggy's World",
    'Froth',
    'Goku',
    'Google T-REX',
    'GOOGLE TREX',
    'HouseCoin',
    'ISHOWSPEED',
    'Jai',
    'jeffkirdeikis',
    'KAI',
    'KellyClaude',
    'KittehCoin',
    'LienFi',
    'Lil Finder Guy',
    'Linera',
    'MEME',
    'Meta Platforms Inc.',
    "Na'vidia",
    'NEWTON',
    'O1 Doll',
    'Pandacoin',
    'Plumber',
    'plumber',
    'PrinterInkCoin',
    'sarah',
    'SingIt',
    'Sub8 Bot',
    "The Garden's Frame",
    'Toshi',
    'TREX',
    'True',
    'Wrapped Coinbase Global Inc ST0x',
    'Zuckasaurus',
    '不许在赌场里哭',
    '4',
    'Advanced Micro Devices Inc',
    'Aster',
    'Battle of Red Cliffs',
    'Bicat',
    'Binance agent OS CAT',
    'Cashcat on CNY',
    'chAIn',
    'Diamond Hands',
    'DICKBUTT',
    'Do nothing, win',
    'Eyes Family',
    'GalbotCZ1',
    'GATSBY',
    'Giggle Market Equity',
    'GOGL',
    'Hedgehog Tesla Mascot',
    'hongchenfei',
    'HOPE星火燎原',
    'HS',
    'memefi',
    'NianNian',
    'Ninja',
    'Optimus',
    'Pepe',
    'Petal Miner',
    'Phone Monkey',
    'Pineapple Cat',
    'stockmemes',
    'Sun Ge',
    'SUNCOIN',
    'Tesla, Inc. ',
    'The Bimpsons',
    'utility token',
    'VIRUS',
    '分红ETH',
    '喵喵币',
    '央视报道杀零狗',
    '如同一头驴',
    '孙履真',
    '小大力神杯',
    '川普',
    '布鲁斯',
    '席卷全网的熊猫烧香',
    '牛仔',
    '牛来',
    '牛来人生',
    '牛来速回',
    '猴子币',
    '空气币',
    '羊头升级版',
    '美团老鼠',
    '老吴',
    '花花',
    '蝴蝶人生',
    '西虹市首富',
    '豹拉',
    '金猫',
    '铜狗',
    '预言家',
    '鹏来',
    '龙Long',
    'american spy',
    'Astracat',
    'AUTSM',
    'BAGHOLDERS',
    'Bankless Coin',
    'Biscotti',
    'BROKERTOOLS',
    'cashbird',
    'catscrolling',
    'CHIPMUNK',
    'Crash',
    'Cyber Hardware-Integrated Pup',
    'Deepstate',
    'Demo3x',
    'DexLaunch',
    'Donald Duck',
    'duck',
    'Dumb Money',
    'Elo Pros',
    'ETH MAXI',
    'fatcoin',
    'Flock Memes',
    'GIWA',
    'HE HUA',
    'Kirkinhood',
    'Longbow',
    'Magic Internet Money',
    'Memestock',
    'Meridian',
    'Merry',
    'NASDAQ 6900',
    'Net Asset Value',
    'NOBI',
    'NovaAi',
    'Passive Income Generator',
    'PEAR',
    'Peeta The Shiba',
    'PLACEBO',
    'PURRSUADER',
    'Quanta Compute',
    'Quantum Capy',
    'Rat Race',
    'Robin Hood',
    'ROBINCAT',
    'Ryzen Kitty',
    'Save the Children',
    'Short Squeeze',
    'Sleuth Intel',
    'Spacesex',
    'Test',
    'The Muskyssey',
    'The Stoic Monk',
    'Trash',
    'Tung Tung Tung Sahur',
    'Unstable Cat',
    'ur mom',
    'Zedkr',
    'aura',
    'Axe Arcade',
    'BabyP',
    'Bitcoin Bob',
    'BOLLOCKS',
    'Bonk',
    'Carl Johnson',
    'CyberOwl',
    'dawg',
    'Dictator Mbappe',
    'DUMPSTR',
    'Escaping the Sandbox',
    'FLOW STATE',
    'FOMO',
    'funwithagent',
    'GIVEBACK',
    'Gold xStock',
    'Gomu Gator',
    'GORK',
    'Grok Bot',
    'Hege',
    'Lenny ',
    'Live Action Roleplay',
    'Lorepedia',
    'Maxwell the Cat',
    'MetaDAO',
    'Microsoft',
    'Mythical Mammalian Reptibird',
    'Nava ecosystem',
    'Nietzschean Penguin',
    'NormieCoin',
    'Official Fomo Mascot',
    'Official Layoff Coin',
    'Oobit',
    'OpenCode Jr',
    'PEACH64',
    'Pixel Cat',
    'Ralph Wiggum',
    'Rare Earth Bull ',
    'RayGun',
    'Return To Memes',
    'Rokha X Agent',
    'Snoovatars',
    'Streamer Bounties',
    'The 7 Wanderers',
    'The Black Bear',
    'the Europoor',
    'The Meme Note',
    'the Thesisoor',
    'Tim and Moby',
    'USDe',
    'Virus Chain',
    'visualize',
    'WOMAN YELLING AT CAT',
    'Wrapped Ether (Wormhole)',
    'wrapped toads',
    'Yodel Techno',
    'ナッツ',
    '八重',
    '奶蛙',
]

# 被丢弃的那几条 —— 逐条标注"被哪条规则毙的 / 为什么可以接受"。
_REAL_DROPPED = [
    ('one life, its worth an attempt', '词数 6 超上限'),
    ('Costco • Robinhood Token', '字符白名单外的字符 •'),
    ('ioo.fun', '形态:域名'),
    ('One Dollar Is All You Need', '词数 6 超上限'),
    ('son 😭😭😭😭😭', '字符白名单外的字符 😭'),
    ('Taiwan Semiconductor Manufacturing • Robinhood Token', '字符白名单外的字符 •'),
    ('Chó the fish vendor', '字符白名单外的字符 ó'),
]


@pytest.mark.parametrize("name", _MUST_PASS)
def test_必须放行的硬基线(name):
    assert safe_display(name) == name, name


@pytest.mark.parametrize(("raw", "rule"), _MUST_BLOCK, ids=[r[1] for r in _MUST_BLOCK])
def test_必须拦住的硬基线(raw, rule):
    assert safe_display(raw) is None, f"{raw!r} 本该被拦住({rule})"


@pytest.mark.parametrize("name", _REAL_PASS)
def test_实测真实币名原样放行(name):
    """⚠️ 期望值是**叠平空白之后**的那份:样本里有几条带首尾空格,那是上游的原样。"""
    assert safe_display(name) == " ".join(name.split()), name


@pytest.mark.parametrize(("name", "rule"), _REAL_DROPPED, ids=[d[1] for d in _REAL_DROPPED])
def test_实测里被丢弃的就是这几条(name, rule):
    assert safe_display(name) is None, f"{name!r} 现在放行了({rule}),丢弃率的账要重算"


def test_实测丢弃率不超过一成():
    """
    ⚠️⚠️ 这条是 T1 的结论本身:7 / 247 = 2.8%。
       上限写 10% 是给后来人留的余量 —— 谁把阈值收紧到丢掉一成以上的真实名字,
       这条会红,而不是等用户来报"推送里的币名全没了"。
    """
    total = len(_REAL_PASS) + len(_REAL_DROPPED)
    assert total == 247, "样本表被改动过,实测结论要重跑"
    assert len(_REAL_DROPPED) / total <= 0.10


# ============================================================
# 阈值的**边界**:恰好放行 / 多一格就丢
# ============================================================
# ⚠️⚠️ 这一节存在的唯一理由:上面那些样本离阈值都很远("Send SOL to my wallet now" 6 词,
#    词数上限从 5 改成 6 它才漏)。没有边界样本,"把上限抬一格"就是一个**改不红**的
#    等价变异 —— 而阈值恰恰是这个模块里最容易被人顺手改的东西。
#    每一对都是 (刚好过, 多一格就不过),数字全部写死字面量。
_BOUNDARIES = [
    # (标签, 恰好放行的串, 多一格就丢的串)
    # ⚠️ 拆成 5 个词而不是一长串:连续 ≥26 位 base58 字符会先被地址形态那条拦下,
    #    那样测到的就不是长度上限了。
    ("长度上限 40", "Abcdefg Abcdefg Abcdefg Abcdefg Abcdefgh",
     "Abcdefg Abcdefg Abcdefg Abcdefg Abcdefghi"),
    ("长度下限 1", "猫", ""),
    ("词数上限 5", "One Two Three Four Five", "One Two Three Four Five Six"),
    ("标点上限 3", "U.S.A. Token", "U.S.A.B. Token"),
    ("连续数字 5 位", "Coin 12345", "Coin 123456"),
    ("裸 hex 16 位", "1a2b3c4d5e6f1a2", "1a2b3c4d5e6f1a2b"),
    ("base58 26 位", "abcdefghjkmnpqrstuvwxyzAB", "abcdefghjkmnpqrstuvwxyzABC"),
    ("含 CJK 时 ASCII 串 5 位", "龙Long", "龙Longg"),
]


@pytest.mark.parametrize(("label", "ok", "bad"), _BOUNDARIES, ids=[b[0] for b in _BOUNDARIES])
def test_阈值边界两侧都钉住(label, ok, bad):
    assert safe_display(ok) == (ok or None), f"{label}:{ok!r} 本该刚好放行"
    assert safe_display(bad) is None, f"{label}:{bad!r} 本该刚好被拦"


def test_每一个Unicode空白都算空白():
    """
    ⚠️⚠️ nameguard.flatten 靠**无参数**的 `str.split()` 把所有"空白替身"
       (NBSP U+00A0、表意空格 U+3000、行分隔符 U+2028、窄 NBSP U+202F…)一并叠平 ——
       它不再手写一张 Z* 替换表。这条把那个前提逐码点验一遍:
       Unicode 里类别以 Z 开头的字符**全体**都满足 str.isspace()。
       前提一旦不成立(或者有人把 split() 写成 split(" ")),`t.<NBSP>me`
       就会在域名规则面前隐身。
    """
    import unicodedata

    off = [hex(cp) for cp in range(0x110000)
           if unicodedata.category(chr(cp)).startswith("Z") and not chr(cp).isspace()]
    assert off == [], f"这些 Z* 码点不被 str.split() 当空白:{off}"
    for ws in ("\u00a0", "\u3000", "\u2028", "\u2029", "\u202f", "\u1680", "\t", "\r", "\n"):
        assert safe_display(f"Cash{ws}Cat") == "Cash Cat", repr(ws)


# ============================================================
# 交易所名:形态正则的边界(它不走形状白名单)
# ============================================================
_EXCHANGE_OK = ["NasdaqGM", "NasdaqGS", "NYSE", "NYSEArca", "NYSE American", "AMEX",
                "A" * 20]
_EXCHANGE_BAD = [
    ("A" * 21, "总长上限 20:21 位就不是交易所代码了"),
    ("1NYSE", "必须字母开头"),
    ("立即访问 t.me/free-airdrop", "整段不匹配 —— 这个字段曾经全程零门禁"),
    ("NYSE/AMEX", "`/` 不在允许字符里"),
    ("NYSE, Inc.", "`,` 不在允许字符里"),
    ("", "空 → None"),
]


@pytest.mark.parametrize("code", _EXCHANGE_OK)
def test_交易所名放行(code):
    from src.nameguard import safe_exchange
    assert safe_exchange(code) == code, code


@pytest.mark.parametrize(("code", "why"), _EXCHANGE_BAD, ids=[e[1] for e in _EXCHANGE_BAD])
def test_交易所名拦住(code, why):
    from src.nameguard import safe_exchange
    assert safe_exchange(code) is None, f"{code!r} 本该被拦({why})"
