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

import random
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


# Telegram sendMessage 的协议上限。⚠️ 这是**外部事实**,故意写成字面量而不是从
# src.notifier / src.bot import —— 门槛一旦从被测模块来,实现把上限调宽,断言就跟着调宽
# (test_超长handle被裁剪且不挤掉别人 就在这个坑里空转过一整轮)。
_TG_HARD_LIMIT = 4096
_TAG = re.compile(r"<(/?)([a-zA-Z][^<>]*)>")


def _tag_fault(s: str) -> str:
    """标签有没有配对闭合。自己数一遍,不复用 src.bot 里的任何东西(复用就又成自指了)"""
    stack: list[str] = []
    for m in _TAG.finditer(s):
        name = m.group(2).split()[0].lower()
        if m.group(1):
            if not stack or stack[-1] != name:
                return f"闭标签配不上开标签: {m.group(0)}"
            stack.pop()
        else:
            stack.append(name)
    return f"标签没闭合: {stack}" if stack else ""


def _assert_ca_invariant(out: str, *, anchored: bool = True) -> None:
    """
    /ca 装配出口的不变式(见 src.bot._ca_assemble 的 docstring):

      1. 长度必然在预算内 —— 超了 notifier 就盲切,切点落在实体中间即整条 400
      2. HTML 必然合法 —— 没有残缺实体、没有裸 `<` / `>`、标签全部配对闭合
      3. CA 锚点必然在,而且是完整闭合的最后一行

    ⚠️ anchored=False 只给"用法提示"这类本来就没有锚点的短回执用,前两条照查。
    """
    from src.bot import CA_MSG_BUDGET

    assert len(out) <= _TG_HARD_LIMIT, f"超过 TG 协议上限 → notifier 盲切 → 400(实际 {len(out)})"
    assert len(out) <= CA_MSG_BUDGET, f"超过本命令自己给自己定的预算(实际 {len(out)})"
    m = _BROKEN_ENTITY.search(out)
    assert m is None, f"残缺 HTML 实体 @{m.start() if m else -1}: {out[max(0, (m.start() if m else 0) - 25):][:60]!r}"
    fault = _tag_fault(out)
    assert not fault, f"{fault}\n{out[:200]!r}"
    n_tags = len(_TAG.findall(out))
    assert out.count("<") == n_tags, f"出现了不属于任何标签的裸 `<`:{out[:200]!r}"
    assert out.count(">") == n_tags, f"出现了不属于任何标签的裸 `>`:{out[:200]!r}"
    if anchored:
        assert out.endswith("</code>"), f"CA 锚点必须活到最后一行,实际结尾: {out[-80:]!r}"
        assert "<code>" in out, "锚点的开标签也得在"


# 喂给不变式扫描的"最坏输入"零件表:实体的每一种形态、我们自己会写的标签、
# 不该出现的标签、各种换行/不可见字符、以及会把 _esc 撑成 6 倍的引号。
_EVIL_FRAGMENTS = (
    "&", "<", ">", '"', "'", "&amp;", "&lt;", "&gt;", "&quot;", "&#x27;", "&#39;",
    "&am", "&#x2", "<code>", "</code>", "<b>", "</b>", "<a href='x'>", "</a>",
    "<script>", "</script>", "<3", "a>b",
    "\n", "\r\n", "\t", "\x85", "\u2028", " ", "\u3000",
    "A", "中", "…", "😀", "0", "$", "/", "\\", "%",
)


def _evil_text(rng, n: int) -> str:
    return "".join(rng.choice(_EVIL_FRAGMENTS) for _ in range(n))


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
    6 条清仓条目(去重后是 5 位清仓作者)整整齐齐落在 #95–#100,而展示上限只有 8 行 ——
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
    # ⚠️ 下面三格补的是"**在持**那一半算不出"。原来的表只覆盖了"已卖那半算不出"和
    #    "两半全缺",于是给在持那半补 0(`held - (unreal or 0)`)照样 531 全绿 ——
    #    而 _ca_cost_usd 的 docstring 把"算不出的一半绝不补 0"写成了本次修复的核心。
    ({"usdValue": 1000.0, "realizedPnlUsd": 500.0, "percentageRealizedPnl": 50.0}, 1000.0,
     "缺 unrealizedPnlUsd → 在持那半算不出,只用已卖那半 500/50%=1000;补 0 会变成 2000"),
    ({"usdValue": 1000.0}, None,
     "只有市值、没有浮盈 → 在持那半是**不知道**,不是 1000;两半都算不出就该是 None"),
    ({"unrealizedPnlUsd": 200.0}, None,
     "反过来只有浮盈没有市值,同样算不出;补 0 会得出 -200 这种没有物理含义的本金"),
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
def _line_starting_with(out: str, prefix: str) -> str:
    """取出以 prefix 开头的那一行 —— 断言"这一行有多长"用,找不到直接失败而不是抛 StopIteration"""
    for ln in out.split("\n"):
        if ln.startswith(prefix):
            return ln
    raise AssertionError(f"输出里没有以 {prefix!r} 开头的行:\n{out[:400]}")


# ⚠️ 下面三条测试的门槛**故意写成字面量**,不从 src.bot import 上限常量。
#    原来写的是 `assert "H" * (CA_HANDLE_CHARS + 1) not in out` —— CA_HANDLE_CHARS
#    就是实现里那个上限,实现把它从 24 放宽到 2900,断言的门槛跟着一起放宽,照样全绿
#    (实测过),只有把上限调**小**才会红。自指的断言等于没有断言,本仓库栽过这一跤。
#    改成钉一个与实现无关的**绝对**事实:手机上一行放不下的东西,再多也没有意义。
_MAX_SANE_HEAD_LINE = 80
_MAX_SANE_ROW_LINE = 120


def test_超长ticker不把观点区整段掏空(monkeypatch, tmp_path):
    """
    head 段是 _ca_assemble 最后才会砍到的一段。ticker 不限长的话,一个字段就能把
    room 顶成负数,观点一条都渲染不出来,最后只剩一个 CA 锚点。
    """
    items = [_item(handle=f"u{i}", usd=float(900 - i), ticker="A" * 5000) for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    head_line = _line_starting_with(out, "<b>$")
    assert len(head_line) <= _MAX_SANE_HEAD_LINE, \
        f"一个 ticker 把头部撑到了 {len(head_line)} 字符:{head_line[:60]!r}…"
    assert "…" in head_line, "被裁剪必须留下痕迹,否则读者以为这就是完整的 ticker"
    for i in range(3):
        assert f"@u{i}" in out, f"一个超长 ticker 不该把 @u{i} 挤掉:\n{out[:300]}"
    _assert_ca_invariant(out)


def test_超长链名不把观点区整段掏空(monkeypatch, tmp_path):
    """networkId 是服务端回读的,normalize_network 对未收录的值**原样透传**"""
    items = [_item(handle=f"c{i}", usd=float(900 - i), network="Z" * 5000) for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    head_line = _line_starting_with(out, "<b>$")
    assert len(head_line) <= _MAX_SANE_HEAD_LINE, \
        f"一个链名把头部撑到了 {len(head_line)} 字符:{head_line[:60]!r}…"
    assert "…" in head_line, "被裁剪必须留下痕迹"
    for i in range(3):
        assert f"@c{i}" in out, f"一个超长链名不该把 @c{i} 挤掉:\n{out[:300]}"
    _assert_ca_invariant(out)


def test_超长handle被裁剪且不挤掉别人(monkeypatch, tmp_path):
    """handle 是陌生人自己起的名字 —— 一行 3000 字符就能吃掉大半个预算"""
    items = [_item(handle="H" * 3000, usd=900.0)]
    items += [_item(handle=f"n{i}", usd=float(100 - i), text="短") for i in range(3)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    row = _line_starting_with(out, "@H")
    assert len(row) <= _MAX_SANE_ROW_LINE, \
        f"一个 handle 把整行撑到了 {len(row)} 字符:{row[:60]!r}…"
    assert "…" in row, "被裁剪必须留下痕迹,否则读者以为这就是他的全名"
    for i in range(3):
        assert f"@n{i}" in out, f"一个超长 handle 不该把 @n{i} 挤掉:\n{out[:300]}"
    _assert_ca_invariant(out)


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
def _seed_local(store_, handle="bob", amount_usd=500.0, market_cap=100_000.0, snap_mc=300_000.0,
                network="bsc"):
    """⚠️ network 也要可参数化:它来自接口、直接落进 network_id 列,长度同样没人校过"""
    with store_.get_conn() as conn:
        store_.add_watch_user(conn, "u1", handle, handle.title())
        store_.mark_stats_ready(conn, "u1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id=network, token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=amount_usd, market_cap=market_cap, badge_reason=REASON_LOCAL_STATS,
            user_handle=handle,
        ))
        if snap_mc is not None:
            store_.upsert_token_snapshots(conn, [(network, CA_BSC, "ELOY", 1.0, snap_mc)])


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


# ============================================================
# 装配出口的不变式 —— 用穷举扫描守,而不是逐个场景/逐个字段追
# ============================================================
# 扫描规模。⚠️ 刻意**不**逐个场景写用例:逐个场景就是逐个字段封顶的翻版,
#    上一轮封了 ticker / handle / 链名三处,这一轮验证者又找出四处(本地买家名、
#    接口错误文案、已试链名、百分比),下一轮还会有第八处第九处。守出口才守得完。
#    规模按"跑得起"取:装配层 2000 条约 5s、命令层 400 条约 2s。种子写死,可复现。
#    离线又扫过 200000 + 20000 条(同一套生成器,不同种子),零反例。
_FUZZ_ASSEMBLE_CASES = 2000
_FUZZ_CMD_CASES = 400

# 数值零件表:全都取自"接口可以返回、_f 必须扛得住"的真实形态
_EVIL_NUMS = (
    None, 0.0, -0.0, 1e-13, -1e-13, 0.004, -0.004, 123.45, -99.9,
    1e300, -1e300, 5e-324, 1e15, 1e9, float("inf"), float("nan"),
    "x", "", "1,234.5", "$12", True, [1], {"a": 1},
)


def _evil_token(rng, n: int) -> str:
    """不含空白的一段垃圾 —— 当"地址"或"链名"用(arg.split() 之后必须还是一个 token)"""
    pool = tuple(f for f in _EVIL_FRAGMENTS if not any(c.isspace() for c in f))
    return "".join(rng.choice(pool) for _ in range(n))


def _evil_row(rng) -> dict:
    """一条最坏情况的观点:每个字段都可能是垃圾、缺失、超长或者根本不是那个类型"""
    def num():
        return rng.choice(_EVIL_NUMS)

    return {
        "ticker": _evil_text(rng, rng.randrange(0, 10)),
        "networkId": _evil_text(rng, rng.randrange(0, 10)),
        "userHandle": _evil_text(rng, rng.randrange(0, 12)),
        "displayName": _evil_text(rng, rng.randrange(0, 12)),
        "userId": rng.choice([None, "u", _evil_text(rng, 3)]),
        "createdAt": rng.choice([None, "", "2026-08-20T00:00:00.000Z"]),
        "comment": rng.choice([
            _evil_text(rng, rng.randrange(0, 40)),
            {"comment": _evil_text(rng, rng.randrange(0, 40))},
        ]),
        "authorTrade": rng.choice([
            {
                "usdValue": num(), "unrealizedPnlUsd": num(), "percentageUnrealizedPnl": num(),
                "realizedPnlUsd": num(), "percentageRealizedPnl": num(),
                "closedAt": rng.choice([None, "2026-01-01T00:00:00Z", ""]),
            },
            None, "not-a-dict",
        ]),
    }


def test_锚点自己超预算时被截短而不是撑破整条消息():
    """
    直接缺陷:旧写法把 lines 掏空之后**无条件** append 锚点 —— 锚点自己就超预算时
    整条消息照样超长 → notifier 盲切 → 400 → 用户什么都收不到。

    触发路径不需要任何攻击技巧:models.normalize_token_address 对非 0x/42 位的输入
    **原样透传**,不做任何长度校验,粘一个几千字符的"地址"上来就到了。
    修好之后锚点该被**截短**而不是被丢掉 —— 它是设计文档 §10.3 的必备锚点。

    ⚠️ 两条路都要扫:
       a) _ca_anchor 造出来的锚点(生产路径,_cmd_ca 走的就是它);
       b) 调用方按老写法**内联拼**、一个字都没收口的锚点 —— 不变式说的是
          "无论入参是什么",_ca_assemble 自己就得扛住,不能把责任推给调用方。
          少了 (b),把 _ca_assemble 里那句 anchor 收口删掉,测试照样全绿(实测过)。
    """
    from src.bot import _ca_anchor, _ca_assemble, _esc

    for n in (30, 3000, 20000):
        for anchor in (_ca_anchor("Z" * n), f"<code>{_esc('Z' * n)}</code>"):
            out = _ca_assemble(["<b>$X</b> · bsc"], [], ["", "👥 你的名单:无人持有"], anchor)
            _assert_ca_invariant(out)
            assert "ZZZZZZZZZZ" in out, f"锚点该被截短,不是被丢掉:{out[-60:]!r}"


def test_装配出口不变式对随机极端输入恒成立():
    """
    穷举式地守:随机生成一大批极端输入喂给 _ca_assemble,每一条输出都断言同一组不变式
    (长度 ≤ 预算、HTML 合法、CA 锚点完整收尾)。

    ⚠️ 一半用例故意**不转义**就往里灌:不变式声明的是"无论输入什么",
       上游哪个字段漏了一次 _esc 也不能让整条消息 400。
    """
    from src.bot import _ca_anchor, _ca_assemble, _esc

    rng = random.Random(20260825)
    for _ in range(_FUZZ_ASSEMBLE_CASES):
        wrap = _esc if rng.random() < 0.5 else str
        head = [wrap(_evil_text(rng, rng.randrange(0, 60))) for _ in range(rng.randrange(0, 4))]
        tail = [wrap(_evil_text(rng, rng.randrange(0, 120))) for _ in range(rng.randrange(0, 12))]
        rows = [_evil_row(rng) for _ in range(rng.randrange(0, 12))]
        addr = _evil_text(rng, rng.randrange(0, 400))
        # 三种锚点形状都扫。第三种(锚点自己就远超预算)是必须的:
        # 只扫前两种的话,把 _ca_assemble 里那句 anchor 收口删掉照样全绿 —— 因为
        # _ca_anchor 已经替它收过口了,而内联那种随机长度很少真的顶破 3600。
        anchor = rng.choice([
            _ca_anchor(addr),
            f"<code>{_esc(addr)}</code>",
            f"<code>{_esc('Z' * rng.randrange(0, 8000))}</code>",
        ])

        _assert_ca_invariant(_ca_assemble(head, rows, tail, anchor))


def test_命令出口不变式端到端对随机极端输入恒成立(monkeypatch, tmp_path):
    """
    _ca_assemble 不是 /ca 唯一的出口:查不到时还有几条**不走装配函数**的早退回执,
    而地址(normalize_token_address 对非 0x/42 位原样透传)和链名(normalize_network
    对未收录的值原样透传)都是用户可控的无界字符串。这里从命令入口整条扫,
    把 normalize → _ca_clip → 渲染行 → 装配 这条链路一起罩住。
    """
    class _Mut:
        items: list = []

        def get_token_thesis(self, token_address, network_id, after_ms=None, limit=100):
            return self.items

    client = _Mut()
    b, _ = _bot(monkeypatch, tmp_path, client)
    rng = random.Random(4242)
    for _ in range(_FUZZ_CMD_CASES):
        client.items = [_evil_row(rng) for _ in range(rng.randrange(0, 12))]
        addr = _evil_token(rng, rng.randrange(1, 30))
        arg = addr if rng.random() < 0.5 else f"{addr} {_evil_token(rng, rng.randrange(1, 20))}"

        out = b._cmd_ca(arg)

        if client.items:
            _assert_ca_invariant(out)                      # 有观点 → 走装配,锚点收尾
        else:
            _assert_ca_invariant(out, anchored=False)      # 早退回执:锚点在第一行
            assert "<code>" in out, f"早退回执里 CA 锚点同样必须在:{out[:120]!r}"


def test_上游把字段上限拿掉后出口不变式仍然兜住(monkeypatch, tmp_path):
    """
    这条测试就是本轮的**主张**本身:安全性不许依赖"每个字段都记得 clip"。

    把 ticker / handle / 链名 / 摘要四处上限统统放宽到 50000(等价于"上游哪天忘了限长",
    或者"接口新加了一个没人 clip 的字段"),出口不变式必须照样成立。
    ⚠️ 而且要求比"发得出去"更进一步:那些超长的行该被**截短后照常展示**,
       不是被整块跳过 —— 一个人名字长,不该等于这个人从消息里消失。
    """
    import src.bot as bot_mod

    for name in ("CA_TICKER_CHARS", "CA_HANDLE_CHARS", "CA_CHAIN_CHARS",
                 "CA_THESIS_SNIPPET_CHARS"):
        monkeypatch.setattr(bot_mod, name, 50_000)
    # 每人 handle 不同,否则 _ca_one_row_per_author 会把八条并成一位作者
    items = [_item(handle="H" * 9000 + str(i), usd=float(900 - i), ticker="T" * 9000,
                   network="N" * 9000, text="'" * 9000) for i in range(8)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    _assert_ca_invariant(out)
    assert "@" + "H" * 100 in out, \
        f"超长的行该被截短后照常展示,不该被整块跳过:\n{out[:200]!r}"
    assert "位未显示" in out, "塞不下的必须如实说"


def test_单行收口只在原子边界上切():
    """
    _ca_fit_line 是出口不变式唯一"切进一行内部"的地方,所以它切错就等于不变式失效。
    ⚠️ 上限一律用字面量传进来,不从被测模块 import(自指的门槛等于没有门槛)。
    """
    from src.bot import _ca_fit_line

    # 实体是一个原子:limit 卡在 `&amp;` 中间时整个实体一起让位,绝不切出 `&am`
    assert _ca_fit_line("A&amp;B&amp;C", 8) == "A&amp;B…"
    assert _BROKEN_ENTITY.search(_ca_fit_line("A&amp;B&amp;C", 8)) is None
    # 标签也是一个原子,而且切点之后要把还开着的标签补齐 —— 否则 `<code>` 开着口
    assert _ca_fit_line("<code>" + "Z" * 50 + "</code>", 20) == "<code>ZZZZZZ…</code>"
    # 不认识的标签当普通文本转义掉(上游漏 _esc 时唯一安全的处置)
    assert _ca_fit_line("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"
    # 落单的 `&` / `<` 同样转义掉
    assert _ca_fit_line("a & b <3") == "a &amp; b &lt;3"
    # 换行叠平:预算按行算,行数被上游控制就等于预算被上游控制
    assert _ca_fit_line("a\nb\tc") == "a b c"
    # 够短的普通行原样返回(绝大多数行走这条,不能被顺手改了样子)
    assert _ca_fit_line("@alice · $28.52 · 未实现 +$10.72 (+60.3%)") == \
        "@alice · $28.52 · 未实现 +$10.72 (+60.3%)"
    assert _ca_fit_line("   @bob · 💎$1.00K 进场") == "   @bob · 💎$1.00K 进场"


def test_最长的合法正文不会被单行上限误伤(monkeypatch, tmp_path):
    """
    出口的单行上限是**兜底**,不是排版规则:本命令自己渲染得出的最长一行 ——
    140 个 `'` 的观点摘要,转义后 846 字符 —— 必须一个字都不少地出来。
    上限调得太紧(比如 320)就会开始切正常内容,那是把兜底当成了排版规则。
    """
    client = _FakeClient({"56": [_item(handle="talky", text="'" * 300)]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.count("&#x27;") == 140, \
        f"摘要上限是 140 个字符,转义后就该是 140 个完整实体,实际 {out.count('&#x27;')}"
    _assert_ca_invariant(out)


# ============================================================
# 上一轮漏掉的那四处无界字段 —— 现在应当由出口不变式自动兜住
# ============================================================
def test_接口错误文案再长也撑不破消息且不被整段丢掉(monkeypatch, tmp_path):
    """
    `💭 观点没拉到:{err}` 里的 err 来自服务端返回体,转义后能到近千字符。
    ⚠️ 断言不止"没撑破":还要求这一行**被截短而不是被整段砍掉** ——
       head 是最后才砍的一段,不收口的话兜底循环只能把它整条 pop 掉,
       于是用户既看不到错误原因,也不知道发生了什么。
    """
    client = _FakeClient(raise_exc=FomoAPIError("'" * 5000))
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="holder", amount_usd=500.0, market_cap=100_000.0)

    out = b._cmd_ca(CA_BSC)

    _assert_ca_invariant(out)
    assert "观点没拉到" in out, "错误那一行不该被整段砍掉"
    assert "@holder" in out, "本地那半段照常出,不跟着接口异常一起丢(两段解耦)"


def test_已试链名再长也撑不破消息且不被整段丢掉(monkeypatch, tmp_path):
    """
    `💭 没查到观点(已试 {链名})` 的链名来自本地库的 network_id,**一个字符都没限过**。
    """
    client = _FakeClient({})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="holder", amount_usd=500.0, market_cap=100_000.0,
                network="N" * 5000)

    out = b._cmd_ca(CA_BSC)

    _assert_ca_invariant(out)
    assert "没查到观点" in out, "这一行不该被整段砍掉"
    assert "NNNNNNNNNN" in out, "链名该被截短,不是整行消失"


def test_指定链时超长链名也撑不破这条早退回执(monkeypatch, tmp_path):
    """
    指定链却查不到时,那条回执**直接 return、不走 _ca_assemble** —— 出口不变式
    管不到它,得靠这一行自己过 _ca_fit_line。而 normalize_network 对未收录的值
    原样透传,用户打一个几千字符的"链名"就能撑破消息。
    """
    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} {'C' * 6000}")

    _assert_ca_invariant(out, anchored=False)
    assert "没查到观点" in out, "这条回执本身不该消失"
    assert "cccccccccc" in out, "链名该被截短,不是整行消失(normalize_network 会转小写)"


def test_本地名单区的买家名再长也撑不破消息且不被整段丢掉(monkeypatch, tmp_path):
    """买家 handle 来自接口、经 DB 落地,一路上没有任何长度校验"""
    client = _FakeClient({"56": []})
    b, store_ = _bot(monkeypatch, tmp_path, client)
    _seed_local(store_, handle="B" * 5000, amount_usd=500.0, market_cap=100_000.0)

    out = b._cmd_ca(CA_BSC)

    _assert_ca_invariant(out)
    assert "@" + "B" * 100 in out, "买家那一行该被截短,不是被整行丢掉"


def test_百分比大到离谱时不白吃掉别人的展示位(monkeypatch, tmp_path):
    """
    ⚠️ 这一处出口不变式**覆盖不了**,所以仍然单独管 —— 但管的是**记法**不是长度:
       不变式保证"这条消息发得出去",保证不了"这条消息里还剩几位作者"。
       pct 直接来自接口没有上限,`f"{1e300:+.1f}%"` 是 302 个字符、一行两处 ——
       实测(修复前)8 位作者只渲染得出 5 位,一个字段白吃掉三分之一的展示位。
    """
    items = [_item(handle=f"p{i}", usd=float(900 - i), pnl=10.0, pct=1e300,
                   realized=5.0, realized_pct=1e300) for i in range(8)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    for i in range(8):
        assert f"@p{i}" in out, f"@p{i} 被一个百分比挤掉了:\n{out[:400]}"
    assert "位未显示" not in out
    row = _thesis_line_of(out, "p0")
    assert len(row) <= _MAX_SANE_ROW_LINE, f"一行 {len(row)} 字符,百分比还在白吃展示位:{row[:80]!r}"
    _assert_ca_invariant(out)


def test_金额大到离谱时不白吃掉别人的展示位(monkeypatch, tmp_path):
    """
    与百分比同一件事的另一半:_money 每三位插一个逗号,`_money(1e300)` 是 391 个字符,
    一行同样有两处(持仓额 + 盈亏)。实测(修复前)8 位作者只渲染得出 4 位。
    ⚠️ 只在 /ca 这一层换记法,不动 _money 本身 —— 那是 /hot 等命令共用的排名展示格式。
    """
    items = [_item(handle=f"m{i}", usd=1e300, pnl=1e300, pct=10.0,
                   realized=1e300, realized_pct=5.0) for i in range(8)]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    for i in range(8):
        assert f"@m{i}" in out, f"@m{i} 被一个金额挤掉了:\n{out[:400]}"
    assert "位未显示" not in out
    row = _thesis_line_of(out, "m0")
    assert len(row) <= _MAX_SANE_ROW_LINE, f"一行 {len(row)} 字符,金额还在白吃展示位:{row[:80]!r}"
    _assert_ca_invariant(out)


# ============================================================
# 短字段收口的**顺序** —— 铁律写在 docstring 里,却一直零覆盖
# ============================================================
def test_短字段必须先截断后转义否则切出残缺实体():
    """
    _ca_clip 的"叠平空白 → 按原文截断 → 转义"这个顺序是本仓库的历史事故换来的,
    可它一直**零覆盖**:把顺序改成先转义后截断,531 个测试全绿,而输出确实是
    `&am` 这种残缺实体,整条 TG 消息 400。两位验证者独立发现了同一条。

    ⚠️ limit 用字面量传,断言写成逐字符相等 —— 不从被测模块 import 任何门槛。
    """
    from src.bot import _ca_clip

    # 原文 13 个字符,取前 10 个 → 8 个 A + 2 个 `&`,转义后是两个**完整**的 &amp;
    # 反过来(先把 `&` 转义成 `&amp;` 再取前 10 个)必然切在 `&am` 上
    assert _ca_clip("AAAAAAAA" + "&" * 5, 10) == "AAAAAAAA&amp;&amp;…"
    assert _BROKEN_ENTITY.search(_ca_clip("AAAAAAAA" + "&" * 5, 10)) is None
    # 尖括号同理:先截断再转义永远得到成对的 &lt; / &gt;
    assert _ca_clip("<b>hello</b>", 5) == "&lt;b&gt;he…"
    # 没超长就不该有省略号,也不该被动过
    assert _ca_clip("&x", 10) == "&amp;x"


def test_本金是两半相加而不是取其中一半():
    """
    这条命令最核心的那个公式**一个测试都没有**:把 `total + sold` 改成
    `max(total, sold)`,531 个测试全绿 —— 而拿 data/fomo_probe/thesis.json 的真实 100 条
    实算过,它会改变展示顺序:前 8 位里 @DukerzBreadz 从第 5 掉到第 8。

    在持那半 500 - 200 = 300,已卖那半 500 / 50% = 1000,相加 1300。
    这三个数互不相同,取 max、取 min、只取任何一半都对不上 1300。
    """
    from src.bot import _ca_cost_usd

    got = _ca_cost_usd({"authorTrade": {
        "usdValue": 500.0, "unrealizedPnlUsd": 200.0,
        "realizedPnlUsd": 500.0, "percentageRealizedPnl": 50.0,
    }})

    assert got == pytest.approx(1300.0), f"两半必须相加(300 + 1000),实际 {got!r}"


def test_两半相加改变排序_不是纯粹的数值细节(monkeypatch, tmp_path):
    """
    上一条钉住公式本身,这一条钉住它**看得见的后果** —— 两个数必须挑得刚好:
    @both 在持那半 500、已卖那半 600,相加 1100 压过 @held 的 900;
    可只要取较大的那一半(600),他反过来被 @held 压下去,展示顺序当场就变了。
    ⚠️ 数挑不好这条测试就是空转:第一版写的是 500/1000,取 max 得 1000 仍然 > 900,
       变异照样全绿 —— 变异验证当场逮到了这一点。
    """
    items = [
        # 在持 700 - 200 = 500,已卖 300 / 50% = 600,两半相加 1100
        _item(handle="both", usd=700.0, pnl=200.0, pct=40.0, realized=300.0, realized_pct=50.0),
        # 纯在持 900:比 1100 小,但比 600 大
        _item(handle="held", usd=900.0, pnl=0.0, pct=0.0),
    ]
    client = _FakeClient({"56": items})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert out.index("@both") < out.index("@held"), \
        f"1100 > 900,@both 必须排在前面;取较大那半就是 600 < 900,顺序会反过来:\n{out}"


# ============================================================
# "已经落袋了多少"的三种状态:卖过 / 一次没卖过 / 不知道
# ============================================================
def test_已实现的三种状态输出必须各不相同(monkeypatch, tmp_path):
    """
    ⚠️ 上一轮的 `realized is not None and realized != 0` 把后两种压成了**逐字节相同**的
       输出:实测 realized=0.0 与 realized=None 一模一样,读者无法区分。
       "一次没卖过就不显示这一段"这个决定本身是合理的(信息量恒为 0),
       问题在于沉默已经被定义成"没卖过"了,再拿沉默表示"不知道"就是断言了一件
       我们不知道的事。所以 None 要写明"未知"(与 _day_str 的"时间未知"同一套处理)。
    """
    unknown = _item(handle="unknown", usd=200.0, pnl=25.0, pct=14.0)
    unknown["authorTrade"].pop("realizedPnlUsd")
    unknown["authorTrade"].pop("percentageRealizedPnl")
    client = _FakeClient({"56": [
        _item(handle="sold", usd=200.0, pnl=25.0, pct=14.0, realized=80.0, realized_pct=40.0),
        _item(handle="never", usd=200.0, pnl=25.0, pct=14.0, realized=0.0, realized_pct=0.0),
        unknown,
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "sold") == \
        "@sold · $200.00 · 未实现 +$25.00 (+14.0%) · 已实现 +$80.00 (+40.0%)"
    assert _thesis_line_of(out, "never") == \
        "@never · $200.00 · 未实现 +$25.00 (+14.0%)"
    assert _thesis_line_of(out, "unknown") == \
        "@unknown · $200.00 · 未实现 +$25.00 (+14.0%) · 已实现 未知"
    bodies = {_thesis_line_of(out, h).split(" · ", 1)[1] for h in ("sold", "never", "unknown")}
    assert len(bodies) == 3, f"三种状态必须给出三种输出,实际只有 {len(bodies)} 种:{bodies}"
    assert "N/A" not in out and "--" not in out and "None" not in out


def test_整行都没渲染出来时不再挂一句已实现未知(monkeypatch, tmp_path):
    """
    "未知"那句是给**已经在陈述仓位**的行做澄清用的。authorTrade 整个缺失、上面一段都
    没渲染出来的行本来就没许诺任何事,再挂一句"已实现 未知"只是噪音。
    """
    it = {
        "ticker": "ELOY", "networkId": "bsc", "comment": "还行",
        "userHandle": "nodata", "displayName": "nodata",
    }
    client = _FakeClient({"56": [it]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "nodata") == "@nodata"


def test_在持路径上尘埃已实现不印成假的打平(monkeypatch, tmp_path):
    """
    realized = 0.004 / -1e-13 都能穿过 `realized != 0`,再被 _money 印成 `+$0.00` ——
    正是上一轮论证要避免的那句假事实("卖过、只是刚好打平")。
    _CA_DUST_USD 这道守卫 _ca_pos_str 早就在用,这条路径一直没用上。
    ⚠️ 方向必须留住:一个不带方向的"不足 $0.01"读者分不清是赚是亏。
    """
    client = _FakeClient({"56": [
        _item(handle="dustwin", usd=200.0, pnl=25.0, pct=14.0,
              realized=0.004, realized_pct=1.0),
        _item(handle="dustloss", usd=200.0, pnl=25.0, pct=14.0,
              realized=-1e-13, realized_pct=-0.5),
    ]})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_ca(f"{CA_BSC} bsc")

    assert _thesis_line_of(out, "dustwin") == \
        "@dustwin · $200.00 · 未实现 +$25.00 (+14.0%) · 已实现 赚不足 $0.01 (+1.0%)"
    assert _thesis_line_of(out, "dustloss") == \
        "@dustloss · $200.00 · 未实现 +$25.00 (+14.0%) · 已实现 亏不足 $0.01 (-0.5%)"
    assert "+$0.00" not in out, "尘埃被印成 +$0.00 就是凭空断言「卖过、刚好打平」"
    assert "-$0.00" not in out, "带负号的零是纯粹的浮点垃圾"


# ============================================================
# /status 的「今日事件」口径
# ============================================================
def test_status的今日事件不把转账算进去(monkeypatch, tmp_path):
    """
    ⚠️ 与网页版看板同一口径(web.queries.dashboard)。这个数字回答的是
       "名单今天动了多少次",而转账是 2026-08 才加的采集,实测非稳定币转入
       13.1 条/人/天 —— 算进来会让它虚增约 10 倍。
       同一个数字悄悄换了含义,比数字算错了更难被发现。
    """
    from src.models import (
        EVENT_TRANSFER_IN,
        EVENT_TRANSFER_OUT,
        FomoEvent,
        dump_raw,
        now_iso,
    )

    b, store_ = _bot(monkeypatch, tmp_path, client=None)
    today = now_iso()
    with store_.get_conn() as c:
        store_.add_watch_user(c, "u1", "alice", "alice")
        store_.mark_stats_ready(c, "u1")
        ev = FomoEvent(event_id="BUY:1", event_type=EVENT_BUY, user_id="u1",
                       event_ts=today, raw_json=dump_raw({}), handle="alice",
                       network_id="solana", token_address=CA_SOL, token_symbol="X",
                       amount_usd=2500.0)
        ev.badge_reason = REASON_LOCAL_STATS
        store_.insert_event(c, ev)
        for i, kind in enumerate([EVENT_TRANSFER_IN] * 5 + [EVENT_TRANSFER_OUT] * 3):
            store_.insert_event(c, FomoEvent(
                event_id=f"{kind}:{i}", event_type=kind, user_id="u1", event_ts=today,
                raw_json=dump_raw({}), handle="alice", network_id="solana",
                token_address=CA_SOL, token_symbol="X", amount_usd=900.0))
        assert c.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 9, \
            "前提不成立:九条事件没都落库"

    out = b._cmd_status()
    assert "📨 今日事件 1 条" in out, f"转账把今日事件灌水了:{out}"


# ============================================================
# /tin —— 指定用户的转入逐条推送:开关 + 清单
# ============================================================
def test_tin命令名不与既有命令冲突且已挂进菜单和帮助():
    """
    ⚠️ 菜单里有、_dispatch 里没有 = 用户点了只得到"未知命令";
       反过来则是"有这个功能但没人知道"。两边必须同步维护。
    """
    from src.bot import _COMMAND_MENU, _HELP

    names = [n for n, _ in _COMMAND_MENU]
    assert names.count("tin") == 1, f"命令名重复或没挂上菜单:{names}"
    assert len(names) == len(set(names)), f"菜单里有重名命令:{names}"
    assert "/tin" in _HELP


def test_tin对名单外的人给出可操作提示(monkeypatch, tmp_path):
    b, _ = _bot(monkeypatch, tmp_path)
    out = b._dispatch("/tin", "nobody")
    assert "未知命令" not in out, "命令没挂进 _dispatch"
    assert "没有" in out and "/add" in out, f"要引导他先 /add:{out}"


def test_tin是开关_再发一次就关掉并且清单跟着变(monkeypatch, tmp_path):
    b, store = _bot(monkeypatch, tmp_path)
    with store.get_conn() as c:
        store.add_watch_user(c, "u1", "PoorGoat_", "PoorGoat")

    assert "一个人都没开" in b._dispatch("/tin", "")

    on = b._dispatch("/tin", "PoorGoat_")
    assert "已开启" in on, on
    listed = b._dispatch("/tin", "")
    assert "PoorGoat" in listed and "@PoorGoat_" in listed
    assert "$100.00" in listed, f"清单里要写清门槛,否则用户不知道为什么没推:{listed}"

    off = b._dispatch("/tin", "PoorGoat_")
    assert "已关闭" in off, off
    assert "一个人都没开" in b._dispatch("/tin", "")


def test_tin命令真的把人数上限传下去了_第十三个必须被拦(monkeypatch, tmp_path):
    """
    ⚠️⚠️ 上限的**唯一执行路径**就是 /tin,而 store.set_transfer_watch 的 max_on
       默认是 None(= 不限)。直接调 store 并显式传 max_on 只证明"这个形参能用",
       一个字都不证明命令这一侧真的把上限传了下去 —— 不传就是彻底没有上限。
    ⚠️ 超限的后果不是"多推几条":poller 每轮都会撞上超限分支、**整体退回轮转**,
       时效承诺当场作废,而用户在 TG 里收到的回执是"已开启"。所以必须在开之前就拦。
    ⚠️ 12 写死(它是 poller 的单轮请求预算算出来的外部事实),不从被测模块 import。
    """
    b, store = _bot(monkeypatch, tmp_path)
    with store.get_conn() as c:
        for i in range(13):
            store.add_watch_user(c, f"u{i:02d}", f"h{i:02d}", f"H{i:02d}")

    outs = [b._dispatch("/tin", f"h{i:02d}") for i in range(13)]
    assert all("已开启" in o for o in outs[:12]), f"前 12 个都该开得成:{outs[:12]}"
    assert "已开启" not in outs[12], f"第 13 个被放行了 —— 上限压根没传下去:{outs[12]}"
    assert "最多" in outs[12] and "12" in outs[12], f"回执要说清为什么被拦:{outs[12]}"

    with store.get_conn() as c:
        assert len(store.transfer_watch_user_ids(c)) == 12, "库里真正开着的必须只有 12 个"
    assert "12/12" in b._dispatch("/tin", ""), "清单上的人数也不许超"


def test_tin的回执与清单都要转义(monkeypatch, tmp_path):
    """昵称/handle 带 '<' 并不罕见,不转义整条回执直接 400 —— 用户什么都收不到"""
    b, store = _bot(monkeypatch, tmp_path)
    with store.get_conn() as c:
        store.add_watch_user(c, "u1", "ev<il", "<b>boom</b>")
    assert "<b>boom</b>" not in b._dispatch("/tin", "ev<il"), "回执没转义"
    listed = b._dispatch("/tin", "")
    assert "&lt;b&gt;boom" in listed and "<b>boom</b>" not in listed
    assert _tag_fault(listed) == "", listed


# ============================================================
# /tin <handle> <金额> —— 每人各自的门槛
# ⚠️ 金额一律**写死字面量**,不从 config / store / bot import 任何门槛值。
# ============================================================
def _tin_two(monkeypatch, tmp_path):
    """两个人的名单:巨鲸 Alice + 小额 Bob。⚠️ 全局门槛显式钉成 100 —— 不钉的话
    断言的是"这台机器的 .env 怎么配的",而不是代码行为"""
    from src.config import get_settings

    monkeypatch.setenv("FOMO_TRANSFER_WATCH_MIN_USD", "100")
    get_settings.cache_clear()
    b, store = _bot(monkeypatch, tmp_path)
    with store.get_conn() as c:
        store.add_watch_user(c, "uA", "alice", "Alice")
        store.add_watch_user(c, "uB", "bob", "Bob")
    return b, store


def _line_of(listed: str, name: str) -> str:
    return next(ln for ln in listed.split("\n") if name in ln)


def test_tin可以给每个人设不同的门槛_清单上各是各的(monkeypatch, tmp_path):
    """
    用户原话:「人物A 我可以设置 30000,人物B 我可以设置 200」。
    ⚠️ 清单必须**逐人**显示门槛:只在抬头写一个全局值的话,用户永远看不出
       自己刚才那条 /tin 到底生效在谁身上。
    """
    b, _ = _tin_two(monkeypatch, tmp_path)

    a = b._dispatch("/tin", "alice 30000")
    bb = b._dispatch("/tin", "bob 200")
    listed = b._dispatch("/tin", "")

    assert "已开启" in a and "$30,000.00" in a, f"回执要说清现在是多少:{a}"
    assert "已开启" in bb and "$200.00" in bb, bb
    assert "2/12" in listed, f"两个人都该在清单里:{listed}"
    a_line, b_line = _line_of(listed, "Alice"), _line_of(listed, "Bob")
    assert "$30,000.00" in a_line, f"A 那一行的门槛不对:{a_line}"
    assert "$200.00" in b_line, f"B 那一行的门槛不对:{b_line}"
    assert "$30,000.00" not in b_line, f"两个人共用了同一个门槛:{b_line}"
    assert _tag_fault(listed) == "", listed


def test_tin带金额时只改门槛_绝不把已经开着的人关掉(monkeypatch, tmp_path):
    """
    ⚠️⚠️ 语法上的关键决定。写成"带金额也切换"的话,
       「已开在 $30000、再发 /tin alice 200」就分不出是关掉还是改门槛,
       而关掉的后果是从此一条推送都收不到 —— 用户不会立刻发现。
    """
    b, store_ = _tin_two(monkeypatch, tmp_path)
    b._dispatch("/tin", "alice 30000")

    out = b._dispatch("/tin", "alice 200")

    assert "已关闭" not in out, f"带金额被当成开关、把人关掉了:{out}"
    assert "$200.00" in out and "$30,000.00" in out, \
        f"回执要说清「现在多少、之前多少」:{out}"
    with store_.get_conn() as c:
        assert store_.transfer_watch_user_ids(c) == {"uA"}, "人被关掉了"
    assert "$200.00" in _line_of(b._dispatch("/tin", ""), "Alice")


def test_tin不带金额仍然是开关_而且门槛留着(monkeypatch, tmp_path):
    """
    不带金额时若也当成"设置",就再也没有写法能关掉了。
    ⚠️ 关掉时门槛**留着**:它在开关关着时完全不参与判定,清掉反而会让下次打开的
       推送量静默涨回全局默认($30000 的 0.5 条/天 → $100 的 1.1 条/天)。
       但"留着"必须写进回执,否则就是暗的。
    """
    b, _ = _tin_two(monkeypatch, tmp_path)
    b._dispatch("/tin", "alice 30000")

    off = b._dispatch("/tin", "alice")
    assert "已关闭" in off, f"不带金额必须能关掉:{off}"
    assert "$30,000.00" in off, f"门槛留着这件事要写明:{off}"
    assert "一个人都没开" in b._dispatch("/tin", "")

    on = b._dispatch("/tin", "alice")
    assert "已开启" in on and "$30,000.00" in on, f"重新打开必须还是他自己设的数:{on}"


def test_tin清单能分出跟随默认与显式设成同一个值(monkeypatch, tmp_path):
    """
    ⚠️⚠️ 两者语义**不同**:跟随的那个会随 .env 里的 FOMO_TRANSFER_WATCH_MIN_USD
       一起变,显式设成 100 的那个不会。清单上都印成 `$100.00` 的话,
       用户没法判断改 .env 会不会影响到这个人。
    """
    b, _ = _tin_two(monkeypatch, tmp_path)
    b._dispatch("/tin", "alice")          # 不带金额 → 跟随全局默认($100)
    b._dispatch("/tin", "bob 100")        # 显式设成同一个数

    listed = b._dispatch("/tin", "")
    a_line, b_line = _line_of(listed, "Alice"), _line_of(listed, "Bob")

    assert "$100.00" in a_line and "$100.00" in b_line, (a_line, b_line)
    assert "默认" in a_line, f"跟随全局的那个必须标出来:{a_line}"
    assert "默认" not in b_line, f"显式设过的不许标成默认:{b_line}"


def test_tin门槛可以清回跟随全局(monkeypatch, tmp_path):
    """显式设过之后必须有路回到「跟着 .env 变」,否则那是个单向的死角"""
    b, _ = _tin_two(monkeypatch, tmp_path)
    b._dispatch("/tin", "alice 30000")

    out = b._dispatch("/tin", "alice 默认")

    assert "默认" in out and "$100.00" in out, f"要说清它现在跟着全局的多少走:{out}"
    a_line = _line_of(b._dispatch("/tin", ""), "Alice")
    assert "默认" in a_line and "$30,000.00" not in a_line, a_line


def test_tin门槛零合法_而且不会被当成关掉(monkeypatch, tmp_path):
    """
    ⚠️ 铁律:0 是有意义的真实值(= 这个人的转入全推)。
       它既不能被解析器当成"没填",也不能在库里与"关掉"撞成同一个状态。
    """
    b, store_ = _tin_two(monkeypatch, tmp_path)

    out = b._dispatch("/tin", "alice 0")

    assert "已开启" in out, f"门槛 0 被当成关掉了:{out}"
    assert "$0.00" in out, f"回执要写明现在是 0:{out}"
    with store_.get_conn() as c:
        assert store_.transfer_watch_user_ids(c) == {"uA"}, "开关必须是开着的"
    a_line = _line_of(b._dispatch("/tin", ""), "Alice")
    assert "$0.00" in a_line and "默认" not in a_line, \
        f"0 在清单里被显示成了「跟随默认」:{a_line}"


def test_tin拒绝负数并把零那条路指出来(monkeypatch, tmp_path):
    """
    负数没有任何可执行的含义。⚠️ 但光说"不行"没用:敲负数的人多半想表达的
    就是"别筛了全给我",回执必须把 0 指出来,否则他只能瞎试。
    """
    b, store_ = _tin_two(monkeypatch, tmp_path)

    for bad in ("-1", "-0.01", "-30000", "-inf"):
        out = b._dispatch("/tin", f"alice {bad}")
        assert "已开启" not in out and "已关闭" not in out, f"{bad!r} 被接受了:{out}"
        assert "负数" in out and "写 0" in out, f"回执要给出可操作的下一步:{out}"
        assert _tag_fault(out) == "", out
    with store_.get_conn() as c:
        assert store_.transfer_watch_user_ids(c) == set(), "被拒绝的命令不许改库"


def test_tin拒绝非数字并说清正确用法(monkeypatch, tmp_path):
    """
    ⚠️ 一个都不许猜:猜错的方向是"门槛变成了别的数",而用户看到的是"已开启"。
    ⚠️ nan / inf 单独钉死 —— 裸 float() 全都收,而 `x >= nan` 恒为 False,
       这个人的推送从此一条都不来,且没有任何报错。
    ⚠️ 全角「５」也要拒:re 的 \\d 和 float() 都收它,一个看不出区别的字符
       就能让人以为自己设的是别的数。
    """
    b, store_ = _tin_two(monkeypatch, tmp_path)

    for bad in ("abc", "30元", "nan", "inf", "1e5", "0x10", "3.1.4", "$",
                "1m", "５", "30,00", "1234,567", "999999999" * 45):
        out = b._dispatch("/tin", f"alice {bad}")
        assert "已开启" not in out and "已关闭" not in out, f"{bad!r} 被接受了:{out}"
        assert "用法" in out or "太大" in out, f"{bad!r} 的回执没说清怎么办:{out}"
        assert _tag_fault(out) == "", f"{bad!r} 的回执标签坏了:{out}"
    with store_.get_conn() as c:
        assert store_.transfer_watch_user_ids(c) == set(), "被拒绝的命令一条都不许改库"


def test_tin接受的每一种金额写法都要对(monkeypatch, tmp_path):
    """
    ⚠️ 每一种写法各测一次:只测 `30000` 的话,k / w / 万 / 千分位随便哪个
       算错 1000 倍都不会红,而算错的后果是"从此一条都不推"。
    ⚠️ 期望值全部手算写死,不复用解析器里的任何常量。
    ⚠️ 每个用例一个独立库:同一个库里第二次设同一个数会走"本来就是"那一支。
    """
    cases = [
        ("30000", "$30,000.00"),
        ("30,000", "$30,000.00"),
        ("$200", "$200.00"),
        ("$1,234.50", "$1,234.50"),
        ("99.99", "$99.99"),
        ("30k", "$30,000.00"),
        ("30K", "$30,000.00"),
        ("3w", "$30,000.00"),
        ("3W", "$30,000.00"),
        ("3万", "$30,000.00"),
        ("0.5k", "$500.00"),
        ("0", "$0.00"),
    ]
    for i, (raw, want) in enumerate(cases):
        b, _ = _tin_two(monkeypatch, tmp_path / f"case{i}")
        out = b._dispatch("/tin", f"alice {raw}")
        assert "已开启" in out, f"{raw!r} 没被接受:{out}"
        assert want in out, f"{raw!r} 应解析成 {want},实际回执:{out}"


def test_tin参数多于两个要给用法而不是猜(monkeypatch, tmp_path):
    """`/tin alice 30 000`(拿空格当千分位)必须回用法 —— 猜成 30 就差 1000 倍"""
    b, store_ = _tin_two(monkeypatch, tmp_path)
    out = b._dispatch("/tin", "alice 30 000")
    assert "已开启" not in out and "用法" in out, out
    with store_.get_conn() as c:
        assert store_.transfer_watch_user_ids(c) == set()


def test_tin帮助里带上推送频率参照(monkeypatch, tmp_path):
    """
    门槛是个"调了才知道"的数字。不给参照系的话用户只能瞎试 ——
    而每试一次的反馈周期是**一天**(推送量按天计)。
    """
    b, _ = _tin_two(monkeypatch, tmp_path)
    empty = b._dispatch("/tin", "")
    b._dispatch("/tin", "alice 30000")
    listed = b._dispatch("/tin", "")

    for out in (empty, listed):
        assert "条/天" in out, f"没给推送频率参照:{out}"
        assert "8.1" in out and "1.1" in out, f"频率表的关键几行要在:{out}"
        assert "≥$5000" in out, f"高门槛那一端也要有,否则 $30000 无从参照:{out}"
        assert _tag_fault(out) == "", out


def test_tin带金额的回执与错误文案都要转义(monkeypatch, tmp_path):
    """昵称带 '<' 并不罕见;错误回执还会把**用户原样输入**回显,更要转义"""
    b, store_ = _bot(monkeypatch, tmp_path)
    with store_.get_conn() as c:
        store_.add_watch_user(c, "u1", "ev<il", "<b>boom</b>")

    ok = b._dispatch("/tin", "ev<il 30000")
    assert "<b>boom</b>" not in ok and "&lt;b&gt;boom" in ok, f"回执没转义:{ok}"
    assert _tag_fault(ok) == "", ok

    bad = b._dispatch("/tin", "ev<il -<i>x</i>")     # 负数那一支会回显用户输入
    assert "<i>x</i>" not in bad, f"用户输入被原样回显进 HTML 了:{bad}"
    assert "&lt;i&gt;x" in bad, f"回显必须转义后出现:{bad}"
    assert _tag_fault(bad) == "", bad
