"""
日志模块 —— 基于 loguru
- 控制台彩色输出
- logs/ 目录按天切分,保留 30 天
- 自动脱敏疑似 token / key 的长字符串
"""
import re
import sys

from loguru import logger

from src.config import PROJECT_ROOT, get_settings

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 敏感词脱敏正则(匹配看起来像 token/key 的 40+ 位字符串)
# Privy 的 JWT 和 refresh token 都会被这条命中
_SENSITIVE_PATTERN = re.compile(r"\b([A-Za-z0-9_-]{20})[A-Za-z0-9_-]{20,}\b")

_INITIALIZED = False


def _mask_sensitive(record: dict) -> None:
    """日志脱敏:把长字符串(疑似 token)中间替换为 ***"""
    record["message"] = _SENSITIVE_PATTERN.sub(r"\1***", record["message"])


def setup_logger() -> None:
    """初始化全局日志配置,幂等(重复调用不会叠加 handler)"""
    global _INITIALIZED
    if _INITIALIZED:
        return

    settings = get_settings()
    logger.remove()  # 移除默认 handler

    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}:{function}:{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        filter=lambda r: (_mask_sensitive(r), True)[1],
    )

    logger.add(
        LOG_DIR / "fomo_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        filter=lambda r: (_mask_sensitive(r), True)[1],
    )

    _INITIALIZED = True
    logger.info("日志系统已初始化")
