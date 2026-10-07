"""
--login 只收**没过期**的令牌(src/auth.py interactive_login / _login_token_fresh)。

2026-10-07 实测的事故:登录浏览器用的是常驻配置目录,localStorage 里留着上一次登录的令牌。
页面一打开就读得到它,旧写法当场收下、关窗、存盘 —— 存进去的 access 两个半小时前就过期了,
配套的 refresh token 也早被换掉,--run 一续期就 401、轮询停摆。

⚠️ 全部离线:浏览器、取令牌、存盘都换成假的,绝不碰 data/fomo_session.json。
"""
# ruff: noqa: N802
from __future__ import annotations

import base64
import json
import time

import pytest

from src import auth


def _jwt(exp) -> str:
    """造一个三段式令牌;exp=None 时 payload 里没有 exp(= 解不出过期时间)"""
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    body = {"sid": "s" * 40}
    if exp is not None:
        body["exp"] = exp
    return f"{b64({'alg': 'ES256', 'typ': 'JWT'})}.{b64(body)}.{'g' * 43}"


class _Page:
    def goto(self, *a, **k):
        return None


class _Ctx:
    def __init__(self):
        self.pages = [_Page()]

    def add_init_script(self, s):
        return None

    def new_page(self):
        return _Page()


class _PW:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def login(monkeypatch):
    """跑一次 interactive_login:tokens 是每一轮读到的 access(读完就一直是最后一个)。返回 (结果, 存盘记录, 日志)"""
    import playwright.sync_api as psa
    from loguru import logger

    def _run(tokens, timeout=0.3):
        monkeypatch.setattr(psa, "sync_playwright", lambda: _PW())
        monkeypatch.setattr(auth, "_open_login_context", lambda p, s, cdp: (_Ctx(), lambda: None))
        monkeypatch.setattr(auth, "_LOGIN_POLL_SEC", 0.001)
        seq = list(tokens)

        def extract(ctx):
            a = seq.pop(0) if len(seq) > 1 else seq[0]
            return a, "refresh-" + a[-6:], "localStorage"
        monkeypatch.setattr(auth, "_extract_tokens", extract)
        saved: list = []
        monkeypatch.setattr(auth, "save_session",
                            lambda a, r, source: saved.append((a, r, source)))
        logs: list[str] = []
        hid = logger.add(lambda m: logs.append(m.record["message"]), level="INFO")
        try:
            ok = auth.interactive_login(timeout_sec=timeout)
        finally:
            logger.remove(hid)
        return ok, saved, logs
    return _run


class Test登录流程:
    def test_先读到旧令牌_等到新令牌才存(self, login):
        now = time.time()
        stale, fresh = _jwt(now - 9000), _jwt(now + 3600)
        ok, saved, logs = login([stale, stale, fresh])
        assert ok is True
        assert [s[0] for s in saved] == [fresh], "存进去的必须是新令牌"
        assert any("旧登录态" in m for m in logs), logs

    def test_一直只有旧令牌_超时不存盘(self, login):
        ok, saved, _ = login([_jwt(time.time() - 9000)], timeout=0.15)
        assert ok is False
        assert saved == [], "绝不能把过期令牌存进会话文件"

    def test_同一份旧令牌只提示一次(self, login):
        now = time.time()
        stale = _jwt(now - 9000)
        _, _, logs = login([stale] * 20 + [_jwt(now + 3600)])
        assert sum("旧登录态" in m for m in logs) == 1, logs

    def test_新令牌一来就收_不多等(self, login):
        fresh = _jwt(time.time() + 3600)
        ok, saved, logs = login([fresh])
        assert ok is True and saved[0][0] == fresh
        assert not any("旧登录态" in m for m in logs)


class Test新旧判定:
    @pytest.mark.parametrize(("remaining", "want"), [
        (3600, True), (301, True), (300, False), (299, False), (0, False), (-9000, False),
    ])
    def test_至少还剩5分钟才收(self, remaining, want):
        """与续期余量同一个数:剩得更少的,--run 一启动就要拿(多半也放旧了的)refresh token 去续期"""
        now = 1_800_000_000.0
        assert auth._login_token_fresh(_jwt(now + remaining), now=now) is want

    def test_解不出过期时间的照收(self):
        """判不了就不拦 —— 拦了反而让一个格式稍有不同的新令牌永远登录不上"""
        assert auth._login_token_fresh(_jwt(None), now=1_800_000_000.0) is True
