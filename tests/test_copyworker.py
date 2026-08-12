"""
买入队列的单测。

⚠️ 这个文件盯的是**无人值守下没人看着时**才会发生的那类错误:
   排队太久还照买、停手开关拦不住已入队的单、队列满了安静丢掉、
   失败没有任何回执。有人点按钮的时候这些都能被人眼兜住,自动之后不能。
⚠️ 绝不真开浏览器:execute 是注入的。
"""
# ruff: noqa: N802
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from src import store
from src.copytrade import CopyConfig
from src.copyworker import MAX_QUEUE, ST_QUEUED, BuyJob, CopyWorker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    return store.DB_PATH


class _Notif:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str, **kw) -> bool:
        self.sent.append(text)
        return True


class _Res:
    def __init__(self, message="ok", confirmed=True):
        self.message, self.confirmed = message, confirmed
        self.ok = True


def _cfg_auto(**kw):
    """一份「已经开了无人值守」的配置"""
    with store.get_conn() as c:
        store.save_copy_config(c, CopyConfig(**{
            "enabled": True, "paper_only": False, "dry_run_execute": False,
            "auto_execute": True, "amount_usd": 40.0, "daily_spend_usd": 200.0, **kw}))


def _queued(ca="ca1", amount=40.0):
    with store.get_conn() as c:
        store.record_copy_signal(
            c, network_id="solana", token_address=ca, token_symbol="TOAD",
            buyers=2, entry_mcap=50_000.0, age_sec=60,
            amount_usd=amount, status=ST_QUEUED)


def _status(ca="ca1"):
    with store.get_conn() as c:
        r = c.execute("SELECT status, note FROM copytrade_signals WHERE token_address = ?",
                      (ca,)).fetchone()
    return (r["status"], r["note"] or "") if r else (None, "")


def _job(ca="ca1", *, age_sec=0.0, dry_run=False, amount=40.0):
    return BuyJob(network_id="solana", token_address=ca, symbol="TOAD",
                  amount_usd=amount, dry_run=dry_run,
                  queued_at=time.monotonic() - age_sec)


def _drain(w, timeout=3.0):
    """等队列跑空(worker 是后台线程)"""
    end = time.time() + timeout
    while time.time() < end:
        if w.pending == 0:
            time.sleep(0.15)      # 让最后一单写完终态
            if w.pending == 0:
                return
        time.sleep(0.02)
    raise AssertionError("队列没跑完")


# ============================================================
# 正常路径
# ============================================================
def test_成交后写filled并发回执(db):
    _cfg_auto()
    _queued()
    n = _Notif()
    w = CopyWorker(n, execute=lambda job, stop=None: _Res("已成交 $40.00 · 持仓 661 TOAD"))
    assert w.submit(_job())
    _drain(w)
    w.close()

    assert _status()[0] == "filled"
    assert n.sent and "已成交" in n.sent[0]
    assert "ca1" in n.sent[0], "回执必须带 CA —— 出事时要能直接去核对"


def test_点了但没读到仓位变化要单独一档(db):
    """
    ⚠️ 报「已成交」会让人以为没事;报「失败」又会诱使人手动再买一次。
       这一档恰恰是最需要人去看一眼的,而无人值守下没人会主动看。
    """
    _cfg_auto()
    _queued()
    n = _Notif()
    w = CopyWorker(n, execute=lambda job, stop=None: _Res("已点击但没读到仓位变化", confirmed=False))
    w.submit(_job())
    _drain(w)
    w.close()

    assert _status()[0] == "filled", "钱可能已经出去了,不能记成没成交"
    assert "待核对" in n.sent[0], f"必须与「已成交」区分开:{n.sent[0]}"


def test_演练模式绝不记filled(db):
    _cfg_auto()
    _queued()
    n = _Notif()
    w = CopyWorker(n, execute=lambda job, stop=None: _Res("演练通过"))
    w.submit(_job(dry_run=True))
    _drain(w)
    w.close()

    assert _status()[0] == "rejected", "演练根本没点成交,记 filled 会让 /paper 算假仓位"
    assert "演练" in n.sent[0]


def test_执行抛异常时记failed并告警(db):
    """⚠️ 全靠这条回执 —— 无人值守下没人点按钮,不发就等于什么都没发生过"""
    _cfg_auto()
    _queued()
    n = _Notif()

    def boom(job, stop=None):
        raise RuntimeError("浏览器起不来")

    w = CopyWorker(n, execute=boom)
    w.submit(_job())
    _drain(w)
    w.close()

    st, note = _status()
    assert st == "failed" and "浏览器起不来" in note
    assert n.sent and "未成交" in n.sent[0]


# ============================================================
# 护栏
# ============================================================
def test_排队太久就不买了(db):
    """
    跟单的全部价值在于早。$Plumber 第 2 个人进是 31.62x、第 5 个人进是 0.74x ——
    「晚三分钟买进去」不是打了折扣的成功,是另一笔交易。
    """
    _cfg_auto()
    _queued()
    n = _Notif()
    calls = []
    w = CopyWorker(n, execute=lambda job, stop=None: calls.append(job) or _Res())
    w.submit(_job(age_sec=999))
    _drain(w)
    w.close()

    assert calls == [], "超时的单绝不能还去执行"
    assert _status()[0] == "failed"
    assert "超时" in n.sent[0]


def test_执行前重查配置_停手要拦得住已入队的单(db):
    """
    ⚠️ 配置是 tick 里读的快照,而这一单可能是一分钟前入的队。
       /copy off 只对未来生效的话,那就不叫「停手」。
    """
    _cfg_auto()
    _queued()
    n = _Notif()
    calls = []
    w = CopyWorker(n, execute=lambda job, stop=None: calls.append(job) or _Res())

    # 入队之后、执行之前被关掉
    with store.get_conn() as c:
        store.save_copy_config(c, replace(store.load_copy_config(c), enabled=False))
    w.submit(_job())
    _drain(w)
    w.close()

    assert calls == [], "已经关掉了还去下单 = 停手开关没用"
    assert _status()[0] == "failed"
    assert "关闭" in n.sent[0]


def test_队列满了返回False而不是排着等(db):
    """
    ⚠️ submit 跑在轮询 tick 里,阻塞它等于把监控停下来等下单。
       满了必须**当场拒绝**,让调用方去记 failed。
    """
    _cfg_auto()
    n = _Notif()
    started, release = [], []

    def slow(job, stop=None):
        started.append(job)
        while not release:
            time.sleep(0.01)
        return _Res()

    w = CopyWorker(n, execute=slow)
    try:
        # 1 单在跑 + MAX_QUEUE 单在排 = 满
        accepted = sum(1 for i in range(MAX_QUEUE + 5) if w.submit(_job(f"ca{i}")))
        assert accepted <= MAX_QUEUE + 1, f"最多接 {MAX_QUEUE + 1} 单,实际接了 {accepted}"
        assert accepted >= MAX_QUEUE, "别拒绝得太狠"
    finally:
        release.append(1)
        w.close()


def test_只有从auto_queued抢到的才执行(db):
    """
    重复入队、或人从别的入口把这条改掉了 —— 抢不到就不该执行。
    ⚠️ 这是「不会买两次」的最后一道,不能只靠浏览器 profile 锁。
    """
    _cfg_auto()
    _queued()
    with store.get_conn() as c:                       # 已经被别人改成终态了
        store.set_copy_status(c, "solana", "ca1", "rejected", expect=ST_QUEUED)

    n = _Notif()
    calls = []
    w = CopyWorker(n, execute=lambda job, stop=None: calls.append(job) or _Res())
    w.submit(_job())
    _drain(w)
    w.close()

    assert calls == [], "抢不到状态就不能执行"
    assert _status()[0] == "rejected", "别人写的终态不能被覆盖"


def test_close之后不再接单(db):
    _cfg_auto()
    n = _Notif()
    w = CopyWorker(n, execute=lambda job, stop=None: _Res())
    w.close()
    assert w.submit(_job()) is False


def test_执行途中关掉跟单_急停回调要能拦下(db):
    """
    ⚠️ 「立刻停手」的意思是**正在跑的这一单**也要停,不是只对下一单生效。
       所以 should_stop 每次都重查配置,不能用 tick 里那份快照。
    """
    _cfg_auto()
    _queued()
    n = _Notif()
    seen = []

    def watch(job, stop):
        # 模拟执行器:走到"点成交前"那个检查点时,配置已经被改掉了
        with store.get_conn() as c:
            store.save_copy_config(c, replace(store.load_copy_config(c), auto_execute=False))
        seen.append(stop())
        if stop():
            raise RuntimeError("执行中途收到停止指令(点成交前),未下单")
        return _Res()

    w = CopyWorker(n, execute=watch)
    w.submit(_job())
    _drain(w)
    w.close()

    assert seen == [True], f"执行途中改了配置,急停必须为真:{seen}"
    assert _status()[0] == "failed"


def test_急停查库出错时不要变成永远停手(db):
    """停手判断本身挂掉不该让跟单静默失效 —— 真要停有 close()"""
    _cfg_auto()
    _queued()
    n = _Notif()
    got = []

    def watch(job, stop):
        got.append(stop())
        return _Res()

    w = CopyWorker(n, execute=watch)
    w.submit(_job())
    _drain(w)
    w.close()
    assert got == [False], "配置正常时不该停"


def test_一条单炸了不会让整条线程死掉(db):
    """⚠️ 线程死了 = 跟单静默停摆,而且没有任何迹象"""
    _cfg_auto()
    _queued("ca1")
    _queued("ca2")
    n = _Notif()
    seen = []

    def flaky(job, stop=None):
        seen.append(job.token_address)
        if job.token_address == "ca1":
            raise RuntimeError("第一单炸了")
        return _Res()

    w = CopyWorker(n, execute=flaky)
    w.submit(_job("ca1"))
    w.submit(_job("ca2"))
    _drain(w)
    w.close()

    assert seen == ["ca1", "ca2"], f"第二单必须照常执行,实际 {seen}"
    assert _status("ca2")[0] == "filled"

