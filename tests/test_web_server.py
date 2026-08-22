"""
src/web/server.py 的冒烟测试 —— 这是六个新 src/web/ 模块里唯一没测过的一个。

⚠️ 本文件只钉两件事:
   1. HTTP 层没崩:四个页面路由 + /static 能跑通,未知路径 404。
   2. path traversal 防护线(server.Handler._static)不能被悄悄削弱 ——
      这台机器的 data/ 目录下是明文登录令牌(data/fomo_session.json)和完整
      登录态浏览器 profile,这道防线是"本机看板"和"文件泄露端点"之间唯一的墙。
      一个 reviewer 已经手工确认过它当前是好的,但没有测试的话,以后任何一次
      重构都可能在没人注意的情况下把它改坏。
"""
# ruff: noqa: N802
from __future__ import annotations

import socket
import threading
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from src import store
from src.web import server


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """
    起一个真实的 HTTP 服务,绑在**临时数据库**上,端口号让操作系统自己挑
    (bind 到 0,再读回真正分配到的端口)—— 这台机器上 10000 以下的端口是保留的,
    硬编码任何固定端口都可能撞车或直接起不来。

    ⚠️ 用 store.DB_PATH 指向 tmp_path,绝不碰 data/fomo.db ——
       那边有一个真实监控进程正在写,单测污染了它就再也说不清"共识数为什么是这个值"。
    """
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name="test-web-server", daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "服务线程没退出,可能留下监听端口"


def _get(port: int, path: str) -> tuple[int, dict, bytes]:
    """发一个原始 GET —— 用 http.client 而不是 requests/urllib,
    这样 path 里的 '..' / '%2f' 会原样发到服务端,不会被客户端先一步"帮忙"规范化掉。
    """
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["/", "/people", "/hot", "/copy"])
def test_四个页面路由返回200(running_server, path):
    status, _, body = _get(running_server, path)
    assert status == 200
    assert body.startswith(b"<!doctype html>")


def test_静态文件返回200且是css类型(running_server):
    status, headers, body = _get(running_server, "/static/app.css")
    assert status == 200
    assert "css" in headers.get("Content-Type", "").lower()
    assert body  # 有实际内容,不是空响应


def test_未知路径返回404(running_server):
    status, _, _ = _get(running_server, "/does-not-exist")
    assert status == 404


# ⚠️ data/fomo_session.json 是这台机器上真实存在的明文登录令牌文件,
#    三条 payload 分别对应:裸 '..'、URL 编码后的 '..'(server 会 unquote 再路由)、
#    以及绝对路径注入(pathlib 在 windows 上遇到带盘符的绝对路径会整体替换掉左操作数,
#    必须靠 STATIC_DIR.resolve() not in f.parents 这道containment 检查挡住)。
_TRAVERSAL_PAYLOADS = [
    "/static/../../../data/fomo_session.json",
    "/static/..%2f..%2f..%2fdata%2ffomo_session.json",
    "/static/E:/cryptoCode/FOMO/data/fomo_session.json",
]


@pytest.mark.parametrize("path", _TRAVERSAL_PAYLOADS)
def test_路径穿越必须被拒绝(running_server, path):
    status, _, body = _get(running_server, path)
    assert status == 404, f"{path} 没有被拒绝,path traversal 防护失效"
    assert b"fomo_session" not in body
    assert b"privy" not in body.lower()  # 会话文件里的字段名,防止内容被原样吐出来


def test_关闭后端口真的释放不留监听():
    """
    teardown 逻辑本身要能把 socket 真正放掉,不能留下一个假死的监听端口 ——
    这条不依赖 running_server fixture,自己起、自己关、自己验证,
    确保断言的是"关闭动作生效"而不是"fixture 恰好没抛异常"。
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name="test-web-server-2", daemon=True)
    thread.start()

    # 先确认它真的在监听
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2)
    probe.connect(("127.0.0.1", port))
    probe.close()

    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()

    with pytest.raises(OSError):
        probe2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe2.settimeout(2)
        try:
            probe2.connect(("127.0.0.1", port))
        finally:
            probe2.close()
