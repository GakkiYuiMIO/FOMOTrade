"""
Telegram 命令层 —— getUpdates 长轮询 + 命令分发(设计文档 §3.4 / §8.3)

⚠️ 安全:只响应 settings.admin_chat_id 发来的消息。
   不设这道门的话,别人把 Bot 拉进任意群就能 /add 任意用户、/del 掉整个名单。
   admin_chat_id 未配置时**拒绝一切命令**,绝不默认放行。

⚠️ 线程约束:本类跑在 daemon 线程里,只做单条 INSERT,
   唯一的网络 IO 是 /add 的 resolve_handle。
   设计文档 C-1 的"WAL + busy_timeout=5000 就够用"是建立在这个前提上的 ——
   一旦把批量拉取搬进本线程,写锁窗口会从毫秒级涨到秒级,/add 的即时回执随即失效。

⚠️ sqlite3 连接默认不允许跨线程复用,每条命令都用 store.get_conn() 现开现关,
   绝不把 poller 线程的连接借过来。

⚠️ 所有回执都以 parse_mode=HTML 发出,凡是来自用户输入或 API 的文本必须 html.escape,
   一个 '<' 就让整条回执 400 Bad Request。
"""
from __future__ import annotations

import html
import math
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from loguru import logger

from src import formatter, pumpchips, store

# total_supply_of 与 client 那边的响应形状是一体的:它知道 totalSupply 埋在 .token.info 里、
# 也知道 0 / 负数要当"没拿到"处理。在 bot 里再解一遍等于把同一个契约抄成两份,迟早走岔。
from src.client import total_supply_of
from src.config import PROBE_DIR, get_settings
from src.copytrade import auto_blockers, pnl
from src.executor import buy as execute_buy
from src.models import (
    COUNTABLE_REASONS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
    NETWORK_DISPLAY,
    normalize_network,
    normalize_token_address,
)

# ⚠️ 只借 MAX_MESSAGE_LEN 这个常量:/ca 要自己算长度预算,而"上限是多少"必须与真正
#    动手截断的那一方同源 —— 抄一个 4000 过来,哪天 notifier 改了这边就悄悄失效。
from src.notifier import MAX_MESSAGE_LEN

# _f/_pick_str 是 poller 已经踩过坑写好的"字段可能缺、可能嵌套、可能是脏字符串"
# 兜底解析器。/ca 解析的是同一个 API(client.get_token_thesis),没道理另起一套。
# TRANSFER_WATCH_MAX 同理:上限的依据是 poller 的**单轮请求预算**,
# 那里才有算式。在这里复制一个字面量,迟早会与真正生效的那个悄悄分家。
from src.poller import TRANSFER_WATCH_MAX, _f, _pick_str

# TG 服务端 long-poll 挂起秒数。notifier.get_updates 内部的 httpx 超时比它长,不用担心误杀
POLL_TIMEOUT_SEC = 30
# 轮询异常后的退避,避免网络断开时疯狂刷日志
ERROR_BACKOFF_SEC = 3.0
# 比这更旧的命令直接丢弃。
# ⚠️ TG 会保留最多 24h 未确认的 update:进程重启时若原样重放,
#    一条昨天的 /del 会在你今天重新 /add 之后再把人删掉。宁可漏执行,不可乱序重放。
STALE_COMMAND_SEC = 300
# /list 单条消息最多列这么多人(TG 单条 4096 字符硬上限)
MAX_LIST_ROWS = 50

# /following 一次最多导入多少人。超出部分按"交易活跃度"取前 N。
# 每轮给每个人拉一次 swaps(实测 p50 0.27s、12 线程并发),名单规模直接决定单轮耗时;
# balances/trades 不随名单线性增长(见 poller._fetch_snapshots)。
MAX_FOLLOWING_IMPORT = 80

# 榜单一次最多列这么多行(TG 单条 4096 字符硬上限;每行约 70 字符)
MAX_TOP_ROWS = 20
# 榜单周期别名 → API 的 period
_TOP_PERIODS = {
    "24h": "24h", "1d": "24h", "day": "24h", "今日": "24h", "日": "24h",
    "7d": "7d", "week": "7d", "周": "7d", "本周": "7d",
    "30d": "30d", "month": "30d", "月": "30d", "本月": "30d",
    "following": "following", "关注": "following", "f": "following",
}
_PERIOD_LABEL = {"24h": "24 小时", "7d": "7 天", "30d": "30 天", "following": "我关注的人 · 24 小时"}
# ⚠️ 盈亏字段是**按周期命名**的:pnl24h / pnl7d / pnl30d。
#    用固定的 pnl24h 去读 7d 榜单会全部取到 None,显示成一片 $0.00 而不报错。
_PNL_FIELD = {"24h": "pnl24h", "7d": "pnl7d", "30d": "pnl30d", "following": "pnl24h"}

# ⚠️ 命令菜单(用户敲 `/` 时 TG 弹出的列表)。
#    这份列表与 _dispatch 里的分支必须**同步维护** —— 菜单里有、_dispatch 里没有,
#    用户点了只会得到"未知命令"。
_COMMAND_MENU = [
    ("hot", "名单买入榜:/hot [今日|3日|7日] — 大家都在买哪些币"),
    ("add", "加入监控:/add <handle>"),
    ("following", "批量导入某人的关注列表:/following <handle>"),
    ("top", "今日榜单:/top [24h|7d|30d|following] [条数]"),
    ("list", "查看监控名单与基线状态"),
    ("status", "运行状态"),
    ("who", "名单里谁买过这个币:/who <CA>"),
    ("ca", "查合约地址:/ca <地址> [链] — FOMO 用户怎么看这个币"),
    ("chips", "筹码分布:/chips <地址> [链] — 平台与名单各持有多少"),
    ("copy", "跟单参数:/copy 查看 · /copy <项> <值> 修改"),
    ("paper", "跟单台账:纸上建的仓现在赚亏多少"),
    ("star", "特别关注:/star <handle> — 推送加 ⭐ 醒目标识"),
    ("unstar", "取消特别关注:/unstar <handle>"),
    ("tin", "转入推送:/tin <handle> <金额> 设他自己的门槛 · /tin 看名单"),
    ("pump", "pump.fun 名单:/pump 看名单 · /pump add|del <名字或钱包>"),
    ("del", "移出监控:/del <handle>"),
    ("rebuild", "重建全部历史基线(回填逻辑改动后用)"),
    ("help", "命令说明"),
]

# 买入榜的时间窗。key 是用户可以敲的写法
_HOT_WINDOWS = {
    "1d": 1, "24h": 1, "今日": 1, "今天": 1, "日": 1, "day": 1,
    "3d": 3, "3日": 3, "三日": 3, "3天": 3,
    "7d": 7, "7日": 7, "七日": 7, "7天": 7, "week": 7, "周": 7,
}
# ⚠️ 是**滚动窗口**不是自然日:"今日"写成"近 24 小时"才不会被误读成"从今天零点起"
_HOT_LABEL = {1: "近 24 小时", 3: "近 3 日", 7: "近 7 日"}
# 每个币展开几个买家。⚠️ 每多一个就是每个币多一行,乘以 MAX_HOT_ROWS 直接顶 TG 的
#    4096 字符硬上限 —— 两者是绑在一起调的,改一个必须重算总长度。
HOT_TOP_BUYERS = 3
MAX_HOT_ROWS = 10
# 买入先后的名次标。第 4 名之后不展开,所以只要三个
_MEDALS = ("🥇", "🥈", "🥉")
# 峰值比现在高出这么多倍才值得单独写一笔 —— 差不多的时候写出来只是噪音
_PEAK_MIN_RATIO = 1.15

_HELP = (
    "🤖 <b>FOMO 监控 Bot</b>\n"
    "/hot [今日|3日|7日] — 名单买入榜:大家都在买哪些币 🔥\n"
    "/add &lt;handle&gt; — 加入监控(立即生效,历史基线由下一轮建立)\n"
    "/following &lt;handle&gt; — 把这个人关注的所有人批量加入监控\n"
    "/top [24h|7d|30d|following] [条数] — 交易员榜单,默认今日前 15\n"
    "/copy — 跟单参数(默认<b>纸上跟单,不花钱</b>);/copy on 开启\n"
    "/paper — 跟单台账:每一单现在赚亏多少\n"
    "/star &lt;handle&gt; — 特别关注:他的推送带 ⭐、币名加【】\n"
    "/unstar &lt;handle&gt; — 取消特别关注\n"
    "/tin &lt;handle&gt; &lt;金额&gt; — 开这个人的<b>转入逐条推送</b>并设"
    "<b>他自己的</b>门槛(已开着就只改门槛,每人可以完全不同);"
    "不带金额=开/关切换;/tin 不带参数=看名单与各自门槛\n"
    "/pump — <b>pump.fun</b> 买卖监控名单;/pump add|del &lt;名字或钱包&gt;\n"
    "/del &lt;handle&gt; — 移出监控(软删除,历史数据保留)\n"
    "/list — 查看监控名单与基线状态(⭐ 的排最前)\n"
    "/status — 运行状态\n"
    "/who &lt;CA&gt; [链] — 名单里谁买过这个币\n"
    "/ca &lt;地址&gt; [链] — 查这个合约地址:观点 + 名单买没买过\n"
    "/chips &lt;地址&gt; [链] — 筹码分布:FOMO 平台与你的名单各持有多少占比\n"
    "/rebuild — 重建全部历史基线(回填逻辑改动后用)\n"
    "/help — 本说明"
)


def _esc(v) -> str:
    """HTML 转义。API 返回的昵称里带 '<' 并不罕见,不转义整条回执直接 400"""
    return html.escape(str(v)) if v is not None else ""


def _iso_days_ago(days: int, hours: int = 0) -> str:
    """
    N 天前的 UTC ISO 字符串。

    ⚠️ 必须与 models.now_iso() 同格式 —— event_ts 的窗口比较是**字符串比较**,
       格式差一点结果就完全失真(store.load_unsent_recent 踩过这个坑)。
    """
    return (datetime.now(UTC) - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def _ago(iso: str | None) -> str:
    """
    ISO → "3 小时前" 这种相对时间。

    ⚠️ 刻意不显示绝对时刻:库里存的是 UTC,而用户在 UTC+8 ——
       直接显示 "19:20 起" 会被读成本地时间,差 8 小时。
       转成本地时间又要猜时区。相对时间没有这个歧义,而且"多久之前"
       本来就比"几点"更贴近看盘时的判断。
    """
    if not iso:
        return "时间未知"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "时间未知"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins = (datetime.now(UTC) - dt).total_seconds() / 60
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{int(mins)} 分钟前"
    if mins < 60 * 24:
        return f"{int(mins // 60)} 小时前"
    return f"{int(mins // 1440)} 天前"


# 行情多旧就算"冻住了"。1 小时:轮询是 15 秒一轮,只要名单里还有人持有,
# 这个值几分钟就会刷新一次 —— 超过一小时基本只意味着"大家都清仓了"。
_STALE_MCAP_MIN = 60


def _stale_mark(iso: str | None) -> str:
    """行情太旧时的标记。⚠️ 够新就返回空串 —— 正常情况不该占屏"""
    if not iso:
        return "⚠️行情缺失"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins = (datetime.now(UTC) - dt).total_seconds() / 60
    if mins <= _STALE_MCAP_MIN:
        return ""
    return f"⚠️行情停在 {_ago_short(iso)} 前"


def _ago_short(iso: str | None) -> str:
    """
    紧凑相对时间:8m / 3h / 5d。/hot 的买家行一行要塞名字+市值+金额+时间,
    「15 小时前」四个汉字在手机上就是换行的那根稻草。与币龄行用同一套单位。
    """
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins = (datetime.now(UTC) - dt).total_seconds() / 60
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{int(mins)}m"
    if mins < 60 * 24:
        return f"{int(mins // 60)}h"
    return f"{int(mins // 1440)}d"


def _chain_name(net: str | None) -> str:
    """链的展示名。未收录时原样透传(该值来自 API,调用方负责 escape)"""
    return NETWORK_DISPLAY.get((net or "").strip(), (net or "").strip() or "?")


def _money(v: float) -> str:
    """
    榜单用的紧凑金额:$205.3K / $1.24M。

    榜单一行要塞下名字、盈亏、笔数,写全 $205,268.32 会撑爆手机一行 ——
    这里的取舍与推送消息里的 _fmt_usd 不同:那边要精确到分(是成交额),
    这边只要量级(是排名依据)。
    """
    a = abs(v)
    sign = "-" if v < 0 else ""
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:,.2f}{unit}"
    return f"{sign}${a:,.2f}"


def _num(v) -> float:
    """排序用的数值化。取不到就当 0 —— 排序场景下 None 比大小会直接 TypeError"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _day_str(iso: str | None) -> str:
    """ISO 时间取日期部分;缺失显示"时间未知"(绝不显示 None / N/A)"""
    return (iso or "")[:10] or "时间未知"


# ============================================================
# /tin <handle> <金额> —— 每人各自的转入门槛
# ============================================================
# 金额的**白名单**正则。⚠️ 刻意不用裸 float():float("nan") / float("inf") /
#    float("1e999") 全都不报错,写进库之后 `amount_usd >= nan` 恒为 False ——
#    这个人的推送从此一条都不来,而且没有任何报错(与 poller._f 是同一条教训)。
#    白名单只放行"人真的会在聊天框里敲出来的写法",其余一律回用法。
# ⚠️ 数字位刻意写成 [0-9] 而不是 \d:re 的 \d 连全角「５」和各种 Unicode 数字都收,
#    而 float("５") 也照收不误 —— 一个看不出区别的字符就能悄悄变成另一个门槛。
_TIN_MIN_RE = re.compile(
    r"^\$?\s*(?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    r"(?:\.(?P<frac>[0-9]+))?\s*(?P<unit>[kKwW万])?$"
)
# k = 千,w / 万 = 万。⚠️ **刻意不收 m**:金融里 M 既被写成 million 也被写成
#    千(罗马数字 M,"$5MM" 才是五百万),这个歧义猜错一次就是差 1000 倍的门槛,
#    而错的方向是"从此一条都不推"。宁可让用户把零打全。
_TIN_UNITS = {"k": 1000.0, "w": 10000.0, "万": 10000.0}
# 清回「跟着全局默认走」的写法。没有它的话,门槛一旦显式设过就再也回不到
# 「跟着 .env 变」——而这两种状态在清单上是分开显示的,回不去就是个死角。
_TIN_RESET_WORDS = {"默认", "default", "auto", "全局"}
_TIN_USAGE = (
    "❓ 用法:/tin &lt;handle&gt; &lt;金额&gt;"
    "(开启并设<b>他自己的</b>门槛,已经开着就只改门槛)\n"
    "· /tin &lt;handle&gt; 不带金额 = 开/关切换 · /tin 看名单与各自门槛\n"
    "· 金额认:30000 · 30,000 · $200 · 99.99 · 30k(千)· 3w / 3万 · "
    "0(这个人全推)· 默认(清回跟随全局)"
)
# 帮助里必须带上这张表:门槛是个"调了才知道"的数字,不给参照系的话用户只能瞎试。
# 本地库 20 天真实数据复核,单人、条/天。⚠️ 这是**量级**参照,不是承诺值 ——
#    每个人的活跃度差得远,写成两位小数反而假。
_TIN_FREQ_HINT = ("📊 单人推送量(本地库 20 天实测 · 条/天):不设 8.1 · ≥$100 1.1 · "
                  "≥$500 0.8 · ≥$1000 0.7 · ≥$5000 0.5")


def _parse_tin_min_usd(raw: str) -> tuple[bool, float | None, str]:
    """
    解析 /tin 的金额参数。返回 (是否合法, 门槛, 错误回执)。

    ⚠️ **三态**,不是"失败返回 None":None 是合法结果(= 清回全局默认),
       与"解析失败"撞在一起就再也分不出来了(与铁律「空用 is None 判断」同一条)。
    ⚠️ 0 合法(= 这个人的转入全推);负数拒绝 —— 但回执必须把 0 那条路指出来,
       因为敲负数的人多半想表达的就是"别筛了,全给我"。
    """
    s = (raw or "").strip()
    if s.casefold() in _TIN_RESET_WORDS:
        return True, None, ""
    if s.startswith("-"):
        return False, None, (f"❌ 门槛不能是负数({_esc(s)})。想让这个人的转入"
                             f"<b>全推</b>就写 0:/tin &lt;handle&gt; 0")
    m = _TIN_MIN_RE.match(s)
    if m is None:
        return False, None, _TIN_USAGE
    val = float(m.group("int").replace(",", ""))
    if m.group("frac"):
        val += float("0." + m.group("frac"))
    unit = m.group("unit")
    if unit:
        val *= _TIN_UNITS[unit.casefold()]
    if not math.isfinite(val):
        # 正则挡不住 "9" * 400 这种纯数字溢出成 inf 的写法
        return False, None, f"❌ 门槛太大了({_esc(s)})—— 换一个能真的比出大小的数"
    return True, val, ""


# ============================================================
# /ca <合约地址> —— 查这个币:观点(全站,任何地址都能查)+ 名单本地记录
# ============================================================
# 从 API 拉多少条观点。拉够了才能在本地按投入本金可靠地重新排序(接口本身不按这个排)
CA_THESIS_FETCH_LIMIT = 30
# 消息里最多展示这么多**位作者**(不是多少条观点,见 _ca_one_row_per_author)
MAX_CA_THESIS_ROWS = 8
# 单条观点摘要最多这么多字符 —— 不截断的话一条长文/刷屏换行就能撑爆整条消息的排版
CA_THESIS_SNIPPET_CHARS = 140
# 接口来的**短**字段各自的字符上限。
# ⚠️ ticker / handle / 链名全都由陌生人或服务端决定,长度不受任何天然约束:
#    一个 3000 字符的 ticker 就能把 head 段撑到顶破预算,_ca_assemble 只会一行行
#    往下砍,最后整条消息只剩一个 CA 锚点 —— 观点区被一个字段整段掏空。
#    正文早就有 CA_THESIS_SNIPPET_CHARS 管着,这三个短字段之前一个都没管。
CA_TICKER_CHARS = 16
CA_HANDLE_CHARS = 24
CA_CHAIN_CHARS = 20
# 整条 /ca 回执自己给自己定的**转义后**字符预算。
# ⚠️ notifier.send 超限时做的是 text[:MAX_MESSAGE_LEN-10] **盲切**,而 _esc 会把 `&` 撑成
#    5 个字符、`'` 撑成 6 个(html.escape 默认 quote=True)。观点正文的作者是任意 FOMO
#    用户 = 互联网上的陌生人:140 个 `'` 一条就是 840 字符,8 条能到 7000+。盲切点落在
#    `&amp;` 中间就是残缺实体 → 整条消息 400 → 管理员发了 /ca 什么都收不到,只在日志里
#    留一行。而且攻击者能自由控制正文长度,也就能自由挑切点,可稳定命中。
#    (formatter._thesis_line 和 _ca_thesis_text 的注释都记过这个事故。)
#    所以长度必须由本命令**按转义后的真实长度**自己算,并且只在整行边界上停手。
CA_MSG_BUDGET = MAX_MESSAGE_LEN - 400
# 给"还有 N 位未显示"那行预留的位置 —— 免得为了塞进这行提示反而把预算顶破
_CA_OMIT_RESERVE = 48
# 装配出口的**单行**上限(转义后字符数)。
# ⚠️ 这不是"再给某个字段加一道封顶",它是出口不变式(见 _ca_assemble)的支点:
#    整条消息只能按**整行边界**砍(切进行内就会切碎实体 → 400),于是只要存在
#    **一行**能单独超过整条预算,按行砍就救不回来 —— CA 锚点行正是那个洞:
#    它必须活到最后,砍无可砍时照样被贴上去,消息照样超长。
#    给每一行一个上限,"按行砍"才是完备的:任意行都塞得下,砍到只剩锚点也在预算内。
#    这一条同时把所有"接口/用户可控的无界短字段"一次性收口 —— ticker、handle、链名、
#    接口错误文案、已试链名、本地名单里的买家名……不必再逐个去追(那是打地鼠)。
#    取整条预算的 1/4:既远大于本命令自己渲染得出的最长一行(观点摘要 140 个 `'`
#    转义后 846 字符),正常内容一个字都不会被误伤;又保证任何一行最多吃掉四分之一条消息。
CA_LINE_CHARS = CA_MSG_BUDGET // 4
# 猜链的**总时长**预算。
# ⚠️ 命令层是严格串行的:一条命令阻塞越久,排在它后面的命令越可能超过 STALE_COMMAND_SEC
#    被当成过期 update **静默丢弃**(不回复、只留日志)。6 条链 × 每条最坏 3 次重试
#    ≈ 18 个请求,足够把后面几条命令一起吃掉。这里只压单条命令的最坏阻塞时长,
#    **不做冷却/限流** —— 实测串行下 10 条连发 21.1s、2.8 req/s,速率本身不构成威胁。
CA_GUESS_BUDGET_SEC = 45.0
# 名单区最多展开几个买家
MAX_CA_LOCAL_BUYERS = 10
# 本地"名单买没买过"要看全部历史,不设时间窗(3650 天 ≈ 本库不可能积累到的年限)
CA_LOCAL_LOOKBACK_DAYS = 3650
# 地址是 0x 开头、本地又没见过时,按这个顺序试链,首个有观点的即停手。
# ⚠️ 实测证实(同一地址分别喂 nid=56/8453/1/999):猜错链只会拿到空列表 [],
#    绝不会拿到别的币的数据(服务端按 tokenAddress+networkId 精确过滤,不做模糊匹配)——
#    所以"按顺序试、命中就停"这个策略是安全的,不存在"静默显示错链数据"的风险。
#    本地已经认识这个地址时完全不走这条路(见 _cmd_ca),这里只覆盖真正陌生的地址。
CA_EVM_GUESS_ORDER = ("bsc", "base", "ethereum", "monad", "hyperliquid", "robinhood")
# ⚠️ 实测(2026-08-24,真实 API):/feed/token/thesis 的 networkId 参数要的是 FOMO 原生的
#    **数字链 ID**(56 = BSC),不是我们本地聚合用的归一化别名 —— 直接传 "bsc" 会 400:
#    `{"message":"Invalid input: query.networkId - Expected number, received nan"}`。
#    这张表是 models._NETWORK_ALIASES 里已经反查出来的原生值,这里只是反过来:
#    归一化值 → 调 get_token_thesis 时真正要传的那个数字 ID。未收录的值原样透传
#    (与 normalize_network 对未知链的兜底策略一致,不吞掉、让服务端的报错说话)。
_NETWORK_RAW_ID = {
    "solana": "1399811149", "base": "8453", "bsc": "56", "ethereum": "1",
    "monad": "143", "robinhood": "4663", "hyperliquid": "1337",
}
_CA_WS_RUN = re.compile(r"\s+")
# "除普通空格之外的一切空白" —— 换行、制表、NEL、U+2028 行分隔符……
# ⚠️ 预算是按**行**算的,一行里混进一个换行就等于行数被上游控制,出口不变式里
#    那句"每行 ≤ CA_LINE_CHARS"也就不再等于"屏幕上只占一行"。
#    普通空格必须留着:本地名单区那几行靠行首三个空格做缩进,一起抹掉排版就散了。
_CA_LINE_BREAK = re.compile(r"[^\S ]+")
# 一个"原子" = 一个完整标签 / 一个完整实体 / 一个字符。切行只在原子边界上切,
# 切碎实体或标签都让 TG 整条消息 400(见 CA_MSG_BUDGET)。
_CA_ATOM = re.compile(r"</?[a-zA-Z][^<>]*>|&(?:amp|lt|gt|quot|#x27|#39);|.", re.S)
# 本命令**自己**会写进消息里的标签,只有这两个。不在表里的一律当普通文本转义掉 ——
# 一行里冒出别的标签只可能是上游漏了 _esc(观点正文的作者是互联网上的陌生人),
# 照抄出去要么 400,要么让陌生人往我们的消息里塞一个 <a href> 链接。
# ⚠️ 故意只列自己写得出的两个,而不是 TG 支持的一整套:白名单越窄,漏一次 _esc 的后果越小。
_CA_OK_TAGS = frozenset({"b", "code"})
# 需要走原子扫描的标记字符。这三个字符一个都没有的行必然是纯文本,可以原样放行。
_CA_MARKUP = re.compile(r"[<>&]")
_CA_ELLIPSIS = "…"
# 盈亏标签。⚠️ 已实现与未实现混在同一列而不加标签同样是误导:读者没法知道手里这个数
#    是账面浮盈还是已经落袋。与 formatter._pnl_line 同一套语义,只是这边一行要短。
_CA_LABEL_REALIZED = "已实现"
_CA_LABEL_UNREALIZED = "未实现"
_CA_CLOSED_MARK = "已清仓"
# _money 只精确到分:|v| 落在半分以下,四舍五入出来就是 $0.00。见 _ca_pos_str
_CA_DUST_USD = 0.005
# 金额大到这个量级就改用科学计数法。见 _ca_money ——
# 千万亿美元比全球 M2 还大几个量级,真到了这儿它已经不是"钱"而是脏数据/攻击载荷了
_CA_MONEY_SCI = 1e15

# ============================================================
# /chips <合约地址> —— 筹码分布:FOMO 全平台 + 本 bot 监控名单
# ============================================================
# ⚠️⚠️ 两块**共用同一份 topHolders**,这是有意的设计,不是图省事:
#    名单侧若改用 balances(无上限)、平台侧用 topHolders(钳在 100 条),
#    就会出现"名单占比 > 平台占比"这种看着像 bug 的输出 —— 分子口径不同,
#    两个数根本不可比。同源之后两块永远同口径,可以直接相减、相比。
# ⚠️ 顺带也是唯一可行的做法:poller 虽然在拉 balances,但**从来没落库**
#    (11 张表里没有持仓表;user_token_stats 只有 buy_count / first_buy_at,
#     答的是"谁买过"不是"现在持有多少")。现场拉 91 个人 = 91 个请求,
#    一条命令十几秒还抢监控进程的会话,不可接受。
# 名单区最多展开几个人,超出的由 _ca_assemble 收口成"还有 N 人未显示"
MAX_CHIPS_MEMBER_ROWS = 10
# 占比小到这个量级以下就不再报数字 —— 再往下全是量化噪声,写出来只是假精确
_CHIPS_PCT_FLOOR = 0.0001

# ============ 💊 pump.fun 平台那半边 ============
# ⚠️⚠️ 它与上面 🏦 FOMO 那半边是**两个平台的两份数据**,只是印在同一条回执里:
#    分子分母各自独立、失败各自独立,两个百分比**不可以**相加、相减或相除
#    (同一个人可能在两个平台上都持有,加起来会重复计算)。
#    共用的只有"精确 vs 下界"这条判据与它的措辞 —— 那必须一致,否则同一条消息里
#    同一个记号(≥)会有两种含义。
# ⚠️ 取值/聚合全在 src/pumpchips.py(含实测事实与翻页策略),这里只负责文案。
EMOJI_PUMP_CHIPS = "💊"          # pump.fun 的药丸标志
# ⚠️ 名单那行**刻意复用 👥**:铁律 1 里行首 emoji 是聊天列表预览的扫描锚点,
#    而"名单命中"在两半边是同一类信息 —— 给它两个锚点反而更难扫。
#    区分交给紧跟的文字(「你的名单」vs「你的 pump 名单」),而上一行的 💊
#    已经把这半边整个标出来了。
EMOJI_PUMP_WATCH = "👥"
# pump 名单区最多展开几个人。⚠️ 比 FOMO 那半边(10)少一半是有意的:
#    这半边是**加在一条已有回执后面**的,两半边加起来不能把原来的信息挤下去;
#    而 pump 名单本身就是个位数规模(/pump add 手工维护),5 行足够看清是谁。
MAX_PUMP_CHIP_ROWS = 5


class CommandBot:
    """Telegram 命令处理器。由 cli.py 起一个 daemon 线程跑 run_forever()"""

    def __init__(self, client, notifier, poller=None, pump_client=None) -> None:
        """
        poller 是可选的:只用来在 /status 里读最近一次 tick 时间
        (该属性由 cli.cmd_run 的调度任务回填,不是 Poller 契约的一部分)。
        取不到就退化成"最近一条事件的入库时间",绝不因为拿不到它而让 /status 失败。

        pump_client 同样可选:/pump add 要打 pump.fun 反查名字。
        不传就在第一次用到时懒建(见 _pump)—— 不用这个命令的部署不必付
        curl_cffi 的加载代价,单测也能直接注入一个离线桩。
        """
        self._client = client
        self._notifier = notifier
        self._poller = poller
        self._settings = get_settings()
        self._offset: int | None = None
        self._pump_client = pump_client

    # ============================================================
    # 主循环
    # ============================================================
    def poll_once(self) -> None:
        """一轮 getUpdates + 分发。get_updates 内部已吞异常,失败返回空列表"""
        updates = self._notifier.get_updates(offset=self._offset, timeout=POLL_TIMEOUT_SEC)
        for up in updates:
            # ⚠️ 必须**先无条件推进 offset 再处理**:
            #    一条处理必崩的消息(编码异常 / 畸形结构)会被 TG 反复重发,
            #    先处理后推进的写法会让整个命令队列永久卡死在这条消息上。
            update_id = up.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            try:
                self._handle_update(up)
            except Exception:  # noqa: BLE001
                logger.exception("命令处理异常,已跳过该条 | update_id={}", update_id)

    def run_forever(self, stop_event: threading.Event) -> None:
        """
        供线程调用的死循环。任何异常都不退出 —— 命令层挂掉不该影响主推送,
        但反过来"命令层静默死掉"用户是察觉不到的,所以异常必须 logger.exception 留痕。
        """
        if not self._notifier.enabled:
            logger.warning("Telegram 未配置,命令层不启动(/add 等命令不可用)")
            return
        admin = self._settings.admin_chat_id
        if not admin:
            logger.error("未配置 FOMO_TELEGRAM_ADMIN_CHAT_ID / CHAT_ID,命令层不启动(拒绝无主 Bot)")
            return

        # 注册 `/` 输入框的命令菜单。这是 TG 服务端保存的状态,每次启动调一次是幂等的;
        # 失败只降级为警告 —— 菜单没了命令照样能手打
        self._notifier.set_my_commands(_COMMAND_MENU)

        logger.info("Telegram 命令层已启动 | 仅响应 chat_id={}", admin)
        while not stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("命令轮询异常,{}s 后继续", ERROR_BACKOFF_SEC)
                stop_event.wait(ERROR_BACKOFF_SEC)
        logger.info("Telegram 命令层已停止")

    # ============================================================
    # 按钮回调(跟单确认)
    # ============================================================
    def _handle_callback(self, cb: dict) -> None:
        """
        处理一次按钮点击。

        ⚠️ 权限门必须和命令层**一模一样**:按钮消息是发到管理员 chat 的,
           但 callback 里的 from/chat 仍要校验 —— 消息可能被转发到别的群,
           那里的人点了按钮同样会产生 callback。这条路径会**花钱**,门不能比命令层松。
        ⚠️ 无论如何都要 answerCallbackQuery:不回的话客户端一直转圈,
           用户会反复点,而每次点击都是一条新 update。
        """
        cb_id = cb.get("id") or ""
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        admin = self._settings.admin_chat_id

        if not admin or chat_id != str(admin):
            logger.warning("忽略非管理员按钮点击 | chat_id={} | data={}", chat_id, cb.get("data"))
            self._notifier.answer_callback(cb_id, "无权限")
            return

        data = (cb.get("data") or "").strip()
        logger.info("收到按钮点击 | {}", data)
        try:
            toast, new_text = self._dispatch_callback(data)
        except Exception as e:  # noqa: BLE001
            logger.exception("按钮处理异常 | {}", data)
            toast, new_text = f"出错了: {e}"[:180], None

        self._notifier.answer_callback(cb_id, toast)
        # 改写原消息并清掉键盘 —— 留着按钮就能被再点一次,而再点一次就是再买一单
        if new_text and msg.get("message_id"):
            self._notifier.edit_message(chat_id, msg["message_id"], new_text)

    def _dispatch_callback(self, data: str) -> tuple[str, str | None]:
        """回调路由。返回 (气泡提示, 改写后的消息正文或 None)"""
        action, _, payload = data.partition(":")
        if action in ("buy", "skip"):
            return self._cb_copy_decision(action, payload)
        return "未知按钮", None

    def _cb_copy_decision(self, action: str, token_key: str) -> tuple[str, str | None]:
        """
        跟单确认 / 忽略。

        ⚠️ 先用 UPDATE 的 rowcount 抢占状态,再执行下单 ——
           连点两次、或消息被转发后两个人各点一次,都必须只成交一次。
           先执行后改状态的写法在这两种情况下会**买两次**。
        """
        with store.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM copytrade_signals WHERE network_id || ':' || "
                "substr(token_address, 1, 12) = ?", (token_key,),
            ).fetchone()
            if row is None:
                return "找不到这条信号(可能已过期)", None
            if row["status"] != "pending":
                return f"已经处理过了({row['status']})", None

            sym = (row["token_symbol"] or "?").lstrip("$")
            # ⚠️ 抢占必须用 CAS(expect="pending"),不能靠上面那个 SELECT ——
            #    读和写不在同一个事务里,而 poller(主线程)与 bot(daemon 线程)是
            #    两条独立连接。上面那句 `status != pending` 只挡得住慢速的连点,
            #    挡不住真正的竞态;WAL 也不提供跨连接的读改写互斥。
            #    抢到的那一方才有权执行,没抢到的直接退出。
            if action == "skip":
                if not store.set_copy_status(conn, row["network_id"], row["token_address"],
                                             "rejected", expect="pending"):
                    return "已经处理过了", None
                return f"已忽略 ${sym}", f"🚫 <b>已忽略</b> · ${_esc(sym)}"

            cfg = store.load_copy_config(conn)
            # ⚠️ **先抢占状态再执行**:executing 是个中间态,抢不到就说明别人已经在跑了。
            #    先执行后改状态的话,连点两次就是买两次。
            if not store.set_copy_status(conn, row["network_id"], row["token_address"],
                                         "executing", expect="pending"):
                return "已经处理过了", None

        net, ca = row["network_id"], row["token_address"]
        try:
            res = execute_buy(net, ca, sym, row["amount_usd"],
                              dry_run=cfg.dry_run_execute,
                              screenshot_dir=str(PROBE_DIR))
        except Exception as e:  # noqa: BLE001
            logger.exception("买入执行失败 | {} {}", sym, ca[:10])
            # ⚠️ 只覆盖自己抢到的那个 executing。不加 expect 的话,这条失败
            #    能把另一条路径写好的 filled 盖掉 —— 钱花了、台账写"未成交"。
            with store.get_conn() as conn:
                store.set_copy_status(conn, net, ca, "failed", str(e)[:200], expect="executing")
            return (f"没有成交:{e}"[:180],
                    f"❌ <b>未成交</b> · ${_esc(sym)}\n{_esc(str(e)[:300])}\n"
                    f"<code>{_esc(ca)}</code>")

        # ⚠️ 演练模式**绝不能记成 filled**:那会让 /paper 给一个并不存在的仓位算盈亏
        with store.get_conn() as conn:
            store.set_copy_status(conn, net, ca,
                                  "rejected" if cfg.dry_run_execute else "filled",
                                  res.message[:200], expect="executing")
        if cfg.dry_run_execute:
            return ("演练通过(未真实成交)",
                    f"🧪 <b>演练通过 · 未成交</b> · ${_esc(sym)}\n{_esc(res.message)}\n"
                    f"确认无误后 <code>/copy live</code> 开真实成交")
        # ⚠️ "读到仓位变大"和"点了但没读到"是两件事,措辞必须分开。
        #    后者报成"已成交"会让人以为没事;报成"失败"又会诱使人再点一次 = 买两次。
        if res.confirmed:
            return (f"已成交 · {res.message}"[:180],
                    f"✅ <b>已成交</b> · ${_esc(sym)}\n{_esc(res.message)}\n"
                    f"<code>{_esc(ca)}</code>")
        return ("已点击,但没读到仓位变化 —— 请到 APP 核对",
                f"⚠️ <b>已点击 · 结果待核对</b> · ${_esc(sym)}\n{_esc(res.message)}\n"
                f"<code>{_esc(ca)}</code>")

    # ============================================================
    # 分发
    # ============================================================
    def _handle_update(self, up: dict) -> None:
        if up.get("callback_query"):
            self._handle_callback(up["callback_query"])
            return
        msg = up.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        chat_id = str((msg.get("chat") or {}).get("id") or "")
        admin = self._settings.admin_chat_id
        if not admin:
            logger.warning("未配置 admin_chat_id,忽略命令 | chat_id={} | {}", chat_id, text[:50])
            return
        if chat_id != str(admin):
            # 别人把 Bot 拉进群时会在这里留痕 —— 只记日志,不回复(回复等于确认 Bot 存在)
            logger.warning("忽略非管理员命令 | chat_id={} | {}", chat_id, text[:50])
            return

        sent_at = msg.get("date")
        if isinstance(sent_at, (int, float)) and time.time() - sent_at > STALE_COMMAND_SEC:
            logger.warning("丢弃过期命令(积压 {:.0f}s) | {}", time.time() - sent_at, text[:50])
            return

        # 群里 TG 会把命令发成 /add@YourBot,不剥掉 @botname 就永远匹配不上
        head, _, arg = text.partition(" ")
        cmd = head.split("@", 1)[0].lower()
        arg = arg.strip()

        logger.info("收到命令 | {} {}", cmd, arg[:60])
        reply = self._dispatch(cmd, arg)
        if reply:
            self._notifier.send(reply, chat_id=chat_id)

    def _dispatch(self, cmd: str, arg: str) -> str:
        if cmd in ("/help", "/start"):
            return _HELP
        if cmd == "/add":
            return self._cmd_add(arg)
        if cmd == "/following":
            return self._cmd_following(arg)
        if cmd in ("/top", "/leaderboard", "/lb"):
            return self._cmd_top(arg)
        if cmd in ("/hot", "/coins", "/buys"):
            return self._cmd_hot(arg)
        if cmd == "/rebuild":
            return self._cmd_rebuild(arg)
        if cmd in ("/del", "/rm", "/remove"):
            return self._cmd_del(arg)
        if cmd == "/copy":
            return self._cmd_copy(arg)
        if cmd in ("/paper", "/positions"):
            return self._cmd_paper()
        if cmd in ("/star", "/fav"):
            return self._cmd_star(arg, on=True)
        if cmd in ("/unstar", "/unfav"):
            return self._cmd_star(arg, on=False)
        if cmd == "/tin":
            return self._cmd_tin(arg)
        # ⚠️ 刻意**只烧一个命令名 + 子命令**,不做 /padd /pdel /plist:
        #    `/plist` 与 `/list` 只差一个字母,最容易手滑,而误用的后果是
        #    「往错的平台加了人」—— pump 的名单与 FOMO 的名单是两张互不相干的表。
        if cmd == "/pump":
            return self._cmd_pump(arg)
        if cmd == "/list":
            return self._cmd_list()
        if cmd == "/status":
            return self._cmd_status()
        if cmd == "/who":
            return self._cmd_who(arg)
        if cmd == "/ca":
            return self._cmd_ca(arg)
        if cmd == "/chips":
            return self._cmd_chips(arg)
        return f"❓ 未知命令 {_esc(cmd)},发 /help 看用法"

    # ============================================================
    # 命令实现
    # ============================================================
    def _cmd_add(self, arg: str) -> str:
        """
        /add <handle> —— 解析 handle → userId,一条 INSERT,**毫秒级立即回执**。

        ⚠️ 绝不在这里建历史基线:seeding 要翻 10 页 swaps,耗时几十秒。
           基线由 poller 下一 tick 的 seed_next_pending_user() 建,
           期间事件照常推送、只是不打徽章(设计文档 A-1 / A-3)。
        """
        # ⚠️ 查询用原始输入(FOMO 端点是否大小写敏感未知),不要先压小写
        handle = store.clean_handle(arg)
        if not handle:
            return "用法: /add &lt;handle&gt;  例: /add maxpain"

        try:
            # 第三项是 API 侧的**规范大小写** handle —— 存它而不是用户敲进来的那个,
            # 消息里才会显示成 @GakkiYuiTifa 而不是 @gakkiyuitifa
            user_id, display, canonical = self._client.resolve_handle(handle)
        except Exception as e:  # noqa: BLE001
            return self._resolve_error(handle, e)
        if not user_id:
            return f"❌ 找不到用户 @{_esc(handle)}(handle 拼错?或该用户已改名)"
        handle = canonical or handle

        with store.get_conn() as conn:
            # ⚠️ 只用 store 返回的布尔值,不直接转发它的文案 ——
            #    那段文案里的 display_name 没有 HTML 转义,昵称带 '<' 会让回执 400。
            need_seed, _ = store.add_watch_user(conn, user_id, handle, display)

        name = _esc(display or handle)
        if not need_seed:
            return f"ℹ️ {name} 已在监控中"
        return (
            f"✅ 已加入 <b>{name}</b>(@{_esc(handle)})\n"
            f"⏳ 正在建立历史基线,完成前的买入不打徽章、不显示共识"
        )

    def _cmd_rebuild(self, arg: str) -> str:
        """
        /rebuild confirm —— 重建全部历史基线。

        什么时候需要:回填逻辑本身改好之后(比如分页参数修对了、回填条数上调了),
        已建好的旧基线仍是按旧规则建的,不重建就一直用着不准的「首次建仓」判据。

        ⚠️ 要打 confirm 才执行:重建期间(N 人 = N 轮)这些人不打徽章、不显示共识。
           这是个有代价的操作,不该手滑就触发。
        """
        if (arg or "").strip().lower() != "confirm":
            with store.get_conn() as conn:
                users = store.list_active_users(conn)
            interval = self._settings.fomo_poll_interval_sec
            mins = len(users) * interval / 60
            return (
                f"⚠️ <b>重建历史基线</b>\n"
                f"会把名单里 {len(users)} 人的基线全部重建,每轮建一个,"
                f"约需 {mins:.0f} 分钟。\n"
                f"期间这些人的买入<b>不打徽章、不显示共识</b>(推送照常,不会丢事件)。\n\n"
                f"确认请发:<code>/rebuild confirm</code>"
            )
        with store.get_conn() as conn:
            n = store.reset_all_baselines(conn)
        interval = self._settings.fomo_poll_interval_sec
        return (
            f"✅ 已重置 {n} 人的基线,将逐轮重建(约 {n * interval / 60:.0f} 分钟)\n"
            f"⏳ 期间不打徽章、不显示共识;每建好一个会有一条通知\n"
            f"游标未改动 —— 不会漏推、也不会重推历史"
        )

    def _cmd_hot(self, arg: str) -> str:
        """
        /hot [今日|3日|7日] —— 名单买入榜:监控的这批人在窗口内买了哪些币。

        这是**按币聚合**,不是按人 —— 想看的是"大家都在买什么",
        所以名次由 **买入人数** 决定(单人反复加仓刷不上来)。

        全部走本地库,零 API 调用:
          买入人数 / 总额 / 首次时间  ← fomo_events
          现在市值                    ← token_snapshot(poller 每轮落的行情)
        倍数 = 现在市值 ÷ **窗口内最早那笔买入时的市值**,
        也就是"名单开始买之后涨了多少" —— 这才是判断金狗的依据。
        """
        days = 1
        for tok in (arg or "").split():
            t = tok.strip().lower()
            if t in _HOT_WINDOWS:
                days = _HOT_WINDOWS[t]
        since = _iso_days_ago(days)
        label = _HOT_LABEL.get(days, f"近 {days} 日")

        with store.get_conn() as conn:
            rows = store.hot_tokens(conn, since, limit=MAX_HOT_ROWS)
            ready = len([r for r in store.list_active_users(conn) if r["stats_ready"]])
            detail = {
                (r["network_id"], r["token_address"]):
                    store.token_buyers(conn, r["network_id"], r["token_address"], since,
                                       limit=HOT_TOP_BUYERS)
                for r in rows
            }

        if not rows:
            return (
                f"🔥 <b>名单买入榜 · {label}</b>\n"
                f"这段时间名单里没人买入。\n"
                f"(名单 {ready} 人已就绪;刚 /add 的人要等基线建好才会有数据)"
            )

        lines = [f"🔥 <b>名单买入榜 · {label}</b>"]
        for i, r in enumerate(rows, 1):
            sym = _esc(r["symbol"] or "?")
            buyers, buys = r["buyers"], r["buys"]
            # 倍数是名次的依据,放进标题行 —— 排序键必须一眼可见,否则名次看着像随机的
            head = f"{i}. <b>${sym}</b>"
            mult = r["mult"]
            if mult is not None:
                x = _num(mult)
                # ⚠️ 写「最高」两个字:这是**峰值**倍数,不是现在的倍数。
                #    不写的话,一个回撤过的币会被当成"现在还有这么多",
                #    而下面 💎 行明明写着现价更低 —— 两个数打架,用户只会当是 bug。
                head += f" · <b>🚀 最高 {x:.1f}x</b>" if x >= 1.1 \
                    else f" · 📉 {(x - 1) * 100:+.0f}%"
            head += f" · 👥 {buyers} 人买入"
            if buys > buyers:
                head += f"({buys} 笔)"
            lines.append(head)

            # 市值:名单开始买时 → 现在。倍数就是这两个数的比。
            # ⚠️ "现在"两个字不能省:光写 `$41.9K → $2.9M` 会被读成"起点 → 最高点",
            #    于是下面某个在 $4.19M 进场的买家看着像不可能。
            #    真相是这个币冲到 4.19M 之后回落了 —— 那正是最该看见的信息,见 peak。
            first_mc, now_mc, peak_mc = r["first_mcap"], r["now_mcap"], r["peak_mcap"]
            if first_mc and now_mc:
                seg = [f"💎 {_money(_num(first_mc))} → 现在 {_money(_num(now_mc))}"]
            elif now_mc:
                seg = [f"💎 现在 {_money(_num(now_mc))}"]
            else:
                seg = []
            # 明显回落过才提峰值:等于现在的时候写出来只是噪音
            if seg and peak_mc and now_mc and _num(peak_mc) >= _num(now_mc) * _PEAK_MIN_RATIO:
                seg.append(f"峰值 {_money(_num(peak_mc))}")
            seg.append(f"💰 名单买入 {_money(_num(r['total_usd']))}")
            lines.append("   " + " · ".join(seg))

            # 前 3 个买的人:名次 · 名字 · 累计买入额 · 首笔时间。
            # ⚠️ 名次是**买入先后**,不是金额大小 —— 这个榜的价值在于"谁先摸到",
            #    金额只是佐证他下了多大的注。
            # ⚠️ 绝不把这一行与上面的「💎 基准市值」合成「@某人在 $42K 时买入」:
            #    基准市值取的是最早**有市值**那笔,可能不是这个人那笔
            #    (见 store.hot_tokens 的说明),合起来就是在断言我们并不知道的事。
            who = detail.get((r["network_id"], r["token_address"])) or []
            for rank, w in enumerate(who[:HOT_TOP_BUYERS]):
                if not w["who"]:
                    continue
                seg = [f"{_MEDALS[rank]} @{_esc(w['who'])}"]
                # 💎 = 他**进场时**的市值。⚠️ 多笔建仓时它只代表第一笔的位置,
                #    所以后面的笔数必须留着 —— 只看一个市值会把"分五笔从 40K 加到 200K"
                #    读成"他在 40K 一把梭"。
                if w["mcap"]:
                    seg.append(f"💎{_money(_num(w['mcap']))}")
                usd = _num(w["usd"])
                if usd > 0:                       # 0 = 这几笔都没解析出金额,整段消失
                    seg.append(_money(usd) + (f"({w['buys']} 笔)" if w["buys"] > 1 else ""))
                seg.append(_ago_short(w["ts"]))
                lines.append("   " + " · ".join(seg))

            tail = [f"🧬 {_esc(_chain_name(r['network_id']))}"]
            more = max(buyers - min(len(who), HOT_TOP_BUYERS), 0)
            if more:
                tail.insert(0, f"👤 另有 {more} 人")
            lines.append("   " + " · ".join(tail))
            lines.append(f"   <code>{_esc(r['token_address'])}</code>")

        # 行情新鲜度:清仓后 token_snapshot 就不再更新,倍数会失真,必须让用户知道
        stale = [r for r in rows if r["mcap_at"] and r["mcap_at"] < _iso_days_ago(0, hours=1)]
        if stale:
            lines.append(f"\n{_esc('⚠️')} {len(stale)} 个币的行情已超过 1 小时未更新"
                         f"(名单里没人持有了,倍数仅供参考)")
        n_nomult = sum(1 for r in rows if r["mult"] is None)
        # ⚠️ 必须点明奖牌是**买入先后**:🥇🥈🥉 通常被读成"金额最大",
        #    而榜里经常出现 🥈 比 🥇 买得多的情况(先摸到的人未必下注最重)。
        foot = (f"\n🥇🥈🥉 = 买入先后 · 按<b>最高倍数</b>排序"
                f"(峰值 ÷ 名单最早买入时市值) · 名单 {ready} 人")
        if n_nomult:
            # 不说明的话,榜尾那几个没有倍数的看着像 bug
            foot += f"\n{_esc('·')} 末尾 {n_nomult} 个币缺基准市值,按人数排"
        lines.append(foot + "\n/hot 今日|3日|7日")
        # 3日/7日 的数据要靠 bot 持续运行积累:每轮只拉每人最近 50 笔 swaps,
        # 刚跑起来时更长的窗口和"今日"看着会差不多。不说明的话用户会以为是 bug。
        if days > 1:
            lines.append("(更长的窗口需要 bot 持续运行来积累,刚启动时数据会偏少)")
        return "\n".join(lines)

    def _cmd_top(self, arg: str) -> str:
        """
        /top [24h|7d|30d|following] [条数] —— 榜单。

        榜单的用处不只是看谁在赚 —— 更实用的是**对照自己的监控名单**:
        每行标注是否已在监控(👁)、是否已建好基线(⏳),
        没标记的就是"排在前面但你还没盯"的人,可以直接 /add。
        """
        period, count = "24h", 15
        for tok in (arg or "").split():
            t = tok.strip().lower()
            if t in _TOP_PERIODS:
                period = _TOP_PERIODS[t]
            elif t.isdigit():
                count = max(1, min(int(t), MAX_TOP_ROWS))

        # ⚠️ following 必须拉满再本地排序。/v2/leaderboard/following 返回的是
        #    **关注列表顺序**而不是排名(实测 -39.96K 排在 +37K 前面),
        #    先截断到 15 条再排,等于"关注列表前 15 人里最赚的 15 个" ——
        #    真正的第一名如果排在关注列表第 40 位就永远看不到,而且榜单看着单调递减、
        #    无报错无 0 值,用户根本察觉不到。24h/7d/30d 服务端已排好序,不受影响。
        api_limit = 100 if period == "following" else count
        try:
            rows = self._client.get_leaderboard(period, limit=api_limit)
        except Exception as e:  # noqa: BLE001
            return self._resolve_error(period, e)
        if not rows:
            return f"ℹ️ {_PERIOD_LABEL.get(period, period)} 榜单暂无数据"

        # ⚠️ /v2/leaderboard/following 返回的是**关注列表顺序**,不是排名
        #    (实测 -39.96K 排在 +37K 前面)。按选定周期的盈亏自己排一遍;
        #    24h/7d/30d 三个榜服务端已排好,再排一次是无害的 no-op。
        pnl_key = _PNL_FIELD.get(period, "pnl24h")
        rows = sorted(
            (u for u in rows if isinstance(u, dict)),
            key=lambda u: _num(u.get(pnl_key)),
            reverse=True,
        )

        # 一次查出名单状态,避免逐行开连接
        with store.get_conn() as conn:
            watched = {
                r["user_id"]: r["stats_ready"]
                for r in conn.execute(
                    "SELECT user_id, stats_ready FROM watch_users WHERE active = 1"
                ).fetchall()
            }

        lines = [f"🏆 <b>FOMO 榜单 · {_PERIOD_LABEL.get(period, period)}</b>"]
        new_cnt = 0
        for i, u in enumerate(rows[:count], 1):
            if not isinstance(u, dict):
                continue
            uid = str(u.get("id") or "")
            name = _esc(str(u.get("displayName") or u.get("userHandle") or "?")[:16])
            handle = _esc(store.clean_handle(u.get("userHandle") or ""))
            # 👁 已在监控且基线就绪 / ⏳ 在监控但基线还没建好 / 无标记 = 还没盯
            if uid in watched:
                mark = "👁" if watched[uid] else "⏳"
            else:
                mark = "　"      # 全角空格占位,保持各行对齐
                new_cnt += 1
            pnl = _num(u.get(pnl_key))
            trades = int(_num(u.get("numTrades")))
            lines.append(
                f"{i:2d}. {mark} <b>{name}</b> @{handle}\n"
                f"      {'📈' if pnl >= 0 else '📉'} {_money(pnl)} · {trades} 笔"
            )

        lines.append("")
        lines.append(f"👁 已监控且基线就绪 · ⏳ 基线建立中 · 无标记 = 还没盯({new_cnt} 人)")
        if new_cnt:
            lines.append("想盯谁就 /add &lt;handle&gt;")
        return "\n".join(lines)

    def _cmd_following(self, arg: str) -> str:
        """
        /following <handle> —— 把这个人关注的所有人批量加入监控。

        ⚠️ 这是**一次性导入**,不是持续同步:对方之后新关注的人不会自动进来。
           做成持续同步会带来"他取关了要不要自动 /del"这种没有正确答案的问题,
           而误删会连带把本地已建好的基线一起作废。
        ⚠️ 名单规模直接决定一轮拉多久(每人 3 个请求、实测约 1s/人)。
           超过 MAX_FOLLOWING_IMPORT 时按 swapCount 取最活跃的那批 ——
           被砍掉的是几乎不交易的人,信息损失最小。
        """
        handle = store.clean_handle(arg)
        if not handle:
            return "用法: /following &lt;handle&gt;  例: /following GakkiYuiTifa"

        try:
            user_id, display, canonical = self._client.resolve_handle(handle)
            following = self._client.get_following(user_id)
        except Exception as e:  # noqa: BLE001
            return self._resolve_error(handle, e)
        if not user_id:
            return f"❌ 找不到用户 @{_esc(handle)}"
        who = _esc(display or canonical or handle)
        if not following:
            return f"ℹ️ {who} 没有关注任何人"

        # 只留能用的:有 id、非受限。private 账号照样收 —— 是否拿得到数据由 API 决定,
        # 这里先不替它做判断,拉不到时 fetch_snapshot 会把该项降级为 None
        cands = [u for u in following
                 if isinstance(u, dict) and u.get("id") and not u.get("isRestricted")]
        total = len(cands)
        # 按交易活跃度排序:超限时砍掉的是几乎不交易的人
        cands.sort(key=lambda u: (_num(u.get("swapCount")), _num(u.get("numTrades"))), reverse=True)
        picked = cands[:MAX_FOLLOWING_IMPORT]

        added = skipped = failed = 0
        with store.get_conn() as conn:
            for u in picked:
                try:
                    need_seed, _ = store.add_watch_user(
                        conn, str(u["id"]),
                        store.clean_handle(u.get("userHandle") or ""),
                        u.get("displayName") or u.get("userHandle"),
                    )
                    added += 1 if need_seed else 0
                    skipped += 0 if need_seed else 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    logger.warning("批量加入失败 | {} | {}", u.get("userHandle"), e)
            active_total = len(store.list_active_users(conn))

        lines = [
            f"✅ 已导入 <b>{who}</b> 的关注列表",
            f"新增 {added} 人 · 已在监控 {skipped} 人" + (f" · 失败 {failed} 人" if failed else ""),
        ]
        if total > len(picked):
            lines.append(
                f"{_esc('⚠️')} 对方关注 {total} 人,只取了交易最活跃的 {len(picked)} 人"
                f"(上限 {MAX_FOLLOWING_IMPORT},再多会拖垮轮询)"
            )
        # 每轮成本 = 全员 swaps(实测 p50 0.27s/人)+ 十来个 balances,后者不随名单增长
        # (见 poller._fetch_snapshots)。观点扫描与采集并行,不计入。
        s = get_settings()
        workers = max(1, s.fomo_fetch_workers)
        est = active_total * 0.27 / workers + 10 * 1.22 / workers + 1
        interval = s.fomo_poll_interval_sec
        lines.append(f"📋 当前名单 {active_total} 人 · 预计每轮约 {est:.0f}s(间隔 {interval}s)")
        if est > interval * 0.8:
            lines.append(
                f"{_esc('⚠️')} 单轮耗时已接近轮询间隔,建议把 .env 的 "
                f"FOMO_POLL_INTERVAL_SEC 调到 {int(est * 2)} 以上后重启"
            )
        lines.append(f"{_esc('⏳')} 历史基线每轮建一个人,约 {active_total} 轮后全部就绪")
        return "\n".join(lines)

    def _resolve_error(self, handle: str, e: Exception) -> str:
        """
        把 client / auth 抛出的异常翻译成人话。
        "解析失败"这四个字对排查毫无帮助 —— 用户需要知道的是"该重新登录"还是"名字打错了"。
        """
        # 延迟导入:让 --init-db 这类不碰网络的命令即使 playwright 没装也能跑
        from src.auth import AuthError
        from src.client import FomoAPIError

        if isinstance(e, AuthError):
            logger.warning("resolve_handle 鉴权失败 | {}", e)
            return "❌ 登录态失效,请到服务器执行 <code>.\\bot.ps1 --login</code> 重新登录"

        status = getattr(e, "status", None) or getattr(e, "status_code", None)
        text = str(e)
        if status == 404 or "404" in text:
            return f"❌ 找不到用户 @{_esc(handle)}(handle 拼错?或该用户已改名)"
        if status in (401, 403) or "401" in text or "403" in text:
            return (
                "❌ FOMO 接口拒绝访问(登录态失效,或 Cloudflare 拦截)\n"
                "先试 <code>.\\bot.ps1 --login</code>;仍不行则把 FOMO_CLIENT_IMPL 改成 playwright"
            )
        if isinstance(e, FomoAPIError):
            logger.warning("resolve_handle 失败 | {} | {}", handle, e)
            return f"❌ FOMO 接口异常: {_esc(text[:150])}"
        logger.exception("resolve_handle 未知异常 | {}", handle)
        return f"❌ 解析 handle 失败: {_esc(text[:150])}"

    def _cmd_del(self, arg: str) -> str:
        """/del <handle> —— 软删除。共识数随之下降是正确行为(设计文档 B-6)"""
        key = (arg or "").strip()
        if not key:
            return "用法: /del &lt;handle&gt;"
        # 原样传:store.remove_watch_user 先按 user_id 精确匹配、再按 handle 归一化匹配。
        # 这里若先 lower() 会把大小写敏感的 user_id 破坏掉。
        with store.get_conn() as conn:
            # 先取一次行:回执里报**库里的名字**而不是用户敲进来的字符串,
            # 敲错大小写或用 user_id 删人时,回执才能确认删对了人
            row = store.get_watch_user(conn, key) or store.find_user_by_handle(conn, key)
            name = (row["display_name"] or row["handle"]) if row else key
            ok, _ = store.remove_watch_user(conn, key)
        if not ok:
            return f"⚠️ 未在监控名单中: {_esc(key)}"
        return f"✅ 已移除 <b>{_esc(name)}</b>(相关代币共识数已下调)"

    # ============================================================
    # 跟单
    # ============================================================
    # /copy 能改的项。值一律走这里的解析器 —— 直接 int()/float() 的话
    # `/copy amount abc` 会抛异常、命令层只回一句"处理异常",用户不知道错在哪。
    _COPY_FIELDS = {
        "on":      ("enabled", lambda v: True),
        "off":     ("enabled", lambda v: False),
        "real":    ("paper_only", lambda v: False),
        "paper":   ("paper_only", lambda v: True),
        "live":    ("dry_run_execute", lambda v: False),
        "rehearse": ("dry_run_execute", lambda v: True),
        "buyers":  ("min_buyers", lambda v: max(1, int(v))),
        "window":  ("window_hours", lambda v: max(1, int(v))),
        "age":     ("max_age_hours", lambda v: None if v in ("off", "0") else max(1, int(v))),
        "mcap":    ("max_entry_mcap", lambda v: None if v in ("off", "0") else float(v)),
        "amount":  ("amount_usd", lambda v: max(0.0, float(v))),
        "daily":   ("daily_max", lambda v: max(0, int(v))),
        "spend":   ("daily_spend_usd", lambda v: None if v in ("off", "0") else float(v)),
        # ⚠️ auto 单独一档,**不跟 real/live 联动**:那两个是"验证自动化点对了没有"
        #    的流程开关,用户会按引导一个个关掉。自动成交要是搭在它们身上,
        #    用户走完验证流程的那一刻就变成无人值守了 —— 而他没打算开这个。
        "auto":    ("auto_execute", lambda v: True),
        "manual":  ("auto_execute", lambda v: False),
        "starred": ("starred_only", lambda v: v in ("1", "on", "true", "yes")),
    }
    _COPY_SWITCHES = ("on", "off", "paper", "real", "live", "rehearse", "auto", "manual")

    def _cmd_copy(self, arg: str) -> str:
        """/copy 查看 · /copy <项> <值> 改。⚠️ 接真实下单必须显式 /copy real"""
        parts = (arg or "").split()
        with store.get_conn() as conn:
            cfg = store.load_copy_config(conn)
            if parts:
                key = parts[0].strip().lower()
                spec = self._COPY_FIELDS.get(key)
                if not spec:
                    return ("❓ 可改:on/off · paper/real · rehearse/live · manual/auto"
                            " · buyers · window · age · mcap · amount · daily · spend · starred\n"
                            "例:<code>/copy buyers 2</code>")
                field, parse = spec
                raw = parts[1].strip().lower() if len(parts) > 1 else ""
                if key not in self._COPY_SWITCHES and not raw:
                    return f"❓ 用法:<code>/copy {_esc(key)} &lt;值&gt;</code>"
                try:
                    val = parse(raw)
                except (TypeError, ValueError):
                    return f"❓ <code>{_esc(raw)}</code> 不是合法的值"
                new_cfg = replace(cfg, **{field: val})
                # ⚠️ 开无人值守要**当场**告诉用户还差什么,而不是让他开完之后
                #    在某个凌晨发现「怎么一单没跟」或者「怎么花了这么多」。
                if key == "auto" and (blockers := auto_blockers(new_cfg)):
                    return ("⛔ <b>还不能开无人值守</b>,先补齐:\n"
                            + "\n".join(f"· {_esc(b)}" for b in blockers))
                cfg = new_cfg
                store.save_copy_config(conn, cfg)
            taken = store.copy_taken_today(conn)
            spent = store.copy_spent_today(conn)

        # ⚠️ 模式按"实际会发生什么"算,不是按开关名字堆:
        #    paper 盖过一切,dry_run 盖过 auto —— 开了 auto 但还在演练时,
        #    显示"无人值守自动成交"就是撒谎(它一分钱都不会花)。
        if cfg.paper_only:
            mode = "🧪 纸上跟单(不花钱)"
        elif cfg.dry_run_execute:
            mode = ("🎭 演练下单 · 自动触发(走流程但不成交)" if cfg.auto_execute
                    else "🎭 演练下单(走流程但不成交)")
        elif cfg.auto_execute:
            mode = "🤖 <b>无人值守自动成交</b>(没有人会被问)"
        else:
            mode = "🛒 <b>真实成交</b>(仍需你点确认)"
        age = f"≤ {cfg.max_age_hours} 小时" if cfg.max_age_hours is not None else "不限"
        mcap = _money(cfg.max_entry_mcap) if cfg.max_entry_mcap is not None else "不限"
        # ⚠️ daily_max=0 是「不限」(与 age/mcap 的 off 同义)。渲染成 "今日 3/0 单"
        #    会被读成「已经限住了」—— 恰恰相反,那是闸门开着。
        cnt = f"{taken}/{cfg.daily_max} 单" if cfg.daily_max > 0 else f"{taken} 单(笔数不限)"
        spend = (f"{_money(spent)}/{_money(cfg.daily_spend_usd)}"
                 if cfg.daily_spend_usd is not None else f"{_money(spent)}(金额不限)")
        return "\n".join([
            f"🤖 <b>跟单</b> · {'✅ 已开启' if cfg.enabled else '⛔ 未开启'} · {mode}",
            f"👥 触发人数 ≥ <b>{cfg.min_buyers}</b>(窗口 {cfg.window_hours}h)"
            + ("· 只数 ⭐" if cfg.starred_only else ""),
            f"🕐 币龄 {age} · 💎 入场市值 {mcap}",
            f"💰 每单 {_money(cfg.amount_usd)} · 📅 今日 {cnt} · 💸 今日 {spend}",
            "同一个币只跟一次 · 改:<code>/copy buyers 2</code> "
            "<code>/copy age 24</code> <code>/copy amount 50</code>",
        ])

    def _cmd_paper(self) -> str:
        """跟单台账 + 现在的盈亏"""
        with store.get_conn() as conn:
            rows = store.copy_ledger(conn, limit=15)
        if not rows:
            return "🧪 <b>跟单台账</b>\n还没有信号。/copy 看当前参数(默认未开启)"

        lines, total_in, total_now, n = ["🧪 <b>跟单台账</b>"], 0.0, 0.0, 0
        for r in rows:
            sym = _esc((r["token_symbol"] or "?").lstrip("$"))
            got = pnl(r["entry_mcap"], r["now_mcap"], r["amount_usd"])
            # ⚠️ 每个状态都要有自己的符号。落到默认的 "•" 就意味着
            #    「结果未知、请去核对」这种最需要被看见的行,看起来和别的一模一样。
            tag = {"paper": "🧪", "pending": "⏳", "filled": "✅",
                   "rejected": "🚫", "failed": "❌", "expired": "⌛",
                   "executing": "🔄", "auto_queued": "📥", "auto_executing": "🔄",
                   "unknown": "❔"}.get(r["status"], "•")
            seg = [f"{tag} <b>${sym}</b>"]
            if got:
                value, x = got
                total_in += r["amount_usd"]
                total_now += value
                n += 1
                seg.append(f"{'📈' if x >= 1 else '📉'} {x:.2f}x")
                seg.append(f"{_money(r['amount_usd'])} → {_money(value)}")
                # ⚠️ 行情冻住了要说出来。token_snapshot 只覆盖"名单里还有人持有"
                #    的币,清仓后不再更新 —— 那个 2.5x 可能是三天前的 2.5x,
                #    而不带标记的话它和实时价长得一模一样。
                stale = _stale_mark(r["mcap_at"] if "mcap_at" in r.keys() else None)
                if stale:
                    seg.append(stale)
            else:
                # 拿不到现价(名单里已经没人持有了)—— 说清楚,别显示成 0
                seg.append(f"{_money(r['amount_usd'])} · 现价未知")
            seg.append(_ago_short(r["triggered_at"]))
            lines.append("  ".join(seg))

        if n:
            x = total_now / total_in if total_in else 0
            lines.append(f"\n合计 {n} 单 · {_money(total_in)} → {_money(total_now)}"
                         f" · <b>{x:.2f}x</b>")
        lines.append(f"{_esc('⚠️')} 纸上盈亏按市值折算,"
                     f"<b>没算手续费/滑点/gas</b>,真实结果只会更差")
        return "\n".join(lines)

    def _cmd_star(self, arg: str, on: bool) -> str:
        """/star | /unstar <handle> —— 特别关注。纯展示开关,不影响任何判定"""
        key = (arg or "").strip()
        if not key:
            return f"用法: /{'star' if on else 'unstar'} &lt;handle&gt;"
        with store.get_conn() as conn:
            _, msg = store.set_starred(conn, key, on)
        return _esc(msg)

    def _cmd_tin(self, arg: str) -> str:
        """
        /tin [handle] —— 「转入逐条推送」的开关与清单。

        这个开关的用途:有些人的成交发生在别处,币是**转进来**的,FOMO 这边看到的
        是一条转入而不是买入。对这些人来说,转入到账才是他动手的那一刻。
        ⚠️ 但推送文案里只摆事实(谁、什么币、多少、收到时市值、从哪个地址来、多久之前),
           **绝不替用户断言这是买入、也不猜他用的什么工具** —— 报文里没有任何证据。
        ⚠️ 默认全员关闭,而且必须一个个开:全名单打开实测约 807 条/天,
           那会把真正要看的买卖推送整个淹掉(见 poller._persist 里的实测数)。

        三种写法,**语法上互不重叠**(理由见 store.set_transfer_watch):
          /tin <handle> <金额>   开启并设成这个金额;已经开着就**只改门槛**
          /tin <handle>          不带金额 = 开/关切换
          /tin                   清单,每人显示各自的门槛
        """
        key = (arg or "").strip()
        default_min = self._settings.fomo_transfer_watch_min_usd
        parts = key.split()
        if len(parts) > 2:
            return _TIN_USAGE
        with store.get_conn() as conn:
            if len(parts) == 2:
                ok, min_usd, err = _parse_tin_min_usd(parts[1])
                if not ok:
                    return err
                # ⚠️ on=True 而不是 None:带金额时**只开不切**。写成切换的话
                #    「已开在 $30000、再发 /tin alice 200」就分不出是关掉还是改门槛了。
                _, msg = store.set_transfer_watch(
                    conn, parts[0], True, min_usd=min_usd,
                    default_min_usd=default_min, max_on=TRANSFER_WATCH_MAX)
                return _esc(msg)
            if parts:
                # 不带金额 = 开关:已开就关。上限由 poller 的请求预算决定
                _, msg = store.set_transfer_watch(
                    conn, parts[0], default_min_usd=default_min, max_on=TRANSFER_WATCH_MAX)
                return _esc(msg)
            rows = [r for r in store.list_active_users(conn) if r["watch_transfer_in"]]

        if not rows:
            return (
                "📥 <b>转入逐条推送</b>:当前一个人都没开\n"
                f"/tin &lt;handle&gt; &lt;金额&gt; 打开 —— 他每收到一笔 ≥ 这个数的币就单独推一条"
                f"(不写金额就用全局默认 ${default_min:,.2f})。\n"
                f"最多同时开 {TRANSFER_WATCH_MAX} 人(每多一人,每轮多一个请求)。\n"
                f"{_TIN_FREQ_HINT}"
            )
        lines = [f"📥 <b>转入逐条推送</b>({len(rows)}/{TRANSFER_WATCH_MAX} 人)"]
        for i, r in enumerate(rows, 1):
            name = r["display_name"] or r["handle"]
            # ⚠️ 门槛逐人显示,而且「(默认)」这三个字不能省:它标的是"这个人跟着
            #    .env 走",与"显式设成了同一个数"是两回事 —— 后者改 .env 不会动他。
            shown = store.fmt_transfer_min_usd(r["transfer_in_min_usd"], default_min)
            lines.append(f"{i}. <b>{_esc(name)}</b> @{_esc(r['handle'])} · 门槛 {_esc(shown)}")
        lines.append("/tin &lt;handle&gt; &lt;金额&gt; 改门槛 · /tin &lt;handle&gt; 关闭。")
        lines.append(_TIN_FREQ_HINT)
        return "\n".join(lines)

    def _cmd_pump(self, arg: str) -> str:
        """
        /pump —— pump.fun 买卖监控的名单与增删。

        ⚠️⚠️ 这是**另一个平台的另一张名单**,与 /add /del /list 操作的 watch_users
           没有任何关系:那张表的主键是 FOMO 的 UUID,被 fomo_events / user_token_stats /
           copytrade 一路当聚合键;pump 的人混进去会让 poller 拿 Solana 钱包去问
           fomo.family,404 之后把这个人标成「账号已不存在」(表注释里写了完整理由)。
           所以命令也刻意分开:错的命令名会把人加到错的平台去。
        ⚠️ 只推通知,**永远不下单**(与 /copy 那条链没有任何交集)。
        """
        sub, _, rest = (arg or "").strip().partition(" ")
        sub = sub.strip().lower()
        rest = rest.strip()
        if sub in ("add", "del", "rm", "remove"):
            if not rest:
                return f"用法: /pump {_esc(sub)} &lt;名字或钱包&gt;"
            return (self._pump_add(rest) if sub == "add" else self._pump_del(rest))
        if sub:
            return "用法: /pump 看名单 · /pump add &lt;名字或钱包&gt; · /pump del &lt;名字或钱包&gt;"
        return self._pump_list()

    def _pump_list(self) -> str:
        s = self._settings
        with store.get_conn() as conn:
            rows = store.list_pump_users(conn)
        head = "🎯 <b>pump.fun 监控</b>"
        if not rows:
            return (
                f"{head}:当前一个人都没加\n"
                "/pump add &lt;名字或钱包&gt; 加人 —— 他在 pump.fun 上每成交一笔、"
                "每发一条观点就推一条。\n"
                f"门槛 ${s.fomo_pump_min_usd:,.2f} · 巡检 {s.fomo_pump_interval_sec}s\n"
                "⚠️ 第一轮只记下当前持仓与已有观点、<b>一条都不推</b>,之后的才会推。"
            )
        lines = [f"{head}({len(rows)} 人) · 门槛 ${s.fomo_pump_min_usd:,.2f}"
                 f" · 巡检 {s.fomo_pump_interval_sec}s"]
        # ⚠️ 必须说出来:名单加了人却没开开关,用户会一直等一条永远不来的推送。
        # ⚠️⚠️ 买卖与观点是**两个独立开关**,所以分别报 —— 只报一个的话,
        #    另一路的"开了却没推送"在界面上完全没有解释。
        if not s.fomo_pump_enabled:
            lines.append("⚠️ <b>买卖开关未打开</b>,不会推成交 —— "
                         "到 .env 设 <code>FOMO_PUMP_ENABLED=true</code> 后重启")
        if not s.fomo_pump_callout_enabled:
            lines.append("⚠️ <b>观点开关未打开</b>,不会推观点 —— "
                         "到 .env 设 <code>FOMO_PUMP_CALLOUT_ENABLED=true</code> 后重启")
        for i, r in enumerate(rows[:MAX_LIST_ROWS], 1):
            name = r["username"] or r["user_id"]
            # 播种状态要显示:没播过种的人这一轮不会有任何推送,不说清楚会被当成坏了。
            # ⚠️ 两路**各播各的种**(库里两列),所以两个状态都要报:一个人可能买卖
            #    早就就绪、而观点开关刚打开还在首轮记录 —— 只报一个会让另一路的静默
            #    看起来像坏了。
            state = "✅就绪" if r["seeded"] else "⏳首轮记录中"
            c_state = "✅就绪" if r["callout_seeded"] else "⏳首轮记录中"
            lines.append(f"{i}. <b>{_esc(name)}</b> · 买卖{state} · 观点{c_state}")
        if len(rows) > MAX_LIST_ROWS:
            lines.append(f"…另有 {len(rows) - MAX_LIST_ROWS} 人未显示")
        lines.append("/pump del &lt;名字或钱包&gt; 移出。")
        return "\n".join(lines)

    def _pump_add(self, key: str) -> str:
        """
        /pump add <名字或钱包> —— 反查 → 落库 → 回执。

        ⚠️ 主键存 pump 的 userId(UUID),**不是用户名**:接口自己返回
           last_username_update_timestamp,证明用户名可被本人改 ——
           拿它当主键等于改个名就换了个人。
        ⚠️ 两个 canonical 钱包都要存:EVM 链上的成交只挂在 canonical_evm_wallet 名下,
           只存 SVM 的话那半边成交永久静默丢失(pumpfun.py 顶部有实测)。
        """
        profile = self._pump().resolve_user(key)
        if profile is None:
            return (f"❌ pump.fun 上找不到 <b>{_esc(key)}</b>"
                    "(名字拼错?或者他改名了 —— 换钱包地址试试)")
        with store.get_conn() as conn, store.tx(conn):
            added = store.add_pump_user(conn, profile.user_id, profile.username,
                                        profile.svm_wallet, profile.evm_wallet)
        name = _esc(profile.username or profile.user_id)
        if not added:
            return f"ℹ️ <b>{name}</b> 已在 pump.fun 监控中"
        return (
            f"✅ 已加入 pump.fun 监控 <b>{name}</b>\n"
            "⏳ 下一轮先把他<b>当前的持仓与已有观点记为已知、一条都不推</b>,"
            "再之后的买卖与观点才会逐条推送"
        )

    def _pump_del(self, key: str) -> str:
        """/pump del —— 软删除(行留着,再 add 回来会重新播种,不会补推这期间的变动)。"""
        with store.get_conn() as conn:
            row = store.find_pump_user(conn, key)
            if row is None:
                return f"❌ pump.fun 监控里没有 <b>{_esc(key)}</b>"
            with store.tx(conn):
                removed = store.remove_pump_user(conn, row["user_id"])
        name = _esc(row["username"] or row["user_id"])
        if not removed:
            return f"ℹ️ <b>{name}</b> 本来就不在 pump.fun 监控中"
        return f"✅ 已移出 pump.fun 监控 <b>{name}</b>"

    def _pump(self):
        """
        懒建 PumpClient —— 没人用 /pump 就不必付 curl_cffi 的加载代价。

        ⚠️ 与推送侧的 PumpWatcher **各用各的实例**:两者跑在不同线程上,
           而 libcurl 的 easy handle 不能跨线程共用(PumpClient 内部按线程分 Session,
           共用一个实例其实也安全,但分开更省心且没有任何代价)。
        """
        if self._pump_client is None:
            from src.pumpfun import PumpClient

            self._pump_client = PumpClient()
        return self._pump_client

    def _cmd_list(self) -> str:
        with store.get_conn() as conn:
            rows = store.list_active_users(conn)
            if not rows:
                return "📋 监控名单为空,用 /add &lt;handle&gt; 添加"
            # ⚠️ 特别关注的人排在最前:名单几十人时,/list 的价值就在于一眼看到重点
            rows = sorted(rows, key=lambda r: (0 if r["starred"] else 1, r["added_at"]))
            n_star = sum(1 for r in rows if r["starred"])
            head = f"📋 <b>监控名单</b>({len(rows)} 人"
            head += f" · ⭐ {n_star} 人)" if n_star else ")"
            lines = [head]
            for i, r in enumerate(rows[:MAX_LIST_ROWS], 1):
                ready = "✅就绪" if r["stats_ready"] else "⏳建立中"
                n_token = store.stats_row_count(conn, r["user_id"])
                name = r["display_name"] or r["handle"]
                star = "⭐ " if r["starred"] else ""
                lines.append(
                    f"{i}. {star}<b>{_esc(name)}</b> @{_esc(r['handle'])} · {ready} · "
                    f"{n_token} 币 · {_day_str(r['added_at'])}"
                )
            if len(rows) > MAX_LIST_ROWS:
                lines.append(f"…另有 {len(rows) - MAX_LIST_ROWS} 人未显示")
        return "\n".join(lines)

    def _cmd_status(self) -> str:
        s = self._settings
        with store.get_conn() as conn:
            active = len(store.list_active_users(conn))
            ready = len(store.ready_user_ids(conn))
            # 今日事件数:ingested_at 是 ISO 字符串,与日期前缀做字典序比较即可
            # ⚠️ 排掉转入/转出,与网页版「今日事件」卡片同一口径(见 web.queries.dashboard):
            #    这个数字回答的是"名单今天动了多少次",而转账是 2026-08 才加的采集,
            #    算进来会让它虚增约 10 倍 —— 同一个数字换了含义,比数字错了更难发现。
            today_key = datetime.now(UTC).strftime("%Y-%m-%d")
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM fomo_events "
                "WHERE ingested_at >= ? AND event_type NOT IN (?, ?)",
                (today_key, EVENT_TRANSFER_IN, EVENT_TRANSFER_OUT),
            ).fetchone()["n"]
            unsent = conn.execute(
                "SELECT COUNT(*) AS n FROM fomo_events WHERE sent = 0"
            ).fetchone()["n"]
            last_ev = conn.execute("SELECT MAX(ingested_at) AS t FROM fomo_events").fetchone()["t"]

        # Poller 契约里没有 last_tick_at,拿不到就退化成"最近一条事件时间",绝不因此报错
        tick_at = getattr(self._poller, "last_tick_at", None) or last_ev or "—"
        lines = [
            "📊 <b>运行状态</b>",
            f"👥 名单 {active} 人 · 基线就绪 {ready} 人",
            f"📨 今日事件 {today} 条 · 待补发 {unsent} 条",
            f"⏱ 最近 tick {_esc(tick_at)}",
            f"🔌 client={_esc(s.fomo_client_impl)} · 轮询 {s.fomo_poll_interval_sec}s",
        ]
        if ready < active:
            lines.append(f"⏳ {active - ready} 人基线未就绪,其买入暂不打徽章、不计入共识")
        return "\n".join(lines)

    def _cmd_who(self, arg: str) -> str:
        """
        /who <CA> [链] —— 名单里谁买过这个币。
        把"具体是谁"这个真需求从推送路径里移出来:推送里只留一个数字,细节按需查。
        """
        parts = (arg or "").split()
        if not parts:
            return "用法: /who &lt;CA&gt; [链]  例: /who A13oRB9FF…pump solana"
        ca = normalize_token_address(parts[0])
        net = normalize_network(parts[1]) if len(parts) > 1 else None
        if not ca:
            return "⚠️ 请给出代币合约地址"

        blocks: list[str] = []
        with store.get_conn() as conn:
            if net:
                nets = [net]
            else:
                # store.py 是冻结契约,没有"按地址反查链"的函数。
                # 这是一条只读 SELECT、不含任何判定逻辑,就地写比动冻结文件代价小。
                nets = [
                    r["network_id"]
                    for r in conn.execute(
                        "SELECT DISTINCT network_id FROM user_token_stats WHERE token_address = ?",
                        (ca,),
                    ).fetchall()
                ]
            for n in nets:
                rows = store.list_buyers(conn, n, ca)
                if not rows:
                    continue
                lines = [f"🔎 <b>{_esc(n)}</b> · {len(rows)} 人买过"]
                for i, r in enumerate(rows, 1):
                    name = r["display_name"] or r["handle"]
                    lines.append(
                        f"{i}. <b>{_esc(name)}</b> · {r['buy_count']} 次 · {_day_str(r['first_buy_at'])}"
                    )
                blocks.append("\n".join(lines))

        if not blocks:
            return f"🔎 名单里没人买过这个币(或该币不在任何人的基线内)\n<code>{_esc(ca)}</code>"
        # CA 独占最后一行且整行是 <code>:点击即复制,不依赖网络(设计文档 §10.3)
        return "\n".join(blocks) + f"\n<code>{_esc(ca)}</code>"

    # ============================================================
    # /ca <合约地址>
    # ============================================================
    def _cmd_ca(self, arg: str) -> str:
        """
        /ca <合约地址> [链] —— 粘一个合约地址上来,看全站 FOMO 用户怎么看 + 名单有没有人碰过。

        两段互相独立、互不依赖:
          观点区 —— 实时查 client.get_token_thesis,**任何地址都能查**,不要求本地认识
                    这个币(这是本命令存在的意义:场景是刚在 Twitter 上看到一个陌生 CA)。
          本地名单区 —— 查本地库,只覆盖名单里的人买过/持有过的币。多数地址查不到,
                    这是正常情况不是 bug,必须显式说"无人持有"而不是留空当没这回事。

        ⚠️ 链解析(按优先级,命中一条就不再往下猜):
           1) 用户给了第二个参数 —— 完全按他说的,不再猜。
           2) 没给,但本地库已经见过这个地址(user_token_stats / token_snapshot 有它)
              —— 链直接从本地拿,**不发一个探测请求**,这是最常见的省请求路径
              (实测:名单碰过的币占比不到 1/3,但只要碰过就不用猜)。
           3) 都没有 —— 0x 开头按 CA_EVM_GUESS_ORDER 依次试(实测证实猜错链只会拿到
              空列表、不会拿到别的币的数据,见该常量的注释),命中就停手;
              非 0x(base58)只有 Solana 一条链,直接查、不用猜。
           最坏情况(全新地址、猜到最后一条才中,或者哪条都不中)是
           len(CA_EVM_GUESS_ORDER) = 6 次请求;再加上 CA_GUESS_BUDGET_SEC 这道
           总时长闸门,单条命令不会把后面排队的命令拖过 STALE_COMMAND_SEC。
        """
        parts = (arg or "").split()
        if not parts:
            return "用法: /ca &lt;合约地址&gt; [链]  例: /ca 0xfc6e...5777 bsc"
        ca = normalize_token_address(parts[0])
        if not ca:
            return "⚠️ 请给出代币合约地址"
        forced_net = normalize_network(parts[1]) if len(parts) > 1 else None
        # 锚点在这里一次性收口,下面每条早退路径共用它 —— 地址是用户可控的无界字符串
        # (normalize_token_address 对非 0x/42 位的输入原样透传),不收口的话
        # 这几条**不走 _ca_assemble** 的早退路径就是出口不变式之外的洞。
        anchor = _ca_anchor(ca)

        local_nets, local_symbol, local_lines = self._ca_local(ca)

        if forced_net:
            candidates = [forced_net]
        elif local_nets:
            candidates = local_nets                 # 本地已经见过,链是确定的,不猜
        elif ca.startswith("0x"):
            candidates = list(CA_EVM_GUESS_ORDER)
        else:
            candidates = ["solana"]

        items, used_net, tried, err, unfinished = self._ca_fetch(ca, candidates)

        # 接口挂了、本地又没有任何记录:确实没东西可说,把错误原样交给用户。
        # ⚠️ 本地**有**记录时不能走这条路 —— 那半段数据早就算好了,不能被接口异常一起扔掉
        #   (与本命令 docstring 承诺的"两段互相独立、互不依赖"直接冲突)。
        if err is not None and not local_nets:
            return err

        # 全都没查到 + 本地也不认识:说不清是"没这个币"还是"猜错了链",
        # 措辞必须把这份不确定性带出来,不能断言任何一边(见常量注释里的实测)
        # ⚠️ 这两条回执逐行过 _ca_fit_line:链名(用户给的第二个参数,normalize_network
        #    对未收录的值原样透传)和已试链名同样无界,而这里不经过 _ca_assemble,
        #    出口不变式得自己在这一行上成立(行数是源码里写死的 2~4,总长必然在预算内)。
        if not items and not local_nets:
            if forced_net:
                return "\n".join([
                    _ca_fit_line(f"🔎 {anchor} · {_esc(_chain_name(forced_net))}"),
                    "这条链上没查到观点,本地也没有记录 —— 可能是新币,也可能还没人发观点",
                ])
            miss = [
                _ca_fit_line(f"🔎 {anchor}"),
                _ca_fit_line(f"{_esc('/'.join(tried))} 都没查到观点,本地也没有记录"),
                "可能是全新的币、还没人发观点,也可能是猜错了链 —— 可指定:/ca &lt;地址&gt; &lt;链&gt;",
            ]
            if unfinished:
                miss.append(_ca_fit_line(f"(猜链耗时太久,{_esc('/'.join(unfinished))} 没试完)"))
            return "\n".join(miss)

        if items:
            # 头部用观点接口**回读到的真实值**,不用自己请求时传的猜测 ——
            # 猜对了两者一致,万一哪天服务端不再是"错链必空"这套行为,头部也不会跟着猜错
            first = items[0]
            sym = (_pick_str(first, "ticker") or "?").lstrip("$")
            chain_id = normalize_network(_pick_str(first, "networkId")) or used_net
        else:
            sym = (local_symbol or "?").lstrip("$")
            chain_id = forced_net or (local_nets[0] if local_nets else None)
        # ⚠️ ticker 与链名都可能来自服务端的任意字符串。这里的 _ca_clip 管的是**可读性**
        #    (head 段是 _ca_assemble 最后才砍的一段,不压一压就白吃掉别人的展示位);
        #    "撑不破消息"那一半由 _ca_assemble 的出口不变式负责,不靠这里。
        head = [f"<b>${_ca_clip(sym, CA_TICKER_CHARS)}</b> · "
                f"{_ca_clip(_chain_name(chain_id), CA_CHAIN_CHARS)}"]

        rows: list[dict] = []
        if items:
            # 一位作者一行:8 个位置要给 8 个人,不是给一个人的 8 条刷屏
            rows = sorted(_ca_one_row_per_author(items), key=_ca_rank_key, reverse=True)
            # ⚠️ 措辞只陈述"我们取到了多少",不陈述全站总数:这个数来自 limit 抓取窗口,
            #    观点超过窗口时它和下面的省略条数都会少报 —— 说成事实就是误导。
            got_line = f"📋 取到 {len(items)} 条观点 · {len(rows)} 位作者"
            if len(items) >= CA_THESIS_FETCH_LIMIT:
                got_line += f"(只取最近 {CA_THESIS_FETCH_LIMIT} 条,实际可能更多)"
            head.append(got_line)
        elif err is not None:
            head.append(f"💭 观点没拉到:{err}")     # 本地那段照常出,不跟着一起丢
        else:
            miss = f"💭 没查到观点(已试 {_esc('/'.join(tried))})"
            if unfinished:
                miss += f",{_esc('/'.join(unfinished))} 没试完"
            head.append(miss)

        return _ca_assemble(head, rows, ["", *local_lines], anchor)

    def _ca_fetch(self, ca: str, candidates: list[str]):
        """
        依次试链拉观点。返回 (items, 命中的链, 试过的链, 出错文案, 因超预算没试的链)。

        ⚠️ 出错时**返回**错误文案而不是直接把整条回执替换掉 —— 本地名单区已经算好了,
           要不要连它一起丢是调用方的判断,不是这里的。
        ⚠️ 总时长闸门:见 CA_GUESS_BUDGET_SEC。第一条链无论如何都要试,预算是防"猜到
           天荒地老",不是让命令一个请求都不发。
        """
        items: list[dict] = []
        used_net: str | None = None
        tried: list[str] = []
        err: str | None = None
        deadline = time.monotonic() + CA_GUESS_BUDGET_SEC
        for i, net in enumerate(candidates):
            if i and time.monotonic() > deadline:
                logger.warning("/ca 猜链超时,剩下 {} 不再试 | {}", candidates[i:], ca[:16])
                return items, used_net, tried, err, list(candidates[i:])
            tried.append(net)
            try:
                # get_token_thesis 的 networkId 要原生数字 ID,不是本地归一化别名,见
                # _NETWORK_RAW_ID 的注释(实测踩过:传 "bsc" 直接 400)
                raw_net = _NETWORK_RAW_ID.get(net, net)
                got = self._client.get_token_thesis(ca, raw_net, limit=CA_THESIS_FETCH_LIMIT)
            except Exception as e:  # noqa: BLE001
                err = self._ca_error(ca, e)
                break
            items = [it for it in (got or []) if isinstance(it, dict)]
            if items:
                used_net = net
                break
        return items, used_net, tried, err, []

    def _ca_local(self, ca: str) -> tuple[list[str], str | None, list[str]]:
        """
        本地名单区。返回 (地址在本地出现过的链, 顺手捞到的一个 symbol, 渲染好的展示行)。

        前两项同时供 _cmd_ca 做链解析用 —— 本地已经认识的地址不用再去猜链、
        也不用再多发一个探测请求(见 _cmd_ca 的链解析说明)。

        ⚠️ store.py 是冻结契约,没有"按地址反查链"的函数,与 _cmd_who 同样的处理:
           这是条只读 SELECT、不含任何判定逻辑,就地写比动冻结文件代价小。
        """
        since = _iso_days_ago(CA_LOCAL_LOOKBACK_DAYS)
        with store.get_conn() as conn:
            local_nets = _local_token_nets(conn, ca)

            symbol = _local_token_symbol(conn, ca)

            blocks: list[str] = []
            for net in local_nets:
                buyers = store.token_buyers(conn, net, ca, since, limit=MAX_CA_LOCAL_BUYERS)
                snap = conn.execute(
                    "SELECT market_cap, max_market_cap FROM token_snapshot "
                    "WHERE network_id = ? AND token_address = ?",
                    (net, ca),
                ).fetchone()
                if not buyers and snap is None:
                    continue

                head = f"👥 你的名单 · {_esc(_chain_name(net))}"
                if buyers:
                    head += f":{len(buyers)} 人买过"
                blocks.append(head)

                now_mc = _f(snap["market_cap"]) if snap else None
                peak_mc = _f(snap["max_market_cap"]) if snap else None
                if now_mc is not None:
                    seg = [f"💎 现在市值 {_money(now_mc)}"]
                    # 明显回落过才提峰值,与 /hot 同一个取舍(见 _PEAK_MIN_RATIO 的注释)
                    if peak_mc is not None and peak_mc > now_mc * _PEAK_MIN_RATIO:
                        seg.append(f"峰值 {_money(peak_mc)}")
                    blocks.append("   " + " · ".join(seg))

                known_usd = self._ca_buyers_with_usd(conn, net, ca, since)
                # 谁先买的排前面(token_buyers 本身就按首买时间正序返回)
                for b in buyers:
                    row = [f"@{_esc(b['who'] or '?')}"]
                    entry_mc = _f(b["mcap"])
                    if entry_mc is not None:
                        row.append(f"💎{_money(entry_mc)} 进场")
                        # 每个买家自己的进场市值不同,倍数必须逐人算,不能借用 peak/now 一概而论
                        if now_mc is not None and entry_mc > 0:
                            row.append(f"现 {now_mc / entry_mc:.1f}x")
                    usd = _f(b["usd"])
                    if usd is not None and b["who"] in known_usd:
                        row.append(_money(usd) + (f"({b['buys']} 笔)" if b["buys"] > 1 else ""))
                    else:
                        # 金额一笔都没解析出来 —— 只报"买了几笔"这个确实知道的事。
                        # 打 $0.00 会被读成"他只买了 0 块钱",那是凭空造出来的假事实
                        row.append(f"{b['buys']} 笔")
                    blocks.append("   " + " · ".join(row))

        if not blocks:
            blocks = ["👥 你的名单:无人持有"]
        return local_nets, symbol, blocks

    @staticmethod
    def _ca_buyers_with_usd(conn, net: str, ca: str, since: str) -> set[str]:
        """
        这批买家里,**至少有一笔真的解析出了买入金额**的是谁(返回 who 集合)。

        ⚠️ 为什么需要它:store.token_buyers 的 `SUM(COALESCE(amount_usd, 0))` 会把
           "一笔都没解析出金额"(amount_usd 全 NULL)压成 0.0,Python 侧的
           `usd is None` 因此**永远为假** —— is None 这道铁律被 SQL 里的 COALESCE
           架空了,于是"金额未知"和"真的只买了 0 元"在渲染上再也分不开。
        ⚠️ store.py 是冻结契约,只能在这里补一条**同谓词**的只读 SELECT
           (与 _cmd_who / _ca_local 里那两条同样的处理)。谓词必须与 token_buyers
           完全一致,否则这边判"已知"、那边算出来的却是另一批行的和。
        """
        countable = ",".join("?" * len(COUNTABLE_REASONS))
        rows = conn.execute(
            f"""
            SELECT COALESCE(MAX(e.user_handle), MAX(e.handle)) AS who
            FROM fomo_events e
            JOIN watch_users w
              ON w.user_id = e.user_id AND w.active = 1 AND w.stats_ready = 1
            WHERE e.event_type = 'BUY' AND e.network_id = ? AND e.token_address = ?
              AND e.event_ts >= ?
              AND COALESCE(e.badge_reason, '') IN ({countable})
            GROUP BY e.user_id
            HAVING COUNT(e.amount_usd) > 0
            """,
            (net, ca, since, *COUNTABLE_REASONS),
        ).fetchall()
        return {r["who"] for r in rows}

    def _ca_error(self, ca: str, e: Exception) -> str:
        """
        把 client 层异常翻成人话。与 _resolve_error 同构,但这里查的是合约地址不是用户
        handle ——"找不到用户 @xxx" 这种措辞对地址没有意义,所以单独写一份而不是改
        影响 /add /following /top 的那个公用函数。
        """
        from src.auth import AuthError
        from src.client import FomoAPIError

        if isinstance(e, AuthError):
            logger.warning("get_token_thesis 鉴权失败 | {}", e)
            return "❌ 登录态失效,请到服务器执行 <code>.\\bot.ps1 --login</code> 重新登录"

        status = getattr(e, "status", None) or getattr(e, "status_code", None)
        text = str(e)
        if status in (401, 403) or "401" in text or "403" in text:
            return (
                "❌ FOMO 接口拒绝访问(登录态失效,或 Cloudflare 拦截)\n"
                "先试 <code>.\\bot.ps1 --login</code>;仍不行则把 FOMO_CLIENT_IMPL 改成 playwright"
            )
        if isinstance(e, FomoAPIError):
            logger.warning("get_token_thesis 失败 | {} | {}", ca[:16], e)
            return f"❌ FOMO 接口异常: {_esc(text[:150])}"
        logger.exception("get_token_thesis 未知异常 | {}", ca[:16])
        return f"❌ 查询失败: {_esc(text[:150])}"

    # ============================================================
    def _cmd_chips(self, arg: str) -> str:
        """
        /chips <合约地址> [链] —— 筹码分布:FOMO 全平台 + 本 bot 监控名单各持有多少。

        ## 这条命令的核心是**诚实**,不是数字
        持有人榜(/hodlers/top)被服务端钳在 100 条,且还有一道约 $2 的持仓市值下限,
        所以我们手上这份名单**可能只是全部持有人的一小撮**。判据只有一条:
        len(topHolders) == totalHolders 时统计是**精确**的,否则只是**下界**。
        用户见过的第三方工具在 CATE 这种币上只统计 98/79891(覆盖 0.12%)却照样显示成
        "持仓占比",不加任何提示 —— 我们必须标出来。两套文案在
        _chips_platform_lines / _chips_watch_lines 里泾渭分明,并且有测试守住。

        ## 三个数据源,失败互不牵连
          分子 —— /hodlers/top,平台与名单**共用同一份**(见 MAX_CHIPS_MEMBER_ROWS 上面的说明)
          分母 —— /public/proxy/filterTokens 的 totalSupply。**匿名请求**,不占用监控进程
                  共用的登录态;拿不到就退到本地 token_snapshot 的 市值÷价格 推算,
                  推算值必须显式标注(那是 FDV 反推,不是权威值)。
          名单 —— 本地 watch_users,按 **user.id** 与 topHolders 匹配(见 _chips_match_members)

        分母挂了不影响持有人数与持仓数量;分子挂了占比无从谈起,但链名、地址、
        以及"为什么没拿到"照样给到用户手里。

        ⚠️ 链解析沿用 /ca 的三级做法(用户指定 → 本地库已知 → 依次试),共用
           _local_token_nets:同一个地址在两条命令下必须定到同一条链。
        """
        parts = (arg or "").split()
        if not parts:
            return "用法: /chips &lt;合约地址&gt; [链]  例: /chips 0xfc6e...5777 bsc"
        ca = normalize_token_address(parts[0])
        if not ca:
            return "⚠️ 请给出代币合约地址"
        forced_net = normalize_network(parts[1]) if len(parts) > 1 else None
        # 与 /ca 同样的理由:地址是用户可控的无界字符串,锚点在这里一次性收口,
        # 下面每条**不走 _ca_assemble** 的早退路径共用它
        anchor = _ca_anchor(ca)

        with store.get_conn() as conn:
            local_nets = _local_token_nets(conn, ca)
            local_symbol = _local_token_symbol(conn, ca)
            members = _load_watch_members(conn)
            # 💊 那半边的名单。⚠️ 与上面那份是**两个平台的两份名单**,绝不能互相顶替:
            #    watch_users 是 FOMO 的人(按 handle 加的),pump_watch_users 是
            #    pump.fun 的人(按 pump 用户名/钱包加的),同一个自然人在两边是两条记录。
            pump_members = _load_pump_members(conn)

        if forced_net:
            candidates = [forced_net]
        elif local_nets:
            candidates = local_nets                 # 本地已经见过,链是确定的,不猜
        elif ca.startswith("0x"):
            candidates = list(CA_EVM_GUESS_ORDER)
        else:
            candidates = ["solana"]

        data, used_net, tried, err, unfinished = self._chips_fetch(ca, candidates)
        net = used_net or forced_net or (local_nets[0] if local_nets else None)

        # 一条链都没定下来 = 连分母都不知道该查哪条链,再往下走只会拼出一条什么都没说的消息。
        # 措辞必须把"没这个币"和"猜错了链"的不确定性一起带出来(与 /ca 同一条理由)
        if err is not None and net is None:
            return "\n".join([_ca_fit_line(f"🔎 {anchor}"), err])
        if net is None:
            miss = [
                _ca_fit_line(f"🔎 {anchor}"),
                _ca_fit_line(f"{_esc('/'.join(tried))} 都没查到持有人"),
                "可能是全新的币、还没人买,也可能是猜错了链 —— 可指定:/chips &lt;地址&gt; &lt;链&gt;",
            ]
            if unfinished:
                miss.append(_ca_fit_line(f"(猜链耗时太久,{_esc('/'.join(unfinished))} 没试完)"))
            return "\n".join(miss)

        # 分母:先问接口(匿名,不花会话),拿不到再退本地推算。两者都拿不到就是 None,
        # 下游据此让整个占比消失 —— 绝不补 0
        meta = self._chips_meta(ca, net)
        supply = total_supply_of(meta)
        estimated = False
        if supply is None:
            supply = _chips_local_supply(ca, net)
            estimated = supply is not None
        symbol = (_chips_meta_symbol(meta) or local_symbol or "?").lstrip("$")

        st = _chips_stats(data, members, supply)
        head = [f"<b>${_ca_clip(symbol, CA_TICKER_CHARS)}</b> · "
                f"{_ca_clip(_chain_name(net), CA_CHAIN_CHARS)}"]
        head += _chips_platform_lines(st, err)
        head += _chips_watch_lines(st, err)
        # ⚠️⚠️ 💊 pump 那半边**只接在这条成功路径上**:上面几条早退分支
        #    (一条链都没定下来 / 接口挂了)返回的是**诊断消息**,在一条"没查到、
        #    可能猜错链"的消息后面挂一段别的平台的筹码只会更难读。
        #    代价是:一个只在 pump 上、FOMO 完全没有的币仍然看不到 💊 那半边 ——
        #    这是刻意的取舍(见 README「已知取舍」),换来的是既有行为一个字节都不变。
        # ⚠️⚠️ 它走 **mid** 而不是 head(本轮 J1 修的 BLOCKER)。接进 head 的那一版
        #    把 🏦 FOMO 名单的成员明细行(= _ca_assemble 的 body)整段挤到了
        #    「👥 你的 pump 名单」表头**下面**,而两边行形一模一样 ——
        #    FOMO 的人被读成 pump 平台的持有人。不变式在 _ca_assemble 的 docstring 里,
        #    tests/test_bot_chips_pump.py 有一条逐行顺序的用例钉着。
        # ⚠️ 它仍然排在 FOMO 半边之后,所以 pump 挂掉/超时最坏只是少了 💊 那几行。
        pump_lines = _pump_chips_lines(self._pump, ca, pump_members)

        tail: list[str] = []
        # ⚠️ 两条注脚都在解释「占比这个数怎么来的」,所以**共用同一道守卫**:
        #    分子没拿到(err)或压根没有持有人(empty)时,消息里根本不存在占比,
        #    再挂一句"拿不到供应量"或"占比是近似值"都是答非所问。
        #    这道守卫曾经只加在前一条分支上,于是"分子挂了 + 本地有行情"会渲染出
        #    一句凭空的"占比是近似值",而上面一个百分号都没有。
        if err is None and not st["empty"]:
            if supply is None:
                tail = ["", "⚠️ 拿不到总供应量,只能报持有人与数量,占比算不出来"]
            elif estimated:
                tail = ["", "ℹ️ 总供应量取自本地行情推算(市值÷价格),占比是近似值"]

        return _ca_assemble(head, st["matched"], tail, anchor,
                            render=_chips_member_row, max_rows=MAX_CHIPS_MEMBER_ROWS,
                            omit_fmt="…按持仓数量排序,还有 {n} 人未显示",
                            mid=pump_lines)

    def _chips_fetch(self, ca: str, candidates: list[str]):
        """
        依次试链拉持有人榜。返回 (命中的那份数据, 命中的链, 试过的链, 出错文案, 没试完的链)。

        ⚠️ "命中"的判据是**有人持有**(topHolders 非空,或 totalHolders > 0),
           而不是"请求成功"—— 猜错链时服务端照样 200,只是给一份空榜(与 /ca 同一套行为)。
           代价是:用户**指定了链**、而这个币在那条链上真的一个人都没有时,我们也走
           "没命中"这条路。这是刻意的取舍 —— 猜链阶段分不清"空"和"错",而指定链时
           candidates 只有一条,tried 里就是他给的那条,文案不会误导。
        ⚠️ 与 _ca_fetch 共用 CA_GUESS_BUDGET_SEC 这道总时长闸门:命令层严格串行,
           单条命令阻塞太久会让排在后面的命令超过 STALE_COMMAND_SEC 被静默丢弃。
        """
        used_net: str | None = None
        tried: list[str] = []
        err: str | None = None
        deadline = time.monotonic() + CA_GUESS_BUDGET_SEC
        for i, net in enumerate(candidates):
            if i and time.monotonic() > deadline:
                logger.warning("/chips 猜链超时,剩下 {} 不再试 | {}", candidates[i:], ca[:16])
                return {}, used_net, tried, err, list(candidates[i:])
            tried.append(net)
            try:
                # 与 get_token_thesis 同一条:networkId 要 FOMO 原生的数字链 ID,不是本地别名
                raw_net = _NETWORK_RAW_ID.get(net, net)
                data = self._client.get_top_holders(ca, raw_net)
            except Exception as e:  # noqa: BLE001
                err = self._chips_error(ca, e)
                break
            if not isinstance(data, dict):
                continue
            holders = [h for h in (data.get("topHolders") or []) if isinstance(h, dict)]
            if holders or (_chips_int(data.get("totalHolders")) or 0) > 0:
                return data, net, tried, err, []
        return {}, used_net, tried, err, []

    def _chips_meta(self, ca: str, net: str) -> dict:
        """
        分母那一路。**整段吞异常**:它与分子是两个互相独立的数据源,
        分母挂掉只该让占比消失,绝不能把已经拿到手的持有人数一起拖走。
        """
        try:
            return self._client.get_token_meta(ca, _NETWORK_RAW_ID.get(net, net)) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("/chips 取总供应量失败(降级为本地推算) | {} | {}", ca[:16], e)
            return {}

    def _chips_error(self, ca: str, e: Exception) -> str:
        """把 client 层异常翻成人话。与 _ca_error 同一套分类,只是换了端点名"""
        from src.auth import AuthError
        from src.client import FomoAPIError

        if isinstance(e, AuthError):
            logger.warning("get_top_holders 鉴权失败 | {}", e)
            return "❌ 登录态失效,请到服务器执行 <code>.\\bot.ps1 --login</code> 重新登录"
        status = getattr(e, "status", None) or getattr(e, "status_code", None)
        text = str(e)
        if status in (401, 403) or "401" in text or "403" in text:
            return (
                "❌ FOMO 接口拒绝访问(登录态失效,或 Cloudflare 拦截)\n"
                "先试 <code>.\\bot.ps1 --login</code>;仍不行则把 FOMO_CLIENT_IMPL 改成 playwright"
            )
        if isinstance(e, FomoAPIError):
            logger.warning("get_top_holders 失败 | {} | {}", ca[:16], e)
            return f"❌ FOMO 接口异常: {_esc(text[:150])}"
        logger.exception("get_top_holders 未知异常 | {}", ca[:16])
        return f"❌ 查询失败: {_esc(text[:150])}"


def _local_token_nets(conn, ca: str) -> list[str]:
    """
    本地在哪几条链上见过这个地址。/ca 与 /chips 的链解析第 2 级共用这一条 ——
    "本地已经认识就不用发探测请求"这个判据必须两条命令完全一致,
    否则同一个地址在两条命令下会定到不同的链,用户看到的是两份对不上的数据。

    ⚠️ 用 fomo_events 而不是 user_token_stats:后者只是前者按"算数的买入"聚合出来的
       派生表,任何一行 user_token_stats 必然对应一行 fomo_events,反过来不成立
       (SELL、非计数原因的 BUY 只落 fomo_events)。这里要的是"本地是否见过这个地址"
       这个更宽的信号,token_snapshot 再补上"持有但没见过买入事件"(比如转入)的那一小撮。
    ⚠️ store.py 是冻结契约,没有"按地址反查链"的函数,与 _cmd_who 同样的处理:
       这是条只读 SELECT、不含任何判定逻辑,就地写比动冻结文件代价小。
    """
    return [
        r["network_id"]
        for r in conn.execute(
            "SELECT DISTINCT network_id FROM fomo_events WHERE token_address = ? "
            "UNION SELECT DISTINCT network_id FROM token_snapshot WHERE token_address = ?",
            (ca, ca),
        ).fetchall()
        if r["network_id"]
    ]


# ============================================================
# /ca 辅助渲染(纯函数,不碰网络 / DB)
# ============================================================
def _ca_cost_usd(it: dict) -> float | None:
    """
    这位作者在这个币上**投入过多少本金**。排序键用;两半都算不出来时返回 None。

    ⚠️ 排序键绝不能用 usdValue。那是"卖完之后还剩多少",对已清仓的人**恒等于 0** ——
       不是他的仓位规模。真实抓包 100 条按 usdValue 排,6 条清仓条目(去重后是 5 位
       清仓作者)被整整齐齐钉在 #95–#100,而展示上限只有 8 行,
       于是"已清仓的人显示已实现盈亏"那条修复
       在真实数据上一行都渲染不出来。本金对两类人都可比:巨鲸投十几万排前面是**对的**,
       而一个投了 $5000 的清仓者能压过一个还拿着 $50 的人 —— 这才是这条命令的价值。

    两半各自反推(实测两个百分比各是对**自己那一半成本**算的):
      还拿着的一半: usdValue - unrealizedPnlUsd
        验算 @change:51474.66 - 16859.28 = 34615.38,16859.28 / 34615.38 = +48.70% ✓
      已卖掉的一半: realizedPnlUsd / (percentageRealizedPnl / 100)
        验算 @Pastrami_A:611.94 / 0.613265 = 997.85,与"赚 611.94 是 +61.3%"自洽 ✓

    ⚠️ 算不出来的一半**绝不补 0**:0 的含义是"没投过钱",而我们只是"不知道"
       (percentageRealizedPnl 为 0 或缺失时,除法根本没有定义)。
       只算得出一半就用那一半 —— 它是本金的**下界**,方向不会错;
       两半都算不出就返回 None,由 _ca_rank_key 把未知统一垫底,
       不让一个"不知道"冒充具体数字去跟真实值比大小。
    """
    trade = it.get("authorTrade") if isinstance(it.get("authorTrade"), dict) else {}
    total: float | None = None

    held = _f(trade.get("usdValue"))
    unreal = _f(trade.get("unrealizedPnlUsd"))
    if held is not None and unreal is not None:
        total = held - unreal

    realized = _f(trade.get("realizedPnlUsd"))
    realized_pct = _f(trade.get("percentageRealizedPnl"))
    if realized is not None and realized_pct is not None and realized_pct != 0:
        # 写成 realized * 100 / pct 而不是 realized / (pct / 100):后者在 pct 是次正规数
        # (如 5e-324)时,pct / 100 会下溢成 0.0,一个 ZeroDivisionError 直接崩掉整条命令
        sold = realized * 100.0 / realized_pct
        total = sold if total is None else total + sold

    if total is None or not math.isfinite(total):
        return None
    # 负本金没有物理含义,只可能来自浮点尘埃(清仓后 usdValue = -3.2e-13)或脏数据。
    # 压回 0 而不是返回 None:我们**知道**这个人投得极少,这是事实不是未知
    return max(total, 0.0)


def _ca_rank_key(it: dict) -> tuple[int, float]:
    """
    排序键:本金已知的按本金从大到小,本金**未知**的一律垫底。

    ⚠️ 元组第一位就是"知不知道"这一位。reverse=True 下 1 排在 0 前面,未知全部并列垫底;
       并列部分靠 sorted 的稳定性保持接口原本的顺序。
       不把未知折成 0 塞进数字里比大小 —— 那等于把"不知道"当成"没投过钱"来陈述。
    """
    cost = _ca_cost_usd(it)
    return (0, 0.0) if cost is None else (1, cost)


def _ca_fit_line(s: str, limit: int = CA_LINE_CHARS) -> str:
    """
    任意一行 → 长度 ≤ limit、且**必定**是合法 TG HTML 的一行。

    这是出口不变式(见 _ca_assemble)唯一允许"切进一行内部"的地方,所以只按**原子**切:
    一个完整实体 `&amp;`、一个完整标签各算一个原子,绝不切到一半 —— 残缺实体与未闭合
    标签都让 TG 整条消息 400(本仓库历史事故,见 CA_MSG_BUDGET 的注释)。
    切点之后把还开着的标签按逆序补齐,所以 `<code>` 里的内容被截短之后仍然是
    `<code>…</code>`,而不是一个开着口的 `<code>`。

    ⚠️ 不认识的标签、配不上对的闭标签、落单的 `<` / `&` 一律**当普通文本转义掉**。
       这一条是本函数"对任意输入都成立"的关键:调用方不必再逐个字段证明自己转义干净、
       也不必逐个字段封顶,漏一个也顶多是这一行难看,不会是整条消息 400。
    ⚠️ 这里**不**做"叠平空白 → 截断 → 转义"那一套(见 _ca_clip):传进来的已经是转义后的
       成品行,再转义一次就是双重转义。两者分工不同 —— _ca_clip 管"字段进来时",
       本函数管"整行出去时",谁都替代不了谁。
    """
    line = _CA_LINE_BREAK.sub(" ", s)
    # ⚠️ 快速路径的判据必须把 `>` 也算上:穷举扫描逮到过一条只有一个 `>` 的行,
    #    它既不超长也没有 `<` / `&`,于是被原样放行 —— 一个裸 `>` 照样是非法 HTML。
    if len(line) <= limit and not _CA_MARKUP.search(line):
        return line                       # 纯文本且够短:绝大多数行走这条,零开销
    out: list[str] = []
    stack: list[str] = []                 # 还开着的标签,用来算"补齐闭标签要占多少"
    used = 0
    cut = False
    for m in _CA_ATOM.finditer(line):
        atom = m.group(0)
        nxt = stack
        if atom.startswith("</") and atom.endswith(">"):
            name = atom[2:-1].strip().lower()
            if stack and stack[-1] == name:
                nxt = stack[:-1]
            else:
                atom = _esc(atom)         # 配不上对 = 上游漏了转义,当文本处理
        elif atom.startswith("<") and atom.endswith(">"):
            name = (atom[1:-1].split() or [""])[0].lower()
            if name in _CA_OK_TAGS:
                nxt = [*stack, name]
            else:
                atom = _esc(atom)
        elif atom in ("<", ">", "&"):
            atom = _esc(atom)
        # 预留:补齐当前还开着的标签 + 省略号,免得刚好卡在"塞得下内容、塞不下闭标签"
        reserve = sum(len(t) + 3 for t in nxt) + len(_CA_ELLIPSIS)
        if used + len(atom) + reserve > limit:
            cut = True
            break
        out.append(atom)
        used += len(atom)
        stack = nxt
    if cut:
        out.append(_CA_ELLIPSIS)
    out.extend(f"</{t}>" for t in reversed(stack))
    return "".join(out)


def _ca_anchor(ca: str) -> str:
    """
    CA 锚点(整行 <code>,点击即复制,设计文档 §10.3 的必备项)。

    ⚠️ 必须过一遍 _ca_fit_line:models.normalize_token_address 对非 0x/42 位的输入
       **原样透传**,不做任何长度校验 —— 用户粘一个几千字符的"地址"上来,锚点自己
       就超预算。而锚点是唯一"砍无可砍时也要贴上去"的那一行,不先收口的话,
       _ca_assemble 最后贴上去的就是一颗炸弹(整条消息超长 → notifier 盲切 → 400)。
    """
    return _ca_fit_line(f"<code>{_esc(ca)}</code>")


def _ca_clip(s: str, limit: int) -> str:
    """
    接口来的短字段 → 单行、限长、已转义。

    ⚠️ 这一层管的是**可读性**不是安全性:安全性由 _ca_fit_line 在装配出口兜底
       (哪个字段忘了 clip 都撑不破消息)。这里把 ticker / handle / 链名压到
       十几二十个字符,是为了不让一个字段白吃掉别人的展示位 —— 出口那道闸只保证
       "消息发得出去",保证不了"消息里还剩几位作者"。

    ⚠️ 顺序必须是"叠平空白 → 截断 → 转义",与 _ca_thesis_text 同一条理由:
       反过来会在截断点切断一个 `&amp;`,残缺实体照样让整条消息 400。
       叠平空白也是必需的:ticker 里塞几个换行就能把一行变成十行,绕开按行算的预算。
    """
    flat = _CA_WS_RUN.sub(" ", s).strip()
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return _esc(flat)


def _ca_thesis_text(raw: dict) -> str:
    """
    观点正文 → 单行摘要,已转义。

    ⚠️ 顺序必须是"叠平空白 → 截断 → 转义":反过来的话会在截断点切断一个 `&amp;`
       之类的实体,残缺实体照样让整条消息 400(formatter._thesis_line 早踩过这个坑)。
    ⚠️ 实测顶层 comment 就是正文字符串本身;poller.py 另一路(活动流)记录过
       {"comment": {"comment": "正文", ...}} 这种嵌套形状,两种都认,不猜死一种。
    """
    text = _pick_str(raw, "comment")
    if not text:
        c = raw.get("comment") if isinstance(raw, dict) else None
        if isinstance(c, dict):
            text = _pick_str(c, "comment", "text", "content", "body")
    if not text:
        return ""
    flat = _CA_WS_RUN.sub(" ", text).strip()
    if not flat:
        return ""
    if len(flat) > CA_THESIS_SNIPPET_CHARS:
        flat = flat[:CA_THESIS_SNIPPET_CHARS].rstrip() + "…"
    return _esc(flat)


def _ca_money(v: float) -> str:
    """
    /ca 里的金额展示。与 _ca_pct_str 同一条理由,只是换了个值域:

    ⚠️ usdValue / pnl 也直接来自接口、同样没有上限,而 _money 每三位插一个逗号:
       `_money(1e300)` 是 **391 个字符**,一行有两处(持仓额 + 盈亏)。出口不变式只保证
       这条消息发得出去,保证不了它还剩几位作者 —— 与 pct 是同一个"白吃展示位"。
       所以超出人能读的量级就换科学计数法。
    ⚠️ 只在 /ca 这一层加,**不动 _money 本身**:那是 /hot 等命令共用的排名展示格式
       ($51.47K),已被两轮验证判定 PASS,不为本命令去改公用函数。
    """
    if abs(v) >= _CA_MONEY_SCI:
        return f"{'-' if v < 0 else ''}${abs(v):.2e}"
    return _money(v)


def _signed_money(v: float) -> str:
    """带正负号的金额,用于盈亏 —— 正值也要显式带 '+' 才看得出是在赚钱(_money 只标负号)"""
    s = _ca_money(v)
    return s if s.startswith("-") else f"+{s}"


def _ca_pos_str(v: float) -> str:
    """
    持仓额展示。

    ⚠️ 阈值的语义是"_money 的显示精度只到分",不是"约等于 0 就当没有":
       线上真实值 usdValue = -2.4e-14(清仓后留下的尘埃残值)被 _money 印成 `-$0.00`,
       一个带负号的零;真实的小额仓位 $0.004 也印成 `$0.00`,与它旁边那行
       "未实现 +$50.00" 自相矛盾。所以**只**把落在显示精度以下的**非零**值改写掉,
       恰好为 0 仍然照常显示 $0.00 —— 0 是"清仓了"这个有意义的真实值。
       ⚠️ 这里绝不能用真值判断代替:`if not v` 会把恰好 0 和尘埃一起吞掉。
    """
    if v != 0 and abs(v) < _CA_DUST_USD:
        return "不足 $0.01"
    return _ca_money(v)


def _ca_pct_str(v) -> str | None:
    """
    百分比展示。

    ⚠️ 为什么出口不变式不足以覆盖这一处:pct 直接来自接口,没有任何天然上限,
       `f"{1e300:+.1f}%"` 是 **302 个字符**,而一行里有两处(未实现 + 已实现)。
       不变式保证的是"这条消息发得出去",保证不了"这条消息里还剩几位作者" ——
       实测(pct=1e300 × 8 位作者)只渲染得出 5 位,一个字段白吃掉三分之一展示位。
       所以这里仍然单独管,但管的是**记法**不是长度:超出人还读得动的量级
       (十亿个点 = 一千万倍)就换科学计数法,信息一个数量级都不少,
       长度从最坏 302 个字符回到最多 11 个。
       ⚠️ 阈值取 1e9 而不是更小:真实的百倍千倍(+10000%)必须照原样显示,
          换记法反而更难读。_f 已经把 NaN / Infinity 过滤成 None,这里不会拿到非有限值。

    ⚠️⚠️ 实现已搬到 formatter.fmt_signed_pct(理由与 _chips_qty 相同:/chips 的
       pump 半边在 formatter 里渲染盈亏,两边各留一份记法迟早写出两种百分比)。
       这里只是一行转调,调用点一个字都没动,输出逐字节相同。
    """
    return formatter.fmt_signed_pct(v)


def _ca_append_pnl(seg: list[str], label: str, pnl: float | None, pct: float | None) -> None:
    """
    往行里追加一段 `已实现 +$88.00 (+12.0%)`。

    ⚠️ 缺失的判据只用 is None:pnl 恰好 0.0 是"不赚不亏"这个真实值,照常成段;
       只有真的取不到(None)才整段消失,绝不打 "N/A" / "--" / 0。百分比同理,
       它缺失时只掉括号那一小段,金额那半段不受牵连。
    ⚠️ 尘埃值走 _CA_DUST_USD 这道守卫,与 _ca_pos_str 同一条理由:|pnl| 落在 _money 的
       显示精度(分)以下时直接印出来就是 `+$0.00 (+0.0%)` —— 读者只会读成"卖过、
       刚好打平",而真相是"卖了,金额小到印不出来"。恰好 0.0 不走这条:那是真的打平。
    """
    if pnl is None:
        return
    if pnl != 0 and abs(pnl) < _CA_DUST_USD:
        # 方向必须留住:一个不带方向的"不足 $0.01"读者分不清是赚是亏
        amount = "赚不足 $0.01" if pnl > 0 else "亏不足 $0.01"
    else:
        amount = _signed_money(pnl)
    piece = f"{label} {amount}"
    pct_text = _ca_pct_str(pct) if pct is not None else None
    if pct_text is not None:
        piece += f" ({pct_text})"
    seg.append(piece)


def _ca_thesis_row(it: dict) -> list[str]:
    """
    一条观点渲染成 1~2 行:@作者 · 持仓/已清仓 · 已实现|未实现盈亏(百分比);下跟正文摘要。

    ⚠️ closedAt 非空 = 这个人**已经清仓**,他的 usdValue / unrealizedPnlUsd 全是 0,
       落袋的盈亏在 realizedPnlUsd 里。只读未实现那三个字段的话,线上真实数据里
       落袋赚 $611.94 的人、实亏 $95.94 的人、实亏 $59.03 的人会被渲染成逐字节相同的
       `@某人 · $0.00 · +$0.00 (+0.0%)` —— 这不是"字段缺失整行消失",是凭空断言了
       一个假事实:读者只会把 +$0.00 (+0.0%) 读成"这人打平了",而且还带着正号。
    ⚠️ 两种盈亏混在同一列而不加标签同样误导,所以 _CA_LABEL_* 是必需的不是可选的。
       取舍与 formatter._pnl_line 完全一致(那边卖出看已实现、其余看未实现)。
    ⚠️ "仍在持仓"**不等于**"一分钱还没落袋"。真实抓包 100 条里 74 条是
       "还拿着 + 已经卖掉一部分":首行 @change 手上 $51,474.66,已经落袋 $35,901.03。
       只报未实现那一段的话,这 3.59 万一分不显示,而且盈亏方向可能整个反过来
       (账面在浮亏、但落袋赚得更多)。所以仍在持仓时也要把已实现那段带出来。
    """
    trade = it.get("authorTrade") if isinstance(it.get("authorTrade"), dict) else {}
    handle = _pick_str(it, "userHandle") or _pick_str(it, "displayName") or "?"
    # handle 是陌生人自己起的名字,长度不受任何约束,见 CA_HANDLE_CHARS
    seg = [f"@{_ca_clip(handle, CA_HANDLE_CHARS)}"]

    closed_at = _pick_str(trade, "closedAt")
    if closed_at is not None:
        # 已清仓:再报 $0.00 持仓只会被读成"他现在空仓且不赚不亏",前半句对、后半句是假的
        seg.append(_CA_CLOSED_MARK)
        _ca_append_pnl(seg, _CA_LABEL_REALIZED,
                       _f(trade.get("realizedPnlUsd")), _f(trade.get("percentageRealizedPnl")))
    else:
        pos = _f(trade.get("usdValue"))
        if pos is not None:                  # 0 是"刚好清完"的真实值,必须照常显示,不能省
            seg.append(_ca_pos_str(pos))
        _ca_append_pnl(seg, _CA_LABEL_UNREALIZED,
                       _f(trade.get("unrealizedPnlUsd")), _f(trade.get("percentageUnrealizedPnl")))
        # "已经落袋了多少"在这条路径上有**三种**状态,必须给出三种不同的输出 ——
        # 压成两种就等于替读者编一个他分辨不出的事实(上一轮把 None 和 0.0 压成了
        # 逐字节相同的输出,那正是这次要拆开的)。
        #
        #   卖过(realized != 0)—— 照常报金额与百分比。
        #   一次没卖过(恰好 0.0)—— 整段**不出现**。这一段回答的是"已经落袋了多少",
        #     而"仍在持仓"这条路径的基线本来就是一分没落袋,不写就是这个意思;
        #     写成 `已实现 +$0.00 (+0.0%)` 反而多断言一次"卖过、只是刚好打平",
        #     那跟"一次都没卖过"是两件事,数据分不出来,不该替读者选一个。
        #     ⚠️ 这是**显示口径**的取舍,不是拿真值判断代替 is None:缺失判据仍然只有
        #        is None,而且持仓额那一列的 0 照常显示(那里的 0 是"清仓了"这个独一无二的
        #        事实,省掉读者就不知道他还剩多少;这里省掉的这一段信息量恒为 0)。
        #   不知道(None,接口没给这个字段)—— 上面那条已经把"沉默"定义成了"没卖过",
        #     所以这里再沉默就是断言了一件我们不知道的事。写明"未知",与 _day_str 的
        #     "时间未知"同一套处理(绝不打 N/A / -- / 0)。
        #     ⚠️ 只有这一行**已经在陈述他的仓位**时才需要这句澄清:authorTrade 整个缺失、
        #        上面一段都没渲染出来的行本来就没许诺任何事,再挂一句"已实现 未知"是噪音。
        realized = _f(trade.get("realizedPnlUsd"))
        told_position = len(seg) > 1
        if realized is None:
            if told_position:
                seg.append(f"{_CA_LABEL_REALIZED} 未知")
        elif realized != 0:
            _ca_append_pnl(seg, _CA_LABEL_REALIZED, realized,
                           _f(trade.get("percentageRealizedPnl")))

    lines = [" · ".join(seg)]
    text = _ca_thesis_text(it)
    if text:
        lines.append(f"  {text}")
    return lines


def _ca_one_row_per_author(items: list[dict]) -> list[dict]:
    """
    一位作者只占一行:同一作者的多条观点里留**最新**那条。

    ⚠️ MAX_CA_THESIS_ROWS 的本意是"看 8 个人怎么看",按**行**截断的话一个人发 4 条
       就能占掉半张表(线上真实数据里 @leiff 一人 4 条、持仓完全相同),把别人挤下去。
    ⚠️ 认不出作者(userId / userHandle 都没有)时**不合并** —— 宁可多占一行,
       也不能把两个陌生人并成同一个人。
    """
    best: dict[str, dict] = {}
    for i, it in enumerate(items):
        key = _pick_str(it, "userId") or _pick_str(it, "userHandle") or f"\x00#{i}"
        cur = best.get(key)
        if cur is None or (_pick_str(it, "createdAt") or "") >= (_pick_str(cur, "createdAt") or ""):
            best[key] = it
    return list(best.values())


def _ca_size(lines: list[str]) -> int:
    """这些行拼进消息要占多少字符(含各自的换行)。⚠️ 传进来的必须是**已转义**的成品行"""
    return sum(len(x) + 1 for x in lines)


def _ca_assemble(head: list[str], rows: list[dict], tail: list[str], anchor: str, *,
                 render=None, max_rows: int = MAX_CA_THESIS_ROWS,
                 omit_fmt: str = "…按投入本金排序,还有 {n} 位未显示",
                 mid: list[str] | None = None) -> str:
    """
    头部 + 可变长的主体行 +(mid)+ 尾部 + CA 锚点 → 最终消息。

    ## mid 是什么,为什么不能拼进 head(本轮 J1)
    主体行(rows)永远排在 head 之后。所以谁把"另一段完整的东西"接在 head 尾巴上,
    它就会把 rows **抽到自己下面**。/chips 就踩过这个:💊 pump 三段接进 head 之后,
    🏦 FOMO 名单的成员明细行(rows)全部被印在「👥 你的 pump 名单」那行下面,
    而两边的行形(三个空格缩进 + ` · ` 分隔)**一模一样** —— 读者无从分辨,
    连"还有 N 人未显示"都一起挪过去了。那不是排版难看,是**把 A 平台的人算到 B 平台名下**。
    mid 就是那一段的位置:排在 rows(及它的收口行)**全部之后**、tail 之前。

    ## 超预算时先截谁
    mid 与 tail 一样在 room 里**先被预留**,也就是说预算不够时先让 rows 少展开几个人。
    理由:rows 自己带一套**诚实的收口机制**("…还有 N 人未显示"),少展开一个人
    读者看得见;而 mid 那几行没有 —— 它被末尾那个 while 循环从下往上 pop 掉时是**静默**的,
    而且最先被 pop 的恰恰是成员行与那句「⚠️ 平台只给了人数、没给持仓明细」的告警 ——
    告警没了、表头还在,读者会把一个有保留的结论当成确定的。容得下就全写、容不下就
    把压力转给一个会自己报数的地方,比静默地掩掉一句告警强。
    (末尾那个 while 仍然是最后的兜底:哪一段自己就胀破预算时,按整行砍仍然成立。)

    render / max_rows / omit_fmt 三个关键字参数只是把"主体行长什么样"外置出去,
    好让 /chips 复用同一套出口不变式 —— 不变式的价值全在"已经被证明过、被测试守住",
    照抄一份到新命令里等于把它的证明也一起复制,两份迟早走岔。
    默认值就是 /ca 原来写死的那三样,/ca 的调用点一个字都不用改。

    ## 出口不变式(**无论四个入参是什么**,返回值一定同时满足这三条)
      1. len(返回值) <= CA_MSG_BUDGET
      2. 返回值是合法的 TG HTML 子集:没有残缺实体,标签全部配对闭合
      3. CA 锚点是最后一行且完整闭合 —— 锚点自己都超预算时**截短它**,不是丢掉它

    这条不变式是本函数的**职责**,不是调用方的:上游任何一个来自接口/用户的字段
    忘了限长(ticker、handle、链名、接口错误文案、已试链名、本地买家名……全都无界),
    顶多让这一行难看,绝不会让整条消息发不出去。逐个字段去追是打地鼠,
    第八处第九处永远追不完。

    怎么做到的 —— 三步,缺一不可:
      a) 每一行都先过 _ca_fit_line:单行 ≤ CA_LINE_CHARS,且切在原子边界上。
         没有这一步,"按整行砍"就不完备:一行就能单独超过整条预算。
      b) 总长超预算时**只按整行边界**砍,绝不切进行内 ——
         切在 `&amp;` 中间就是残缺实体,整条消息 400(见 CA_MSG_BUDGET 的注释)。
         这活儿也绝不能留给 notifier.send 去盲切,它切的就是字节。
      c) 锚点最后贴,而 (a) 已经保证 len(anchor) <= CA_LINE_CHARS < CA_MSG_BUDGET,
         所以"砍到一行不剩 + 贴上锚点"这个最坏情况仍然在预算内 —— 第 1 条成立。
         (旧写法在这里破功:掏空 lines 之后仍然无条件 append 一个可能几千字符的锚点。)
    """
    render = render or _ca_thesis_row
    head = [_ca_fit_line(x) for x in head]
    mid = [_ca_fit_line(x) for x in (mid or [])]
    tail = [_ca_fit_line(x) for x in tail]
    anchor = _ca_fit_line(anchor)         # 幂等:调用方已经收过口也不会二次损坏
    room = (CA_MSG_BUDGET - _ca_size(head) - _ca_size(mid) - _ca_size(tail)
            - len(anchor) - _CA_OMIT_RESERVE)
    body: list[str] = []
    used = 0
    shown = 0
    for it in rows[:max_rows]:
        block = [_ca_fit_line(x) for x in render(it)]
        cost = _ca_size(block)
        if used + cost > room:
            # ⚠️ 必须 continue 不能 break:break 会让一条长评论(正常人就写得出来)
            #    把排在它**后面**、本来完全塞得下的短行全部连带丢掉 —— 一个人话多,
            #    后面所有人就都消失了。跳过这一条,继续试下一条。
            #    整行边界上停手这一点不变:不切半行,更不切半个 HTML 实体。
            continue
        body.extend(block)
        used += cost
        shown += 1

    lines = [*head, *body]
    # 未显示 = 总条数 - **真正渲染出来的**条数。跳过的和超出 max_rows 的都算在里面,
    # shown 只在真正 extend 之后才自增,所以 continue 不会让它少报/多报
    omitted = len(rows) - shown
    if omitted:
        lines.append(omit_fmt.format(n=omitted))
    # ⚠️⚠️ mid 必须排在**收口行之后**:"还有 N 人未显示"说的是 rows,
    #    排到 mid 下面就变成在说 mid 那一段的人。
    lines.extend(mid)
    lines.extend(tail)
    # 兜底:哪一段自己就撑破预算都照样只按整行砍(_ca_size(lines) + len(anchor)
    # 恰好等于 "\n".join(lines + [anchor]) 的长度,不是估算)
    while lines and _ca_size(lines) + len(anchor) > CA_MSG_BUDGET:
        lines.pop()
    lines.append(anchor)
    return "\n".join(lines)


# ============================================================
# /chips 辅助(纯函数 + 两条只读 SELECT)
# ============================================================
def _chips_int(v) -> int | None:
    """转 int,转不出来返回 None。⚠️ 绝不退化成 0 —— 0 是「真的一个持有人都没有」这个真实值"""
    n = _f(v)
    return int(n) if n is not None else None


def _chips_exact(holders: list[dict], total_holders: int | None) -> bool:
    """
    手上这份 topHolders 是不是这个币的**全部**持有人。

    ⚠️⚠️ 这一行是整条 /chips 的支点:为真才敢说「持仓 X%」,为假就只能说「≥X%」。
       判据只有一条 —— 返回条数与服务端自报的 totalHolders **相等**。
       服务端把返回条数硬钳在 100,还叠了一道约 $2 的持仓市值下限
       (见 client.EP_TOP_HOLDERS),所以两者不等就意味着有人被过滤掉了,
       我们算出来的只是下界。实测四个样本全部相等因而精确:
         Whimsy 62/62 · Maliens 46/46 · FOREST 26/26 · LESTER 5/5;
       反例 copycat totalHolders=113 却只返 8 条。
    ⚠️ totalHolders 缺失时返回 False 而不是「就当它精确」:不知道总数就没有资格
       声称精确,宁可多打一个 ≥ 也不能把下界谎报成实数。
    """
    return total_holders is not None and len(holders) == total_holders


def _chips_sum(values: list[float | None]) -> float | None:
    """
    求和,但**一个都没解析出来时返回 None 而不是 0**。

    ⚠️ sum([]) == 0 会让「字段全缺」和「加起来真的是 0」变成同一个值,
       下游据此打出一个 0.000% 的占比 —— 那是凭空造出来的假事实。
    """
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _chips_ratio(amount: float | None, supply: float | None) -> float | None:
    """占比(百分数)。任何一半缺失就返回 None,让那一段整段消失 —— 绝不用 0 顶替"""
    if amount is None or supply is None or supply <= 0:
        return None
    return amount / supply * 100.0


def _chips_pct(p: float) -> str:
    """
    占比展示。量级越小给的小数位越多 —— 固定两位会把 0.0034% 压成 0.00%,
    那等于告诉用户「没有仓位」,而真相是「有,但很小」。
    小到 _CHIPS_PCT_FLOOR 以下就不再报数字:再往下全是量化噪声,写出来只是假精确。
    """
    a = abs(p)
    if a == 0:
        return "0%"                       # 0 是真实值(确实一枚都不剩),照实写
    if a < _CHIPS_PCT_FLOOR:
        return f"&lt;{_CHIPS_PCT_FLOOR:g}%"
    if a >= 10:
        return f"{p:.1f}%"
    if a >= 1:
        return f"{p:.3f}%"
    if a >= 0.01:
        return f"{p:.2f}%"
    return f"{p:.4f}%"


def _chips_ge_pct(p: float, sep: str = "") -> str:
    """
    「下界」形态的占比展示 —— 平台侧与名单侧共用同一句写法。

    ⚠️ 低于展示下限时**绝不能**直接拼成 `≥<0.0001%`:两个方向相反的比较符黏在一起,
       读出来是「不小于小于万分之一」,自相矛盾。这时改口说「不足 0.0001%」——
       方向只剩一个,而且陈述的对象是**已统计到的那部分**;
       「真实值更高」由旁边那句截断提示负责,不在这里重复。
    ⚠️ 恰好为 0 仍然走 `≥0%`:0 是有意义的真实值,而 `≥0%` 本身没有方向冲突。
    ⚠️ sep 只为保住两个调用点各自已验证过的排版(平台侧 `≥ 10.0%` 带空格、
       名单侧 `≥0.30%` 不带),不是可调风格 —— 那两行的字节形态是被测试钉死的。
    """
    if p != 0 and abs(p) < _CHIPS_PCT_FLOOR:
        return f"不足 {_CHIPS_PCT_FLOOR:g}%"
    return f"≥{sep}{_chips_pct(p)}"


def _chips_qty(v) -> str | None:
    """
    持仓数量展示。⚠️ 实现已搬到 formatter.fmt_token_amount,这里只是一行转调。

    ⚠️⚠️ 为什么要搬:同一条 /chips 回执现在有两半边(🏦 FOMO 与 💊 pump),
       pump 那半边的成员行在 formatter 里渲染(它带一个必须过门禁的用户名,
       见 render_pump_chip_row)。两边各留一份数量记法的话,同一条回执里
       会出现 `14,584,546` 与 `14.58M` 两种写法,读者会以为那是两种不同的量。
    ⚠️ 输出与搬之前逐字节相同(有测试钉着);唯一的差别是解析不出来时返回 None
       而不是抛 TypeError —— 那一段照既有规矩消失,绝不补一个凭空的 0。
    """
    return formatter.fmt_token_amount(v)


def _chips_match_members(holders: list[dict], members: dict[str, str]) -> list[dict]:
    """
    从持有人榜里认出名单成员。返回 [{handle, amount, value}],按持仓数量从多到少。

    ⚠️⚠️ 匹配键是 **topHolders[].user.id ↔ watch_users.user_id**(两边都是同一个 UUID),
       **绝不按 handle 匹配**:FOMO 的 handle 随时可以改、大小写还不稳定,
       本项目已经为此踩过坑(store.normalize_handle 存在的全部理由)。
       按 handle 匹配会同时产生两类错误 —— 改过名的成员认不出来(漏),
       以及某个陌生人恰好占用了成员的旧 handle 时把他算进名单(错)。
    """
    out: list[dict] = []
    for h in holders:
        user = h.get("user")
        if not isinstance(user, dict):
            continue
        uid = user.get("id")
        if uid is None:
            continue
        handle = members.get(str(uid))
        if handle is None:
            continue
        out.append({"handle": handle,
                    "amount": _f(h.get("humanAmount")),
                    "value": _f(h.get("value"))})
    # 数量未知的垫底:None 不能与 float 比大小,而且「不知道」绝不该排在「确实很多」前面
    out.sort(key=lambda m: (m["amount"] is not None, m["amount"] or 0.0), reverse=True)
    return out


def _chips_stats(data: dict, members: dict[str, str], supply: float | None) -> dict:
    """
    一份 /hodlers/top 响应 + 名单 + 分母 → 渲染要用的全部数字。纯函数。

    平台侧与名单侧的分子**都从这里的同一份 holders 算出来**,口径天然一致、可以直接
    相比(见 MAX_CHIPS_MEMBER_ROWS 上面那段说明)。
    """
    holders = [h for h in (data.get("topHolders") or []) if isinstance(h, dict)]
    total = _chips_int(data.get("totalHolders"))
    exact = _chips_exact(holders, total)
    # ⚠️ 服务端自报的总数比它自己给的条数还少 —— 这份响应自相矛盾(不该发生,但发生过就会
    #    渲染成「持有人 0」下面却列着一串持有人)。这时以**手上真有的条数**为准:
    #    我们数得出来的东西比服务端的自报更可信。
    # ⚠️ 但绝不因此声称精确 —— exact 在覆盖之前就已经算好了:连总数都不可信,
    #    更没有资格说「这就是全部」。宁可多打一个下界记号。
    if total is not None and total < len(holders):
        total = len(holders)
    matched = _chips_match_members(holders, members)
    return {
        "covered": len(holders),
        "total": total,
        "exact": exact,
        # 一条持有人记录都没有、服务端也没给总数:说不清是「还没人买」还是「链不对」
        "empty": not holders and total is None,
        "matched": matched,
        "plat_pct": _chips_ratio(_chips_sum([_f(h.get("humanAmount")) for h in holders]), supply),
        "watch_pct": _chips_ratio(_chips_sum([m["amount"] for m in matched]), supply),
    }


def _chips_platform_lines(st: dict, err: str | None) -> list[str]:
    """
    平台侧文案。**精确与下界必须是两套一眼可辨的写法** —— 这是本功能的核心诚实点:
    用户见过的第三方工具在 CATE 上只统计 98/79891(覆盖 0.12%)却照样显示成
    「持仓占比」,不加任何提示;我们宁可多占一行也要把覆盖范围写出来。
    """
    if err is not None:
        return [f"🏦 FOMO 平台 · 持有人榜没拉到:{err}"]
    if st["empty"]:
        return ["🏦 FOMO 平台 · 没查到持有人 —— 可能还没人买,也可能这个币不在这条链上"]

    covered, total, pct = st["covered"], st["total"], st["plat_pct"]
    if st["exact"]:
        line = f"🏦 FOMO 平台 · 持有人 {total:,}"
        # 分母缺失时这一段整段消失,绝不打 0%(见 _chips_ratio)
        if pct is not None:
            line += f" · 持仓 {_chips_pct(pct)}"
        return [line]

    lines = [f"🏦 FOMO 平台 · 持有人 {total:,}" if total is not None
             else f"🏦 FOMO 平台 · 持有人 ≥{covered:,}(服务端没给总数)"]
    if covered == 0:
        # 服务端自报有持有人、却一条明细都没给(那道约 $2 的市值下限足以把人全滤光)。
        # 这时说"仅统计前 0 名"是句自相矛盾的废话,直接讲清占比为什么算不出来
        lines.append("   ⚠️ 没拿到任何持有人明细,占比无从统计")
        return lines
    warn = f"⚠️ 仅统计前 {covered:,} 名,真实值更高"
    lines.append(f"   持仓 {_chips_ge_pct(pct, ' ')}   {warn}" if pct is not None else f"   {warn}")
    return lines


def _chips_watch_lines(st: dict, err: str | None) -> list[str]:
    """
    名单侧文案。与平台侧同源同口径,所以两个百分比可以直接相比。

    ⚠️ 被截断时措辞是「N 人在前 X 名内」而不是「N 人持有」:名单成员没出现在前 X 名里
       **不等于**他没持有(他可能只是仓位小于那道 $2 门槛,或被 100 条截掉了)。
       写成「无人持有」就是把「我们没看见」谎报成「不存在」。
    """
    if err is not None:
        return ["👥 你的名单 · 与平台侧同一份数据,没拉到就判断不了"]
    if st["empty"]:
        return ["👥 你的名单 · 无人持有"]

    matched, covered, pct = st["matched"], st["covered"], st["watch_pct"]
    if st["exact"]:
        if not matched:
            return ["👥 你的名单 · 无人持有"]
        line = f"👥 你的名单 · {len(matched)} 人持有"
        if pct is not None:
            line += f" · {_chips_pct(pct)}"
        return [line]

    if covered == 0:
        # 与平台侧同一条理由:一条明细都没拿到时,「前 0 名内无人」是句什么都没说的话
        return ["👥 你的名单 · 没有持有人明细,判断不了"]
    if not matched:
        return [f"👥 你的名单 · 前 {covered:,} 名内无人"]
    line = f"👥 你的名单 · {len(matched)} 人在前 {covered:,} 名内"
    if pct is not None:
        line += f" · {_chips_ge_pct(pct)}"
    return [line]


def _pump_coverage_warn(ch) -> str:
    """
    「我们只看到了一部分人」这句提示。⚠️ 每一种"看不全"的原因**措辞必须不同** ——
    读者据此判断"要不要自己再查一次":

      · 平台没给总数 —— 连分母都没有,手上这些人只是下界;
      · 我们有页没取到 —— **我们这边**的请求挂了(或限速闸没排上)。再查一次很可能就全了;
      · 撞预算       —— 我们本来要全翻,时间不够;
      · 轻档         —— 是**我们**主动只看前 50 名(币太大,全翻会拖住这条同步命令);
      · 自报比明细还少 —— 平台给的总数比我们手上的条数还小,这份数据**自相矛盾**;
      · 平台没给全   —— 我们该翻的都翻完了、每一页都拿到了,是**平台**只给出这么多明细
                        (实测很常见:totalCount=107 的币明细只有 1 条,见 pumpchips 模块头)。

    把它们写成同一句"仅统计前 N 名"就是在编原因。
    ⚠️⚠️ 第二条(failed_pages)是本轮 J3 补的:上一版 _page 把任何一页的失败
       **吞成空页且不留痕迹**,于是"我们自己挂了"被静默并进"平台只给出 N/M 人的明细"——
       读者据此以为"再查也没用",而真相恰恰相反。
    ⚠️⚠️ 第五条(自报比明细还少)是本轮 J7 补的:pumpchips 在 total < covered 时
       会把 total 抬到 covered(以手上真有的条数为准),于是上一版会打出
       「平台只给出 9/9 人的明细,真实值更高」这种**自己打自己脸**的句子。
       ⚠️ 判据是 `covered >= total`:exact 为真时根本不会调到这个函数,
          所以进到这里还 covered == total,只可能是被抬上来的那种自相矛盾。
    """
    covered, total = ch.covered, ch.total
    if total is None:
        return f"⚠️ 平台没给总数,{covered:,} 人只是下界"
    if ch.failed_pages:
        return (f"⚠️ 我们这边有 {ch.failed_pages} 页没取到,"
                f"只统计到 {covered:,}/{total:,} 人,真实值更高")
    if ch.partial:
        return f"⚠️ 时间不够,只统计到 {covered:,}/{total:,} 人,真实值更高"
    if not ch.full_scan:
        return f"⚠️ 人多,仅统计前 {covered:,} 名,真实值更高"
    if covered >= total:
        return f"⚠️ 平台自报的人数比明细还少,这份数据自相矛盾,已按 {covered:,} 人算"
    return f"⚠️ 平台只给出 {covered:,}/{total:,} 人的明细,真实值更高"


def _pump_chips_platform_lines(ch) -> list[str]:
    """
    💊 平台侧文案。与 🏦 FOMO 侧**同一条判据**:covered == total 才敢说「持仓 X%」,
    否则只能说「≥X%」并把覆盖范围写出来。

    ⚠️ 分母缺失时占比那一段整段消失,绝不打 0%(见 pumpchips._ratio)。
    ⚠️ 「持有人 0」是**真实值**照常显示(实测 200 `{"positions":[],"totalCount":0}`
       —— pump 认得这个币,平台上确实没人托管持仓),它与"查不到"是两件事,
       后者在 pumpchips.fetch_chips 里返回 None,💊 整块根本不出现。
    """
    total, covered, pct = ch.total, ch.covered, ch.plat_pct
    head = (f"{EMOJI_PUMP_CHIPS} pump.fun 平台 · 持有人 {total:,}" if total is not None
            else f"{EMOJI_PUMP_CHIPS} pump.fun 平台 · 持有人 ≥{covered:,}(平台没给总数)")
    if ch.exact and pct is not None:
        return [f"{head} · 持仓 {_chips_pct(pct)}"]

    lines = [head]
    if covered == 0:
        # ⚠️ 平台自报有人、却一条明细都不给(实测:非 pump 上架的币 —— 它只是被 pump
        #    用户持有的外部币,coins-v3 直接返回 null)。这时**必须说清楚为什么**,
        #    否则下面那句"判断不了"会被读成"我们查过了,名单里没人"。
        if total:
            lines.append("   ⚠️ 平台只给了人数、没给持仓明细,占比与名单都判断不了")
        return lines
    if not ch.exact:
        warn = _pump_coverage_warn(ch)
        lines.append(f"   持仓 {_chips_ge_pct(pct, ' ')}   {warn}" if pct is not None
                     else f"   {warn}")
    if pct is None:
        # 分子有、分母没有(或分母对不上)。⚠️ 与 FOMO 侧那句注脚同义,但必须挨着 💊
        #    这一块说 —— 两半边的分母是两个不同的来源,一句话盖两边会指鹿为马。
        # ⚠️⚠️ "没拿到分母"与"分母自相矛盾"是**两句话**:后者读者应当知道
        #    pump 自己给的数就对不上(实测 $PUMP:已统计的持仓比它自报的总供应量还多),
        #    写成"没给"会让人以为是我们没查到。
        lines.append("   ⚠️ pump 给的总供应量比已统计的持仓还少,这个分母不可信,占比不显示"
                     if ch.bad_supply else "   ⚠️ pump 没给总供应量,占比算不出来")
    return lines


def _pump_chips_watch_lines(ch) -> list[str]:
    """
    💊 名单侧文案。与 FOMO 侧同源同口径,措辞逐字对齐(同一条消息里同一个说法
    必须是同一个意思)。

    ⚠️⚠️ 「无人持有」与「前 N 名内无人」是**两件不同的事**,绝不能混:
       前者是"全都看过了,确实没有",后者是"我们只看到前 N 名,他可能在后面"。
       写成「无人持有」就是把"我们没看见"谎报成"不存在"。
    ⚠️ 一条明细都没拿到时既不说"有"也不说"无" —— 那时「前 0 名内无人」
       是句什么都没说的话。
    ⚠️⚠️ exact 那一支必须判在 covered==0 **之前**(本轮 J7):`totalCount=0` 是
       「平台上确实没人托管持仓」这个**已确认**的事实(实测 200
       `{"positions":[],"totalCount":0}`),它的 covered 也是 0 ——
       先判 covered==0 会让这条自洽的回执打出「持有人 0」+「没有持仓明细,判断不了」
       两句互相打架的话。exact 为真且 covered==0 只可能是 total==0 这一种情况。
    ⚠️⚠️ 「前 N 名内」这个说法**有前提**:手上这批人得真的是按持仓排下来的一个
       **连续前缀**。页失败(failed_pages)或撞预算(partial)时不是 ——
       第 1 页没取到、第 2 页取到了,手上这批人中间是有窟窿的,
       这时说「前 N 名内无人」是**假陈述**。那两种情况改说「已看到的 N 人里」,
       并把"没能确认"四个字写出来(本轮 J3)。
    """
    matched, covered, pct = ch.matched, ch.covered, ch.watch_pct
    if ch.exact:
        if not matched:
            return [f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · 无人持有"]
        line = f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · {len(matched)} 人持有"
        if pct is not None:
            line += f" · {_chips_pct(pct)}"
        return [line]
    if covered == 0:
        return [f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · 没有持仓明细,判断不了"]
    # 手上这批人是不是一个连续的"前 N 名"
    prefix = not ch.failed_pages and not ch.partial
    if not matched:
        return [f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · 前 {covered:,} 名内无人"] if prefix else [
            f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · 已看到的 {covered:,} 人里没有,没能确认"]
    line = (f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · {len(matched)} 人在前 {covered:,} 名内"
            if prefix else
            f"{EMOJI_PUMP_WATCH} 你的 pump 名单 · {len(matched)} 人在已看到的 {covered:,} 人里")
    if pct is not None:
        line += f" · {_chips_ge_pct(pct)}"
    return [line]


def _pump_chip_member_lines(ch) -> list[str]:
    """
    命中的名单成员,每人一行。超出 MAX_PUMP_CHIP_ROWS 的收口成一句"还有 N 人未显示"。

    ⚠️⚠️ 行本身由 **formatter.render_pump_chip_row** 渲染,不在这里拼:
       那一行里的 pump 用户名是攻击者可控的自由文本,而门禁表(UNTRUSTED_FIELDS)
       只对挂了 @_guard_untrusted 的渲染函数生效。在这里 f-string 拼一下
       就绕过了整套收口 —— 那正是 tests/test_nameguard_chokepoint.py 存在的理由。
    ⚠️ 返回的行**已转义**,交给 _ca_fit_line 时不要再 escape。
    """
    lines = [f"   {formatter.render_pump_chip_row(pump_username=m['name'], amount_held=m['amount'], pnl_pct=m['pnl_pct'])}"
             for m in ch.matched[:MAX_PUMP_CHIP_ROWS]]
    omitted = len(ch.matched) - len(lines)
    if omitted:
        lines.append(f"   …按持仓数量排序,还有 {omitted} 人未显示")
    return lines


def _pump_chips_lines(get_client, ca: str, members: dict[str, str]) -> list[str]:
    """
    💊 那半边的全部行。**任何失败都返回空列表**(整块消失),绝不炸掉整条回执。

    ⚠️⚠️ 第一个参数收的是"**怎么拿到客户端**"这个动作,而不是客户端本身:
       PumpClient 是懒建的,建它要 import curl_cffi(带原生库),而那一步自己
       也会失败(没装 / 装坏了)。放在 try 外面的话,一个 ImportError 会把整条
       /chips 炸掉 —— 连 🏦 FOMO 那半边一起。有用例钉着这条。
    ⚠️⚠️ 名单为空(还没 /pump add 过人)时**名单那两段整个不出现** ——
       对着一个空名单说"无人持有"是句误导:读者会以为我们查过了。
       平台人数与占比照常显示,它们与名单没有关系。
    ⚠️ 这里是 /chips 里**唯一**一处对 pump 的外呼。它排在 🏦 FOMO 那半边**之后**,
       所以 pump 挂掉/超时的最坏后果是"回执少了 💊 那几行",FOMO 半边一个字都不变。
    """
    try:
        ch = pumpchips.fetch_chips(get_client(), ca, members)
    except Exception:  # noqa: BLE001
        logger.exception("/chips 的 pump 半边整块失败(只让 💊 消失) | {}", ca[:16])
        return []
    if ch is None:
        return []                      # 这个币根本不在 pump 上 → 回执与接入前一模一样
    lines = _pump_chips_platform_lines(ch)
    if members:
        lines += _pump_chips_watch_lines(ch)
        lines += _pump_chip_member_lines(ch)
    return lines


def _load_pump_members(conn) -> dict[str, str]:
    """
    pump 名单成员:user_id → 展示名。

    ⚠️ 只取 active = 1:/pump del 是软删除,退出名单的人不该再算进「你的 pump 名单」
       (与 _load_watch_members 同一条理由)。
    ⚠️ **不看 seeded / callout_seeded** —— 那两位是推送侧的冷启动播种位,
       与"他现在持不持有"完全无关;拿它们过滤会让刚 /pump add 的人凭空消失。
    ⚠️ username 允许是 NULL:那时展示名退回接口给的 userName(见
       pumpchips._match_members),两者都会在渲染入口过 safe_display。
    """
    return {
        str(r["user_id"]): (r["username"] or "")
        for r in conn.execute(
            "SELECT user_id, username FROM pump_watch_users WHERE active = 1"
        ).fetchall()
        if r["user_id"]
    }


def _chips_member_row(m: dict) -> list[str]:
    """
    名单成员一行:`   @handle · 1,234,567 枚 · $890`。

    ⚠️ 缺的字段整段消失,不打 "N/A" 也不打 0 —— 与 /ca 的买家行同一条规矩。
    """
    seg = [f"@{_ca_clip(m['handle'], CA_HANDLE_CHARS)}"]
    qty = _chips_qty(m["amount"]) if m["amount"] is not None else None
    if qty is not None:
        seg.append(f"{qty} 枚")
    if m["value"] is not None:
        seg.append(_ca_money(m["value"]))
    return ["   " + " · ".join(seg)]


def _chips_meta_symbol(meta: dict) -> str | None:
    """从 filterTokens 的元数据里捞 ticker。捞不到返回 None,由调用方退回本地 symbol"""
    token = meta.get("token") if isinstance(meta, dict) else None
    if not isinstance(token, dict):
        return None
    for holder in (token.get("info"), token):
        if isinstance(holder, dict):
            s = _pick_str(holder, "symbol", "ticker", "name")
            if s:
                return s
    return None


def _chips_local_supply(ca: str, net: str) -> float | None:
    """
    兜底分母:本地 token_snapshot 的 市值 ÷ 现价。

    ⚠️ 这是**推算值,不是权威值**,调用方必须在消息里标注出来。依据:FOMO 的「市值」
       就是按总供应量算的 FDV,所以 marketCap / priceUSD ≈ totalSupply ——
       实测复核 964,163,717 vs 964,376,492,差 0.02%(价格取整造成)。
    ⚠️ 价格为 0 / 缺失时返回 None:除零之外,0 价格算出来的「供应量」是无穷大,
       会把占比压成 0.0000% 这种看起来像真数据的假值。
    """
    with store.get_conn() as conn:
        row = conn.execute(
            "SELECT market_cap, price_usd FROM token_snapshot "
            "WHERE network_id = ? AND token_address = ?",
            (net, ca),
        ).fetchone()
    if row is None:
        return None
    mcap, price = _f(row["market_cap"]), _f(row["price_usd"])
    if mcap is None or price is None or price <= 0 or mcap <= 0:
        return None
    return mcap / price


def _local_token_symbol(conn, ca: str) -> str | None:
    """本地见过的这个地址的 symbol(随便捞一条就够,只用于标题)。/ca 与 /chips 共用"""
    row = conn.execute(
        "SELECT token_symbol FROM fomo_events "
        "WHERE token_address = ? AND token_symbol IS NOT NULL LIMIT 1",
        (ca,),
    ).fetchone()
    return row["token_symbol"] if row else None


def _load_watch_members(conn) -> dict[str, str]:
    """
    名单成员:user_id → 展示用 handle。

    ⚠️ 只取 active = 1:/del 是软删除,退出名单的人不该再算进「你的名单」。
       **不看 stats_ready** —— 那是「历史基线建没建好」(功能 A/B 的门槛),
       与「他现在持不持有」完全无关,拿它过滤会让刚 /add 的人凭空消失。
    """
    return {
        str(r["user_id"]): (r["handle"] or r["display_name"] or "?")
        for r in conn.execute(
            "SELECT user_id, handle, display_name FROM watch_users WHERE active = 1"
        ).fetchall()
        if r["user_id"]
    }
