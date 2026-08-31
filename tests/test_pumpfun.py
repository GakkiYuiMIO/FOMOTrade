"""
pump.fun 指定用户买卖监控的测试。

⚠️ 全部走**离线夹具**(tests/fixtures/pump_*.json,是 2026-08-31 真实响应存下来的,
   只删行不改值),一条用例都不打网络 —— pump.fun 的两个端点都带 X-RateLimit-*,
   每跑一次测试就打一次接口是在拿用户的出口 IP 冒险。
⚠️ 断言里的门槛/上限/键名一律**写死字面量**,绝不从被测模块 import 过来 ——
   那种断言等价于 `x == x`,把常量改坏了它照样绿(本项目踩过)。
⚠️ 时间一律相对 time.time() 构造,绝不写死绝对时刻 —— 夹具里的成交会随日历变老,
   写死的话这套测试过几个月自己就红了(与真实缺陷无关的红比不红更糟)。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。ruff 的 N802 只认 ASCII 小写。
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src import pumpfun as pf
from src import store
from src.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"

# 三个真实地址(用户指定、已链上校验)。⚠️ 写死字面量,不从被测模块取。
HEX_SVM = "21rgbFW6sujQovCw3qt6R2EdE97Yzzvk8sSc37Bb72Cm"
HEX_EVM = "0xbebbad0b95ed88e6c56c8d79daa82257e97c3f44"
HEX_UID = "046999d1-1609-4654-b8ed-06827aa50ae9"
OTHER_SVM = "DieowDJ137xDRyCDn3YZYfAe4qzeGmdQJvryyqdWhtuc"   # 名单**之外**的人

MINT_SOL = "G2ZYvnesQzucoSy3VP7xap1PpMb3btiVXYNWCPKepump"    # Solana(chainId 1399811149)
MINT_BSC = "0x205812cdbed920aff76c6580abd681a46d11efc7"      # BNB Chain(chainId 56)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---- 载荷构造器 ----------------------------------------------------------
def position_payload(*rows) -> dict:
    """
    造一个 /user-portfolio 响应。

    ⚠️ `summary.positionCount` 刻意给一个**与实际行数不符**的脏值:
       实测同一次枚举三页分别报 236/250/229,它就是脏的。
       被测代码但凡拿它当总数或终止条件,这里的用例就会红。
    """
    return {"positions": list(rows), "summary": {"positionCount": 9999}}


def pos_row(mint=MINT_SOL, chain=1399811149, held=1.0, pnl=0.0, symbol="PUNCHMA",
            updated="2026-08-31T00:59:07.793Z") -> dict:
    return {
        "coinMint": mint, "chainId": chain, "amountHeld": held,
        "realizedPnlUsd": pnl, "updatedAt": updated, "isExited": held == 0,
        "coin": {"symbol": symbol, "name": symbol},
    }


def trade_payload(*, addr=HEX_SVM, upper=True, age_sec=60.0, side="buy",
                  usd="1234.5", price="0.0000285", tx="TX_1", slot="0001") -> dict:
    """
    造一个 trades/batch 响应。

    ⚠️ `upper` 切换字段大小写:POST /v1/…/trades/batch 给的是 `amountUSD` / `priceUSD`,
       GET /v2/…/trades 给的是 `amountUsd` / `priceUsd`(2026-08-31 同一个 mint
       两个端点各打一次、亲眼比对过)。只兼容一种的话另一种会静默取到 None。
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - age_sec))
    row = {"tx": tx, "slotIndexId": slot, "timestamp": ts, "type": side,
           "userAddress": addr, "isBondingCurve": True}
    row["amountUSD" if upper else "amountUsd"] = usd
    row["priceUSD" if upper else "priceUsd"] = price
    return {addr: [row]}


class FakeNotifier:
    """记下发出去的每一条消息。send 的返回值可控 —— 要测「发失败」那条路径。"""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[str] = []
        self.ok = ok

    def send(self, text: str, **kw) -> bool:
        self.sent.append(text)
        return self.ok


class FakeClient:
    """
    离线的 pump 客户端。

    ⚠️ 刻意存**原始载荷**并走被测模块自己的 parse_* —— 这样夹具是真的流过解析器的。
       直接喂已解析好的 dataclass 会让解析层完全测不到(字段大小写那个坑就在解析层)。
    ⚠️ 契约与真 client 一致:fetch_* 不抛异常、失败返回 None。
       `raise_on_portfolio` 是**故意违约**,用来验证 PumpWatcher 顶得住。
    """

    def __init__(self, portfolios=None, trades=None, *, raise_on_portfolio=None) -> None:
        self.portfolios = portfolios or {}      # wallet -> 载荷 或 None(拉取失败)
        self.trades = trades or {}              # mint   -> 载荷 或 None
        self.raise_on_portfolio = raise_on_portfolio
        self.portfolio_calls: list[str] = []
        self.trade_calls: list[tuple[str, tuple[str, ...]]] = []

    def fetch_portfolio(self, wallet):
        self.portfolio_calls.append(wallet)
        if self.raise_on_portfolio is not None:
            raise self.raise_on_portfolio
        payload = self.portfolios.get(wallet, "__missing__")
        if payload == "__missing__" or payload is None:
            return None
        return pf.parse_positions(payload)

    def fetch_trades(self, mint, addresses):
        self.trade_calls.append((mint, tuple(addresses)))
        payload = self.trades.get(mint)
        if payload is None:
            return None
        return pf.parse_trades(payload)

    def close(self) -> None:
        pass


@pytest.fixture
def db(tmp_path):
    """
    真实文件库 —— PumpWatcher 内部走 store.get_conn(),内存库够不着。

    ⚠️ 与 test_binance_alpha 同样的写法:**自己开一个 MonkeyPatch**,不复用函数级的
       monkeypatch 夹具 —— 用例里任何一句 monkeypatch.undo() 都会把 DB_PATH
       还原成 data/fomo.db,那一刻起测试打的就是用户**正在跑的生产库**。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "pump.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


@pytest.fixture
def cfg(monkeypatch):
    """
    可控的配置。⚠️ 门槛/窗口一律在这里显式给死,不吃 .env 的默认值 ——
    默认值改了不该让这套用例的语义跟着漂。
    """
    def _apply(**kw):
        env = {"FOMO_PUMP_MIN_USD": "100", "FOMO_PUMP_MAX_MINTS": "8",
               "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200"}
        env.update({k: str(v) for k, v in kw.items()})
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
    _apply()
    yield _apply
    get_settings.cache_clear()


def add_user(username="hexiecs", uid=HEX_UID, svm=HEX_SVM, evm=HEX_EVM):
    with store.get_conn() as conn, store.tx(conn):
        store.add_pump_user(conn, uid, username, svm, evm)


def seeded_user(**kw):
    """加人并直接标成已播种 —— 大多数用例要测的是播种之后的行为。"""
    add_user(**kw)
    with store.get_conn() as conn, store.tx(conn):
        store.mark_pump_seeded(conn, kw.get("uid", HEX_UID))


def snapshot_keys(uid=HEX_UID):
    with store.get_conn() as conn:
        return set(store.pump_positions(conn, uid))


def ledger_rows():
    with store.get_conn() as conn:
        return conn.execute("SELECT * FROM pump_pushed_trades").fetchall()


# ============================================================
# 解析:字段大小写、脏 positionCount、缺字段
# ============================================================
class Test解析:
    def test_两个端点的字段大小写都要认(self):
        """
        ⚠️⚠️ 这是本轮最容易静默出错的一条:POST batch 给 amountUSD/priceUSD,
           GET v2 给 amountUsd/priceUsd。只兼容一种,另一种就是「金额永远没了」——
           不报错、不告警。两份夹具都是真实响应,数字写死在断言里。
        """
        batch = pf.parse_trades(_load("pump_trades_batch.json"))
        t_upper = batch[OTHER_SVM][0]
        assert t_upper.amount_usd == pytest.approx(0.0000028483994)
        assert t_upper.price_usd == pytest.approx(2.852012996042046e-06)
        assert t_upper.side == "sell"
        assert t_upper.tx == (
            "3AJvuDLZTiDtGwcsj3jNUsFp13MfbN4hcn7eVuDiYzcZNBAdG22DWg8htADqKMCCmUWbdHUURoFeVfKyNSyUXaZA"
        )

        v2 = _load("pump_trades_v2.json")["trades"][0]
        t_lower = pf.parse_trades({v2["userAddress"]: [v2]})[v2["userAddress"]][0]
        assert t_lower.amount_usd == pytest.approx(0.0000028483994)
        assert t_lower.price_usd == pytest.approx(2.852012996042046e-06)

    def test_金额恰好是0的成交要解析成0而不是缺失(self):
        """
        0 是真实值(粉尘成交),真值判断会把它读成「没有这个字段」。

        ⚠️⚠️ 这里必须传**真正的 0**,不能传字符串 "0" —— `"0"` 的真值是 True,
           拿它去测「用 `in` 判存在还是用真值判断」根本测不出区别(本项目踩过:
           把 `if n in row` 改成 `if row.get(n)` 之后这条用例照样全绿)。
        """
        got = pf.parse_trades(trade_payload(usd=0, price=0))[HEX_SVM][0]
        assert got.amount_usd == 0.0
        assert got.price_usd == 0.0

    def test_缺type字段的方向必须是未知而不是默认成买入(self):
        """
        ⚠️⚠️ 猜错方向比不说方向糟得多:默认成 "buy" 的话,一笔卖出会被说成买入。
           渲染器那头「没见过的方向 → 中性措辞」有用例钉着,**解析器这头**
           一直没有 —— 把 `side.lower() if side else None` 改成 `else "buy"`
           整套用例照样全绿。
        """
        missing = trade_payload()
        missing[HEX_SVM][0].pop("type")
        assert pf.parse_trades(missing)[HEX_SVM][0].side is None
        # 上游给空串也是"没说方向",同样不许猜
        assert pf.parse_trades(trade_payload(side=""))[HEX_SVM][0].side is None

    def test_持仓解析完全不看summary里的positionCount(self):
        """
        ⚠️ positionCount 是脏值(实测同一次枚举三页报 236/250/229)。
           这里给一个荒唐的 0,行数照样按数组本身算。
        """
        payload = {"positions": [pos_row(), pos_row(mint=MINT_BSC, chain=56)],
                   "summary": {"positionCount": 0}}
        assert len(pf.parse_positions(payload)) == 2
        payload["summary"]["positionCount"] = 9999
        assert len(pf.parse_positions(payload)) == 2

    def test_真实夹具里的清仓行要被正面解析出来(self):
        """filter=ALL 会带回 isExited/amountHeld=0 的行 —— 清仓是正面可观测的。"""
        rows = pf.parse_positions(_load("pump_portfolio.json"))
        held = {p.coin_mint: p.amount_held for p in rows}
        assert 0.0 in held.values()                       # 确实有清仓行
        assert all(v is not None for v in held.values())  # 0 不能被读成 None

    def test_拉取失败与真的没有持仓语义不同(self):
        assert pf.parse_positions("不是字典") is None
        assert pf.parse_positions({"positions": "不是数组"}) is None
        assert pf.parse_positions({"positions": []}) == []

    def test_缺tx或缺时刻的成交整条丢弃(self):
        payload = {HEX_SVM: [
            {"slotIndexId": "1", "timestamp": "2026-08-31T01:00:00Z", "type": "buy",
             "userAddress": HEX_SVM, "amountUSD": "500"},                       # 缺 tx
            {"tx": "T2", "type": "buy", "userAddress": HEX_SVM, "amountUSD": "500"},  # 缺时刻
        ]}
        assert pf.parse_trades(payload) == {HEX_SVM: []}

    def test_档案反查取到两个canonical钱包(self):
        p = pf.parse_profile(_load("pump_user_profile.json"))
        assert p.user_id == HEX_UID
        assert p.username == "hexiecs"
        assert p.svm_wallet == HEX_SVM
        assert p.evm_wallet == HEX_EVM
        # 固定用 SVM 去查持仓(两个视图返回逐行相同的数据,跟着接口回显的那个走)
        assert p.portfolio_wallet == HEX_SVM


class Test链映射:
    def test_pump的chainId映射到本仓库的链标识(self):
        assert pf.Position("1399811149", "x", 1, 0, None).network_id == "solana"
        assert pf.Position("56", "x", 1, 0, None).network_id == "bsc"
        assert pf.Position("8453", "x", 1, 0, None).network_id == "base"
        assert pf.Position("4663", "x", 1, 0, None).network_id == "robinhood"
        assert pf.Position("999", "x", 1, 0, None).network_id == "hyperliquid"

    def test_models不认识的链只给链名不给链接(self):
        """⚠️ 拼一个 GMGN 不支持的链只会 404,错的链接比没有链接更糟。"""
        p = pf.Position("42161", "0x" + "a" * 40, 1, 0, None)
        assert p.network_id is None
        assert p.chain_display == "Arbitrum"

    def test_完全不认识的chainId两行都消失(self):
        p = pf.Position("777777", "0x" + "a" * 40, 1, 0, None)
        assert p.network_id is None
        assert p.chain_display is None


# ============================================================
# 巡检主干
# ============================================================
class Test冷启动播种:
    def test_第一轮一条都不推只记快照(self, db, cfg):
        """
        ⚠️⚠️ 这是整个功能的生死线:不播种的话一个有 1905 个持仓的人
           会在第一轮把 Telegram 打爆,用户当场静音,功能第一天就死。
        """
        add_user()
        payload = position_payload(pos_row(), pos_row(mint=MINT_BSC, chain=56, symbol="QQQB"))
        client = FakeClient(portfolios={HEX_SVM: payload},
                            trades={MINT_SOL: trade_payload(), MINT_BSC: trade_payload()})
        tg = FakeNotifier()

        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []
        # 播种轮**一个逐笔请求都不该打** —— 那是纯浪费限流预算
        assert client.trade_calls == []
        assert snapshot_keys() == {("1399811149", MINT_SOL), ("56", MINT_BSC)}

    def test_播种之后的变动才推(self, db, cfg):
        add_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=1.0))},
                            trades={MINT_SOL: trade_payload()})
        tg = FakeNotifier()
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 0                       # 第一轮:播种

        client.portfolios[HEX_SVM] = position_payload(pos_row(held=3.0))
        assert w.run_once() == 1                       # 第二轮:持仓变了 → 推真实成交
        assert len(tg.sent) == 1
        assert "买入" in tg.sent[0]

    def test_新加的人单独播种不受别人已播种影响(self, db, cfg):
        """
        ⚠️ 播种位必须**每人一位**。做成全局一位的话,名单跑了三天之后再加一个新人,
           他的几百个持仓会在第一轮全部当成"刚变动"推出去。
        """
        seeded_user()                                   # 老人:已播种
        add_user(username="brc20niubi", uid="uid-2", svm="BQ4Kzz", evm="0x" + "b" * 40)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=1.0)),
                        "BQ4Kzz": position_payload(pos_row(mint=MINT_BSC, chain=56, held=7.0))},
            trades={MINT_BSC: trade_payload(addr="BQ4Kzz")},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []
        assert snapshot_keys("uid-2") == {("56", MINT_BSC)}


class Test已推台账:
    def test_同一笔成交不会被推第二次(self, db, cfg):
        """
        ⚠️⚠️ portfolio 每变动一次就会把该 mint 的成交整段拉回来。
           没有台账就是"每变动一次重推一遍"。
        """
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(tx="TX_A")})
        tg = FakeNotifier()
        w = pf.PumpWatcher(tg, client)
        assert w.run_once() == 1

        # 持仓又变了一次,但逐笔接口返回的还是同一笔成交
        client.portfolios[HEX_SVM] = position_payload(pos_row(held=5.0))
        assert w.run_once() == 0
        assert len(tg.sent) == 1

    def test_同一个tx里的两笔拆单都要推(self, db, cfg):
        """⚠️ 只按 tx 去重会把拆单的后几笔静默吃掉,所以主键带 slotIndexId。"""
        seeded_user()
        payload = trade_payload(tx="TX_SPLIT", slot="0001")
        payload[HEX_SVM].append({**payload[HEX_SVM][0], "slotIndexId": "0002"})
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: payload})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 2
        assert {r["slot_index_id"] for r in ledger_rows()} == {"0001", "0002"}

    def test_同一轮里重复出现的同一笔只推一次(self, db, cfg):
        """
        ⚠️⚠️ 跨轮由台账挡着,**轮内不挡**:done 只在轮首读一次,
           推送边发边写台账却不回填 done,而候选列表本身也不去重。
           上游把同一行给两遍(同 tx 同 slotIndexId)就是两条一模一样的消息。
        """
        seeded_user()
        payload = trade_payload(tx="TX_DUP", slot="0007")
        payload[HEX_SVM].append(dict(payload[HEX_SVM][0]))       # 逐字段相同的第二行
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: payload})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1
        assert len(ledger_rows()) == 1


class Test只推被盯的人:
    def test_名单外地址的成交一条都不推(self, db, cfg):
        """
        ⚠️⚠️ batch 响应是按地址分组的。任何不在名单里的键必须整段丢弃 ——
           推出去就是把陌生人的交易当成"我盯的人在买",信息本身是错的。
        """
        seeded_user()
        payload = trade_payload(addr=HEX_SVM, tx="MINE")
        payload.update(trade_payload(addr=OTHER_SVM, tx="NOT_MINE", usd="99999"))
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: payload})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1
        assert "99,999" not in tg.sent[0]
        assert {r["tx"] for r in ledger_rows()} == {"MINE"}

    def test_软删除的人不再巡检(self, db, cfg):
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.remove_pump_user(conn, HEX_UID)
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=9.0))},
                            trades={MINT_SOL: trade_payload()})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert client.portfolio_calls == []             # 连持仓请求都不该打

    def test_逐笔请求必须同时带SVM与EVM两个钱包(self, db, cfg):
        """
        ⚠️⚠️ 实测:1000XCryptoD 在 BSC 的买入,传 SVM 地址得到 `[]`,
           传 EVM 地址(0x1160…)才拿到那笔 $1.46 的 buy。
           只传一个 = 整条 EVM 侧的成交永久静默丢失。
        """
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload()})
        pf.PumpWatcher(FakeNotifier(), client).run_once()
        assert client.trade_calls
        assert set(client.trade_calls[0][1]) == {HEX_SVM, HEX_EVM}

    def test_EVM钱包名下的成交照样能归属到这个人(self, db, cfg):
        seeded_user()
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(mint=MINT_BSC, chain=56,
                                                          held=2.0, symbol="QQQB"))},
            trades={MINT_BSC: trade_payload(addr=HEX_EVM, tx="EVM_TX")},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "hexiecs" in tg.sent[0]
        assert {r["tx"] for r in ledger_rows()} == {"EVM_TX"}

    def test_两个人共用一个钱包时两个人都要拿到这笔成交(self, db, cfg):
        """
        ⚠️⚠️ 「重复地址只认先加进来的那个」是条静默丢数据的路:后加的那个人在
           归属阶段拿到空列表 → _push 返回「本来就没有该推的」→ **快照照常前移** →
           这笔变动对他永久消失,下一轮也不会重来。
           名单里两个人共用一个钱包,那就是同一个钱包的两个身份,
           两个人都是用户自己加进来的,两条都该发出去。
        """
        seeded_user()
        seeded_user(username="影子", uid="uid-2", svm=HEX_SVM, evm=None)
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(tx="SHARED")})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 2
        assert len(tg.sent) == 2
        assert {r["user_id"] for r in ledger_rows()} == {HEX_UID, "uid-2"}
        # 两条消息各自说的是各自的名字,绝不能把一条重复两遍
        assert any("hexiecs" in t for t in tg.sent)
        assert any("影子" in t for t in tg.sent)


class Test门槛与窗口:
    def test_门槛设成0时金额恰好为0的成交照样推(self, db, cfg):
        """
        ⚠️⚠️ 「拿不到金额」与「金额是 0」是两件事,判空必须 is None。
           用真值判断的话,把门槛调到 0(= 明确表示"什么都想看")之后,
           粉尘成交会被当成"缺字段"静默丢掉 —— 而用户要的恰恰是它们。
        ⚠️⚠️ 金额传的必须是**真正的 0**,不是字符串 "0":`"0"` 的真值是 True,
           传字符串的话 `if n in row` 与 `if row.get(n)` 表现一模一样,
           这条用例就测不到它自称要测的那个区别(本项目踩过)。
        """
        cfg(FOMO_PUMP_MIN_USD="0")
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(usd=0, price=0)})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "$0.00" in tg.sent[0]

    def test_低于金额门槛的不推(self, db, cfg):
        cfg(FOMO_PUMP_MIN_USD="100")
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(usd="99.99")})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 0
        assert tg.sent == []

    def test_恰好等于门槛的要推(self, db, cfg):
        """⚠️ 门槛是「低于此不推」,等于门槛的必须过 —— 差一个等号就是静默漏推。"""
        cfg(FOMO_PUMP_MIN_USD="100")
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(usd="100")})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1

    def test_门槛改大之后原来能过的就不推了(self, db, cfg):
        """门槛必须真的取自配置,写死一个常量的话这条会红。"""
        cfg(FOMO_PUMP_MIN_USD="5000")
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(usd="1234.5")})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_取不到金额的成交不推并留痕(self, db, cfg):
        """拿不到金额就证明不了它过线。⚠️ 绝不当成 0 也绝不放行。"""
        seeded_user()
        payload = trade_payload()
        payload[HEX_SVM][0].pop("amountUSD")
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: payload})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_太旧的成交不推(self, db, cfg):
        """
        ⚠️ 逐笔接口返回的是这个人在这个币上的**一段历史**,不是"刚刚那笔"。
           没有新鲜窗口的话,一个持有半年的币今天动一下,半年前的成交会被整段推出来。
        """
        cfg(FOMO_PUMP_TRADE_MAX_AGE_SEC="600")
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(age_sec=3600)})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0


class Test故障不扩散:
    def test_客户端抛异常也不会逃到调度器(self, db, cfg):
        """
        ⚠️⚠️ run_once 是这个功能与 APScheduler 之间的唯一接触面。
           异常逃出去,轻则一堆 job 崩溃栈,重则被上层某个 except 当成"该停机了"。
        """
        seeded_user()
        client = FakeClient(raise_on_portfolio=RuntimeError("pump 挂了"))
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0

    def test_持仓拉取失败时快照一动不动(self, db, cfg):
        """动了就等于把这段时间的变动静默吃掉。"""
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, HEX_UID,
                                        [("1399811149", MINT_SOL, 1.0, 0.0, "T0")])
        client = FakeClient(portfolios={HEX_SVM: None})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        with store.get_conn() as conn:
            row = store.pump_positions(conn, HEX_UID)[("1399811149", MINT_SOL)]
        assert row["amount_held"] == 1.0

    def test_播种轮拉取失败绝不能标成已播种(self, db, cfg):
        """
        ⚠️⚠️ 把「拉取失败」当成「他一个持仓都没有」的后果是最贵的:
           播种位被置上、快照却是空的,于是**下一轮**他的全部持仓都成了"新出现的",
           一次把 Telegram 打爆。失败必须原样返回、播种位一动不动。
        """
        add_user()
        client = FakeClient(portfolios={HEX_SVM: None})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        with store.get_conn() as conn:
            assert store.list_pump_users(conn)[0]["seeded"] == 0

    def test_逐笔拉取失败时这个mint的快照不前移(self, db, cfg):
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: None})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert snapshot_keys() == set()                 # 一行都没写下去 → 下一轮重来

    def test_推送失败绝不标记已推且下一轮会重来(self, db, cfg):
        """
        ⚠️⚠️ 先记台账再推 = 一次 TG 400 或网络抖动就把这笔成交永久判死,
           而且没有任何日志能让人发现。
        """
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload(tx="TX_LOST")})
        bad = FakeNotifier(ok=False)
        assert pf.PumpWatcher(bad, client).run_once() == 0
        assert len(bad.sent) == 1                       # 确实试着发了
        assert ledger_rows() == []                      # 但没记台账
        assert snapshot_keys() == set()                 # 快照也没前移

        good = FakeNotifier(ok=True)
        assert pf.PumpWatcher(good, client).run_once() == 1     # 下一轮补上
        assert {r["tx"] for r in ledger_rows()} == {"TX_LOST"}


class Test请求预算:
    def test_变动的mint超过上限时只处理前几个(self, db, cfg):
        cfg(FOMO_PUMP_MAX_MINTS="2")
        seeded_user()
        mints = [f"MintFixture{i}pump" for i in range(5)]
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(
                *[pos_row(mint=m, held=1.0 + i) for i, m in enumerate(mints)])},
            trades={m: trade_payload(tx=f"T{i}") for i, m in enumerate(mints)},
        )
        pf.PumpWatcher(FakeNotifier(), client).run_once()
        assert len(client.trade_calls) == 2

    def test_单个币上待推太多时本轮截断且快照不前移(self, db, cfg):
        """
        ⚠️ 截断只截**本轮**:快照不前移 → 剩下的下一轮接着推,已推的被台账挡住。
           截断后照样前移快照的话,超出上限的那些就是永久丢失。
        """
        seeded_user()
        payload = trade_payload(tx="T0", slot="0")
        payload[HEX_SVM] = [{**payload[HEX_SVM][0], "tx": f"T{i}", "slotIndexId": str(i)}
                            for i in range(25)]
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: payload})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 20     # 写死字面量,不从模块取
        assert snapshot_keys() == set()
        assert len(ledger_rows()) == 20

        assert pf.PumpWatcher(tg, client).run_once() == 5      # 下一轮把剩下的推完
        assert len(ledger_rows()) == 25

    def test_单轮上限是全局的两个mint加起来也不许超(self, db, cfg):
        """
        ⚠️⚠️ 上限的语义是「一轮最多发几条消息(**所有人合计**)」,它存在的唯一理由
           是防刷屏 —— 而刷屏不认 mint 边界。只在"处理每个 mint 之前"判一次的话,
           最后一个 mint 自己还能再发满一轮:19 + 20 = 39 条,
           而日志还在说「本轮先推最早的 20 笔」。
        """
        seeded_user()
        mint_b = "MintBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbpump"   # 排序在 MINT_SOL 之后
        first = trade_payload(tx="A0", slot="0")
        first[HEX_SVM] = [{**first[HEX_SVM][0], "tx": f"A{i}", "slotIndexId": str(i)}
                          for i in range(19)]
        second = trade_payload(tx="B0", slot="0")
        second[HEX_SVM] = [{**second[HEX_SVM][0], "tx": f"B{i}", "slotIndexId": str(i)}
                           for i in range(40)]
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(mint=MINT_SOL, held=2.0),
                                                  pos_row(mint=mint_b, held=3.0))},
            trades={MINT_SOL: first, mint_b: second},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 20     # 写死字面量,不从模块取
        assert len(tg.sent) == 20
        assert len(ledger_rows()) == 20

    def test_同一个mint上两个人分着用同一份预算(self, db, cfg):
        """
        ⚠️⚠️ 单个 mint 内部按"人"再循环一次,每个人各自还能推满一轮 ——
           所以"一个 mint 最多 20 条"也是错的,盯 N 个人就是 20×N 条。
           上限必须是**整轮一份预算,谁先用谁扣掉**。

        ⚠️ 两个人各 15 笔(而不是各 25 笔)是刻意的:各 25 笔的话第一个人一次就把
           预算用光,后面那个人被任何一道"见底就跳过"的闸挡住都能让总数停在 20 ——
           于是"预算有没有真的扣减"根本测不出来(实测:把传给第二个人的额度写成
           整轮预算,各 25 笔那版用例照样全绿)。15 + 15 才逼出**扣减**本身:
           第一个人拿 20 剩 5,第二个人只能拿到那 5 条。
        """
        seeded_user()
        seeded_user(username="brc20niubi", uid="uid-2", svm="BQ4Kzz", evm="0x" + "b" * 40)
        mine = trade_payload(addr=HEX_SVM, tx="M0", slot="0")
        mine[HEX_SVM] = [{**mine[HEX_SVM][0], "tx": f"M{i}", "slotIndexId": str(i)}
                         for i in range(15)]
        his = trade_payload(addr="BQ4Kzz", tx="H0", slot="0")
        his["BQ4Kzz"] = [{**his["BQ4Kzz"][0], "tx": f"H{i}", "slotIndexId": str(i)}
                         for i in range(15)]
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=2.0)),
                        "BQ4Kzz": position_payload(pos_row(held=7.0))},
            trades={MINT_SOL: {**mine, **his}},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 20     # 15 + 5,不是 15 + 15
        assert len(tg.sent) == 20
        # 先来的推干净了 → 快照前移;后面那个被截断 → 快照不前移,剩下 10 条下一轮补
        assert snapshot_keys(HEX_UID) == {("1399811149", MINT_SOL)}
        assert snapshot_keys("uid-2") == set()

    def test_预算用光之后剩下的mint连逐笔请求都不打(self, db, cfg):
        """
        ⚠️ 上限不只是"少发几条消息":超限的 mint 本轮**整个不处理**,
           那一个逐笔请求也省下来了(变动的 mint 数 = 本轮的额外请求数)。
        """
        seeded_user()
        mint_b = "MintBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbpump"   # 排序在 MINT_SOL 之后
        first = trade_payload(tx="A0", slot="0")
        first[HEX_SVM] = [{**first[HEX_SVM][0], "tx": f"A{i}", "slotIndexId": str(i)}
                          for i in range(25)]
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(mint=MINT_SOL, held=2.0),
                                                  pos_row(mint=mint_b, held=3.0))},
            trades={MINT_SOL: first, mint_b: trade_payload(tx="B0")},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 20
        assert [m for m, _addrs in client.trade_calls] == [MINT_SOL]

    def test_没有变动就一个逐笔请求都不打(self, db, cfg):
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, HEX_UID,
                                        [("1399811149", MINT_SOL, 1.0, 0.0, "T0")])
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=1.0, pnl=0.0))},
                            trades={MINT_SOL: trade_payload()})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.trade_calls == []

    def test_清仓之后重新买回来也算变动(self, db, cfg):
        """
        ⚠️⚠️ 上一轮 amountHeld=0(已清仓)是**真实值不是缺失**。
           用真值判断("上一轮没量就当没变")会让「清仓后重新建仓」这件事
           永久静默丢失 —— 而那恰恰是最该看见的一笔。
        """
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, HEX_UID,
                                        [("1399811149", MINT_SOL, 0.0, 0.0, "T0")])
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=5.0))},
                            trades={MINT_SOL: trade_payload(side="buy")})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "买入" in tg.sent[0]

    def test_清仓到0也算变动(self, db, cfg):
        """⚠️ 0 是真实值。真值判断会让"从 100 变成 0"读成"没变"。"""
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, HEX_UID,
                                        [("1399811149", MINT_SOL, 100.0, 0.0, "T0")])
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=0.0))},
                            trades={MINT_SOL: trade_payload(side="sell")})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "卖出" in tg.sent[0]


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "59"}

    def json(self):
        return self._payload


class _FakeSession:
    """够 PumpClient._json 用的最小会话。记下每次请求的 URL 与参数。"""

    def __init__(self, resp):
        self.resp = resp
        self.calls: list[tuple] = []

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self.resp

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self.resp

    def close(self):
        pass


class TestHTTP层:
    """
    ⚠️ 不打网络:直接把线程本地的 session 换成桩。
       (碰私有属性是刻意的 —— 这一层要验的恰恰是"HTTP 响应怎么处理",
        再往上包一层抽象只会把要测的东西藏起来。)
    """

    def _client(self, resp):
        c = pf.PumpClient(proxy="")
        c._tl.session = _FakeSession(resp)
        return c, c._tl.session

    def test_逐笔成交返回201也要收下(self, cfg):
        """
        ⚠️⚠️ POST /v1/coins/{mint}/trades/batch 实测返回的是 **201 Created**。
           只认 200 的话整条逐笔链路永远拿不到数据,而且**不报错**——
           推送会安静地一条都不发,看上去就像"这个人最近没交易"。
        """
        payload = {HEX_SVM: [{"tx": "T1", "timestamp": "2026-08-31T01:00:00.000Z",
                              "type": "buy", "userAddress": HEX_SVM,
                              "amountUSD": "500", "priceUSD": "1"}]}
        c, _ = self._client(_FakeResp(201, payload))
        got = c.fetch_trades(MINT_SOL, [HEX_SVM])
        assert got is not None
        assert [t.tx for t in got[HEX_SVM]] == ["T1"]

    def test_非2xx一律当成拉取失败(self, cfg):
        c, _ = self._client(_FakeResp(500, {"positions": []}))
        assert c.fetch_portfolio(HEX_SVM) is None

    def test_持仓请求必须带filter_ALL与sortBy_RECENCY(self, cfg):
        """
        ⚠️⚠️ 两个参数都是这个设计的地基,少一个就不成立:
           · filter=ALL 才带得回 isExited/amountHeld=0 的行 → 清仓才是正面可观测的;
           · sortBy=RECENCY 才保证最近动过的排在最前 → 只拉 page 0 就够。
        """
        c, sess = self._client(_FakeResp(200, {"positions": []}))
        c.fetch_portfolio(HEX_SVM)
        params = sess.calls[0][2]["params"]
        assert params["filter"] == "ALL"
        assert params["sortBy"] == "RECENCY"
        assert params["page"] == 0

    def test_逐笔请求体里两个钱包一个都不能少(self, cfg):
        """
        ⚠️⚠️ EVM 链上的成交**只挂在 canonical_evm_wallet 名下**(实测 1000XCryptoD
           在 BSC 的 QQQB:传 SVM 得到 `[]`,传 EVM 才拿到那笔 $1.46 的 buy)。
           少传一个 = 整条 EVM 侧的成交永久静默丢失,不报错、不告警。
        ⚠️⚠️ 这条必须打在**真 client 的请求体**上:上层用例喂的是假 client,
           压根走不到 fetch_trades 里那句 `json=`,把它改成 `list(addresses)[:1]`
           照样全绿(本项目踩过)。
        """
        c, sess = self._client(_FakeResp(201, {}))
        c.fetch_trades(MINT_SOL, [HEX_SVM, HEX_EVM])
        method, _url, kw = sess.calls[0]
        assert method == "post"
        assert kw["json"]["userAddresses"] == [HEX_SVM, HEX_EVM]

    def test_用户键里的路径分隔符不许拼出别的端点(self, cfg):
        """
        ⚠️⚠️ key 直接来自 Telegram 消息。原样拼进路径的话,
           `/pump add ../../following-positions/alerts` 会拼出
           `…/users/../../following-positions/alerts` —— curl 把 `..` 正规化掉之后,
           打的正是那个**需要登录**的端点(实测 401)。
           白名单挡在发请求之前:非法的键**一个字节都不发出去**。
        """
        c, sess = self._client(_FakeResp(200, _load("pump_user_profile.json")))
        for bad in ("../../following-positions/alerts", "hexiecs/../x", "a b",
                    "hexiecs?x=1", "", "  "):
            assert c.resolve_user(bad) is None
        assert sess.calls == []

    def test_合法的名字与两种钱包地址照常反查(self, cfg):
        """白名单不能把正常的键也挡掉 —— 挡掉了这个功能就没法用了。"""
        c, sess = self._client(_FakeResp(200, _load("pump_user_profile.json")))
        for good in ("hexiecs", "1000XCryptoD", "brc20_niubi", HEX_SVM, HEX_EVM):
            assert c.resolve_user(good) is not None
        assert [url.rsplit("/", 1)[-1] for _m, url, _kw in sess.calls] == [
            "hexiecs", "1000XCryptoD", "brc20_niubi", HEX_SVM, HEX_EVM]

    def test_传输层异常也当成拉取失败而不是往上抛(self, cfg):
        """
        ⚠️⚠️ 真实世界的失败绝大多数是 TimeoutError / ConnectionResetError 这类
           **传输层异常**,不是 JSON 解析错。把 `except Exception` 收窄成
           `except ValueError` 之后,一个人的网络抖动会顺着
           _candidates → _check 一路炸上去 —— 本轮别人的成交本来是推得出来的。
           (原来一条驱动这条路的用例都没有,收窄之后 56 条 pump 用例全绿。)
        """
        class _BoomSession:
            def __init__(self, exc):
                self.exc = exc

            def get(self, url, **kw):
                raise self.exc

            def post(self, url, **kw):
                raise self.exc

            def close(self):
                pass

        for exc in (TimeoutError("超时"), ConnectionResetError("对端断开"),
                    OSError("网络不可达")):
            c = pf.PumpClient(proxy="")
            # ⚠️ 每次都要重新塞:_json 失败后会 close() 把线程本地的 session 置空,
            #    不重塞的话下一句就去建**真的** curl 会话、真的打网络了。
            c._tl.session = _BoomSession(exc)
            assert c.fetch_portfolio(HEX_SVM) is None
            c._tl.session = _BoomSession(exc)
            assert c.fetch_trades(MINT_SOL, [HEX_SVM]) is None
            c._tl.session = _BoomSession(exc)
            assert c.resolve_user("hexiecs") is None

    def test_只打公开端点绝不碰要登录的那些(self, cfg):
        """
        ⚠️ 实测 /following-positions/alerts 需要登录(401)。本模块只用免鉴权端点,
           且**不带任何凭据**:请求里不该出现 Authorization / Cookie / token。
        """
        c, sess = self._client(_FakeResp(200, {"positions": []}))
        c.fetch_portfolio(HEX_SVM)
        c.fetch_trades(MINT_SOL, [HEX_SVM])
        for _method, url, kw in sess.calls:
            assert "alerts" not in url
            assert "headers" not in kw          # 一个自定义头都不加,更不会有凭据
            assert "cookies" not in kw and "auth" not in kw


# ============================================================
# 渲染
# ============================================================
class Test推送文案:
    def _render(self, **kw):
        from src.formatter import render_pump_trade

        base = dict(username="hexiecs", side="buy", token_symbol="PUNCHMA",
                    coin_mint=MINT_SOL, amount_usd=1234.5, price_usd=0.0000285,
                    traded_at="2026-08-31T01:59:02.000Z", network_id="solana",
                    chain_display="Solana", tx="SIG_ABC", now=1787193542.0)
        base.update(kw)
        return render_pump_trade(**base)

    def test_买入卖出都如实说方向(self):
        assert "买入" in self._render(side="buy")
        assert "卖出" in self._render(side="sell")

    def test_没见过的方向退化成中性措辞而不是猜成买入(self):
        """⚠️ 猜错方向比不说方向糟得多。"""
        out = self._render(side="???")
        assert "买入" not in out and "卖出" not in out

    def test_绝不替用户断言意图(self):
        """
        ⚠️⚠️ 措辞铁律:只摆可证的事实。本项目已经因为「他们一分钱没花」
           这种断言被审查打回过一次。
        """
        out = self._render()
        for banned in ("建仓", "跑路", "该跟", "抄底", "看好", "准备", "可能"):
            assert banned not in out

    def test_金额单价时刻签名都在(self):
        out = self._render()
        assert "$1,234.50" in out
        assert "$0.0000285" in out
        assert "2026-08-31 01:59 UTC" in out
        assert "SIG_ABC" in out

    def test_粉尘金额按有效数字渲染绝不塌成两位小数的0(self):
        """
        ⚠️⚠️ 真实夹具里就有 amountUSD=0.0000028483994 这一笔卖出。
           金额行按两位小数 quantize 会把它渲染成「💰 金额 $0.00」——
           一行**看起来像缺失值的真实值**,与「缺失整行消失、绝不打 0」的观感
           直接打架。单价行早就有有效数字处理,金额行也得有。
        """
        assert "金额 $0.000002848" in self._render(amount_usd=0.0000028483994)
        assert "金额 $0.004999" in self._render(amount_usd=0.004999)
        # ⚠️ 边界:quantize 是 banker's rounding,恰好 0.005 也会塌成 $0.00,
        #    所以它同样得走有效数字那条路(写死 `< 0.005` 的阈值会正好在这里漏一个)
        assert "金额 $0.005" in self._render(amount_usd=0.005)
        # 够得着两位小数的金额照旧,别把正常金额也改成有效数字
        assert "金额 $1,234.50" in self._render(amount_usd=1234.5)
        assert "金额 $0.01" in self._render(amount_usd=0.0051)

    def test_金额真的是0时照常显示0(self):
        """⚠️ 0 是真实值不是缺失,它必须显示出来 —— 别被上一条带偏成"小额一律有效数字"。"""
        assert "金额 $0.00" in self._render(amount_usd=0)

    def test_CA独占最后一行且完整(self):
        out = self._render()
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_链接覆盖不到的链只丢链接行其余照推(self):
        """⚠️ 拼一个必然 404 的链接比没有链接更糟。"""
        out = self._render(network_id=None, chain_display="Arbitrum")
        assert "Arbitrum" in out
        assert "gmgn.ai" not in out and "fomo.family" not in out
        assert "$1,234.50" in out                       # 主干照推

    def test_缺失字段整行消失绝不打占位符(self):
        out = self._render(amount_usd=None, price_usd=None, traded_at=None,
                           network_id=None, chain_display=None)
        assert "None" not in out and "N/A" not in out and "--" not in out

    def test_预算被顶破时CA仍然完整且仍在最后一行(self, monkeypatch):
        """
        ⚠️⚠️ 出口不变式必须**在砍行的时候**也成立,而不只是在正常长度下成立。
           把 CA 当成普通一行追加(而不是走锚点)在正常长度下看起来一模一样,
           一旦要砍行,CA 就是第一个被砍掉的 —— 而它是整条消息里唯一
           在中国网络下 100% 可用的操作(tap-to-copy)。
           这里把预算调到只够一行,逼出砍行路径。
        """
        from src import formatter

        monkeypatch.setattr(formatter, "TRANSFER_MSG_BUDGET", 60)
        out = self._render()
        assert len(out) <= 60
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_陌生人可控的超长文本撑不破预算(self):
        """用户名与 ticker 长度不受任何天然约束,一个超长值不该把消息挤没。"""
        out = self._render(username="超长" * 5000, token_symbol="X" * 5000)
        assert len(out) <= 4000 - 400
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_HTML标签必须闭合(self):
        """⚠️ Telegram 只认 HTML 子集,残缺实体或未闭合标签 = 整条消息 400。"""
        for out in (self._render(), self._render(username="<b>坏昵称</b>", token_symbol="a&b")):
            assert out.count("<b>") == out.count("</b>")
            assert out.count("<code>") == out.count("</code>")
            assert "<b>坏昵称" not in out                # 原始标签必须被转义掉


# ============================================================
# 命令层
# ============================================================
class Test命令:
    def _bot(self, pump_client=None):
        from src.bot import CommandBot

        return CommandBot(client=None, notifier=FakeNotifier(), pump_client=pump_client)

    class _FakeResolver:
        def __init__(self, profile=None):
            self.profile = profile

        def resolve_user(self, key):
            return self.profile

    def test_不带参数给名单与用法(self, db, cfg):
        out = self._bot()._cmd_pump("")
        assert "pump.fun" in out and "/pump add" in out

    def test_add落库并回执说明第一轮不推(self, db, cfg):
        prof = pf.parse_profile(_load("pump_user_profile.json"))
        out = self._bot(self._FakeResolver(prof))._cmd_pump("add hexiecs")
        assert "已加入" in out and "一条都不推" in out
        with store.get_conn() as conn:
            rows = store.list_pump_users(conn)
        assert [r["user_id"] for r in rows] == [HEX_UID]
        assert rows[0]["svm_wallet"] == HEX_SVM
        assert rows[0]["evm_wallet"] == HEX_EVM          # ⚠️ EVM 侧成交全靠它
        assert rows[0]["seeded"] == 0

    def test_add查不到的人给出可操作的回执(self, db, cfg):
        out = self._bot(self._FakeResolver(None))._cmd_pump("add 查无此人")
        assert "找不到" in out

    def test_del软删除且再add回来会重新播种(self, db, cfg):
        prof = pf.parse_profile(_load("pump_user_profile.json"))
        bot = self._bot(self._FakeResolver(prof))
        bot._cmd_pump("add hexiecs")
        with store.get_conn() as conn, store.tx(conn):
            store.mark_pump_seeded(conn, HEX_UID)
        assert "已移出" in bot._cmd_pump("del hexiecs")
        with store.get_conn() as conn:
            assert store.list_pump_users(conn) == []
            assert len(store.list_pump_users(conn, active_only=False)) == 1
        bot._cmd_pump("add hexiecs")
        with store.get_conn() as conn:
            # ⚠️ 复活必须重新播种:人被移出去这段时间他照样在交易,
            #    不归零的话复活那一轮会把这期间的全部变动一次推出来
            assert store.list_pump_users(conn)[0]["seeded"] == 0

    def test_pump名单与FOMO名单是两张互不相干的表(self, db, cfg):
        """
        ⚠️⚠️ 这是"绝不给 watch_users 加 platform 列"那条决定的守卫:
           pump 的人一旦混进 watch_users,poller 会拿 Solana 钱包去问 fomo.family,
           404 之后把这个人标成「账号已不存在」。
        """
        prof = pf.parse_profile(_load("pump_user_profile.json"))
        self._bot(self._FakeResolver(prof))._cmd_pump("add hexiecs")
        with store.get_conn() as conn:
            assert store.list_active_users(conn) == []   # FOMO 名单必须还是空的

    def test_命令分发认得pump(self, db, cfg):
        bot = self._bot()
        assert "未知命令" not in bot._dispatch("/pump", "")

    def test_未知子命令给用法而不是静默吞掉(self, db, cfg):
        assert "用法" in self._bot()._cmd_pump("addd hexiecs")
