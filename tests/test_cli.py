"""
⚠️ 这个文件钉两件事:
  1) main() 里 --web 必须跳过 store.init_db();
  2) cmd_run 必须把币安 Alpha 巡检真的注册成调度器里的第二个 job。

那是个读写连接(见 src/web/db.py 顶部注释),网页版整个安全设计的前提是
"这个进程物理上写不进库" —— 如果 main() 在分发到 cmd_web() 之前就无条件建了库,
这个前提在进程启动的第一行就破了。
"""
# ruff: noqa: N802
from __future__ import annotations

from unittest.mock import Mock

from src import cli, store


def test_web参数跳过init_db(monkeypatch):
    """--web 分发前不能调用 store.init_db(),否则读写连接已经开过一次了"""
    monkeypatch.setattr(cli, "setup_logger", Mock())
    init_db_mock = Mock()
    monkeypatch.setattr(store, "init_db", init_db_mock)
    cmd_web_mock = Mock(return_value=0)
    monkeypatch.setattr(cli, "cmd_web", cmd_web_mock)
    monkeypatch.setattr("sys.argv", ["fomo", "--web"])

    rc = cli.main()

    assert rc == 0
    init_db_mock.assert_not_called()
    cmd_web_mock.assert_called_once()


def test_其他命令仍会建库(monkeypatch):
    """对照组:证明上面那条跳过是 --web 专属分支,不是 main() 整体不建库了"""
    monkeypatch.setattr(cli, "setup_logger", Mock())
    init_db_mock = Mock()
    monkeypatch.setattr(store, "init_db", init_db_mock)
    cmd_dry_run_mock = Mock(return_value=0)
    monkeypatch.setattr(cli, "cmd_dry_run", cmd_dry_run_mock)
    monkeypatch.setattr("sys.argv", ["fomo", "--dry-run"])

    rc = cli.main()

    assert rc == 0
    init_db_mock.assert_called_once()
    cmd_dry_run_mock.assert_called_once()


# ============================================================
# cmd_run 有没有真的把 job 接进调度器
# ============================================================
# ⚠️⚠️ 仓库原先**没有任何测试**碰过 cmd_run。把 cmd_run 里的
#    `if s.fomo_alpha_enabled:` 改成 `if False:`(= 整个币安 Alpha 功能压根不接进
#    APScheduler)时,全部测试照样绿 —— 而用户端的表现是「一条推送都收不到」,
#    日志里没有任何报错。所以这两条必须存在。
class _FakeScheduler:
    """记下注册了哪些 job。start() 立刻返回,让 cmd_run 走完 finally 那段清理。"""

    def __init__(self, **kw) -> None:
        self.jobs: list[dict] = []
        self.running = False

    def add_job(self, func, trigger, **kw) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kw})

    def start(self) -> None:
        return None

    def shutdown(self, wait: bool = True) -> None:
        self.running = False


def _stub_run(monkeypatch, settings):
    """
    把 cmd_run 的外部依赖全部换掉:不建 client、不连 TG、不开真调度器、不碰库。

    ⚠️ build_client / CommandBot / Poller / BlockingScheduler / AlphaWatcher 都是在
       cmd_run **函数体内**才 import 的,所以要打到各自的源模块上,而不是 cli 的命名空间。
    """
    sched = _FakeScheduler()
    alpha_cls = Mock()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "TelegramNotifier", Mock())
    monkeypatch.setattr(cli, "request_stop", Mock())
    monkeypatch.setattr(cli, "_reconcile_copytrade", Mock())
    monkeypatch.setattr("apscheduler.schedulers.blocking.BlockingScheduler",
                        lambda **kw: sched)
    monkeypatch.setattr("src.client.build_client", Mock())
    monkeypatch.setattr("src.poller.Poller", Mock())
    monkeypatch.setattr("src.bot.CommandBot", Mock())
    monkeypatch.setattr("src.binance_alpha.AlphaWatcher", alpha_cls)
    return sched, alpha_cls


def test_cmd_run把币安Alpha注册成独立的第二个job(monkeypatch):
    """
    开关打开时,巡检必须真的成为调度器里的一个 job。

    ⚠️ 同时钉住「它是**第二个独立** job」而不是被塞进 fomo_tick:
       tick 里抛 AuthError 会 sched.shutdown(),币安接口抖一下绝不该有权力
       停掉整条 FOMO 推送链路。
    """
    from src.config import FomoSettings

    s = FomoSettings(fomo_alpha_enabled=True, fomo_alpha_interval_sec=300)
    sched, alpha_cls = _stub_run(monkeypatch, s)

    assert cli.cmd_run() == 0

    ids = [j["id"] for j in sched.jobs]
    assert "fomo_tick" in ids, "对照:主轮询本来就该在"
    assert "binance_alpha" in ids, "开关打开却没注册 = 用户一条推送都收不到,且日志无异常"
    job = next(j for j in sched.jobs if j["id"] == "binance_alpha")
    assert job["func"] is alpha_cls.return_value.run_once, \
        "注册进去的必须是 AlphaWatcher.run_once —— 它是唯一契约上不抛异常的入口"
    assert job["trigger"] == "interval"
    assert job["seconds"] == 300
    assert job["max_instances"] == 1, "两轮叠在一起会重复推送"


def test_关掉开关就不注册币安Alpha的job(monkeypatch):
    """对照组:证明上面那条钉的是开关真的生效,而不是无条件注册。"""
    from src.config import FomoSettings

    # ⚠️ 三个可选开关**必须全部显式关掉**:只写 alpha 那一个的话,
    #    其余仍然从仓库根 .env 读 —— 这条用例的结果就取决于「这台机器怎么配的」
    #    (实测 .env 里 FOMO_PUMP_ENABLED=true 时它恒红,而被测行为完全正常)。
    s = FomoSettings(fomo_alpha_enabled=False,
                     fomo_pump_enabled=False,
                     fomo_pump_callout_enabled=False)
    sched, _ = _stub_run(monkeypatch, s)

    assert cli.cmd_run() == 0
    assert [j["id"] for j in sched.jobs] == ["fomo_tick"]


# ============================================================
# pump.fun 的两个 job:买卖 / 观点,两个**互相独立**的开关
# ============================================================
# ⚠️⚠️ 与上面币安 Alpha 那两条同一条理由:仓库原先没有任何测试碰过 cmd_run,
#    把 `if s.fomo_pump_callout_enabled:` 改成 `if False:`(= 观点功能压根没接进
#    APScheduler)时,别的测试照样全绿 —— 而用户端的表现是「一条观点都收不到」,
#    日志里没有任何报错。
def _stub_pump(monkeypatch, settings):
    """在 _stub_run 的基础上,再把 pump 的两个 watcher 换成 Mock。"""
    sched, _alpha = _stub_run(monkeypatch, settings)
    pump_cls, callout_cls = Mock(), Mock()
    monkeypatch.setattr("src.pumpfun.PumpWatcher", pump_cls)
    monkeypatch.setattr("src.pumpfun.PumpCalloutWatcher", callout_cls)
    return sched, pump_cls, callout_cls


def test_观点开关打开时注册成独立的第五个job(monkeypatch):
    """
    ⚠️ 同时钉住「它是**独立的一个** job」:与买卖那个分开注册,
       portfolio 挂了不该让观点也停,反之亦然。
    """
    from src.config import FomoSettings

    s = FomoSettings(fomo_alpha_enabled=False, fomo_pump_enabled=False,
                     fomo_pump_callout_enabled=True, fomo_pump_interval_sec=90)
    sched, _pump, callout_cls = _stub_pump(monkeypatch, s)

    assert cli.cmd_run() == 0

    ids = [j["id"] for j in sched.jobs]
    assert "pumpfun_callout" in ids, "开关打开却没注册 = 用户一条观点都收不到,且日志无异常"
    job = next(j for j in sched.jobs if j["id"] == "pumpfun_callout")
    assert job["func"] is callout_cls.return_value.run_once, \
        "注册进去的必须是 PumpCalloutWatcher.run_once —— 它是唯一契约上不抛异常的入口"
    assert job["trigger"] == "interval"
    assert job["seconds"] == 90
    assert job["max_instances"] == 1, "两轮叠在一起会重复推送"


def test_关掉观点开关就不注册那个job(monkeypatch):
    """对照组:证明上面那条钉的是开关真的生效,而不是无条件注册。"""
    from src.config import FomoSettings

    s = FomoSettings(fomo_alpha_enabled=False, fomo_pump_enabled=False,
                     fomo_pump_callout_enabled=False)
    sched, _pump, _callout = _stub_pump(monkeypatch, s)

    assert cli.cmd_run() == 0
    assert [j["id"] for j in sched.jobs] == ["fomo_tick"]


def test_两个pump开关互相独立(monkeypatch):
    """
    ⚠️⚠️ 这是"分成两个开关"那条决定的守卫:只想看观点的人不该被逼着
       连逐笔成交一起收,反过来也一样。共用一个开关的实现会让下面两条都红。
    """
    from src.config import FomoSettings

    only_callout = FomoSettings(fomo_alpha_enabled=False, fomo_pump_enabled=False,
                                fomo_pump_callout_enabled=True)
    sched, _p, _c = _stub_pump(monkeypatch, only_callout)
    assert cli.cmd_run() == 0
    assert [j["id"] for j in sched.jobs] == ["fomo_tick", "pumpfun_callout"]

    only_trade = FomoSettings(fomo_alpha_enabled=False, fomo_pump_enabled=True,
                              fomo_pump_callout_enabled=False)
    sched2, _p2, _c2 = _stub_pump(monkeypatch, only_trade)
    assert cli.cmd_run() == 0
    assert [j["id"] for j in sched2.jobs] == ["fomo_tick", "pumpfun"]
