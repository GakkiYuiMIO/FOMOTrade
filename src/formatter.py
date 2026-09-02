"""
FomoEvent → Telegram HTML 消息渲染(设计文档 §10)

============ formatter 实现铁律(§10.4) ============
1. 标题行第一个字符必须是事件 emoji,且全局唯一 —— 快速滑动时唯一的扫描锚点
2. 任何可选字段缺失时「整行消失」,绝不打印 "N/A" / "--" / "0"
3. badge 三态,None 表示数据不足 —— 宁可漏标不可错标,None 一律渲染成 🟢
4. 所有来自 API 的文本(handle / thesis 正文)必须 html.escape,否则一个 '<' 就 400
5. 不使用空行分组:手机端空行照样占行高,全推场景一屏只放得下一条半消息

补充两条同样致命的:
6. CA 独占最后一行、纯 <code>、绝不截断、不加「CA:」前缀、不用 <a> 包裹 ——
   点 <code> 实体一键复制是中国网络下唯一 100% 可用的操作(§10.3)
7. 本模块是纯函数,不做任何 IO / DB 访问 —— 拿不到真实 API 数据的阶段,
   它是唯一能被完整单测的展示层

⚠️ 「缺失即整行消失」的判据一律是 `is None`,**绝不能用真值判断**:
   amount_usd=0.0 / holders=0 都是有意义的真实值(卖出清仓就靠 `📦 剩余 $0.00` 体现),
   用 `if not x` 会把它们连同 None 一起吞掉。
"""
from __future__ import annotations

import functools
import html
import inspect
import math
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

from loguru import logger

from src.models import (
    BADGE_FIRST,
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    GMGN_SLUG,
    NETWORK_DISPLAY,
    NETWORK_SLUG,
    FomoEvent,
)

# 外部文本的展示门禁。⚠️ 与 namecn 里"送翻译前"那道是**两道独立的墙**:
#    译文是另一个来源(代理可篡改),渲染前必须自己再过一遍。nameguard 零依赖,不会成环。
# ⚠️⚠️ _no_controls 用的是**保留 ZWJ** 的那个版本(strip_controls_keep_emoji):
#    它服务的是 handle / symbol / 观点正文这些**用户自己写的**字段,而 U+200D 是
#    组合 emoji(👨‍💻 / 🏳️‍🌈)的粘合剂,全删会把它们拆开。不可信名字那条路走
#    safe_display,里面用的是全删版本 —— 两条路刻意分开,各自有测试。
from src.nameguard import (
    safe_address,
    safe_display,
    safe_exchange,
    safe_ident,
    safe_launchpad,
    safe_social_links,
)
from src.nameguard import strip_controls_keep_emoji as _no_controls

# ⚠️ 只借 MAX_MESSAGE_LEN 这一个常量(与 bot.py 同样的做法):
#    转入告警要自己算长度预算,而"上限是多少"必须与真正发消息的那一侧同源 ——
#    两处各写一个数字,改了一处另一处就变成一颗定时炸弹。
#    notifier 只依赖 config,不依赖本模块,所以这个 import 不会成环。
from src.notifier import MAX_MESSAGE_LEN

# ============================================================
# 行首锚点(§10.1)—— 全局唯一,永不重复,永不被别的字符挤掉
# ============================================================
EMOJI_FIRST = "🌱"           # 首次建仓
EMOJI_ADD = "🟢"             # 加仓(也是 badge=None 时的兜底,铁律 3)
EMOJI_SELL = "🔴"            # 卖出
EMOJI_THESIS = "💭"          # 发表观点
EMOJI_TRANSFER_IN = "📥"     # 收到转入
EMOJI_TRANSFER_OUT = "📤"    # 转出

# 特别关注标记。⚠️ 它跟在事件 emoji **后面**,不占行首(见 _title_line 里的说明)
STAR_MARK = "⭐"

LABEL_FIRST = "首次建仓"
LABEL_ADD = "加仓"
LABEL_SELL = "卖出"
LABEL_THESIS = "发表观点"
LABEL_TRANSFER_IN = "收到转入"
LABEL_TRANSFER_OUT = "转出"
# 方向判不出来时的标题文案(§九 降级矩阵:「标题写『交易』,无徽章无共识」)
LABEL_UNKNOWN_SIDE = "交易"

# ============================================================
# 第二列以后的 emoji —— 永不占行首
# ============================================================
EMOJI_AMOUNT_IN = "💰"       # 买入 / 转账数量
EMOJI_AMOUNT_OUT = "💸"      # 卖出
EMOJI_HOLDING = "📦"
EMOJI_AVG_PRICE = "📊"
EMOJI_TRADE_COUNT = "🔄"
EMOJI_MARKET_CAP = "💎"
EMOJI_TOKEN_AGE = "🕐"
EMOJI_CONSENSUS = "👥"
EMOJI_NETWORK = "🧬"
EMOJI_COUNTERPARTY = "👤"
EMOJI_WARN = "⚠️"
EMOJI_PENDING = "⏳"
EMOJI_PNL_UP = "📈"
EMOJI_PNL_DOWN = "📉"
EMOJI_LINK = "🔗"
EMOJI_POOL = "🌊"            # 底池对手资产
EMOJI_TOKEN_ZH = "📝"        # 币名的中文译名(紧跟标题)
EMOJI_STOCK = "🏢"           # 底池对手股票的说明(紧跟 🌊)。⚠️ 📈 已被盈亏占用,别撞
EMOJI_LAUNCHPAD = "🚀"       # 发射台(这个币是从哪个平台发出来的)
# ⚠️⚠️ 持有人数**必须**用 🧑‍🤝‍🧑 而不是 👥:👥 已经被「名单内 3/108 人买过」占了,
#    两条含义完全不同的行共用一个 emoji,扫一眼会被读成同一件事(用户亲自定的)。
#    它由 ZWJ 粘合(U+1F9D1 U+200D U+1F91D U+200D U+1F9D1)—— 是本模块自己的常量,
#    不经过任何"删 Cf 字符"的通道(那条只作用于外部文本,见 _esc)。
EMOJI_TOKEN_HOLDERS = "🧑‍🤝‍🧑"

# ============================================================
# 固定文案
# ============================================================
SEP = " · "                                      # 标题行分隔符(U+00B7)
# ⚠️⚠️ **视觉容器** —— 所有不可信外部文本(币名 / 译名 / 公司名)渲染时都套这一层。
#    这是 "`·` 伪造字段" 那个漏洞的**真正解**:标题是 `🌱 alice · 首次建仓 · $CUM · {币名}`,
#    币名完全攻击者可控,只要它里面能出现分隔符就能凭空长出两个字段
#    (`已清仓 · 亏损 99%`)。禁掉 `·` 只是治标 —— 用空格、全角标点、纯中文照样能伪造。
#    套上容器之后,伪造的分隔符明显落在容器**内部**:
#      🌱 alice · 首次建仓 · $CUM · 「已清仓 · 亏损 99%」
#    读者一眼看得出后半段是**数据**不是本文。
#    用 CJK 直角引号:不与 Telegram 的 HTML 子集冲突、中文语境读得顺、辨识度高。
#    ⚠️ 名字里如果含 「 或 」 → nameguard 直接整段丢弃(不许转义后放行 ——
#       转义破坏不了 HTML,但破坏了容器的视觉不变量)。
QUOTE_OPEN = "「"
QUOTE_CLOSE = "」"
TEXT_TRANSFER_NOT_BUY = f"{EMOJI_WARN} 转账获得,非市场买入"
TEXT_INTERNAL_TRANSFER = f"{EMOJI_WARN} 名单内转账"
TEXT_BASELINE_PENDING = f"{EMOJI_PENDING} 基线建立中,首次/共识暂不可用"
TEXT_UNKNOWN_USER = "未知用户"

LABEL_HOLDING_BUY = "持仓"
LABEL_HOLDING_SELL = "剩余"      # 清仓自然体现为「📦 剩余 $0.00」,不设独立的清仓事件类型
LABEL_HOLDING_THESIS = "他持仓"  # 「他」字必须有,否则会被误读成 token 总量(§10.2 场景 E)
LABEL_AMOUNT_QTY = "数量"
LABEL_AMOUNT_USD = "金额"
LABEL_FROM = "来自"
LABEL_TO = "转给"
# 方向判不出时用的中性说法 —— 「来自」和「转给」都是在断言方向,断错就是彻底的错误信息
LABEL_COUNTERPARTY = "对手方"
LABEL_PNL_REALIZED = "已实现盈亏"
LABEL_PNL_UNREALIZED = "未实现盈亏"
LABEL_POOL = "底池"
LABEL_LAUNCHPAD = "发射台"
LABEL_TOKEN_HOLDERS = "持有人"
# 持有人数的**上界**。⚠️ 这不是"防御性编程",它挡的是一个具体的形态:
#    这个槽位收的是上游给的数,而"一串 10~11 位数字"正是手机号 / QQ 号的形态,
#    印成「持有人 13,800,138,000」既是假事实、又是一条可拨可加的目标。
#    现实里的上界差着两个数量级:实测全库最大的是 base 的 USDC(10,967,609)、
#    bsc 的 BNB(7,118,229)—— 10 亿留了近百倍余量。
#    ⚠️ 超界 → 整行消失(与 _pump_mcap_line 拒绝 ≤0 的市值同一条理由:
#       这一行**说不出口**,少一行是"我们没说",印出来是"我们说错了")。
_MAX_TOKEN_HOLDERS = 1_000_000_000
# 发射台名的限长。⚠️ 它是**上游给的自由文本**(FOMO 的 launchpadName),
# 实测最长的是 "MeteoraDBC"/"UniswapCCA"(10 字符),24 绰绰有余。
_LAUNCHPAD_CHARS = 24
# 社媒链接的**文字**。⚠️⚠️ 这几个字全是我们自己的常量,**永远不用上游给的 label**
#    (DexScreener 的 `websites[].label` 是发币的人填的自由文本,而链接文字带着
#     我们的背书 —— 让攻击者写"官方客服"再指向他的站,是这一整块最贵的那个洞)。
# ⚠️ 键必须与 nameguard.SOCIAL_KINDS 逐字对上;表外的类别在门禁那一侧就已经丢掉了。
SOCIAL_LABELS = {
    "website": "官网",
    "twitter": "Twitter",
    "telegram": "Telegram",
    "discord": "Discord",
    "reddit": "Reddit",
    "github": "GitHub",
}
# 对手全名的限长。⚠️ 它是**第三方(DexScreener)返回的任意字符串**,长度不受任何
# 天然约束,也可能带 `<`/`&` —— 必须走 _clip(叠平空白 → 截断 → 转义)。
# 32:实测最长的一个是 "NVIDIA • Robinhood Token"(24 字符),留一点余量即可;
# 再长就是营销文案,读者也不会读。
_POOL_NAME_CHARS = 32
# 币自己的英文全名(标题尾巴、📝 行左半)限长。同样是 DexScreener 给的**任意字符串**,
# 任何人都能给币起名 —— 必须 _clip(叠平空白 → 截断 → 转义)。
_TOKEN_NAME_CHARS = 32
# 中文译名(📝 右半、🏢 的公司名)限长。译文来自维基/Google,namecn 已过输入/输出两道过滤,
# 这里是第三道:仍按第三方字符串对待,限长 + 转义。
_TOKEN_ZH_CHARS = 24
LABEL_LISTED = "上市"
# ⚠️⚠️ **场外市场不是「上市」**(本轮 F6)。OTC Markets 的 OTCPK / OTCQX / OTCID 三档
#    都是**场外报价**,那些公司恰恰是没有在交易所上市的 —— 印成「OTC Markets OTCPK 上市」
#    是一句事实错误的话(§10.3:错的信息比没有信息更糟)。单独一个说法。
LABEL_OTC = "场外交易"
# OTC Markets 那三档的共同前缀(小写比对)。⚠️ 它们全在 nameguard._EXCHANGES 的封闭枚举里,
#    所以这里认前缀是安全的:能走到这一步的串只可能是表里那三个之一。
_OTC_PREFIX = "otc markets"
# 交易所代码 → 中文。⚠️ 事实(代码本身)只从 Yahoo 来,这里只是**展示映射**,
#    映射不到的只印原代码(绝不猜一个中文名)。Nasdaq 有 GS/GM/CM/NMS 四种写法,前缀匹配。
# ⚠️⚠️ **同一个交易所的每一种写法都必须映射到同一个中文**(本轮 F6):
#    上一版收了 "nyse american" 与 "amex" 却漏了无空格写法 "nyseamerican",
#    于是同一家交易所在推送里一会儿是「美国证券交易所」一会儿是「NYSEAmerican」。
#    nameguard._EXCHANGES 里每一个别名在这里都要有一行,由测试逐条钉住。
_EXCHANGE_ZH = {
    "nyse": "纽约证券交易所",
    "nysearca": "纽交所 Arca",
    "nyse american": "美国证券交易所",
    "nyseamerican": "美国证券交易所",   # 同一家的无空格写法
    "amex": "美国证券交易所",           # 同一家的旧名
    "cboe us": "芝加哥期权交易所美国市场",
    "bats": "芝加哥期权交易所美国市场",  # Cboe US 的旧名
}


# ============================================================
# 不可信字段的**单一收口**
# ============================================================
# ⚠️⚠️ 这张表存在的理由是一个真实的失败模式:过去 poller / pumpfun / formatter
#    **各自记得**给哪些字段套 safe_display,于是漏了两个 ——
#      · 🏢 行的交易所名(Yahoo fullExchangeName)全程零门禁;
#      · 🌊 行的对手全名(pool_quote_name)在渲染层没有门禁。
#    "记得给新字段加过滤"不是一个能靠人维持的不变量。改成**结构上不可能漏**:
#    渲染入口(@_guard_untrusted)按这张表统一过一遍,上游传进来的原始值
#    **一律不直接使用**。新增外部来源字段时只需要在这里登记一行。
#
# ⚠️ 配套的测试(tests/test_nameguard_chokepoint.py)做两件事:
#      1. 断言这张表的内容(改表就得改测试,不能悄悄放行);
#      2. 反射检查四个渲染函数的**每一个**关键字参数,要么在这张表里(不可信),
#         要么在 _REVIEWED_PARAMS 里(已逐个复核过)。**新加一个字段两边都不登记 → 测试红。**
UNTRUSTED_FIELDS = {
    "token_name": safe_display,        # DexScreener baseToken.name,谁都能给自己的币起名
    "token_name_zh": safe_display,     # 维基 / Google 译文,外呼走代理,代理能篡改
    "pool_quote_name": safe_display,   # 底池对手全名(DexScreener 或 Yahoo longName)
    "stock_company_zh": safe_display,  # 公司名译文,来源同 token_name_zh
    "stock_exchange": safe_exchange,   # Yahoo fullExchangeName,**封闭枚举**不是模式匹配
    # ⚠️⚠️ 本轮(G3)补登记:币安 Alpha 上新那条推送的**币名**。它与 token_name 是
    #    同一类东西(链上文本,谁都能给自己发的币起名),上一版却登记成"已审查、
    #    只走 _clip",既没门禁也没容器,于是 '已清仓 · 亏损 99%' 与
    #    'Join t.me/pumpgroup now' 原样拼进标题行的 SEP 之后 —— 笛卡尔积里
    #    唯一一个泄漏的名字类槽位。见 render_alpha_listing 的注释。
    "name": safe_display,              # 币安 Alpha 的币名
    # ⚠️⚠️ 本轮(H1)新增:🚀 那一行的发射台名,走 **safe_launchpad** ——
    #    **封闭枚举**,与 stock_exchange 同一套路数,不是 safe_display。
    #    理由是实测:真实的发射台名里最常见的几个恰好是**域名形态**
    #    (Pump.fun 1139 个 / o1.exchange 44 / Four.meme 27 / Feel.cash 5 …),
    #    safe_display 的"域名形态整段丢弃"会打掉 35.6% 的命中,包括 Solana 上
    #    唯一重要的那一个。发射台不是"名字"、是**平台标识**,世界上就那么几个,
    #    可以逐个数出来 —— 而"能枚举的一律不许手写模式匹配"是本项目上一轮的血教训。
    #    详见 nameguard._LAUNCHPADS 上面那一大段(含全量实测依据与代价)。
    "launchpad": safe_launchpad,
}

# 译文字段 → 它的**原文**是哪个字段。⚠️ safe_display 的"含 CJK 时不许有 ≥5 位 ASCII 串"
#    那条判的是"凭空**多出**原文没有的英文串",少了原文它会把 '特斯拉 xStock' 这类
#    正常译文全毙掉(实测 xStock 全家族 100% 丢译名)。所以这里把两者配上对。
#    ⚠️ 配错方向没有安全后果(只会更严),配漏了会误杀 —— 新增译文字段时在这里登记。
_GUARD_SOURCE = {
    "token_name_zh": "token_name",          # 📝 行左半就是它的原文
    "stock_company_zh": "pool_quote_name",  # 🏢 行的公司名,原文是 Yahoo 的 longName
}

# ⚠️⚠️ **symbol / handle 类字段的轻门禁**(nameguard.safe_ident)。它们和被门禁保护的
#    币名**印在同一行**,而上一版把它们登记成"已审查、只走 _clip",于是门禁掐掉的出口
#    在旁边被原样打开:
#        render(token_symbol='t.me/pumpgrp')      → 🌱 … · <b>$t.me/pumpgrp</b>
#        render(pool_quote_symbol='discord.gg/x') → 🌊 底池 · discord.gg/x · 「…」
#    symbol 与 handle 同样是攻击者可控的(谁都能给币起符号、给自己起用户名)。
#    ⚠️ 只施加**形态**规则(域名/scheme/@提及/0x/base58/裸 hex/IPv4/bidi),
#       **不施加**长度词数标点那套形状规则 —— 符号天生长得怪,套形状会把正经符号全干掉。
#       实测丢弃率:token_symbol 1/2434 = 0.04%,handle + 昵称 0/208 = 0.00%。
#    ⚠️ 命中 → None,那一段按既有"字段缺失"规矩消失(标题没有 $符号 / 🏢 整行消失 /
#       展示名回退成"未知用户"),绝不打占位符。
IDENT_FIELDS = {
    "token_symbol": safe_ident,       # 币的符号(FOMO / pump / DexScreener 都给)
    "pool_quote_symbol": safe_ident,  # 底池对手符号,同时也是 🏢 行的 ticker
    "username": safe_ident,           # pump 用户名,本人可控
    "symbol": safe_ident,             # 币安 Alpha 的币符号
    # ⚠️ 下面两个是**币安 Alpha 上新**那条推送的字段(本轮 F6 补登记)。它们同样是
    #    外部来源的自由文本(币安运营编的板块名、上游给的原始链名),与被门禁保护的
    #    符号印在同一条消息里,上一版**全程零门禁**。用轻门禁而不是 safe_display:
    #    板块名是"股票 Meme 币"这种带空格的短语,形状规则会把它误伤。
    "chain_name": safe_ident,         # 币安给的原始链名(内部映射查不到时的兜底)
    "sector": safe_ident,             # 板块标注
}

# ⚠️⚠️ **地址类字段**(本轮 F6 补登记)。它们不能走 safe_ident —— 那道的 0x / 裸 hex /
#    base58 三条规则本来就是拿来拦地址的,套在"这里就该是一个地址"的槽位上等于全丢。
#    但它同样是上游给的任意字符串,而且会被拼进 fomo.family / gmgn 的 URL 再印成
#    `<code>` 锚点。用**封闭形状**收:一段 ASCII 字母数字,后面可以跟若干个 `::段`
#    (Sui 的类型标签,实测币安真的会给这种),≤128 字符;**单个冒号一律不许** ——
#    那正是 scheme 的形态。见 nameguard.safe_address。
ADDRESS_FIELDS = {
    "contract_address": safe_address,  # 币安 Alpha 上新那条推送的合约地址
}

# ⚠️⚠️ **URL 类字段**(本轮 H1 新增)。它是本项目**第一个**会被放进 `<a href>` 的
#    外部字符串,前面三张表守的都是"印出来的字",这一张守的是"点下去会去哪儿" ——
#    失败后果完全不是一个量级(前者最坏是读者读到一句假话,后者是读者被带到
#    攻击者的站点,而链接文字还是我们自己写的「官网」,天然带着我们的背书)。
#    所以它单开一张表而不是塞进 UNTRUSTED_FIELDS:表的名字本身就是那句
#    "这里的门要按 URL 的口径去看"。
# ⚠️ 门禁函数收的是 ((类别, URL), …) 这个**整体**,返回过完门禁的同形结构或 None ——
#    单条不合格只丢那一条(见 nameguard.safe_social_links)。
URL_FIELDS = {
    "token_socials": safe_social_links,  # DexScreener pair.info 的官网 / 社媒
}

# 渲染入口真正过一遍的全表。⚠️ 四张表**不许有同名键**(一个字段只能有一道门)。
_GUARDED_FIELDS = {**UNTRUSTED_FIELDS, **IDENT_FIELDS, **ADDRESS_FIELDS, **URL_FIELDS}
assert len(_GUARDED_FIELDS) == (len(UNTRUSTED_FIELDS) + len(IDENT_FIELDS)
                                + len(ADDRESS_FIELDS)
                                + len(URL_FIELDS)), "同一个字段登记了两道门"

# 已逐个复核、**不需要**形状门禁的渲染参数。分三类:
#   a. 不是文本(数字 / 布尔 / 时间戳 / 列表 / 事件对象);
#   b. 内部枚举或已归一化的标识(network_id / side / chain_display);
#   c. 符号 / handle / 地址 / 正文类字段 —— 它们**确实是外部可控的**,但走的是
#      _clip(叠平空白 → 限长 → 转义)这条既有通道,形状门禁会把它们(比如带 emoji 的
#      昵称、带点的 ticker)大面积误伤。⚠️ 这是**已知的取舍**,不是遗漏:
#      详见 README「已知取舍」一节。
_REVIEWED_PARAMS = frozenset({
    "ev", "buyers", "watchlist", "holders", "baseline_pending", "starred", "now",
    "network_id", "token_address", "receiver_count", "receivers",
    "window_hours", "senders",
    "side", "coin_mint", "amount_usd", "price_usd", "holding_usd",
    "is_cleared", "unrealized_pnl_usd", "unrealized_pnl_pct", "realized_pnl_usd",
    "realized_pnl_pct", "market_cap_usd", "ath_market_cap_usd", "holders_in_list",
    "traded_at", "chain_display", "tx",
    # 币安 Alpha 上新那条推送的参数。
    # ⚠️ chain_name / sector 已挪进 IDENT_FIELDS、contract_address 挪进 ADDRESS_FIELDS
    #    (F6:它们同样是外部来源,上一版全程零门禁);
    #    `name` 本轮(G3)挪进 UNTRUSTED_FIELDS —— 那个"已知缺口"已经补上。
    "listing_time_ms", "market_cap",
    # ⚠️ token_holders(🧑‍🤝‍🧑 那一行的持有人数)是**数字**:取值层已经把它解析成 int、
    #    把上游的哨兵 0 归成 None(见 tokeninfo.parse_holders),渲染层只做千分位。
    #    形状门禁是给"名字"用的,套在一个 int 上没有意义。
    #    ⚠️ 它**不叫** holders —— 那个名字已经被「名单内 N 人仍持有」与币安 Alpha
    #       那条推送各占一次了。
    "token_holders",
    # pump 喊单那条推送的参数。thesis 是**用户自己写的正文**,走 _clip 不走形状门禁
    # (形状门禁是给"名字"用的,一句话本来就过不了词数上限)。
    "thesis", "multiple", "likes", "view_count", "created_at",
})


def _guard_one(name: str, value, source=None):
    """
    单个不可信字段过门禁。⚠️ 门禁自己炸了也不能让渲染炸 —— 一律当作"不合格"。

    source:译文字段的**原文**(见 _GUARD_SOURCE)。只有 safe_display 收这个参数。
    """
    fn = _GUARDED_FIELDS[name]
    try:
        return fn(value, source) if source is not None and fn is safe_display else fn(value)
    except Exception as e:  # noqa: BLE001
        logger.warning("不可信字段门禁异常,该段不显示 | {} | {}", name, e)
        return None


def _guard_untrusted(fn):
    """
    渲染入口的装饰器:把签名里出现的**每一个**不可信字段替换成过完门禁的值。

    ⚠️ 用签名绑定而不是只看 kwargs:render() 是可以按位置传参的。
    ⚠️ 只替换**调用方真正传了的**参数 —— 没传的保持默认(None),不会凭空多出键。
    """
    sig = inspect.signature(fn)
    names = tuple(n for n in sig.parameters if n in _GUARDED_FIELDS)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        # ⚠️ 先把原文取出来再改写:译文字段要拿**没过门禁的原文**去比对
        #    ("原文自己合不合格"是另一回事,不影响"译文有没有凭空多出英文串")。
        raw = dict(bound.arguments)
        for n in names:
            if n in bound.arguments:
                src = raw.get(_GUARD_SOURCE.get(n))
                bound.arguments[n] = _guard_one(n, bound.arguments[n], src)
        return fn(*bound.args, **bound.kwargs)

    wrapper.__signature__ = sig      # 让反射检查看到真实签名而不是 (*args, **kwargs)
    wrapper.__guarded_fields__ = names
    return wrapper


# ⚠️⚠️ 这里**刻意没有**一个 `unclassified_render_params()` 之类的"自查函数"。
#    上一版有,而 D1 那条不变量的测试写的就是
#      `assert formatter.unclassified_render_params(*_RENDERERS) == set()`
#    —— 那个函数与它要检查的两张表在**同一个模块**里,等于被测模块给自己判卷:
#    把它内部条件改成 `if False and ...`,全量 1768 条测试 **0 红**(实测过)。
#    现在判卷逻辑整个搬到测试那一侧(tests/test_nameguard_chokepoint.py 自己用 inspect
#    取签名,和一份写死在测试文件里的字段清单逐字比对),这里连那个函数一起删掉 ——
#    死代码 + 空转测试是最糟的组合。


def _quoted(s: str) -> str:
    """不可信文本 → 套上视觉容器。⚠️ 传进来的必须是**已经过门禁 + 已转义**的那份。"""
    return f"{QUOTE_OPEN}{s}{QUOTE_CLOSE}"


# 代币页链接。⚠️ 链 slug 未收录时**整个链接不出** ——
# 错的链接比没有链接更糟(§10.3),拼一个平台不支持的链只会得到 404。
FOMO_TOKEN_URL = "https://fomo.family/tokens/{slug}/{ca}"
GMGN_TOKEN_URL = "https://gmgn.ai/{slug}/token/{ca}"

# thesis 正文硬截断长度。TG 单条上限 4096,但一条几百字的观点在全推流里已经过长,
# 超出部分用 expandable 折叠(Bot API 7.4+;旧客户端退化成普通引用,仍可读)
THESIS_MAX_CHARS = 500

# ============================================================
# 链名展示
# ============================================================
# 键是 models.normalize_network() 的输出(全小写)。未命中时原样透传 ——
# ⚠️ 未命中值直接来自 API,必须 escape(见 _network_line)
# 链的展示名与 slug 统一由 models 提供(与 fomo.family 官方映射一致),
# 这里不再维护第二份 —— 两份必然会不同步。

# 观点正文里的连续空行:手机端空行照样占行高(铁律 5),用户原文里的空行同样要压掉
_BLANK_LINES = re.compile(r"\n\s*\n+")

# 金额缩写单位(市值行用)
_COMPACT_UNITS = (
    (Decimal("1e12"), "T"),
    (Decimal("1e9"), "B"),
    (Decimal("1e6"), "M"),
    (Decimal("1e3"), "K"),
)


# ============================================================
# 数值格式化 —— 全部返回 str | None,None 表示「这一行不要出现」
# ============================================================
def _to_decimal(v) -> Decimal | None:
    """
    任意输入 → Decimal。无法解析 / NaN / Inf 一律返回 None(调用方据此整行消失)。

    ⚠️ 必须走 Decimal 而不是 float:token 数量是 1e15 量级的字符串,
       float 在 17 位有效数字之后就开始丢精度,`1,200,000,000,000,001` 会显示成 ...000。
    ⚠️ API 字段语义未实测,任何脏值都可能出现(空串 / "N/A" / 带千分位的字符串),
       这里必须吃掉所有异常,绝不能让一条脏数据炸掉整条推送。
    """
    if v is None:
        return None
    if isinstance(v, bool):          # bool 是 int 的子类,当数量用一定是上游 bug
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("_", "")
        if not s:
            return None
    else:
        s = str(v)
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _fmt_usd(v) -> str | None:
    """普通金额:$2,500.00 / -$12.30 / $0.00(0 是真实值,照常显示)"""
    d = _to_decimal(v)
    if d is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 60                        # 默认 prec=28,大额 quantize 会 InvalidOperation
            q = d.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def _fmt_usd_compact(v) -> str | None:
    """缩写金额(市值行):$19.14M / $2.10B。不足 1000 时退化成普通金额"""
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            for base, suffix in _COMPACT_UNITS:
                if a >= base:
                    return f"{sign}${(a / base).quantize(Decimal('0.01')):,.2f}{suffix}"
    except (InvalidOperation, ValueError):
        return None
    return _fmt_usd(d)


def _fmt_price(v) -> str | None:
    """
    单价:$0.016 / $1,234.56 / $0.0000012345

    ⚠️ 不能复用 _fmt_usd —— memecoin 单价普遍在 1e-6 量级,
       两位小数会把每一个价格都渲染成 $0.00,那一行就成了纯噪音。
    """
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    if a >= 1 or a == 0:
        return f"{sign}${a:,.2f}"
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            # 小于 1 时保留 4 位有效数字:0.016 → 0.016,0.0000012345 → 0.000001234
            exponent = a.adjusted()              # 10 的幂次,0.016 → -2
            q = a.quantize(Decimal(1).scaleb(exponent - 3))
    except (InvalidOperation, ValueError):
        return None
    s = format(q, "f").rstrip("0").rstrip(".")
    return f"{sign}${s or '0'}"


def _fmt_usd_tiny(v) -> str | None:
    """
    金额,但**非零的粉尘额按有效数字给**:$1,234.50 / $0.00 / $0.000002848

    ⚠️⚠️ 存在的理由:pump.fun 逐笔里真的有 amountUSD=0.0000028483994 这种成交。
       两位小数会把它渲染成「💰 金额 $0.00」—— 一行**看起来像缺失值的真实值**,
       与"缺失整行消失、绝不打 0"的观感直接打架(用户会以为字段没取到)。
    ⚠️ 恰好是 0 的仍然走 _fmt_usd 显示成 $0.00:0 是真实值,它就该显示成 0,
       不能被"小额一律有效数字"顺手改掉语义。
    ⚠️ 判据是「两位小数会不会把这个非零值渲染成 0」本身,**不是拍一个阈值**:
       quantize 默认是 banker's rounding,恰好 0.005 也会塌成 0.00,
       写 `< 0.005` 就正好在边界上漏一个。
       (`abs(d) < 1` 只是先挡掉大额 —— 大额永远塌不成 0,却会在默认 prec 下
        让 quantize 抛 InvalidOperation。)
    """
    d = _to_decimal(v)
    if d is None:
        return None
    if d != 0 and abs(d) < 1 and d.quantize(Decimal("0.01")) == 0:
        return _fmt_price(d)
    return _fmt_usd(d)


def _fmt_qty(v) -> str | None:
    """
    token 数量:1,200,000 / 1,250,000.5 / 0.00000123

    ⚠️ 整数部分**永不截断、永不缩写** —— 数量是用户核对链上记录的依据,
       缩写成 1.2M 就核对不了了。小数部分才做截断。
    """
    d = _to_decimal(v)
    if d is None:
        return None
    sign = "-" if d < 0 else ""
    a = abs(d)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            if a >= 1:
                q = a.quantize(Decimal("0.0001"))    # 大额只保留 4 位小数,整数位一位不动
            elif a == 0:
                q = a
            else:
                # 小于 1 保留 8 位有效数字,避免整串 0
                q = a.quantize(Decimal(1).scaleb(a.adjusted() - 7))
            s = format(q, "f")
    except (InvalidOperation, ValueError):
        return None
    int_part, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    # int() 走 Python 大整数,1e30 也不会丢精度;f"{:,}" 负责千分位
    out = f"{int(int_part or '0'):,}"
    return f"{sign}{out}.{frac}" if frac else f"{sign}{out}"


# ============================================================
# 单行渲染 —— 每个函数返回 str | None,None 一律被 render 丢弃
# ============================================================
def _esc(v) -> str:
    """
    所有来自 API 的文本必经之路(铁律 4)。

    ⚠️ handle / symbol / thesis 正文 / 对手方名 全是用户可控内容,
       一个裸 '<' 就让整条消息 400 Bad Request —— 这既是稳定性问题,
       更是一个可被投毒的攻击面(改个昵称就能让监控静默失效)。
    ⚠️⚠️ 顺手删掉 Unicode Cf(格式控制)字符。放在这里是因为它是**唯一**的必经之路:
       symbol / handle / 观点正文都只走 _esc、不走 _flatten,而 U+202E(RTL 覆盖)
       能让它后面的字在 Telegram 里反向显示 —— 一个把自己昵称改成
       "ali<U+202E>ecs" 的人,在推送里看起来就是另一个人。这类字符在这些字段里
       没有任何正当用途。
    ⚠️⚠️ **`「` `」` 也在这里删掉**(本轮 F2 / BLOCKER-3)。它们是本模块自己的
       **结构标记**(视觉容器 QUOTE_OPEN / QUOTE_CLOSE),地位与 `<` 完全一样 ——
       `<` 由 html.escape 转义掉,而 `「` 没有转义形式,只能删。少了这一步,任何一个
       能进 _esc 的字段(symbol / handle / 观点正文 / 对手方名)里塞一个 `」`
       就能把容器提前关掉:12500 条模糊测试里 3255 条「」不配平,全部来自这里。
       ⚠️ 删而不是整段丢弃:观点正文是**用户自己写的**,为一对引号丢掉整条观点更糟;
          名字类字段在上游 nameguard 那道本来就是整段丢弃(它俩不在字符白名单里),
          symbol / handle 也在 safe_ident 那道整段丢弃,这里是**最后一道**。
       ⚠️ 顺序:_quoted 是在 _clip / _esc **之后**才把容器贴上去的,所以这一步删的
          只可能是外部文本自带的那对,绝不会误删我们自己的容器。
    """
    return html.escape(_no_controls(str(v)).replace(QUOTE_OPEN, "").replace(QUOTE_CLOSE, ""))


def _display_name(ev: FomoEvent) -> str:
    """
    展示名。有 @handle 时一并显示:`血手人屠·厉飞雨 (@GakkiYuiTifa)`。

    展示名可以随时改、也可能重名,@handle 才是能拿去搜的那个标识 ——
    两个都给,扫消息时认人、要查证时有据可循。
    两者相同时不重复显示(有些人没设展示名,handle 会被直接当展示名用)。
    """
    # ⚠️ handle 是**本人可控**的字符串,和币名印在同一行 —— 过一道形态轻门禁
    #    (safe_ident:域名/scheme/@提及/地址形态)。命中 → 当作没有展示名,
    #    退回 user_id、再退回"未知用户"(既有的缺失规矩,不打占位符)。
    #    ⚠️ 这几个字段挂在 ev 上、不是渲染参数,收口装饰器够不着,只能在取值处过。
    name = (safe_ident((ev.handle or "").strip()) or "") or (ev.user_id or "").strip()
    return _esc(name) if name else TEXT_UNKNOWN_USER


def _handle_suffix(ev: FomoEvent) -> str:
    """`(@handle)` 后缀。与展示名相同时不重复显示(有人没设展示名,handle 会被当展示名用)"""
    h = safe_ident((ev.user_handle or "").strip().lstrip("@")) or ""
    if not h:
        return ""
    name = (ev.handle or "").strip()
    if name and h.lower() == name.lower():
        return ""
    return f" (@{_esc(h)})"


def _symbol_plain(ev: FomoEvent) -> str | None:
    """去掉 API 可能自带的 $ 前缀,由模板统一补 —— 否则会出现 $$TOAD"""
    s = (ev.token_symbol or "").strip().lstrip("$").strip()
    # ⚠️ 符号同样是陌生人可控的(`$t.me/pumpgrp` 曾原样进标题)→ 形态轻门禁,
    #    命中就当作没有符号(标题少那一段,既有的缺失规矩)。
    return safe_ident(s)


def _title_anchor(ev: FomoEvent) -> tuple[str, str]:
    """
    (行首 emoji, 标题文案)。

    ⚠️ 铁律 3:badge 为 None(数据不足)时一律 🟢 加仓,**绝不显示 🌱** ——
       一屏全 🌱 会让这个符号在用户心里当场作废,而且没有第二次机会。
    """
    et = ev.event_type
    if et == EVENT_THESIS:
        return EMOJI_THESIS, LABEL_THESIS
    if et in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT):
        # ⚠️ 这道门必须在下面两个分支之前。poller 在方向判不出时会把 direction
        #    兜底成 TRANSFER_IN 并置 side_unknown —— 若不拦,一笔实际是转出的记录
        #    会被渲染成「📥 收到转入」,这是彻底的错误信息,比不显示糟得多。
        #    锚点仍复用 📥,不新增第七个行首 emoji(铁律 1:行首 emoji 全局唯一且固定)。
        if ev.side_unknown:
            return EMOJI_TRANSFER_IN, LABEL_UNKNOWN_SIDE
        if et == EVENT_TRANSFER_IN:
            return EMOJI_TRANSFER_IN, LABEL_TRANSFER_IN
        return EMOJI_TRANSFER_OUT, LABEL_TRANSFER_OUT
    if et == EVENT_SELL:
        return EMOJI_SELL, (LABEL_UNKNOWN_SIDE if ev.side_unknown else LABEL_SELL)
    # BUY 与任何未知 event_type 都收敛到这里:兜底成 🟢,不新增第七个行首锚点
    if et != EVENT_BUY or ev.side_unknown:
        return EMOJI_ADD, LABEL_UNKNOWN_SIDE
    if ev.badge == BADGE_FIRST:
        return EMOJI_FIRST, LABEL_FIRST
    return EMOJI_ADD, LABEL_ADD


def _title_line(ev: FomoEvent, starred: bool = False, token_name=None) -> str:
    emoji, label = _title_anchor(ev)
    # ⚠️ 星标只能放在**事件 emoji 之后**,绝不能顶到行首(铁律 1):
    #    行首那个字符是聊天列表预览里唯一的扫描锚点。被 ⭐ 顶掉之后,
    #    所有特别关注的消息在列表预览里长得一模一样,买入卖出当场分不出来。
    mark = f"{STAR_MARK} " if starred else ""
    # 只给展示名加粗:@handle 是辅助信息,一起加粗会把行首锚点的视觉重量冲散
    parts = [f"{emoji} {mark}<b>{_display_name(ev)}</b>{_handle_suffix(ev)}", label]
    sym = _symbol_plain(ev)
    if sym is not None:
        parts.append(_style_symbol(sym, starred))
    # 英文全名跟在 symbol 后面;与 symbol 相同就不重复,拿不到就没有尾巴(见 _name_suffix)
    suffix = _name_suffix(sym, token_name)
    if suffix:
        parts.append(suffix)
    return SEP.join(parts)


def _style_symbol(sym: str, starred: bool) -> str:
    """
    币名的样式。特别关注的人要更醒目。

    ⚠️ Telegram 的 Bot API **不支持任意文字颜色** —— 允许的标签只有
       b / i / u / s / code / pre / a / blockquote / tg-spoiler,没有 font、没有 style。
       真能出颜色的只有两条:diff 代码块(加号行绿、减号行红)和彩色 emoji;
       而前者是等宽的块级元素,会把标题行整个拆开、还吃掉行内链接。
       所以这里用「加粗 + 方括号」做强调,颜色交给行首的 🌱/🟢/🔴 承担 ——
       那本来就是这条消息的颜色锚点。改样式只需要动这一个函数。
    """
    body = f"${_esc(sym)}"
    return f"<b>【{body}】</b>" if starred else f"<b>{body}</b>"


def _thesis_line(ev: FomoEvent) -> str | None:
    """
    观点正文。<blockquote> 在 TG 里渲染成左侧竖线,与数据行视觉分层。

    ⚠️ 必须**先截断原文再 escape**:反过来的话会把 `&amp;` 从中间劈开,
       残缺实体同样 400。
    """
    text = (ev.thesis_text or "").strip()
    if not text:
        return None
    text = _BLANK_LINES.sub("\n", text)
    if len(text) > THESIS_MAX_CHARS:
        body = _esc(text[:THESIS_MAX_CHARS].rstrip()) + "…"
        return f"<blockquote expandable>{body}</blockquote>"
    return f"<blockquote>{_esc(text)}</blockquote>"


def _amount_line(ev: FomoEvent) -> str | None:
    """
    金额行。三种降级路径(§九 降级矩阵):
      USD 有   → 💰 买入 $2,500.00
      USD 缺   → 💰 买入 1,250,000 TOAD      (显示原生数量,功能 A/B 完全不受影响)
      两者皆缺 → 整行消失
    转账形态额外带上换算:💰 数量 1,200,000 ≈ $19,200.00
    """
    et = ev.event_type
    if et == EVENT_THESIS:
        return None                              # 观点没有金额
    usd = _fmt_usd(ev.amount_usd)
    qty = _fmt_qty(ev.token_amount)
    if usd is None and qty is None:
        return None

    is_transfer = et in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT)
    if is_transfer or ev.side_unknown:
        # 方向不明时也走这套中性文案,免得标题写「交易」而正文写「买入」自相矛盾
        emoji = EMOJI_AMOUNT_IN
        if qty is not None and usd is not None:
            return f"{emoji} {LABEL_AMOUNT_QTY} {qty} ≈ {usd}"
        if qty is not None:
            return f"{emoji} {LABEL_AMOUNT_QTY} {qty}"
        return f"{emoji} {LABEL_AMOUNT_USD} {usd}"

    emoji = EMOJI_AMOUNT_OUT if et == EVENT_SELL else EMOJI_AMOUNT_IN
    label = LABEL_SELL if et == EVENT_SELL else "买入"
    if usd is not None:
        return f"{emoji} {label} {usd}"
    sym = _symbol_plain(ev)
    return f"{emoji} {label} {qty} {_esc(sym)}" if sym else f"{emoji} {label} {qty}"


def _counterparty_line(ev: FomoEvent) -> str | None:
    """转账对手方。名单内转账必须标出来,否则用户无法辨别筹码是不是在名单内搬家(B-9)"""
    if ev.event_type not in (EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT):
        return None
    who = safe_ident((ev.counterparty_handle or "").strip()) or ""
    if not who:
        return None
    # 方向判不出时用中性说法:「来自」/「转给」都在断言方向,断错就是彻底的错误信息
    if ev.side_unknown:
        label = LABEL_COUNTERPARTY
    else:
        label = LABEL_FROM if ev.event_type == EVENT_TRANSFER_IN else LABEL_TO
    line = f"{EMOJI_COUNTERPARTY} {label} {_esc(who)}"
    if ev.counterparty_is_watched:
        line += f" {TEXT_INTERNAL_TRANSFER}"
    return line


def _holding_line(ev: FomoEvent) -> str | None:
    """
    持仓行。⚠️ 判据必须是 `is None` —— holding_usd=0.0 是清仓的真实体现
    (`📦 剩余 $0.00`),不是缺失。
    """
    if ev.holding_usd is None:
        return None
    usd = _fmt_usd(ev.holding_usd)
    if usd is None:
        return None
    if ev.event_type == EVENT_THESIS:
        label = LABEL_HOLDING_THESIS
    elif ev.event_type in (EVENT_SELL, EVENT_TRANSFER_OUT):
        label = LABEL_HOLDING_SELL
    else:
        label = LABEL_HOLDING_BUY
    return f"{EMOJI_HOLDING} {label} {usd}"


def _avg_price_line(ev: FomoEvent) -> str | None:
    """⚠️ 只透传 API 字段,**绝不本地推算**(§10.2 场景 C):均价需要累计买入 token 数量,
    而 token 数量按设计存 TEXT、不做算术。显示一个错的均价比不显示更糟。"""
    p = _fmt_price(ev.avg_price)
    return f"{EMOJI_AVG_PRICE} 均价 {p}" if p is not None else None


def _trade_count_line(ev: FomoEvent) -> str | None:
    """
    ⚠️ 同样只透传(probe #8 未确认语义前 poller 根本不会填这个字段)。
       绝不能拿 user_token_stats.buy_count 顶替:拆单会让它 +N,给出的是错的次数。
    """
    n = ev.api_trade_count
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return f"{EMOJI_TRADE_COUNT} 第 {n} 次交易" if n > 0 else None


def _market_cap_line(ev: FomoEvent) -> str | None:
    mc = _fmt_usd_compact(ev.market_cap)
    return f"{EMOJI_MARKET_CAP} 市值 {mc}" if mc is not None else None


def _launchpad_line(name) -> str | None:
    """
    🚀 发射台 · 「LONG」

    这个币是从哪个平台发出来的。robinhood 上 pons=1163 / LONG=95 / Flap=25…,
    solana 上 Pump.fun=1128 / StonkFun=125…—— 它与市值、币龄同一族,都是**币的出身**。

    ⚠️⚠️ 事实**只从 FOMO 的 launchpadName 来**。拿不到 → 整行消失,
       **绝不用域名反推**:这条路已经被证伪(CASHCAT 的官网是 cashcat.cc、
       AI 的是 artificialinu.com,都是项目自己的站,与发射台无关)。
       猜一个发射台名 = 印一句假事实,比少一行糟得多(§10.3)。
    ⚠️ Pons 的 V1/V2 在**数据层**分(tokeninfo._pons_version),这里给什么画什么
       (铁律 7)。分不出来时数据层给的就是 "Pons",不是 "Pons V2"。
    ⚠️⚠️ 门禁是 **nameguard.safe_launchpad(封闭枚举)**,不是 safe_display ——
       理由见 UNTRUSTED_FIELDS 里那条注释与 nameguard._LAUNCHPADS(实测:
       Pump.fun / o1.exchange / Four.meme 这些最常见的名字本身就是域名形态,
       safe_display 会把 35.6% 的命中打掉)。统一在渲染入口做,这里再做一次是
       刻意保留的第二道(幂等)。
    ⚠️ **不套 `「」` 容器**:返回值只可能是封闭表里那几个规范写法,不可能含分隔符 ——
       与 _exchange_text 同一条理由。这也正好是用户样例里的形态(`🚀 发射台 · LONG`)。
    ⚠️ 表外的名字 → 整行消失 + 一条 DEBUG。那条日志是"上游出了新发射台"的**唯一**
       发现途径,别删:没有它,这张封闭表会悄悄过期。
    """
    text = safe_launchpad(name)
    if text is None:
        if name is not None and str(name).strip():
            logger.debug("发射台名不在封闭表里,那一行不显示(该加进 nameguard._LAUNCHPADS 了) | {}",
                         _flatten(name, _LAUNCHPAD_CHARS))
        return None
    return f"{EMOJI_LAUNCHPAD} {LABEL_LAUNCHPAD}{SEP}{_esc(text)}"


def _token_holders_line(n) -> str | None:
    """
    🧑‍🤝‍🧑 持有人 1,194

    ⚠️⚠️ **0 在这里不可能出现**,因为取值层已经把上游的 0 归成 None
       (tokeninfo.parse_holders)—— 实测 robinhood 上 31 个 holders=0 里 28 个
       是错的(有个显示 0 的实际 4302 人),solana 的 USDC 也是 FOMO=0 / 真值 524 万。
       "0 个持有人"这句话本身就说不出口:同一条消息里紧挨着的是一笔真实成交,
       成交对手手上必然有货。这里再判一次 `n <= 0 → None` 是第二道,不是主判据。
    ⚠️ 千分位:33633 这种数不分位读者要数零。
    ⚠️ 数据来源按链分(robinhood 用 Blockscout、其余用 FOMO),那是数据层的事;
       本模块是纯展示(铁律 7)。
    """
    if n is None or isinstance(n, bool):
        return None
    try:
        v = int(n)
    except (TypeError, ValueError, OverflowError):
        return None
    if v <= 0 or v > _MAX_TOKEN_HOLDERS:
        return None
    return f"{EMOJI_TOKEN_HOLDERS} {LABEL_TOKEN_HOLDERS} {v:,}"


def _socials_line(pairs) -> str | None:
    """
    🔗 官网 · Twitter · Telegram

    发币时项目方自己填的链接(**可能有也可能没有** —— 用户原话)。全都没有 → 整行消失。

    ⚠️⚠️ 链接文字是**我们自己的常量**(SOCIAL_LABELS),永远不用上游给的 label。
       上游的 `websites[].label` 是发币的人填的自由文本,而链接文字带着我们的背书 ——
       让他写"官方客服"再指向自己的站,是这一整块最贵的那个洞。
    ⚠️⚠️ href 里的 URL 已经在渲染入口过完 nameguard.safe_url(封闭 scheme + 封闭
       host 表 + 封闭字符集);这里**仍然**再 escape 一次(quote=True):
       门禁保证了 `"` 进不来,escape 保证了"万一进来了也闭合不了属性" ——
       两道各自独立,不互相依赖。
    ⚠️ 类别不认识的一律跳过(理论上不可能走到:门禁已经按封闭表滤过一遍)。
    """
    parts = []
    for item in pairs or ():
        try:
            kind, url = item
        except (TypeError, ValueError):
            continue
        label = SOCIAL_LABELS.get(kind)
        if label is None or not url:
            continue
        parts.append(f'<a href="{html.escape(str(url), quote=True)}">{label}</a>')
    return f"{EMOJI_LINK} {' · '.join(parts)}" if parts else None


def _pool_quote_line(symbol, name) -> str | None:
    """
    🌊 底池 · WYFI · 「WhiteFiber, Inc.」

    这个币最深的那个池子对面摆的是**哪家公司的股票**。$Rabbit 的底池对着 WYFI
    (代币化的 WhiteFiber 股票),意味着 WYFI 一跌它就跟着跌 —— 与一个对着
    BNB / SOL 的币是**两种风险**。

    ⚠️ `name` 这里收的是**公司/产品名**(WhiteFiber, Inc.),不是上游原文
       "WhiteFiber, Inc. • Robinhood Token" —— 后缀是判据不是信息,剥在数据层
       (dexscreener.notable)。本模块是纯展示,给什么画什么(铁律 7)。
    ⚠️⚠️ 符号与公司名**仍然是第三方字符串**(剥了后缀不等于变干净了),必须 _clip:
       叠平空白 → 限长 → 转义。少了转义,一个 `<` 就让整条消息 400(铁律 4);
       少了限长,一个几千字符的 name 就能把消息顶破预算(实测最长的公司名
       "Space Exploration Technologies Corp. Class A Common Stock" 就有 54 字符)。
    ⚠️ 公司名与符号相同(去掉大小写和空白之后)时**只出符号** ——
       「NVDA · NVDA」是纯粹的重复,占一行却什么都没多说。
    ⚠️ 两个都没有 → 整行消失(铁律 2)。绝不退化成打一个地址:
       这一行的价值在于"对手是**什么东西**",一串 hex 回答不了这个问题。
    ⚠️ 是否显示(对手够不够格占这一行)**不在这里判** —— 那是数据层的判断,
       落在 dexscreener.notable。
    ⚠️⚠️ 对手全名**同样要过形状门禁并套视觉容器**:它曾经是"漏记了门禁"的两个字段之一
       (13 个攻击串原样进过这一行:t.me/scamgroup / tg://resolve / discord.gg/… /
        0x… / base58 长串)。现在门禁在渲染入口统一做(UNTRUSTED_FIELDS),
       这里再做一次是刻意保留的第二道(safe_display 幂等)。
    """
    sym = _clip(symbol, _SIG_SYMBOL_CHARS)
    safe = safe_display(name)
    full = "" if safe is None else _clip(safe, _POOL_NAME_CHARS)
    if sym and full and sym.strip().lower() == full.strip().lower():
        full = ""
    parts = [sym] if sym else []
    if full:
        parts.append(_quoted(full))
    if not parts:
        return None
    return f"{EMOJI_POOL} {LABEL_POOL}{SEP}{SEP.join(parts)}"


def _bare_name(s) -> str:
    """比较用:叠平空白、去掉首尾的 $ 与空白、小写。"""
    return " ".join(str(s or "").split()).strip().strip("$").strip().lower()


def _name_suffix(symbol, name) -> str | None:
    """
    标题尾巴的英文全名:`· $CUM · 「Cummingtonite」`。

    ⚠️⚠️ 外面那对 `「」` 是**视觉容器**(见 QUOTE_OPEN):币名完全攻击者可控,
       不套容器时一个叫 "已清仓 · 亏损 99%" 的币就能在标题里凭空长出两个假字段。

    ⚠️ 与 symbol 相同(忽略大小写、忽略 $ 与首尾空白)→ None,不重复:
       「$WIF · WIF」占了位置什么都没多说。
    ⚠️ 顺序是"叠平空白 → 截 32 → 转义"(_clip),反过来会切开实体。拿不到 → 不加尾巴。
    ⚠️⚠️ **币名是完全攻击者可控的**(DexScreener 的 baseToken.name,谁都能给自己发的币
       起任意名字),所以在这里过一道白名单门禁(safe_display):不合格 → 整段丢弃、
       标题没有尾巴。绝不做"剔掉坏的那部分再显示" —— 剔一半会拼出一个似是而非的假名字。
       ⚠️ 这道门与 namecn 里"送不送去翻译"那道**互相独立**:那道只管省请求,
          这道管的是"显不显示"。少了这道,攻击者给币起个名字就能把内容推进用户的标题。
    """
    safe = safe_display(name)
    if safe is None:
        return None
    if not _bare_name(safe) or _bare_name(safe) == _bare_name(symbol):
        return None
    return _quoted(_clip(safe, _TOKEN_NAME_CHARS))


def _token_zh_line(name, zh) -> str | None:
    """
    📝 「Cummingtonite」 = 「镁铁闪石」

    左半是 A 那个英文全名,右半是它的中文译名(namecn.token_zh)。两者缺一整行消失。
    ⚠️⚠️ 两半**各自**过形状门禁,任一不合格 → 整行消失。
       右半尤其必须自己过一遍:译文来自维基 / Google,而外呼走 fomo_proxy,
       代理能篡改响应 —— "namecn 那边已经过滤过了"不能成为这里跳过的理由。
    ⚠️ 两半**都**套视觉容器:两边都是不可信文本,只套一半就留下一个没有容器的槽位。
    """
    safe_name = safe_display(name)
    # ⚠️ 第二道门同样要带上**原文**:少了它 '特斯拉 xStock' 这类正常译文会被
    #    "含 CJK 时不许有 ≥5 位 ASCII 串"那条毙掉(理由见 nameguard._RE_ASCII_RUN)。
    safe_zh = safe_display(zh, name)
    if safe_name is None or safe_zh is None:
        return None
    left = _clip(safe_name, _TOKEN_NAME_CHARS)
    right = _clip(safe_zh, _TOKEN_ZH_CHARS)
    if not left or not right:
        return None
    return f"{EMOJI_TOKEN_ZH} {_quoted(left)} = {_quoted(right)}"


def _exchange_text(code) -> str | None:
    """
    交易所代码 → "纳斯达克(NasdaqGM)上市";场外市场 → "OTC Markets OTCPK 场外交易";
    映射不到 → "XXX 上市"(只印原代码);没有 → None。

    ⚠️⚠️ 这个字段曾经**全程零门禁**:同一份 Yahoo 响应里 longName / company_zh 都套了
       门禁,唯独 fullExchangeName 漏了 —— 被篡改的代理能把
       `立即访问 t.me/free-airdrop` 原样送进 🏢 行。现在它走的是
       **nameguard.safe_exchange 的封闭枚举**(14 个真实交易所写法,表外的值那一段不显示)。
       ⚠️ 上一版这里的注释还在写"字母开头、只许字母数字空格点横杠、≤20" ——
          那正是被换掉的**旧正则**,它把所有 ≤20 字符的裸域名整段放行了(上一轮的 BLOCKER)。
          注释已改,别再照着旧描述改回模式匹配。
    ⚠️ 返回的是**表里的规范写法**,不是上游原串;大小写也不受上游摆布。
    ⚠️ 不套 `「」` 容器:它已经不是自由文本了(封闭枚举保证里面出不了分隔符),
       而 `纳斯达克(NasdaqGM)上市` 这句本来就是本模块写的固定文案。
    ⚠️⚠️ **场外市场(OTC Markets *)不许印「上市」** —— 那些公司恰恰是**没有**在交易所
       上市的,印「上市」是一句事实错误的话。见 LABEL_OTC。
    """
    c = safe_exchange(code)
    if c is None:
        return None
    c = _flatten(c, _TOKEN_ZH_CHARS)
    if not c:
        return None
    low = c.lower()
    if low.startswith(_OTC_PREFIX):
        return f"{_esc(c)} {LABEL_OTC}"
    zh = "纳斯达克" if low.startswith("nasdaq") else _EXCHANGE_ZH.get(low)
    if zh is None:
        return f"{_esc(c)} {LABEL_LISTED}"
    return f"{zh}({_esc(c)}){LABEL_LISTED}"


def _stock_line(symbol, company_zh, exchange, company_en=None) -> str | None:
    """
    🏢 USAR = 「美国稀土公司」 · 纳斯达克(NasdaqGM)上市
    🏢 USAR · 纳斯达克(NasdaqGM)上市                  ← 公司名翻不出时仍显示交易所

    symbol 就是 🌊 那行的对手符号;事实(交易所)来自 Yahoo、中文名来自 namecn。
    ⚠️ 是否显示(对手是不是币股、Yahoo 查没查到、是不是 EQUITY/ETF)都在数据层判;
       这里只在"符号 + 至少一段内容"都有时才画,否则整行消失。
    ⚠️⚠️ 中文公司名同样过白名单门禁(它是译文,来源与币名一样不可信):
       不合格 → 只剩交易所那半句,绝不把它剔一半印出去。
    """
    sym = _clip(symbol, _SIG_SYMBOL_CHARS)
    if not sym:
        return None
    # ⚠️ company_en 是这段译文的**原文**(Yahoo longName,就是 🌊 那行印的那个),
    #    带上它的理由与 📝 行一样,见 _token_zh_line。
    safe_zh = safe_display(company_zh, company_en)
    zh = "" if safe_zh is None else _clip(safe_zh, _TOKEN_ZH_CHARS)
    ex = _exchange_text(exchange)
    if not zh and not ex:
        return None
    line = f"{EMOJI_STOCK} {sym}"
    if zh:
        line += f" = {_quoted(zh)}"
    if ex:
        line += f"{SEP}{ex}"
    return line


def fmt_token_age(created_at: int | float | None, now: float | None = None) -> str | None:
    """
    币龄:8M / 3H / 5D / 2MO / 1.4Y。拿不到就返回 None(整行消失,铁律 2)。

    ⚠️ 分钟用 M、月份用 MO —— 单独一个 M 在币圈语境里会被读成市值(market cap)。
    ⚠️ 未来时间戳返回 None 而不是负数:宁可不显示,也不能出现「币龄 -3H」。
    ⚠️ 天数以内不做小数(3.7H 没有意义),超过一年才给一位小数 ——
       "1.4Y" 比 "511D" 好读。
    """
    if created_at is None:
        return None
    try:
        age = (time.time() if now is None else now) - float(created_at)
    except (TypeError, ValueError):
        return None
    # ⚠️ 必须显式挡 NaN/Inf:`age < 0` 对 NaN 恒为 False,会一路落到最后一支
    #    渲染成 "nanY" —— 一条一眼假的信息比没有这一行糟得多(铁律 2)。
    if not math.isfinite(age) or age < 0:
        return None
    if age < 3600:
        return f"{max(int(age // 60), 1)}M"
    if age < 86400:
        return f"{int(age // 3600)}H"
    if age < 86400 * 30:
        return f"{int(age // 86400)}D"
    if age < 86400 * 365:
        return f"{int(age // (86400 * 30))}MO"
    return f"{age / (86400 * 365):.1f}Y"


def _token_age_line(ev: FomoEvent) -> str | None:
    """
    🕐 币龄 3H。新币是这个项目最关心的信号,而"多新"只有这一行能回答。

    ⚠️ 数据只来自 balances 里的 token.createdAt。名单里没人持有的币(比如刚清仓的)
       拿不到,那就整行消失 —— 绝不本地推算、也绝不用交易对创建时间凑数
       (后者对老币差几年,见 poller._token_created_at)。
    """
    age = fmt_token_age(ev.token_created_at)
    return f"{EMOJI_TOKEN_AGE} 币龄 {age}" if age else None


def _consensus_line(buyers: int | None, watchlist: int | None, holders: int | None) -> str | None:
    """
    功能 B:👥 名单内 3/12 人买过 · 2 人仍持有

    - buyers / watchlist 任一为 None → 整行消失(共识算不出来时绝不留半句话)
    - holders 为 None → 只有「· N 人仍持有」这一段消失,主指标不受影响(B-5)
    - holders=0 是真实值,照常显示 —— 卖出消息里「0 人仍持有」本身就是强信号
    ⚠️ 文案永远写「买过」而不是「刚刚买入」:分子分母会随 /add /del 跃迁,
       任何时效承诺都会在下一次跃迁时变成假话(B-7)。
    """
    if buyers is None or watchlist is None:
        return None
    try:
        b, w = int(buyers), int(watchlist)
    except (TypeError, ValueError):
        return None
    if w <= 0:
        return None                              # 0/0 没有任何信息量,不如不显示
    line = f"{EMOJI_CONSENSUS} 名单内 {b}/{w} 人买过"
    if holders is None:
        return line
    try:
        h = int(holders)
    except (TypeError, ValueError):
        return line
    return f"{line} · {h} 人仍持有"


def _network_line(ev: FomoEvent) -> str | None:
    net = (ev.network_id or "").strip()
    if not net:
        return None
    # 未命中映射表时原样透传 —— 该值直接来自 API,必须 escape
    return f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}"


def _pnl_line(ev: FomoEvent) -> str | None:
    """
    盈亏行。卖出看**已实现**,其余看**未实现** —— 卖出那一刻真正落袋的是前者。

    ⚠️ 判据一律 `is None`:盈亏正好是 0 是有意义的真实值(刚开仓、或买卖打平)。
    """
    if ev.event_type in (EVENT_SELL, EVENT_TRANSFER_OUT):
        val, pct, label = ev.realized_pnl, ev.realized_pnl_pct, LABEL_PNL_REALIZED
    else:
        val, pct, label = ev.unrealized_pnl, ev.unrealized_pnl_pct, LABEL_PNL_UNREALIZED
    if val is None:
        return None
    usd = _fmt_usd(abs(val))
    if usd is None:
        return None
    emoji = EMOJI_PNL_DOWN if val < 0 else EMOJI_PNL_UP
    sign = "-" if val < 0 else "+"
    line = f"{emoji} {label} {sign}{usd}"
    if pct is not None:
        try:
            line += f" ({float(pct):+.2f}%)"
        except (TypeError, ValueError):
            pass
    return line


def _links_line(ev: FomoEvent) -> str | None:
    """
    快速跳转:FOMO 代币页 + GMGN。

    ⚠️ 必须排在 CA 行**之前** —— CA 独占最后一行是硬规则(§10.3),
       它是中国网络下唯一 100% 可用的操作(tap-to-copy),链接只是锦上添花。
    ⚠️ 链 slug 未收录时对应链接直接不出:GMGN 不支持 Monad / Robinhood,
       硬拼出来只会 404,而错的链接比没有链接更糟。
    """
    ca = (ev.token_address or "").strip()
    net = (ev.network_id or "").strip()
    if not ca or not net:
        return None
    parts = []
    fomo_slug = NETWORK_SLUG.get(net)
    if fomo_slug:
        parts.append(f'<a href="{FOMO_TOKEN_URL.format(slug=fomo_slug, ca=_esc(ca))}">FOMO</a>')
    gmgn_slug = GMGN_SLUG.get(net)
    if gmgn_slug:
        parts.append(f'<a href="{GMGN_TOKEN_URL.format(slug=gmgn_slug, ca=_esc(ca))}">GMGN</a>')
    return f"{EMOJI_LINK} {' · '.join(parts)}" if parts else None


def _ca_line(ev: FomoEvent) -> str | None:
    """
    CA 独占最后一行,纯 <code>(§10.3)。

    ⚠️ 绝不截断:截断了就复制不了,整条消息的实用价值归零,宁可换行。
    ⚠️ 绝不加「CA:」前缀:tap-to-copy 的命中区就是 code 实体覆盖的字符范围,
       整行都是 code 时点哪都能复制。
    ⚠️ 绝不用 <a href> 包裹:TG 内置浏览器在中国网络下大概率白屏,
       那等于把最可靠的操作换成了最不可靠的。
    """
    ca = (ev.token_address or "").strip()
    return f"<code>{_esc(ca)}</code>" if ca else None


# ============================================================
# 对外唯一入口
# ============================================================
@_guard_untrusted
def render(
    ev: FomoEvent,
    buyers: int | None = None,
    watchlist: int | None = None,
    holders: int | None = None,
    baseline_pending: bool = False,
    starred: bool = False,
    pool_quote_symbol: str | None = None,
    pool_quote_name: str | None = None,
    token_name: str | None = None,
    token_name_zh: str | None = None,
    stock_company_zh: str | None = None,
    stock_exchange: str | None = None,
    launchpad: str | None = None,
    token_holders: int | None = None,
    token_socials=None,
) -> str:
    """
    渲染一条 Telegram HTML 消息。

    参数:
        ev               事件(badge 已在落库时判定并冻结,这里只读不判)
        buyers/watchlist 功能 B 主指标;任一为 None → 共识行整段消失
        holders          功能 B 副指标;None → 只掉「N 人仍持有」这一段
        baseline_pending 基线未就绪 → 末尾追加 ⏳ 尾行
        starred          特别关注 → 标题加 ⭐、币名加方括号。**纯展示**,
                         不影响徽章、共识、采集的任何判定
        pool_quote_*     底池对手的符号与**公司/产品名**(见 _pool_quote_line)。
                         ⚠️ 调用方只在对手是**币股**时才传(dexscreener.notable);
                         本模块不做那个判断,也不做任何 IO(铁律 7)。
        token_name       这个币自己的英文全名(dexscreener.token_name)→ 标题尾巴(A)
        token_name_zh    它的中文译名(namecn.token_zh)→ 📝 行(C);None 整行消失
        stock_company_zh / stock_exchange
                         底池对手股票的中文公司名与交易所代码(namecn.stock_info)→ 🏢 行(B)
        launchpad        发射台名(tokeninfo)→ 🚀 行;None 整行消失
        token_holders    这个币的持有人数(tokeninfo)→ 🧑‍🤝‍🧑 行;None 整行消失
        token_socials    ((类别, URL), …)(dexscreener 同一份响应)→ 🔗 社媒行;
                         空/None 整行消失
        ⚠️ 上面这三个**各自独立**:任一拿不到只掉那一行,不影响另外两行,
           更不影响整条推送。

    ⚠️ 本函数**不得抛异常**。它在 poller 的发送循环里被调用,
       一条脏数据把渲染炸掉会连带整个 tick 停摆 —— 宁可发一条降级消息。
    """
    try:
        return _render(ev, buyers, watchlist, holders, baseline_pending, starred,
                       pool_quote_symbol, pool_quote_name,
                       token_name, token_name_zh, stock_company_zh, stock_exchange,
                       launchpad, token_holders, token_socials)
    except Exception as e:  # noqa: BLE001
        # 走到这里一定是本模块的 bug(所有字段级异常都已在下游吃掉),必须留痕
        logger.exception("消息渲染失败,降级为最简文本 | event_id={} | {}", getattr(ev, "event_id", "?"), e)
        return _fallback(ev)


def _render(
    ev: FomoEvent,
    buyers: int | None,
    watchlist: int | None,
    holders: int | None,
    baseline_pending: bool,
    starred: bool = False,
    pool_quote_symbol: str | None = None,
    pool_quote_name: str | None = None,
    token_name: str | None = None,
    token_name_zh: str | None = None,
    stock_company_zh: str | None = None,
    stock_exchange: str | None = None,
    launchpad: str | None = None,
    token_holders: int | None = None,
    token_socials=None,
) -> str:
    # 行序固定,缺失的行整行消失。这个顺序逐条对齐设计文档 §10.2 的七个场景
    candidates = [
        _title_line(ev, starred, token_name),
        _token_zh_line(token_name, token_name_zh),   # 📝 中文名紧跟标题
        _thesis_line(ev),
        _amount_line(ev),
        _counterparty_line(ev),
        # 「转账获得」只对**确定是转入**的记录成立:
        #   转出不存在「被误当成买入」的风险;方向判不出时更不能这么断言
        TEXT_TRANSFER_NOT_BUY
        if (ev.event_type == EVENT_TRANSFER_IN and not ev.side_unknown)
        else None,
        _holding_line(ev),
        _avg_price_line(ev),
        _pnl_line(ev),                           # 卖出看已实现,其余看未实现
        _trade_count_line(ev),
        _market_cap_line(ev),
        _token_age_line(ev),
        # ⚠️ 🚀 与 🧑‍🤝‍🧑 排在这里的理由:它们与市值/币龄是同一族 ——
        #    都在回答"这个币本身是什么样的",而不是"谁在买"。位置与用户给的样例一致。
        _launchpad_line(launchpad),
        _token_holders_line(token_holders),
        # 底池对手排在币本身那几行(市值/币龄)之后、"人"那几行(共识)之前 ——
        # 它回答的是"这个币是什么",不是"谁在买"
        _pool_quote_line(pool_quote_symbol, pool_quote_name),
        _stock_line(pool_quote_symbol, stock_company_zh, stock_exchange,
                    pool_quote_name),                                       # 🏢 紧跟 🌊
        _consensus_line(buyers, watchlist, holders),
        _network_line(ev),
        # ⚠️ 社媒排在平台链接(FOMO/GMGN)**之前**:它们是同一族(都是"点出去"),
        #    而项目自己的链接比平台页更具体。两行都用 🔗 是刻意的 —— 行首 emoji 的
        #    唯一性铁律说的是**标题锚点**,同族的次级行共用一个 emoji 反而好扫。
        _socials_line(token_socials),
        _links_line(ev),                         # 链接在 CA 之前 —— CA 必须独占最后一行
        _ca_line(ev),                            # CA 永远是数据部分的最后一行
        TEXT_BASELINE_PENDING if baseline_pending else None,
    ]
    # 不使用空行分组(铁律 5):这里 join 的是已经过滤掉 None 的行,不会产生空行
    return "\n".join(line for line in candidates if line)


def _fallback(ev: FomoEvent) -> str:
    """渲染彻底失败时的保底消息 —— 信息量最小,但绝不丢消息、绝不 400"""
    try:
        who = _display_name(ev)
        et = _esc(getattr(ev, "event_type", "?") or "?")
        return f"{EMOJI_WARN} <b>{who}</b>{SEP}{et}{SEP}消息渲染异常"
    except Exception:  # noqa: BLE001
        return f"{EMOJI_WARN} 消息渲染异常"


# ============================================================
# 跟单信号
# ============================================================
def render_copy_signal(cand, d, cfg, status: str) -> str:
    """
    跟单信号消息。

    ⚠️ 必须把**入场市值**和**币龄**摆在最显眼的位置:这两个数决定这一单是"早"还是"追高",
       而信号本身("N 个人买了")对两者一无所知 —— 光看人数会把 $4.19M 的追高
       和 $41.9K 的埋伏读成同一件事。
    ⚠️ 纸上模式必须写明「未真实成交」。含糊的措辞会让人以为钱已经出去了。
    """
    sym = _esc((cand.token_symbol or "?").lstrip("$"))
    head = "🧪 <b>纸上跟单</b>" if status == "paper" else "🛒 <b>跟单信号</b>"
    lines = [f"{head} · <b>${sym}</b> · 👥 {cand.buyers} 人买过"]

    seg = []
    if cand.entry_mcap:
        seg.append(f"💎 入场市值 {_fmt_usd_compact(cand.entry_mcap)}")
    age = fmt_token_age(cand.token_created_at)
    if age:
        seg.append(f"{EMOJI_TOKEN_AGE} 币龄 {age}")
    if seg:
        lines.append(" · ".join(seg))

    lines.append(f"💰 跟单金额 ${cfg.amount_usd:,.2f}"
                 + ("(仅记账,<b>未真实成交</b>)" if status == "paper" else ""))
    net = (cand.network_id or "").strip()
    if net:
        lines.append(f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}")
    link = _links_line(_fake_ev(net, cand.token_address))
    if link:
        lines.append(link)
    lines.append(f"<code>{_esc(cand.token_address)}</code>")
    return "\n".join(lines)


def _fake_ev(net: str, ca: str):
    """_links_line 只用到这两个字段 —— 复用它,免得链接拼装出现第二份实现"""
    return FomoEvent(event_id="", event_type=EVENT_BUY, user_id="", event_ts="", raw_json="",
                     network_id=net or None, token_address=ca)


# ============================================================
# 转入告警:N 个名单成员「收到」了同一个币
# ============================================================
# 行首锚点。⚠️ 全局唯一(铁律 1),与买卖那六个、与跟单的 🧪/🛒 都不重样 ——
#    这条消息在聊天列表预览里必须一眼能跟别的区分开:它说的是"有人白拿到了筹码",
#    与"有人买入"含义相反,认错锚点就是把相反的信号读成同一件事。
EMOJI_DISTRIBUTION = "🚨"
# 单条消息的**转义后**字符预算,与 /ca 同一套规矩(见 bot.CA_MSG_BUDGET 的事故记录):
# notifier.send 超限时做的是盲切,切点落在 `&amp;` 中间就是残缺实体 → 整条 400,
# 用户什么都收不到。所以长度必须由渲染方按转义后的真实长度自己算,且只在整行边界停手。
TRANSFER_MSG_BUDGET = MAX_MESSAGE_LEN - 400
# handle / ticker 由陌生人和服务端决定,长度不受任何天然约束 —— 一个 3000 字符的
# ticker 就能把标题撑到顶破预算。先按**转义前**的字符数压一压再转义(顺序见 _clip)。
_SIG_HANDLE_CHARS = 24
_SIG_SYMBOL_CHARS = 16
# CA 也要收口:normalize_token_address 对非 0x/42 位的输入原样透传、不做长度校验,
# 而 CA 是"砍无可砍时也要贴上去"的那一行。Solana 44 位 / EVM 42 位,128 绰绰有余。
_SIG_CA_CHARS = 128
# 发货地址在消息里的显示形态:头 6 位 + 尾 5 位。⚠️ 截短只是为了好读,
# 判定用的永远是完整地址(在 store 里比对);而且地址同样是陌生人可控内容,
# 截短之后仍然要走 _clip(叠平空白 → 限长 → 转义)。
_SIG_ADDR_HEAD = 6
_SIG_ADDR_TAIL = 5
# 消息里最多展开几个收到者。
# ⚠️ 这个上限**只属于展示层**,它曾经漏在 store.transfer_receivers 的 LIMIT 上 ——
#    于是"合计"和台账都只算了这 10 个人,却被摆在全量人数旁边(见 render 里的说明)。
#    查询取全量、这里只决定"列几行",是两件必须分开的事。
#    10 行:名单当前 91 人,全列出来既撑破预算也没人会读;而分发事件里排在最前面的
#    (按到账时间正序)恰恰是最早拿到货的那几个,信息密度最高。
_SIG_RECEIVER_ROWS = 10
# 给"…还有 N 人未显示"那行预留的位置 —— 免得为了塞进这行提示反而把预算顶破。
# 与 bot._CA_OMIT_RESERVE 同一个理由。
_SIG_OMIT_RESERVE = 40
EMOJI_SENDER = "📮"          # 发货地址(转入告警专用)
EMOJI_CLOCK = "⏱"           # 到账时刻


def _clip(s, limit: int) -> str:
    """
    任意短字段 → 单行、限长、**已转义**。

    ⚠️ 顺序必须是"叠平空白 → 截断 → 转义",与 bot._ca_clip 同一条理由:
       反过来会在截断点切断一个 `&amp;`,残缺实体照样让整条消息 400。
       叠平空白也是必需的:一个 handle 里塞几个换行就能把一行变成十行,
       绕开"按行算预算"这个前提。
    """
    return _esc(_flatten(s, limit))


def _flatten(s, limit: int) -> str:
    """
    叠平空白 + 限长,**不转义**。

    ⚠️ 单独拆出来只为一种情况:调用方后面还要把它塞进 `${...}` 之类的模板,
       而那个模板自己会 escape(见 _style_symbol)。先 escape 再 escape 一次,
       `&` 会变成 `&amp;amp;` 显示成一串乱码。除此之外一律用 _clip。
    ⚠️⚠️ 先删 Unicode Cf(格式控制)字符**再**叠平空白:`str.split()` **不吞** Cf ——
       零宽空格(U+200B)能把 `t.me` 拆成 `t.<U+200B>me` 绕开任何形态判断,
       RTL 覆盖(U+202E)能让它后面的字在 Telegram 里反向显示("币<U+202E>pmup"
       看起来就是 "币pump")。这两类字符在符号 / 名字 / handle 里没有任何正当用途。
       ⚠️ 这一步对**所有**走 _clip / _flatten 的字段生效(符号、名字、译名、对手全名…);
          白名单那道(safe_display)只加在名字类字段上,见 _name_suffix。
    """
    flat = " ".join(_no_controls(s).split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat


def _fit_signal(lines: list[str], anchor: str | None) -> str:
    """
    整条信号消息的出口:按**整行边界**砍到预算内,锚点最后贴。

    出口不变式(无论入参是什么都成立):
      1. len(返回值) <= TRANSFER_MSG_BUDGET
      2. CA 锚点是最后一行

    ⚠️ 只按整行砍,绝不切进行内 —— 切在实体中间就是残缺实体、整条 400。
    ⚠️ 锚点最后贴,且它自己已经过 _clip 收口(≤ _SIG_CA_CHARS 转义后)——
       所以"砍到一行不剩 + 贴上锚点"这个最坏情况仍然在预算内。
    ⚠️ anchor 允许为 None:上游拿不到 CA 时(币安 Alpha 名单理论上可能缺 contractAddress),
       宁可少这一行也不能贴一个空的 <code></code> —— 那是个点了复制不出东西的假区域。
    """
    kept = [ln for ln in lines if ln]
    tail = [anchor] if anchor else []
    while kept and len("\n".join([*kept, *tail])) > TRANSFER_MSG_BUDGET:
        kept.pop()
    return "\n".join([*kept, *tail])


def _sig_size(lines: list[str]) -> int:
    """
    这几行拼进消息要占多少字符(含各自那个换行)。

    ⚠️ 算的是**转义后**的真实长度,不是估算:len(x) + 1 逐行加起来,恰好等于
       "\\n".join 之后的长度 + 1。多算的那 1 个字符留给锚点前的换行,宁可保守。
    """
    return sum(len(x) + 1 for x in lines)


def _short_addr(addr) -> str | None:
    """钱包地址 → `8FtY7n…cZx72`(已转义)。空/非字符串返回 None,让那一格消失。"""
    flat = " ".join(str(addr or "").split())
    if not flat:
        return None
    if len(flat) > _SIG_ADDR_HEAD + _SIG_ADDR_TAIL + 1:
        flat = f"{flat[:_SIG_ADDR_HEAD]}…{flat[-_SIG_ADDR_TAIL:]}"
    return _esc(flat)


def _parse_ts(ts) -> float | None:
    """ISO 时间串 → unix 秒。解析不出来返回 None(对应那一格整格消失,绝不拿 0 冒充)。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _fmt_span(sec) -> str | None:
    """
    一段时长 → 「23 秒」「5 分 23 秒」「2 小时 11 分」「1 天 4 小时」。

    ⚠️ 这一格是这条告警里信息量最大的东西之一:「5 分钟内到齐」和「散在 20 小时里」
       是完全不同的信号,前者几乎不可能是三个人各自去买了充进来。
    ⚠️ 负数/NaN 一律 None —— 宁可不显示,也不能出现「-3 分钟内」。
    """
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s) or s < 0:
        return None
    s = int(s)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} 分 {r} 秒" if r else f"{m} 分"
    if s < 86400:
        h, r = divmod(s, 3600)
        m = r // 60
        return f"{h} 小时 {m} 分" if m else f"{h} 小时"
    d, r = divmod(s, 86400)
    h = r // 3600
    return f"{d} 天 {h} 小时" if h else f"{d} 天"


def _sender_line(senders: dict | None) -> str | None:
    """
    发货地址那一行 —— 整条告警里**唯一可证**的证据。

    ⚠️ 为什么必须有这一行:报文里只有 fromAddress/toAddress,**没有 userId**
       (8404 条真实转账里 userId 键出现 0 次)。所以这两件事在数据上一模一样:
         (a) 项目方/内部人在给一群人分发筹码
         (b) 这个人把自己在别处买的币充进 FOMO —— 那其实**就是买入**
       "他们一分钱没花"这种话是在断言别人的意图,数据证不了。
       而"同一个钱包 5 分 23 秒内发给了名单里三个人"是地址比对出来的事实,
       它不能证明 (a),但把天平明显压向 (a),而且经得起追问。
    ⚠️ 三种情况必须分开说,一句都不能串:
         有聚类   → 点名几个人、多长时间、哪个地址
         无聚类   → 只说"地址各不相同",绝不因此就不告警(聚类是加强证据,不是触发条件)
         查不到   → 整行消失(老库没有这一列、或上游没给地址),绝不说"没有共同发货方"
    """
    if not senders or not senders.get("known"):
        return None
    top = senders.get("top") or {}
    n = top.get("receivers") or 0
    if n >= 2:
        addr = _short_addr(top.get("address"))
        if addr is None:
            return None
        line = f"{EMOJI_SENDER} 其中 <b>{n} 人</b>的币来自<b>同一个发货地址</b>{SEP}{addr}"
        first, last = _parse_ts(top.get("first_ts")), _parse_ts(top.get("last_ts"))
        span = _fmt_span(last - first) if (first is not None and last is not None) else None
        if span is not None:
            line += f"{SEP}前后 {span}"
        return line
    known = senders["known"]
    if known >= 2 and senders.get("distinct") == known:
        return f"{EMOJI_SENDER} 这 {known} 人的发货地址{SEP}<b>各不相同</b>"
    return None


def _receiver_row(r: dict, now: float | None) -> str:
    """一个收到者一行。缺哪个键就少哪一格(铁律 2:缺失整格消失,绝不打 0 / N/A)。"""
    # ⚠️ who 是名单成员的 handle,同样过形态轻门禁;命中 → 那一格显示"未知用户"
    #    (这一行的主体是金额与市值,人名缺了不该把整行带走)。
    cells = [f"{EMOJI_COUNTERPARTY} {_clip(safe_ident(r.get('who')) or TEXT_UNKNOWN_USER, _SIG_HANDLE_CHARS)}"]
    usd = _fmt_usd(r.get("usd"))
    if usd is not None:
        cells.append(usd)
    mc = _fmt_usd_compact(r.get("mcap"))
    if mc is not None:
        # 「收到时」这个说法只有在市值确实取自那个时刻才成立 ——
        # poller 用新鲜度闸门保证了这一点,取不到就是 None,这一格直接不出现
        cells.append(f"{EMOJI_MARKET_CAP} 收到时 {mc}")
    hits = r.get("hits")
    if hits is not None and hits > 1:
        cells.append(f"{EMOJI_TRADE_COUNT} {hits} 笔")
    # 各自什么时候到的账。⚠️「5 分钟内到齐」和「散落在 20 小时里」是完全不同的信号,
    #    只报人数等于把这个差别抹平。取不到时间就整格消失,不拿"刚刚"凑数。
    got_at = _parse_ts(r.get("ts"))
    ago = None if got_at is None else _fmt_span(
        (time.time() if now is None else now) - got_at)
    if ago is not None:
        cells.append(f"{EMOJI_CLOCK} {ago}前")
    return SEP.join(cells)


def _receiver_rows(receivers: list[dict], now: float | None,
                   room: int) -> tuple[list[str], int]:
    """
    收到者明细 → (要渲染的行, 真正渲染出来的行数)。**截断在这里,不在 SQL 里。**

    ⚠️ 两道闸各管一件事,少哪道都不行:
      1. `_SIG_RECEIVER_ROWS` —— 名单 91 人时不能真往消息里塞 91 行。
      2. `room` —— handle 由陌生人决定、_esc 会把 `'` 撑成 6 个字符,
         10 行也可能吃光预算。行数够少不等于字符够少。
    ⚠️ 装不下的那一行必须 `continue` 而不是 `break`:break 会让一个长 handle 把排在
       它**后面**、本来完全塞得下的短行全部连带丢掉 —— 一个人名字长,后面所有人就都
       消失了。(与 bot._ca_assemble 同一条教训。)
    ⚠️ 只在**整行边界**上停手,绝不切进行内:切在 `&amp;` 中间就是残缺实体 → 整条 400。
    ⚠️ 返回 shown 而不是让调用方去数:"还有 N 人未显示"的 N 必须扣掉**真正渲染出来的**
       行数,被 room 跳过的那几行也算未显示 —— 拿 _SIG_RECEIVER_ROWS 去减就会少报。
    """
    body: list[str] = []
    used = 0
    shown = 0
    for r in receivers[:_SIG_RECEIVER_ROWS]:
        line = _receiver_row(r, now)
        cost = len(line) + 1
        if used + cost > room:
            continue
        body.append(line)
        used += cost
        shown += 1
    return body, shown


@_guard_untrusted
def render_transfer_in_signal(
    *,
    network_id: str | None,
    token_address: str,
    token_symbol: str | None,
    receiver_count: int,
    receivers: list[dict],
    window_hours: int,
    buyers: list[str] | None = None,
    senders: dict | None = None,
    now: float | None = None,
    token_name: str | None = None,
    token_name_zh: str | None = None,
    launchpad: str | None = None,
    token_holders: int | None = None,
    token_socials=None,
) -> str:
    """
    「同一个币被 N 个名单成员『收到』」的告警。

    参数:
        receiver_count  窗口内合格收到者总人数。与 receivers 必须是**同一批人**
                        (poller 那边分别来自 count_recent_receivers 与 transfer_receivers,
                         两处谓词漂移时会打 WARNING)。
        receivers       窗口内**全部**收到者,一人一条,按到账时间正序:
                        [{"who": handle, "usd": 到账美元, "mcap": 收到时市值,
                          "hits": 笔数, "ts": 最早到账时刻 ISO}, …]
                        缺哪个键就少哪一格(铁律 2:缺失整格消失,绝不打 0 / N/A)
                        ⚠️ 调用方**不要**替本函数截断:合计要对全量求和,而"消息里列几行"
                           是展示层的事,由 _receiver_rows 负责,末尾如实写"还有 N 人未显示"。
        buyers          名单里**真金白银买过**这个币的人。
                        ⚠️ None 与 [] 语义不同:None = 查不出来(那一行整行消失),
                           [] = 查过了、确实没人买过 —— 后者是有价值的信息,要显示。
        senders         发货地址聚类,见 store.transfer_senders / 本模块 _sender_line。
                        None = 查不到地址 → 那一行整行消失。
        now             渲染时刻(unix 秒),只用来算"多久之前到账"。默认取当前时间。

    ⚠️ 这条消息的第一职责是**让人一眼看出这不是在 FOMO 上买的**:「收到」= 从外部钱包
       转进来,与"他自己在 FOMO 上掏钱买"是两回事,不能扫一眼读成"三个人在抢这个币"。
    ⚠️⚠️ 但**到此为止**,再往前一步就是编。这里曾经写着「他们一分钱没花」——
       那句话数据证不了:报文里只有 fromAddress/toAddress,**没有 userId**
       (8404 条真实转账里 userId 键出现 0 次),所以下面两件事完全无法区分:
         (a) 项目方/内部人在分发筹码        ← 用户关心的
         (b) 本人把在 Jupiter/OKX 买的币充进 FOMO ← 这恰恰**是**花了钱的买入
       说"一分钱没花"就是在替别人断言意图,与刚在 /ca 修掉的那类假事实同级。
       现在只说数据能证明的:不是在 FOMO 上买的、几个人、什么时候到的、
       是不是同一个发货地址(见 _sender_line —— 那才是真正有力的那条证据)。
    ⚠️ 本函数是纯函数,不查库(铁律 7)。所有事实由 poller 取好传进来。
    """
    sym = _clip((token_symbol or "").lstrip("$"), _SIG_SYMBOL_CHARS)
    net = (network_id or "").strip()

    title = (f"{EMOJI_DISTRIBUTION} <b>筹码分发预警</b>{SEP}"
             f"<b>{receiver_count} 人「收到」同一个币</b>")
    if sym:
        title += f"{SEP}<b>${sym}</b>"
    # 英文全名 / 中文名:与买入推送同一套(A / C)。调用方只给缓存里有的,没有就没有
    suffix = _name_suffix(token_symbol, token_name)
    if suffix:
        title += f"{SEP}{suffix}"
    head_lines = [
        title,
        f"{EMOJI_TRANSFER_IN} <b>不是在 FOMO 上买的</b> —— 币是从外部钱包转进来的",
    ]
    zh_line = _token_zh_line(token_name, token_name_zh)
    if zh_line is not None:
        head_lines.insert(1, zh_line)     # 📝 紧跟标题

    # ⚠️⚠️ 合计对 **receivers 全量**求和,不是对下面渲染得出的那几行 ——
    #    它旁边写的是 receiver_count(全量人数),两个数字必须是同一批人。
    #    这里曾经是对的、而 poller 传进来的 receivers 被 SQL 的 LIMIT 砍到 10 行,
    #    于是「25 人收到 · 合计 $X」里的 X 只是其中 10 个人的合计。
    #    截断是**这个函数**的职责(见下面的 _SIG_RECEIVER_ROWS),调用方必须给全量。
    # ⚠️ 判空一律 is None(模块铁律):一个人也没报出金额时 total 是 None、那一段消失;
    #    而 $0.00 是有意义的真实值,不能被 `if total` 连同 None 一起吞掉。
    priced = [r["usd"] for r in receivers if r.get("usd") is not None]
    total_str = _fmt_usd(sum(priced)) if priced else None
    head = f"{EMOJI_CONSENSUS} 最近 {window_hours} 小时内 {receiver_count} 人收到"
    if total_str is not None:
        head += f"{SEP}合计 {total_str}"
    head_lines.append(head)

    # 尾段先算出来:收到者能占多少地方,取决于尾段吃掉多少 ——
    # 而且尾段(发货地址证据 / 买家对照 / 链接 / CA)一行都不能被收到者挤掉。
    tail_lines: list[str] = []
    # 发货地址聚类 —— 这条告警里唯一可证的硬证据,见 _sender_line 的说明
    sender_line = _sender_line(senders)
    if sender_line is not None:
        tail_lines.append(sender_line)
    # 有没有人真金白银买过 —— 有对比才有判断力
    if buyers is not None:
        if buyers:
            who = "、".join(_clip(b, _SIG_HANDLE_CHARS)
                            for b in buyers if safe_ident(b) is not None)
            tail_lines.append(f"{EMOJI_AMOUNT_IN} 名单里另有 {len(buyers)} 人"
                              f"<b>真金白银</b>买过:{who}")
        else:
            tail_lines.append(f"{EMOJI_AMOUNT_IN} 名单里<b>还没有人</b>真金白银买过这个币")
    # 🚀 / 🧑‍🤝‍🧑:与买卖推送同一族。⚠️ 这条路径**只读缓存**(poller 传 cached_only=True),
    #    缓存里没有就没有 —— 分发预警绝不为这两行新开请求。
    for extra in (_launchpad_line(launchpad), _token_holders_line(token_holders)):
        if extra is not None:
            tail_lines.append(extra)
    if net:
        tail_lines.append(f"{EMOJI_NETWORK} {_esc(NETWORK_DISPLAY.get(net, net))}")
    social = _socials_line(token_socials)
    if social:
        tail_lines.append(social)
    link = _links_line(_fake_ev(net, token_address))
    if link:
        tail_lines.append(link)

    # CA 独占最后一行、纯 <code>(铁律 6)
    anchor = f"<code>{_clip(token_address, _SIG_CA_CHARS)}</code>"
    room = (TRANSFER_MSG_BUDGET - _sig_size(head_lines) - _sig_size(tail_lines)
            - len(anchor) - _SIG_OMIT_RESERVE)
    body, shown = _receiver_rows(receivers, now, room)

    lines = [*head_lines, *body]
    # 未显示 = 全量人数 - **真正渲染出来的**行数。被 room 跳过的和超出
    # _SIG_RECEIVER_ROWS 的都算在里面,shown 只在真正 append 之后才自增。
    omitted = receiver_count - shown
    if omitted > 0:
        lines.append(f"…还有 {omitted} 人未显示")
    lines.extend(tail_lines)
    return _fit_signal(lines, anchor)


# ============================================================
# 指定用户的转入 —— 逐条推送(/tin)
# ============================================================
# 「到账时刻」那一格用相对时间。⚠️ 绝对时刻要带时区才不会被读错,而这条消息的
#    唯一用途是"他刚刚拿到货,我还来不来得及",相对时间直接回答这个问题。
def _watch_ago_line(ev: FomoEvent, now: float | None) -> str | None:
    got_at = _parse_ts(ev.event_ts)
    if got_at is None:
        return None
    span = _fmt_span((time.time() if now is None else now) - got_at)
    return None if span is None else f"{EMOJI_CLOCK} {span}前到账"


# 「收到时市值」这个**时点断言**最多容忍这笔转账有多旧(秒)。
# ⚠️ poller 那道闸(_TRANSFER_MCAP_FRESH_SEC = 900)管的是另一件事:900 秒是为了
#    把首轮/停机后一次性吃进来的历史转账(单页 25 条,最远十几小时)挡在外面,
#    对**聚合告警**那条路径够用。但它填进来的值是"本轮 balances 观测到的市值",
#    在 900 秒这个宽度下,「收到时」这三个字最坏会比数据真实含义早 15 分钟 ——
#    对 memecoin 来说 15 分钟能走出几倍。**标签不能比数据强**,所以逐条推送这条
#    路径自己再收一道,超了就整行消失(铁律:缺失字段整行消失,绝不降级成弱说法)。
# ⚠️ 120s 的依据(本地库 read-only 实测,2026-08-30):
#    拿"每轮都拉"的那条流(BUY,swaps 每 tick 一次)当 /tin 的同构参照,
#    剔掉停机补数那一段(滞后 > 900s)后 n=5762:p50 29s · p75 51s · p90 98s · p95 248s。
#    即 120s 覆盖约 91.5% 的推送,而把标签的最坏误差从 15 分钟压到 2 分钟。
#    再放宽到 300s 只多捞 4.4%,却让误差回到 5 分钟 —— 不值。
_WATCH_MCAP_MAX_AGE_SEC = 120


def _watch_mcap_line(ev: FomoEvent, now: float | None) -> str | None:
    """
    「收到时」的市值。

    ⚠️⚠️ 这一格与 _market_cap_line(「💎 市值 X」)**不是一回事,不许合并**:
       那一格说的是"现在多少",这一格说的是"他拿到货的那一刻多少" ——
       对这条消息来说后者才是能用来判断早晚的那个数。
       poller 只在这笔转账足够新时才把本轮观测到的市值填进 ev.market_cap
       (见 poller._TRANSFER_MCAP_FRESH_SEC),所以有值时这个说法才**可能**成立;
       取不到就整行消失,**绝不拿"现在的市值"冒充"收到时的市值"**。
    ⚠️ 渲染时刻距到账越久,"本轮观测值 = 收到时的值"这个等号就越站不住。
       这里用**渲染时的账龄**再兜一道:它是"观测时账龄"的上界(观测必然发生在
       渲染之前),所以这道门只会误杀、不会放行不该放行的 —— 正是该偏的那一侧。
       代价是补发路径(推送失败后最长补 10 分钟)会丢掉这一格,认了。
    ⚠️ 只卡上界:账龄为小负数是上游时钟偏移(实测 min = -22s),那种情况恰恰是
       "刚刚到账",不该因为差了几秒就把这一格抹掉。
    """
    mc = _fmt_usd_compact(ev.market_cap)
    if mc is None:
        return None
    got_at = _parse_ts(ev.event_ts)
    if got_at is None:
        # 连到账时刻都读不出来,就无从证明这个市值是"那一刻"的
        return None
    if (time.time() if now is None else now) - got_at > _WATCH_MCAP_MAX_AGE_SEC:
        return None
    return f"{EMOJI_MARKET_CAP} 收到时市值 {mc}"


def _watch_sender_line(ev: FomoEvent) -> str | None:
    """发货地址。截短只为好读,判定用的永远是完整地址(在 store 里比对)。"""
    addr = _short_addr(ev.counterparty_address)
    return None if addr is None else f"{EMOJI_SENDER} 发货地址 {addr}"


@_guard_untrusted
def render_transfer_in_watch(ev: FomoEvent, *, starred: bool = False,
                             now: float | None = None,
                             token_name: str | None = None,
                             token_name_zh: str | None = None,
                             launchpad: str | None = None,
                             token_holders: int | None = None,
                             token_socials=None) -> str:
    """
    被 /tin 点名的人**收到**了一笔币 —— 逐条推送。

    ⚠️⚠️ **措辞铁律:只摆可证的事实,一个字都不许替用户下结论。**
       这个功能存在的理由是"有些人在别处成交,币是转进来的,对他们来说收到就是进货"。
       但报文里只有 fromAddress/toAddress(8404 条真实转账里 userId 键出现 0 次),
       **没有任何证据**表明这笔转账来自哪个工具、是不是买入、花没花钱。
       所以这里只写:谁、什么币、多少枚、多少美元、收到时市值、从哪个地址来、多久之前。
       "他在 XX 上买的" / "他又建仓了" / "一分钱没花" 这类话全部禁止 ——
       用户自己知道这个人用什么工具,他读得出来;替他断言就是编。
       (本项目已经因为「他们一分钱没花」这种断言被审查打回过一次。)
    ⚠️ 也**刻意不带** render() 里那句「⚠️ 转账获得,非市场买入」:那句话在
       "N 人收到同一个币"的语境里是在防误读,而在这里它恰恰是反方向的断言 ——
       这笔转账很可能**就是**他在别处付了钱的买入。两边都不说,才是数据支持的位置。
    ⚠️ 出口不变式与 /ca、与分发预警同一套:≤ TRANSFER_MSG_BUDGET、只在整行边界砍、
       CA 锚点最后贴且完整(见 _fit_signal)。handle / ticker 是陌生人可控的
       任意长文本,先 _clip 收口再进模板,否则一个超长昵称就能把整条消息挤没。
    ⚠️ 本函数是纯函数,不查库(铁律 7);缺失的字段整行消失,绝不打 0 / N/A。
    """
    emoji, label = _title_anchor(ev)
    mark = f"{STAR_MARK} " if starred else ""
    # ⚠️ 与 _display_name 同一条口径:handle 过形态轻门禁,命中就退回 user_id。
    name = (safe_ident((ev.handle or "").strip()) or "") or (ev.user_id or "").strip()
    head = f"{emoji} {mark}<b>{_clip(name, _SIG_HANDLE_CHARS) if name else TEXT_UNKNOWN_USER}</b>"
    h = safe_ident((ev.user_handle or "").strip().lstrip("@")) or ""
    # @handle 与展示名相同时不重复显示(有人没设展示名,handle 会被当展示名用)
    if h and h.lower() != name.lower():
        head += f" (@{_clip(h, _SIG_HANDLE_CHARS)})"
    parts = [head, label]
    sym = _symbol_plain(ev)
    if sym is not None:
        # ⚠️ 传**未转义**的截断结果:_style_symbol 自己会 escape,先转义会变成 &amp;amp;
        parts.append(_style_symbol(_flatten(sym, _SIG_SYMBOL_CHARS), starred))
    # 英文全名 / 中文名:与买入推送同一套(A / C)。调用方只给缓存里有的,没有就没有
    suffix = _name_suffix(sym, token_name)
    if suffix:
        parts.append(suffix)

    lines = [
        SEP.join(parts),
        _token_zh_line(token_name, token_name_zh),   # 📝 中文名紧跟标题
        _amount_line(ev),            # 💰 数量 12,000,000 ≈ $2,439.09
        _watch_mcap_line(ev, now),   # 💎 收到时市值 $198.2K(太旧就整行消失)
        # 🚀 / 🧑‍🤝‍🧑:与买卖推送同一族、同一位置。⚠️ 这条路径**只读缓存**
        # (poller 传 cached_only=True),缓存里没有就没有 —— 不为它新开请求
        _launchpad_line(launchpad),
        _token_holders_line(token_holders),
        _counterparty_line(ev),      # 👤 来自 someone(名单内转账会自己标出来)
        _watch_sender_line(ev),      # 📮 发货地址 8FtY7n…cZx72
        _watch_ago_line(ev, now),    # ⏱ 3 分 20 秒前到账
        _network_line(ev),           # 🧬 Solana
        _socials_line(token_socials),  # 🔗 官网 · Twitter(排在平台链接之前)
        _links_line(ev),             # 🔗 FOMO · GMGN(必须排在 CA 之前)
    ]
    anchor = f"<code>{_clip(ev.token_address, _SIG_CA_CHARS)}</code>" if ev.token_address else None
    return _fit_signal([ln for ln in lines if ln], anchor)


# ============================================================
# 币安 Alpha 新上架
# ============================================================
# 行首锚点。⚠️ 全局唯一(铁律 1):与买卖那六个、跟单的 🧪/🛒、分发预警的 🚨 都不重样。
#    这条消息说的是"币安上了个新币",与"名单里谁买了"没有任何关系 ——
#    在聊天列表预览里必须一眼分得开,认错锚点就是把两类完全不同的事读成一件。
EMOJI_ALPHA = "🆕"
EMOJI_SECTOR = "🏷"          # 板块标注(Alpha 专用)
LABEL_ALPHA = "币安 Alpha 新上架"
# 板块名来自 .env(自己人写的),但仍然限长 —— 配置写错不该把消息顶破预算
_ALPHA_SECTOR_CHARS = 24
# name / symbol 来自链上,是**陌生人可控的任意字符串**:必须限长 + 转义,且只转一次
_ALPHA_NAME_CHARS = 32
# 汇总消息里最多列几个符号
_ALPHA_BATCH_SYMBOLS = 30


def _fmt_count(v) -> str | None:
    """
    人数/个数 → "48,872"。取不到返回 None(整行消失,铁律 2)。

    ⚠️ 0 是有意义的真实值(刚上架确实可能一个持有人都没有),照常显示 ——
       判空一律 is None,绝不用真值判断。
    """
    d = _to_decimal(v)
    if d is None:
        return None
    try:
        n = int(d)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if n < 0:
        return None                              # 负数持有人一眼是脏数据,宁可不显示
    return f"{n:,}"


def _alpha_listing_line(listing_time_ms, now: float | None = None) -> str | None:
    """
    🕐 上架 2026-08-26 10:00 UTC · 3H前

    ⚠️ 绝对时刻必须有 —— "3H前"在一条隔夜才看到的消息里是错的,而绝对时刻永远对。
    ⚠️ 名单里会出现**还没到上架时刻**的币(实测 DEBIT 在 10:00 前就已经在名单里),
       这时 fmt_token_age 返回 None(它拒绝渲染负数币龄),改说「即将上架」——
       那是这条消息此刻最重要的一句话,不能因为算不出"多久之前"就整格消失。
    """
    ms = listing_time_ms
    if ms is None or isinstance(ms, bool):
        return None
    try:
        secs = float(ms) / 1000.0
        stamp = datetime.fromtimestamp(secs, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    line = f"{EMOJI_TOKEN_AGE} 上架 {stamp}"
    ref = time.time() if now is None else now
    if secs > ref:
        return f"{line}{SEP}即将上架"
    age = fmt_token_age(secs, now)
    return f"{line}{SEP}{age}前" if age else line


@_guard_untrusted
def render_alpha_listing(
    *,
    symbol: str | None,
    listing_time_ms,
    name: str | None = None,
    network_id: str | None = None,
    chain_name: str | None = None,
    contract_address: str | None = None,
    market_cap=None,
    holders=None,
    sector: str | None = None,
    now: float | None = None,
) -> str:
    """
    「币安 Alpha 上了个新币」的推送。

    参数:
        symbol           链上文本,陌生人可控 —— 走 safe_ident(轻门禁)+ _clip
        name             链上币名,陌生人可控 —— 走 **safe_display**(形状门禁)并套 `「」`
        network_id       已归一化的链标识(models.normalize_network 的输出),用来查展示名与链接
        chain_name       币安给的原始链名,只在 network_id 查不到展示名时兜底
        sector           板块标注,None = 没命中或板块拉取失败 → **那一行整行消失**,
                         绝不打 "N/A" / "未知板块"(铁律 2)
        now              渲染时刻(unix 秒),只用来算"多久之前上架"

    ⚠️⚠️ **`name` 本轮(G3)接进收口**。上一版它是"已审查、只走 _clip"的:
       既不过门禁也不套容器,直接拼在标题行的 SEP 之后 ——
           render_alpha_listing(symbol='CUM', name='已清仓 · 亏损 99%')
             → 🆕 <b>币安 Alpha 新上架</b> · <b>$CUM</b> · 已清仓 · 亏损 99%
           render_alpha_listing(name='Join t.me/pumpgroup now')
             → 🆕 … · <b>$CUM</b> · Join t.me/pumpgroup now
       它与 token_name 是**同一类东西**(谁都能给自己发的币起名),却是笛卡尔积里
       唯一一个没有门禁也没有容器的名字类槽位。现在与其它名字类槽位一致:
       safe_display(不合格整段丢弃,标题就没有这一段)+ `「」` 视觉容器。
    ⚠️ 板块是**本地配置**里的显示名(config.alpha_sectors,运维自己写在 .env 里),
       不是币安给的文本;它只是这条消息里的一行标注,没有它这条消息照样成立。
       主干(名单新增)与标注(板块)的地位不对等,不要写成"拿不到板块就不推"。
    ⚠️ 本函数是纯函数,不查库、不发请求(铁律 7)。
    """
    sym = _clip((symbol or "").lstrip("$"), _SIG_SYMBOL_CHARS)
    title = f"{EMOJI_ALPHA} <b>{LABEL_ALPHA}</b>"
    if sym:
        title += f"{SEP}<b>${sym}</b>"
    # 全名与符号相同时不重复("牛来 · 牛来"只是噪音)。比对用**转义前**的原文
    # ⚠️ name 走到这里时已经过完 safe_display(叠平且合格),不合格的是 None。
    raw_name = " ".join(str(name or "").split())
    raw_sym = " ".join(str(symbol or "").lstrip("$").split())
    if raw_name and raw_name.casefold() != raw_sym.casefold():
        title += f"{SEP}{_quoted(_clip(raw_name, _ALPHA_NAME_CHARS))}"

    lines = [title]
    if sector is not None:
        lines.append(f"{EMOJI_SECTOR} 板块{SEP}<b>{_clip(sector, _ALPHA_SECTOR_CHARS)}</b>")

    mc = _fmt_usd_compact(market_cap)
    if mc is not None:
        lines.append(f"{EMOJI_MARKET_CAP} 市值 {mc}")
    hold = _fmt_count(holders)
    if hold is not None:
        lines.append(f"{EMOJI_CONSENSUS} 持有人 {hold}")
    ts_line = _alpha_listing_line(listing_time_ms, now)
    if ts_line is not None:
        lines.append(ts_line)

    net = (network_id or "").strip()
    # 链展示名:先查内部映射表,查不到才用币安给的原始链名(那是 API 文本,必须转义)
    chain_disp = NETWORK_DISPLAY.get(net) or " ".join(str(chain_name or "").split()) or None
    if chain_disp:
        lines.append(f"{EMOJI_NETWORK} {_esc(chain_disp)}")
    link = _links_line(_fake_ev(net, contract_address or ""))
    if link:
        lines.append(link)

    ca = " ".join(str(contract_address or "").split())
    # CA 独占最后一行、纯 <code>(铁律 6)。拿不到就没有这一行 —— 空的 <code></code>
    # 是个点了复制不出东西的假区域,比没有更糟
    anchor = f"<code>{_clip(ca, _SIG_CA_CHARS)}</code>" if ca else None
    return _fit_signal(lines, anchor)


def render_alpha_batch(items: list[tuple[str | None, int]]) -> str:
    """
    一轮新增太多时的汇总消息(见 binance_alpha.MAX_PUSH_PER_ROUND)。

    ⚠️ 这条消息存在的唯一理由是**别把 Telegram 打爆**:上游若把一批老币的
       listingTime 重写成今天,逐条推就是几百条,用户当场静音,这个功能就死了。
       所以它必须自己说清楚"这不正常",而不是假装一切正常地报一个大数字。
    ⚠️ items 里的符号同样是陌生人可控文本,一律 _clip。
    """
    n = len(items)
    lines = [
        f"{EMOJI_ALPHA} <b>{LABEL_ALPHA}</b>{SEP}<b>一次新增 {n} 个</b>",
        f"{EMOJI_WARN} 数量异常(多半是上游重写了上架时间),已跳过逐条推送",
    ]
    syms = [f"${_clip((s or '').lstrip('$'), _SIG_SYMBOL_CHARS)}"
            for s, _ in items[:_ALPHA_BATCH_SYMBOLS] if s]
    if syms:
        lines.append(SEP.join(syms))
    if n > _ALPHA_BATCH_SYMBOLS:
        lines.append(f"…还有 {n - _ALPHA_BATCH_SYMBOLS} 个未列出")
    return _fit_signal(lines, None)


# ============================================================
# pump.fun 指定用户的逐笔成交
# ============================================================
# 行首锚点。⚠️ 全局唯一(铁律 1):买卖那六个、跟单的 🧪/🛒、分发预警的 🚨、
#    Alpha 的 🆕 都不重样。这条消息说的是"我盯的这个人在 pump.fun 上成交了",
#    在聊天列表预览里必须一眼与 FOMO 那边的买卖分得开 —— 认错锚点就是认错平台。
EMOJI_PUMP_BUY = "🟩"
EMOJI_PUMP_SELL = "🟥"
LABEL_PUMP = "pump.fun"
LABEL_PUMP_BUY = "买入"
LABEL_PUMP_SELL = "卖出"
EMOJI_PUMP_TX = "🧾"          # 成交签名
# 用户名来自 pump.fun,是**本人可改的任意字符串**:限长 + 转义,且只转一次
_PUMP_NAME_CHARS = 24
# 签名:Solana base58 88 位 / EVM 0x+64 = 66 位。128 绰绰有余,又挡得住脏数据撑爆预算
_PUMP_TX_CHARS = 128
# 清仓措辞。⚠️ 与 bot._CA_CLOSED_MARK 同一句话、同一条理由:清仓之后再报
#    「📦 持仓 $0.00 / 📈 未实现盈亏 +$0.00」,读者只会读成"他空仓且不赚不亏"——
#    前半句对、后半句是**凭空断言的假事实**,而真正落袋的那笔在已实现里。
LABEL_PUMP_CLEARED = "已清仓"
# 「距最高 -X%」。ath_market_cap 与 usd_market_cap 同为**美元**(见 pumpfun.parse_coin
# 里那段实测推导),所以两者可以直接相比。
LABEL_PUMP_DRAWDOWN = "距最高"


@_guard_untrusted
def render_pump_trade(
    *,
    username: str | None,
    side: str | None,
    token_symbol: str | None,
    coin_mint: str | None,
    amount_usd=None,
    price_usd=None,
    holding_usd=None,
    is_cleared: bool = False,
    unrealized_pnl_usd=None,
    unrealized_pnl_pct=None,
    realized_pnl_usd=None,
    realized_pnl_pct=None,
    market_cap_usd=None,
    ath_market_cap_usd=None,
    holders_in_list: int | None = None,
    traded_at: str | None = None,
    network_id: str | None = None,
    chain_display: str | None = None,
    tx: str | None = None,
    now: float | None = None,
    pool_quote_symbol: str | None = None,
    pool_quote_name: str | None = None,
    token_name: str | None = None,
    token_name_zh: str | None = None,
    stock_company_zh: str | None = None,
    stock_exchange: str | None = None,
    launchpad: str | None = None,
    token_holders: int | None = None,
    token_socials=None,
) -> str:
    """
    「被盯的人在 pump.fun 上成交了一笔」的推送。

    参数:
        username       pump 用户名,**本人可控** —— 一律走 _clip(叠平空白→限长→转义)
        side           "buy" / "sell"。其它值(含 None)→ 退化成中性的「交易」,
                       **绝不猜方向** —— 猜错方向比不说方向糟得多
        token_symbol   链上文本,同样陌生人可控
        holding_usd    成交后还剩多少美元的仓位(portfolio 的 valueUsd)
        is_cleared     已清仓(isExited / amountHeld=0)。为真时持仓行改说「已清仓」,
                       盈亏行改看**已实现** —— 见 LABEL_PUMP_CLEARED
        unrealized_*   未实现盈亏与百分比;**只在还持有时使用**
        realized_*     已实现盈亏与百分比;**只在已清仓时使用**
        market_cap_usd 当前市值(美元)。⚠️ 必须是 coins-v3 的 `usd_market_cap`,
                       **绝不是 `market_cap`** —— 后者在 Solana 上是 SOL 计价
        ath_market_cap_usd 历史最高市值(美元),只用来算「距最高 -X%」
        holders_in_list 名单里仍持有这个币的人数,而且是**本轮亲眼观测到的下界**
                       (调用方绝不能把库里的旧快照混进来);**只报分子不报分母**,
                       渲染出来带「至少」二字 —— 理由见 _pump_holders_line
        network_id     已归一化的内部链标识,用来查展示名与链接;查不到就没链接行
        chain_display  链展示名的兜底(pump 只给数字 chainId,没有链名字段)
        traded_at      成交时刻 ISO;解析不出来 → 那一行整行消失
        now            渲染时刻(unix 秒),只用来算"多久之前"
        pool_quote_*   底池对手的符号与**公司/产品名**(见 _pool_quote_line)。
                       ⚠️ 与 FOMO 那条推送同一套:调用方只在对手是**币股**时才传;
                       判断落在 dexscreener.notable,不在这里
        token_name / token_name_zh / stock_company_zh / stock_exchange
                       与 render() 同名参数同义:标题尾巴的英文全名(A)、📝 中文名(C)、
                       🏢 股票说明(B)。缺哪个哪段消失

    ⚠️⚠️ **措辞铁律:只摆可证的事实,一个字都不许替用户下结论。**
       这里的数据是 swap-api 的逐笔成交(签名/时刻/方向/价格/金额),
       所以可以如实说"买入 / 卖出" —— 那是报文里 type 字段的原话。
       但"他在建仓"/"他在跑路"/"该跟"这类**意图断言**全部禁止:
       报文里没有任何证据支持它们,用户自己读得出来。
    ⚠️ 出口不变式与 /ca、Alpha、转入推送同一套:≤ TRANSFER_MSG_BUDGET、
       只在整行边界砍、CA 锚点最后贴且完整(见 _fit_signal)。
    ⚠️ 本函数是纯函数,不查库、不发请求(铁律 7);缺失字段整行消失,绝不打 0 / N/A。
    """
    s = str(side or "").strip().lower()
    if s == "buy":
        emoji, label = EMOJI_PUMP_BUY, LABEL_PUMP_BUY
    elif s == "sell":
        emoji, label = EMOJI_PUMP_SELL, LABEL_PUMP_SELL
    else:
        # 上游给了个没见过的 type。方向未知就说未知 —— 绝不默认成买入
        emoji, label = EMOJI_PUMP_BUY, LABEL_UNKNOWN_SIDE
    title = f"{emoji} <b>{LABEL_PUMP}</b>{SEP}{label}"
    name = _clip(username or "", _PUMP_NAME_CHARS)
    if name:
        title += f"{SEP}<b>{name}</b>"
    sym = _clip((token_symbol or "").lstrip("$"), _SIG_SYMBOL_CHARS)
    if sym:
        title += f"{SEP}<b>${sym}</b>"
    suffix = _name_suffix(token_symbol, token_name)
    if suffix:
        title += f"{SEP}{suffix}"

    lines = [title]
    zh_line = _token_zh_line(token_name, token_name_zh)
    if zh_line is not None:
        lines.append(zh_line)
    # ⚠️ 用 _fmt_usd_tiny 而不是 _fmt_usd:门槛调到 0 之后粉尘成交会进来,
    #    两位小数会把 $0.0000028 渲染成 $0.00,看着像字段没取到(其实是真实值)
    usd = _fmt_usd_tiny(amount_usd)
    if usd is not None:
        lines.append(f"{EMOJI_AMOUNT_IN if s != 'sell' else EMOJI_AMOUNT_OUT} 金额 {usd}")
    px = _fmt_price(price_usd)
    if px is not None:
        lines.append(f"{EMOJI_AVG_PRICE} 单价 {px}")
    # 行序与 FOMO 那条推送对齐(持仓 → 盈亏 → 市值 → 共识),让两种推送看着是一家的
    for extra in (_pump_holding_line(holding_usd, is_cleared, s),
                  _pump_pnl_line(is_cleared, unrealized_pnl_usd, unrealized_pnl_pct,
                                 realized_pnl_usd, realized_pnl_pct),
                  _pump_mcap_line(market_cap_usd, ath_market_cap_usd),
                  # 🚀 / 🧑‍🤝‍🧑 与 FOMO 那条推送同一个位置(市值之后、底池之前),
                  # 两种推送看着是一家的
                  _launchpad_line(launchpad),
                  _token_holders_line(token_holders),
                  # 与 FOMO 那条同样的位置:币本身的事实之后、人的事实之前
                  _pool_quote_line(pool_quote_symbol, pool_quote_name),
                  _stock_line(pool_quote_symbol, stock_company_zh, stock_exchange),
                  _pump_holders_line(holders_in_list)):
        if extra is not None:
            lines.append(extra)
    ts_line = _pump_time_line(traded_at, now)
    if ts_line is not None:
        lines.append(ts_line)

    net = (network_id or "").strip()
    # 链展示名:先查内部映射表,查不到才用调用方给的兜底名(它也可能没有 → 整行消失)
    disp = NETWORK_DISPLAY.get(net) or " ".join(str(chain_display or "").split()) or None
    if disp:
        lines.append(f"{EMOJI_NETWORK} {_esc(disp)}")
    txt = " ".join(str(tx or "").split())
    if txt:
        # 签名独占一行、纯 <code>:它是这条消息里**唯一可自行核验**的东西,
        # 必须能一键复制粘到浏览器里。⚠️ 排在 CA 之前 —— CA 独占最后一行是硬规则
        lines.append(f"{EMOJI_PUMP_TX} <code>{_clip(txt, _PUMP_TX_CHARS)}</code>")
    ca = " ".join(str(coin_mint or "").split())
    # 社媒排在平台链接之前(与 render() 同一套行序)
    social = _socials_line(token_socials)
    if social:
        lines.append(social)
    link = _links_line(_fake_ev(net, ca))
    if link:
        lines.append(link)

    # CA 独占最后一行、纯 <code>(铁律 6)。拿不到就没有这一行 ——
    # 空的 <code></code> 是个点了复制不出东西的假区域,比没有更糟
    anchor = f"<code>{_clip(ca, _SIG_CA_CHARS)}</code>" if ca else None
    return _fit_signal(lines, anchor)


def _pump_holding_line(holding_usd, is_cleared: bool, side: str) -> str | None:
    """
    📦 持仓 $1,234.56 / 📦 剩余 $1,234.56 / 📦 已清仓

    ⚠️⚠️ 已清仓时**必须换措辞**,绝不能报「📦 持仓 $0.00」。与 bot._ca_thesis_row
       同一条教训:那半句读起来是"他空仓",没错;但紧跟着的「未实现盈亏 +$0.00」
       会把一个刚落袋 -$970 的人渲染成"不赚不亏",那是凭空断言的**假事实**。
       清仓之后手上确实一分未实现盈亏都没有 —— 该看的是已实现,由 _pump_pnl_line 接手。
    ⚠️ 还持有时判据一律 is None:holding_usd = 0.0 是"卖到只剩粉尘"的真实值,
       照常显示(它与"已清仓"是两件事 —— 前者手上还有量,只是不值钱了)。
    ⚠️ 卖出用「剩余」、其余用「持仓」,与 FOMO 那条推送的 LABEL_HOLDING_* 同一套口径。
    """
    if is_cleared:
        return f"{EMOJI_HOLDING} {LABEL_PUMP_CLEARED}"
    if holding_usd is None:
        return None
    # ⚠️ 与「💰 金额」同一条理由用 _fmt_usd_tiny:pump 上粉尘仓位是**常态**
    #    (实测 PUNCHMA 那一行 valueUsd = 0.00000703)。两位小数会把它渲染成
    #    「📦 持仓 $0.00」—— 而这条消息里「$0.00」已经被 LABEL_PUMP_CLEARED 占走了
    #    "清仓"这个含义,粉尘仓位再塌成同一个字面量就真的分不出来了。
    usd = _fmt_usd_tiny(holding_usd)
    if usd is None:
        return None
    label = LABEL_HOLDING_SELL if side == "sell" else LABEL_HOLDING_BUY
    return f"{EMOJI_HOLDING} {label} {usd}"


def _pump_pnl_line(is_cleared: bool, unrealized_usd, unrealized_pct,
                   realized_usd, realized_pct) -> str | None:
    """
    📈 未实现盈亏 +$123.45 (+11.11%) / 📉 已实现盈亏 -$970.68 (-88.78%)

    ⚠️⚠️ 清仓看**已实现**、在仓看**未实现**,与 formatter._pnl_line 同一套取舍
       (那边卖出看已实现、其余看未实现)。混用就是把落袋的钱说成账面的、
       或者反过来 —— 两者在清仓那一刻差的正是全部。
    ⚠️ 判据一律 is None:盈亏恰好是 0 是有意义的真实值(刚开仓、或买卖打平)。
    ⚠️ 百分比拿不到时**只掉括号那一段**,金额照常显示 —— 与 _consensus_line 的
       副指标同一种降级。
    """
    if is_cleared:
        val, pct, label = realized_usd, realized_pct, LABEL_PNL_REALIZED
    else:
        val, pct, label = unrealized_usd, unrealized_pct, LABEL_PNL_UNREALIZED
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    # 同上:粉尘仓位的盈亏同样是粉尘,两位小数会把它塌成一个看着像"字段没取到"的 $0.00
    usd = _fmt_usd_tiny(abs(v))
    if usd is None:
        return None
    line = f"{EMOJI_PNL_DOWN if v < 0 else EMOJI_PNL_UP} {label} {'-' if v < 0 else '+'}{usd}"
    if pct is None:
        return line
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return line
    return f"{line} ({p:+.2f}%)" if math.isfinite(p) else line


def _pump_mcap_line(market_cap_usd, ath_market_cap_usd) -> str | None:
    """
    💎 市值 $445.53K · 距最高 -78.9%

    ⚠️⚠️ market_cap_usd 必须来自 coins-v3 的 `usd_market_cap`。**同一个响应里的
       `market_cap` 在 Solana 上是 SOL 计价**(实测 PUNCHMA:market_cap=28.04、
       usd_market_cap=2906.09,前者恰好等于 bonding curve 的 SOL 市值),
       拿它当美元就是把一个 $2,906 的币说成 $28 —— 差两个数量级,而且一眼看不出错。
    ⚠️ 市值拿不到 → **整行消失**(连带「距最高」),绝不用 ath / 入场价 / 0 顶替。
    ⚠️⚠️ 市值**恰好 ≤ 0 时同样整行消失**。这不是拿真值判断代替 is None ——
       取值一路仍然只用 is None 判缺失(0 与缺失在解析层分得很清楚);
       这里判的是另一件事:**这一行说不出口**。
       市值 0 的含义是"这个币的全部流通份额加起来一分钱不值",而同一条消息里
       紧挨着的是一笔以非零单价成交的真实交易 —— 两者直接矛盾,现实里它几乎
       只可能是上游算漏(缺储备量 / 缺价 / 新币还没被索引)。
       而这一行此刻会打出「💎 市值 $0.00 · 距最高 -100.0%」:两个数字连起来
       就是"这币归零了"这句**替读者下的结论**,我们手上一个字的证据都没有。
       负数同理(市值没有负的)。少一行是"我们没说",打这两个数字是"我们说错了"。
    ⚠️ 「距最高」只在 ath 严格大于当前市值时才出:
       · ath <= 当前 = 正在创新高或数据同步滞后,「距最高 -0.0%」是纯噪音;
       · 顺带也挡住了单位错配 —— 万一哪条链的 ath 换成了 quote 计价,
         它会比美元市值小,这一段自己就不出现,而不是打出一个 +10000% 的鬼数。
    """
    cur = _to_decimal(market_cap_usd)
    if cur is None or cur <= 0:
        return None
    mc = _fmt_usd_compact(market_cap_usd)
    if mc is None:
        return None
    line = f"{EMOJI_MARKET_CAP} 市值 {mc}"
    ath = _to_decimal(ath_market_cap_usd)
    if ath is None or ath <= 0 or ath <= cur:
        return line
    return f"{line}{SEP}{LABEL_PUMP_DRAWDOWN} -{(ath - cur) / ath * 100:.1f}%"


def _pump_holders_line(holders_in_list: int | None) -> str | None:
    """
    👥 名单内至少 2 人持有

    ⚠️⚠️ **「至少」这两个字是这句话为真的全部条件,任何时候都不许拿掉。**
       这个 N 的来源是「**本轮**每个人的 portfolio page 0 里,真的看见 N 行
       这个币的正数持仓」—— 它只能往少了数,原因有两条,而且都是常态:
         · page 0 只有 50 行(实测有人 1905 个持仓),某人拿着但那一页没排到;
         · 某人这一轮的 portfolio 请求失败了(返回 None),他整个人都没被观测到。
       所以真实人数 ≥ N。写成「名单内 2 人持有」就是断言"恰好 2 个",
       而我们证明不了那个"恰好"。
       ⚠️⚠️ 反过来更严重的是**多报**:上一版这个数是从 pump_positions 快照表里
       数出来的,而那张表只 upsert、从不删除本轮没出现的行 —— 一个三个月前
       清了仓、清仓那次又恰好没被看见的人,会被永远算成持有者。
       那是主动说了一句假话,比少报严重得多。现在的数只认本轮的正面观测。
    ⚠️ **只报分子,不报分母** —— 与 FOMO 那条的「名单内 3/101 人买过」刻意不同。
       分母的含义是"另外 98 个人没买过",而同样因为上面那个部分视图,
       我们连一个人"没持有"都证明不了。名单只有个位数时那个分数还会被读成"共识"。
    ⚠️ 同理 **0 不显示**:0 不是"没人持有",是"我们这份部分视图里一个都没看见"。
       这不是拿真值判断代替 is None —— None 与 0 在这里指向同一件事
       (都不构成任何可摆出来的事实),而 N ≥ 1 是**正面观测到的**:
       那 N 行持仓我们真的看见了。
    ⚠️ 用「持有」不用「买过」:数据来自本轮的持仓视图(现在还拿着多少),
       不是买入历史。FOMO 那边有 user_token_stats 才敢说"买过"。
    """
    if holders_in_list is None:
        return None
    try:
        n = int(holders_in_list)
    except (TypeError, ValueError):
        return None
    return f"{EMOJI_CONSENSUS} 名单内至少 {n} 人持有" if n > 0 else None


def _pump_time_line(traded_at, now: float | None = None) -> str | None:
    """
    ⏱ 2026-08-31 01:59 UTC · 3M前

    ⚠️ 绝对时刻必须有 —— "3M前"在一条隔夜才看到的消息里是错的,绝对时刻永远对。
    ⚠️ 解析不出来整行消失(铁律 2),绝不拿 0 或当前时间冒充。
    """
    secs = _parse_ts(traded_at)
    if secs is None:
        return None
    try:
        stamp = datetime.fromtimestamp(secs, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return None
    line = f"{EMOJI_CLOCK} {stamp}"
    age = fmt_token_age(secs, now)
    return f"{line}{SEP}{age}前" if age else line


# ============================================================
# pump.fun 指定用户的观点(callout)
# ============================================================
# 行首锚点。⚠️ 全局唯一(铁律 1):FOMO 那边的 💭 是"名单里的人在 FOMO 上发了观点",
#    这里是**另一个平台**上的另一件事。两条消息在聊天列表预览里必须一眼分得开 ——
#    认错锚点就是认错平台,而"他在 pump.fun 上喊了一句"与"他在 FOMO 上写了篇观点"
#    的可信度完全不是一回事。
EMOJI_PUMP_CALLOUT = "📣"
LABEL_PUMP_CALLOUT = "观点"
EMOJI_PUMP_THESIS = "💬"      # 正文
EMOJI_PUMP_LIKE = "👍"        # 点赞数
EMOJI_PUMP_VIEW = "👀"        # 浏览数
# 「发表时市值」—— 刻意不是"当前市值":这条消息说的是**他说话的那一刻**这个币多大。
# 数字直接来自 callout 自己的 marketCap 字段(零额外请求)。
LABEL_PUMP_CALLOUT_MCAP = "发表时市值"
# 「发表至今 ×0.26」—— upstream 的 multiple 字段原样透传(现价 / 发表时价格)。
LABEL_PUMP_CALLOUT_MULTIPLE = "发表至今"
# 正文长度上限。⚠️ thesis 是**用户自由输入的正文**,没有任何天然上界
#    (接口对它不设限,实测里就有整段话)。必须收口,否则一条观点能把整条消息挤爆,
#    而 _fit_signal 只会按整行砍 —— 一行超长的话它要么整行没了、要么顶破预算。
# ⚠️ 比 bot 那边的 CA_THESIS_SNIPPET_CHARS(140)宽一倍是有理由的:那边是
#    「一屏塞 8 个人的观点」,每人只能给一小格;这里一条消息就一个人一句话,
#    砍到 140 会把大半句话吃掉,而这句话正是这条推送的**主体**。
_PUMP_THESIS_CHARS = 280


@_guard_untrusted
def render_pump_callout(
    *,
    username: str | None,
    thesis: str | None,
    coin_mint: str | None,
    token_symbol: str | None = None,
    market_cap_usd=None,
    multiple=None,
    likes=None,
    view_count=None,
    created_at: str | None = None,
    network_id: str | None = None,
    chain_display: str | None = None,
    now: float | None = None,
) -> str:
    """
    「被盯的人在 pump.fun 上发了一条观点(callout)」的推送。

    参数:
        username       pump 用户名,**本人可控** —— 一律走 _clip(叠平空白→限长→转义)
        thesis         观点正文,**完全自由的用户输入** —— 同样走 _clip,
                       转义**只转一次**(_clip 内部转,调用方绝不能再转一遍,
                       否则 `&` 会变成 `&amp;amp;` 显示成乱码)
        coin_mint      他说的是哪个币(CA);独占最后一行的锚点
        token_symbol   币的符号,来自 /coins-v3(/callout/list 自己不给)
        market_cap_usd **发表时**的市值(callout.marketCap),不是当前市值
        multiple       发表至今的倍数(callout.multiple = 现价 / 发表时价格)
        likes/view_count 点赞数 / 浏览数;各自缺失就各自那一段消失
        created_at     发表时刻 ISO(调用方已把 epoch 毫秒归一化过);解析不出来整行消失
        network_id     已归一化的内部链标识,用来查展示名与链接;查不到就没链接行
        chain_display  链展示名的兜底

    ⚠️⚠️ **措辞铁律:只摆可证的事实。** 这条消息里唯一的"内容"是他自己说的那句话,
       我们**原样引用、不做任何解读**。「他看好」「值得跟」这类意图断言全部禁止 ——
       一条 callout 就是一句话,它不构成任何关于他仓位的证据
       (他可能一股没买,也可能早就卖光了)。
    ⚠️ 出口不变式与 render_pump_trade 完全同一套:≤ TRANSFER_MSG_BUDGET、
       只在整行边界砍、CA 锚点最后贴且完整(见 _fit_signal)。
    ⚠️ 本函数是纯函数,不查库、不发请求(铁律 7);缺失字段整行消失,绝不打 0 / N/A。
    """
    title = f"{EMOJI_PUMP_CALLOUT} <b>{LABEL_PUMP}</b>{SEP}{LABEL_PUMP_CALLOUT}"
    name = _clip(username or "", _PUMP_NAME_CHARS)
    if name:
        title += f"{SEP}<b>{name}</b>"
    sym = _clip((token_symbol or "").lstrip("$"), _SIG_SYMBOL_CHARS)
    if sym:
        title += f"{SEP}<b>${sym}</b>"

    lines = [title]
    # ⚠️ 正文:_clip 一次搞定"叠平空白 → 截断 → 转义"三件事,顺序不能反
    #    (反过来会在截断点切断一个 &amp;,残缺实体让整条消息 400)。
    #    叠平空白也是必需的:正文里塞几十个换行就能把一行变成几十行,
    #    绕开 _fit_signal「按整行算预算」这个前提。
    body = _clip(thesis or "", _PUMP_THESIS_CHARS)
    if body:
        lines.append(f"{EMOJI_PUMP_THESIS} {body}")
    for extra in (_pump_callout_mcap_line(market_cap_usd),
                  _pump_callout_multiple_line(multiple),
                  _pump_callout_stats_line(likes, view_count)):
        if extra is not None:
            lines.append(extra)
    ts_line = _pump_time_line(created_at, now)
    if ts_line is not None:
        lines.append(ts_line)

    net = (network_id or "").strip()
    disp = NETWORK_DISPLAY.get(net) or " ".join(str(chain_display or "").split()) or None
    if disp:
        lines.append(f"{EMOJI_NETWORK} {_esc(disp)}")
    ca = " ".join(str(coin_mint or "").split())
    link = _links_line(_fake_ev(net, ca))
    if link:
        lines.append(link)

    # CA 独占最后一行、纯 <code>(铁律 6),与买卖那条逐字同一套
    anchor = f"<code>{_clip(ca, _SIG_CA_CHARS)}</code>" if ca else None
    return _fit_signal(lines, anchor)


def _pump_callout_mcap_line(market_cap_usd) -> str | None:
    """
    💎 发表时市值 $16.38K

    ⚠️ 措辞必须是「发表时」:这个数来自 callout 自己的 marketCap 字段,是他
       **按下发送键那一刻**的市值,不是现在的。写成「市值」会被读成当前值,
       而这条消息可能是几小时前的观点 —— 那就是一个错的数。
    ⚠️ 与 _pump_mcap_line 同一条:拿不到 / ≤ 0 一律整行消失。市值 0 的币
       更可能是上游算漏而不是事实,打出「$0.00」等于替读者断言这币归零了。
    """
    d = _to_decimal(market_cap_usd)
    if d is None or d <= 0:
        return None
    mc = _fmt_usd_compact(d)
    if mc is None:
        return None
    return f"{EMOJI_MARKET_CAP} {LABEL_PUMP_CALLOUT_MCAP} {mc}"


def _pump_callout_multiple_line(multiple) -> str | None:
    """
    📈 发表至今 ×1.29 / 📉 发表至今 ×0.26

    ⚠️ 原样透传上游的 multiple(现价 / 发表时价格),**不换算成百分比、不加任何评价**。
       「×0.26」是事实,「跌了 74%」也是同一个事实的另一种写法,但再往前一步的
       「他喊在了顶上」就是替读者下结论了。
    ⚠️ ≤ 0 整行消失:价格没有负的,而 ×0 意味着现价恰好是 0 —— 与市值 0 同一条,
       更可能是上游算漏。比值本身也没有 0 这个有意义的取值。
    ⚠️ 判空一律 is None(0 与缺失是两件事),再单独判 ≤ 0 那件"说不出口"的事。
    """
    d = _to_decimal(multiple)
    if d is None or d <= 0:
        return None
    emoji = EMOJI_PNL_UP if d >= 1 else EMOJI_PNL_DOWN
    return f"{emoji} {LABEL_PUMP_CALLOUT_MULTIPLE} ×{d:,.2f}"


def _pump_callout_stats_line(likes, view_count) -> str | None:
    """
    👍 3 · 👀 844

    ⚠️ 两个数各自独立降级:只拿到一个就只显示一个,两个都没有整行消失。
       绝不给缺失的那个补 0 —— 「👍 0」是"没人点赞"(真实值),
       而缺失是"我们没拿到这个字段",两者含义相反。
    ⚠️ 0 照常显示(_fmt_count 用 is None 判空):一条没人点赞的观点是常见的真实情况,
       实测夹具里就有 likes=0 的那条。
    """
    parts = []
    n_like = _fmt_count(likes)
    if n_like is not None:
        parts.append(f"{EMOJI_PUMP_LIKE} {n_like}")
    n_view = _fmt_count(view_count)
    if n_view is not None:
        parts.append(f"{EMOJI_PUMP_VIEW} {n_view}")
    return SEP.join(parts) if parts else None
