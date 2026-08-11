"""
models.py 归一化纯函数的单测。

这一层是整个项目的地基:聚合键 (network_id, token_address) 错一点,
功能 A(首次买入)与功能 B(共识计数)就整体算错,而且是**静默算错** ——
不会抛异常,只会给出一个看起来很正常但完全错误的数字。

对应验收用例:#4(拆单去重)、#7(计价币排除)。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单,
# 排查线上问题时能直接对到设计文档的条目。ruff 的 N802 只认 ASCII 小写,这里整文件豁免。
from __future__ import annotations

import pytest

from src.models import (
    BADGE_FIRST,
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    FomoEvent,
    dump_raw,
    is_quote_token,
    make_event_id,
    normalize_network,
    normalize_token_address,
    pick,
    to_iso,
)

from .conftest import CA_CATE, CA_CATE_CHECKSUM, CA_TOAD, CA_WSOL, make_event


# ============================================================
# normalize_network
# ============================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("solana", "solana"),
        ("SOL", "solana"),          # 大小写 + 简称
        (1399811149, "solana"),     # 数值 chainId
        ("1399811149", "solana"),   # 字符串 chainId —— 与上一行必须归到同一个值
        ("Base", "base"),
        (8453, "base"),
        ("BNB", "bsc"),
        ("binance-smart-chain", "bsc"),
        (56, "bsc"),
        ("ETH", "ethereum"),
        (1, "ethereum"),
    ],
)
def test_链标识别名映射到统一取值(raw, expected):
    """
    ⚠️ 同一条链在 swaps / balances / transfers 三处若返回不同表示(如 "SOL" 与 1399811149),
       不归一化就会裂成两个聚合键 —— 同一个币被当成两个币,共识计数直接算错。
    """
    assert normalize_network(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arbitrum", "arbitrum"),   # 未知链:原样返回小写,而不是丢弃
        (42161, "42161"),
        ("  Polygon  ", "polygon"),  # 前后空白必须去掉
    ],
)
def test_未知链原样返回小写而不是丢弃(raw, expected):
    """
    丢弃(返回 None)会让未知链的事件永远拿不到聚合键、功能 A/B 全废;
    原样返回小写至少保证"同一来源内部自洽",新链上线时不需要改代码。
    """
    assert normalize_network(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_链标识缺失返回None(raw):
    """None / 空串必须返回 None,否则会构造出 ('', ca) 这种垃圾聚合键混进 stats 表。"""
    assert normalize_network(raw) is None


# ============================================================
# normalize_token_address
# ============================================================
def test_EVM地址必须转小写():
    """
    ⚠️ EVM hex 地址大小写不敏感,API 可能返回 EIP-55 checksum 混合大小写。
       不 lower() 的话同一个币在 user_token_stats 里裂成两行,
       共识数会凭空少一半,而且永远不会报错。
    """
    assert normalize_token_address(CA_CATE_CHECKSUM) == CA_CATE
    assert normalize_token_address(CA_CATE_CHECKSUM).islower()
    # 幂等:已经是小写的再归一化一次不能变
    assert normalize_token_address(CA_CATE) == CA_CATE


def test_Solana地址绝不能被小写():
    """
    ⚠️ Solana 是 base58,**大小写敏感**。lower() 之后就是一个不存在的地址,
       这条测试是防止有人"顺手统一小写"的最后一道防线。
    """
    assert normalize_token_address(CA_TOAD) == CA_TOAD
    assert normalize_token_address(CA_WSOL) == CA_WSOL


def test_非42位0x开头的地址不做小写():
    """
    判据是"地址自身的编码形态(0x + 42 位)",不是链名白名单。
    长度不对说明它不是 EVM 地址(可能是 tx hash 或别的东西),原样返回不做假设。
    """
    assert normalize_token_address("0xABCdef") == "0xABCdef"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_代币地址缺失返回None(raw):
    assert normalize_token_address(raw) is None


def test_代币地址前后空白被去掉():
    assert normalize_token_address(f"  {CA_CATE_CHECKSUM}  ") == CA_CATE


# ============================================================
# is_quote_token —— 验收用例 #7
# ============================================================
@pytest.mark.parametrize(
    ("net", "ca"),
    [
        ("solana", CA_WSOL),                                              # WSOL
        ("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),       # Solana USDC
        ("base", "0x4200000000000000000000000000000000000006"),           # Base WETH
        ("bsc", "0x55d398326f99059ff775485246999027b3197955"),            # BSC USDT
        ("ethereum", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"),       # ETH WETH
    ],
)
def test_计价币命中白名单(net, ca):
    """
    验收用例 #7:swap 另一侧是 SOL/USDC 时,该侧不打徽章、不算共识。
    不排除的话 $SOL 的共识数恒等于名单人数,功能 B 整体变噪音。
    """
    assert is_quote_token(net, ca) is True


@pytest.mark.parametrize(
    ("net", "ca"),
    [
        ("solana", CA_TOAD),   # meme 币
        ("base", CA_CATE),
        ("bsc", CA_CATE),      # 同一个 CA 换条链也不是计价币
    ],
)
def test_meme币不命中计价币白名单(net, ca):
    assert is_quote_token(net, ca) is False


def test_原生币占位地址在任何链上都算计价币():
    """0x0 / 0xEeee... / Solana System Program 是各家 API 表示"原生币"的惯例写法,与链无关。"""
    assert is_quote_token("base", "0x0000000000000000000000000000000000000000") is True
    assert is_quote_token("bsc", "0xEeEeEEEeeEEeeEeEEeEEeEEEeeeeEeeeeeeeEEeE") is True
    assert is_quote_token("solana", "11111111111111111111111111111111") is True
    # 占位地址判定不依赖 network_id —— 未知链也要拦住
    assert is_quote_token(None, "0x0000000000000000000000000000000000000000") is True


def test_计价币白名单按链地址判定而不是按symbol():
    """
    ⚠️ 链上假 USDC / 假 SOL 遍地。按 symbol 判会把一个真的 meme 币误当计价币直接排除,
       用户会发现"这个币的推送永远没有共识行"却查不出原因。
    """
    fake_usdc_on_solana = "USDCfakeMintAddress1111111111111111111111111"
    assert is_quote_token("solana", fake_usdc_on_solana) is False


def test_计价币白名单要求地址已归一化():
    """
    白名单键是 normalize_token_address 的输出形态。传入未归一化的 checksum 大写地址会漏判 ——
    这条测试把"调用 is_quote_token 前必须先归一化"这个隐含契约钉死。
    """
    # BSC WBNB,checksum 形态(含大写字母)
    wbnb_checksum = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    assert wbnb_checksum != wbnb_checksum.lower()          # 确认样本确实含大写
    assert is_quote_token("bsc", wbnb_checksum) is False
    assert is_quote_token("bsc", normalize_token_address(wbnb_checksum)) is True


def test_地址缺失时不算计价币():
    """地址缺失走的是 no_token_key 降级路径,不能被误判成 quote_token —— 两者的 reason 不同。"""
    assert is_quote_token("solana", None) is False
    assert is_quote_token(None, None) is False


# ============================================================
# to_iso —— 秒 / 毫秒 / 微秒 / ISO 四种形态
# ============================================================
# 2023-11-14T22:13:20Z 的四种写法
_EXPECT = "2023-11-14T22:13:20+00:00"


@pytest.mark.parametrize(
    ("raw", "desc"),
    [
        (1700000000, "秒"),
        (1700000000000, "毫秒"),
        (1700000000000000, "微秒"),
        ("1700000000", "秒(字符串)"),
        (1700000000.0, "秒(浮点)"),
        ("2023-11-14T22:13:20Z", "ISO+Z"),
        ("2023-11-14T22:13:20+00:00", "ISO+offset"),
        ("2023-11-14T22:13:20", "ISO 无时区(按 UTC 解)"),
    ],
)
def test_时间戳四种形态都能解成同一个UTC时刻(raw, desc):
    """
    ⚠️ 秒/毫秒判错会导致"永远拉不到新数据"的静默失效 —— 程序照常跑、日志照常刷,
       就是一条推送都没有。这是本项目最危险的一类 bug。
    """
    iso, fallback = to_iso(raw)
    assert fallback is False, f"{desc} 形态被误判为兜底"
    assert iso == _EXPECT, f"{desc} 形态解析结果不对"


@pytest.mark.parametrize(
    ("raw", "desc"),
    [
        (1, "1970-01-01(纪元附近)"),
        (100000, "1970-01-02"),
        (1577836799, "2019-12-31,刚好在下界之外"),
        ("1970-01-01T00:00:00+00:00", "1970 ISO"),
        (99999999999999999999, "公元 55000 年之后,三种量纲都越界"),
        ("2999-01-01T00:00:00+00:00", "公元 2999 年 ISO"),
    ],
)
def test_越界时间戳一律拒绝走兜底降级(raw, desc):
    """
    合理性断言区间是 [2020-01-01, now+1d]。越界返回 (None, True),
    调用方据此走"时间戳缺失"降级并**不写 first_buy_at** —— 一个 1970 年的
    first_buy_at 会永久占据 MIN() 的位置,再也修不回来。
    """
    iso, fallback = to_iso(raw)
    assert iso is None, f"{desc} 应被拒绝却解析出了 {iso}"
    assert fallback is True


@pytest.mark.parametrize("raw", [None, "", 0, -1, "abc", "2023-13-45", {}, []])
def test_无法解析的时间戳返回兜底标记(raw):
    """任何解析不了的输入都必须返回 (None, True),**绝不能抛异常炸掉主流程**。"""
    iso, fallback = to_iso(raw)
    assert iso is None
    assert fallback is True


def test_下界正好在2020年元旦():
    """边界值本身必须被接受,否则区间描述与实现对不上。"""
    iso, fallback = to_iso("2020-01-01T00:00:00+00:00")
    assert fallback is False
    assert iso == "2020-01-01T00:00:00+00:00"


def test_非UTC时区被转换成UTC():
    """全项目时序比较只认 UTC ISO,带偏移量的输入必须换算,不能原样存。"""
    iso, fallback = to_iso("2023-11-15T06:13:20+08:00")
    assert fallback is False
    assert iso == _EXPECT


# ============================================================
# make_event_id —— 验收用例 #4(拆单)
# ============================================================
def test_有原生id时直接用原生id():
    """API 给了稳定 id 就用它,这是最可靠的去重键,不该再去哈希一堆可能缺失的字段。"""
    assert make_event_id("BUY", "swap_123", user_id="u1") == "BUY:swap_123"


def test_原生id相同但事件类型不同时id不冲突():
    """swaps 与 transfers 的原生 id 可能撞号,前缀 kind 是必须的。"""
    assert make_event_id("BUY", "1", user_id="u1") != make_event_id("TRANSFER_IN", "1", user_id="u1")


def test_验收4_等额同秒拆单四条必须产生四个不同id():
    """
    验收用例 #4:一笔买入被 DEX 路由拆成 4 条**等额、同秒、同币、同 tx** 的 swap,
    且 API 没给原生 id。

    ⚠️ 这是本项目最贵的一个 bug:兜底 hash 不掺页内序号的话,
       四条会哈希成同一个 event_id → 被 INSERT OR IGNORE 误去重 →
       只落库 1 条 → **金额少报 75%**,而且日志里一点异常都没有。
    """
    ids = {
        make_event_id(
            "BUY", None,
            user_id="u1",
            tx_hash="0xsametx",          # 同一笔交易,tx_hash 完全一样
            token_address=CA_TOAD,
            event_ts="2026-08-11T10:00:00+00:00",
            amount="625.0",              # 等额分片
            page_index=i,                # ← 唯一的区分量
        )
        for i in range(4)
    }
    assert len(ids) == 4, "四条等额同秒拆单被哈希成了同一个 id,金额会少报 75%"


def test_同一笔重复拉取时id必须稳定():
    """
    id 不稳定 = INSERT OR IGNORE 失效 = 每轮轮询都重复推送同一笔 +
    buy_count 一路虚增,徽章逻辑随之全面失真。
    """
    kw = dict(
        user_id="u1", tx_hash="0xabc", token_address=CA_TOAD,
        event_ts="2026-08-11T10:00:00+00:00", amount="1000", page_index=2,
    )
    assert make_event_id("BUY", None, **kw) == make_event_id("BUY", None, **kw)


@pytest.mark.parametrize(
    "diff",
    [
        {"tx_hash": "0xother"},
        {"token_address": CA_CATE},
        {"event_ts": "2026-08-11T10:00:01+00:00"},
        {"amount": "1001"},
        {"user_id": "u2"},
    ],
)
def test_任一分量不同则id不同(diff):
    """
    每个分量都必须真正参与哈希。少掺一个(尤其是 tx_hash)就会出现"两笔不同的交易被当成同一笔"。
    """
    base = dict(
        user_id="u1", tx_hash="0xabc", token_address=CA_TOAD,
        event_ts="2026-08-11T10:00:00+00:00", amount="1000", page_index=0,
    )
    assert make_event_id("BUY", None, **base) != make_event_id("BUY", None, **{**base, **diff})


def test_兜底id带h标记便于排查():
    """带 ':h:' 前缀,一眼能看出这条是"API 没给 id、我们自己哈希的",排查去重问题时很关键。"""
    eid = make_event_id("BUY", None, user_id="u1", tx_hash="0xabc")
    assert eid.startswith("BUY:h:")


# ============================================================
# FomoEvent 派生属性
# ============================================================
def test_聚合键任一分量缺失都返回None():
    """构造不出键就不打徽章、不算共识 —— 但事件照常落库照常推送(降级,不是丢弃)。"""
    assert make_event(network_id="solana", token_address=CA_TOAD).token_key == ("solana", CA_TOAD)
    assert make_event(network_id=None).token_key is None
    assert make_event(token_address=None).token_key is None
    assert make_event(network_id="", token_address="").token_key is None


def test_is_quote属性直通计价币判定():
    assert make_event(network_id="solana", token_address=CA_WSOL).is_quote is True
    assert make_event(network_id="solana", token_address=CA_TOAD).is_quote is False


@pytest.mark.parametrize(
    ("kw", "expected", "why"),
    [
        ({}, True, "数据齐全的买入"),
        ({"event_type": EVENT_SELL}, False, "卖出不计入 buy_count"),
        ({"event_type": EVENT_THESIS}, False, "观点不是交易"),
        ({"side_unknown": True}, False, "方向不明:宁可不计,也不能把卖出算成买入"),
        ({"token_address": None}, False, "无聚合键"),
        ({"network_id": None}, False, "无聚合键"),
        ({"token_address": CA_WSOL}, False, "计价币不进 stats"),
    ],
)
def test_可计数买入的四道门(kw, expected, why):
    """
    countable_buy 是"能不能写进 user_token_stats"的唯一判据。
    放宽任何一道门都会污染共识分子:计价币放行 → $SOL 共识恒等于名单人数;
    方向不明放行 → 卖出被当成买入,徽章打在一笔清仓上。
    """
    assert make_event(**kw).countable_buy is expected, why


def test_to_row只输出落库字段():
    """
    展示字段(市值/持仓/观点正文)与内存标志(ts_fallback/side_unknown)不在 fomo_events 表里。
    多输出一个键 insert_event 的具名参数绑定就会报错。
    """
    ev = make_event(market_cap=1.9e7, holding_usd=2498.1, ts_fallback=True, side_unknown=True)
    row = ev.to_row()
    assert set(row) == {
        "event_id", "event_type", "user_id", "handle", "user_handle",
        "network_id", "token_address", "token_symbol", "amount_usd", "token_amount",
        "price_usd", "tx_hash", "event_ts", "ingested_at",
        "badge", "badge_reason", "raw_json",
    }
    assert "market_cap" not in row
    assert "ts_fallback" not in row
    # 盈亏是展示字段(每 tick 从 /trades 重新拿),不落库
    assert "realized_pnl" not in row
    assert "unrealized_pnl" not in row


def test_徽章字段默认为空():
    """徽章必须由 store.judge_badge 显式写入,dataclass 默认值绝不能是 FIRST。"""
    ev = make_event()
    assert ev.badge is None and ev.badge_reason is None
    ev.badge = BADGE_FIRST
    assert ev.to_row()["badge"] == BADGE_FIRST


# ============================================================
# 辅助函数
# ============================================================
def test_多键兜底按优先级取第一个非None():
    """字段名全是逆向猜的,pick 是"猜错了也不崩"的唯一保障。"""
    raw = {"chainId": 8453, "network": "base"}
    assert pick(raw, "networkId", "chainId", "network") == 8453
    assert pick(raw, "networkId", "network") == "base"
    assert pick(raw, "networkId", default="?") == "?"
    # 值为 None 的键要继续往后找,不能当成"命中了"
    assert pick({"networkId": None, "chainId": 56}, "networkId", "chainId") == 56
    # 非 dict 输入不能抛异常
    assert pick(None, "networkId", default="?") == "?"
    assert pick([1, 2], "networkId") is None


def test_原始报文序列化不会因为不可序列化对象崩掉():
    """raw_json 是 NOT NULL 列。序列化抛异常就等于整条事件落不了库,必须兜住。"""
    assert '"a": 1' in dump_raw({"a": 1})

    class Weird:
        def __repr__(self):
            return "<weird>"

    out = dump_raw({"x": Weird()})
    assert isinstance(out, str) and out  # 不抛异常、且不是空串


def test_中文与emoji不被转义成unicode码点():
    """ensure_ascii=False:观点正文全是中文,转义后原始报文的可读性归零。"""
    assert "筹码" in dump_raw({"t": "筹码很干净🌱"})


def test_事件类型常量是设计文档里的五个():
    """常量拼错(比如 "buy" 小写)会让 judge_badge 的 event_type 判断全部落空,徽章永远不出现。"""
    assert EVENT_BUY == "BUY"
    ev = FomoEvent(event_id="x", event_type=EVENT_BUY, user_id="u", event_ts="2026-01-01T00:00:00+00:00",
                   raw_json="{}")
    assert ev.ingested_at  # 默认工厂必须填上,NOT NULL 列
