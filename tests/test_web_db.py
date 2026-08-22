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
    monkeypatch.setattr(webdb, "DB_PATH", tmp_path / "t.db")
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
    monkeypatch.setattr(webdb, "DB_PATH", tmp_path / "nope.db")
    with pytest.raises(webdb.WebDbError, match="先跑"):
        with webdb.readonly_conn():
            pass
