"""
⚠️ 这个文件只钉一件事:web 进程**物理上写不进数据库**。
   这是整个网页版最重要的一条保证 —— 游标/徽章/cs_* 都是「写了就不能改」的冻结列,
   一次误写就是永久污染,而且没有任何告警。
"""
# ruff: noqa: N802
from __future__ import annotations

import sqlite3

import pytest

from src import store
from src.web import db as webdb


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """在临时文件上建一个真实的库(内存库没有文件路径,只读连接测不了)"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    with store.get_conn() as c:
        store.add_watch_user(c, "u1", "alice", "Alice")
    return tmp_path / "t.db"


def test_能读到数据(real_db):
    with webdb.readonly_conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM watch_users").fetchone()["n"] == 1


def test_写入必须被拒绝(real_db):
    """⚠️ 不是靠自觉,是 SQLite 在驱动层拒绝"""
    with webdb.readonly_conn() as c, pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO watch_users(user_id, handle, added_at) VALUES ('x','x','x')")


def test_删表也要被拒绝(real_db):
    with webdb.readonly_conn() as c, pytest.raises(sqlite3.OperationalError):
        c.execute("DROP TABLE watch_users")


def test_库不存在时给出能照做的报错(tmp_path, monkeypatch):
    """⚠️ 默认报错是 'unable to open database file',用户看不出该干什么"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "nope.db")
    with pytest.raises(webdb.WebDbError, match="先跑"):
        with webdb.readonly_conn():
            pass


def test_只读打不开时退回query_only仍然写不进去(real_db, monkeypatch):
    """
    ⚠️ 极端情况(比如 poller 崩溃留下未 checkpoint 的 WAL)下 mode=ro 打不开库,
       会退回普通连接 + PRAGMA query_only —— 这条兜底路径必须单独验证:
       读要能读,写依然要被拒绝,不能因为退回了普通连接就悄悄放开写权限。
    """
    real_connect = sqlite3.connect
    calls = {"n": 0}

    def fake_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 只让"第一次"(mode=ro 那次)失败,后续退回的普通连接必须走真实实现,
            # 否则会递归回到这个 fake 上
            raise sqlite3.OperationalError("模拟 mode=ro 打不开")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)

    with webdb.readonly_conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM watch_users").fetchone()["n"] == 1
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO watch_users(user_id, handle, added_at) VALUES ('x','x','x')")

    assert calls["n"] == 2  # 确认真的走了"先失败、再退回"这条路,不是巧合通过
