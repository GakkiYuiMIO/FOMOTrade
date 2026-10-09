"""
Privy 续期与「自定义域名 + HttpOnly Cookie」模式(src/auth.py)。

事故(2026-10-08 ~ 10-09):FOMO 给 Privy 开了 custom_api_url = https://privy.fomo.family。
从那以后续期响应正文与 localStorage 里的 refresh_token 都只剩字面量 "deprecated",真令牌在
`.privy.fomo.family` 的 HttpOnly Cookie `privy-refresh-token` 里。旧写法把 "deprecated" 当真令牌存了,
一小时后续期 401、轮询停摆;重新 --login 也一样。

⚠️ 全部离线:续期请求整个换成假的(auth._post_session),会话文件写在临时目录,绝不碰 data/。
⚠️ 断言写死字面量(URL、Cookie 名、占位符)。
"""
# ruff: noqa: N802
from __future__ import annotations

import base64
import json
import time

import pytest

from src import auth
from src.auth import AuthError, RetryableAuthError, TokenProvider, _SessionResp


def _jwt(exp) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'ES256'})}.{b64({'exp': exp, 'sid': 's' * 40})}.{'g' * 43}"


OLD = _jwt(time.time() - 60)          # 已过期 → 一取就要续期
NEW = _jwt(time.time() + 3600)


@pytest.fixture
def sess(tmp_path):
    return tmp_path / "session.json"


@pytest.fixture
def post(monkeypatch):
    """把续期请求换成假的:返回值由 post.reply 决定,调用记录在 post.calls"""
    class P:
        calls: list = []
        reply = None
        exc = None

        def __call__(self, url, *, headers, body, cookies, proxy):
            self.calls.append({"url": url, "headers": headers, "body": body, "cookies": cookies})
            if self.exc is not None:
                raise self.exc
            return self.reply
    p = P()
    p.calls = []
    monkeypatch.setattr(auth, "_post_session", p)
    return p


def _ok(body: dict, cookies=()) -> _SessionResp:
    return _SessionResp(status_code=200, text=json.dumps(body), cookies=tuple(cookies))


def _file(sess) -> dict:
    return json.loads(sess.read_text(encoding="utf-8"))


# ============================================================
# 占位符绝不落盘、读进来也当没有
# ============================================================
class Test占位符:
    def test_存盘时占位符变成null(self, sess):
        auth.save_session(NEW, "deprecated", source="t", path=sess)
        assert _file(sess)["refresh_token"] is None

    @pytest.mark.parametrize("v", ["deprecated", "DEPRECATED", " deprecated ", "", "null", "undefined"])
    def test_各种空值与占位符都不算令牌(self, v):
        assert auth._real_refresh(v) is None

    def test_真令牌原样保留(self):
        assert auth._real_refresh("abc_DEF-123") == "abc_DEF-123"

    def test_会话里只剩占位符时_直接要求重新登录_不去续期(self, sess, post):
        """⚠️ 旧写法拿 "deprecated" 去续期、401 连刷十几次才停 —— 现在一次请求都不发"""
        sess.write_text(json.dumps({"access_token": OLD, "refresh_token": "deprecated",
                                    "expires_at": time.time() - 60}), encoding="utf-8")
        with pytest.raises(AuthError, match="--login"):
            TokenProvider(sess).get_access_token()
        assert post.calls == []


# ============================================================
# Cookie 模式
# ============================================================
class TestCookie模式:
    def _seed(self, sess, api_base="https://privy.fomo.family"):
        auth.save_session(OLD, "REAL1", source="t", path=sess, refresh_via="cookie", api_base=api_base)

    def test_存盘写明模式与认证地址(self, sess):
        self._seed(sess)
        d = _file(sess)
        assert d["refresh_via"] == "cookie" and d["api_base"] == "https://privy.fomo.family"

    def test_请求照SDK原样发_地址_正文占位符_Cookie带真令牌_带上旧access(self, sess, post):
        self._seed(sess)
        post.reply = _ok({"token": NEW, "refresh_token": "deprecated"},
                         [("privy-refresh-token", "REAL2", ".privy.fomo.family")])
        TokenProvider(sess).get_access_token()
        c = post.calls[0]
        assert c["url"] == "https://privy.fomo.family/api/v1/sessions"
        assert c["body"] == {"refresh_token": "deprecated"}
        assert c["cookies"] == {"privy-refresh-token": "REAL1"}
        assert c["headers"]["Authorization"] == f"Bearer {OLD}"

    def test_新续期令牌从SetCookie拿回来_存盘(self, sess, post):
        self._seed(sess)
        post.reply = _ok({"token": NEW, "refresh_token": "deprecated"},
                         [("privy-refresh-token", "REAL2", ".privy.fomo.family")])
        assert TokenProvider(sess).get_access_token() == NEW
        d = _file(sess)
        assert d["access_token"] == NEW and d["refresh_token"] == "REAL2"
        assert d["refresh_via"] == "cookie" and d["api_base"] == "https://privy.fomo.family"

    def test_会话里没记认证地址时用兜底地址(self, sess, post):
        self._seed(sess, api_base=None)
        post.reply = _ok({"token": NEW}, [("privy-refresh-token", "REAL2", ".privy.fomo.family")])
        TokenProvider(sess).get_access_token()
        assert post.calls[0]["url"] == "https://privy.fomo.family/api/v1/sessions"

    def test_连续两次续期_第二次用的是第一次拿回来的令牌(self, sess, post):
        self._seed(sess)
        post.reply = _ok({"token": _jwt(time.time() + 10)},
                         [("privy-refresh-token", "REAL2", ".privy.fomo.family")])
        tp = TokenProvider(sess)
        tp.get_access_token()               # 拿到的新 access 只剩 10 秒 → 下一次还要续期
        post.reply = _ok({"token": NEW}, [("privy-refresh-token", "REAL3", ".privy.fomo.family")])
        tp.get_access_token()
        assert post.calls[1]["cookies"] == {"privy-refresh-token": "REAL2"}
        assert _file(sess)["refresh_token"] == "REAL3"


# ============================================================
# 老模式与过渡
# ============================================================
class Test老模式:
    def test_老会话照旧发到auth_privy_io_真令牌放正文(self, sess, post):
        auth.save_session(OLD, "BODY1", source="t", path=sess)
        post.reply = _ok({"token": NEW, "refresh_token": "BODY2"})
        TokenProvider(sess).get_access_token()
        c = post.calls[0]
        assert c["url"] == "https://auth.privy.io/api/v1/sessions"
        assert c["body"] == {"refresh_token": "BODY1"} and c["cookies"] is None
        d = _file(sess)
        assert d["refresh_token"] == "BODY2" and "refresh_via" not in d

    def test_过渡_老模式发出去_正文是占位符_SetCookie给真令牌_切到Cookie模式(self, sess, post):
        """⚠️⚠️ 2026-10-08 18:58 那一次正是这样 —— 旧写法把正文里的 "deprecated" 存了"""
        auth.save_session(OLD, "BODY1", source="t", path=sess)
        post.reply = _ok({"token": NEW, "refresh_token": "deprecated"},
                         [("privy-refresh-token", "REAL2", ".privy.fomo.family")])
        TokenProvider(sess).get_access_token()
        d = _file(sess)
        assert d["refresh_token"] == "REAL2"
        assert d["refresh_via"] == "cookie" and d["api_base"] == "https://privy.fomo.family"

    def test_过渡时Cookie没写域名_认证地址跟着发请求的那个域名(self, sess, post):
        auth.save_session(OLD, "BODY1", source="t", path=sess)
        post.reply = _ok({"token": NEW, "refresh_token": "deprecated"},
                         [("privy-refresh-token", "REAL2", "")])
        TokenProvider(sess).get_access_token()
        assert _file(sess)["api_base"] == "https://auth.privy.io"

    def test_两处都没有新令牌_沿用旧的_绝不存占位符(self, sess, post):
        auth.save_session(OLD, "BODY1", source="t", path=sess)
        post.reply = _ok({"token": NEW, "refresh_token": "deprecated"})
        TokenProvider(sess).get_access_token()
        assert _file(sess)["refresh_token"] == "BODY1"


# ============================================================
# 失败
# ============================================================
class Test失败:
    def test_4xx是登录态失效_会话文件不动(self, sess, post):
        auth.save_session(OLD, "REAL1", source="t", path=sess, refresh_via="cookie",
                          api_base="https://privy.fomo.family")
        before = sess.read_text(encoding="utf-8")
        post.reply = _SessionResp(401, '{"error":"Invalid auth token"}')
        with pytest.raises(AuthError):
            TokenProvider(sess).get_access_token()
        assert sess.read_text(encoding="utf-8") == before

    def test_5xx可重试(self, sess, post):
        auth.save_session(OLD, "REAL1", source="t", path=sess)
        post.reply = _SessionResp(503, "")
        with pytest.raises(RetryableAuthError):
            TokenProvider(sess).get_access_token()

    def test_网络层异常可重试_不当成登出(self, sess, post):
        auth.save_session(OLD, "REAL1", source="t", path=sess)
        post.exc = OSError("代理断了")
        with pytest.raises(RetryableAuthError):
            TokenProvider(sess).get_access_token()


# ============================================================
# 登录时取令牌(_extract_tokens)
# ============================================================
class _Page:
    def __init__(self, ls):
        self._ls = ls

    def is_closed(self):
        return False

    def evaluate(self, js):
        return self._ls


class _Ctx:
    def __init__(self, ls, cookies):
        self.pages = [_Page(ls)]
        self._ck = cookies

    def cookies(self):
        if isinstance(self._ck, Exception):
            raise self._ck
        return self._ck


class Test登录取令牌:
    def test_localStorage是占位符_去Cookie里取真令牌_认证地址跟着Cookie的域名(self):
        ls = {"privy:token": json.dumps(NEW), "privy:refresh_token": json.dumps("deprecated")}
        ck = [{"name": "privy-token", "value": "x", "domain": ".fomo.family"},
              {"name": "privy-refresh-token", "value": "REALC", "domain": ".privy.fomo.family"}]
        access, refresh, source, api_base = auth._extract_tokens(_Ctx(ls, ck))
        assert access == NEW and refresh == "REALC"
        assert api_base == "https://privy.fomo.family"
        assert "cookie" in source

    def test_认证地址真的是从Cookie域名推出来的_不是兜底值(self):
        """⚠️ 上一条的期望值恰好等于兜底地址 —— 换个域名才分得出「推出来的」和「写死的」"""
        ls = {"privy:token": json.dumps(NEW), "privy:refresh_token": json.dumps("deprecated")}
        ck = [{"name": "privy-refresh-token", "value": "REALC", "domain": ".privy.example.org"}]
        assert auth._extract_tokens(_Ctx(ls, ck))[3] == "https://privy.example.org"

    def test_localStorage里是真令牌_老模式_不去碰Cookie(self):
        ls = {"privy:token": json.dumps(NEW), "privy:refresh_token": json.dumps("BODY1")}
        access, refresh, _, api_base = auth._extract_tokens(_Ctx(ls, RuntimeError("不该读 Cookie")))
        assert refresh == "BODY1" and api_base is None

    def test_占位符且没有Cookie_续期令牌就是没有(self):
        ls = {"privy:token": json.dumps(NEW), "privy:refresh_token": json.dumps("deprecated")}
        _, refresh, _, api_base = auth._extract_tokens(_Ctx(ls, []))
        assert refresh is None and api_base is None

    def test_Cookie里也是占位符_同样不算(self):
        ls = {"privy:token": json.dumps(NEW)}
        ck = [{"name": "privy-refresh-token", "value": "deprecated", "domain": ".privy.fomo.family"}]
        _, refresh, _, api_base = auth._extract_tokens(_Ctx(ls, ck))
        assert refresh is None and api_base is None


def test_登录存盘时带上Cookie模式与认证地址(monkeypatch):
    import playwright.sync_api as psa

    class _PW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _C:
        pages = [type("P", (), {"goto": lambda *a, **k: None})()]

        def add_init_script(self, s):
            return None

    monkeypatch.setattr(psa, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(auth, "_open_login_context", lambda p, s, cdp: (_C(), lambda: None))
    monkeypatch.setattr(auth, "_extract_tokens",
                        lambda ctx: (NEW, "REALC", "localStorage+cookie", "https://privy.fomo.family"))
    got = {}
    monkeypatch.setattr(auth, "save_session", lambda a, r, **kw: got.update(kw, a=a, r=r))
    assert auth.interactive_login(timeout_sec=1) is True
    assert got["r"] == "REALC"
    assert got["refresh_via"] == "cookie" and got["api_base"] == "https://privy.fomo.family"
