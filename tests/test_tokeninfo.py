"""
发射台 / 持有人的取值层(src/tokeninfo.py)。

⚠️ 全部离线:响应是 2026-09-03 真网络录下来的夹具(tests/fixtures/fomo_filter_tokens_*.json、
   blockscout_*.json),裁剪只删字段、不改值。
⚠️ 断言写死字面量:不从 src.tokeninfo import 任何阈值、TTL、工厂表来断言自己。
"""
# ruff: noqa: N802
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import store, tokeninfo
from src.tokeninfo import (
    TokenExtra,
    TokenExtraLookup,
    parse_creation_tx,
    parse_filter_tokens,
    parse_holders,
    parse_launchpad_name,
    parse_tx_to,
    pons_version,
)

FIX = Path(__file__).parent / "fixtures"

# ⚠️ conftest 的 _no_tokeninfo_network 会把这两个方法桩掉(那是全场的兜底防线)。
#    下面几条**就是要测这两个方法本身**,所以在这里先把真身留一份,
#    用 _wire 把它还原回去 —— 还原走 monkeypatch,用例结束自动收回。
_REAL_FETCH = tokeninfo.FilterTokensClient.fetch
_REAL_BS_GET = tokeninfo.BlockscoutClient._get

CUM = "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18"
CASHCAT = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
BLAZE = "0x30ff1d18f1e6c0e0e7f6cf0b4c0b7d0e8ce30c0e"        # 夹具里那个 pons 币,见下
PONS_V1 = "0xc080ca36443999a5c91fee51ac0ebd2dc2c12bfc"
PONS_V2 = "0x2556ba350f119b613efa3a0d9aaff8717b87801a"
CATE = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"
MARSCOIN = "0xfe189e97832da1573e4e4ff034f4ffc3a15c7777"
NVDAC = "0xb20000000000000000000078ee7ce2fe4908108c"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _fix(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _multi():
    return _fix("fomo_filter_tokens_multi")["responseObject"]


@pytest.fixture
def glossary(tmp_path):
    """一个只有 name_glossary 的临时库。⚠️ 绝不碰生产库(conftest 已把地板铺死)。"""
    path = tmp_path / "g.db"

    @contextmanager
    def factory():
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(store._SCHEMA)
        try:
            yield conn
        finally:
            conn.close()

    return factory


# ============================================================
# 解析(纯函数)
# ============================================================
def test_持有人为0一律当缺失():
    """
    ⚠️⚠️ 这是本轮最重要的一条。上游用 **0 表示"取不到"**:
       实测 robinhood 上 31 个 holders=0 里 28 个是错的(有个显示 0 的实际 4302 人);
       solana 的 USDC 也是 FOMO=0 / 真值 524 万(夹具
       fomo_filter_tokens_holders_zero.json 就是那条真实响应)。
    ⚠️ 这与"判空一律 is None、0 是真实值"那条铁律不矛盾:铁律说的是
       "不要用真值判断吞掉真实的 0",而这里是上游拿 0 当哨兵,判据只在这一层做一次。
    """
    assert parse_holders(0) is None
    assert parse_holders("0") is None
    assert parse_holders(-5) is None
    assert parse_holders(1194) == 1194
    assert parse_holders("1194") == 1194          # Blockscout 给的是字符串
    assert parse_holders(None) is None
    assert parse_holders("") is None
    assert parse_holders("abc") is None
    assert parse_holders(True) is None            # bool 是 int 的子类,别被 int() 变成 1


def test_持有人只认纯ASCII数字串():
    """
    ⚠️⚠️ `int()` 自己收得比我们想要的宽得多,而这个槽位是**上游可控的自由文本**,
       "一串数字"恰好是手机号 / QQ 号的形态。下面每一条 `int()` 都吃得下:
    """
    assert parse_holders("+79001234567") is None      # 前导 +:int() 吃,我们不吃
    assert parse_holders("１３８００１３８０００") is None   # 全角数字:int() 吃
    assert parse_holders("1_000") is None             # 下划线分隔:int() 吃
    assert parse_holders(" 1194 ") == 1194            # 首尾空白仍然收
    assert parse_holders("一三八") is None


def test_真实响应里的USDC持有人确实是0而且被当成缺失():
    payload = _fix("fomo_filter_tokens_holders_zero")["responseObject"]
    assert payload[0]["holders"] == 0, "夹具被改过 —— 这条实测事实要重录"
    got = parse_filter_tokens(payload)
    assert got[("solana", USDC_SOL)] == TokenExtra(launchpad=None, holders=None)


def test_发射台的三种形态都落到None():
    """⚠️ 实测存在三种:带名字的对象 / **空对象 {}** / launchpadName 为 null。"""
    assert parse_launchpad_name({"token": {"launchpad": {"launchpadName": "LONG"}}}) == "LONG"
    assert parse_launchpad_name({"token": {"launchpad": {}}}) is None
    assert parse_launchpad_name({"token": {"launchpad": {"launchpadName": None}}}) is None
    assert parse_launchpad_name({"token": {}}) is None
    assert parse_launchpad_name({}) is None
    assert parse_launchpad_name(None) is None
    assert parse_launchpad_name({"token": {"launchpad": "LONG"}}) is None   # 不是对象


def test_跨链混批按响应里的networkId自己分():
    """
    ⚠️⚠️ **不按下标对齐**:上游没承诺过顺序,按下标对齐一旦错位就是静默的张冠李戴。
    ⚠️ 断言逐条写死(链、发射台、持有人),这就是那份真实响应。
    """
    got = parse_filter_tokens(_multi())
    assert got[("robinhood", CUM)].launchpad == "LONG"
    assert got[("robinhood", CUM)].holders == 1222
    assert got[("robinhood", CASHCAT)].launchpad is None       # 实测这条的 launchpad 是 null
    assert got[("robinhood", CASHCAT)].holders == 104456
    assert got[("solana", CATE)].launchpad == "Pump.fun"
    assert got[("bsc", MARSCOIN)].launchpad == "Flap"
    assert got[("base", NVDAC)].launchpad is None
    assert got[("base", NVDAC)].holders == 6703
    assert len(got) == 6


def test_认不出链或地址的项直接丢弃():
    assert parse_filter_tokens([{"token": {"networkId": 99999, "address": "0xabc"}}]) == {}
    assert parse_filter_tokens([{"token": {"networkId": 4663, "address": ""}}]) == {}
    assert parse_filter_tokens("不是数组") == {}
    assert parse_filter_tokens([None, 42]) == {}


def test_Blockscout的三个解析():
    tok = _fix("blockscout_token_cum")
    assert tok["holders_count"] == "1384", "夹具被改过"
    assert parse_holders(tok["holders_count"]) == 1384
    assert parse_creation_tx(_fix("blockscout_address_pons_v1")) == \
        "0x26703d538bd1d7c91f18908922bc20eae3de1b577cec13a467edcd18ab53e289"
    assert parse_tx_to(_fix("blockscout_tx_pons_v1")) == \
        "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
    assert parse_tx_to(_fix("blockscout_tx_pons_v2")) == \
        "0xe33E9E479dF8802cb0866d5d05258bEc4cF62948"
    assert parse_creation_tx({}) is None
    assert parse_creation_tx({"creation_transaction_hash": None}) is None
    assert parse_tx_to({"to": None}) is None
    assert parse_tx_to({"to": {}}) is None


def test_工厂到版本是封闭枚举():
    """
    ⚠️⚠️ **等值比对,不做任何前缀/包含匹配。** 表外的工厂一律 None(调用方退回 "Pons")。
       实测就存在表外的工厂:0xe47e41f4…(未验证合约、链上没有名字,14 个 pons 币里
       有 3 个出自它)—— 我们**不猜**它是哪一版。
    """
    assert pons_version("0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB") == "Pons"
    assert pons_version("0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb") == "Pons"   # 大小写
    assert pons_version("0xe33E9E479dF8802cb0866d5d05258bEc4cF62948") == "Pons V2"
    assert pons_version("0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e") == "Pons V2"
    assert pons_version("0xe47e41f449fB934dd09A2015c9D3658fcBd8B286") is None
    assert pons_version("0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB0000") is None
    assert pons_version(None) is None


# ============================================================
# 假客户端
# ============================================================
class FakeFilter:
    def __init__(self, payload=None, cooldown=0.0):
        self.payload = _multi() if payload is None else payload
        self.cooldown = cooldown
        self.calls = []

    def fetch(self, keys):
        self.calls.append(list(keys))
        if self.cooldown:
            return None, self.cooldown
        return self.payload, 0.0

    def close(self):
        pass


class FakeBS:
    def __init__(self, holders=None, addr=None, tx=None):
        self.holders = holders or {}
        self.addr = addr or {}
        self.tx = tx or {}
        self.calls = []

    def token(self, a):
        self.calls.append(("token", a))
        return self.holders.get(a)

    def address(self, a):
        self.calls.append(("address", a))
        return self.addr.get(a)

    def transaction(self, t):
        self.calls.append(("tx", t))
        return self.tx.get(t)

    def close(self):
        pass


class OpenGate:
    def __init__(self, allow=True):
        self.allow = allow
        self.penalties = []

    def acquire(self):
        return self.allow

    def penalize(self, s):
        self.penalties.append(s)


def _lookup(glossary, fc=None, bs=None, gate=None, **kw):
    return TokenExtraLookup(filter_client=fc or FakeFilter(), blockscout=bs or FakeBS(),
                            conn_factory=glossary, gate=gate or OpenGate(), **kw)


# ============================================================
# 调度 / 缓存 / 降级
# ============================================================
def test_一个tick攒成一个批次跨链混批(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc)
    lk.begin_round()
    lk.lookup([("robinhood", CUM), ("solana", CATE), ("bsc", MARSCOIN), ("base", NVDAC)])
    assert len(fc.calls) == 1, f"应当只发一个请求,实际 {len(fc.calls)}"
    assert fc.calls[0] == [f"{CUM}:4663", f"{CATE}:1399811149",
                           f"{MARSCOIN}:56", f"{NVDAC}:8453"]


def test_请求体的顺序是地址冒号链ID(glossary):
    """⚠️ 反过来写(chainId:address)不会报错,只会回一个空数组 —— 最阴的失败形态。"""
    fc = FakeFilter()
    lk = _lookup(glossary, fc)
    lk.begin_round()
    lk.lookup([("robinhood", CUM)])
    assert fc.calls[0] == ["0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18:4663"]


def test_robinhood的持有人用Blockscout而不是FOMO(glossary):
    """
    ⚠️⚠️ 实测 552 个币对比:FOMO 对已经死掉的小币严重过期,最离谱的 fone 是
       Blockscout=36 / FOMO=2186(差 5972%),而 Blockscout 是对的
       (那 36 个地址余额合计 = 总供应量的 100.0000%)。
    ⚠️ 这条断言故意让两个源给出**不同**的数:退回用 FOMO 的那一刻它当场红。
    """
    fc = FakeFilter()
    bs = FakeBS(holders={CUM: {"holders_count": "1384"}})
    lk = _lookup(glossary, fc, bs)
    lk.begin_round()
    got = lk.lookup([("robinhood", CUM)])
    assert got[("robinhood", CUM)].holders == 1384, "FOMO 那份是 1222,用错源了"


def test_Blockscout拿不到时才退回FOMO(glossary):
    """⚠️ 少数代币 Blockscout 真的 500(实测)。那时有一个偏一点的数好过没有。"""
    fc = FakeFilter()
    bs = FakeBS(holders={})          # token() 一律 None
    lk = _lookup(glossary, fc, bs)
    lk.begin_round()
    got = lk.lookup([("robinhood", CUM)])
    assert got[("robinhood", CUM)].holders == 1222


def test_其余三条链用FOMO的持有人不打Blockscout(glossary):
    fc = FakeFilter()
    bs = FakeBS()
    lk = _lookup(glossary, fc, bs)
    lk.begin_round()
    got = lk.lookup([("solana", CATE), ("bsc", MARSCOIN), ("base", NVDAC)])
    assert bs.calls == [], "非 robinhood 链不该碰 Blockscout"
    assert got[("solana", CATE)].holders == 118265
    assert got[("base", NVDAC)].holders == 6703


def test_发射台为null整条不显示且不猜(glossary):
    """⚠️⚠️ **绝不用域名反推**:CASHCAT 的官网是 cashcat.cc、AI 的是 artificialinu.com,
       都是项目自己的站,与发射台无关 —— 那条路已经被证伪。"""
    fc = FakeFilter()
    lk = _lookup(glossary, fc, FakeBS(holders={CASHCAT: {"holders_count": "104458"}}))
    lk.begin_round()
    got = lk.lookup([("robinhood", CASHCAT)])
    assert got[("robinhood", CASHCAT)].launchpad is None


# ---- Pons V1/V2 ----------------------------------------------------------
def _pons_bs():
    a1, a2 = _fix("blockscout_address_pons_v1"), _fix("blockscout_address_pons_v2")
    return FakeBS(
        holders={PONS_V1: {"holders_count": "130"}, PONS_V2: {"holders_count": "77"}},
        addr={PONS_V1: a1, PONS_V2: a2},
        tx={a1["creation_transaction_hash"]: _fix("blockscout_tx_pons_v1"),
            a2["creation_transaction_hash"]: _fix("blockscout_tx_pons_v2")})


def _pons_payload(addr):
    """把夹具里那条 pons 记录的地址换成想要的 —— 只改地址,别的值原样。"""
    src = [i for i in _multi()
           if (i["token"].get("launchpad") or {}).get("launchpadName") == "pons"]
    assert src, "夹具里没有 pons 记录了"
    item = json.loads(json.dumps(src[0]))
    item["token"]["address"] = addr
    return [item]


def test_pons要分V1和V2(glossary):
    bs = _pons_bs()
    for addr, want in ((PONS_V1, "Pons"), (PONS_V2, "Pons V2")):
        lk = _lookup(glossary, FakeFilter(_pons_payload(addr)), bs)
        lk.begin_round()
        got = lk.lookup([("robinhood", addr)])
        assert got[("robinhood", addr)].launchpad == want, addr


def test_拿不到创建交易就退回Pons绝不猜V2(glossary):
    """⚠️⚠️ 实测 14 个 pons 币里 1 个拿不到创建交易、3 个工厂不在封闭表里
       (0xe47e41f4…,未验证合约、链上没有名字)。**一律退回 V1 的说法,不猜。**"""
    bs = FakeBS(addr={PONS_V1: {}})                    # 没有 creation_transaction_hash
    lk = _lookup(glossary, FakeFilter(_pons_payload(PONS_V1)), bs)
    lk.begin_round()
    got = lk.lookup([("robinhood", PONS_V1)])
    assert got[("robinhood", PONS_V1)].launchpad == "Pons"


def test_工厂不在封闭表里也退回Pons(glossary):
    bs = FakeBS(addr={PONS_V1: {"creation_transaction_hash": "0xdead"}},
                tx={"0xdead": {"to": {"hash": "0xe47e41f449fB934dd09A2015c9D3658fcBd8B286"}}})
    lk = _lookup(glossary, FakeFilter(_pons_payload(PONS_V1)), bs)
    lk.begin_round()
    got = lk.lookup([("robinhood", PONS_V1)])
    assert got[("robinhood", PONS_V1)].launchpad == "Pons"


def test_pons版本永久缓存第二次零请求(glossary):
    bs = _pons_bs()
    fc = FakeFilter(_pons_payload(PONS_V2))
    lk = _lookup(glossary, fc, bs)
    lk.begin_round()
    assert lk.lookup([("robinhood", PONS_V2)])[("robinhood", PONS_V2)].launchpad == "Pons V2"
    n = len(bs.calls)
    lk2 = _lookup(glossary, FakeFilter(_pons_payload(PONS_V2)), bs)   # 换个实例,只有库缓存
    lk2.begin_round()
    assert lk2.lookup([("robinhood", PONS_V2)])[("robinhood", PONS_V2)].launchpad == "Pons V2"
    assert [c for c in bs.calls[n:] if c[0] in ("address", "tx")] == [], "版本判定重复外呼了"


# ---- 缓存 ---------------------------------------------------------------
def test_发射台永久缓存命中时零filterTokens请求(glossary):
    """
    ⚠️⚠️ 这条是缓存策略的**收益本身**:robinhood 的持有人走 Blockscout,
       所以发射台一旦缓存住,这条链的币可以做到 filterTokens 零请求 ——
       而 filterTokens 正是限速最紧的那个端点。
    """
    bs = FakeBS(holders={CUM: {"holders_count": "1384"}})
    fc1 = FakeFilter()
    lk = _lookup(glossary, fc1, bs)
    lk.begin_round()
    lk.lookup([("robinhood", CUM)])
    assert len(fc1.calls) == 1

    fc2 = FakeFilter()
    lk2 = _lookup(glossary, fc2, bs)     # 新实例:内存缓存是空的,只剩库里的发射台
    lk2.begin_round()
    got = lk2.lookup([("robinhood", CUM)])
    assert fc2.calls == [], "发射台已经永久缓存了,不该再打 filterTokens"
    assert got[("robinhood", CUM)].launchpad == "LONG"
    assert got[("robinhood", CUM)].holders == 1384


def test_持有人短TTL过期后重新问(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc, holders_ttl=0.0)     # TTL 0 = 每次都过期
    lk.begin_round()
    lk.lookup([("solana", CATE)])
    lk.begin_round()
    lk.lookup([("solana", CATE)])
    assert len(fc.calls) == 2, "持有人过期了却没重新问"


def test_持有人在TTL内不重复问(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc, holders_ttl=9999.0)
    lk.begin_round()
    lk.lookup([("solana", CATE)])
    lk.begin_round()
    lk.lookup([("solana", CATE)])
    assert len(fc.calls) == 1


def test_同一轮同一个币只进一次批次(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc)
    lk.begin_round()
    # ⚠️ 第二个是 checksum 形态(上游真的会给这种):归一化之后与第一个是同一个币
    lk.lookup([("robinhood", CUM), ("robinhood", "0x" + CUM[2:].upper()), ("robinhood", CUM)])
    assert fc.calls[0] == [f"{CUM}:4663"]


def test_失败和查无的负缓存TTL不是同一个(glossary):
    """
    ⚠️⚠️ 两者混用一个 TTL 是这一类缓存最常见的坑:
       "查过了、上游确实没有发射台" 可以按住 7 天;
       "一次网络抖动" 按住 7 天就是让一行消失一周,而且没有任何日志会说是缓存干的。
    """
    fail = FakeFilter(payload=None)
    fail.fetch = lambda keys: (None, 0.0)          # 请求失败
    lk = _lookup(glossary, fail)
    lk.begin_round()
    lk.lookup([("robinhood", CUM)])
    with glossary() as conn:
        err = conn.execute("select source, expires_at from name_glossary "
                           "where kind='launchpad'").fetchone()

    lk2 = _lookup(glossary, FakeFilter())
    lk2.begin_round()
    lk2.lookup([("robinhood", CASHCAT)])           # 回来了但没有发射台 = 查无
    with glossary() as conn:
        miss = conn.execute("select source, expires_at from name_glossary "
                            "where kind='launchpad' and key = ?",
                            (f"robinhood:{CASHCAT}",)).fetchone()
    assert err["source"] == "error" and miss["source"] == "miss"
    assert miss["expires_at"] - err["expires_at"] > 6 * 86400, "两档 TTL 撞在一起了"


def test_成功的发射台是永久缓存(glossary):
    lk = _lookup(glossary, FakeFilter())
    lk.begin_round()
    lk.lookup([("robinhood", CUM)])
    with glossary() as conn:
        row = conn.execute("select value, expires_at from name_glossary "
                           "where kind='launchpad' and key=?", (f"robinhood:{CUM}",)).fetchone()
    assert row["value"] == "LONG"
    assert row["expires_at"] is None, "发射台是代币出生时定死的属性,应当永久缓存"


# ---- 限速 / 预算 / 降级 ---------------------------------------------------
def test_闸没拿到就整轮不查而且不阻塞(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc, gate=OpenGate(allow=False))
    lk.begin_round()
    assert lk.lookup([("robinhood", CUM)]) == {}
    assert fc.calls == [], "闸没开却把请求发出去了"


def test_429时读retry_after进冷却且本轮整体消失(glossary):
    fc = FakeFilter(cooldown=203.0)
    gate = OpenGate()
    lk = _lookup(glossary, fc, gate=gate)
    lk.begin_round()
    assert lk.lookup([("robinhood", CUM), ("solana", CATE)]) == {}
    assert gate.penalties == [203.0]
    assert len(fc.calls) == 1, "429 之后不该重试"


def test_超过一批的上限时多出来的下一轮再说(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc, keys_per_request=2, max_batches=2)
    lk.begin_round()
    lk.lookup([("robinhood", CUM), ("robinhood", CASHCAT), ("solana", CATE),
               ("bsc", MARSCOIN), ("base", NVDAC)])
    assert len(fc.calls) == 2, "每轮最多两批"
    assert [len(c) for c in fc.calls] == [2, 2]


def test_每tick的Blockscout次数上限(glossary):
    """⚠️ Blockscout 实测每个请求 ~860ms,不封顶就是让 tick 被它拖着走。"""
    addrs = [f"0x{i:040x}" for i in range(1, 8)]
    payload = []
    for a in addrs:
        item = json.loads(json.dumps(_multi()[0]))
        item["token"]["address"] = a
        payload.append(item)
    bs = FakeBS(holders={a: {"holders_count": "999"} for a in addrs})
    lk = _lookup(glossary, FakeFilter(payload), bs, holders_budget=3)
    lk.begin_round()
    lk.lookup([("robinhood", a) for a in addrs])
    assert len([c for c in bs.calls if c[0] == "token"]) == 3


def test_墙钟预算用尽就停(glossary):
    addrs = [f"0x{i:040x}" for i in range(1, 6)]
    payload = []
    for a in addrs:
        item = json.loads(json.dumps(_multi()[0]))
        item["token"]["address"] = a
        payload.append(item)
    bs = FakeBS(holders={a: {"holders_count": "999"} for a in addrs})
    lk = _lookup(glossary, FakeFilter(payload), bs, wall_clock_sec=-1.0)
    lk.begin_round()
    lk.lookup([("robinhood", a) for a in addrs])
    assert [c for c in bs.calls if c[0] == "token"] == []


def test_外呼炸了也只是这两行消失(glossary):
    """⚠️⚠️ **绝不能出现"因为查不到发射台所以整条推送没发出去"**。"""
    class Boom:
        def fetch(self, keys):
            raise RuntimeError("上游炸了")

        def close(self):
            pass

    lk = _lookup(glossary, Boom())
    lk.begin_round()
    assert lk.lookup([("robinhood", CUM)]) == {}


def test_缓存库读写失败当作没缓存(glossary):
    @contextmanager
    def broken():
        raise sqlite3.OperationalError("库锁住了")
        yield

    fc = FakeFilter()
    lk = TokenExtraLookup(filter_client=fc, blockscout=FakeBS(), conn_factory=broken,
                          gate=OpenGate())
    lk.begin_round()
    got = lk.lookup([("solana", CATE)])
    assert got[("solana", CATE)].launchpad == "Pump.fun"


def test_认不出的链一个请求都不发(glossary):
    fc = FakeFilter()
    lk = _lookup(glossary, fc)
    lk.begin_round()
    assert lk.lookup([("hyperliquid", "0xabc"), ("", CUM), ("solana", "")]) == {}
    assert fc.calls == []


# ---- cached:只读缓存 ------------------------------------------------------
def test_cached只读缓存绝不发请求(glossary):
    fc = FakeFilter()
    bs = FakeBS(holders={CUM: {"holders_count": "1384"}})
    lk = _lookup(glossary, fc, bs)
    lk.begin_round()
    lk.lookup([("robinhood", CUM)])
    n_fc, n_bs = len(fc.calls), len(bs.calls)

    got = lk.cached("robinhood", CUM)
    assert got.launchpad == "LONG" and got.holders == 1384
    assert len(fc.calls) == n_fc and len(bs.calls) == n_bs, "只读缓存却发了请求"


def test_cached在缓存为空时给空值而不是抛(glossary):
    lk = _lookup(glossary)
    assert lk.cached("robinhood", CUM) == TokenExtra(None, None)
    assert lk.cached("robinhood", "") == TokenExtra(None, None)
    assert lk.cached(None, None) == TokenExtra(None, None)


def test_cached路径上的pons不为分版本发请求(glossary):
    """⚠️ 转入推送这条路径 **一个请求都不许发**;分不出版本就退回 "Pons"。"""
    bs = _pons_bs()
    lk = _lookup(glossary, FakeFilter(_pons_payload(PONS_V2)), bs)
    lk.begin_round()
    lk.lookup([("robinhood", PONS_V2)])
    n = len(bs.calls)
    assert lk.cached("robinhood", PONS_V2).launchpad == "Pons V2"
    assert len(bs.calls) == n


# ============================================================
# HTTP 客户端:那个头
# ============================================================
class _Resp:
    def __init__(self, body, status=200, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self._body)


def _wire(monkeypatch, resp):
    box = {}

    class S:
        def post(self, url, json=None, headers=None):
            box.update(url=url, json=json, headers=headers or {})
            return resp

        def get(self, url):
            box.update(url=url)
            return resp

    monkeypatch.setattr(tokeninfo.FilterTokensClient, "fetch", _REAL_FETCH)
    monkeypatch.setattr(tokeninfo.BlockscoutClient, "_get", _REAL_BS_GET)
    monkeypatch.setattr(tokeninfo._CurlClient, "_session", lambda self: S())
    return box


def test_filterTokens必须带X_Supported_Chains头(monkeypatch):
    """
    ⚠️⚠️ 真网络实测(2026-09-03,同一个 robinhood 地址):
        带头   → HTTP 200,responseObject 有 1 条
        不带头 → HTTP 200,responseObject = []     ← 成功状态码 + 空数组
    少了它,EVM 链**永远**查不到,而且没有任何错误码、没有任何日志会说它错了。
    ⚠️ 断言写死字面量,不从被测模块 import 那个常量。
    """
    box = _wire(monkeypatch, _Resp(json.dumps({"responseObject": []})))
    tokeninfo.FilterTokensClient().fetch([f"{CUM}:4663"])
    headers = {k.lower(): v for k, v in box["headers"].items()}
    assert headers.get("x-supported-chains") == "1,56,143,4663,8453,1399811149", headers
    assert headers.get("Content-Type".lower()) == "application/json"


def test_filterTokens打的是public路径(monkeypatch):
    """⚠️ 非 /public/ 的同名路径匿名一律 431(要 Bearer)。别碰。"""
    box = _wire(monkeypatch, _Resp(json.dumps({"responseObject": []})))
    tokeninfo.FilterTokensClient().fetch([f"{CUM}:4663"])
    assert box["url"] == "https://prod-api.fomo.family/public/proxy/filterTokens"


def test_filterTokens的429返回冷却秒数(monkeypatch):
    _wire(monkeypatch, _Resp("{}", status=429, headers={"Retry-After": "203"}))
    payload, cooldown = tokeninfo.FilterTokensClient().fetch([f"{CUM}:4663"])
    assert payload is None and cooldown == 203.0


def test_filterTokens的429没给头时用默认冷却(monkeypatch):
    _wire(monkeypatch, _Resp("{}", status=429))
    payload, cooldown = tokeninfo.FilterTokensClient().fetch([f"{CUM}:4663"])
    assert payload is None and cooldown >= 200.0


def test_filterTokens非2xx降级为空而不是抛(monkeypatch):
    _wire(monkeypatch, _Resp("nope", status=500))
    assert tokeninfo.FilterTokensClient().fetch([f"{CUM}:4663"]) == (None, 0.0)


def test_Blockscout只打robinhood那个实例(monkeypatch):
    """
    ⚠️⚠️ base 的那个 Blockscout 实例 holders_count 是**错的**
       (Basecat:blockscout 2234 / basescan 23399 / FOMO 23423)——
       别拿它当 base 的兜底。这里钉住 base URL 只有 robinhood 那一个。
    """
    box = _wire(monkeypatch, _Resp("{}"))
    tokeninfo.BlockscoutClient().token(CUM)
    assert box["url"] == f"https://robinhoodchain.blockscout.com/api/v2/tokens/{CUM}"


# ============================================================
# 限速闸(进程级)
# ============================================================
def test_限速闸把两次请求隔开():
    now = {"t": 0.0}
    slept = []
    gate = tokeninfo._RateGate(interval=10.0, max_wait=30.0,
                               clock=lambda: now["t"],
                               sleep=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))
    assert gate.acquire() is True
    assert slept == []
    assert gate.acquire() is True
    assert slept == [10.0], "第二次没有等满间隔"


def test_限速闸的默认间隔就是10秒():
    """
    ⚠️⚠️ 这条钉的是**模块默认值**,不是构造参数:上面几条都显式传了 interval=10.0,
       把 _MIN_INTERVAL_SEC 改成 0 它们照样绿。而实测是"无间隔连打第 10 次就 429、
       冷却 203 秒" —— 间隔本身就是这个功能能不能长期活着的全部条件。
    ⚠️ 断言写死 10.0,不 import 那个常量。
    """
    now = {"t": 0.0}
    slept = []
    gate = tokeninfo._RateGate(
        max_wait=30.0, clock=lambda: now["t"],
        sleep=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))
    assert gate.acquire() is True
    assert gate.acquire() is True
    assert slept == [10.0], f"默认间隔不是 10 秒:{slept}"


def test_要等太久就不查而不是阻塞tick():
    """⚠️ 这个等待直接加在 tick 的墙钟上 —— 等不到就这一轮不查,那几行消失。"""
    now = {"t": 0.0}
    gate = tokeninfo._RateGate(interval=10.0, max_wait=2.0, clock=lambda: now["t"],
                               sleep=lambda s: None)
    assert gate.acquire() is True
    assert gate.acquire() is False


def test_429冷却期间闸一直关着():
    now = {"t": 0.0}
    gate = tokeninfo._RateGate(interval=10.0, max_wait=2.0, clock=lambda: now["t"],
                               sleep=lambda s: None)
    gate.penalize(203.0)
    assert gate.acquire() is False
    now["t"] = 204.0
    assert gate.acquire() is True


def test_冷却秒数有上限():
    """⚠️ 一个畸形的 retry-after 不该把这个功能按住一整天。"""
    now = {"t": 0.0}
    gate = tokeninfo._RateGate(interval=10.0, max_wait=2.0, clock=lambda: now["t"],
                               sleep=lambda s: None)
    gate.penalize(999999.0)
    now["t"] = 901.0
    assert gate.acquire() is True


def test_限速闸是进程级单例():
    """
    ⚠️⚠️ poller 与 pumpfun 各持一个 TokenExtraLookup。闸做成实例级 = 把 10 秒间隔
       悄悄变成 5 秒,而限流是**按 IP** 算的。这条钉住"两个实例共用同一把闸"。
    """
    a = TokenExtraLookup(filter_client=FakeFilter(), blockscout=FakeBS())
    b = TokenExtraLookup(filter_client=FakeFilter(), blockscout=FakeBS())
    assert a._gate is b._gate is tokeninfo._GATE


def test_retry_after解析():
    assert tokeninfo.retry_after_seconds({"Retry-After": "203"}) == 203.0
    assert tokeninfo.retry_after_seconds({"retry-after": "203"}) == 203.0
    assert tokeninfo.retry_after_seconds({"retry-after": "垃圾"}) >= 200.0
    assert tokeninfo.retry_after_seconds({}) >= 200.0
    assert tokeninfo.retry_after_seconds(None) >= 200.0


# ============================================================
# ⚠️⚠️ 预算 / TTL 的**默认值**(H2)
# ============================================================
# 上面那些用例全都**显式传参**(keys_per_request=2 / holders_budget=3 / …),
# 于是把 tokeninfo 里那 7 个模块常量同时改坏(200→100000、2→100000、2.0→600.0、
# 12→100000、2→100000、8.0→100000.0、90.0→86400.0)跑全量,**一条都不红**。
# 而这 7 个数字恰恰是"这个功能会不会把 tick 拖垮 / 会不会把用户 IP 打进 429 冷却"
# 的全部条件 —— 显式传参的用例证明的是"参数生效",不是"默认值是对的"。
# 下面每条各钉一个**默认值**:构造时不传那个参数,让它走默认路径。
# ⚠️ 断言全部写死字面量,不 import 任何常量。
class _FakeTime:
    """
    替掉 tokeninfo 模块里的 `time`(它只用得到 time() 与 monotonic())。

    ⚠️ monotonic 每被调一次就往前走 mono_step —— _timed 一次调两下,
       于是"一次外呼恰好花掉 mono_step 秒",墙钟预算就能脱网精确到小数点。
    """

    def __init__(self, now: float = 1_000_000.0, mono_step: float = 0.0) -> None:
        self.now = now
        self.mono_step = mono_step
        self._mono = 0.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        v = self._mono
        self._mono += self.mono_step
        return v


def _evm(i: int) -> str:
    return f"0x{i:040x}"


def _item(addr: str, launchpad: str | None = "LONG", holders=None, net: int = 4663) -> dict:
    """一条 filterTokens 响应项。⚠️ 字面量,不从被测模块借结构。"""
    return {"holders": holders,
            "token": {"address": addr, "networkId": net, "symbol": "X",
                      "launchpad": None if launchpad is None else {"launchpadName": launchpad}}}


def test_默认一批就是200个地址(glossary):
    """
    ⚠️⚠️ 钉 **MAX_KEYS_PER_REQUEST 的默认值**:上面「超过一批的上限」那条显式传了
       keys_per_request=2,把默认值改成 100000 它照样绿。
       实测 300 个一次能全返回,200 是留了余量的那个数 —— 调大它等于把
       "超过 200 要切第二批"这条分支在真实负载下彻底走不到。
    ⚠️ 不传 keys_per_request,让它走默认;断言写死 200 / 1。
    """
    fc = FakeFilter([])
    lk = TokenExtraLookup(filter_client=fc, blockscout=FakeBS(),
                          conn_factory=glossary, gate=OpenGate())
    lk.begin_round()
    lk.lookup([("bsc", _evm(i)) for i in range(201)])
    assert [len(c) for c in fc.calls] == [200, 1], f"默认批大小不是 200:{[len(c) for c in fc.calls]}"


def test_默认一轮最多两批(glossary):
    """
    ⚠️⚠️ 钉 **MAX_BATCHES_PER_ROUND 的默认值**。每一批都要各过一次 10 秒的限速闸,
       批数放开 = 一个 tick 里连打 N 次 filterTokens ——
       实测无间隔连打第 10 次就 429、冷却 203 秒。
    ⚠️ 不传 max_batches;601 个地址按默认 200 一批本该切 4 批,只许发前 2 批。
    """
    fc = FakeFilter([])
    lk = TokenExtraLookup(filter_client=fc, blockscout=FakeBS(),
                          conn_factory=glossary, gate=OpenGate())
    lk.begin_round()
    lk.lookup([("bsc", _evm(i)) for i in range(601)])
    assert [len(c) for c in fc.calls] == [200, 200], \
        f"默认批数上限不是 2:{[len(c) for c in fc.calls]}"


def test_闸的默认等待上限就是2秒():
    """
    ⚠️⚠️ 钉 **_GATE_MAX_WAIT_SEC 的默认值**:上面所有闸的用例都显式传了 max_wait=2.0
       或 30.0,把默认值改成 600.0 它们照样绿 —— 而这个数字是**直接加在 tick 墙钟上**的,
       poller 每 15 秒一个 tick,等 600 秒等于整个轮询停摆。
    ⚠️ 两个方向各钉一次:要等 2.0 秒(正好等于上限)→ 等;要等 2.5 秒 → 不等。
       于是默认值被夹在 [2.0, 2.5) 里,写死字面量,不 import 常量。
    """
    now = {"t": 0.0}
    slept = []
    g2 = tokeninfo._RateGate(
        interval=2.0, clock=lambda: now["t"],
        sleep=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))
    assert g2.acquire() is True
    assert g2.acquire() is True, "要等 2.0 秒就已经放弃 —— 默认上限比 2 秒还小"
    assert slept == [2.0]

    now2 = {"t": 0.0}
    slept2 = []
    g25 = tokeninfo._RateGate(
        interval=2.5, clock=lambda: now2["t"],
        sleep=lambda s: (slept2.append(s), now2.__setitem__("t", now2["t"] + s)))
    assert g25.acquire() is True
    assert g25.acquire() is False, "要等 2.5 秒还在等 —— 默认上限被放大了"
    assert slept2 == []


def test_默认每轮最多12次Blockscout持有人(glossary):
    """
    ⚠️⚠️ 钉 **BLOCKSCOUT_HOLDERS_PER_ROUND 的默认值**:上面那条显式传了 holders_budget=3。
       Blockscout 实测每个请求 ~860ms,不封顶就是让 tick 被外部接口拖着走。
    ⚠️ 13 个 robinhood 币,只许打 12 次;断言写死 12。
    """
    addrs = [_evm(i) for i in range(1, 14)]
    bs = FakeBS(holders={a: {"holders_count": "999"} for a in addrs})
    lk = TokenExtraLookup(filter_client=FakeFilter([_item(a) for a in addrs]),
                          blockscout=bs, conn_factory=glossary, gate=OpenGate())
    lk.begin_round()
    lk.lookup([("robinhood", a) for a in addrs])
    assert len([c for c in bs.calls if c[0] == "token"]) == 12


def test_默认每轮最多判2个pons版本(glossary):
    """
    ⚠️⚠️ 钉 **BLOCKSCOUT_PONS_PER_ROUND 的默认值**。判一个版本要**两个** Blockscout
       请求,而且结果是永久缓存(一个币这辈子只判一次)—— 所以每轮只给 2 个额度,
       慢慢把库里的 pons 币判完,不跟 tick 抢时间。放开它 = 上线那天一次性把
       库里上千个 pons 币全判一遍。
    ⚠️ 3 个 pons 币,只许判 2 个;第 3 个退回 "Pons"(不猜 V2)。断言写死 2。
    """
    addrs = [_evm(i) for i in range(1, 4)]
    bs = FakeBS(addr={a: {"creation_transaction_hash": f"0xtx{i}"}
                      for i, a in enumerate(addrs)},
                tx={f"0xtx{i}": {"to": {"hash": "0xe33e9e479df8802cb0866d5d05258bec4cf62948"}}
                    for i in range(3)})
    lk = TokenExtraLookup(filter_client=FakeFilter([_item(a, "pons") for a in addrs]),
                          blockscout=bs, conn_factory=glossary, gate=OpenGate())
    lk.begin_round()
    got = lk.lookup([("robinhood", a) for a in addrs])
    assert len([c for c in bs.calls if c[0] == "address"]) == 2
    names = sorted(got[("robinhood", a)].launchpad for a in addrs)
    assert names == ["Pons", "Pons V2", "Pons V2"], names


def test_默认每轮墙钟预算就是8秒(glossary, monkeypatch):
    """
    ⚠️⚠️ 钉 **ROUND_WALL_CLOCK_SEC 的默认值**:上面那条显式传了 wall_clock_sec=-1.0
       (只证明"传 -1 就一个都不查"),把默认值改成 100000.0 它照样绿。
       墙钟闸才是真正要防的那件事 —— 次数没超但每个请求都超时,tick 一样被拖垮。
    ⚠️ 用假 time:一次外呼恰好花掉 mono_step 秒(_timed 一次调两下 monotonic)。
       第一次外呼是 filterTokens 本身,之后才是 Blockscout。
       · 每次 2.0 秒 → 2(FOMO)+2+2+2=8 → 第 4 次 Blockscout 被拦 ⇒ 3 次,证明预算 ≤ 8
       · 每次 3.5 秒 → 3.5(FOMO)+3.5+3.5=10.5 → 第 3 次被拦 ⇒ 2 次,证明预算 > 7
       两条夹出 (7, 8]。断言写死次数,不 import 常量。
    """
    def _count(step: float, base: int) -> int:
        # ⚠️ 两次用**不同的地址**:发射台成功是永久缓存,同一批地址第二次跑连
        #    filterTokens 都不会发,那一次外呼的开销就凭空少了(踩过)。
        addrs = [_evm(base + i) for i in range(6)]
        monkeypatch.setattr(tokeninfo, "time", _FakeTime(mono_step=step))
        bs = FakeBS(holders={a: {"holders_count": "999"} for a in addrs})
        lk = TokenExtraLookup(filter_client=FakeFilter([_item(a) for a in addrs]),
                              blockscout=bs, conn_factory=glossary, gate=OpenGate())
        lk.begin_round()
        lk.lookup([("robinhood", a) for a in addrs])
        return len([c for c in bs.calls if c[0] == "token"])

    assert _count(2.0, 100) == 3, "默认墙钟预算大于 8 秒"
    assert _count(3.5, 200) == 2, "默认墙钟预算不到 7 秒"


def test_持有人的默认内存TTL就是90秒(glossary, monkeypatch):
    """
    ⚠️⚠️ 钉 **HOLDERS_TTL_SEC 的默认值**:上面那两条 TTL 用例都显式传了 holders_ttl,
       把默认值改成 86400 它们照样绿 —— 而那意味着一整天不再问一次持有人数,
       推送里印的是一天前的数字(一句读起来完全正常的假话)。
    ⚠️ 89 秒仍算命中、90 秒就得重问,于是默认值被夹在 (89, 90] 里。
       只数 filterTokens 的次数(持有人没过期时整个键都不进批次)。
    """
    clock = _FakeTime()
    monkeypatch.setattr(tokeninfo, "time", clock)
    addr = "So11111111111111111111111111111111111111112"
    fc = FakeFilter([_item(addr, "Pump.fun", 4321, net=1399811149)])
    lk = TokenExtraLookup(filter_client=fc, blockscout=FakeBS(),
                          conn_factory=glossary, gate=OpenGate())
    lk.begin_round()
    assert lk.lookup([("solana", addr)])[("solana", addr)].holders == 4321
    assert len(fc.calls) == 1

    clock.now += 89.0
    lk.begin_round()
    lk.lookup([("solana", addr)])
    assert len(fc.calls) == 1, "89 秒就把持有人缓存丢了 —— 默认 TTL 比 90 秒短"

    clock.now += 1.0
    lk.begin_round()
    lk.lookup([("solana", addr)])
    assert len(fc.calls) == 2, "满 90 秒还在用旧的持有人数 —— 默认 TTL 被放大了"
