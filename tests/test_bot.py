"""
/ca <合约地址> 的单测。

⚠️ 这条命令有两段互相独立的信息:
     观点区 —— 实时查 client.get_token_thesis,任何地址都能查(常见情况:本地压根不认识这个币)。
     本地名单区 —— 查本地库,大多数地址查不到,必须显式说"无人持有"而不是留空或崩溃。
   本文件盯的重点:两段各自的"没有"能不能正确渲染、HTML 转义会不会导致 400、
   排序/展示上限对不对、异常会不会崩到线程外面去。
"""
# ruff: noqa: N802
from __future__ import annotations

import re

import pytest

from src.client import FomoAPIError
from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

# 与任务描述里同一个真实探测过的地址(BSC,chainid 56)
CA_BSC = "0xfc6e0617a6cc19afe9e0d367e97d07245d357777"
CA_SOL = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"

# html.escape(quote=True) 只会产出这五种实体。出现别的 `&` 开头片段 = 有实体被切断了,
# 而残缺实体让 TG 整条消息 400 —— 用户什么都收不到,只在日志里留一行。
_BROKEN_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27|#39);)")


def _assert_tg_safe(out: str) -> None:
    """这条消息能被 TG 的 HTML 子集吃下去:没有残缺实体、标签成对、CA 锚点还在最后一行"""
    m = _BROKEN_ENTITY.search(out)
    assert m is None, f"残缺 HTML 实体 @{m.start() if m else -1}: {out[max(0, (m.start() if m else 0) - 30):][:70]!r}"
    assert out.count("<code>") == out.count("</code>"), "<code> 没闭合"
    assert out.count("<b>") == out.count("</b>"), "<b> 没闭合"
    assert out.endswith("</code>"), f"CA 锚点必须活到最后一行,实际结尾: {out[-80:]!r}"


class _FakeClient:
    """按 network_id 返回预设的 thesis 列表;raise_exc 给了就每次调用都抛它"""

    def __init__(self, by_net: dict[str, list[dict]] | None = None, *, raise_exc=None):
        self.by_net = by_net or {}
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, str, int]] = []

    def get_token_thesis(self, token_address, network_id, after_ms=None, limit=100):
        self.calls.append((token_address, network_id, limit))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.by_net.get(network_id, [])


def _item(handle="alice", usd=100.0, pnl=10.0, pct=10.0, text="不错的一个币",
         ticker="ELOY", network="bsc", user_id=None,
         realized=0.0, realized_pct=0.0, closed_at=None,
         created_at="2026-08-20T00:00:00.000Z"):
    """
    构造一条 get_token_thesis 条目。

    ⚠️ comment 字段的真实形状是**嵌套 dict**(2026-08-24 对真实 API 实测确认,
       ticker/networkId 等其余字段确实在顶层):
         {"comment": {"comment": "正文", "tokenAddress": ..., "networkId": ...}, ...}
       与最初任务描述里"comment 就是正文字符串"这个印象不一致 —— 按实测数据为准。
       _ca_thesis_text 两种形状都认,见 test_观点正文是嵌套comment字典时也能取到正文。

    ⚠️ realized/closed_at 必须可参数化:这三个字段曾被写死成
       (0.0, 0.0, None) —— 恰好是唯一能掩盖"已清仓的人被渲染成不赚不亏"那个 bug 的
       取值,于是整个 fixture 在替实现打掩护。已清仓的用例见
       test_已清仓的人显示已实现盈亏而不是凭空的0。
    """
    return {
        "ticker": ticker, "tokenAddress": CA_BSC, "networkId": network,
        "comment": {"comment": text, "tokenAddress": CA_BSC, "networkId": network},
        "displayName": handle, "userHandle": handle,
        "userId": user_id or handle, "createdAt": created_at,
        "numReplies": 0, "isDev": False, "threshold": 0,
        "authorTrade": {
            "usdValue": usd, "humanTokenAmount": 1.0,
            "unrealizedPnlUsd": pnl, "percentageUnrealizedPnl": pct,
            "realizedPnlUsd": realized, "percentageRealizedPnl": realized_pct,
            "closedAt": closed_at,
        },
    }


def _closed(handle, realized, realized_pct, closed_at="2026-08-11T01:20:35.163Z", **kw):
    """
    已清仓的一条观点 —— 形状照抄 data/fomo_probe/thesis.json 里的真实数据:
    usdValue / unrealizedPnlUsd / percentageUnrealizedPnl 全是 0,盈亏落在 realized 那两个键上。
    """
    return _item(handle=handle, usd=0.0, pnl=0.0, pct=0.0,
                 realized=realized, realized_pct=realized_pct, closed_at=closed_at, **kw)


def _thesis_line_of(out: str, handle: str) -> str:
    """取出 @handle 那一行,用来做逐字符断言 —— 只断言子串会漏掉"多了/少了一段"这类回归"""
    for ln in out.split("\n"):
        if ln.startswith(f"@{handle} ") or ln == f"@{handle}":
            return ln
    raise AssertionError(f"输出里没有 @{handle} 这一行:\n{out}")


def _bot(monkeypatch, tmp_path, client=None):
    from src import store
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ca.db")
    store.init_db()
    b = CommandBot(client=client, notifier=None)
    return b, store


# ============================================================
# 常见情况:本地没人碰过,只有观点区
# ============================================================
def test_无人持有时观点区仍正常渲染(monkeypatch, tmp_path):
    """这是最常见的路径(粘一个刚在 Twitter 看到的地址),必须能正常出结果而不是报错"""
    client = _FakeClient({"56": [_item(handle="Ott0", usd=28.52, pnl=10.72, pct=60.25)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "$ELOY" in out
    assert "BNB Chain" in out
    assert "@Ott0" in out
    assert "$28.52" in out
    assert "+$10.72" in out
    assert "+60.3%" in out or "+60.2%" in out or "+60.25%" in out  # 只要求带正负号且是这个量级
    assert "无人持有" in out
    assert CA_BSC in out


# ============================================================
# 名单确实买过:两段都要有内容
# ============================================================
def test_名单买过时两段都显示(monkeypatch, tmp_path):
    """名单里的人买过 → 本地名单区要报出人、进场市值、现在的倍数;观点区照常独立工作"""
    client = _FakeClient({"56": [_item(handle="Ott0", usd=28.52, pnl=10.72, pct=60.25)]})
    b, store_ = _bot(monkeypatch, tmp_path, client)

    with store_.get_conn() as conn:
        store_.add_watch_user(conn, "u1", "bob", "Bob")
        store_.mark_stats_ready(conn, "u1")
        ev = FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=500.0, market_cap=100_000.0, badge_reason=REASON_LOCAL_STATS,
            user_handle="bob",
        )
        store_.insert_event(conn, ev)
        # 先冲到 50 万再回落到 30 万,顺带验证"峰值"那一行
        store_.upsert_token_snapshots(conn, [("bsc", CA_BSC, "ELOY", 1.0, 500_000.0)])
        store_.upsert_token_snapshots(conn, [("bsc", CA_BSC, "ELOY", 1.0, 300_000.0)])

    out = b._cmd_ca(CA_BSC)   # 不给链:本地已经认识,应当自动定链到 bsc

    assert "@Ott0" in out              # 观点区
    assert "@bob" in out               # 本地名单区
    assert "无人持有" not in out
    assert "3.0x" in out               # 300,000 / 100,000 进场市值
    assert "峰值" in out and "500.00K" in out
    assert client.calls == [(CA_BSC, "56", 30)], "本地已认识这条链,不该再去猜别的链"


# ============================================================
# HTML 转义:必须转一次,不能不转、也不能转两次
# ============================================================
def test_观点正文转义且只转义一次_换行被压平(monkeypatch, tmp_path):
    raw_text = "<script>alert(1)</script> & good\nsecond line"
    client = _FakeClient({"56": [_item(handle="evil", text=raw_text)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "<script>" not in out, "原始标签必须被转义掉,否则 TG 直接 400"
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "&amp;amp;" not in out, "不能被转义两次"
    # 换行必须叠平成空格,否则正文会在表格里另起一行、把排版撑坏
    assert "good second line" in out
    assert "\nsecond line" not in out.split("good")[-1][:20]


def test_观点正文是嵌套comment字典时也能取到正文(monkeypatch, tmp_path):
    """默认 _item() 已经是嵌套形状(2026-08-24 对真实 API 实测确认),这里显式点名验证"""
    it = _item(handle="nested", text="嵌套的正文")
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "嵌套的正文" in out


def test_观点正文是扁平字符串时也能取到正文(monkeypatch, tmp_path):
    """comment 字段也可能是扁平字符串(最初任务描述里的形状)—— 两种形状都不能漏"""
    it = _item(handle="flat")
    it["comment"] = "扁平的正文"
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "扁平的正文" in out


# ============================================================
# 缺失 vs 真实的 0 —— formatter.py 头部铁律在这里同样适用
# ============================================================
def test_authorTrade字段缺失时该行消失而不是显示0(monkeypatch, tmp_path):
    it = {
        "ticker": "ELOY", "networkId": "bsc", "comment": "还行",
        "userHandle": "nodata", "displayName": "nodata",
        # 没有 authorTrade —— 整个持仓/盈亏都拿不到
    }
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "@nodata" in out
    assert "还行" in out
    assert "$0.00" not in out, "拿不到不能显示成 0,那会被读成'空仓/不赚不亏'这个假结论"
    assert "None" not in out
    assert "N/A" not in out
    assert "--" not in out


def test_持仓恰好为0仍显示0点00(monkeypatch, tmp_path):
    """
    0 是有意义的真实值(刚好卖光但仓位还没关),和"拿不到"是两回事,
    不能被 `if usd:` 那类真值判断误吞。

    ⚠️ 断言必须钉**整行**:只写 `assert "$0.00" in out` 的话,把实现里的 `is None`
       改成真值判断照样全绿 —— 因为盈亏那半段会自己贡献一个 "$0.00"。
       这个测试本该守住的正是这条铁律,却在空转。
    """
    it = _item(handle="soldout", usd=0.0, pnl=-12.34, pct=-5.0)
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "soldout") == "@soldout · $0.00 · 未实现 -$12.34 (-5.0%)"


def test_盈亏恰好为0仍显示0点00(monkeypatch, tmp_path):
    """盈亏正好打平也是真实值(刚开仓、或买卖抵消),同样不能被真值判断吞掉"""
    it = _item(handle="flat", usd=123.45, pnl=0.0, pct=0.0)
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "flat") == "@flat · $123.45 · 未实现 +$0.00 (+0.0%)"


# ============================================================
# 排序与展示上限
# ============================================================
def test_按投入本金从大到小排序(monkeypatch, tmp_path):
    """浮盈为 0 时本金 == 持仓额,这条只钉"大的在前"这个方向"""
    items = [
        _item(handle="low", usd=10.0, pnl=0.0, pct=0.0),
        _item(handle="high", usd=500.0, pnl=0.0, pct=0.0),
        _item(handle="mid", usd=100.0, pnl=0.0, pct=0.0),
    ]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.index("@high") < out.index("@mid") < out.index("@low"), \
        f"本金应从大到小排(500 > 100 > 10):\n{out}"


def test_清仓者按本金排序而不是按卖完之后剩下的0(monkeypatch, tmp_path):
    """
    usdValue 对已清仓的人**恒等于 0** —— 那是他卖完之后的剩余,不是他的仓位规模。
    拿剩余当规模排序等于把所有清仓者钉死在队尾:真实抓包 100 条按 usdValue 排,
    6 个清仓者整整齐齐落在 #95–#100,而展示上限只有 8 行 ——
    于是"已清仓者显示已实现盈亏"那条修复在真实数据上一行都渲染不出来。

    ⚠️ 验收标准**不是**"清仓者必须排前面"。巨鲸拿着几万、清仓者本金才一千,
       巨鲸排前面是**对的**,硬把清仓者塞进来反而是错的。标准是两类人可比:
       本金 $5,000 的清仓者必须能压过一个只拿着 $50 的人,同时压不过真正的巨鲸。
    """
    items = [
        # 本金 $5,000 翻倍后全部卖出 → realized 5000 / +100%,反推本金 5000
        _closed("sold5000", 5000.0, 100.0),
        # 还拿着 $50、浮盈 0 → 本金 $50。旧键下他 50 > 0,能把清仓者压在下面
        _item(handle="holds50", usd=50.0, pnl=0.0, pct=0.0),
        # 真巨鲸:在持成本 34,615(= 51,474.66 - 16,859.28),比谁都大
        _item(handle="whale", usd=51474.66, pnl=16859.28, pct=48.7),
    ]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.index("@sold5000") < out.index("@holds50"), \
        f"本金 $5,000 的清仓者必须排在只拿着 $50 的人前面:\n{out}"
    assert out.index("@whale") < out.index("@sold5000"), \
        f"本金最大的巨鲸仍然排第一 —— 不能为了照顾清仓者把他挤下去:\n{out}"


def test_本金算不出来的人垫底而不是被当成没投过钱(monkeypatch, tmp_path):
    """
    percentageRealizedPnl 缺失/为 0 时那个除法根本没有定义,本金是**不知道**。
    把未知折成 0 丢进排序,等于断言"这个人没投过钱"—— 那是把未知当成事实。
    未知统一垫底:排前面是抬举,垫底至少不制造假事实。
    ⚠️ unknown 故意放在输入的**第一位**:折成 0 的写法会让它和 zerocost 打平,
       稳定排序会原样保留输入顺序,于是 unknown 反而跑到前面去 —— 那正是要守住的错。
    """
    unknown = _item(handle="unknown", usd=0.0, pnl=0.0, pct=0.0)
    for k in ("usdValue", "unrealizedPnlUsd", "realizedPnlUsd", "percentageRealizedPnl"):
        unknown["authorTrade"].pop(k)
    # 本金**确实**是 0(在持 0、浮盈 0):这是事实,该排在"不知道"前面
    zerocost = _item(handle="zerocost", usd=0.0, pnl=0.0, pct=0.0)
    client = _FakeClient({"56": [unknown, zerocost]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "@unknown" in out, "本金未知不等于该被吞掉,人还是要出现"
    assert out.index("@zerocost") < out.index("@unknown"), \
        f"本金已知(哪怕是 0)排在本金未知的前面:\n{out}"


@pytest.mark.parametrize("trade, want, why", [
    ({"usdValue": 1000.0, "unrealizedPnlUsd": 200.0,
      "realizedPnlUsd": 500.0, "percentageRealizedPnl": 0.0}, 800.0,
     "pct 恰好 0 时除法没定义,已卖那一半算不出来,只用在持那一半(下界),不补 0"),
    ({"usdValue": 1000.0, "unrealizedPnlUsd": 200.0,
      "realizedPnlUsd": 500.0, "percentageRealizedPnl": 5e-324}, None,
     "次正规数百分比:pct/100 会下溢成 0.0 直接 ZeroDivisionError;算出 inf 也不能当本金"),
    ({"usdValue": 0.0, "unrealizedPnlUsd": 1000.0}, 0.0,
     "浮盈大于市值 → 本金算出负数,没有物理含义,压回 0(我们知道他投得极少,不是未知)"),
    ({}, None, "两半都取不到 → 未知,不是 0"),
])
def test_本金反推对各种脏字段都有确定行为(trade, want, why):
    """⚠️ 这些组合全都来自"陌生人可控 + 服务端可变"的字段,崩一次整条命令就没了回执"""
    from src.bot import _ca_cost_usd

    got = _ca_cost_usd({"authorTrade": trade})

    if want is None:
        assert got is None, f"{why};实际 {got!r}"
    else:
        assert got == pytest.approx(want), f"{why};实际 {got!r}"


def test_展示条数有上限且说明省略了多少条(monkeypatch, tmp_path):
    from src.bot import MAX_CA_THESIS_ROWS

    n = MAX_CA_THESIS_ROWS + 4
    items = [_item(handle=f"h{i}", usd=float(100 - i)) for i in range(n)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    shown = sum(1 for i in range(n) if f"@h{i}" in out)
    assert shown == MAX_CA_THESIS_ROWS, f"应只展示 {MAX_CA_THESIS_ROWS} 条,实际 {shown}"
    # ⚠️ 条数必须钉死:原来写的是 `"4" in out`,而 @h4 这个 handle 自己就带一个 "4",
    #    条数算错也照样绿 —— 那半个断言在空转
    assert f"…按投入本金排序,还有 {n - MAX_CA_THESIS_ROWS} 位未显示" in out


# ============================================================
# 出错路径:不能崩,要给可操作的提示
# ============================================================
def test_地址为空给出用法提示而不是崩溃(monkeypatch, tmp_path):
    b, _ = _bot(monkeypatch, tmp_path, client=None)

    out = b._cmd_ca("")

    assert "用法" in out
    assert "/ca" in out


def test_API异常给出可操作错误不崩溃(monkeypatch, tmp_path):
    client = _FakeClient(raise_exc=FomoAPIError("upstream 500"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_SOL)   # 非 0x 地址 → 只会试一条链(solana),命中异常直接返回

    assert out.startswith("❌")
    assert "500" in out or "异常" in out
    # get_token_thesis 要的是原生数字链 ID,solana 是 1399811149(见 src.bot._NETWORK_RAW_ID)
    assert client.calls == [(CA_SOL, "1399811149", 30)], "异常应立即中止,不应该换链重试"


def test_未知异常也给出可操作错误不崩溃(monkeypatch, tmp_path):
    client = _FakeClient(raise_exc=RuntimeError("网络炸了"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_SOL)

    assert out.startswith("❌")
    assert "网络炸了" in out


# ============================================================
# 链解析:本地已认识就不猜;完全陌生就按顺序试、命中即停手
# ============================================================
def test_本地已认识的地址不再猜链(monkeypatch, tmp_path):
    """token_snapshot 里已经有这个地址 → 直接用那条链,不发探测请求去猜别的链"""
    client = _FakeClient({"8453": [_item(handle="known", network="base")]})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    with store_.get_conn() as conn:
        store_.upsert_token_snapshots(conn, [("base", CA_BSC, "ELOY", 1.0, 10_000.0)])

    out = b._cmd_ca(CA_BSC)   # 不给链

    assert "@known" in out
    assert "Base" in out
    # get_token_thesis 要的是原生数字链 ID,base 是 8453(见 src.bot._NETWORK_RAW_ID)
    assert client.calls == [(CA_BSC, "8453", 30)]


def test_陌生地址按顺序试链命中即停手(monkeypatch, tmp_path):
    """完全陌生的 0x 地址:bsc/base 猜空,ethereum 才中 —— 命中后不该再往后猜"""
    from src.bot import _NETWORK_RAW_ID, CA_EVM_GUESS_ORDER

    client = _FakeClient({"1": [_item(handle="found", network="ethereum")]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_BSC)   # 不给链,本地也没有任何记录

    assert "@found" in out
    assert "Ethereum" in out
    assert [c[1] for c in client.calls] == [_NETWORK_RAW_ID[n] for n in CA_EVM_GUESS_ORDER[:3]], \
        "应按 bsc → base → ethereum 顺序试(各自转成原生数字 ID),命中 ethereum 后立刻停手"


# ============================================================
# 已清仓 vs 仍在持仓 —— 两种盈亏不是一回事,混着报就是凭空断言假事实
# ============================================================
@pytest.mark.parametrize(("handle", "realized", "realized_pct", "want"), [
    # 三组取值直接来自 data/fomo_probe/thesis.json 里的真实抓包(100 条里 6 条是这形状)。
    # 修复前这三个人除 handle 外**逐字节相同**,全是 `· $0.00 · +$0.00 (+0.0%)`
    ("Pastrami_A", 611.9449938058357, 61.32650387101396,
     "@Pastrami_A · 已清仓 · 已实现 +$611.94 (+61.3%)"),
    ("michiguy", -95.93801100000022, -10.434633668711145,
     "@michiguy · 已清仓 · 已实现 -$95.94 (-10.4%)"),
    ("Ruler", -59.032474582975425, -5.93291201838949,
     "@Ruler · 已清仓 · 已实现 -$59.03 (-5.9%)"),
])
def test_已清仓的人显示已实现盈亏而不是凭空的0(monkeypatch, tmp_path,
                                              handle, realized, realized_pct, want):
    """
    closedAt 非空 = 这个人已经清仓,他的 usdValue / unrealizedPnl 全是 0,
    落袋的盈亏在 realizedPnlUsd 里。只读未实现那三个键的话,落袋赚 $611.94 的人和
    实亏 $95.94 的人会被渲染成同一行 `+$0.00 (+0.0%)` ——
    那不是「字段缺失整行消失」,是凭空断言「这人打平了」,而且还带正号。
    """
    client = _FakeClient({"56": [_closed(handle, realized, realized_pct)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, handle) == want
    assert "+$0.00" not in out, "已清仓的人绝不能再被报成 +$0.00(那是一个假事实)"


def test_已清仓与仍持仓的几个人渲染各不相同(monkeypatch, tmp_path):
    """把真实抓包里那三条并排放进一条消息 —— 修复前它们除 handle 外一模一样"""
    client = _FakeClient({"56": [
        _closed("Pastrami_A", 611.9449938058357, 61.32650387101396),
        _closed("michiguy", -95.93801100000022, -10.434633668711145),
        _closed("Ruler", -59.032474582975425, -5.93291201838949),
        _item(handle="rxyz", usd=157.11, pnl=-6.26, pct=-2.5),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    rows = [_thesis_line_of(out, h) for h in ("Pastrami_A", "michiguy", "Ruler", "rxyz")]
    bodies = [r.split(" · ", 1)[1] for r in rows]
    assert len(set(bodies)) == 4, f"四个人的盈亏各不相同,渲染也必须各不相同:\n{rows}"
    assert _thesis_line_of(out, "rxyz") == "@rxyz · $157.11 · 未实现 -$6.26 (-2.5%)"


def test_仍在持仓又已经落袋一部分时两段都要显示(monkeypatch, tmp_path):
    """
    真实抓包 100 条里 **74 条**是"还拿着 + 已经卖掉一部分",不是只有完全清仓那 6 条。
    数字照抄首行 @change 的原值:手上 $51,474.66、已经落袋 $35,901.03。
    只报未实现那一段的话,这 3.59 万一分不显示。

    ⚠️ 金额用的是 _money 的紧凑记法($51.47K),那是全项目共用的排名展示格式
       (见 _money 的 docstring),这里不为 /ca 单独改。要看的是**两个量都在**。
    """
    client = _FakeClient({"56": [
        _item(handle="change", usd=51474.65676630271, pnl=16859.27797467851,
              pct=48.7045890098938,
              realized=35901.02696208642, realized_pct=46.32469188226596),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "change") == (
        "@change · $51.47K · 未实现 +$16.86K (+48.7%) · 已实现 +$35.90K (+46.3%)")


def test_浮亏但已落袋赚更多时方向不会被读反(monkeypatch, tmp_path):
    """
    只显示未实现那一段时,这个人看上去在亏 $500;真实情况是他已经落袋 $2,000。
    盈亏方向整个反过来 —— 而"看别人到底赚没赚"正是这条命令唯一的价值。
    """
    client = _FakeClient({"56": [
        _item(handle="flip", usd=1000.0, pnl=-500.0, pct=-33.3,
              realized=2000.0, realized_pct=80.0),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "flip") == (
        "@flip · $1.00K · 未实现 -$500.00 (-33.3%) · 已实现 +$2.00K (+80.0%)")


def test_一次都没卖过时不显示已实现那一段(monkeypatch, tmp_path):
    """
    realizedPnlUsd 恰好 0.0 是"一次都没卖过"的**真实值**,不是缺失。这里定的规矩是不显示,
    理由:这一段回答的是"已经落袋了多少",而"仍在持仓"这条路径的基线本来就是一分没落袋,
    不写这一段就是这个意思;写成 `已实现 +$0.00 (+0.0%)` 反而多断言了一次
    "卖过、只是刚好打平" —— 与"一次都没卖过"是两件事,数据分不出来,不该替读者选一个。

    ⚠️ 这不是拿真值判断去代替 is None:缺失判据仍然只有 is None(见 _ca_append_pnl),
       而且**持仓额那一列**的 0 照常显示(见 test_持仓恰好为0仍显示0点00)——
       那里的 0 是"清仓了"这个独一无二的事实,这里省掉的段信息量恒为 0。
    """
    client = _FakeClient({"56": [
        _item(handle="hodl", usd=200.0, pnl=25.0, pct=14.0, realized=0.0, realized_pct=0.0),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "hodl") == "@hodl · $200.00 · 未实现 +$25.00 (+14.0%)"


def test_未实现盈亏必须带未实现标签(monkeypatch, tmp_path):
    """两种盈亏同处一列,不加标签读者没法知道手里这个数是账面浮盈还是已经落袋"""
    client = _FakeClient({"56": [_item(handle="holder", usd=157.11, pnl=-6.26, pct=-2.5)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    line = _thesis_line_of(out, "holder")
    assert "未实现" in line and "已实现" not in line
    assert "已清仓" not in line


def test_已清仓时不再报0持仓(monkeypatch, tmp_path):
    """清仓的人报 `$0.00` 持仓,读者会理解成「他空仓且不赚不亏」—— 后半句是假的"""
    client = _FakeClient({"56": [_closed("gone", 88.0, 12.0)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "gone") == "@gone · 已清仓 · 已实现 +$88.00 (+12.0%)"


def test_清仓后的尘埃残值不印成0(monkeypatch, tmp_path):
    """
    线上真实值 usdValue = -2.4e-14(清仓后的尘埃残值)被 _money 印成 `-$0.00`,
    一个带负号的零;真实的小额仓位 $0.004 也印成 `$0.00`,与旁边「未实现 +$50.00」
    自相矛盾。⚠️ 这里不能用真值判断去解决 —— 那会把恰好 0 一起吞掉(见上面那个测试)。
    """
    client = _FakeClient({"56": [
        _item(handle="dust", usd=-2.4e-14, pnl=0.0, pct=0.0),
        _item(handle="tiny", usd=0.004, pnl=50.0, pct=120.0),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "dust") == "@dust · 不足 $0.01 · 未实现 +$0.00 (+0.0%)"
    assert _thesis_line_of(out, "tiny") == "@tiny · 不足 $0.01 · 未实现 +$50.00 (+120.0%)"
    assert "-$0.00" not in out, "带负号的零是纯粹的浮点垃圾,不该出现在给人看的消息里"


# ============================================================
# 一位作者一行 —— 8 个位置要给 8 个人
# ============================================================
def test_同一作者多条观点只占一行且取最新那条(monkeypatch, tmp_path):
    """
    线上真实数据里 @leiff 一个人发了 4 条观点、持仓完全相同,在 8 条的表里占掉
    3-4 个位置,把别人挤下去。上限的本意是「看 8 个人怎么看」,不是「看 8 条刷屏」。
    """
    from src.bot import MAX_CA_THESIS_ROWS

    items = [
        _item(handle="leiff", usd=900.0, text=f"第{i}条", created_at=f"2026-08-1{i}T00:00:00.000Z")
        for i in range(1, 5)
    ]
    items += [_item(handle=f"other{i}", usd=float(800 - i)) for i in range(MAX_CA_THESIS_ROWS)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.count("@leiff") == 1, f"一个人发 4 条也只该占一行:\n{out}"
    assert "第4条" in out and "第1条" not in out, "同一作者留最新那条"
    # 8 个位置 = 8 个人:leiff + other0..other6
    shown = sum(1 for i in range(MAX_CA_THESIS_ROWS) if f"@other{i}" in out)
    assert shown == MAX_CA_THESIS_ROWS - 1, f"另外 7 个位置要留给别人,实际 {shown}"


def test_认不出作者的多条观点不被并成一个人(monkeypatch, tmp_path):
    """userId / userHandle 都没有时宁可多占一行,也不能把两个陌生人并成同一个"""
    items = []
    for i in range(2):
        it = _item(usd=float(100 - i), text=f"匿名{i}")
        for k in ("userId", "userHandle", "displayName"):
            it.pop(k)
        items.append(it)
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "匿名0" in out and "匿名1" in out


# ============================================================
# 条数措辞:抓取窗口的数字不能当成全站总数陈述
# ============================================================
def test_条数措辞不冒充全站总数(monkeypatch, tmp_path):
    """
    N 来自 limit=CA_THESIS_FETCH_LIMIT 的抓取窗口,不是全站真实总数;
    观点超过窗口时这个数字和省略条数都会少报,所以文案不能用陈述事实的口气。
    """
    from src.bot import CA_THESIS_FETCH_LIMIT

    items = [_item(handle=f"u{i}", usd=float(500 - i)) for i in range(CA_THESIS_FETCH_LIMIT)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert f"取到 {CA_THESIS_FETCH_LIMIT} 条观点" in out
    assert f"只取最近 {CA_THESIS_FETCH_LIMIT} 条,实际可能更多" in out, \
        "抓满窗口时必须挑明这只是窗口,不是全站总数"


def test_没抓满窗口时不加那句可能更多(monkeypatch, tmp_path):
    """没抓满就说明确实取全了 —— 再加「可能更多」反而是另一种误导"""
    client = _FakeClient({"56": [_item(handle="only")]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "取到 1 条观点 · 1 位作者" in out
    assert "可能更多" not in out


# ============================================================
# 长度预算 —— 陌生人可触发的可用性攻击
# ============================================================
def _evil(handle):
    """
    最坏情况的正文:全是单引号。html.escape 默认 quote=True,一个 `'` 膨胀成
    `&#x27;` 6 个字符 —— 140 字符的原文转义后是 840 字符,8 条就是 7000+。
    发观点的是任意 FOMO 用户 = 互联网上的陌生人,长度完全由他控制。
    """
    from src.bot import CA_THESIS_SNIPPET_CHARS
    return _item(handle=handle, usd=999.0, text="'" * (CA_THESIS_SNIPPET_CHARS + 50))


def test_全恶意正文不撑破长度预算且CA锚点完整(monkeypatch, tmp_path):
    from src.bot import CA_MSG_BUDGET, MAX_CA_THESIS_ROWS
    from src.notifier import MAX_MESSAGE_LEN

    client = _FakeClient({"56": [_evil(f"evil{i}") for i in range(MAX_CA_THESIS_ROWS)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert len(out) <= CA_MSG_BUDGET, f"预算必须按**转义后**的长度算,实际 {len(out)}"
    assert len(out) <= MAX_MESSAGE_LEN, "超了 notifier 就会盲切,盲切点落在实体中间即 400"
    _assert_tg_safe(out)
    assert "位未显示" in out, "塞不下的必须如实说,不能悄悄吞掉"


def test_四条恶意四条正常也不撑破预算(monkeypatch, tmp_path):
    from src.bot import CA_MSG_BUDGET
    from src.notifier import MAX_MESSAGE_LEN

    items = [_evil(f"evil{i}") for i in range(4)]
    items += [_item(handle=f"ok{i}", usd=float(100 - i), text="这个币基本面还行,继续拿着看看")
              for i in range(4)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert len(out) <= CA_MSG_BUDGET, f"实际 {len(out)}"
    assert len(out) <= MAX_MESSAGE_LEN
    _assert_tg_safe(out)


def test_一条塞不下的长观点不连带丢掉后面塞得下的短观点(monkeypatch, tmp_path):
    """
    预算用完时**跳过这一行继续试下一行**,不是就此收摊。

    ⚠️ 这不是对抗场景:一个正常人写了条长评论,排在他后面的所有人就都消失了。
       前几条把 room 吃掉之后,后面那些几十字符的短行本来完全塞得下。
    ⚠️ 顺带钉死"还有 N 位未显示"的计数 —— 数字从输出自己反推,少报多报都会红。
    """
    talky = [_evil(f"talky{i}") for i in range(5)]          # 每条转义后 ~850 字符
    shorts = [_item(handle=f"short{i}", usd=float(500 - i), text="不错") for i in range(3)]
    handles = [f"talky{i}" for i in range(5)] + [f"short{i}" for i in range(3)]
    client = _FakeClient({"56": talky + shorts})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    shown = [h for h in handles if f"@{h}" in out]
    assert any(h.startswith("short") for h in shown), \
        f"长行塞不下就该跳过它继续往下试,不能把后面的人一起丢掉:\n{out}"
    m = re.search(r"还有 (\d+) 位未显示", out)
    missing = len(handles) - len(shown)
    if missing == 0:
        assert m is None, f"一个没少却报了「{m.group(0) if m else ''}」"
    else:
        assert m is not None, f"少了 {missing} 位却没说:\n{out}"
        assert int(m.group(1)) == missing, \
            f"少了 {missing} 位,却报「{m.group(0)}」—— 少报多报都是错的"
    _assert_tg_safe(out)


# ============================================================
# 短字段限长 —— ticker / handle / 链名都由陌生人或服务端决定,长度不受任何天然约束
# ============================================================
def test_超长ticker不把观点区整段掏空(monkeypatch, tmp_path):
    """
    head 段是 _ca_assemble 最后才会砍到的一段。ticker 不限长的话,一个字段就能把
    room 顶成负数,观点一条都渲染不出来,最后只剩一个 CA 锚点。
    """
    from src.bot import CA_MSG_BUDGET, CA_TICKER_CHARS

    items = [_item(handle=f"u{i}", usd=float(900 - i), ticker="A" * 5000) for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert len(out) <= CA_MSG_BUDGET, f"实际 {len(out)}"
    assert "A" * (CA_TICKER_CHARS + 1) not in out, "ticker 必须被裁到上限以内"
    for i in range(3):
        assert f"@u{i}" in out, f"一个超长 ticker 不该把 @u{i} 挤掉:\n{out[:300]}"
    _assert_tg_safe(out)


def test_超长链名不把观点区整段掏空(monkeypatch, tmp_path):
    """networkId 是服务端回读的,normalize_network 对未收录的值**原样透传**"""
    from src.bot import CA_CHAIN_CHARS, CA_MSG_BUDGET

    items = [_item(handle=f"c{i}", usd=float(900 - i), network="Z" * 5000) for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert len(out) <= CA_MSG_BUDGET, f"实际 {len(out)}"
    assert "Z" * (CA_CHAIN_CHARS + 1) not in out, "链名必须被裁到上限以内"
    for i in range(3):
        assert f"@c{i}" in out, f"一个超长链名不该把 @c{i} 挤掉:\n{out[:300]}"
    _assert_tg_safe(out)


def test_超长handle被裁剪且不挤掉别人(monkeypatch, tmp_path):
    """handle 是陌生人自己起的名字 —— 一行 3000 字符就能吃掉大半个预算"""
    from src.bot import CA_HANDLE_CHARS, CA_MSG_BUDGET

    items = [_item(handle="H" * 3000, usd=900.0)]
    items += [_item(handle=f"n{i}", usd=float(100 - i), text="短") for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert len(out) <= CA_MSG_BUDGET, f"实际 {len(out)}"
    assert "H" * (CA_HANDLE_CHARS + 1) not in out, "handle 必须被裁到上限以内"
    for i in range(3):
        assert f"@n{i}" in out, f"一个超长 handle 不该把 @n{i} 挤掉:\n{out[:300]}"
    _assert_tg_safe(out)


def test_短字段里的换行被叠平不另起新行(monkeypatch, tmp_path):
    """
    只限长不叠平等于没限:handle 里塞 16 个换行,一行就变成十几行 ——
    预算是按**行**算的,行数被别人控制就等于预算被别人控制。
    """
    client = _FakeClient({"56": [_item(handle="a\nb\nc", usd=100.0, ticker="X\nY")]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "$X Y" in out, f"ticker 里的换行必须叠平:\n{out}"
    assert "@a b c" in out, f"handle 里的换行必须叠平:\n{out}"


def test_正常内容不会被预算误伤(monkeypatch, tmp_path):
    """预算是给恶意正文兜底的,8 条正常中文观点必须一条不少地全展示出来"""
    from src.bot import MAX_CA_THESIS_ROWS

    items = [_item(handle=f"cn{i}", usd=float(900 - i),
                   text="这个盘子我看了两天,筹码结构还算干净,先拿一点仓位试试水位")
             for i in range(MAX_CA_THESIS_ROWS)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    for i in range(MAX_CA_THESIS_ROWS):
        assert f"@cn{i}" in out
    assert "位未显示" not in out
    _assert_tg_safe(out)


def test_本地名单区再长也保住CA锚点(monkeypatch, tmp_path):
    """
    兜底路径:本地名单区自己就撑破预算时,照样只按整行砍,而且 CA 锚点必须活到最后。
    盲切的老路径里它 100% 被吃掉(实测「截断后还含 </code> 结尾吗: False」)。
    """
    from src.bot import CA_MSG_BUDGET

    client = _FakeClient({"56": []})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    with store_.get_conn() as conn:
        # ⚠️ handle 要长到「光是名单区就顶破预算」:MAX_CA_LOCAL_BUYERS 只展开 10 个买家,
        #    名字短的话这段根本撑不破 3600,兜底分支就等于没被测到(变异验证发现过)
        for i in range(60):
            who = "x" * 600 + str(i)
            store_.add_watch_user(conn, f"u{i}", who, None)
            store_.mark_stats_ready(conn, f"u{i}")
            store_.insert_event(conn, FomoEvent(
                event_id=f"e{i}", event_type=EVENT_BUY, user_id=f"u{i}",
                event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
                network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
                amount_usd=500.0, market_cap=100_000.0, badge_reason=REASON_LOCAL_STATS,
                user_handle=who,
            ))

    out = b._cmd_ca(CA_BSC)

    assert len(out) <= CA_MSG_BUDGET, f"实际 {len(out)}"
    _assert_tg_safe(out)


def test_观点正文超长时先截断再转义_不留残缺实体(monkeypatch, tmp_path):
    """
    截断顺序必须是「叠平空白 → 按原文截断 → 转义」。反过来(先转义后截断)会在
    截断点把 `&amp;` 从中间劈开,残缺实体照样让整条消息 400。
    ⚠️ 这个分支此前零覆盖:把顺序改成先转义后截断,原来 14 个测试仍然全绿。
    """
    from src.bot import CA_THESIS_SNIPPET_CHARS

    # 截断点正好落在一串 `&` 上:先转义再截断必然切出 `&am` / `&a` / `&` 这类残片
    raw = "A" * (CA_THESIS_SNIPPET_CHARS - 2) + "&" * 20
    client = _FakeClient({"56": [_item(handle="longone", text=raw)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    _assert_tg_safe(out)
    assert "…" in out, "超长正文必须被截断并留下省略号"
    assert out.count("&amp;") == 2, \
        f"140 字符原文里只有 2 个 `&`,转义后就该是 2 个完整实体,实际 {out.count('&amp;')}"


# ============================================================
# 接口异常:本地那半段已经算好了,不能被一起丢掉
# ============================================================
def _seed_local(store_, handle="bob", amount_usd=500.0, market_cap=100_000.0, snap_mc=300_000.0):
    with store_.get_conn() as conn:
        store_.add_watch_user(conn, "u1", handle, handle.title())
        store_.mark_stats_ready(conn, "u1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=amount_usd, market_cap=market_cap, badge_reason=REASON_LOCAL_STATS,
            user_handle=handle,
        ))
        if snap_mc is not None:
            store_.upsert_token_snapshots(conn, [("bsc", CA_BSC, "ELOY", 1.0, snap_mc)])


def test_接口异常时本地名单区照常输出(monkeypatch, tmp_path):
    """
    与本命令 docstring 承诺的「两段互相独立、互不依赖」相反:接口一异常就 return,
    本地那半段其实早就算好了,却被一起扔掉。
    """
    client = _FakeClient(raise_exc=FomoAPIError("upstream 500"))
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_)

    out = b._cmd_ca(CA_BSC)

    assert "@bob" in out, "接口挂了不代表本地数据也没了"
    assert "3.0x" in out
    assert "观点没拉到" in out and "500" in out, "得说清观点这一段为什么是空的"
    _assert_tg_safe(out)


def test_接口异常且本地也没记录时只报错误(monkeypatch, tmp_path):
    """本地确实没东西可说,就别为了「两段独立」硬凑一个空壳出来"""
    client = _FakeClient(raise_exc=FomoAPIError("upstream 500"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_SOL)

    assert out.startswith("❌")
    assert "无人持有" not in out


def test_鉴权失败给出重新登录提示(monkeypatch, tmp_path):
    """用户最常撞见的一段文案,此前零覆盖"""
    from src.auth import AuthError

    client = _FakeClient(raise_exc=AuthError("token 过期"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_SOL)

    assert out.startswith("❌")
    assert "登录态失效" in out
    assert "--login" in out


def test_403给出Cloudflare与降级提示(monkeypatch, tmp_path):
    client = _FakeClient(raise_exc=FomoAPIError("403 Forbidden"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_SOL)

    assert "Cloudflare" in out
    assert "playwright" in out


# ============================================================
# 用户最常看到的几段兜底文案(此前零覆盖)
# ============================================================
def test_全猜空时的兜底文案带出不确定性(monkeypatch, tmp_path):
    from src.bot import CA_EVM_GUESS_ORDER

    client = _FakeClient({})            # 每条链都返回空
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_BSC)

    assert "/".join(CA_EVM_GUESS_ORDER) in out, "要说清到底试过哪几条链"
    assert "本地也没有记录" in out
    assert "也可能是猜错了链" in out, "不能断言「这个币不存在」"
    assert "没试完" not in out, "预算够用时不该说没试完"
    assert len(client.calls) == len(CA_EVM_GUESS_ORDER)


def test_指定了链但查不到时不提猜错链(monkeypatch, tmp_path):
    """用户点名了链就别再说「可能是猜错了链」—— 没猜,是他说的"""
    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "BNB Chain" in out
    assert "这条链上没查到观点" in out
    assert "猜错了链" not in out
    assert len(client.calls) == 1


def test_本地认识但没人发观点(monkeypatch, tmp_path):
    """本地有记录、观点区是空的 —— 两段独立,本地那段照常出"""
    client = _FakeClient({})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_)

    out = b._cmd_ca(CA_BSC)

    assert "💭 没查到观点(已试 bsc)" in out
    assert "@bob" in out
    assert "$ELOY" in out, "观点区空的时候 symbol 要退回本地拿"
    _assert_tg_safe(out)


# ============================================================
# 猜链的总时长预算
# ============================================================
def test_猜链超预算后停止继续猜并如实说明(monkeypatch, tmp_path):
    """
    命令层严格串行:单条命令阻塞超过 STALE_COMMAND_SEC,同批次里排在它后面的命令
    会被静默丢弃。所以猜链要有总时长闸门 —— 超了就停手并说「没猜完」,
    而不是无限往下试。⚠️ 这里只压时长,不加冷却/限流。
    """
    import src.bot as bot_mod
    from src.bot import CA_EVM_GUESS_ORDER

    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)
    monkeypatch.setattr(bot_mod, "CA_GUESS_BUDGET_SEC", -1.0)   # 预算一开始就是负的

    out = b._cmd_ca(CA_BSC)

    assert [c[1] for c in client.calls] == [bot_mod._NETWORK_RAW_ID[CA_EVM_GUESS_ORDER[0]]], \
        "第一条链无论如何要试,之后超预算就不该再发请求"
    assert "没试完" in out
    assert "/".join(CA_EVM_GUESS_ORDER[1:]) in out, "得说清剩下哪几条链没试"


def test_预算够用时不会提前收手(monkeypatch, tmp_path):
    """闸门不能反过来伤到正常路径"""
    from src.bot import CA_EVM_GUESS_ORDER

    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(CA_BSC)

    assert len(client.calls) == len(CA_EVM_GUESS_ORDER)
    assert "没试完" not in out


# ============================================================
# 本地名单区:0 是真实值 / NULL 是未知,两者必须可辨识
# ============================================================
def test_本地名单区的0都是真实值不能被吞(monkeypatch, tmp_path):
    """
    市值 0、进场市值 0、买入 0 元都是有意义的真实值。
    把这三处的 `is None` 改成真值判断,整行/整段就会凭空消失。
    """
    client = _FakeClient({})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="zero", amount_usd=0.0, market_cap=0.0, snap_mc=0.0)

    out = b._cmd_ca(CA_BSC)

    assert "💎 现在市值 $0.00" in out, "现在市值恰好 0 也是真实值"
    assert "@zero · 💎$0.00 进场 · $0.00" in out, "进场市值 0 与买入 0 元同样是真实值"


def test_买入金额一笔都没解析出来时不印成0元(monkeypatch, tmp_path):
    """
    store.token_buyers 的 SUM(COALESCE(amount_usd,0)) 把「金额未知」压成了 0.0,
    Python 侧的 is None 因此永远为假 —— 于是「未知」和「真的只买了 0 元」再也分不开。
    渲染层必须让「未知」重新可辨识:只报确实知道的笔数,不报凭空的 $0.00。
    """
    client = _FakeClient({})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="nousd", amount_usd=None, market_cap=100_000.0)

    out = b._cmd_ca(CA_BSC)

    assert "@nousd" in out, "金额未知不代表这个人没买"
    assert "$0.00" not in out, "金额未知却报 $0.00,读者会理解成「他只买了 0 块钱」"
    assert "1 笔" in out, "笔数是确实知道的,照常报"


def test_金额部分未知时仍按已知的那部分报(monkeypatch, tmp_path):
    """只要有一笔解析出了金额,这个人的累计额就是有意义的(会低报,但不是凭空的 0)"""
    client = _FakeClient({})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="halfusd", amount_usd=None, market_cap=100_000.0)
    with store_.get_conn() as conn:
        store_.insert_event(conn, FomoEvent(
            event_id="e2", event_type=EVENT_BUY, user_id="u1",
            event_ts="2026-08-02T00:00:00+00:00", raw_json="{}",
            network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=250.0, market_cap=120_000.0, badge_reason=REASON_LOCAL_STATS,
            user_handle="halfusd",
        ))

    out = b._cmd_ca(CA_BSC)

    assert "$250.00(2 笔)" in out
