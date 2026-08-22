"""
网页版的数据库入口 —— **本模块是整个 src/web 里唯一允许打开数据库的地方。**

⚠️ 绝不能复用 store.get_conn():那个打开的是**读写**连接。
   fomo_cursors / badge / cs_* 都是「写了就不能改」的冻结列,一次误写就是永久污染,
   而且不会有任何告警。所以这里用 mode=ro —— 写在驱动层就被拒绝,不依赖自觉。

⚠️ 为什么还留一个 query_only 兜底:极端情况下(poller 崩溃留下未 checkpoint 的 WAL),
   只读连接无法执行 WAL 恢复,会打不开。那时退回普通连接 + PRAGMA query_only,
   它同样让写返回 SQLITE_READONLY,只是理论上能被再一次 PRAGMA 关掉。
   两条路都实测过可行。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from loguru import logger

from src.store import DB_PATH


class WebDbError(RuntimeError):
    """数据库打不开。⚠️ 报错要能让人照着做,不要只丢 sqlite 的原文"""


@contextmanager
def readonly_conn():
    """
    只读连接。每个请求开一次、用完立刻关。

    ⚠️ 不做连接池:泄漏的读事务会把 WAL 钉住、无上限增长、全程静默无报错 ——
       这是本设计最隐蔽的风险,而连接池正是最容易泄漏的地方。
    """
    if not DB_PATH.exists():
        raise WebDbError(f"找不到数据库 {DB_PATH} —— 先跑 .\\bot.ps1 --run 让它建库")

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.OperationalError as e:
        logger.warning("只读方式打不开库,退回 query_only: {}", e)
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        conn.execute("PRAGMA query_only = ON")

    conn.row_factory = sqlite3.Row
    # ⚠️ busy_timeout 必须设:Windows 上锁竞争很常见,不设会直接抛 database is locked
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()
