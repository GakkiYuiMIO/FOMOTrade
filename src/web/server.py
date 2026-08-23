"""
网页版 HTTP 服务。

⚠️ 只绑 127.0.0.1 —— 这台机器上有明文登录令牌和完整登录态浏览器 profile,
   多开一个对外端口意味着那些东西背后只隔着一层现写的代码。
⚠️ **只读、无任何写操作**。尤其不做买入按钮:真实下单唯一入口是 TG 确认按钮,
   网页上多一个按钮就绕开了那道人工确认。
"""
from __future__ import annotations

import traceback
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger

from src.web import pages
from src.web.db import WebDbError, readonly_conn

STATIC_DIR = Path(__file__).parent / "static"

# 路由表:路径 → 渲染函数(conn, query) -> str。
# ⚠️ 信号卡片流是新首页,旧看板挪到 /board —— 四个老页面都还在,只是首页换了人。
ROUTES = {
    "/": pages.feed,
    "/board": pages.dashboard,
    "/people": pages.people,
    "/hot": pages.hot,
    "/copy": pages.copy_ledger,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "fomo-web"

    def log_message(self, fmt, *args):        # noqa: A003
        logger.debug("web {} {}", self.address_string(), fmt % args)

    def do_GET(self):                          # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith("/static/"):
                return self._static(path)
            fn = ROUTES.get(path)
            if fn is None:
                return self._send(404, "text/html; charset=utf-8",
                                  b"<h1>404</h1>")
            # ⚠️ 查询串是唯一的不可信输入面(?min_buyers=/?hours=)。这里只解析、
            #    不校验 —— 校验+夹值的责任在 pages.feed(),别处不重复这道逻辑。
            query = parse_qs(parsed.query)
            # ⚠️ 每个请求开一次连接、用完立刻关。不做连接池 ——
            #    泄漏的读事务会把 WAL 钉住、无上限增长、全程静默无报错。
            with readonly_conn() as conn:
                body = fn(conn, query)
            return self._send(200, "text/html; charset=utf-8", body.encode())
        except WebDbError as e:
            return self._send(503, "text/plain; charset=utf-8", str(e).encode())
        except Exception:                      # noqa: BLE001
            # 本机自用,把 traceback 直接给出来 —— 藏起来只会让排查变难
            logger.exception("页面渲染失败 | {}", path)
            return self._send(500, "text/plain; charset=utf-8",
                              traceback.format_exc().encode())

    def _static(self, path: str) -> None:
        name = path.removeprefix("/static/")
        f = (STATIC_DIR / name).resolve()
        # ⚠️ 防路径穿越:必须确认解析后仍在 static 目录内
        if not f.is_file() or STATIC_DIR.resolve() not in f.parents:
            return self._send(404, "text/plain", b"not found")
        ctype = "text/css; charset=utf-8" if f.suffix == ".css" else "application/octet-stream"
        self._send(200, ctype, f.read_bytes())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8420) -> None:
    httpd = ThreadingHTTPServer((host, port), partial(Handler))
    httpd.daemon_threads = True
    logger.info("网页版已启动: http://{}:{}  (只读,Ctrl+C 退出)", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C,关闭网页服务")
    finally:
        httpd.shutdown()
        httpd.server_close()
