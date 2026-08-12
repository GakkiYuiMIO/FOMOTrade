"""
Telegram 推送模块
- 用 httpx 直接调 Bot API,避免引入重 SDK
- 未配置 Token/Chat ID 时自动降级为仅日志输出
- 处理 429 限流:按 retry_after 退避后重试一次
"""
import threading
import time

import httpx
from loguru import logger

from src.config import get_settings

# TG 单条消息硬上限 4096 字符,留点余量
MAX_MESSAGE_LEN = 4000
# 一条消息最多试几次。⚠️ 只有 429 与传输层失败值得重试,4xx 重试多少次都是 4xx
_MAX_SEND_ATTEMPTS = 3
_SEND_RETRY_SEC = 1.0
# 429 退避的硬上限。与 client._retry_after 保持一致 —— 一个畸形/极端的头不该把整轮卡死
_MAX_RETRY_AFTER_SEC = 60.0


class TelegramNotifier:
    """轻量 Telegram 推送"""

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.tg_token
        self._chat_id = settings.fomo_telegram_chat_id
        self._proxy = settings.fomo_proxy
        self.enabled = settings.tg_enabled
        self._lock = threading.Lock()
        self._shared: httpx.Client | None = None

        if self.enabled:
            logger.info("Telegram 推送已启用 | chat_id={}", self._chat_id)
        else:
            logger.warning("Telegram 未配置,推送将只写入日志")

    def _client(self) -> httpx.Client:
        """
        长期复用的连接池。

        ⚠️ 原来每发一条就 new 一个 Client 再关掉 —— 等于每条消息都重做一次
           「连代理 → TLS 握手 → HTTP/2 协商」,实测每条多花 0.3~1s,
           而这段时间全部计入单轮 tick 耗时。复用之后走 keep-alive,第二条起几乎零开销。
        ⚠️ httpx.Client 本身是线程安全的;这里加锁只是保证不会并发建出两个实例。
        """
        with self._lock:
            if self._shared is None:
                # 国内直连 api.telegram.org 通常不通,走代理
                self._shared = httpx.Client(
                    timeout=20.0,
                    proxy=self._proxy,
                    limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120.0),
                )
            return self._shared

    def _reset_client(self) -> None:
        """连接被对端掐断(代理常见)后丢弃整个池,下次重建。"""
        with self._lock:
            c, self._shared = self._shared, None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def send(self, text: str, parse_mode: str = "HTML", chat_id: str | None = None,
             buttons: list[tuple[str, str]] | None = None) -> bool:
        """
        同步发送消息。支持 HTML 标签: <b> <i> <code> <blockquote>
        返回 True 表示 TG 确认收到 —— 调用方据此把 fomo_events.sent 置 1,
        绝不能"发之前就标已发",否则崩在中间会永久丢消息。

        buttons: [(按钮文字, callback_data), …] —— 一行内联键盘。
        ⚠️ callback_data 是 TG 的硬限制:**最多 64 字节**。Solana 的 CA 是 44 字符、
           加上链名和动作前缀就顶满了,所以调用方必须传短标识(见 bot._copy_cb)。
           超长时 TG 直接 400,而那条消息会**没有按钮地发出去** —— 你以为能点,其实不能。
        """
        target = chat_id or self._chat_id
        if not self.enabled or not target:
            logger.info("[TG-未配置] {}", text)
            return False

        if len(text) > MAX_MESSAGE_LEN:
            # 宁可截断也不要整条 400 —— 截断标记放末尾,不破坏开头的 emoji 锚点
            text = text[: MAX_MESSAGE_LEN - 10] + "\n…(已截断)"

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if buttons:
            # ⚠️ 超过 64 字节的 callback_data 会让 TG 整条 400。宁可丢掉那个按钮
            #    也不能丢掉整条消息 —— 消息里有 CA,没按钮照样能手动买。
            keep = [(t, d) for t, d in buttons if len(d.encode()) <= 64]
            if len(keep) != len(buttons):
                logger.error("callback_data 超 64 字节,已丢弃 {} 个按钮(消息照常发)",
                             len(buttons) - len(keep))
            if keep:
                payload["reply_markup"] = {
                    "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in keep]]
                }

        # ⚠️ 不要写成 `with self._client() as c` —— 那会在退出时关掉共享连接池,
        #    等于每条消息又退回到"重新握手"。
        for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
            try:
                resp = self._client().post(url, json=payload)
                # 429 限流:TG 会在 body 里给 retry_after,退避后重试
                if resp.status_code == 429:
                    # ⚠️ 必须夹上限。这段 sleep 跑在 poller 线程的 _dispatch 里 ——
                    #    直接采信 TG 给的 retry_after,一个 300 就把整个轮询堵 5 分钟,
                    #    期间 last_tick_at 不前进,/status 看着就像挂了,日志只有一行警告。
                    #    client._retry_after 早就夹了 60s 上限,这条同类路径当初漏了。
                    retry_after = _MAX_RETRY_AFTER_SEC
                    try:
                        raw = resp.json().get("parameters", {}).get("retry_after", 3)
                        retry_after = max(1.0, min(float(raw), _MAX_RETRY_AFTER_SEC))
                    except Exception:  # noqa: BLE001
                        retry_after = 3.0
                    if attempt < _MAX_SEND_ATTEMPTS:
                        logger.warning("TG 限流,{:.0f}s 后重试", retry_after)
                        time.sleep(retry_after + 1)
                        continue
                resp.raise_for_status()
                return True
            except httpx.HTTPStatusError as e:
                # 4xx(429 除外)重试也没用:多半是 HTML 没转义导致的 400。
                # 把原文打进日志,方便定位是哪条消息
                logger.error("Telegram 推送失败(HTTP {}): {} | text={!r}",
                             e.response.status_code, e, text[:200])
                if e.response.status_code < 500 and e.response.status_code != 429:
                    return False
            except Exception as e:  # noqa: BLE001
                # 传输层失败(代理掐断 / SSL EOF)。连接池里那条连接已经废了,整池丢弃重建
                logger.error("Telegram 推送失败(第 {} 次): {} | text={!r}", attempt, e, text[:200])
                self._reset_client()
            if attempt < _MAX_SEND_ATTEMPTS:
                time.sleep(_SEND_RETRY_SEC * attempt)
        return False

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        """
        回应一次按钮点击 —— 不回的话 TG 客户端会把按钮**转圈到超时**,
        用户以为卡住了会反复点,而每一次点击都是一条新的 update。
        """
        if not self.enabled:
            return False
        try:
            resp = self._client().post(
                f"https://api.telegram.org/bot{self._token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:200]},
            )
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("answerCallbackQuery 失败(不影响已执行的动作): {}", e)
            return False

    def edit_message(self, chat_id, message_id: int, text: str) -> bool:
        """
        改写已发出的消息,并**清掉键盘**(reply_markup 传空)。

        ⚠️ 清键盘是必须的:按钮留在那里就能被再点一次,
           而"再点一次"在真实下单模式下就是再买一单。
        """
        if not self.enabled:
            return False
        try:
            resp = self._client().post(
                f"https://api.telegram.org/bot{self._token}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE_LEN],
                      "parse_mode": "HTML", "disable_web_page_preview": True,
                      "reply_markup": {"inline_keyboard": []}},
            )
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("editMessageText 失败: {}", e)
            return False

    def set_my_commands(self, commands: list[tuple[str, str]]) -> bool:
        """
        注册命令菜单 —— 用户在输入框敲 `/` 时 Telegram 弹出的那个列表。

        ⚠️ 这是 Telegram 服务端保存的状态,不是消息:调一次就一直生效,
           所以每次启动调是幂等的,不会刷屏。
        ⚠️ command 只能是小写字母/数字/下划线,且**不带前导斜杠** ——
           带了斜杠 TG 会静默拒绝整个列表(返回 400),菜单就一直是空的。
        """
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self._token}/setMyCommands"
        payload = {
            "commands": [
                {"command": c.lstrip("/").lower(), "description": d[:256]}
                for c, d in commands
            ]
        }
        try:
            resp = self._client().post(url, json=payload)
            resp.raise_for_status()
            logger.info("已注册 {} 条命令菜单", len(payload["commands"]))
            return True
        except Exception as e:  # noqa: BLE001
            # 菜单注册失败不影响命令本身可用,降级为警告
            logger.warning("注册命令菜单失败(不影响命令可用): {}", e)
            return False

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        """
        长轮询拉取命令消息。返回 update 列表,失败返回空列表(不抛异常,由调用方决定重试)。
        timeout 是 TG 服务端 long-poll 的挂起秒数,httpx 超时必须比它长。
        """
        if not self.enabled:
            return []
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        # ⚠️ allowed_updates 是白名单:不写 callback_query,按钮点击**永远收不到**,
        #    而且没有任何报错 —— 表现是"按钮点了没反应",极难联想到是这里。
        params: dict = {"timeout": timeout,
                        "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            with httpx.Client(timeout=timeout + 15, proxy=self._proxy) as c:
                resp = c.get(url, params=params)
            resp.raise_for_status()
            return resp.json().get("result", []) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("getUpdates 失败: {}", e)
            return []
