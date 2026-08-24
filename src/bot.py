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
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from loguru import logger

from src import store
from src.config import PROBE_DIR, get_settings
from src.copytrade import auto_blockers, pnl
from src.executor import buy as execute_buy
from src.models import NETWORK_DISPLAY, normalize_network, normalize_token_address

# _f/_pick_str 是 poller 已经踩过坑写好的"字段可能缺、可能嵌套、可能是脏字符串"
# 兜底解析器。/ca 解析的是同一个 API(client.get_token_thesis),没道理另起一套。
from src.poller import _f, _pick_str

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
    ("copy", "跟单参数:/copy 查看 · /copy <项> <值> 修改"),
    ("paper", "跟单台账:纸上建的仓现在赚亏多少"),
    ("star", "特别关注:/star <handle> — 推送加 ⭐ 醒目标识"),
    ("unstar", "取消特别关注:/unstar <handle>"),
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
    "/del &lt;handle&gt; — 移出监控(软删除,历史数据保留)\n"
    "/list — 查看监控名单与基线状态(⭐ 的排最前)\n"
    "/status — 运行状态\n"
    "/who &lt;CA&gt; [链] — 名单里谁买过这个币\n"
    "/ca &lt;地址&gt; [链] — 查这个合约地址:观点 + 名单买没买过\n"
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
# /ca <合约地址> —— 查这个币:观点(全站,任何地址都能查)+ 名单本地记录
# ============================================================
# 从 API 拉多少条观点。拉够了才能在本地按持仓额可靠地重新排序(接口本身不按这个排)
CA_THESIS_FETCH_LIMIT = 30
# 消息里最多展示这么多条(TG 4096 字符硬上限,一条观点还可能带一段长文)
MAX_CA_THESIS_ROWS = 8
# 单条观点摘要最多这么多字符 —— 不截断的话一条长文/刷屏换行就能撑爆整条消息的排版
CA_THESIS_SNIPPET_CHARS = 140
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


class CommandBot:
    """Telegram 命令处理器。由 cli.py 起一个 daemon 线程跑 run_forever()"""

    def __init__(self, client, notifier, poller=None) -> None:
        """
        poller 是可选的:只用来在 /status 里读最近一次 tick 时间
        (该属性由 cli.cmd_run 的调度任务回填,不是 Poller 契约的一部分)。
        取不到就退化成"最近一条事件的入库时间",绝不因为拿不到它而让 /status 失败。
        """
        self._client = client
        self._notifier = notifier
        self._poller = poller
        self._settings = get_settings()
        self._offset: int | None = None

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
        if cmd == "/list":
            return self._cmd_list()
        if cmd == "/status":
            return self._cmd_status()
        if cmd == "/who":
            return self._cmd_who(arg)
        if cmd == "/ca":
            return self._cmd_ca(arg)
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
            today_key = datetime.now(UTC).strftime("%Y-%m-%d")
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM fomo_events WHERE ingested_at >= ?", (today_key,)
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
           len(CA_EVM_GUESS_ORDER) = 6 次请求,单条命令用不了更多。
        """
        parts = (arg or "").split()
        if not parts:
            return "用法: /ca &lt;合约地址&gt; [链]  例: /ca 0xfc6e...5777 bsc"
        ca = normalize_token_address(parts[0])
        if not ca:
            return "⚠️ 请给出代币合约地址"
        forced_net = normalize_network(parts[1]) if len(parts) > 1 else None

        local_nets, local_symbol, local_lines = self._ca_local(ca)

        if forced_net:
            candidates = [forced_net]
        elif local_nets:
            candidates = local_nets                 # 本地已经见过,链是确定的,不猜
        elif ca.startswith("0x"):
            candidates = list(CA_EVM_GUESS_ORDER)
        else:
            candidates = ["solana"]

        items: list[dict] = []
        used_net: str | None = None
        tried: list[str] = []
        for net in candidates:
            tried.append(net)
            try:
                # get_token_thesis 的 networkId 要原生数字 ID,不是本地归一化别名,见
                # _NETWORK_RAW_ID 的注释(实测踩过:传 "bsc" 直接 400)
                raw_net = _NETWORK_RAW_ID.get(net, net)
                got = self._client.get_token_thesis(ca, raw_net, limit=CA_THESIS_FETCH_LIMIT)
            except Exception as e:  # noqa: BLE001
                return self._ca_error(ca, e)
            items = [it for it in (got or []) if isinstance(it, dict)]
            if items:
                used_net = net
                break

        # 全都没查到 + 本地也不认识:说不清是"没这个币"还是"猜错了链",
        # 措辞必须把这份不确定性带出来,不能断言任何一边(见常量注释里的实测)
        if not items and not local_nets:
            if forced_net:
                return (f"🔎 <code>{_esc(ca)}</code> · {_esc(_chain_name(forced_net))}\n"
                        f"这条链上没查到观点,本地也没有记录 —— 可能是新币,也可能还没人发观点")
            return (f"🔎 <code>{_esc(ca)}</code>\n"
                    f"{_esc('/'.join(tried))} 都没查到观点,本地也没有记录\n"
                    f"可能是全新的币、还没人发观点,也可能是猜错了链 —— "
                    f"可指定:/ca &lt;地址&gt; &lt;链&gt;")

        if items:
            # 头部用观点接口**回读到的真实值**,不用自己请求时传的猜测 ——
            # 猜对了两者一致,万一哪天服务端不再是"错链必空"这套行为,头部也不会跟着猜错
            first = items[0]
            sym = (_pick_str(first, "ticker") or "?").lstrip("$")
            chain_id = normalize_network(_pick_str(first, "networkId")) or used_net
        else:
            sym = (local_symbol or "?").lstrip("$")
            chain_id = forced_net or (local_nets[0] if local_nets else None)
        lines = [f"<b>${_esc(sym)}</b> · {_esc(_chain_name(chain_id))}"]

        if items:
            rows = sorted(items, key=lambda it: _num(_ca_pos_usd(it)), reverse=True)
            author_ids = {_pick_str(it, "userId") or _pick_str(it, "userHandle") for it in rows}
            author_ids.discard(None)
            lines.append(f"📋 {len(rows)} 条观点 · {len(author_ids)} 位作者")
            shown = rows[:MAX_CA_THESIS_ROWS]
            for it in shown:
                lines.extend(_ca_thesis_row(it))
            omitted = len(rows) - len(shown)
            if omitted:
                lines.append(f"…按持仓额排序,还有 {omitted} 条未显示")
        else:
            lines.append(f"💭 没查到观点(已试 {_esc('/'.join(tried))})")

        lines.append("")
        lines.extend(local_lines)
        lines.append(f"<code>{_esc(ca)}</code>")
        return "\n".join(lines)

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
            # ⚠️ 用 fomo_events 而不是 user_token_stats:后者只是前者按"算数的买入"
            #    聚合出来的派生表,任何一行 user_token_stats 必然对应一行 fomo_events,
            #    反过来不成立(SELL、非计数原因的 BUY 只落 fomo_events)。
            #    这里要的是"本地是否见过这个地址"这个更宽的信号,token_snapshot
            #    再补上"持有但没见过买入事件"(比如转入)的那一小撮。
            local_nets = [
                r["network_id"]
                for r in conn.execute(
                    "SELECT DISTINCT network_id FROM fomo_events WHERE token_address = ? "
                    "UNION SELECT DISTINCT network_id FROM token_snapshot WHERE token_address = ?",
                    (ca, ca),
                ).fetchall()
                if r["network_id"]
            ]

            symbol = None
            srow = conn.execute(
                "SELECT token_symbol FROM fomo_events "
                "WHERE token_address = ? AND token_symbol IS NOT NULL LIMIT 1",
                (ca,),
            ).fetchone()
            if srow:
                symbol = srow["token_symbol"]

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
                    if usd is not None:              # 0 = 这几笔没解析出金额,不是没买
                        row.append(_money(usd) + (f"({b['buys']} 笔)" if b["buys"] > 1 else ""))
                    blocks.append("   " + " · ".join(row))

        if not blocks:
            blocks = ["👥 你的名单:无人持有"]
        return local_nets, symbol, blocks

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
# /ca 辅助渲染(纯函数,不碰网络 / DB)
# ============================================================
def _ca_pos_usd(it: dict) -> float | None:
    """这条观点作者的持仓额(authorTrade.usdValue)。排序键用,取不到就是 None"""
    trade = it.get("authorTrade") if isinstance(it.get("authorTrade"), dict) else {}
    return _f(trade.get("usdValue"))


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


def _signed_money(v: float) -> str:
    """带正负号的金额,用于盈亏 —— 正值也要显式带 '+' 才看得出是在赚钱(_money 只标负号)"""
    s = _money(v)
    return s if s.startswith("-") else f"+{s}"


def _ca_thesis_row(it: dict) -> list[str]:
    """一条观点渲染成 1~2 行:@作者 · 持仓 · 未实现盈亏(百分比);下面跟正文摘要"""
    trade = it.get("authorTrade") if isinstance(it.get("authorTrade"), dict) else {}
    handle = _pick_str(it, "userHandle") or _pick_str(it, "displayName") or "?"
    seg = [f"@{_esc(handle)}"]

    pos = _f(trade.get("usdValue"))
    if pos is not None:                      # 0 是「已清仓」的真实值,必须照常显示,不能省
        seg.append(_money(pos))

    pnl = _f(trade.get("unrealizedPnlUsd"))
    if pnl is not None:
        piece = _signed_money(pnl)
        pct = _f(trade.get("percentageUnrealizedPnl"))
        if pct is not None:
            piece += f" ({pct:+.1f}%)"
        seg.append(piece)

    lines = [" · ".join(seg)]
    text = _ca_thesis_text(it)
    if text:
        lines.append(f"  {text}")
    return lines
