"""
⚠️ 这个文件目前只钉一件事:main() 里 --web 必须跳过 store.init_db()。

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
