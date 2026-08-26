"""
币安 Alpha 新上架监控的测试。

⚠️ 全部走**离线夹具**(tests/fixtures/binance_*.json,是两次真实响应存下来的),
   一条用例都不打网络 —— 币安风控严,每跑一次测试就打一次接口是在拿用户 IP 冒险。
⚠️ 断言里的门槛数字(500 / 10 / 水位线的键名)一律**写死字面量**,绝不从被测模块
   import 过来 —— 那种断言等价于 `x == x`,把常量改坏了它照样绿。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。ruff 的 N802 只认 ASCII 小写。
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import binance_alpha as ba
from src import store

FIXTURES = Path(__file__).parent / "fixtures"

# 水位线在 runtime_state 里的键。⚠️ 刻意写死:从 ba.STATE_KEY 取的话,
# 谁把键名改了(= 线上所有实例退回冷启动、静默吃掉一批上新)测试也不会红。
WATERMARK_KEY = "binance_alpha_watermark_ms"

# 夹具里最新的三个币(按 listingTime 倒序),写死用来当断言基准
DEBIT_MS = 1787738400000          # DEBIT  2026-08-26 10:00 UTC
TMX_MS = 1787652000000            # TMX    2026-08-25 10:00 UTC
NIULAI_MS = 1787041234999         # 牛来   2026-08-18 08:20 UTC
NIULAI_CA = "0xbeea1d618e533a387d941f58a7d4c9b7bd377777"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _alpha_payload():
    return _load("binance_alpha_token_list.json")


def _sector_payload():
    return _load("binance_sector_rank.json")


def _tok(**kw) -> ba.AlphaToken:
    """造一个 AlphaToken。默认是一个数据齐全的 BSC 代币。"""
    base = dict(
        listing_time_ms=1787738400000, symbol="DEBIT", name="Teller",
        chain_id="56", chain_name="BSC",
        contract_address="0x66661c7229901f568f16bd1551b3ba826f83ce49",
        market_cap="20044098.0173872", holders="522", alpha_id="ALPHA_1108",
    )
    base.update(kw)
    return ba.AlphaToken(**base)


class FakeNotifier:
    """记下发出去的每一条消息。send 的返回值可控 —— 要测"发失败"那条路径。"""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[str] = []
        self.ok = ok

    def send(self, text: str, **kw) -> bool:
        self.sent.append(text)
        return self.ok


class FakeClient:
    """
    离线的币安客户端。tokens/sector 都可以设成 None(拉取失败)或抛异常。

    ⚠️ 契约与真 client 一致:fetch_* 不抛异常、失败返回 None。
       `raise_on_tokens` 是**故意违约**,用来验证 AlphaWatcher 顶得住。
    """

    def __init__(self, tokens=None, sector=None, *, raise_on_tokens=None,
                 raise_on_sector=None) -> None:
        self.tokens = tokens
        self.sector = sector
        self.raise_on_tokens = raise_on_tokens
        self.raise_on_sector = raise_on_sector
        self.token_calls = 0
        self.sector_calls: list[tuple[int, int]] = []

    def fetch_tokens(self):
        self.token_calls += 1
        if self.raise_on_tokens is not None:
            raise self.raise_on_tokens
        return self.tokens

    def fetch_sector(self, rank_type: int, tab_id: int):
        self.sector_calls.append((rank_type, tab_id))
        if self.raise_on_sector is not None:
            raise self.raise_on_sector
        return self.sector

    def close(self) -> None:
        pass


@pytest.fixture
def db(tmp_path):
    """
    真实文件库 —— AlphaWatcher 内部走 store.get_conn(),内存库够不着。

    ⚠️ 与 test_poller 同样的写法:**自己开一个 MonkeyPatch**,不复用函数级的
       monkeypatch 夹具 —— 用例里任何一句 monkeypatch.undo() 都会把 DB_PATH
       还原成 data/fomo.db,那一刻起测试打的就是用户**正在跑的生产库**。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "alpha.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


def _mark() -> int | None:
    with store.get_conn() as c:
        raw = store.get_state(c, WATERMARK_KEY)
    return None if raw is None else int(raw)


def _watcher(client, notifier=None, sectors=()) -> ba.AlphaWatcher:
    """默认**不配任何板块** —— 板块是可选标注,大部分用例不该被它牵连。"""
    return ba.AlphaWatcher(notifier or FakeNotifier(), client=client, sectors=list(sectors))


# ============================================================
# 解析:全量名单
# ============================================================
def test_真实响应能解析出全部代币且都带上架时间():
    toks = ba.parse_tokens(_alpha_payload())
    assert toks is not None
    assert len(toks) == 30, "夹具是真实响应裁行得来的,行数变了说明夹具被改过"
    assert all(t.listing_time_ms > 0 for t in toks)
    newest = max(toks, key=lambda t: t.listing_time_ms)
    assert newest.symbol == "DEBIT"
    assert newest.listing_time_ms == DEBIT_MS


def test_拉取失败与名单为空必须是两种返回值():
    """
    None = 这轮不知道名单长什么样;[] = 确实一个都没有。
    混起来的话,一次结构变更会被当成"名单空了",水位线的判断整个失去依据。
    """
    assert ba.parse_tokens(None) is None
    assert ba.parse_tokens({"code": "100001", "success": False, "data": []}) is None
    assert ba.parse_tokens({"code": "000000", "success": True, "data": "oops"}) is None
    assert ba.parse_tokens({"code": "000000", "success": True, "data": []}) == []


def test_缺上架时间的行被丢掉而不是当成0():
    """
    listingTime 缺失时若兜底成 0,那一行会永远比水位线旧、永远不推 ——
    看起来"没坏",实际是静默漏掉。宁可丢掉并留一条 WARNING。
    """
    payload = {"code": "000000", "success": True, "data": [
        {"symbol": "GOOD", "listingTime": 1787738400000},
        {"symbol": "NOTIME"},
        {"symbol": "ZERO", "listingTime": 0},
        {"symbol": "JUNK", "listingTime": "abc"},
    ]}
    toks = ba.parse_tokens(payload)
    assert [t.symbol for t in toks] == ["GOOD"]


# ============================================================
# 主干:水位线
# ============================================================
def test_冷启动必须静默播种一条都不推(db):
    """
    ⚠️⚠️ 这是整个功能的生死线:第一次跑时名单里 600 多个币全都"比水位线新",
       照推就是几百条消息,用户当场静音,这个功能第一天就死了。
    """
    n = FakeNotifier()
    w = _watcher(FakeClient(tokens=ba.parse_tokens(_alpha_payload())), n)

    assert w.run_once() == 0
    assert n.sent == [], "冷启动一条都不许推"
    assert _mark() == DEBIT_MS, "水位线要落在名单里最新的那个上架时间上"


def test_播种之后同一份名单再跑一遍仍然一条都不推(db):
    """
    ⚠️ 这条守的是水位线用 `>` 而不是 `>=`。
       用 `>=` 的话,listingTime 恰好等于水位线的那个币(= 上一次上架的那个,必然存在)
       会**每一轮都被重推**,5 分钟一条直到用户静音。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    assert _watcher(FakeClient(tokens=toks), n).run_once() == 0
    assert n.sent == []


def test_比水位线新一毫秒的币要推出来(db):
    """`>` 的另一半:严格大于就必须推,不能因为"只差 1ms"被吞掉。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种到 DEBIT_MS
    n.sent.clear()

    fresh = _tok(symbol="NEW1", name="Newcomer", listing_time_ms=DEBIT_MS + 1,
                 contract_address="0x1111111111111111111111111111111111111111")
    assert _watcher(FakeClient(tokens=[*toks, fresh]), n).run_once() == 1
    assert len(n.sent) == 1
    assert "$NEW1" in n.sent[0]
    assert _mark() == DEBIT_MS + 1


def test_水位线必须落库进程重启后不能退回冷启动(db):
    """
    ⚠️ 只存在内存里的话:进程一重启 → 又是冷启动 → 静默播种 →
       重启期间上架的那个币**永远不会推**。表现是"没坏,就是收不到",最难查。
       这里用两个互不相干的 AlphaWatcher 实例模拟重启。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 实例 1:播种
    n.sent.clear()

    fresh = _tok(symbol="AFTERBOOT", listing_time_ms=DEBIT_MS + 60_000,
                 contract_address="0x2222222222222222222222222222222222222222")
    w2 = _watcher(FakeClient(tokens=[*toks, fresh]), n)      # 实例 2 = "重启后"
    assert w2.run_once() == 1, "水位线没落库的话这里会被当成冷启动,静默吞掉这个新币"
    assert "$AFTERBOOT" in n.sent[0]


def test_拉取失败时水位线一动不动(db):
    """
    失败就前移水位线 = 把这段时间的上新静默吃掉。
    失败后必须还能补推 —— 这才是"没丢"的证据。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    fresh = _tok(symbol="LATE", listing_time_ms=DEBIT_MS + 5,
                 contract_address="0x3333333333333333333333333333333333333333")
    assert _watcher(FakeClient(tokens=None), n).run_once() == 0   # 这一轮拉取失败
    assert _mark() == DEBIT_MS, "拉取失败绝不能动水位线"
    assert _watcher(FakeClient(tokens=[*toks, fresh]), n).run_once() == 1
    assert "$LATE" in n.sent[0]


def test_名单为空时不会把水位线清掉(db):
    toks = ba.parse_tokens(_alpha_payload())
    _watcher(FakeClient(tokens=toks)).run_once()
    assert _watcher(FakeClient(tokens=[])).run_once() == 0
    assert _mark() == DEBIT_MS


def test_水位线只前移不后退(db):
    """币安把最新那个币从名单里撤下来时,水位线不能跟着退 —— 退了就会重推一批。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    without_newest = [t for t in toks if t.listing_time_ms != DEBIT_MS]
    assert _watcher(FakeClient(tokens=without_newest), n).run_once() == 0
    assert _mark() == DEBIT_MS
    assert n.sent == []


def test_多个新币按上架时间从早到晚推(db):
    """先发生的先推 —— 顺序颠倒会让人以为后上的先上。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    a = _tok(symbol="EARLY", listing_time_ms=DEBIT_MS + 10, contract_address="0x" + "a" * 40)
    b = _tok(symbol="LATER", listing_time_ms=DEBIT_MS + 20, contract_address="0x" + "b" * 40)
    _watcher(FakeClient(tokens=[*toks, b, a]), n).run_once()
    assert ["$EARLY" in n.sent[0], "$LATER" in n.sent[1]] == [True, True]


def test_一次冒出太多改推一条汇总(db):
    """
    上游若把一批老币的 listingTime 集体重写成今天,逐条推就是几百条。
    上限写死 10:第 11 个开始改推**一条**汇总,信息不丢、Telegram 不炸。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    many = [_tok(symbol=f"M{i}", listing_time_ms=DEBIT_MS + 1 + i,
                 contract_address="0x" + f"{i:040d}") for i in range(11)]
    _watcher(FakeClient(tokens=[*toks, *many]), n).run_once()
    assert len(n.sent) == 1, "11 个新币必须收敛成一条汇总,而不是 11 条"
    assert "11" in n.sent[0]
    assert "$M0" in n.sent[0]


def test_刚好十个仍然逐条推(db):
    """上限是 10:等于 10 时不该被汇总吞掉,否则边界差一位就丢细节。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    many = [_tok(symbol=f"K{i}", listing_time_ms=DEBIT_MS + 1 + i,
                 contract_address="0x" + f"{i:040d}") for i in range(10)]
    _watcher(FakeClient(tokens=[*toks, *many]), n).run_once()
    assert len(n.sent) == 10


# ============================================================
# 板块标注(尽力而为)
# ============================================================
def test_板块响应能解析出成员():
    members = ba.parse_sector(_sector_payload(), rank_type=60, tab_id=61)
    assert members is not None
    assert len(members) == 21, "夹具是真实响应,成员数变了说明夹具被改过"
    assert ("56", NIULAI_CA) in members


def test_未知tabId返回全量时必须整份丢弃():
    """
    ⚠️⚠️ 实测的坑:传一个不存在的 tabId(如 63)币安**不报错**,而是返回全量
       3139 个代币。照单全收 = 把几千个币全标成「股票 Meme 幣」,那一行标注就成了
       彻头彻尾的假信息。超过 500 就整份丢弃 + 告警。
    ⚠️ 判据必须看 total:请求带了 size=100,服务端把 tokens 截到 100 条 ——
       失效时的三千多条在 tokens 里表现为 100,**只看 len 的话这道闸一辈子不会响**。
    """
    payload = _sector_payload()
    payload["data"]["total"] = 3139          # tokens 仍然只有 21/100 条
    assert ba.parse_sector(payload, rank_type=60, tab_id=63) is None


def test_total缺失时用条数兜底():
    payload = {"code": "000000", "success": True, "data": {
        "tokens": [{"chainId": "56", "contractAddress": f"0x{i:040d}"} for i in range(501)],
    }}
    assert ba.parse_sector(payload, rank_type=60, tab_id=61) is None


def test_刚好五百个成员仍然收下():
    """上限是"超过 500",等于 500 不该被误杀 —— 板块做大了不是失效。"""
    payload = {"code": "000000", "success": True, "data": {
        "total": 500,
        "tokens": [{"chainId": "56", "contractAddress": f"0x{i:040d}"} for i in range(100)],
    }}
    members = ba.parse_sector(payload, rank_type=60, tab_id=61)
    assert members is not None and len(members) == 100


def test_命中板块的币推送里多一行标注(db):
    """用真实夹具:牛来(0xbeea…7777)确实同时在 Alpha 名单和股票 Meme 幣板块里。"""
    toks = ba.parse_tokens(_alpha_payload())
    older = [t for t in toks if t.listing_time_ms < NIULAI_MS]
    n = FakeNotifier()
    c = FakeClient(tokens=older, sector=ba.parse_sector(_sector_payload(), rank_type=60, tab_id=61))
    ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once()   # 播种
    n.sent.clear()

    c.tokens = [t for t in toks if t.listing_time_ms <= NIULAI_MS]
    ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once()
    assert len(n.sent) == 1
    assert "股票 Meme 幣" in n.sent[0]
    assert "🏷" in n.sent[0]


def test_板块拉取失败照样推主干只是少一行标注(db):
    """
    ⚠️ 主干(名单新增)与标注(板块)地位不对等:板块是币安运营编排的、
       随时可能下线的东西,它挂了绝不能把上新推送一起带走。
    """
    toks = ba.parse_tokens(_alpha_payload())
    older = [t for t in toks if t.listing_time_ms < NIULAI_MS]
    n = FakeNotifier()
    c = FakeClient(tokens=older, sector=None)                 # 板块拉取失败
    ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once()
    n.sent.clear()

    c.tokens = [t for t in toks if t.listing_time_ms <= NIULAI_MS]
    assert ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once() == 1
    assert "牛来" in n.sent[0], "板块没了,主干推送必须照发"
    assert "🏷" not in n.sent[0], "缺失整行消失,绝不打 N/A"


def test_板块接口抛异常也不能带倒主干(db):
    """client 契约上不抛,但它是可替换依赖 —— 主干不能把命交给它。"""
    toks = ba.parse_tokens(_alpha_payload())
    older = [t for t in toks if t.listing_time_ms < NIULAI_MS]
    n = FakeNotifier()
    c = FakeClient(tokens=older, raise_on_sector=RuntimeError("板块接口 500"))
    ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once()
    n.sent.clear()

    c.tokens = [t for t in toks if t.listing_time_ms <= NIULAI_MS]
    assert ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once() == 1


def test_没配板块时不打板块接口(db):
    """一个都没配就别去打人家接口 —— 币安风控严,白打一次是白挨一次。"""
    toks = ba.parse_tokens(_alpha_payload())
    c = FakeClient(tokens=[t for t in toks if t.listing_time_ms < NIULAI_MS])
    _watcher(c).run_once()
    c.tokens = toks
    _watcher(c).run_once()
    assert c.sector_calls == []


# ============================================================
# 异常绝不逃逸到调度器
# ============================================================
def test_名单接口抛异常时run_once不抛(db):
    """
    ⚠️⚠️ run_once 是这个功能与 APScheduler 的唯一接触面。异常逃逸出去,
       轻则日志里一堆 job 崩溃栈,重则被上层某个 except 当成"该停机了" ——
       而 cli._tick_job 里那条 AuthError 路径是真的会 sched.shutdown()。
    """
    w = _watcher(FakeClient(raise_on_tokens=RuntimeError("连接被重置")))
    assert w.run_once() == 0


def test_数据库坏掉时run_once不抛(db, monkeypatch):
    monkeypatch.setattr(store, "get_state", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("database is locked")))
    w = _watcher(FakeClient(tokens=ba.parse_tokens(_alpha_payload())))
    assert w.run_once() == 0


def test_推送本身抛异常时run_once不抛(db):
    class BoomNotifier:
        def send(self, text, **kw):
            raise RuntimeError("代理断了")

    toks = ba.parse_tokens(_alpha_payload())
    _watcher(FakeClient(tokens=toks)).run_once()             # 播种
    fresh = _tok(symbol="BOOM", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "c" * 40)
    w = ba.AlphaWatcher(BoomNotifier(), client=FakeClient(tokens=[*toks, fresh]), sectors=[])
    assert w.run_once() == 0


def test_推送失败不算数但水位线照样前移(db):
    """
    ⚠️ 水位线的语义是「已经处理过」,不是「已经送达」。notifier 自己重试三次,
       再失败就记 error —— 不前移的话 TG 长期不可用时每轮重来一遍,
       恢复那一刻全炸出来。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier(ok=False)
    _watcher(FakeClient(tokens=toks), n).run_once()
    fresh = _tok(symbol="FAILSEND", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "d" * 40)
    assert _watcher(FakeClient(tokens=[*toks, fresh]), n).run_once() == 0
    assert _mark() == DEBIT_MS + 1


# ============================================================
# 推送文案
# ============================================================
def _tags(msg: str) -> list[tuple[str, str]]:
    return re.findall(r"<(/?)([a-zA-Z]+)[^<>]*>", msg)


def _html_ok(msg: str) -> bool:
    """标签全部配对闭合 —— 未闭合标签让 TG 整条 400,用户什么都收不到"""
    stack: list[str] = []
    for slash, name in _tags(msg):
        if slash:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _render(**kw) -> str:
    from src.formatter import render_alpha_listing

    base = dict(
        symbol="DEBIT", name="Teller", network_id="bsc", chain_name="BSC",
        contract_address="0x66661c7229901f568f16bd1551b3ba826f83ce49",
        listing_time_ms=DEBIT_MS, market_cap="20044098.0173872", holders="522",
        # 渲染时刻写死,否则"多久之前上架"那一格会随测试运行日期漂
        now=DEBIT_MS / 1000 + 3 * 3600,
    )
    base.update(kw)
    return render_alpha_listing(**base)


def test_推送含币名市值持有人上架时间链和CA():
    msg = _render()
    assert msg.startswith("🆕"), "行首锚点必须有,且与买卖/跟单/分发那几个都不重样"
    assert msg[0] not in "🌱🟢🔴💭📥📤🧪🛒🚨"
    assert "$DEBIT" in msg and "Teller" in msg
    assert "$20.04M" in msg
    assert "522" in msg
    assert "2026-08-26 10:00 UTC" in msg
    assert "BNB Chain" in msg
    assert "gmgn.ai" in msg
    assert msg.split("\n")[-1] == "<code>0x66661c7229901f568f16bd1551b3ba826f83ce49</code>", \
        "CA 独占最后一行、纯 code —— 中国网络下唯一 100% 可用的操作是 tap-to-copy"


def test_币名与符号相同时不重复显示():
    """「牛来 · 牛来」只是噪音。"""
    msg = _render(symbol="牛来", name="牛来")
    assert msg.count("牛来") == 1


def test_缺市值缺持有人时整行消失绝不打0():
    msg = _render(market_cap=None, holders=None)
    assert "市值" not in msg
    assert "持有人" not in msg
    assert "N/A" not in msg and "--" not in msg


def test_持有人为0照常显示():
    """0 是有意义的真实值(刚上架确实可能一个持有人都没有),不能被真值判断吞掉。"""
    assert "持有人 0" in _render(holders="0")


def test_币名里的尖括号必须转义且只转一次():
    """
    ⚠️ name/symbol 来自链上,是**陌生人可控的任意字符串**。一个裸 '<' 就让整条消息
       400 —— 既是稳定性问题,更是"改个币名就让监控静默失效"的攻击面。
    """
    msg = _render(symbol="<b>PWN</b>", name="a & b <script>")
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    assert "&amp; b" in msg
    assert "&amp;amp;" not in msg, "转义了两次的话用户会看到 &amp;lt;"
    assert _html_ok(msg)


def test_超长币名不会顶破长度预算也不会破坏HTML():
    msg = _render(symbol="Ω" * 5000, name="猫" * 5000)
    assert len(msg) <= 3600, "超预算的消息会被 notifier 盲切,切在实体中间就是整条 400"
    assert _html_ok(msg)
    assert msg.split("\n")[-1].startswith("<code>")


def test_还没到上架时刻要说即将上架():
    """
    实测名单里会出现**还没到上架时刻**的币(DEBIT 在 10:00 之前就已经在名单里)。
    这时"多久之前"是负数,绝不能渲染成「-3H前」,也不该整格消失 ——
    「即将上架」是这条消息此刻最要紧的一句话。
    """
    msg = _render(now=DEBIT_MS / 1000 - 3600)
    assert "即将上架" in msg
    assert "前" not in msg.split("\n")[-2]


def test_拿不到CA时不留空的code块():
    """空的 <code></code> 是个点了复制不出东西的假区域,比没有更糟。"""
    msg = _render(contract_address=None)
    assert "<code></code>" not in msg
    assert _html_ok(msg)


def test_未收录的链用币安给的原始链名兜底():
    msg = _render(network_id="99999", chain_name="SomeNewChain")
    assert "SomeNewChain" in msg


def test_汇总消息说清楚这不正常():
    from src.formatter import render_alpha_batch

    msg = render_alpha_batch([(f"T{i}", DEBIT_MS + i) for i in range(42)])
    assert "42" in msg
    assert "异常" in msg, "必须自己说清楚这不正常,而不是假装一切正常地报个大数字"
    assert "$T0" in msg
    assert len(msg) <= 3600
    assert _html_ok(msg)


# ============================================================
# 配置
# ============================================================
def test_板块配置能解析多个且允许显示名带冒号():
    from src.config import FomoSettings

    s = FomoSettings(fomo_alpha_sectors="60:61:股票 Meme 幣, 10:0:热门:榜")
    assert s.alpha_sectors == [(60, 61, "股票 Meme 幣"), (10, 0, "热门:榜")]


def test_板块配置写坏了只跳过那一条不炸进程():
    """
    ⚠️ 板块标注是锦上添花,一个手滑的配置绝不该让整个进程起不来 ——
       更不该让主干的上新推送跟着死。
    """
    from src.config import FomoSettings

    s = FomoSettings(fomo_alpha_sectors="坏的, 60:61:股票 Meme 幣, 9:x:歪的, 1:2:")
    assert s.alpha_sectors == [(60, 61, "股票 Meme 幣")]


def test_板块留空就是不标注():
    from src.config import FomoSettings

    assert FomoSettings(fomo_alpha_sectors="").alpha_sectors == []
