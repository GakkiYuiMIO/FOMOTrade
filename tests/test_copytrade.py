"""
跟单判定的单测。

⚠️ 这个文件盯的是**会让人亏钱的那一类错误**:
   过滤器在最该起作用的时候静默失效。
   比如"拿不到币龄就放行" —— 而币龄恰恰是新币最容易缺的字段,
   等于筛选器专挑最该拦的那些币放过去。
"""
# ruff: noqa: N802
from __future__ import annotations

import time

import pytest

from src.copytrade import (
    SKIP_ALREADY,
    SKIP_DAILY,
    SKIP_DISABLED,
    SKIP_MCAP,
    SKIP_NETWORK,
    SKIP_NO_AGE,
    SKIP_NO_MCAP,
    SKIP_NOT_ENOUGH,
    SKIP_TOO_OLD,
    Candidate,
    CopyConfig,
    decide,
    pnl,
)

NOW = 1_800_000_000


def _cand(**kw) -> Candidate:
    base = {
        "network_id": "solana", "token_address": "CA1", "token_symbol": "TOAD",
        "buyers": 3, "entry_mcap": 50_000.0,
        "token_created_at": NOW - 3600,      # 1 小时的新币
        "already_taken": False, "taken_today": 0,
    }
    return Candidate(**{**base, **kw})


def _cfg(**kw) -> CopyConfig:
    return CopyConfig(**{"enabled": True, **kw})


def test_默认不启用():
    """⚠️ 升级一版就自己开始跟单是绝对不能发生的事"""
    assert CopyConfig().enabled is False
    assert CopyConfig().paper_only is True, "默认必须是纸上跟单"
    assert decide(_cand(), CopyConfig(), now=NOW).reason == SKIP_DISABLED


def test_人数够了才跟():
    assert decide(_cand(buyers=2), _cfg(min_buyers=2), now=NOW).take
    assert decide(_cand(buyers=1), _cfg(min_buyers=2), now=NOW).reason == SKIP_NOT_ENOUGH


def test_同一个币只跟一次():
    assert decide(_cand(already_taken=True), _cfg(), now=NOW).reason == SKIP_ALREADY


def test_币龄超上限不跟():
    old = _cand(token_created_at=NOW - 48 * 3600)
    assert decide(old, _cfg(max_age_hours=24), now=NOW).reason == SKIP_TOO_OLD
    assert decide(old, _cfg(max_age_hours=None), now=NOW).take, "不限时应当放行"


def test_拿不到币龄一律不跟():
    """
    ⚠️ 这条最要紧。币龄来自 balances,而 balances 快照晚于 swaps 索引 ——
       **最新的币最容易缺这个字段**。放行等于筛选器专挑最该拦的那些放过去。
    """
    c = _cand(token_created_at=None)
    assert decide(c, _cfg(max_age_hours=24), now=NOW).reason == SKIP_NO_AGE
    assert decide(c, _cfg(max_age_hours=None), now=NOW).take, "不设上限时才放行"


def test_入场市值超上限不跟():
    c = _cand(entry_mcap=5_000_000.0)
    assert decide(c, _cfg(max_entry_mcap=500_000), now=NOW).reason == SKIP_MCAP
    assert decide(c, _cfg(max_entry_mcap=None), now=NOW).take


def test_设了市值上限却拿不到市值时不跟():
    """同上:宁可漏一单,不可在不知道买的是什么的情况下建仓"""
    c = _cand(entry_mcap=None)
    assert decide(c, _cfg(max_entry_mcap=500_000), now=NOW).reason == SKIP_NO_MCAP


def test_未来时间戳当成不合格():
    """脏时间戳会算出负币龄,绝不能因为"负数 < 上限"就放行"""
    c = _cand(token_created_at=NOW + 86400)
    assert decide(c, _cfg(max_age_hours=24), now=NOW).reason == SKIP_TOO_OLD


def test_链白名单():
    c = _cand(network_id="bsc")
    assert decide(c, _cfg(networks=("solana",)), now=NOW).reason == SKIP_NETWORK
    assert decide(c, _cfg(networks=()), now=NOW).take, "空白名单 = 不限"


def test_每日上限():
    assert decide(_cand(taken_today=10), _cfg(daily_max=10), now=NOW).reason == SKIP_DAILY
    assert decide(_cand(taken_today=10), _cfg(daily_max=0), now=NOW).take, "0 = 不限"


def test_每日上限排在最后判():
    """
    ⚠️ 达到上限之后,如果先判它,所有币的原因都变成"已达当日上限",
       就看不出哪些币其实本来也不合格 —— 而那才是调参数时要看的。
    """
    bad = _cand(buyers=1, taken_today=99)
    assert decide(bad, _cfg(min_buyers=3, daily_max=10), now=NOW).reason == SKIP_NOT_ENOUGH


def test_纸上盈亏按市值比折算():
    assert pnl(50_000, 150_000, 100.0) == (300.0, 3.0)
    assert pnl(50_000, 25_000, 100.0) == (50.0, 0.5)


@pytest.mark.parametrize("entry,now_", [(None, 100), (100, None), (0, 100)])
def test_盈亏缺输入时不显示(entry, now_):
    """绝不显示成 0 —— 那会被读成「亏光了」"""
    assert pnl(entry, now_, 100.0) is None


def test_now_默认取当前时间():
    """不传 now 时必须走真实时间,否则线上永远按某个固定时刻判币龄"""
    c = _cand(token_created_at=int(time.time()) - 3600)
    assert decide(c, _cfg(max_age_hours=24)).take


# ============================================================
# TG 确认按钮
# ⚠️ 这条路径会**花钱**,每一条都是"点两次会不会买两次"这类问题
# ============================================================
class _Notif:
    def __init__(self):
        self.enabled = True
        self.sent: list[tuple[str, list | None]] = []
        self.answers: list[str] = []
        self.edits: list[str] = []

    def send(self, text, parse_mode="HTML", chat_id=None, buttons=None):
        self.sent.append((text, buttons))
        return True

    def answer_callback(self, cb_id, text=""):
        self.answers.append(text)
        return True

    def edit_message(self, chat_id, message_id, text):
        self.edits.append(text)
        return True

    def get_updates(self, offset=None, timeout=30):
        return []


def _bot(monkeypatch, tmp_path, notif):
    from src import store
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cb.db")
    store.init_db()
    monkeypatch.setenv("FOMO_TELEGRAM_CHAT_ID", "999")
    b = CommandBot(client=None, notifier=notif)
    monkeypatch.setattr(type(b._settings), "admin_chat_id", property(lambda s: "999"))
    return b, store


def _pending(store, ca="GCa9TZMK9Q3VUSkh1234", sym="TOAD"):
    with store.get_conn() as c:
        store.record_copy_signal(c, network_id="solana", token_address=ca, token_symbol=sym,
                                 buyers=2, entry_mcap=50_000.0, age_sec=3600,
                                 amount_usd=50.0, status="pending")
    return f"solana:{ca[:12]}"


def test_非管理员点按钮一律拒绝(monkeypatch, tmp_path):
    """
    ⚠️ 消息可能被转发到别的群,那里的人点按钮同样会产生 callback。
       这条路径会花钱,门不能比命令层松。
    """
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    b._handle_callback({"id": "1", "data": f"buy:{key}",
                        "message": {"message_id": 1, "chat": {"id": "12345"}}})
    assert n.answers == ["无权限"]
    with store_.get_conn() as c:
        assert c.execute("SELECT status FROM copytrade_signals").fetchone()["status"] == "pending"


def test_点忽略只改状态不成交(monkeypatch, tmp_path):
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    b._handle_callback({"id": "1", "data": f"skip:{key}",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})
    with store_.get_conn() as c:
        assert c.execute("SELECT status FROM copytrade_signals").fetchone()["status"] == "rejected"
    assert n.edits and "已忽略" in n.edits[0]


def test_连点两次不会重复处理(monkeypatch, tmp_path):
    """连点、或消息被转发后两个人各点一次 —— 都必须只生效一次"""
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    ev = {"id": "1", "data": f"skip:{key}", "message": {"message_id": 1, "chat": {"id": "999"}}}
    b._handle_callback(ev)
    b._handle_callback(ev)
    assert "已经处理过了" in n.answers[1]


def test_下单未接入时明确失败而不是假装成交(monkeypatch, tmp_path):
    """
    ⚠️ 把状态写成 filled 却没真的买,是这个功能最坏的一种失效:
       台账显示"已成交"、`/paper` 给你算着盈亏,而你根本没有这个仓位。
    """
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    b._handle_callback({"id": "1", "data": f"buy:{key}",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})
    with store_.get_conn() as c:
        assert c.execute("SELECT status FROM copytrade_signals").fetchone()["status"] == "failed"
    assert "没有成交" in n.answers[0]
    assert n.edits and "未成交" in n.edits[0]


def test_找不到信号时不报错(monkeypatch, tmp_path):
    n = _Notif()
    b, _ = _bot(monkeypatch, tmp_path, n)
    b._handle_callback({"id": "1", "data": "buy:solana:deadbeef",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})
    assert "找不到" in n.answers[0]


def test_callback_data不超TG的64字节上限():
    """超了 TG 直接 400,而消息会**没有按钮地发出去** —— 你以为能点,其实不能"""
    ca = "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump"
    for prefix in ("buy", "skip"):
        data = f"{prefix}:solana:{ca[:12]}"
        assert len(data.encode()) <= 64, data
