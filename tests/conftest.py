"""
pytest 公共夹具与构造器。

⚠️ 全部测试都跑在 sqlite3 内存库上,**绝不碰 data/fomo.db** ——
   单测污染真实库会让"共识数为什么是这个值"变成无法排查的问题。
⚠️ 内存连接必须 isolation_level=None:store.tx() 显式发 "BEGIN IMMEDIATE",
   Python 默认的隐式事务管理会在这里报 "cannot start a transaction within a transaction"。
"""
from __future__ import annotations

import sqlite3

import pytest

from src import store
from src.config import get_settings
from src.models import EVENT_BUY, FomoEvent, dump_raw


@pytest.fixture(autouse=True, scope="session")
def _never_touch_production_db(tmp_path_factory):
    """
    整场测试的**兜底防线**:把 store.DB_PATH 从 data/fomo.db 挪开。

    ⚠️⚠️ 为什么需要它:store.DB_PATH 是模块级全局,而 get_conn() 是**读写**打开
       (它会发 PRAGMA journal_mode = WAL)。任何一条走了 get_conn() 却没用 db 夹具的
       用例,都会直接连上用户**正在运行**的生产库。
       这不是假想 —— 2026-08-26 真的发生过一次:一条用例用 monkeypatch.undo()
       还原自己打的桩,把 db 夹具的 DB_PATH 补丁**一并**还原了(monkeypatch 是
       函数级共享的),后半段就打在了生产库上。
       靠"每条用例记得加 db 夹具"守不住,这里在会话级把地板铺死。
    ⚠️ 只挪路径、不建库:需要真实文件的用例仍然要用 db 夹具(它负责 init_db)。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path_factory.mktemp("nodb") / "never_used.db")
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _clear_stop_flag():
    """
    client 的停机 Event 是**模块级全局**。某个用例设了它而不清,
    后面所有用例的请求都会被当成"停机中"直接放弃 —— 而且失败信息完全指不到根因。
    """
    from src import client as _c

    _c.reset_stop()
    yield
    _c.reset_stop()


@pytest.fixture(autouse=True)
def _no_send_throttle(monkeypatch):
    """
    单测里不要真的 sleep。

    poller._dispatch 每发一条消息就 sleep(fomo_send_interval_sec),默认 3.5s ——
    那是给 Telegram 同 chat 约 20 msg/min 的限流用的,单测既不发真消息也没有限流,
    白等只会让整套测试从 0.2 秒涨到 25 秒,拖垮开发循环。
    get_settings 带 lru_cache,改环境变量前后都要清一次。
    """
    monkeypatch.setenv("FOMO_SEND_INTERVAL_SEC", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

# ---- 测试用常量 ----------------------------------------------------------
# Solana base58 CA:大小写敏感,任何 lower() 都会把它改坏
CA_TOAD = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
# EVM CA:checksum 混合大小写,归一化后必须全小写
CA_CATE_CHECKSUM = "0x8F2a4C19e7B3d5A0f16C88bE2d7419Aa3c05E6F1"
CA_CATE = CA_CATE_CHECKSUM.lower()
# 计价币(Solana),必须与 models.QUOTE_TOKENS 里的字面量完全一致
CA_WSOL = "So11111111111111111111111111111111111111112"
CA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TS_EARLY = "2026-08-01T00:00:00+00:00"
TS_MID = "2026-08-05T12:00:00+00:00"
TS_LATE = "2026-08-09T23:59:59+00:00"


@pytest.fixture
def conn() -> sqlite3.Connection:
    """建好表的内存库。每个测试一份,互不干扰。"""
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    store.init_db(c)
    try:
        yield c
    finally:
        c.close()


# ---- 构造器 --------------------------------------------------------------
def make_event(
    event_type: str = EVENT_BUY,
    *,
    event_id: str = "BUY:e1",
    user_id: str = "u1",
    handle: str | None = "maxpain",
    network_id: str | None = "solana",
    token_address: str | None = CA_TOAD,
    token_symbol: str | None = "TOAD",
    event_ts: str = TS_MID,
    **kw,
) -> FomoEvent:
    """构造一条事件。默认是一笔"数据齐全的 Solana 买入",测试只覆盖它要变的那几个字段。"""
    return FomoEvent(
        event_id=event_id,
        event_type=event_type,
        user_id=user_id,
        event_ts=event_ts,
        raw_json=dump_raw({"_test": event_id}),
        handle=handle,
        network_id=network_id,
        token_address=token_address,
        token_symbol=token_symbol,
        **kw,
    )


def add_user(conn, user_id: str, handle: str, *, ready: bool = True) -> None:
    """加一个监控用户。ready=True 表示历史基线已建好(可打徽章、计入共识分子分母)。"""
    store.add_watch_user(conn, user_id, handle, handle)
    if ready:
        store.mark_stats_ready(conn, user_id)


def force_stats(conn, user_id: str, network_id: str, token_address: str,
                buy_count: int = 1, first_buy_at: str | None = None) -> None:
    """
    绕过 store 的判定,直接写一行 user_token_stats。

    只用于构造"脏数据"场景 —— 比如给未就绪用户塞一行,验证共识 SQL 的 JOIN
    确实把他挡在分子外面(不挡住就会出现分子 > 分母)。
    """
    conn.execute(
        "INSERT OR REPLACE INTO user_token_stats "
        "(user_id, network_id, token_address, buy_count, first_buy_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-08-01T00:00:00+00:00')",
        (user_id, network_id, token_address, buy_count, first_buy_at),
    )
