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
长度下限      **没有常量** —— "非空即可"。单字中文译名("猫")是正常结果,
                  1 个字也装不下任何攻击载荷。(上一版那个 _MIN_CHARS = 1 是死代码,
                  safe_display 在它之前已经 `if not text: return None`,已删。)
_MAX_WORDS = 5    实测最多 5 词;"Send SOL to my wallet now" 6 词、
                  "Buy now 100% safe visit my profile" 7 词 —— 一句话总比一个名字长。
                  ⚠️ 实测里有 2 条 6 词的真名字("One Dollar Is All You Need" /
                     "one life, its worth an attempt")因此被误杀 —— 这是**冲突样本**:
                     放行它们就等于放行 "Send SOL to my wallet now"。按"拦住优先"取舍。
_MAX_PUNCT = 3    实测上界只有 2;抬到 3 是为了放行缩写式名字 "U.S.A. Token"(3 个点)。
                  抬这一格不会多放行任何一条攻击基线。
_MAX_DIGITS = 5   判的是**数字总个数**不是"连续几位"。上一版写的是"连续 ≥6 位",
                  而 `-` `.` 空格都在字符白名单里 —— '138-0013-8000' / '138 0013 8000' /
                  '138.0013.8000' 全部平凡绕过。实测 247 个真实币名的数字总量分布
                  {0: 214, 1: 20, 2: 8, 3: 3, 4: 2},上界 4('NASDAQ 6900');
                  85 个 Yahoo longName 上界也是 4。留一格取 5。**不许松。**
                  ⚠️ 总量规则严格强于连续规则(连续 n 位 ⇒ 总量至少 n),
                     所以"连续"那条已经删掉,不留被完全覆盖的死规则。
CJK 判定          用 unicodedata.category(+ 字符名),**不是码点区间**。上一版的
                  `[぀-ヿ…]` 里混着 61 个非字母码点,其中 U+30FB(片假名中点)
                  与字段分隔符 `·` 同形 —— "`·` 不许松"那条当时实际已经破了。
半角冒号 `:`      已从标点白名单拿掉(CJK 打头的 `加我微信:abcd` 从 scheme 规则下面漏过)。
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
# ⚠️ "SPDR S&P 500 ETF Trust" **不是 Yahoo 实际返回的字符串**(实测 SPY 的 longName 是
#    "State Street SPDR S&P 500 ETF Trust",7 个词,过不了词数上限)。它留在这里的身份是
#    **5 词 + `&` + 3 位数字的边界样本**,不是"Yahoo 会给这个值"的断言 ——
#    上一轮报告点名说它冒充了实测值,这里改口。真实的 Yahoo longName 见 _REAL_YAHOO_*。
_MUST_PASS = [
    "Cummingtonite", "Cash Cat", "dogwifhat", "Artificial Inu",
    "Quantum White Fiber Rabbit", "USA Rare Earth, Inc.", "WhiteFiber, Inc.",
    "NVIDIA Corporation", "SPDR S&P 500 ETF Trust",
    "Space Exploration Technologies Corp.", "U.S.A. Token",
    "镁铁闪石", "美国稀土公司", "英伟达", "Tether Gold",
    # 2026-09-02 实测的**币股名**(剥掉 " • Robinhood Token" 后缀之后的那份,
    # 剥后缀在 dexscreener 数据层做,见 test_dexscreener 里那条)。
    # 上一轮它们连同 📝 行整段消失,是这一轮必须放行的东西。
    "Circle Internet Group", "AMC Entertainment", "Meta Platforms",
    "United States Oil Fund", "ASML Holding NV",
]

# ============================================================
# 硬基线 1.5:真实的 Yahoo longName(2026-09-02 实测 87 个 ticker,85 个有值)
# ============================================================
# ⚠️ 这一段是**实话**:Yahoo 的 longName 比币名长得多,门禁会丢掉一部分(实测 13/85 = 15.3%)。
#    丢掉不等于那一行没了 —— 🌊 那行拿不到 Yahoo 的名字就退回 DexScreener 剥完后缀的
#    issuer(见 poller._name_extras),所以代价是"名字不是最权威的那份",不是"没有名字"。
_REAL_YAHOO_PASS = [
    "NVIDIA Corporation", "Apple Inc.", "Tesla, Inc.", "Microsoft Corporation",
    "Meta Platforms, Inc.", "Alphabet Inc.", "Circle Internet Group",
    "GameStop Corp.", "ASML Holding N.V.", "United States Oil Fund, LP",
    "McDonald's Corporation", "The Coca-Cola Company", "Ford Motor Company",
    "Invesco QQQ Trust", "iShares Russell 2000 ETF", "ARK Innovation ETF",
    "Vanguard S&P 500 ETF", "SPDR Gold Shares", "iShares Silver Trust",
    "Berkshire Hathaway Inc.", "Royal Bank of Canada", "Shopify Inc.",
    "Rio Tinto Group", "BHP Group Limited", "Imperial Oil Limited",
    "Chevron Corporation", "JPMorgan Chase & Co.", "Walmart Inc.", "AT&T Inc.",
    "Pfizer Inc.", "The Walt Disney Company", "NIKE, Inc.", "The Boeing Company",
    "Trio-Tech International", "Galiano Gold Inc.", "SAP SE", "Comstock Inc.",
    "i-80 Gold Corp.", "Eni S.p.A.", "Toyota Motor Corporation",
    "Sony Group Corporation", "Volkswagen AG", "Strategy Inc",
    "iShares Bitcoin Trust ETF", "ProShares Bitcoin ETF", "ProShares UltraPro QQQ",
    "iShares MSCI EAFE ETF",
]
_REAL_YAHOO_DROPPED = [
    ("Amazon.com, Inc.", "形态:域名 —— 公司名里真的含一个真域名,这条是**真误伤**"),
    ("State Street SPDR S&P 500 ETF Trust", "词数 7 > 5"),
    ("State Street SPDR Dow Jones Industrial Average ETF Trust", "长度 56 > 40"),
    ("State Street Energy Select Sector SPDR ETF", "词数 7 > 5"),
    ("iShares 20+ Year Treasury Bond ETF", "字符白名单外的 `+`(Telegram 可拨号通道)"),
    ("Taiwan Semiconductor Manufacturing Company Limited", "长度 49 > 40"),
    ("International Business Machines Corporation", "长度 42 > 40"),
    ("Nestlé S.A.", "非 ASCII 拉丁字母 é(同形字取舍)"),
    ("Petróleo Brasileiro S.A. - Petrobras", "非 ASCII 拉丁字母 ó"),
    ("Direxion Daily Semiconductor Bull 3X Shares", "长度 42 > 40"),
    ("Direxion Daily S&P500 Bull 3X Shares", "词数 6 > 5"),
    ("ProShares Ultra VIX Short-Term Futures ETF", "词数 6 > 5"),
    ("iPath Series B S&P 500 VIX Short-Term Futures ETN", "词数 9 > 5"),
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
    # ======== 本轮新增(三个复验者在 50f19ac 上亲手打出来的)========
    ("已清仓 ・ 亏损 99%", "U+30FB 片假名中点与 `·` 同形 —— 上一版的 CJK 码点区间放行了它"),
    ("・", "U+30FB 单独一个也不许:它是伪造分隔符的原料"),
    ("Coin ゛ Token", "U+309B(Sk)也混在上一版那段区间里"),
    ("Coin ゠ Token", "U+30A0(Pd 连字号)同上"),
    ("138-0013-8000", "数字总量 11 > 5 —— 连续那条被 `-` 平凡绕过"),
    ("138 0013 8000", "数字总量 11 > 5 —— 被空格绕过"),
    ("138.0013.8000", "数字总量 11 > 5 —— 被 `.` 绕过"),
    ("t.me", "裸域名 —— 它当交易所名时曾被 safe_exchange 整段放行"),
    ("evil.zzz", "形态:域名。⚠️ TLD 'zzz' **不在** _SPLIT_DOMAIN_TLDS 里,"
                 "所以只有 _RE_DOMAIN 这一条拦得住它 —— 这是那条规则的**独占**覆盖"),
    ("t. ME/scam", "NBSP 拆开 + **大写** TLD:靠 _looks_like_split_domain 里的 .lower()"),
    ("...", "纯标点不是名字:每个字符都在标点白名单里,长度/词数/标点数也都在上限内"),
    ("1.2.3.4", "IPv4 形态:数字总量只有 4,拦不住,单列一条"),
    ("加我微信:abcd", "半角冒号已从标点白名单拿掉 —— scheme 那条只认 ASCII 打头的形态"),
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

# 2026-09-02 **重测**(本轮改完之后,同一批 247 条):丢弃的仍然是上面这 7 条,
# 一条不多一条不少 —— 新加的四条规则(数字总量 / IPv4 / 纯标点 / 去掉冒号)
# 在真实币名上零误伤。这条由 test_实测丢弃率不超过一成 与上面的参数化用例一起钉着。


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
    ("长度下限:非空即可", "猫", ""),
    ("词数上限 5", "One Two Three Four Five", "One Two Three Four Five Six"),
    ("标点上限 3", "U.S.A. Token", "U.S.A.B. Token"),
    # ⚠️ 数字类的边界样本要**避开对方那条规则**,否则测到的不是自己那一条:
    #    数字总量 5 的边界必须用连续数字(否则连续那条先命中),而裸 hex 的边界
    #    必须**一个数字都不带**(1a2b3c… 里有 8 个数字,数字总量那条会抢先拦下)。
    ("数字总量 5 位", "Coin 12345", "Coin 123456"),
    # ⚠️ 用 A1B2C3… 而不是 "1-2-3-4-5":后者 4 个横杠会先撞上标点上限 3,
    #    测到的就不是数字那一条了。这一对里数字是**离散**的,老的"连续 ≥6 位"一条都拦不住。
    ("数字总量(离散数字)5 位", "A1B2C3D4E5", "A1B2C3D4E5F6"),
    ("裸 hex 16 位", "abcdefabcdefabc", "abcdefabcdefabcd"),
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
# ============================================================
# 交易所名:**封闭枚举**(不是模式匹配)
# ============================================================
# ⚠️⚠️ 上一版是正则 `[A-Za-z][A-Za-z0-9 .\-]{0,19}` —— 字符集里有 `.` 和 `-`,
#    于是**所有 ≤20 字符的裸域名整段 fullmatch**('t.me' / 'discord.gg' /
#    'www.evil-airdrop.com' / 'bit.ly' / 'vitalik.eth' …),而 stock_exchange 是收口表里
#    唯一走这道门的字段、还进永久缓存。交易所是有限集合,直接枚举就没有绕过面。
#
# 表怎么来的(2026-09-02 实测):87 个真实 ticker → Yahoo v8/finance/chart 的
# meta.fullExchangeName,85 个有值,实际取值只有 12 种:
#   NasdaqGS(20) NYSE(28) NasdaqGM(4) NasdaqCM(2) NYSEArca(11) NYSE American(7)
#   Cboe US(4) OTC Markets OTCPK(2) OTC Markets OTCQX(1) OTC Markets OTCID(1)
#   CCC(1,BTC-USD) SNP(1,^GSPC)
# 后两个不是股票交易所(instrumentType 是 CRYPTOCURRENCY / INDEX,数据层的 STOCK_TYPES
# 本来就不让它们走到 🏢 行)→ **不进表**,这里当"表外的值"钉住。
_EXCHANGE_OK = [
    "NasdaqGS", "NasdaqGM", "NasdaqCM", "NYSE", "NYSEArca", "NYSE American",
    "Cboe US", "OTC Markets OTCPK", "OTC Markets OTCQX", "OTC Markets OTCID",
    # 同一批交易所的旧写法 / 别名,收进表里零风险
    "AMEX", "NYSEAmerican", "NasdaqNMS", "BATS",
]
_EXCHANGE_BAD = [
    ("t.me", "裸域名 —— 上一版的正则把它整段放行了,这是本轮的 BLOCKER"),
    ("www.evil-airdrop.com", "裸域名(20 字符,正好在旧上限内)"),
    ("vitalik.eth", "裸域名"),
    ("discord.gg", "裸域名"),
    ("pump.fun", "裸域名"),
    ("bit.ly", "裸域名"),
    ("x.gift", "裸域名"),
    ("tme-scam.io", "裸域名(带横杠)"),
    ("A" * 20, "长度合规也没用:**不在表里就是不在表里**"),
    ("A" * 21, "同上"),
    ("1NYSE", "不在表里"),
    ("NYSEX", "不在表里 —— 只差一个字母的仿冒也进不来"),
    ("CCC", "加密货币的伪交易所(BTC-USD),不是股票交易所,刻意不进表"),
    ("SNP", "指数的伪交易所(^GSPC),同上"),
    ("TSX", "多伦多 —— 实测没出现过,不在表里就不显示(要收就补表 + 补测试)"),
    ("立即访问 t.me/free-airdrop", "整段不匹配 —— 这个字段曾经全程零门禁"),
    ("NYSE/AMEX", "不在表里"),
    ("NYSE, Inc.", "不在表里"),
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


def test_交易所名大小写不同也认但显示用表里的写法():
    """
    ⚠️ 上游偶尔大小写不一致;比对不区分大小写,但**返回表里的规范写法** ——
       🏢 那一行的写法永远是我们自己定的,连大小写都不受上游摆布。
    """
    from src.nameguard import safe_exchange
    assert safe_exchange("nyse american") == "NYSE American"
    assert safe_exchange("NASDAQGS") == "NasdaqGS"
    assert safe_exchange("  NYSE\u00a0Arca  ") is None, "空白位置不对就不是同一个值"


def test_表是封闭枚举不是模式匹配():
    """
    ⚠️⚠️ 这条钉的是"改回模式匹配当场红"。枚举表的大小与内容写死在测试这一侧,
       谁把 safe_exchange 换回正则,下面这一批(每一条都能 fullmatch 那条老正则)
       会立刻放行。
    """
    from src.nameguard import safe_exchange
    for bad in ("t.me", "bit.ly", "NYSE-2", "Nasdaq GS", "nasdaq", "Nasdaq"):
        assert safe_exchange(bad) is None, bad


# ============================================================
# E2:CJK 白名单的**逐码点**自测 —— 防 U+30FB 再混进来的护栏
# ============================================================
def test_CJK白名单里没有任何标点空白控制符():
    """
    ⚠️⚠️ 这条断言就是 U+30FB 那个洞的护栏本身。枚举**整个 0x110000 码点空间**,
       把门禁认作"中日韩文字"的每一个码点都拿出来,断言它的 Unicode 类别
       **一个都不属于** P*(标点)/ S*(符号)/ Z*(空白)/ C*(控制、私用、未分配)。
       上一版的码点区间在这条下会红 61 次,其中就有与字段分隔符 `·` 同形的 U+30FB。
    ⚠️ 断言写死类别字母,不从被测模块 import 任何区间 / 前缀表。
    """
    import unicodedata

    from src.nameguard import is_cjk_char

    bad = [f"U+{cp:04X}({unicodedata.category(chr(cp))})"
           for cp in range(0x110000)
           if is_cjk_char(chr(cp)) and unicodedata.category(chr(cp))[0] in "PSZC"]
    assert bad == [], f"这些码点被当成了中日韩文字,但它们是标点/符号/空白/控制符:{bad[:20]}"


def test_那几个曾经混进来的码点逐个钉住():
    """⚠️ 上一版把它们当中日韩文字放行,其中 U+30FB 与本项目的字段分隔符同形。"""
    from src.nameguard import is_cjk_char, safe_display
    for cp, why in ((0x30FB, "KATAKANA MIDDLE DOT,与 SEP 的 `·` 同形"),
                    (0x309B, "浊音符 Sk"),
                    (0x309C, "半浊音符 Sk"),
                    (0x30A0, "KATAKANA-HIRAGANA DOUBLE HYPHEN,Pd"),
                    (0x3001, "表意逗号"),
                    (0x3002, "表意句号")):
        assert not is_cjk_char(chr(cp)), f"U+{cp:04X} {why}"
        assert safe_display(f"币{chr(cp)}名") is None, f"U+{cp:04X} {why}"


def test_真正需要的假名与汉字一个都不许丢():
    """⚠️ 收紧 CJK 判定不能顺手把真名字打掉。ー 与 々 是 Lm 不是 Lo,单独钉住。"""
    from src.nameguard import safe_display
    for name in ("ナッツ", "コーヒー", "佐々木", "八重", "奶蛙", "镁铁闪石", "가나다", "龍"):
        assert safe_display(name) == name, name


# ============================================================
# E3:数字规则是**总量**不是"连续"
# ============================================================
_DIGIT_CASES = [
    ("138-0013-8000", None, "手机号插横杠:11 位"),
    ("138 0013 8000", None, "手机号插空格"),
    ("138.0013.8000", None, "手机号插点"),
    ("13800138000", None, "手机号原样"),
    ("1 2 3 4 5 6", None, "六个孤立数字照样算 6"),
    ("1000X", "1000X", "4 位,正常"),
    ("Web3", "Web3", "1 位"),
    ("SPDR S&P 500 ETF Trust", "SPDR S&P 500 ETF Trust", "3 位"),
    ("NASDAQ 6900", "NASDAQ 6900", "实测上界 4 位"),
    ("Coin 12345", "Coin 12345", "5 位,恰好在上限"),
]


@pytest.mark.parametrize(("raw", "want", "why"), _DIGIT_CASES, ids=[c[2] for c in _DIGIT_CASES])
def test_数字总量规则(raw, want, why):
    assert safe_display(raw) == want, f"{raw!r}({why})"


def test_总量规则严格强于连续规则():
    """
    ⚠️⚠️ "连续 ≥6 位"那条已经删掉,理由是它被总量规则**完全覆盖**:
       连续 n 位数字 ⇒ 数字总量至少 n。这条把那个推理直接验一遍 ——
       凡是老规则会拦的,新规则也拦;而新规则多拦了一批老规则漏掉的。
    ⚠️ 阈值 6 写死字面量,不从被测模块 import。
    """
    import re
    old = re.compile(r"[0-9]{6,}")
    for raw in ("联系电话13800138000", "抽奖码123456", "Coin 999999", "138-0013-8000",
                "1.3.8.0.0.1.3.8", "Coin 12345", "1000X", "NASDAQ 6900"):
        if old.search(raw):
            assert safe_display(raw) is None, f"老规则拦得住的,新规则也必须拦:{raw!r}"
    # 老规则漏、新规则拦 —— 这就是这次改动的全部价值
    assert old.search("138-0013-8000") is None
    assert safe_display("138-0013-8000") is None


# ============================================================
# E8:_RE_DOMAIN 的**独占**覆盖
# ============================================================
def test_域名规则有自己独占的样本():
    """
    ⚠️⚠️ 上一轮 _RE_DOMAIN 是**零独占覆盖**的:把它打成永不匹配,1768 条测试全绿 ——
       因为语料里每个域名的 TLD 恰好都在手写的 _SPLIT_DOMAIN_TLDS(48 项)里,
       被"被空白拆开的域名"那条捎带拦了。
    ⚠️ 两条规则的分工:
       · _RE_DOMAIN        —— 判**没被拆开**的域名,与 TLD 是什么无关(冷门 TLD 也拦)。
       · _looks_like_split_domain —— 只判"抽掉空白之后才长出来"的域名,
         额外要求 TLD 真实存在,否则 "U.S.A. Token" 这类缩写名字会被误杀。
       下面这一批的 TLD 全部**不在**那张 48 项表里,所以只有 _RE_DOMAIN 拦得住它们。
    """
    for raw in ("evil.zzz", "airdrop.qqq", "claim.bank", "free.rocks", "get.ninja"):
        assert safe_display(raw) is None, f"{raw!r} 只有 _RE_DOMAIN 拦得住"
    # 反过来:这一批**没有**域名形态(点后面不是 2 个以上字母),必须放行
    for raw in ("U.S.A. Token", "Inc. Coin", "Web3.0 Protocol", "St. Louis Coin"):
        assert safe_display(raw) is not None, raw


def test_被空白拆开的域名规则有自己独占的样本():
    """
    ⚠️ 这一批**只有** _looks_like_split_domain 拦得住:抽掉空白之前不含域名形态
       (点后面紧跟的是空白),抽掉之后才长出 t.me / x.com。
    ⚠️ 其中大写那条钉的是 .lower():少了它 't.<NBSP>ME/scam' 当场漏过去
       (上一轮那句 .lower() 是零覆盖的)。
    """
    for raw in ("t. me/scam", "t.\u00a0me/scam", "t.\u00a0ME/scam", "X. COM/free"):
        assert safe_display(raw) is None, repr(raw)


# ============================================================
# 真实 Yahoo longName 的实测基线(87 个 ticker,85 个有值)
# ============================================================
@pytest.mark.parametrize("name", _REAL_YAHOO_PASS)
def test_真实Yahoo公司名放行(name):
    assert safe_display(name) == name, name


@pytest.mark.parametrize(("name", "why"), _REAL_YAHOO_DROPPED,
                         ids=[d[1] for d in _REAL_YAHOO_DROPPED])
def test_真实Yahoo公司名里被丢弃的就是这几条(name, why):
    assert safe_display(name) is None, f"{name!r} 现在放行了({why}),丢弃率的账要重算"


def test_Yahoo公司名的丢弃率是实测出来的那个数():
    """
    ⚠️ 15.3% 比币名的 2.8% 高得多,原因是公司全名天生长、词多。**这不等于那一行没了**:
       🌊 那行拿不到 Yahoo 的名字就退回 DexScreener 剥完后缀的 issuer
       (见 poller._name_extras),代价是"名字不是最权威的那份",不是"没有名字"。
    """
    total = len(_REAL_YAHOO_PASS) + len(_REAL_YAHOO_DROPPED)
    assert total == 60, "样本表被改动过,实测结论要重跑"
    assert len(_REAL_YAHOO_DROPPED) / total <= 0.25


def test_别的文字体系不算中日韩():
    """
    ⚠️⚠️ CJK 判定用的是 `category(ch) == 'Lo'` **加上字符名前缀**。少了后半句,
       全世界所有"其他字母"(阿拉伯、希伯来、泰、天城体、埃塞俄比亚…)会一起放行 ——
       而 M05 那次变异证明:光靠上面那些用例,把前缀限制去掉是**改不红**的等价变异。
    ⚠️ 这不是"歧视别的文字",是这个功能的边界:推送里的名字只可能是中英日韩,
       而阿拉伯/希伯来是 RTL 文字,与 bidi 那条(整段丢弃)是同一类视觉风险。
    """
    from src.nameguard import is_cjk_char, safe_display
    for ch, why in (("ا", "阿拉伯字母 ALEF(Lo,RTL)"),
                    ("א", "希伯来字母 ALEF(Lo,RTL)"),
                    ("ก", "泰文字母 KO KAI(Lo)"),
                    ("क", "天城体字母 KA(Lo)"),
                    ("ሀ", "埃塞俄比亚音节 HA(Lo)"),
                    ("一", "对照:汉字"),):
        want = ch == "一"
        assert is_cjk_char(ch) is want, f"U+{ord(ch):04X} {why}"
    assert safe_display("العرب") is None, "整段阿拉伯文不许当名字"
    assert safe_display("币ا名") is None, "混进一个阿拉伯字母也不许"
    assert safe_display("币ก名") is None, "混进一个泰文字母也不许"
