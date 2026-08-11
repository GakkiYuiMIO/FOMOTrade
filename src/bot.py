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
import threading
import time
from datetime import UTC, datetime

from loguru import logger

from src import store
from src.config import get_settings
from src.models import normalize_network, normalize_token_address

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

_HELP = (
    "🤖 <b>FOMO 监控 Bot</b>\n"
    "/add &lt;handle&gt; — 加入监控(立即生效,历史基线由下一轮建立)\n"
    "/del &lt;handle&gt; — 移出监控(软删除,历史数据保留)\n"
    "/list — 查看监控名单与基线状态\n"
    "/status — 运行状态\n"
    "/who &lt;CA&gt; [链] — 名单里谁买过这个币\n"
    "/help — 本说明"
)


def _esc(v) -> str:
    """HTML 转义。API 返回的昵称里带 '<' 并不罕见,不转义整条回执直接 400"""
    return html.escape(str(v)) if v is not None else ""


def _day_str(iso: str | None) -> str:
    """ISO 时间取日期部分;缺失显示"时间未知"(绝不显示 None / N/A)"""
    return (iso or "")[:10] or "时间未知"


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

        logger.info("Telegram 命令层已启动 | 仅响应 chat_id={}", admin)
        while not stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("命令轮询异常,{}s 后继续", ERROR_BACKOFF_SEC)
                stop_event.wait(ERROR_BACKOFF_SEC)
        logger.info("Telegram 命令层已停止")

    # ============================================================
    # 分发
    # ============================================================
    def _handle_update(self, up: dict) -> None:
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
        if cmd in ("/del", "/rm", "/remove"):
            return self._cmd_del(arg)
        if cmd == "/list":
            return self._cmd_list()
        if cmd == "/status":
            return self._cmd_status()
        if cmd == "/who":
            return self._cmd_who(arg)
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
        handle = store.normalize_handle(arg)
        if not handle:
            return "用法: /add &lt;handle&gt;  例: /add maxpain"

        try:
            user_id, display = self._client.resolve_handle(handle)
        except Exception as e:  # noqa: BLE001
            return self._resolve_error(handle, e)
        if not user_id:
            return f"❌ 找不到用户 @{_esc(handle)}(handle 拼错?或该用户已改名)"

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

    def _cmd_list(self) -> str:
        with store.get_conn() as conn:
            rows = store.list_active_users(conn)
            if not rows:
                return "📋 监控名单为空,用 /add &lt;handle&gt; 添加"
            lines = [f"📋 <b>监控名单</b>({len(rows)} 人)"]
            for i, r in enumerate(rows[:MAX_LIST_ROWS], 1):
                ready = "✅就绪" if r["stats_ready"] else "⏳建立中"
                n_token = store.stats_row_count(conn, r["user_id"])
                name = r["display_name"] or r["handle"]
                lines.append(
                    f"{i}. <b>{_esc(name)}</b> @{_esc(r['handle'])} · {ready} · "
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
