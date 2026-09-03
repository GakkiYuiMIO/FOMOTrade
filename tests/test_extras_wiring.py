"""
🚀 / 🧑‍🤝‍🧑 / 🔗 三行**接进推送主路径**的测试:dexscreener 取社媒、poller 的买入推送、
转入推送(只读缓存)、pump.fun 成交推送。

⚠️ 全部离线(假 client + 真实夹具),一个请求都不打。
⚠️ 断言写死字面量。
"""
# ruff: noqa: N802
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import store
from src.config import get_settings
from src.dexscreener import PoolQuoteLookup, parse_pool_quote, token_socials
from src.poller import Poller
from src.tokeninfo import TokenExtra, TokenExtraLookup
from tests.test_poller import (
    _CA_AI,
    _CA_AI_CHECKSUM,
    _CA_NVDA,
    FakeClient,
    FakeNotifier,
    UserSnapshot,
    _add_ready,
    _FakeDex,
    _rh_swap,
)
from tests.test_tokeninfo import FakeBS, FakeFilter, OpenGate

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path):
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "extras.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _pin_env(monkeypatch):
    monkeypatch.setenv("FOMO_PROXY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ============================================================
# dexscreener:社媒从**同一份已经在取的响应**里来
# ============================================================
_INFO = {"websites": [{"url": "https://cashcat.cc/", "label": "Website"}],
         "socials": [{"url": "https://x.com/cashcat_token", "type": "twitter"},
                     {"url": "https://t.me/cashcat_robinhood", "type": "telegram"}]}


def _pair(base_ca, quote_ca, info=None):
    p = {"chainId": "robinhood",
         "baseToken": {"address": base_ca, "name": "Artificial Inu", "symbol": "AI"},
         "quoteToken": {"address": quote_ca, "name": "NVIDIA • Robinhood Token",
                        "symbol": "NVDA"},
         "liquidity": {"usd": 1_000_000.0}}
    if info is not None:
        p["info"] = info
    return p


def test_社媒来自我方是baseToken的那一侧():
    pq = parse_pool_quote(_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO), "robinhood", _CA_AI)
    assert pq.socials == (("website", "https://cashcat.cc/"),
                          ("twitter", "https://x.com/cashcat_token"),
                          ("telegram", "https://t.me/cashcat_robinhood"))


def test_我方是quoteToken时不取info():
    """
    ⚠️⚠️ `pair.info` 描述的是这条 pair 的**基础币**,而 base/quote 的方向不固定
       (实测 $AI 在 AI/NVDA 里是 base、在 CLANKER/AI 这类池里是 quote)。
       在我方是 quote 的 pair 上照抄 info,印出去的就是**另一个币的官网和推特** ——
       一条读起来完全正常、错得毫无痕迹的假信息。这条钉住"宁可少一行,不许印错"。
    """
    pq = parse_pool_quote(_pair(_CA_NVDA, _CA_AI_CHECKSUM, _INFO), "robinhood", _CA_AI)
    assert pq.socials == ()


def test_没有info时是空元组不是报错():
    pq = parse_pool_quote(_pair(_CA_AI_CHECKSUM, _CA_NVDA), "robinhood", _CA_AI)
    assert pq.socials == ()
    pq = parse_pool_quote(_pair(_CA_AI_CHECKSUM, _CA_NVDA, "不是对象"), "robinhood", _CA_AI)
    assert pq.socials == ()
    pq = parse_pool_quote(_pair(_CA_AI_CHECKSUM, _CA_NVDA, {"websites": "x", "socials": None}),
                          "robinhood", _CA_AI)
    assert pq.socials == ()


def test_真实夹具里的社媒():
    """⚠️ 2026-09-03 真网络录的响应,逐字对上。"""
    payload = json.loads((FIX / "dexscreener_latest_cum.json").read_text(encoding="utf-8"))
    ca = "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"
    pq = parse_pool_quote(payload["pairs"][0], "robinhood", ca)
    assert pq.socials == (
        ("website", "https://app.long.xyz/tokens/0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"),
        ("twitter", "https://x.com/cumonrh"))


def test_社媒条数封顶():
    """⚠️ 上游数组没有上限,不封顶就能让一条推送被它顶爆。"""
    info = {"websites": [{"url": f"https://a{i}.com/"} for i in range(30)],
            "socials": [{"url": f"https://x.com/{i}", "type": "twitter"} for i in range(30)]}
    pq = parse_pool_quote(_pair(_CA_AI_CHECKSUM, _CA_NVDA, info), "robinhood", _CA_AI)
    assert len(pq.socials) == 12


def test_lookup的缓存把社媒一起带着():
    """⚠️ 转入推送那条路径只读这个缓存,社媒必须跟着一起躺在里面。"""
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    lk = PoolQuoteLookup(client=dex)
    lk.lookup("robinhood", [_CA_AI])
    assert lk.cached("robinhood", _CA_AI).socials[0] == ("website", "https://cashcat.cc/")
    assert token_socials({}, _CA_AI) is None


# ============================================================
# poller:买入推送
# ============================================================
def _one_buy(swap=None):
    _add_ready("uA", "alice")
    return FakeClient({"uA": UserSnapshot("uA", swaps=[swap or _rh_swap("a1")],
                                          transfers=[], thesis=[], balances=[])})


def _poller(client, notifier, dex, *, extras_payload=None, bs=None, gate=None):
    p = Poller(client, notifier)
    p._pool_lookup = PoolQuoteLookup(client=dex)
    fc = FakeFilter(extras_payload if extras_payload is not None else [])
    p._token_extras = TokenExtraLookup(filter_client=fc, blockscout=bs or FakeBS(),
                                       gate=gate or OpenGate())
    p._fake_filter = fc
    return p


def _extras_item(addr, launchpad, holders, net=4663):
    return {"holders": holders,
            "token": {"address": addr, "networkId": net, "symbol": "AI",
                      "launchpad": None if launchpad is None else {"launchpadName": launchpad}}}


def test_买入推送带全三行(db):
    """⚠️ 位置也一并钉住:🚀/🧑‍🤝‍🧑 在 🌊 之前,社媒在 🔗 FOMO 之前,CA 独占最后一行。"""
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex,
                extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()

    assert len(tg.sent) == 1
    lines = tg.sent[0].split("\n")
    assert "🚀 发射台 · LONG" in lines
    assert "🧑‍🤝‍🧑 持有人 35,405" in lines
    soc = next(i for i, ln in enumerate(lines) if "网站" in ln)
    assert lines[soc] == ('🔗 <a href="https://cashcat.cc/">网站</a> · '
                          '<a href="https://x.com/cashcat_token">Twitter</a> · '
                          '<a href="https://t.me/cashcat_robinhood">Telegram</a>')
    assert lines.index("🚀 发射台 · LONG") < next(i for i, ln in enumerate(lines)
                                                if ln.startswith("🌊"))
    assert soc < next(i for i, ln in enumerate(lines) if ">FOMO<" in ln)
    assert lines[-1].startswith("<code>")


def test_一个tick只打一次filterTokens(db):
    """
    ⚠️⚠️ 整轮攒成**一个**跨链混批的请求(实测无间隔连打第 10 次就 429、冷却 203 秒)。
       这与底池那边"一条链一个请求"刻意不同 —— 那个端点的限速松得多。
    """
    _add_ready("uB", "bob")
    swaps = [_rh_swap(f"a{i}") for i in range(3)]
    client = FakeClient({"uA": UserSnapshot("uA", swaps=swaps, transfers=[], thesis=[],
                                            balances=[])})
    _add_ready("uA", "alice")
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]] * 4)
    p = _poller(client, tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()
    assert len(p._fake_filter.calls) == 1, p._fake_filter.calls


def test_发射台外呼报错不影响推送(db):
    """⚠️⚠️ **绝不能出现"因为查不到发射台所以整条推送没发出去"**。"""
    class Boom:
        def fetch(self, keys):
            raise RuntimeError("上游炸了")

        def close(self):
            pass

    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = Poller(_one_buy(), tg)
    p._pool_lookup = PoolQuoteLookup(client=dex)
    p._token_extras = TokenExtraLookup(filter_client=Boom(), blockscout=FakeBS(),
                                       gate=OpenGate())
    p.tick()
    assert len(tg.sent) == 1
    assert "🚀" not in tg.sent[0] and "持有人" not in tg.sent[0]
    assert "网站" in tg.sent[0], "社媒来自另一个来源,不该被它带走"


def test_限速闸关着时那两行消失但社媒还在(db):
    """⚠️ 三行各自独立:filterTokens 挨了限速,只掉 🚀 和 🧑‍🤝‍🧑,社媒不受影响。"""
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                gate=OpenGate(allow=False))
    p.tick()
    assert len(tg.sent) == 1
    assert "🚀" not in tg.sent[0] and "持有人" not in tg.sent[0]
    assert "网站" in tg.sent[0]
    assert p._fake_filter.calls == []


def test_社媒拿不到时另外两行照常(db):
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA)]])       # 没有 info
    p = _poller(_one_buy(), tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()
    assert "网站" not in tg.sent[0]
    assert "🚀 发射台 · LONG" in tg.sent[0]
    assert "🧑‍🤝‍🧑 持有人 35,405" in tg.sent[0]


# ⚠️ 这个币有官网(`app.long.xyz` —— 域名里明晃晃写着 LONG),但上游**没给** launchpadName。
_INFO_LONG = {"websites": [{"url": "https://app.long.xyz/tokens/0xabc", "label": "Website"}],
              "socials": [{"url": "https://x.com/cumonrh", "type": "twitter"}]}


def test_有官网也绝不用域名反推发射台(db):
    """
    ⚠️⚠️ 这条钉住调研里**已经被证伪**的那条路:用社媒/官网的域名去猜发射台。
       CASHCAT 的官网是 `cashcat.cc`、$AI 的是 `artificialinu.com` —— 都是项目自己的站,
       跟发射台无关。这里给的是最诱人的那种样本:官网域名里就写着 `long`,
       而上游明确**没有** launchpadName。猜一个 = 印一句假事实,比少一行糟得多。
    """
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO_LONG)]])
    p = _poller(_one_buy(), tg, dex,
                extras_payload=[_extras_item(_CA_AI, None, 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()
    assert len(tg.sent) == 1
    assert "🚀" not in tg.sent[0] and "发射台" not in tg.sent[0], tg.sent[0]
    # 对照:另外两行照常(三行各自独立)
    assert "🧑‍🤝‍🧑 持有人 35,405" in tg.sent[0]
    assert ">网站</a>" in tg.sent[0]


def test_持有人为0的哨兵一路到不了推送(db):
    """⚠️⚠️ 端到端地钉住坑 1:上游给 0,推送里不该出现「持有人」这三个字。"""
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 0)],
                bs=FakeBS())
    p.tick()
    assert "持有人" not in tg.sent[0]
    assert "🚀 发射台 · LONG" in tg.sent[0]


def test_robinhood的持有人取自Blockscout而不是FOMO(db):
    """⚠️ 两个源给不同的数;退回用 FOMO 的那一刻这条当场红。"""
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 2186)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "36"}}))
    p.tick()
    assert "🧑‍🤝‍🧑 持有人 36" in tg.sent[0], "用了 FOMO 那个过期的数"


# ============================================================
# 转入推送:只读缓存,一个请求都不发
# ============================================================
def test_转入推送这条路径一个请求都不发(db):
    """
    ⚠️⚠️ 与既有的币名口径一致:/tin 与转入聚合走 _name_extras(cached_only=True),
       **一个请求都不许发**。缓存里有就带上三行,没有就没有。
    """
    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex, extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))

    # 缓存全空:什么都没有,也一个请求都不发
    assert p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True) == {}
    assert p._fake_filter.calls == [] and dex.calls == []

    p.tick()                       # 买入推送把两份缓存都填上了
    n_fc, n_dex = len(p._fake_filter.calls), len(dex.calls)

    got = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True)
    assert got["launchpad"] == "LONG"
    assert got["token_holders"] == 35405
    assert got["token_socials"] == (("website", "https://cashcat.cc/"),
                                    ("twitter", "https://x.com/cashcat_token"),
                                    ("telegram", "https://t.me/cashcat_robinhood"))
    assert len(p._fake_filter.calls) == n_fc and len(dex.calls) == n_dex, "只读缓存却发了请求"

    # 缓存里没有的币:三行都没有,同样不发请求
    other = "0x1111111111111111111111111111111111111111"
    assert p._name_extras({}, "robinhood", other, "X", None, cached_only=True) == {}
    assert len(p._fake_filter.calls) == n_fc and len(dex.calls) == n_dex


def test_转入路径的pons不为分版本外呼(db):
    """⚠️ 分不出版本就退回 "Pons" —— 这条路径连 Blockscout 也不许碰。"""
    tg = FakeNotifier()
    dex = _FakeDex([])
    p = _poller(_one_buy(), tg, dex, extras_payload=[])
    p._token_extras._glossary_put("launchpad", f"robinhood:{_CA_AI}", "pons", "fomo", None)
    got = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True)
    assert got == {"launchpad": "Pons"}
    assert p._token_extras._bs.calls == []


# ============================================================
# pump.fun 成交推送
# ============================================================
def test_pump成交推送也接了这三行(monkeypatch):
    """⚠️ 只验"接线对不对":_name_extras 是 pump 那条路径上唯一的汇合点。"""
    from src.pumpfun import PumpWatcher

    w = PumpWatcher.__new__(PumpWatcher)
    w._names = type("N", (), {"token_zh": lambda *a, **k: None,
                              "stock_info": lambda *a, **k: None})()
    got = w._name_extras("AI", None, "Artificial Inu",
                         (("website", "https://cashcat.cc/"),),
                         TokenExtra(launchpad="LONG", holders=35405))
    assert got["token_socials"] == (("website", "https://cashcat.cc/"),)
    assert got["launchpad"] == "LONG"
    assert got["token_holders"] == 35405


def test_pump的三行各自独立():
    from src.pumpfun import PumpWatcher

    w = PumpWatcher.__new__(PumpWatcher)
    w._names = type("N", (), {"token_zh": lambda *a, **k: None,
                              "stock_info": lambda *a, **k: None})()
    assert w._name_extras("AI", None, None, None, None) == {}
    only_lp = w._name_extras("AI", None, None, None, TokenExtra(launchpad="LONG", holders=None))
    assert only_lp == {"launchpad": "LONG"}
    only_ho = w._name_extras("AI", None, None, None, TokenExtra(launchpad=None, holders=7))
    assert only_ho == {"token_holders": 7}


# ============================================================
# ⚠️⚠️ poller 侧的「三行各自独立」(H3)
# ============================================================
def test_币名那一段炸了发射台和持有人两行仍在(db, monkeypatch):
    """
    ⚠️⚠️ poller._name_extras 里「币名/股票」与「发射台/持有人」是**两段独立的 try**,
       源码注释写着"合在一起的话上面任何一步抛异常都会把这两行一起带走" ——
       但在这条用例之前**没有任何测试钉住它**:把两段 try 合成一段跑全量,一条都不红。
       (formatter 那一侧的独立性是有覆盖的,缺的是 poller 这一侧。)
    ⚠️ 让第一段的第一步(dexscreener.token_name)真的抛异常:
       · 币名 / 中文名 / 社媒 三样都没了(它们同属第一段)
       · 🚀 与 🧑‍🤝‍🧑 两行**必须还在**
       · 整条推送**必须照样发出去**
       两段合并 → 这条当场红。
    """
    def _boom(quotes, addr):
        raise RuntimeError("币名那一段炸了")

    monkeypatch.setattr("src.poller.token_name", _boom)

    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex,
                extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()

    assert len(tg.sent) == 1, "第一段炸了把整条推送带走了"
    msg = tg.sent[0]
    assert "🚀 发射台 · LONG" in msg, msg
    assert "🧑‍🤝‍🧑 持有人 35,405" in msg, msg
    # 对照:第一段那几样确实没了(证明异常真的抛在了第一段里,不是这条用例空转)
    # ⚠️ 🌊 那行的「NVIDIA」是**进 try 之前**就落好的回退值,与第一段无关,不在此列。
    assert "网站" not in msg, msg
    assert "「" not in msg.split("\n")[0], msg      # 标题尾巴的英文全名没了
    assert "📝" not in msg, msg                     # 中文名那一行没了


# ============================================================
# ⚠️⚠️ 转入推送为什么几乎永远没有 🧑‍🤝‍🧑 那一行(H6)
# ============================================================
def test_转入推送带发射台但过了内存TTL就没有持有人(db, monkeypatch):
    """
    ⚠️⚠️ 这条把 H6 那个**如实登记的取舍**钉住,免得下一个人以为是 bug 顺手改掉:
       · 发射台走 name_glossary,成功是**永久**缓存 ⇒ 转入推送**有** 🚀;
       · 持有人只有 90 秒的**进程内存**缓存,而转入走 cached()(只读、不发请求)
         ⇒ 过了 90 秒就**没有** 🧑‍🤝‍🧑,而且不会为它发任何请求。
    ⚠️ 结论写在 README「已知取舍」里。给 holders 也落 SQLite 的方案被否掉了:
       短 TTL 照样命中不了,长 TTL 等于在一条刚发生的转账旁边印几小时前的持有人数。
    """
    import time as _time

    tg = FakeNotifier()
    dex = _FakeDex([[_pair(_CA_AI_CHECKSUM, _CA_NVDA, _INFO)]])
    p = _poller(_one_buy(), tg, dex,
                extras_payload=[_extras_item(_CA_AI, "LONG", 35405)],
                bs=FakeBS(holders={_CA_AI: {"holders_count": "35405"}}))
    p.tick()                                   # 买入推送把两份缓存都填上
    n_fc = len(p._fake_filter.calls)
    n_bs = len(p._token_extras._bs.calls)

    # 90 秒之内:两行都在(内存缓存还没过期)
    fresh = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True)
    assert fresh["launchpad"] == "LONG"
    assert fresh["token_holders"] == 35405

    # 往后拨 91 秒:发射台还在(永久落库),持有人没了(内存 TTL 90 秒)
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 91.0)
    stale = p._name_extras({}, "robinhood", _CA_AI, "AI", None, cached_only=True)
    assert stale["launchpad"] == "LONG", "发射台是永久缓存,不该跟着掉"
    assert "token_holders" not in stale, "持有人只有 90 秒内存缓存,过期就该整行消失"
    assert len(p._fake_filter.calls) == n_fc, "只读缓存路径却发了 filterTokens"
    assert len(p._token_extras._bs.calls) == n_bs, "只读缓存路径却打了 Blockscout"
