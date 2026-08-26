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
import time
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


def _ledger() -> set[tuple[str, str]]:
    """已推送台账里现有的键。⚠️ 表名/列名写死,不从被测模块 import。"""
    with store.get_conn() as c:
        rows = c.execute("SELECT network_id, token_address FROM binance_alpha_pushed").fetchall()
    return {(r["network_id"], r["token_address"]) for r in rows}


# 宽限窗口 7 天。⚠️ 同样刻意写死:从 ba.GRACE_WINDOW_MS 取的话,
# 谁把它改成 0(= 退回纯水位线、同毫秒的币照样漏)测试也不会红。
WEEK_MS = 7 * 24 * 60 * 60 * 1000


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


# ============================================================
# 【必修 1】同毫秒代币分批到达 —— 已推送台账
# ============================================================
def test_同毫秒上架的币分两轮到达三个都要推到且不重复(db):
    """
    ⚠️⚠️ 这是 `listingTime > 水位线` 那个写法**永久静默漏推**的实测复现。

    一次真实响应的统计:665 条名单里 32 组代币共享同一个 listingTime,
    覆盖 100 个币(15%),最大一组 10 个 —— 15% 的代币都处在这个风险里。
    「进名单的时刻」与「listingTime」是脱钩的两件事(名单里存在尚未到上架时刻的币,
    formatter 那个「即将上架」分支就是为它写的),所以同组分两轮进名单完全可能,
    而巡检间隔是 5 分钟。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种,水位线 = DEBIT_MS
    n.sent.clear()

    same = DEBIT_MS + 60_000
    a = _tok(symbol="SAMEA", listing_time_ms=same, contract_address="0x" + "a" * 40)
    b = _tok(symbol="SAMEB", listing_time_ms=same, contract_address="0x" + "b" * 40)
    c = _tok(symbol="SAMEC", listing_time_ms=same, contract_address="0x" + "c" * 40)

    # 第一轮:同毫秒的三个里先进名单两个
    assert _watcher(FakeClient(tokens=[*toks, a, b]), n).run_once() == 2
    assert _mark() == same, "水位线被这一批顶到了同一毫秒上"

    # 第二轮:第三个才进名单,它的 listingTime 与水位线**一模一样**
    assert _watcher(FakeClient(tokens=[*toks, a, b, c]), n).run_once() == 1, \
        "用 `listingTime > 水位线` 的话这里是 0 —— 该币永久丢失,且没有任何日志留痕"

    got = [s for s in ("SAMEA", "SAMEB", "SAMEC") if any(f"${s}" in m for m in n.sent)]
    assert got == ["SAMEA", "SAMEB", "SAMEC"], "三个都要推到"
    assert len(n.sent) == 3, "而且一条都不许重复 —— 改用 `>=` 换来的就是这里变成 5 条"


def test_推过的币在宽限窗口内不会被重推(db):
    """
    候选窗口放宽到「水位线 - 7 天」之后,**去重必须由台账负责**:
    否则窗口里的每个币每 5 分钟重推一次,比原来的漏推还糟。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    fresh = _tok(symbol="ONCE", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "e" * 40)
    full = [*toks, fresh]
    assert _watcher(FakeClient(tokens=full), n).run_once() == 1
    for _ in range(3):
        assert _watcher(FakeClient(tokens=full), n).run_once() == 0
    assert len(n.sent) == 1, "同一个币这辈子只推一次"


def test_推送失败的币下一轮补推(db):
    """
    ⚠️ 台账只记**真正发出去的**。把发失败的也记进去就等于把这个币判死:
       一次 TG 400 或代理抖动 = 永久漏推,而日志里只有一行 error。
       (水位线仍然照常前移 —— 它的语义是「已经处理过」,不是「已经送达」。)
    """
    toks = ba.parse_tokens(_alpha_payload())
    ok = FakeNotifier()
    _watcher(FakeClient(tokens=toks), ok).run_once()         # 播种
    fresh = _tok(symbol="RETRY", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "f" * 40)

    dead = FakeNotifier(ok=False)
    assert _watcher(FakeClient(tokens=[*toks, fresh]), dead).run_once() == 0
    assert ("bsc", "0x" + "f" * 40) not in _ledger(), "没发出去的绝不能记进台账"

    ok.sent.clear()
    assert _watcher(FakeClient(tokens=[*toks, fresh]), ok).run_once() == 1, \
        "上一轮没发出去,这一轮必须补上"
    assert "$RETRY" in ok.sent[0]


def test_台账会清掉掉出宽限窗口的行不会无限增长(db):
    """
    ⚠️ 去重表必须有清理策略,否则每上一个新币就多一行、永不回收。
       阈值 = 新水位线 - 宽限窗口:掉到窗口外的币永远不会再成为候选,留着纯属浪费。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种,水位线 DEBIT_MS

    old = _tok(symbol="OLDONE", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "1" * 40)
    _watcher(FakeClient(tokens=[*toks, old]), n).run_once()
    assert ("bsc", "0x" + "1" * 40) in _ledger(), "刚推过的币当然在台账里"

    # 一个整整 7 天之后上架的币把水位线顶过去 → OLDONE 掉到窗口下界上,该被清掉
    far = _tok(symbol="FARONE", listing_time_ms=DEBIT_MS + 1 + WEEK_MS,
               contract_address="0x" + "2" * 40)
    _watcher(FakeClient(tokens=[*toks, old, far]), n).run_once()
    led = _ledger()
    assert ("bsc", "0x" + "2" * 40) in led, "还在窗口里的必须留着,否则它下一轮就被重推"
    assert ("bsc", "0x" + "1" * 40) not in led, "掉出宽限窗口的行必须清掉,否则台账永不回收"


def test_台账掉出窗口之后那个币也不会被重推(db):
    """清理不能把「已处理」清成「没处理过」—— 窗口下界本身就挡着它。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    old = _tok(symbol="OLDONE", listing_time_ms=DEBIT_MS + 1, contract_address="0x" + "1" * 40)
    far = _tok(symbol="FARONE", listing_time_ms=DEBIT_MS + 1 + WEEK_MS,
               contract_address="0x" + "2" * 40)
    _watcher(FakeClient(tokens=[*toks, old]), n).run_once()
    _watcher(FakeClient(tokens=[*toks, old, far]), n).run_once()
    n.sent.clear()

    assert _watcher(FakeClient(tokens=[*toks, old, far]), n).run_once() == 0
    assert n.sent == []


def test_老库升级不会重推历史上已经推过的币(db):
    """
    ⚠️ 台账是新加的表,老库里它是空的 —— 若把「不在台账里」直接当成「没推过」,
       升级那一轮会把宽限窗口内的历史币全部重推一遍。
       水位线的语义本来就是「这个时刻(含)之前的都已处理过」,照它初始化台账即可,
       而且 base 之上那些**同一轮就推出来**,不该白等一个巡检间隔。
    """
    toks = ba.parse_tokens(_alpha_payload())
    with store.get_conn() as c, store.tx(c):
        store.set_state(c, WATERMARK_KEY, str(TMX_MS))       # 模拟老版本留下的水位线

    n = FakeNotifier()
    assert _watcher(FakeClient(tokens=toks), n).run_once() == 1, \
        "只有比老水位线新的 DEBIT 该推,窗口内的历史币一个都不许重推"
    assert "$DEBIT" in n.sent[0]
    assert not any("$TMX" in m for m in n.sent), "TMX 的上架时间等于老水位线,历史上已经推过"


def test_台账为空是正常状态不能被当成没初始化过(db):
    """
    ⚠️ 清理会把台账清空(窗口内一个币都没有时),这是**正常状态**。
       靠「表是不是空的」判有没有初始化过,会让一个迟到的同毫秒币被当成历史存量
       二次播种 —— 于是它又一次静默丢失。所以初始化标记必须是独立的一位。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    with store.get_conn() as c, store.tx(c):
        c.execute("DELETE FROM binance_alpha_pushed")        # 手工模拟"被清理干净了"
    assert _ledger() == set()
    n.sent.clear()

    late = _tok(symbol="LATESAME", listing_time_ms=DEBIT_MS, contract_address="0x" + "7" * 40)
    _watcher(FakeClient(tokens=[*toks, late]), n).run_once()
    assert any("$LATESAME" in m for m in n.sent), \
        "台账空了不等于没初始化过 —— 二次播种会把这个迟到的同毫秒币再吃掉一次"


def test_名单里最新的币被撤下时水位线也不许后退(db):
    """
    ⚠️ 水位线后退 = 候选窗口下界跟着后退,而那段区间的台账早被清理掉了 ——
       于是一批老币重新变成「没推过」,全部重推一遍。
       既有的「只前移不后退」测的是**没有新币可推**的情形;这条补上有新币可推的那一半,
       那才是水位线真正被写回去的那条路径。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种,水位线 DEBIT_MS
    n.sent.clear()

    # 币安把最新的 DEBIT 撤下,同时一个更早的币迟到进名单
    without_newest = [t for t in toks if t.listing_time_ms != DEBIT_MS]
    late = _tok(symbol="LATECOMER", listing_time_ms=TMX_MS + 1,
                contract_address="0x" + "3" * 40)
    assert _watcher(FakeClient(tokens=[*without_newest, late]), n).run_once() == 1
    assert "$LATECOMER" in n.sent[0]
    assert _mark() == DEBIT_MS, "推了一个更早的币,水位线绝不能被拽回到它那里"


def test_台账重复写同一个币不抛也不重复(db):
    """
    ⚠️ 主键冲突不是错误:播种与后续推送本来就会碰上同一个币。
       写成裸 INSERT 的话一次冲突就让整个事务回滚 —— 水位线跟着不动、台账也没记上,
       下一轮撞同一处,从此原地卡死。
    """
    row = ("bsc", "0x" + "4" * 40, "DUP", DEBIT_MS)
    with store.get_conn() as c:
        with store.tx(c):
            store.record_alpha_pushed(c, [row])
        with store.tx(c):
            store.record_alpha_pushed(c, [row, row])         # 同一轮里也撞
        cnt = c.execute("SELECT COUNT(*) AS n FROM binance_alpha_pushed").fetchone()["n"]
    assert cnt == 1, "同一个键只该有一行"


# ============================================================
# 【必修 2】链解析:chainId 不是纯数字
# ============================================================
# 线上一次真实调用的全部 9 种 chainId,以及各自该解析成什么、该不该有链接行。
# 分布:56:490 / CT_501:70 / 8453:42 / 1:38 / CT_784:13 / 42161:4 / 146:4 / CT_195:3 / 59144:1
# ⚠️ 期望值全部写死字面量,不从 models 的 slug 表反推 —— 那样等于 `x == x`。
_LIVE_CHAINS = [
    ("56", "BSC", "bsc", True, "BNB Chain"),
    ("CT_501", "Solana", "solana", True, "Solana"),
    ("8453", "Base", "base", True, "Base"),
    ("1", "Ethereum", "ethereum", True, "Ethereum"),
    # 下面五条本仓库没有 slug:解析结果就是原始 chainId 透传(= "我们不认识这条链"),
    # 链接行整行消失,其余各行照常。
    ("CT_784", "Sui", "ct_784", False, "Sui"),
    ("42161", "Arbitrum", "42161", False, "Arbitrum"),
    ("146", "Sonic", "146", False, "Sonic"),
    ("CT_195", "TRON", "ct_195", False, "TRON"),
    ("59144", "Linea", "59144", False, "Linea"),
]


@pytest.mark.parametrize(("chain_id", "chain_name", "net", "has_link", "display"), _LIVE_CHAINS)
def test_线上九种链逐个解析与渲染(chain_id, chain_name, net, has_link, display):
    """
    ⚠️⚠️ 原来代码里写着「实测 665 条名单里 chainId 清一色 56」——**那是假的**,
       BSC 只占 490/665。靠 chainId 数字去映射,70 个 Solana 币(chainId 是 `CT_501`)
       会直接丢掉整行链接,而 Solana 是本仓库完整支持的链。
    ⚠️ 未收录的链没有链接行是**正确行为**:硬拼一个必然 404 的链接比没有链接更糟。
       但它绝不能影响其余各行。
    """
    assert ba.resolve_network(chain_name, chain_id) == net

    msg = _render(symbol="TESTSYM", name="Test Token", network_id=net, chain_name=chain_name)
    assert ("🔗" in msg) is has_link
    if has_link:
        assert "fomo.family" in msg and "gmgn.ai" in msg
    assert f"🧬 {display}" in msg, "链那一行任何情况下都在(内部展示名,查不到才用币安原始链名)"
    # 链接有没有都不影响其余各行
    assert "$TESTSYM" in msg
    assert "$20.04M" in msg
    assert msg.split("\n")[-1].startswith("<code>"), "CA 永远独占最后一行"


def test_Solana代币的推送必须带链接行(db):
    """
    ⚠️ 上一条测的是纯函数;这一条走完整的 run_once 链路,钉住**接线本身** ——
       把 _push 里的 network_id 改回 normalize_network(chain_id) 时,上一条照样绿。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    sol_ca = "G7vQWurMkMMm2dU3iZpXYFTHT9Biio4F4gZCrwFpKNwG"
    sol = ba.AlphaToken(listing_time_ms=DEBIT_MS + 1, symbol="BIRB", name="Moonbirds",
                        chain_id="CT_501", chain_name="Solana", contract_address=sol_ca,
                        market_cap="15570000", holders="15135", alpha_id="ALPHA_999")
    assert _watcher(FakeClient(tokens=[*toks, sol]), n).run_once() == 1
    assert f"https://fomo.family/tokens/solana/{sol_ca}" in n.sent[0]
    assert f"https://gmgn.ai/sol/token/{sol_ca}" in n.sent[0]


def test_未收录的链照常推只是没有链接行(db):
    """⚠️ 拼不出链接绝不能升级成「不推」—— 那是把一行的缺失变成整条消息的缺失。"""
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    sui_ca = "0x97c7571f4406cdd7a95f3027075ab80d3e9c937c2a567690d31e14ab1872ccee::xmn::XMN"
    sui = ba.AlphaToken(listing_time_ms=DEBIT_MS + 1, symbol="XMN", name="Ximen",
                        chain_id="CT_784", chain_name="Sui", contract_address=sui_ca,
                        market_cap="1234567", holders="88", alpha_id="ALPHA_888")
    assert _watcher(FakeClient(tokens=[*toks, sui]), n).run_once() == 1
    assert "🔗" not in n.sent[0]
    assert "$XMN" in n.sent[0] and "$1.23M" in n.sent[0] and "🧬 Sui" in n.sent[0]
    assert n.sent[0].split("\n")[-1] == f"<code>{sui_ca}</code>"


def test_链名缺失时退回chainId():
    """chainName 是这次修复的首选依据,但它不是必填 —— 缺了要能退回 chainId。"""
    assert ba.resolve_network(None, "56") == "bsc"
    assert ba.resolve_network("", "8453") == "base"
    assert ba.resolve_network(None, None) is None


# ============================================================
# 【必修 3】未来时间戳不许顶死水位线
# ============================================================
def test_未来时间戳不许顶高水位线功能不能因此死掉(db):
    """
    ⚠️⚠️ 没有上界的话,一行 listingTime=99999999999999(公元 5138 年)就把水位线顶到那里,
       此后**再也没有任何代币比水位线新** —— 功能永久静默死亡,没有日志、没有告警,
       用户只会以为币安很久没上新了。
       不是纯假想:formatter 里那个「即将上架」分支的存在,说明名单里确实出现过未来时间戳,
       一次预挂牌公告就够了。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    bogus = _tok(symbol="Y5138", listing_time_ms=99999999999999,
                 contract_address="0x" + "9" * 40)
    _watcher(FakeClient(tokens=[*toks, bogus]), n).run_once()
    assert _mark() == DEBIT_MS, "水位线绝不能跟着未来时间戳走"

    # 证据:功能没死 —— 后面来的正常新币照样推得出来
    n.sent.clear()
    later = _tok(symbol="STILLALIVE", listing_time_ms=DEBIT_MS + 1000,
                 contract_address="0x" + "8" * 40)
    assert _watcher(FakeClient(tokens=[*toks, bogus, later]), n).run_once() == 1, \
        "水位线被顶到公元 5138 年的话,这里永远是 0,而且一条日志都没有"
    assert "$STILLALIVE" in n.sent[0]


def test_整份名单全是未来时间戳时水位线一动不动(db):
    """一个可信值都没有 ≠ 没有新币。宁可这一轮什么都不做,下一轮重来。"""
    toks = ba.parse_tokens(_alpha_payload())
    _watcher(FakeClient(tokens=toks)).run_once()             # 播种

    n = FakeNotifier()
    junk = [_tok(symbol="J1", listing_time_ms=99999999999999,
                 contract_address="0x" + "a" * 40)]
    assert _watcher(FakeClient(tokens=junk), n).run_once() == 0
    assert _mark() == DEBIT_MS
    assert n.sent == []


def test_几小时后才上架的币是正常业务不能被当成脏数据(db):
    """
    ⚠️ 上界不能一刀切:名单里确实会出现尚未到上架时刻的币(实测 DEBIT 在 10:00 前
       就已经在名单里)。预挂牌是正常业务,把它判脏就是把真实的上新吃掉。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()
    n.sent.clear()

    soon_ms = int(time.time() * 1000) + 6 * 3600 * 1000
    soon = _tok(symbol="SOON", listing_time_ms=soon_ms, contract_address="0x" + "5" * 40)
    assert _watcher(FakeClient(tokens=[*toks, soon]), n).run_once() == 1
    assert "即将上架" in n.sent[0]
    assert _mark() == soon_ms, "6 小时后上架在上界之内,水位线该跟上"


def test_一个月之后的上架时间不可信不许顶高水位线(db):
    """
    ⚠️ 光"拒绝公元 5138 年"是不够的:上界只要放得够松(比如 100 年),
       一个 2100 年的脏值照样能把水位线顶死,而测试全绿。
       这条把余量的**上限**钉住 —— 实测预告提前量是几小时级,一个月已经宽出两个数量级,
       再往后的值只能是脏数据。与上一条一起把余量夹在 (6 小时, 30 天) 之间。
    """
    toks = ba.parse_tokens(_alpha_payload())
    n = FakeNotifier()
    _watcher(FakeClient(tokens=toks), n).run_once()          # 播种
    n.sent.clear()

    far_ms = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
    far = _tok(symbol="MONTHLATER", listing_time_ms=far_ms, contract_address="0x" + "6" * 40)
    _watcher(FakeClient(tokens=[*toks, far]), n).run_once()
    assert _mark() == DEBIT_MS, "一个月之后的上架时间只能是脏数据,水位线绝不能跟着走"


# ============================================================
# 【必修 4】symbol 的转义
# ============================================================
def test_符号里的尖括号与和号必须转义且只转一次():
    """
    ⚠️⚠️ symbol 与 name 一样是**陌生人可控的链上文本**,一个裸 '<' 就让整条消息 400 ——
       既是稳定性问题,更是「改个币名就让监控静默失效」的攻击面。
    ⚠️ 原来那条用例虽然也传了带标签的 symbol,但三条断言**全部落在 name 上**:
       把 render_alpha_listing 里 symbol 那一路的转义删掉,131 条测试照样全绿。
       这条专门守 symbol。
    """
    msg = _render(symbol="<i>A & B</i>", name="Teller")
    assert "&lt;i&gt;A &amp; B&lt;/i&gt;" in msg, "符号必须转义"
    assert "<i>" not in msg and "</i>" not in msg, "裸标签一个都不许漏进去"
    assert "&amp;amp;" not in msg and "&amp;lt;" not in msg, "转两次的话用户看到的是 &lt;"
    assert _html_ok(msg)


def test_汇总消息里的符号也必须转义():
    """汇总走的是另一条渲染路径,同样是陌生人可控文本。"""
    from src.formatter import render_alpha_batch

    msg = render_alpha_batch([("<i>X & Y</i>", DEBIT_MS)])
    assert "&lt;i&gt;X &amp; Y&lt;/i&gt;" in msg
    assert "<i>" not in msg and "</i>" not in msg
    assert "&amp;amp;" not in msg
    assert _html_ok(msg)


# ============================================================
# 【必修 6】市值恰为 0
# ============================================================
def test_市值恰为0照常显示():
    """
    ⚠️ 0 是有意义的真实值 —— 实测线上此刻就有 1 个 marketCap 为 "0" 的代币(PORT3)。
       这一格用真值判断的话整行会消失,用户读到的是「这个币没有市值数据」,
       而事实是「这个币的市值是 0」。持有人那一侧有测试守着,市值这一侧原先漏了。
    """
    assert "市值 $0.00" in _render(market_cap="0")
    assert "市值 $0.00" in _render(market_cap=0)


# ============================================================
# 【必修 7】板块映射的地址归一化
# ============================================================
def test_板块映射对地址大小写不敏感(db):
    """
    ⚠️⚠️ 这正是代码自己注释里警告过的失效模式:「两边必须用同一个函数,
       大小写一差就永远命不中」。两份夹具恰好都是小写,构造不出差异 ——
       这里手工造一个 checksum 大小写的名单地址,与板块侧的小写地址配对。
    """
    ca_lower = "0xbeea1d618e533a387d941f58a7d4c9b7bd377777"
    ca_mixed = "0xBEEA1d618E533a387D941f58A7d4C9b7bd377777"
    assert ca_mixed != ca_lower and ca_mixed.lower() == ca_lower

    toks = ba.parse_tokens(_alpha_payload())
    older = [t for t in toks if t.listing_time_ms < NIULAI_MS]
    n = FakeNotifier()
    c = FakeClient(tokens=older, sector=[("56", ca_lower)])
    ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once()   # 播种
    n.sent.clear()

    mixed = _tok(symbol="MIXED", name="Mixed Case", listing_time_ms=NIULAI_MS,
                 contract_address=ca_mixed)
    c.tokens = [*older, mixed]
    assert ba.AlphaWatcher(n, client=c, sectors=[(60, 61, "股票 Meme 幣")]).run_once() == 1
    assert "股票 Meme 幣" in n.sent[0], \
        "名单侧是 checksum 大小写、板块侧是小写,两边归一化后必须命中"


def test_板块响应里的大小写地址也要归一化():
    """归一化的另一半:板块侧返回 checksum 大小写时同样要压平。"""
    payload = {"code": "000000", "success": True, "data": {"total": 1, "tokens": [
        {"chainId": "56", "contractAddress": "0xBEEA1d618E533a387D941f58A7d4C9b7bd377777"},
    ]}}
    assert ba.parse_sector(payload, rank_type=60, tab_id=61) == [
        ("56", "0xbeea1d618e533a387d941f58a7d4c9b7bd377777")]


def test_Solana地址绝不能被小写掉():
    """
    ⚠️ Solana 是 base58,**大小写敏感**。归一化若无脑 lower(),
       板块映射永远命不中,去重台账也会跟真实地址对不上。
    """
    sol_ca = "G7vQWurMkMMm2dU3iZpXYFTHT9Biio4F4gZCrwFpKNwG"
    tok = ba.AlphaToken(listing_time_ms=DEBIT_MS, symbol="BIRB", chain_id="CT_501",
                        chain_name="Solana", contract_address=sol_ca)
    assert tok.sector_key == ("CT_501", sol_ca)
    assert tok.dedupe_key == ("solana", sol_ca)
