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

from src.client import FomoAPIError
from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

# 与任务描述里同一个真实探测过的地址(BSC,chainid 56)
CA_BSC = "0xfc6e0617a6cc19afe9e0d367e97d07245d357777"
CA_SOL = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"


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
         ticker="ELOY", network="bsc", user_id=None):
    """
    构造一条 get_token_thesis 条目。

    ⚠️ comment 字段的真实形状是**嵌套 dict**(2026-08-24 对真实 API 实测确认,
       ticker/networkId 等其余字段确实在顶层):
         {"comment": {"comment": "正文", "tokenAddress": ..., "networkId": ...}, ...}
       与最初任务描述里"comment 就是正文字符串"这个印象不一致 —— 按实测数据为准。
       _ca_thesis_text 两种形状都认,见 test_观点正文是嵌套comment字典时也能取到正文。
    """
    return {
        "ticker": ticker, "tokenAddress": CA_BSC, "networkId": network,
        "comment": {"comment": text, "tokenAddress": CA_BSC, "networkId": network},
        "displayName": handle, "userHandle": handle,
        "userId": user_id or handle, "createdAt": "2026-08-20T00:00:00.000Z",
        "numReplies": 0, "isDev": False, "threshold": 0,
        "authorTrade": {
            "usdValue": usd, "humanTokenAmount": 1.0,
            "unrealizedPnlUsd": pnl, "percentageUnrealizedPnl": pct,
            "realizedPnlUsd": 0.0, "percentageRealizedPnl": 0.0, "closedAt": None,
        },
    }


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
    """0 是「已经清仓」的真实值,和"拿不到"是两回事,不能被 _cmd 那类 if 判断误吞"""
    it = _item(handle="soldout", usd=0.0, pnl=0.0, pct=0.0)
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert "@soldout" in out
    assert "$0.00" in out, "持仓恰好 0 是有意义的真实值(清仓了),必须照常显示"


# ============================================================
# 排序与展示上限
# ============================================================
def test_按持仓额从大到小排序(monkeypatch, tmp_path):
    items = [
        _item(handle="low", usd=10.0),
        _item(handle="high", usd=500.0),
        _item(handle="mid", usd=100.0),
    ]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.index("@high") < out.index("@mid") < out.index("@low"), \
        f"持仓额应从大到小排(500 > 100 > 10):\n{out}"


def test_展示条数有上限且说明省略了多少条(monkeypatch, tmp_path):
    from src.bot import MAX_CA_THESIS_ROWS

    n = MAX_CA_THESIS_ROWS + 4
    items = [_item(handle=f"h{i}", usd=float(100 - i)) for i in range(n)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    shown = sum(1 for i in range(n) if f"@h{i}" in out)
    assert shown == MAX_CA_THESIS_ROWS, f"应只展示 {MAX_CA_THESIS_ROWS} 条,实际 {shown}"
    assert "4" in out and "未显示" in out, "必须说明省略了多少条,不能悄悄截断"


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
