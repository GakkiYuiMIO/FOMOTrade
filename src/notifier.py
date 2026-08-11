"""
Telegram 推送模块
- 用 httpx 直接调 Bot API,避免引入重 SDK
- 未配置 Token/Chat ID 时自动降级为仅日志输出
- 处理 429 限流:按 retry_after 退避后重试一次
"""
import time

import httpx
from loguru import logger

from src.config import get_settings

# TG 单条消息硬上限 4096 字符,留点余量
MAX_MESSAGE_LEN = 4000


class TelegramNotifier:
    """轻量 Telegram 推送"""

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.tg_token
        self._chat_id = settings.fomo_telegram_chat_id
        self._proxy = settings.fomo_proxy
        self.enabled = settings.tg_enabled

        if self.enabled:
            logger.info("Telegram 推送已启用 | chat_id={}", self._chat_id)
        else:
            logger.warning("Telegram 未配置,推送将只写入日志")

    def _client(self) -> httpx.Client:
        # 国内直连 api.telegram.org 通常不通,走代理
        return httpx.Client(timeout=20.0, proxy=self._proxy)

    def send(self, text: str, parse_mode: str = "HTML", chat_id: str | None = None) -> bool:
        """
        同步发送消息。支持 HTML 标签: <b> <i> <code> <blockquote>
        返回 True 表示 TG 确认收到 —— 调用方据此把 fomo_events.sent 置 1,
        绝不能"发之前就标已发",否则崩在中间会永久丢消息。
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

        for attempt in (1, 2):
            try:
                with self._client() as c:
                    resp = c.post(url, json=payload)
                # 429 限流:TG 会在 body 里给 retry_after,退避后重试一次
                if resp.status_code == 429:
                    retry_after = 3
                    try:
                        retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
                    except Exception:  # noqa: BLE001
                        pass
                    if attempt == 1:
                        logger.warning("TG 限流,{}s 后重试", retry_after)
                        time.sleep(retry_after + 1)
                        continue
                resp.raise_for_status()
                return True
            except Exception as e:  # noqa: BLE001
                # 400 多半是 HTML 没转义 —— 把原文打进日志,方便定位是哪条消息
                logger.error("Telegram 推送失败(第 {} 次): {} | text={!r}", attempt, e, text[:200])
                if attempt == 2:
                    return False
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
            with self._client() as c:
                resp = c.post(url, json=payload)
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
        params: dict = {"timeout": timeout, "allowed_updates": '["message"]'}
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
