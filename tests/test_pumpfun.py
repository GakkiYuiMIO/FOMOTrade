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


def callout_row(*, cid="c-1", mint=MINT_SOL, thesis="my bad fellas i got hypnotized",
                age_sec=60.0, mcap=16379, multiple=0.256, likes=3, views=844,
                has_liked=None, has_reposted=None) -> dict:
    """
    造一条 /callout/list 里的观点。

    ⚠️⚠️ `createdAt` 一律给 **epoch 毫秒**(× 1000),这就是上游的真实形态。
       被测代码当成秒的话时间差 1000 倍,窗口判定会整个翻车。
    ⚠️ hasLiked / hasReposted 缺省是 **None**(= JSON 的 null),匿名请求实测就是这样。
    """
    return {"calloutId": cid, "userId": HEX_SVM, "coinMint": mint,
            "thesis": thesis,
            "createdAt": int((time.time() - age_sec) * 1000),
            "marketCap": mcap, "calloutPrice": 1.58e-07, "calloutPriceUsd": 1.637e-05,
            "multiple": multiple, "maxMultiplier": 1.2289,
            "maxMultiplierAt": "2026-08-30T21:25:07.466Z",
            "likes": likes, "viewCount": views, "replyCount": 0, "commentCount": 0,
            "repostCount": 0, "quoteCount": 0, "updateCount": 0, "updates": [],
            "mediaUrl": None, "quotedCalloutId": None, "quotedCallout": None,
            "user_uuid": "", "hasLiked": has_liked, "hasReposted": has_reposted}


def callout_payload(*rows, token="") -> dict:
    """造一个 /callout/list 响应。⚠️ 空结果在上游是 200 + 空数组,不是 404。"""
    return {"callouts": list(rows), "nextPageToken": token}


def capture_warnings():
    """收集 loguru 的 WARNING 及以上;返回 (列表, 关闭函数)。与 test_poller 同一套。"""
    from loguru import logger as _lg

    out: list[str] = []
    hid = _lg.add(lambda m: out.append(str(m)), level="WARNING")
    return out, lambda: _lg.remove(hid)


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

    def __init__(self, portfolios=None, trades=None, coins=None, *,
                 raise_on_portfolio=None, callouts=None, raise_on_callout=None) -> None:
        self.portfolios = portfolios or {}      # wallet -> 载荷 或 None(拉取失败)
        self.trades = trades or {}              # mint   -> 载荷 或 None
        # mint -> /coins-v3 载荷 或 None(拉取失败)。⚠️ 缺省是**空表**:没显式给载荷的
        #    用例里市值就是拿不到,而那些用例照样该绿 —— 市值绝不是推送的前置条件。
        self.coins = coins or {}
        # user_id -> /callout/list 载荷 或 None(拉取失败)。
        # ⚠️ 空结果要写成 {"callouts": [], …} 而**不是** None:上游那两件事
        #    (200 空数组 vs 请求失败)语义完全不同,夹具里也必须分得开。
        self.callouts = callouts or {}
        self.raise_on_portfolio = raise_on_portfolio
        self.raise_on_callout = raise_on_callout
        self.portfolio_calls: list[str] = []
        self.trade_calls: list[tuple[str, tuple[str, ...]]] = []
        self.coin_calls: list[str] = []
        self.callout_calls: list[tuple[str, int]] = []

    def fetch_callouts(self, user_key, limit=30):
        self.callout_calls.append((user_key, limit))
        if self.raise_on_callout is not None:
            raise self.raise_on_callout
        payload = self.callouts.get(user_key, "__missing__")
        if payload == "__missing__" or payload is None:
            return None
        return pf.parse_callouts(payload)

    def fetch_coin(self, mint):
        self.coin_calls.append(mint)
        payload = self.coins.get(mint)
        if payload is None:
            return None
        return pf.parse_coin(payload)

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
               "FOMO_PUMP_TRADE_MAX_AGE_SEC": "7200",
               "FOMO_PUMP_CALLOUT_MAX_AGE_SEC": "7200"}
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
# 市值:/coins-v3 的解析
# ============================================================
class Test市值解析:
    """
    ⚠️ 三份夹具都是 2026-08-31 从 /coins-v3/{mint} 真实抓下来的,
       只删了 image_uri / description 这类展示字段,**数值一位没动**。
    ⚠️ 断言全部写死字面量,绝不从 pf 里取 —— 否则取错字段时断言跟着一起错。
    """

    SOL = "pump_coin_sol.json"            # PUNCHMA / Solana,还在 bonding curve 上
    SOL_EXITED = "pump_coin_sol_exited.json"   # GTA / Solana,已经跑起来的币
    EVM = "pump_coin_evm.json"            # KISS / Robinhood(eip155:4663)
    BSC = "pump_coin_bsc.json"            # QQQB / BNB Chain(eip155:56)

    def test_市值必须取usd_market_cap而不是SOL计价的market_cap(self):
        """
        ⚠️⚠️⚠️ 这条是这次改动里最贵的一个坑。同一个响应里:
           · Solana 的 `market_cap` 是 **SOL 计价**(PUNCHMA 28.0355,
             恰好等于 bonding curve 自己算出来的 SOL 市值);
           · `usd_market_cap` 才是美元(2906.09)。
           取错字段 = 把一个 $2,906 的币说成 $28,差两个数量级,而且一眼看不出来。
        ⚠️ 两个 Solana 样本都验:两者的比值(103.66 / 104.01)就是当时的 SOL 价 ——
           一个样本可能是巧合,两个样本对上同一个汇率就不是了。
        """
        sol = pf.parse_coin(_load(self.SOL))
        assert sol.market_cap_usd == 2906.090666119672
        assert sol.market_cap_usd != 28.035521634857712      # ← 这就是 SOL 计价那个数

        gta = pf.parse_coin(_load(self.SOL_EXITED))
        assert gta.market_cap_usd == 276774.6685814697
        assert gta.market_cap_usd != 2661.0081513200334      # ← 同上

    def test_EVM链上两个字段相等所以只用EVM验测不出上一条(self):
        """
        ⚠️ 留这条不是为了凑覆盖率,是为了钉住"**只用 EVM 验一遍就上线**"这个失败模式:
           EVM 上 market_cap == usd_market_cap,取错字段照样全绿,
           而 Solana 的每一条推送都会把市值说小两个数量级。
        """
        raw = _load(self.EVM)
        assert raw["market_cap"] == raw["usd_market_cap"] == 445526.7119903613
        assert pf.parse_coin(raw).market_cap_usd == 445526.7119903613
        raw_bsc = _load(self.BSC)
        assert raw_bsc["market_cap"] == raw_bsc["usd_market_cap"] == 573660.6510680942

    def test_两条链字段集不同但要取的那两个都在(self):
        """⚠️ Solana 多 bonding_curve / complete / market_cap_quote,EVM 多
           canonical_pool_liquidity_usd —— 解析必须对两种形态都成立。"""
        sol, evm = _load(self.SOL), _load(self.EVM)
        assert "bonding_curve" in sol and "bonding_curve" not in evm
        assert "canonical_pool_liquidity_usd" in evm and "canonical_pool_liquidity_usd" not in sol
        for raw in (sol, evm, _load(self.BSC), _load(self.SOL_EXITED)):
            s = pf.parse_coin(raw)
            assert s.market_cap_usd is not None
            assert s.ath_market_cap_usd is not None

    def test_历史最高市值也取得到(self):
        assert pf.parse_coin(_load(self.SOL)).ath_market_cap_usd == 18489.197465326295
        assert pf.parse_coin(_load(self.EVM)).ath_market_cap_usd == 2116892.897778326

    def test_缺字段或结构不对时是None而不是0(self):
        """⚠️ 0 会被渲染成「💎 市值 $0.00」—— 一条一眼假的信息比没有这一行糟得多。"""
        assert pf.parse_coin(None) is None
        assert pf.parse_coin([1, 2]) is None
        empty = pf.parse_coin({})
        assert empty.market_cap_usd is None and empty.ath_market_cap_usd is None
        # ⚠️ 只给 SOL 计价的那个字段时也必须是 None —— 绝不拿它兜底
        assert pf.parse_coin({"market_cap": 28.0355}).market_cap_usd is None


# ============================================================
# 持仓 / 盈亏:portfolio 那一行里本来就有、以前没用上的字段
# ============================================================
class Test持仓与盈亏:
    """夹具 pump_portfolio.json 是 2026-08-31 真实响应,数值一位没动。"""

    def _row(self, symbol: str):
        rows = _load("pump_portfolio.json")["positions"]
        raw = next(r for r in rows if r["coin"]["symbol"] == symbol)
        return raw, pf.parse_positions({"positions": [raw]})[0]

    def test_未实现盈亏是市值减成本而不是报文里的pnlUsd(self):
        """
        ⚠️⚠️ `pnlUsd` 是**总**盈亏(已实现 + 未实现),不是未实现。
           真实夹具六行逐行验过:pnlUsd == (valueUsd − costBasisUsd) + realizedPnlUsd,
           差值恰好为 0。QQQB 那行 pnlUsd = -12,346.57 里有 -444.51 是已经落袋的,
           把它当未实现打出去就是把账面浮亏凭空多报了 444 美元。
        """
        raw, pos = self._row("QQQB")
        assert pos.unrealized_pnl_usd == -11902.050877559918
        assert raw["pnlUsd"] == -12346.565395949918          # ← 报文里那个总盈亏
        assert pos.unrealized_pnl_usd != raw["pnlUsd"]
        # 恒等式本身也钉住:哪天上游改了语义,这条会先红
        assert raw["pnlUsd"] == pytest.approx(
            pos.unrealized_pnl_usd + raw["realizedPnlUsd"], rel=0, abs=1e-9)

    def test_未实现盈亏率的基数是持仓成本而不是累计买入额(self):
        """
        ⚠️⚠️ 报文里的 `pnlPercentage` 分子是总盈亏、分母是 amountBoughtUsd
           (累计买入额)—— 两头都跟"未实现"对不上。QQQB 这一行:
           报文说 -11.01%(总盈亏 / 11.2 万累计买入),而账面上那点残仓
           实际浮亏 -99.97%。差了整整一个数量级,而且方向上会让人以为"还好"。
        """
        raw, pos = self._row("QQQB")
        assert raw["pnlPercentage"] == -11.006025934156668
        assert pos.unrealized_pnl_pct == pytest.approx(-99.96615, abs=1e-4)

    def test_成本为0时不给百分比但金额照出(self):
        """⚠️ 整仓靠转入拿到的(costBasisUsd=0)算不出收益率,除零之外
           "成本 0 赚无穷倍"本身也不是个能摆给人看的数。金额那一段不受影响。"""
        _raw, pos = self._row("DJT")
        assert pos.cost_basis_usd == 0
        assert pos.unrealized_pnl_usd == 4.675107218680533
        assert pos.unrealized_pnl_pct is None

    def test_清仓的判据与已实现百分比(self):
        """⚠️ 清仓时 value == cost == 0,未实现恒为 0,pnlPercentage 说的就是已实现的
           百分比 —— 只有在这个前提成立时才取用它。"""
        _raw, gta = self._row("GTA")
        assert gta.is_cleared is True
        assert gta.unrealized_pnl_usd == 0
        assert gta.realized_pnl_usd == 1072.6177752902256
        assert gta.realized_pnl_pct == 35.84117610173922

    def test_还持有的行绝不会被当成清仓(self):
        for sym in ("QQQB", "DJT", "PUNCHMA"):
            _raw, pos = self._row(sym)
            assert pos.is_cleared is False, sym

    def test_isExited给字符串false时不算清仓(self):
        """
        ⚠️⚠️ bool("false") 是 **True**。原样真值判断会把一个还满仓的人
           渲染成「📦 已清仓」,而且盈亏行跟着切到已实现 —— 整条消息全错。
           认不出来就退回 amountHeld(这里是 5.0,所以仍在持仓)。
        """
        pos = pf.parse_positions(position_payload(
            {**pos_row(held=5.0), "isExited": "false"}))[0]
        assert pos.is_exited is None
        assert pos.is_cleared is False

    def test_拿不到持有量时不推断成清仓(self):
        """⚠️ None 是"这一轮没拿到量",不是"清仓了"。"""
        pos = pf.parse_positions(position_payload(
            {"coinMint": MINT_SOL, "chainId": 1399811149, "coin": {"symbol": "X"}}))[0]
        assert pos.amount_held is None
        assert pos.is_cleared is False

    def test_持仓字段不进快照(self):
        """
        ⚠️ 快照只存 diff 判据要用的那几列。多存一列 = 多一个每轮都在变的触发器。
        """
        base = {**pos_row(held=1.0, pnl=0.0), "valueUsd": 100.0, "costBasisUsd": 10.0}
        moved = {**base, "valueUsd": 999.0, "costBasisUsd": 11.0, "pnlPercentage": 42.0}
        prev = pf.parse_positions(position_payload(base))[0]
        now = pf.parse_positions(position_payload(moved))[0]
        assert pf._snap_row(prev) == pf._snap_row(now)       # 快照一字不差
        assert now.value_usd == 999.0                        # 但推送拿得到新值

    def test_清仓但未实现不为0时不给已实现百分比(self):
        """
        ⚠️⚠️ pnlPercentage 能当"已实现百分比"用,**前提**是清仓时未实现恒为 0
           (那时 pnlUsd == realizedPnlUsd)。上游哪天把 isExited 与 valueUsd
           说岔了,这个前提就没了 —— 那时贴上去的百分比说的是**总**盈亏率,
           而标签写着"已实现",两者对不上却看不出来。宁可只报金额。
        ⚠️ 这条现在打不到真实数据(实测清仓行 value/cost 都是 0),它钉的是
           那道自查本身:去掉自查这条就绿了,而线上没有任何东西会告诉你它错了。
        """
        pos = pf.parse_positions(position_payload({
            **pos_row(held=0.0), "isExited": True,
            "valueUsd": 5.0, "costBasisUsd": 0.0,
            "realizedPnlUsd": 100.0, "pnlPercentage": 12.34,
        }))[0]
        assert pos.is_cleared is True
        assert pos.unrealized_pnl_usd == 5.0                 # 前提被打破了
        assert pos.realized_pnl_pct is None                  # → 不给百分比
        assert pos.realized_pnl_usd == 100.0                 # 金额照出


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

    def test_只有行情字段变动时一个逐笔请求都不打(self, db, cfg):
        """
        ⚠️⚠️ valueUsd / costBasisUsd / pnlPercentage 随行情每轮都在动 ——
           它们一旦被算进 diff 判据,整页 mint 每轮都会被当成"刚变动"全问一遍:
           K 从 0 变成 50,而这次还给每个 mint 又挂了一个市值请求,
           一轮就能把两个端点的限流全打光。判据必须只认 amountHeld / realizedPnlUsd。
        ⚠️ 这条必须让持仓行**真的带上** valueUsd —— 不带的话把它加进判据也看不出来
           (None 参与比较恰好还是"没变"),等于白写一条用例。
        """
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, HEX_UID,
                                        [("1399811149", MINT_SOL, 1.0, 0.0, "T0")])
        row = {**pos_row(held=1.0, pnl=0.0), "valueUsd": 987.65,
               "costBasisUsd": 12.34, "pnlPercentage": 42.0}
        client = FakeClient(portfolios={HEX_SVM: position_payload(row)},
                            trades={MINT_SOL: trade_payload()},
                            coins={MINT_SOL: {"usd_market_cap": 1.0}})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.trade_calls == []
        assert client.coin_calls == []

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


# ============================================================
# 市值:每轮多打的那几个请求
# ============================================================
def coin_payload(mcap=445526.7119903613, ath=2116892.897778326) -> dict:
    """
    造一个 /coins-v3 响应。

    ⚠️ 同时给 SOL 计价的 `market_cap`,而且**故意给一个明显不同的值** ——
       被测代码但凡取错字段,渲染断言就会看见 $28.04 而不是 $445.53K。
    """
    return {"usd_market_cap": mcap, "market_cap": 28.035521634857712,
            "ath_market_cap": ath, "symbol": "X"}


class Test市值请求与缓存:
    """
    ⚠️⚠️ 这一组钉的是**成本**:市值是这次唯一按 mint 计费的额外请求。
       没有闸的话每轮请求数会从 3+K 涨到 3+2K 以上(甚至按"人×成交笔数"膨胀),
       而 /coins-v3 只有 60 次/分。
    """

    def _many_trades(self, addr, prefix, n):
        payload = trade_payload(addr=addr, tx=f"{prefix}0", slot="0")
        payload[addr] = [{**payload[addr][0], "tx": f"{prefix}{i}", "slotIndexId": str(i)}
                         for i in range(n)]
        return payload

    def test_一个mint一轮只问一次市值哪怕推了很多条(self, db, cfg):
        """
        ⚠️⚠️ 市值是**按 mint** 的属性,不是按成交、也不是按人的。
           在逐笔渲染那一层现问现取的话,一个人拆 12 单就是 12 个请求;
           两个人各 12 单就是 24 个 —— 一个 mint 就能把 60/分 的额度打掉小半。
        """
        seeded_user()
        seeded_user(username="brc20niubi", uid="uid-2", svm="BQ4Kzz", evm="0x" + "b" * 40)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=2.0)),
                        "BQ4Kzz": position_payload(pos_row(held=7.0))},
            trades={MINT_SOL: {**self._many_trades(HEX_SVM, "M", 6),
                               **self._many_trades("BQ4Kzz", "H", 6)}},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 12
        assert client.coin_calls == [MINT_SOL]           # 12 条消息,1 个市值请求
        assert all("💎 市值 $445.53K" in m for m in tg.sent)

    def test_没有要推的成交就一个市值请求都不打(self, db, cfg):
        """
        ⚠️⚠️ 变动的 mint 里有相当一部分最后一条都推不出来(金额没过门槛 /
           已在台账里 / 掉出新鲜窗口)。无条件先问一次 coins-v3 就是拿限流额度
           换一个没人会看到的数 —— 而"变动"比"该推"常见得多。
        """
        cfg(FOMO_PUMP_MIN_USD="100")
        seeded_user()
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
            trades={MINT_SOL: trade_payload(usd="1.0")},   # 低于门槛,推不出去
            coins={MINT_SOL: coin_payload()},
        )
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 0
        assert client.trade_calls != []                   # 逐笔照问(要它才知道推不出去)
        assert client.coin_calls == []                    # 市值一个都不问

    def test_推送预算见底的mint也不问市值(self, db, cfg):
        """⚠️ 那些成交本轮根本不发,现在问到的市值到下一轮已经过期了。"""
        seeded_user()
        mint_b = "MintBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbpump"   # 排序在 MINT_SOL 之后
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(mint=MINT_SOL, held=2.0),
                                                  pos_row(mint=mint_b, held=3.0))},
            trades={MINT_SOL: self._many_trades(HEX_SVM, "A", 25),
                    mint_b: self._many_trades(HEX_SVM, "B", 3)},
            coins={MINT_SOL: coin_payload(), mint_b: coin_payload()},
        )
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 20
        assert client.coin_calls == [MINT_SOL]

    def test_市值问不到时成交照推只是少一行(self, db, cfg):
        """⚠️⚠️ 市值**绝不是推送的前置条件**:限流抖一下就把成交推送整段吃掉,
           那是拿一个锦上添花的字段勒索整个功能。"""
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
                            trades={MINT_SOL: trade_payload()},
                            coins={MINT_SOL: None})       # 拉取失败
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert client.coin_calls == [MINT_SOL]
        assert "市值" not in tg.sent[0] and "💎" not in tg.sent[0]
        assert "💰 金额 $1,234.50" in tg.sent[0]

    # ---- 跨轮缓存 --------------------------------------------------------
    def _round_two_client(self):
        """两轮都有变动、都有新成交的一个 client(第二轮持仓量与成交都换了)。"""
        return FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=2.0))},
            trades={MINT_SOL: trade_payload(tx="T1", slot="1")},
            coins={MINT_SOL: coin_payload()},
        )

    def _advance_round(self, client, held, tx):
        client.portfolios[HEX_SVM] = position_payload(pos_row(held=held))
        client.trades[MINT_SOL] = trade_payload(tx=tx, slot=tx)

    def test_TTL之内的第二轮不再问同一个mint(self, db, cfg):
        """
        ⚠️ 缓存的主职责是"同一轮里同一个 mint 只查一次";跨轮命中只在把巡检间隔
           调到 TTL 以下时才发生,那正是请求最密、最需要压一压的时候。
        """
        seeded_user()
        client = self._round_two_client()
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 1
        self._advance_round(client, 3.0, "T2")
        assert w.run_once() == 1
        assert client.coin_calls == [MINT_SOL]            # 两轮共 1 个市值请求
        assert client.trade_calls != []                   # 逐笔照旧每轮都问

    def test_缓存过期之后重新问(self, db, cfg):
        """⚠️ 市值变化快,缓存不能变成"问过一次就永远不再问"。"""
        seeded_user()
        client = self._round_two_client()
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 1
        # 把这一项的时间戳往回拨一小时 —— 等价于"这条缓存早就过期了"
        ts, stats = w._coin_cache[MINT_SOL]
        w._coin_cache[MINT_SOL] = (ts - 3600.0, stats)
        self._advance_round(client, 3.0, "T2")
        assert w.run_once() == 1
        assert client.coin_calls == [MINT_SOL, MINT_SOL]

    def test_失败绝不进缓存下一轮还会重试(self, db, cfg):
        """
        ⚠️⚠️ 把失败也缓存下来 = 一次网络抖动把市值那一行按住整个 TTL,
           而且没有任何日志会说"这行是被缓存按住的"。
        """
        seeded_user()
        client = self._round_two_client()
        client.coins[MINT_SOL] = None                     # 第一轮拉取失败
        w = pf.PumpWatcher(FakeNotifier(), client)
        assert w.run_once() == 1
        assert w._coin_cache == {}
        client.coins[MINT_SOL] = coin_payload()
        self._advance_round(client, 3.0, "T2")
        tg = w._notifier
        assert w.run_once() == 1
        assert client.coin_calls == [MINT_SOL, MINT_SOL]
        assert "💎 市值 $445.53K" in tg.sent[-1]

    def test_一轮的峰值请求数是人数加两倍变动mint数(self, db, cfg):
        """
        ⚠️⚠️ 这是这次改动的成本核算,写成断言钉住:
           portfolio 每人 1 个(P),逐笔每个变动 mint 1 个(K),
           市值每个**要推的** mint 至多 1 个(≤ K)—— 合计 P + 2K 封顶。
           少一道闸(不缓存 / 按成交问 / 无条件问)这个等式立刻破。
        """
        cfg(FOMO_PUMP_MAX_MINTS="8")
        seeded_user()
        seeded_user(username="brc20niubi", uid="uid-2", svm="BQ4Kzz", evm="0x" + "b" * 40)
        mints = [f"Mint{i}fixturepump" for i in range(3)]
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(
                *[pos_row(mint=m, held=1.0 + i) for i, m in enumerate(mints)]),
                "BQ4Kzz": position_payload(
                *[pos_row(mint=m, held=5.0 + i) for i, m in enumerate(mints)])},
            trades={m: {**self._many_trades(HEX_SVM, f"A{i}", 1),
                        **self._many_trades("BQ4Kzz", f"B{i}", 1)}
                    for i, m in enumerate(mints)},
            coins={m: coin_payload() for m in mints},
        )
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 6
        p, k = 2, 3
        assert len(client.portfolio_calls) == p
        assert len(client.trade_calls) == k
        assert len(client.coin_calls) == k
        total = len(client.portfolio_calls) + len(client.trade_calls) + len(client.coin_calls)
        assert total == p + 2 * k == 8


# ============================================================
# 👥 名单内至少 N 人持有 —— 只认本轮的正面观测,纯本地零请求
# ============================================================
class Test名单内人数:
    """
    ⚠️⚠️ 这个数的分子**只能**来自「本轮每个人的 page 0 里真的看见的那几行」,
       一行都不许从 pump_positions 快照表里补。
       那张表只 upsert、**从不删除本轮没出现的行**(store.upsert_pump_positions
       的注释写了理由),而 portfolio 只拉 page 0 的 50 行 ——
       某人清了仓、那次清仓又恰好没被看见(进程停过,或他持仓多到 50 行放不下,
       实测有人 1905 个持仓),他那行 amount_held 就永远停在旧值。
       拿它当分子 = 把三个月前的快照说成"他现在还拿着",**主动说了一句假话**。
    ⚠️ 代价也要写清楚:只认本轮观测就只会**少算**(page 0 没排到 / 本轮拉取失败),
       所以文案里必须有「至少」二字 —— 有用例专门钉它。
    """

    def _snap(self, uid, chain, mint, held):
        with store.get_conn() as conn, store.tx(conn):
            store.upsert_pump_positions(conn, uid, [(chain, mint, held, 0.0, "T0")])

    # ---- A1:陈旧快照绝不能进分子 ---------------------------------------
    def test_库里的陈旧快照绝不算进人数(self, db, cfg):
        """
        ⚠️⚠️ A1 的原样复现:B 早就在这个币上清了仓,而那次清仓恰好没被看见,
           库里那行 amount_held 于是一直停在 1000(三个月前的快照)。
           A 本轮在同一个币上成交 —— 分子若从库里数就会推出「名单内 2 人持有」,
           而 B 手上一枚都没有。**那句话是假的。**
        ⚠️ B 本轮的 page 0 里确实没有这个币(被别的币挤出去了),
           这正是"只拉一页"的常态,不是构造出来的极端情况。
        """
        seeded_user()                                          # A = hexiecs
        seeded_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        self._snap("uid-b", "1399811149", MINT_SOL, 1000.0)    # ← 陈旧快照
        self._snap("uid-b", "56", MINT_BSC, 7.0)               # B 本轮唯一没变的持仓
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=3.0)),
                        # ⚠️ B 这一页里**根本没有** MINT_SOL
                        "BSvm": position_payload(pos_row(mint=MINT_BSC, chain=56, held=7.0))},
            trades={MINT_SOL: trade_payload(side="buy")},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]
        assert "2 人持有" not in tg.sent[0], "B 的持仓是三个月前的快照,不是本轮看到的"

    def test_本轮拉取失败的人不算进人数(self, db, cfg):
        """
        ⚠️⚠️ 另一半:B 本轮的 portfolio 请求失败(返回 None)。
           "没拉到"是**不知道**,不是"他拿着"——
           而库里那行旧快照恰恰会把"不知道"补成"拿着"。
        ⚠️ 这条与上一条是两种不同的成因(页面挤出 vs 请求失败),
           上一条修好而这一条没修的实现是存在的,所以两条都要有。
        """
        seeded_user()
        seeded_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        self._snap("uid-b", "1399811149", MINT_SOL, 1000.0)
        client = FakeClient(                                   # BSvm 不在 portfolios 里 = 拉取失败
            portfolios={HEX_SVM: position_payload(pos_row(held=3.0))},
            trades={MINT_SOL: trade_payload(side="buy")},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]
        assert "2 人持有" not in tg.sent[0]

    def test_本轮真的看见了才算进人数(self, db, cfg):
        """
        对照组:证明上面两条钉的不是"永远只算 1 个人"。
        B 本轮的 page 0 里就有这个币、量是正数 —— 这才是可以摆出来的正面观测。
        """
        seeded_user()
        seeded_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=3.0)),
                        "BSvm": position_payload(pos_row(held=9.0))},
            trades={MINT_SOL: trade_payload(side="buy")},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        # ⚠️ B 也是"新出现的持仓"→ 他那笔也会被推,所以这里两条消息都发得出去
        assert pf.PumpWatcher(tg, client).run_once() >= 1
        assert "👥 名单内至少 2 人持有" in tg.sent[0]

    def test_文案必须是至少而不是断言恰好几个人(self, db, cfg):
        """
        ⚠️⚠️ 「至少」两个字是这句话为真的全部条件。我们只看 page 0 的 50 行,
           而且拉取失败的人整个不算 —— 真实人数永远 ≥ 我们数出来的这个 N。
           写成「名单内 2 人持有」就是断言"恰好 2 个",而那个"恰好"证明不了。
        """
        seeded_user()
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=6.0))},
                            trades={MINT_SOL: trade_payload()},
                            coins={MINT_SOL: coin_payload()})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        line = next(ln for ln in tg.sent[0].splitlines() if "名单内" in ln)
        assert line == "👥 名单内至少 1 人持有"

    # ---- 其余口径 --------------------------------------------------------
    def test_软删除的人不算进人数(self, db, cfg):
        """
        ⚠️⚠️ /pump del 是**软删除**:active 置 0,持仓快照那几行照旧留在库里。
           被移出名单的人绝不能留在分子里 —— 这个数字的全部含义就是
           「**名单里**有几个人拿着」。
        """
        seeded_user()
        seeded_user(username="走了的人", uid="uid-gone", svm="GoneSvm", evm=None)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=6.0)),
                        "GoneSvm": position_payload(pos_row(held=5.0))},
            trades={MINT_SOL: trade_payload()},
            coins={MINT_SOL: coin_payload()},
        )
        with store.get_conn() as conn, store.tx(conn):
            store.remove_pump_user(conn, "uid-gone")
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]
        assert "GoneSvm" not in client.portfolio_calls, "软删除的人连请求都不该打"

    def test_清仓和拿不到量的都不算持有(self, db, cfg):
        """⚠️ 0 是"已清仓"、None 是"不知道" —— 两者都不足以支撑"他持有"这句断言。"""
        seeded_user()
        seeded_user(username="清了的人", uid="uid-zero", svm="ZeroSvm", evm=None)
        seeded_user(username="没量的人", uid="uid-null", svm="NullSvm", evm=None)
        no_amount = pos_row(held=0.0)
        no_amount["amountHeld"] = None
        no_amount["isExited"] = None
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=6.0)),
                        "ZeroSvm": position_payload(pos_row(held=0.0)),
                        "NullSvm": position_payload(no_amount)},
            trades={MINT_SOL: trade_payload()},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]

    def test_上游说已清仓时哪怕量是正数也不算持有(self, db, cfg):
        """
        ⚠️ 量与 isExited 打架时(量 > 0 却标着已清仓)两边都不可信 ——
           这个数字宁可少算,也绝不能多算一个人。
        """
        seeded_user()
        seeded_user(username="自相矛盾", uid="uid-x", svm="XSvm", evm=None)
        weird = pos_row(held=5.0)
        weird["isExited"] = True
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=6.0)),
                        "XSvm": position_payload(weird)},
            trades={MINT_SOL: trade_payload()},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]

    def test_同一个地址串在两条链上分开数(self, db, cfg):
        """⚠️ 同一个地址串在两条链上是**两个币**(见建表注释)。"""
        seeded_user()
        seeded_user(username="别的链", uid="uid-bsc", svm="BscSvm", evm=None)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=6.0)),
                        # 同一个 mint 字符串,但 chainId 是 56
                        "BscSvm": position_payload(pos_row(chain=56, held=5.0))},
            trades={MINT_SOL: trade_payload()},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        pf.PumpWatcher(tg, client).run_once()
        sol = next(m for m in tg.sent if "🧬 Solana" in m)
        assert "👥 名单内至少 1 人持有" in sol

    def test_本轮刚清仓的人不算进分子(self, db, cfg):
        """
        ⚠️⚠️ 库里还记着他上一轮的 100,而他本轮卖光了。直接读库 =
           把一个刚跑掉的人算成持有者,而这条消息说的正是他跑掉这件事。
        """
        seeded_user()
        self._snap(HEX_UID, "1399811149", MINT_SOL, 100.0)
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=0.0))},
            trades={MINT_SOL: trade_payload(side="sell")},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "名单内" not in tg.sent[0]                  # 0 人 → 整行消失
        assert "📦 已清仓" in tg.sent[0]

    def test_名单外的人不进人数(self, db, cfg):
        """⚠️ 与"只推被盯的人"同一道闸:分子里也绝不能混进名单外的人。"""
        seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            # 直接往快照表塞一个从没进过名单的 user_id(模拟历史遗留脏行)
            store.upsert_pump_positions(conn, "uid-陌生人",
                                        [("1399811149", MINT_SOL, 5.0, 0.0, "T0")])
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=6.0))},
                            trades={MINT_SOL: trade_payload()},
                            coins={MINT_SOL: coin_payload()})
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1
        assert "👥 名单内至少 1 人持有" in tg.sent[0]

    def test_播种轮看到的持仓照样算进别人的人数(self, db, cfg):
        """
        ⚠️ 播种只压住"推他自己的成交"这件事,不影响"我们看见了他的持仓"——
           那一页是本轮亲眼拉回来的,是可以摆出来的正面观测。
        """
        seeded_user()
        add_user(username="新人", uid="uid-new", svm="NewSvm", evm=None)   # 没播过种
        client = FakeClient(
            portfolios={HEX_SVM: position_payload(pos_row(held=6.0)),
                        "NewSvm": position_payload(pos_row(held=5.0))},
            trades={MINT_SOL: trade_payload()},
            coins={MINT_SOL: coin_payload()},
        )
        tg = FakeNotifier()
        assert pf.PumpWatcher(tg, client).run_once() == 1   # 新人本轮一条都不推
        assert "👥 名单内至少 2 人持有" in tg.sent[0]

    def test_数人数不打任何请求(self, db, cfg):
        """⚠️ 这一行是**纯本地**的:它一个请求都不该增加。"""
        seeded_user()
        self._snap(HEX_UID, "1399811149", MINT_SOL, 5.0)
        client = FakeClient(portfolios={HEX_SVM: position_payload(pos_row(held=6.0))},
                            trades={MINT_SOL: trade_payload()},
                            coins={MINT_SOL: coin_payload()})
        assert pf.PumpWatcher(FakeNotifier(), client).run_once() == 1
        assert len(client.portfolio_calls) == 1
        assert len(client.trade_calls) == 1
        assert len(client.coin_calls) == 1                 # 1 + 1 + 1,没有第四种请求


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
# 渲染:这次补上的四组字段
# ============================================================
class Test补齐的字段:
    """
    对齐 FOMO 那条推送的 📦 持仓 / 📈 盈亏 / 💎 市值 / 👥 名单内。
    ⚠️ 断言写死字面量,不从 formatter 里 import 任何标签常量。
    """

    def _render(self, **kw):
        from src.formatter import render_pump_trade

        base = dict(username="hexiecs", side="buy", token_symbol="PUNCHMA",
                    coin_mint=MINT_SOL, amount_usd=1234.5, price_usd=0.0000285,
                    holding_usd=4321.0, is_cleared=False,
                    unrealized_pnl_usd=123.45, unrealized_pnl_pct=11.11,
                    realized_pnl_usd=-970.6830142173674, realized_pnl_pct=-88.77873844252953,
                    market_cap_usd=445526.7119903613, ath_market_cap_usd=2116892.897778326,
                    holders_in_list=2,
                    traded_at="2026-08-31T01:59:02.000Z", network_id="solana",
                    chain_display="Solana", tx="SIG_ABC", now=1787193542.0)
        base.update(kw)
        return render_pump_trade(**base)

    # ---- 📦 持仓 ---------------------------------------------------------
    def test_买入说持仓卖出说剩余(self):
        assert "📦 持仓 $4,321.00" in self._render(side="buy")
        assert "📦 剩余 $4,321.00" in self._render(side="sell")

    def test_清仓时说已清仓而不是持仓0(self):
        """
        ⚠️⚠️ 与 bot._ca_thesis_row 同一条教训:「📦 持仓 $0.00」这半句没错,
           但紧跟着的「未实现盈亏 +$0.00」会把一个刚落袋 -$970.68 的人渲染成
           "不赚不亏"—— 那是凭空断言的假事实。清仓必须换措辞、换成已实现。
        """
        out = self._render(is_cleared=True, holding_usd=0.0, side="sell",
                           unrealized_pnl_usd=0.0, unrealized_pnl_pct=None)
        assert "📦 已清仓" in out
        assert "持仓 $0.00" not in out and "剩余 $0.00" not in out
        assert "未实现" not in out
        assert "📉 已实现盈亏 -$970.68 (-88.78%)" in out

    def test_清仓判定为真时哪怕持仓额还在也不报持仓额(self):
        """⚠️ 两个来源打架时以 is_cleared 为准:一条消息里既说"已清仓"又说
           "持仓 $4,321"是自相矛盾,读者不知道该信哪一句。"""
        out = self._render(is_cleared=True)
        assert "$4,321.00" not in out
        assert "📦 已清仓" in out

    def test_持仓额拿不到时只掉这一行(self):
        out = self._render(holding_usd=None)
        assert "📦" not in out
        assert "💰 金额 $1,234.50" in out                 # 主干照推
        assert "N/A" not in out and "--" not in out

    def test_粉尘仓位不塌成看着像缺失的0(self):
        """⚠️ 实测 PUNCHMA 那一行 valueUsd = 0.00000703。两位小数会把它渲染成
           「📦 持仓 $0.00」,而 $0.00 在这条消息里已经被「已清仓」占走了含义。"""
        out = self._render(holding_usd=7.03771423819e-06,
                           unrealized_pnl_usd=4.164011864051587e-06,
                           unrealized_pnl_pct=144.9005958837345)
        assert "📦 持仓 $0.000007038" in out
        assert "📈 未实现盈亏 +$0.000004164 (+144.90%)" in out

    # ---- 📈 盈亏 ---------------------------------------------------------
    def test_在仓看未实现清仓看已实现(self):
        """⚠️ 混用就是把落袋的钱说成账面的,或者反过来 —— 清仓那一刻两者差的是全部。"""
        held = self._render(is_cleared=False)
        assert "📈 未实现盈亏 +$123.45 (+11.11%)" in held
        assert "已实现" not in held
        closed = self._render(is_cleared=True)
        assert "📉 已实现盈亏 -$970.68 (-88.78%)" in closed
        assert "未实现" not in closed

    def test_盈亏正好是0照常显示(self):
        """⚠️ 判据一律 is None:0 是有意义的真实值(刚开仓、或买卖打平)。"""
        assert "📈 未实现盈亏 +$0.00" in self._render(unrealized_pnl_usd=0.0,
                                                 unrealized_pnl_pct=0.0)

    def test_百分比拿不到时只掉括号金额照出(self):
        out = self._render(unrealized_pnl_pct=None)
        assert "📈 未实现盈亏 +$123.45" in out
        assert "(" not in out.split("未实现盈亏")[1].split("\n")[0]

    def test_盈亏拿不到时整行消失(self):
        out = self._render(unrealized_pnl_usd=None, unrealized_pnl_pct=None)
        assert "盈亏" not in out
        assert "📈" not in out and "📉" not in out

    def test_涨跌用不同的emoji且带正负号(self):
        assert "📈 未实现盈亏 +$123.45" in self._render(unrealized_pnl_usd=123.45)
        assert "📉 未实现盈亏 -$123.45" in self._render(unrealized_pnl_usd=-123.45)

    # ---- 💎 市值 ---------------------------------------------------------
    def test_市值按缩写显示并带距最高(self):
        out = self._render()
        assert "💎 市值 $445.53K · 距最高 -79.0%" in out

    def test_市值拿不到时整行消失绝不打0或占位符(self):
        """
        ⚠️⚠️ 市值是这次唯一要多打一个请求换来的字段,它**最容易拿不到**
           (限流、404、上游改字段)。拿不到时绝不能退化成 $0.00 / N/A / -- ——
           那三种写法都会被读成"这币市值是 0 / 归零了",而真相是"我们没问到"。
        """
        out = self._render(market_cap_usd=None)
        assert "💎" not in out and "市值" not in out
        # ⚠️ 只比对整行:`$0.00` 会命中「单价 $0.0000285」的前缀,
        #    那种松断言换个字段照样绿(等于没测)
        for ln in out.splitlines():
            assert ln not in ("💎 市值 $0.00", "💎 市值 N/A", "💎 市值 --", "💎 市值 0")
        assert "N/A" not in out and " -- " not in out
        assert "距最高" not in out                        # 连带那一段也不能留
        assert "💰 金额 $1,234.50" in out                 # 成交本身照推

    def test_市值拿不到时连带距最高也不出(self):
        out = self._render(market_cap_usd=None, ath_market_cap_usd=2116892.897778326)
        assert "距最高" not in out

    def test_历史最高拿不到时只掉后半段市值照出(self):
        out = self._render(ath_market_cap_usd=None)
        assert "💎 市值 $445.53K" in out
        assert "距最高" not in out

    def test_历史最高不高于当前时不出距最高(self):
        """
        ⚠️ 两层意思:① 正在创新高时「距最高 -0.0%」是纯噪音;
           ② 万一哪条链的 ath 换成了 quote 计价(会比美元市值小一两个数量级),
           这一段自己就不出现,而不是打出一个 +10000% 的鬼数。
        """
        for ath in (445526.7119903613, 28.0355, 0, -1):
            out = self._render(ath_market_cap_usd=ath)
            assert "距最高" not in out, ath
            assert "💎 市值 $445.53K" in out

    def test_市值是SOL计价那个数时渲染出来差两个数量级(self):
        """
        ⚠️ 这条钉的是"取错字段的后果长什么样",让人一眼看出它有多贵:
           同一个 Solana 币,usd_market_cap 是 $2.91K,market_cap 是 28.04(SOL)。
        """
        assert "💎 市值 $2.91K" in self._render(market_cap_usd=2906.090666119672,
                                              ath_market_cap_usd=None)
        assert "💎 市值 $28.04" in self._render(market_cap_usd=28.035521634857712,
                                              ath_market_cap_usd=None)

    # ---- 👥 名单内 -------------------------------------------------------
    def test_名单内人数只报分子不报分母(self):
        """
        ⚠️⚠️ 与 FOMO 那条的「3/101 人买过」刻意不同:分母的含义是"另外那些人没买过",
           而我们只看得到每人 page 0 的 50 行(实测有人 1905 个持仓),
           连一个人"没持有"都证明不了。写成「2/3」既是拿部分视图冒充全量,
           又会在名单只有个位数时被读成"共识"。
        """
        out = self._render(holders_in_list=2)
        assert "👥 名单内至少 2 人持有" in out
        assert "/" not in out.split("名单内")[1].split("\n")[0]

    def test_人数必须带至少二字而不是断言恰好几个人(self):
        """
        ⚠️⚠️ 这个 N 只能往少了数(page 0 只有 50 行、本轮拉取失败的人整个不算),
           所以真实人数 ≥ N。写成「名单内 2 人持有」就是断言"恰好 2 个",
           而那个"恰好"我们证明不了。
        ⚠️ 断言**整行相等**,不用 in:`"2 人持有" in out` 在带不带「至少」两种写法下
           都成立,那种松断言等于没测。
        """
        line = next(ln for ln in self._render(holders_in_list=2).splitlines()
                    if "名单内" in ln)
        assert line == "👥 名单内至少 2 人持有"

    def test_名单内人数为0时整行消失(self):
        """⚠️ 0 不是"没人持有",是"我们这份部分视图里没看见"—— 不能拿它下断言。"""
        out = self._render(holders_in_list=0)
        assert "名单内" not in out and "👥" not in out

    def test_名单内人数拿不到时整行消失(self):
        assert "名单内" not in self._render(holders_in_list=None)

    def test_措辞是持有不是买过(self):
        """⚠️ 数据来自本轮的持仓视图(现在还拿着多少),不是买入历史 ——
           FOMO 那边有 user_token_stats 才敢说"买过"。"""
        out = self._render(holders_in_list=3)
        assert "3 人持有" in out and "买过" not in out

    # ---- 行序 / 出口不变式 -----------------------------------------------
    def test_行序与FOMO那条对齐(self):
        """标题 → 金额 → 单价 → 持仓 → 盈亏 → 市值 → 名单内 → 时刻 → 链 → 签名 → 链接 → CA"""
        lines = self._render().splitlines()
        heads = [ln.split(" ")[0] for ln in lines]
        assert heads == ["🟩", "💰", "📊", "📦", "📈", "💎", "👥", "⏱", "🧬", "🧾", "🔗",
                         "<code>SIG"[:0] + "<code>G2ZYvnesQzucoSy3VP7xap1PpMb3btiVXYNWCPKepump</code>"]

    def test_行全满时仍然满足出口不变式(self):
        """⚠️ 多了四行,≤ 预算 / HTML 合法 / CA 独占最后一行三条都不许破。"""
        out = self._render()
        assert len(out) <= 4000 - 400
        assert out.count("<b>") == out.count("</b>")
        assert out.count("<code>") == out.count("</code>")
        assert out.count("<a ") == out.count("</a>")
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_行全满且陌生人可控字段超长时CA仍然完整在最后一行(self):
        out = self._render(username="超长" * 5000, token_symbol="X" * 5000,
                           tx="T" * 5000)
        assert len(out) <= 4000 - 400
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_预算被顶破时先砍新增的行而CA仍在(self, monkeypatch):
        from src import formatter

        monkeypatch.setattr(formatter, "TRANSFER_MSG_BUDGET", 80)
        out = self._render()
        assert len(out) <= 80
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_新增的字段一个都不许替用户下结论(self):
        out = self._render()
        for banned in ("建仓", "跑路", "该跟", "抄底", "看好", "共识", "腰斩", "回本"):
            assert banned not in out


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


# ============================================================
# 观点(callout):解析
# ============================================================
def callout_seeded_user(**kw):
    """加人并把**观点**播种位置上 —— 大多数用例要测的是播种之后的行为。"""
    add_user(**kw)
    with store.get_conn() as conn, store.tx(conn):
        store.mark_pump_callout_seeded(conn, kw.get("uid", HEX_UID))


def callout_ledger_ids():
    with store.get_conn() as conn:
        rows = conn.execute("SELECT callout_id FROM pump_pushed_callouts").fetchall()
    return {r["callout_id"] for r in rows}


class Test观点解析:
    """
    ⚠️ 夹具 pump_callouts.json 是 2026-08-31 从
       GET /callout/list/{钱包}?limit=5&sortBy=TIMESTAMP&sortOrder=DESC&pageToken=
       真实抓下来的,**一个字段都没改**。断言里的数字一律写死字面量。
    """

    def test_真实夹具逐字段解析(self):
        rows = pf.parse_callouts(_load("pump_callouts.json"))
        assert len(rows) == 5
        c = rows[0]
        assert c.callout_id == "8e065932-66f7-4f1a-883f-5415771f8827"
        assert c.coin_mint == "0x198dba421a7db566a90da5de7901abe3443b4444"
        assert c.thesis == "This is a good story, I should've bought more yesterday."
        assert c.market_cap_usd == 3194101.0
        assert c.likes == 5.0
        assert c.view_count == 2953.0
        # ⚠️ userId 这个字段名骗人:值是**钱包地址**,不是 pump 的 userId(UUID)
        assert c.user_id == HEX_SVM

    def test_createdAt是epoch毫秒不是秒(self):
        """
        ⚠️⚠️ 当成秒直接用就差 1000 倍:1788150521329 秒 ≈ 公元 58000 年,
           新鲜窗口会把每一条都判成"来自未来、永远新鲜",于是一开机
           就把这个人的全部历史观点倒出来。反过来当成毫秒则每条都"老得掉出窗口"、
           一条都不推。两个方向都不报错,只是行为完全错。
        ⚠️ 断言写死绝对值:1788150521329 ms → 2026-08-31T04:28:41+00:00。
        """
        c = pf.parse_callouts(_load("pump_callouts.json"))[0]
        assert c.created_ts == pytest.approx(1788150521.329)
        assert c.created_at == "2026-08-31T04:28:41+00:00"
        # 最老的那条也钉一下,免得只对了一个点
        assert pf.parse_callouts(_load("pump_callouts.json"))[4].created_at == \
            "2026-08-29T14:50:57+00:00"

    def test_同一个功能里两种时间格式都要认(self):
        """
        ⚠️⚠️ pump 自己的两个地方就不一致(2026-08-31 实测):
           · /callout/list 的 createdAt 是 **epoch 毫秒**(数字);
           · 同一条记录里的 maxMultiplierAt、以及 updates[] 与别的 callout 端点,
             是 **ISO 字符串**。
           按类型分派,不靠数量级猜 —— 阈值本身就是个会过期的魔法数。
        """
        assert pf._parse_callout_ts(1788150521329) == pytest.approx(1788150521.329)
        assert pf._parse_callout_ts("2026-08-31T04:28:41.329Z") == \
            pytest.approx(1788150521.329)
        # 上游偶尔把毫秒包成字符串,同样认
        assert pf._parse_callout_ts("1788150521329") == pytest.approx(1788150521.329)
        # ⚠️ bool 是 int 的子类,不挡掉的话 True 会被当成 1 毫秒
        assert pf._parse_callout_ts(True) is None
        assert pf._parse_callout_ts(None) is None
        assert pf._parse_callout_ts("不是时间") is None

    def test_hasLiked匿名时是None而不是False(self):
        """
        ⚠️⚠️ 正撞铁律:`null` 的含义是"不知道"(我们是匿名的,没有观看者身份),
           真值判断会把它读成 False,也就是把"不知道"说成"他没点过赞"。
        ⚠️ 三态都要钉:null → None、false → False、true → True。
           只钉 null 的话,把整个字段丢掉不解析的实现也能全绿。
        """
        c = pf.parse_callouts(callout_payload(callout_row()))[0]
        assert c.has_liked is None
        assert c.has_reposted is None
        assert c.viewer_state_leaked is False

        got = pf.parse_callouts(callout_payload(
            callout_row(has_liked=False, has_reposted=True)))[0]
        assert got.has_liked is False
        assert got.has_reposted is True

    def test_观看者状态非null就是身份泄漏(self):
        """
        ⚠️⚠️ 本模块的硬约束是「不登录、不带任何凭据」。hasLiked / hasReposted
           在匿名下实测恒为 null,它们一旦有值就说明有 cookie / 令牌混进来了。
        ⚠️ **False 同样算泄漏**(它一样是身份态):判据必须是 `is not None`,
           写成真值判断的话 False 会被漏过去,而 None 会被误判成泄漏。
        """
        leaked_true = pf.parse_callouts(callout_payload(callout_row(has_liked=True)))[0]
        leaked_false = pf.parse_callouts(callout_payload(callout_row(has_liked=False)))[0]
        clean = pf.parse_callouts(callout_payload(callout_row()))[0]
        assert leaked_true.viewer_state_leaked is True
        assert leaked_false.viewer_state_leaked is True
        assert clean.viewer_state_leaked is False

    def test_字符串false不算真的bool(self):
        """⚠️ 与 isExited 同一条教训:bool("false") 是 True。认不出来就说不知道。"""
        row = callout_row()
        row["hasLiked"] = "false"
        assert pf.parse_callouts(callout_payload(row))[0].has_liked is None

    def test_空结果是空列表不是拉取失败(self):
        """
        ⚠️⚠️ 实测:没有观点的人返回 `200 {"callouts": [], "nextPageToken": ""}`,
           **不是 404**;非法 id 才是 400。所以 [] 是会真实出现的正常返回,
           它与 None(拉取失败)必须分得开 —— 混成一件事会让"他确实没发过观点"
           被当成"这轮没问到",播种位于是永远置不上。
        """
        assert pf.parse_callouts({"callouts": [], "nextPageToken": ""}) == []
        assert pf.parse_callouts("不是字典") is None
        assert pf.parse_callouts({"callouts": "不是数组"}) is None

    def test_缺calloutId或缺时刻的行整条丢弃(self):
        """没有 calloutId 就没有去重主键(下一轮必然重推),没有时刻就判不了窗口。"""
        no_id = callout_row()
        no_id.pop("calloutId")
        no_ts = callout_row(cid="c-2")
        no_ts.pop("createdAt")
        assert pf.parse_callouts(callout_payload(no_id, no_ts)) == []

    def test_离谱的createdAt不会让解析抛异常(self):
        """
        ⚠️ createdAt 是**裸数字**,没有 ISO 那种天然范围约束:上游给个 1e30
           就能让 fromtimestamp 抛,而所有 parse_* 的契约都是"不抛,丢这一行"。
        """
        crazy = callout_row()
        crazy["createdAt"] = 1e30
        assert pf.parse_callouts(callout_payload(crazy)) == []

    def test_币的链与符号只能从coins_v3拿(self):
        """
        ⚠️⚠️ /callout/list 只给 coinMint —— 既没有符号也没有链。
           链要从 coins-v3 的 `chain_id` 来,而它是 **CAIP-2** 形态
           (`eip155:56` / `solana:5eykt4Us…`),与 portfolio 给的数字 chainId
           (56 / 1399811149)**不是同一种写法**,别指望它们能对上。
        """
        sol = pf.parse_coin(_load("pump_coin_sol.json"))
        assert sol.chain_id == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
        assert sol.network_id == "solana"
        assert sol.chain_display == "Solana"
        assert sol.symbol == "PUNCHMA"

        bsc = pf.parse_coin(_load("pump_coin_bsc.json"))
        assert bsc.chain_id == "eip155:56"
        assert bsc.network_id == "bsc"
        assert bsc.symbol == "QQQB"

        evm = pf.parse_coin(_load("pump_coin_evm.json"))
        assert evm.network_id == "robinhood"
        assert evm.symbol == "KISS"

    def test_认不出的链只掉链接不掉整条(self):
        """⚠️ 拼一个 GMGN 不支持的链只会 404,错的链接比没有链接更糟。"""
        arb = pf.CoinStats(None, None, symbol="X", chain_id="eip155:42161")
        assert arb.network_id is None
        assert arb.chain_display == "Arbitrum"
        unknown = pf.CoinStats(None, None, symbol="X", chain_id="eip155:777777")
        assert unknown.network_id is None and unknown.chain_display is None
        assert pf.CoinStats(None, None).network_id is None


# ============================================================
# 观点:冷启动静默播种
# ============================================================
class Test观点冷启动播种:
    def test_第一轮一条都不推只记台账(self, db, cfg):
        """
        ⚠️⚠️ 不播种的话,开关打开的第一轮会把整页历史观点全推出去
           (实测 hexiecs 光最近 5 条就跨了 37.6 小时)—— 用户当场静音。
        """
        add_user()                                       # 没播过种
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1"), callout_row(cid="c-2"))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 0
        assert tg.sent == []
        assert callout_ledger_ids() == {"c-1", "c-2"}     # 记成"已知",不是推过
        with store.get_conn() as conn:
            assert store.list_pump_users(conn)[0]["callout_seeded"] == 1

    def test_播种之后的新观点才推(self, db, cfg):
        """对照组:证明播种压住的只是第一轮,不是把功能关掉了。"""
        add_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(callout_row(cid="old"))})
        w = pf.PumpCalloutWatcher(FakeNotifier(), client)
        assert w.run_once() == 0
        client.callouts[HEX_UID] = callout_payload(
            callout_row(cid="new", thesis="fresh take"), callout_row(cid="old"))
        tg = FakeNotifier()
        w2 = pf.PumpCalloutWatcher(tg, client)
        assert w2.run_once() == 1
        assert "fresh take" in tg.sent[0]
        assert callout_ledger_ids() == {"old", "new"}

    def test_买卖播过种不代表观点播过种(self, db, cfg):
        """
        ⚠️⚠️ 两路的开关是分别打开的:一个人可能已经被买卖监控播过种好几天,
           而观点开关今天才打开。复用一位播种位的话,他那几十条历史观点
           会在第一轮全部推出来。
        """
        seeded_user()                                    # 只置了买卖那一位
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1"), callout_row(cid="c-2"))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 0
        assert tg.sent == []

    def test_新加的人单独播种不受别人已播种影响(self, db, cfg):
        """
        ⚠️ 播种位必须**每人一位**:名单跑了三天之后再加一个新人,
           全局位早就是 1 了,那个新人的历史观点会在第一轮全推出去。
        """
        callout_seeded_user()
        add_user(username="新人", uid="uid-new", svm="NewSvm", evm=None)
        client = FakeClient(callouts={
            HEX_UID: callout_payload(callout_row(cid="mine")),
            "uid-new": callout_payload(callout_row(cid="his")),
        })
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1                          # 只推了老用户那条
        with store.get_conn() as conn:
            rows = {r["user_id"]: r for r in store.list_pump_users(conn)}
        assert rows["uid-new"]["callout_seeded"] == 1
        assert rows[HEX_UID]["seeded"] == 0               # 买卖那位没被顺手置上

    def test_播种轮拉取失败绝不能标成已播种(self, db, cfg):
        """
        ⚠️⚠️ 标成已播种就等于"这个人的历史观点已记为已知",而我们其实一条都没看到 ——
           下一轮起他在这期间发的观点会被永久静默吃掉。
        """
        add_user()
        client = FakeClient(callouts={HEX_UID: None})     # 拉取失败
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 0
        with store.get_conn() as conn:
            assert store.list_pump_users(conn)[0]["callout_seeded"] == 0
        assert callout_ledger_ids() == set()

    def test_复活的人重新播种观点(self, db, cfg):
        """
        ⚠️ 人被移出去这段时间他照样在发观点,而台账里那几条早被清理任务
           按新鲜窗口删掉了 —— 不归零就会把这期间的观点整段补推。
        """
        callout_seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.remove_pump_user(conn, HEX_UID)
            store.add_pump_user(conn, HEX_UID, "hexiecs", HEX_SVM, HEX_EVM)
        with store.get_conn() as conn:
            assert store.list_pump_users(conn)[0]["callout_seeded"] == 0


# ============================================================
# 观点:去重台账
# ============================================================
class Test观点台账:
    def test_同一条观点不会被推第二次(self, db, cfg):
        """
        ⚠️⚠️ /callout/list 每轮都把这个人**最近一页**整段返回,
           没有台账就是每轮重推一遍同样的几条。
        """
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(callout_row(cid="c-1"))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 0
        assert len(tg.sent) == 1
        assert callout_ledger_ids() == {"c-1"}

    def test_同一轮里重复出现的同一条只推一次(self, db, cfg):
        """
        ⚠️ 台账只挡**跨轮**重复(done 是轮首读的一份快照),轮内重复要自己挡:
           上游翻页边界抖动时同一页里能出现两条同 calloutId 的行。
        """
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="dup"), callout_row(cid="dup"))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert len(tg.sent) == 1

    def test_台账主键是calloutId本身(self, db, cfg):
        """
        ⚠️ calloutId 是 pump 给的 UUID,天然稳定且全局唯一 —— 它一个就够做主键。
           同一个人换个币再发一条是另一个 UUID,照常推。
        """
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1", mint=MINT_SOL),
            callout_row(cid="c-2", mint=MINT_BSC))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 2
        assert callout_ledger_ids() == {"c-1", "c-2"}

    def test_推送失败绝不记台账且下一轮会重来(self, db, cfg):
        """
        ⚠️⚠️ **推送成功才记台账**(与 poller._dispatch 的 ok = notifier.send(…) /
           if ok: 同一条铁律)。反过来写的话,一次 TG 400 或网络抖动就是
           这条观点的**永久丢失**,而且没有任何日志能让人发现。
        """
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(callout_row(cid="c-1"))})
        bad = FakeNotifier(ok=False)
        assert pf.PumpCalloutWatcher(bad, client).run_once() == 0
        assert len(bad.sent) == 1                         # 确实试着发了
        assert callout_ledger_ids() == set()              # 但一行台账都没有

        good = FakeNotifier()
        assert pf.PumpCalloutWatcher(good, client).run_once() == 1
        assert callout_ledger_ids() == {"c-1"}

    def test_一条失败不影响同一轮里的其它条(self, db, cfg):
        """失败的那条不记台账、下一轮重来;成功的那条照常记上,不会被连坐重推。"""
        callout_seeded_user()

        class Flaky(FakeNotifier):
            def send(self, text, **kw):
                self.sent.append(text)
                return "第二条" in text

        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1", thesis="第一条", age_sec=120),
            callout_row(cid="c-2", thesis="第二条", age_sec=60))})
        tg = Flaky()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert callout_ledger_ids() == {"c-2"}

    def test_掉出窗口的台账行会被清掉(self, db, cfg):
        """⚠️ 掉出新鲜窗口的观点永远不会再成为候选,留着只让表无限长大。"""
        cfg(FOMO_PUMP_CALLOUT_MAX_AGE_SEC="60")
        callout_seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.record_pump_callouts_pushed(
                conn, [("old", HEX_UID, MINT_SOL, "2020-01-01T00:00:00+00:00")])
        client = FakeClient(callouts={HEX_UID: callout_payload()})
        pf.PumpCalloutWatcher(FakeNotifier(), client).run_once()
        assert callout_ledger_ids() == set()


# ============================================================
# 观点:新鲜窗口与单轮上限
# ============================================================
class Test观点窗口与上限:
    def test_太老的观点不推(self, db, cfg):
        """
        ⚠️ /callout/list 返回的是这个人的**一段历史**。没有窗口的话,
           进程停一天再起来就是把几十条隔夜观点一次性倒出来。
        """
        cfg(FOMO_PUMP_CALLOUT_MAX_AGE_SEC="600")
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="old", age_sec=7200.0),
            callout_row(cid="new", age_sec=60.0))})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert callout_ledger_ids() == {"new"}

    def test_窗口改大之后原来太老的就推得出来了(self, db, cfg):
        """对照组:证明上一条卡住的是窗口本身,不是别的什么。"""
        cfg(FOMO_PUMP_CALLOUT_MAX_AGE_SEC="86400")
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="old", age_sec=7200.0))})
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 1

    def test_单轮上限是全局的两个人加起来也不许超(self, db, cfg):
        """
        ⚠️⚠️ 上限的语义是「一轮最多发几条(**所有人合计**)」——
           买卖那边刚踩过"不是全局上限"的坑:只在处理每个人之前判一次的话,
           最后一个人自己还能再发满一轮。
        ⚠️⚠️ 这里**验的是真实上界,不是文档里那个数**:两个人各 8 条 = 16 条待推,
           断言总数恰好等于 10 —— 如果预算没有真的扣减,结果会是 16。
           (写成"两个人各 12 条"就测不出来:第一个人一次就用光额度,
            任何一道"见底就跳过"的闸都能让总数停在 10。)
        """
        callout_seeded_user()
        callout_seeded_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        client = FakeClient(callouts={
            HEX_UID: callout_payload(*[callout_row(cid=f"A{i}", age_sec=100 - i)
                                       for i in range(8)]),
            "uid-b": callout_payload(*[callout_row(cid=f"B{i}", age_sec=100 - i)
                                       for i in range(8)]),
        })
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 10   # 写死字面量
        assert len(tg.sent) == 10
        assert len(callout_ledger_ids()) == 10
        # 第一个人 8 条全推完,第二个人只分到剩下的 2 条
        assert len([c for c in callout_ledger_ids() if c.startswith("A")]) == 8
        assert len([c for c in callout_ledger_ids() if c.startswith("B")]) == 2

    def test_同一个人待推太多时截断剩下的下一轮继续(self, db, cfg):
        """⚠️ 截断只截**本轮**:没记台账的那些下一轮还是候选,每轮都在推进。"""
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            *[callout_row(cid=f"C{i}", age_sec=100 - i) for i in range(15)])})
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 10
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 5
        assert len(callout_ledger_ids()) == 15

    def test_预算用光之后剩下的人连请求都不打(self, db, cfg):
        """
        ⚠️ 上限不只是"少发几条":发不出去的东西没必要先问回来。
           他们的台账与播种位都没被动过,下一轮照常轮到他们。
        """
        callout_seeded_user()
        add_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        client = FakeClient(callouts={
            HEX_UID: callout_payload(*[callout_row(cid=f"A{i}", age_sec=100 - i)
                                       for i in range(12)]),
            "uid-b": callout_payload(callout_row(cid="B0")),
        })
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 10
        assert [k for k, _lim in client.callout_calls] == [HEX_UID]
        with store.get_conn() as conn:
            rows = {r["user_id"]: r for r in store.list_pump_users(conn)}
        assert rows["uid-b"]["callout_seeded"] == 0       # 没轮到他,播种位不许动


# ============================================================
# 观点:故障不扩散 / 只打公开端点
# ============================================================
class Test观点故障不扩散:
    def test_客户端抛异常也不会逃到调度器(self, db, cfg):
        """⚠️ run_once 是这个功能与调度器之间的唯一接触面,它绝不抛。"""
        callout_seeded_user()
        client = FakeClient(raise_on_callout=RuntimeError("网络炸了"))
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 0

    def test_名单为空时一个请求都不打(self, db, cfg):
        client = FakeClient()
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 0
        assert client.callout_calls == []

    def test_软删除的人不再扫观点(self, db, cfg):
        callout_seeded_user()
        with store.get_conn() as conn, store.tx(conn):
            store.remove_pump_user(conn, HEX_UID)
        client = FakeClient(callouts={HEX_UID: callout_payload(callout_row())})
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 0
        assert client.callout_calls == []

    def test_身份泄漏要在日志里喊出来(self, db, cfg):
        """
        ⚠️⚠️ 匿名请求却拿回了 hasLiked/hasReposted = 这条链路上混进了 cookie 或令牌,
           而本模块的硬约束是绝不带凭据。这是要立刻看得见的事故信号。
        ⚠️ 只记日志、**不改变行为**(与限流告警同一条):告警不该顺手把功能关掉。
        """
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1", has_liked=False))})
        logs, stop = capture_warnings()
        try:
            assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 1
        finally:
            stop()
        assert any("hasLiked" in m for m in logs), logs

    def test_匿名的正常响应不该报警(self, db, cfg):
        """对照组:null 是常态,别把它也喊成事故(那样告警就没人看了)。"""
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(callout_row(cid="c-1"))})
        logs, stop = capture_warnings()
        try:
            assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 1
        finally:
            stop()
        assert not any("hasLiked" in m for m in logs), logs

    def test_一轮的峰值请求数是人数加要推的观点数(self, db, cfg):
        """
        ⚠️⚠️ 成本核算写成断言钉住:观点那一路每人 1 个 /callout/list(P'),
           外加**每条真要推的观点**至多 1 个 /coins-v3(C,同 mint 走缓存只算一次)。
           合计 P' + C 封顶。少一道闸(无条件问币、不缓存)这个等式立刻破。
        """
        callout_seeded_user()
        callout_seeded_user(username="B", uid="uid-b", svm="BSvm", evm=None)
        client = FakeClient(
            callouts={HEX_UID: callout_payload(callout_row(cid="A0", mint=MINT_SOL),
                                               callout_row(cid="A1", mint=MINT_SOL)),
                      "uid-b": callout_payload(callout_row(cid="B0", mint=MINT_BSC))},
            coins={MINT_SOL: coin_payload(), MINT_BSC: coin_payload()},
        )
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 3
        assert len(client.callout_calls) == 2                    # P' = 人数
        assert client.coin_calls == [MINT_SOL, MINT_BSC]         # 同 mint 只问一次

    def test_没有要推的观点就一个币请求都不打(self, db, cfg):
        """⚠️ 掉出窗口 / 已推过的那些,一个 coins-v3 请求都不该花。"""
        cfg(FOMO_PUMP_CALLOUT_MAX_AGE_SEC="60")
        callout_seeded_user()
        client = FakeClient(
            callouts={HEX_UID: callout_payload(callout_row(cid="old", age_sec=9999))},
            coins={MINT_SOL: coin_payload()},
        )
        assert pf.PumpCalloutWatcher(FakeNotifier(), client).run_once() == 0
        assert client.coin_calls == []

    def test_币信息问不到时观点照推只是少两行(self, db, cfg):
        """⚠️ 链与符号绝不是推送的前置条件 —— 观点本身才是这条消息的主体。"""
        callout_seeded_user()
        client = FakeClient(callouts={HEX_UID: callout_payload(
            callout_row(cid="c-1", thesis="still shipping"))})      # coins 缺省是空表
        tg = FakeNotifier()
        assert pf.PumpCalloutWatcher(tg, client).run_once() == 1
        assert "still shipping" in tg.sent[0]
        assert "🧬" not in tg.sent[0]
        assert tg.sent[0].splitlines()[-1] == f"<code>{MINT_SOL}</code>"


# ============================================================
# 观点:推送文案
# ============================================================
class Test观点文案:
    def _render(self, **kw):
        from src.formatter import render_pump_callout

        base = dict(username="hexiecs", thesis="my bad fellas i got hypnotized",
                    coin_mint=MINT_SOL, token_symbol="PUNCHMA",
                    market_cap_usd=16379, multiple=0.256, likes=3, view_count=844,
                    created_at="2026-08-31T04:28:41+00:00", network_id="solana",
                    chain_display="Solana", now=1788150600.0)
        base.update(kw)
        return render_pump_callout(**base)

    def test_主干字段都在(self):
        out = self._render()
        assert "观点" in out
        assert "hexiecs" in out and "$PUNCHMA" in out
        assert "my bad fellas i got hypnotized" in out
        assert "💎 发表时市值 $16.38K" in out
        assert "📉 发表至今 ×0.26" in out
        assert "👍 3 · 👀 844" in out
        assert "2026-08-31 04:28 UTC" in out
        assert "🧬 Solana" in out

    def test_正文必须转义而且只转一次(self):
        """
        ⚠️⚠️ thesis 是**完全自由的用户输入**。不转义 = 一个 `<b>` 就让整条消息
           被 Telegram 400(未闭合标签),而且是**静默**的 —— 用户只会觉得"没推送"。
        ⚠️ 转两次同样错:`&` 会变成 `&amp;amp;`,读者看到一串乱码。
        """
        out = self._render(thesis="<b>看好</b> a&b \"quoted\"")
        assert "&lt;b&gt;" in out
        assert "<b>看好" not in out                       # 原始标签必须被吃掉
        assert "&amp;" in out and "&amp;amp;" not in out  # 只转一次
        assert out.count("<b>") == out.count("</b>")

    def test_正文里的换行被叠平(self):
        """
        ⚠️ 正文里塞几十个换行就能把一行变成几十行,绕开 _fit_signal
           「按整行算预算」这个前提 —— 那是把消息挤爆的最省事的办法。
        """
        out = self._render(thesis="第一行\n\n\n第二行\n第三行")
        assert "第一行 第二行 第三行" in out

    def test_超长正文被截断且整条消息仍在预算内(self):
        """
        ⚠️⚠️ 长度必须收口:接口对 thesis 不设限。截断点还必须在**转义之前**,
           否则会把一个 `&amp;` 切成残缺实体 —— 整条消息 400。
        """
        out = self._render(thesis="话" * 5000)
        assert len(out) <= 4000 - 400
        assert "…" in out
        # 写死字面量:正文最多 280 个字符 + 一个省略号
        body = next(ln for ln in out.splitlines() if ln.startswith("💬"))
        assert len(body) == len("💬 ") + 280 + 1

    def test_截断绝不切出残缺实体(self):
        """⚠️ 在 `&amp;` 中间切一刀 = 残缺实体 = 整条消息 400。"""
        out = self._render(thesis="&" * 400)
        assert "&amp" in out
        assert not out.rstrip("…").endswith("&am")
        for frag in ("&am;", "&a;", "&amp;;"):
            assert frag not in out

    def test_CA独占最后一行且完整(self):
        assert self._render().splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_预算被顶破时CA仍然完整且仍在最后一行(self, monkeypatch):
        from src import formatter

        monkeypatch.setattr(formatter, "TRANSFER_MSG_BUDGET", 60)
        out = self._render()
        assert len(out) <= 60
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_陌生人可控的超长文本撑不破预算(self):
        out = self._render(username="超长" * 5000, token_symbol="X" * 5000,
                           thesis="话" * 5000)
        assert len(out) <= 4000 - 400
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_HTML标签必须闭合(self):
        for out in (self._render(), self._render(username="<b>坏昵称</b>",
                                                 token_symbol="a&b",
                                                 thesis="</code>注入")):
            assert out.count("<b>") == out.count("</b>")
            assert out.count("<code>") == out.count("</code>")
            assert out.count("<a ") == out.count("</a>")

    def test_缺失字段整行消失绝不打占位符(self):
        out = self._render(market_cap_usd=None, multiple=None, likes=None,
                           view_count=None, created_at=None, network_id=None,
                           chain_display=None, token_symbol=None)
        assert "None" not in out and "N/A" not in out and "--" not in out
        assert "发表时市值" not in out and "发表至今" not in out
        assert "👍" not in out and "👀" not in out

    def test_点赞与浏览各自独立降级(self):
        """⚠️ 只拿到一个就只显示一个 —— 绝不给缺失的那个补 0。"""
        assert "👍 3" in self._render(view_count=None)
        assert "👀" not in self._render(view_count=None)
        assert "👀 844" in self._render(likes=None)
        assert "👍" not in self._render(likes=None)

    def test_点赞数是0照常显示(self):
        """⚠️ 0 是真实值(夹具里就有一条 likes=0),它与"没拿到"含义相反。"""
        assert "👍 0" in self._render(likes=0)

    def test_发表时市值为0时整行消失(self):
        """⚠️ 与买卖那条同一条:市值 0 更可能是上游算漏,打出来就是替读者断言归零。"""
        out = self._render(market_cap_usd=0)
        assert "发表时市值" not in out and "💎" not in out
        assert "my bad fellas" in out                     # 观点本身照推

    def test_倍数为0或负数时整行消失(self):
        """⚠️ 价格没有负的,×0 意味着现价恰好是 0 —— 与市值 0 同一条。"""
        for bad in (0, -1.5):
            out = self._render(multiple=bad)
            assert "发表至今" not in out, bad

    def test_倍数大于等于1用涨的emoji(self):
        assert "📈 发表至今 ×1.29" in self._render(multiple=1.2889)
        assert "📉 发表至今 ×0.26" in self._render(multiple=0.256)

    def test_措辞是发表时市值而不是市值(self):
        """
        ⚠️⚠️ 这个数来自 callout 自己的 marketCap,是他**按下发送键那一刻**的市值。
           写成「市值」会被读成当前值,而这条观点可能是几小时前发的 —— 那就是个错的数。
        """
        line = next(ln for ln in self._render().splitlines() if "市值" in ln)
        assert line == "💎 发表时市值 $16.38K"

    def test_绝不替用户断言意图(self):
        """
        ⚠️⚠️ 一条 callout 就是一句话,它不构成任何关于他仓位的证据
           (他可能一股没买,也可能早就卖光了)。
        """
        out = self._render()
        for banned in ("建仓", "跑路", "该跟", "抄底", "看好", "喊单", "共识",
                       "准备", "可能", "持有"):
            assert banned not in out, banned

    def test_行首锚点与买卖那条不重样(self):
        """
        ⚠️ 铁律 1:行首锚点全局唯一。在聊天列表预览里认错锚点就是认错消息类型 ——
           「他成交了」与「他说了句话」的分量完全不同。
        """
        from src.formatter import render_pump_trade

        trade = render_pump_trade(username="hexiecs", side="buy", token_symbol="P",
                                  coin_mint=MINT_SOL, amount_usd=1.0)
        assert self._render()[0] != trade[0]

    def test_链接覆盖不到的链只丢链接行其余照推(self):
        out = self._render(network_id=None, chain_display="Arbitrum")
        assert "Arbitrum" in out
        assert "gmgn.ai" not in out and "fomo.family" not in out
        assert "my bad fellas" in out


# ============================================================
# 💎 市值恰好为 0(A2)
# ============================================================
class Test市值为零:
    """
    ⚠️⚠️ 这**不是**"拿真值判断代替 is None":取值一路仍然只用 is None 判缺失
       (0 与缺失在解析层分得很清楚,有用例钉着)。这里判的是另一件事 ——
       **这一行说不出口**:市值 0 的含义是"全部流通份额加起来一分钱不值",
       而同一条消息里紧挨着的是一笔以非零单价成交的真实交易,两者直接矛盾。
       现实里它几乎只可能是上游算漏,而这一行会打出
       「💎 市值 $0.00 · 距最高 -100.0%」= 替读者断言"这币归零了"。
    """

    def _render(self, **kw):
        from src.formatter import render_pump_trade

        base = dict(username="hexiecs", side="buy", token_symbol="PUNCHMA",
                    coin_mint=MINT_SOL, amount_usd=1234.5, price_usd=0.0000285,
                    market_cap_usd=445526.7119903613,
                    ath_market_cap_usd=2116892.897778326,
                    traded_at="2026-08-31T01:59:02.000Z", network_id="solana",
                    tx="SIG_ABC", now=1787193542.0)
        base.update(kw)
        return render_pump_trade(**base)

    def test_市值恰好为0时整行消失(self):
        out = self._render(market_cap_usd=0)
        assert "💎" not in out and "市值" not in out
        assert "距最高" not in out
        for ln in out.splitlines():
            assert ln != "💎 市值 $0.00"

    def test_市值为0时绝不打出距最高负100(self):
        """⚠️ 这才是最贵的那半句:两个数字连起来就是"归零了"这句结论。"""
        out = self._render(market_cap_usd=0, ath_market_cap_usd=2116892.9)
        assert "-100.0%" not in out and "距最高" not in out

    def test_市值为负同样整行消失(self):
        """⚠️ 市值没有负的,那只可能是脏数据。"""
        assert "💎" not in self._render(market_cap_usd=-1.0)

    def test_成交本身照推(self):
        """⚠️ 少一行是"我们没说";把整条消息扣下才是过度反应。"""
        out = self._render(market_cap_usd=0)
        assert "💰 金额 $1,234.50" in out
        assert out.splitlines()[-1] == f"<code>{MINT_SOL}</code>"

    def test_粉尘级但非零的市值照常显示(self):
        """
        ⚠️ 对照组:证明砍掉的只是 ≤ 0 那一个点,不是"小额一律不显示"。
           $0.01 的市值是个可怜但**可证**的事实,照常摆出来。
        """
        assert "💎 市值 $0.01" in self._render(market_cap_usd=0.01,
                                              ath_market_cap_usd=None)


# ============================================================
# 配置上界(A3)
# ============================================================
class Test配置上界:
    """
    ⚠️⚠️ 一个变动的 mint 现在要花 2 个请求(逐笔 + 市值),补市值之前只花 1 个 ——
       同一个 max_mints 的真实成本已经翻倍。frontend-api-v3 实测 60/分,
       巡检间隔下限 30 秒 = 2 轮/分,于是 mint 那一路每分钟吃 2 × max_mints。
       没有上界时手写 max_mints=60 + 间隔 30 就是 120/分,直接打穿。
    ⚠️ 断言写死字面量(15 / 16),不从 config 里取 —— 从被测模块取等于 x == x。
    """

    def test_超过上界的配置直接拒绝而不是跑到一半才发现(self):
        from pydantic import ValidationError

        from src.config import FomoSettings

        with pytest.raises(ValidationError):
            FomoSettings(fomo_pump_max_mints=60)
        with pytest.raises(ValidationError):
            FomoSettings(fomo_pump_max_mints=16)

    def test_上界之内照常接受(self):
        from src.config import FomoSettings

        assert FomoSettings(fomo_pump_max_mints=15).fomo_pump_max_mints == 15
        assert FomoSettings(fomo_pump_max_mints=1).fomo_pump_max_mints == 1

    def test_下界仍然是1(self):
        from pydantic import ValidationError

        from src.config import FomoSettings

        with pytest.raises(ValidationError):
            FomoSettings(fomo_pump_max_mints=0)

    def test_巡检间隔下限没被改动(self):
        """⚠️ 上界 15 是**从这个下限反推**出来的,它一变上界的依据就没了。"""
        from pydantic import ValidationError

        from src.config import FomoSettings

        with pytest.raises(ValidationError):
            FomoSettings(fomo_pump_interval_sec=29)
        assert FomoSettings(fomo_pump_interval_sec=30).fomo_pump_interval_sec == 30

    def test_两个开关默认都是关的(self):
        """⚠️ 老库升级后行为必须逐字节不变:谁也不该因为拉了个新版本就多收一路推送。"""
        from src.config import FomoSettings

        s = FomoSettings()
        assert s.fomo_pump_enabled is False
        assert s.fomo_pump_callout_enabled is False

    def test_观点窗口与成交窗口是两个独立的值(self):
        """⚠️ 共用一个值意味着调其中一个功能的灵敏度会**静默**改掉另一个。"""
        from src.config import FomoSettings

        s = FomoSettings(fomo_pump_trade_max_age_sec=111,
                         fomo_pump_callout_max_age_sec=222)
        assert s.fomo_pump_trade_max_age_sec == 111
        assert s.fomo_pump_callout_max_age_sec == 222


# ============================================================
# 老库迁移
# ============================================================
def test_老库升级后补上callout_seeded列且默认为0(tmp_path):
    """
    ⚠️⚠️ CREATE TABLE IF NOT EXISTS 不会给已存在的表补列,不迁移就是
       升级后直接 `no such column: callout_seeded`。
    ⚠️ 默认 **0**:老库里的人一律当成"观点还没播过种",第一轮只记水位线。
       默认 1 的话,升级那一刻每个人的历史观点会一次性倒出来。
    """
    import sqlite3

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    # 造一张**旧版**的 pump_watch_users(没有 callout_seeded 列)
    conn.execute("""
        CREATE TABLE pump_watch_users (
            user_id TEXT PRIMARY KEY, username TEXT, svm_wallet TEXT, evm_wallet TEXT,
            added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            removed_at TEXT, seeded INTEGER NOT NULL DEFAULT 0)
    """)
    conn.execute("INSERT INTO pump_watch_users (user_id, username, added_at, seeded) "
                 "VALUES ('u-old', 'hexiecs', '2026-08-01T00:00:00+00:00', 1)")
    store.init_db(conn)
    row = conn.execute("SELECT * FROM pump_watch_users").fetchone()
    assert row["seeded"] == 1, "老数据不能被迁移改掉"
    assert row["callout_seeded"] == 0
    conn.close()
