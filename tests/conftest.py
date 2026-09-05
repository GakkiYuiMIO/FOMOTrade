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
def _no_dexscreener_network(monkeypatch):
    """
    整场测试的兜底防线之二:**没有任何一条用例可以真的去打 DexScreener**。

    ⚠️⚠️ 为什么需要它:Poller / PumpWatcher 在 __init__ 里各建一个 PoolQuoteLookup,
       它默认自带一个真实的 DexScreenerClient。任何一条走到 _dispatch / _check 的
       用例都会**真的发 HTTP 请求出去** —— 这不是假想:这个功能刚接上去、还没加这道
       防线时跑了一次全量,1024 条用例从 20 秒涨到 **132 秒**,涨的全是真实网络往返,
       等于每跑一次测试就拿用户的 IP 去挨一遍限流。
       靠"每条用例记得注入假 client"守不住,这里把地板铺死。
    ⚠️ 桩成"请求失败"(返回 None)而不是抛异常:None 是这个功能的正常降级路径
       (那一行整行消失),于是**没显式关心底池的用例行为与改造前完全一致**。
       要测这条路的用例自己注入 PoolQuoteLookup(client=假 client)。
    """
    from src import dexscreener as _d

    # ⚠️ 签名跟着端点走:/latest/dex/tokens 不带链,所以这里也不收 slug。
    #    参数对不上不会报错、只会在调用处炸 TypeError,那是很难读的失败。
    monkeypatch.setattr(_d.DexScreenerClient, "fetch_pairs",
                        lambda self, addresses: None)
    yield


@pytest.fixture(autouse=True)
def _no_tokeninfo_network(monkeypatch):
    """
    整场测试的兜底防线之三:**没有任何一条用例可以真的去打 filterTokens / Blockscout**。

    ⚠️⚠️ 与 _no_dexscreener_network 同一条理由,而且这条更急:filterTokens 的限流
       实测是"无间隔连打第 10 次就 429、冷却 203 秒"。一次全量测试有上百条用例会
       走到 _dispatch —— 那等于拿用户的 IP 去撞限流,还会把真实业务按住三分半钟。
       接上去、还没加这道防线时跑的那一次全量,时长从 53s 涨到 75s,日志里
       实打实印着「filterTokens 限速闸未就绪」—— 请求真的发出去了。
    ⚠️ 桩成"请求失败"(返回 None / (None, 0))而不是抛异常:那是这个功能的正常
       降级路径(那两行整行消失),于是**没显式关心这三行的用例行为与改造前完全一致**。
       要测这条路的用例自己注入假 client(TokenExtraLookup(filter_client=…))。
    ⚠️ 连限速闸的 sleep 也一并桩掉:闸是**进程级单例**,一条用例把它推后 10 秒,
       后面所有用例都要为它等 —— 那是测试之间的隐性耦合。
    """
    from src import tokeninfo as _t

    monkeypatch.setattr(_t.FilterTokensClient, "fetch",
                        lambda self, keys: (None, 0.0))
    monkeypatch.setattr(_t.BlockscoutClient, "_get", lambda self, path, tag: None)
    monkeypatch.setattr(_t._GATE, "_next_at", 0.0, raising=False)
    monkeypatch.setattr(_t._GATE, "_sleep", lambda _s: None, raising=False)

    # ⚠️⚠️ pump.fun 的那把闸同理,而且它是后加的、当初漏了桩 ——
    #    复验实测:整场测试**真的 sleep 了 39.0 秒**,而且是进程级跨用例耦合
    #    (一条用例把 _next_at 推后,后面所有用例陪等)。与上面那把同一条理由。
    # ⚠️ 要测闸本身的用例请自己造实例(`PumpRateGate(clock=…, sleep=…)`),
    #    别依赖这把单例 —— 见 tests/test_pumpfun.py::Test限速闸。
    from src import pumpfun as _pf

    monkeypatch.setattr(_pf._GATE, "_next_at", 0.0, raising=False)
    monkeypatch.setattr(_pf._GATE, "_sleep", lambda _s: None, raising=False)
    yield


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


@pytest.fixture(autouse=True)
def _no_namecn_network(monkeypatch):
    """
    整场测试的兜底防线之三:**没有任何一条用例可以真的去打维基 / Google / Yahoo**。

    ⚠️⚠️ 与 _no_dexscreener_network 同一条理由:Poller / PumpWatcher 在 __init__ 里各建一个
       NameGlossary,它默认自带真实的 curl_cffi 传输层。一条给了 DexScreener 假响应的用例
       (币名因此有了)会顺着 token_zh → 维基/Google 真的发请求出去。
    ⚠️ 桩成"网络失败"(抛 UnavailableError)而不是返回假数据:那是这个功能的正常降级路径
       (中文名 / 股票说明那几行整行消失),于是**没显式关心它们的用例行为与改造前完全一致**。
       要测这条路的用例自己注入 NameGlossary(client=NameClient(transport=假传输层))。
    """
    from src import namecn as _n

    def _boom(self, url, params=None, headers=None):
        raise _n.UnavailableError("测试环境禁止外呼")

    monkeypatch.setattr(_n.CurlTransport, "get_json", _boom)
    yield
