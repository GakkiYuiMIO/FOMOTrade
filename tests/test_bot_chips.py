"""
/chips <合约地址> 的单测。

⚠️ 这条命令的价值不在数字,而在**诚实**:/hodlers/top 被服务端钳在 100 条、
   还叠了一道约 $2 的持仓下限,所以多数情况下我们手上只是全部持有人的一小撮。
   len(topHolders) == totalHolders 时统计才精确,否则只是下界。
   本文件盯的第一重点就是**这两种情况的文案必须一眼可辨**,并且判据本身不能被改坏。

⚠️ 断言门槛一律写字面量,**不从 src.bot import 任何常量** —— 门槛一旦从被测模块来,
   实现把上限调宽断言就跟着调宽,测试变成永远为真的同义反复
   (test_bot.py 头部记过这个坑,这里同样适用)。
"""
# ruff: noqa: N802
from __future__ import annotations

import random
import re

import pytest

from src.client import FomoAPIError

CA_SOL = "BoAQaykj3LtkM2Brevc7cQcRAzpqcsP47nJ2rkyopump"
CA_BSC = "0xfc6e0617a6cc19afe9e0d367e97d07245d357777"

# Telegram sendMessage 的协议上限。这是**外部事实**,故意写死
_TG_HARD_LIMIT = 4096
# html.escape(quote=True) 只会产出这五种实体;出现别的 `&` 开头片段 = 有实体被切断
_BROKEN_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27|#39);)")
_TAG = re.compile(r"<(/?)([a-zA-Z][^<>]*)>")


def _tag_fault(s: str) -> str:
    """标签有没有配对闭合。自己数一遍,不复用 src.bot 里的任何东西(复用就成自指了)"""
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


def _assert_chips_invariant(out: str, *, anchored: bool = True) -> None:
    """
    /chips 与 /ca 共用同一套装配出口,所以出口不变式也是同一条:
      1. 长度在 TG 协议上限内 —— 超了 notifier 就盲切,切点落在实体中间即整条 400
      2. HTML 合法 —— 没有残缺实体、没有裸 `<` / `>`、标签全部配对闭合
      3. CA 锚点在,而且是完整闭合的最后一行
    """
    assert len(out) <= _TG_HARD_LIMIT, f"超过 TG 协议上限(实际 {len(out)})"
    m = _BROKEN_ENTITY.search(out)
    assert m is None, f"残缺 HTML 实体 @{m.start() if m else -1}: {out[max(0, (m.start() if m else 0) - 25):][:60]!r}"
    fault = _tag_fault(out)
    assert not fault, f"{fault}\n{out[:200]!r}"
    n_tags = len(_TAG.findall(out))
    assert out.count("<") == n_tags, f"出现了不属于任何标签的裸 `<`:{out[:200]!r}"
    assert out.count(">") == n_tags, f"出现了不属于任何标签的裸 `>`:{out[:200]!r}"
    if anchored:
        assert out.endswith("</code>"), f"CA 锚点必须活到最后一行,实际结尾: {out[-80:]!r}"


def _holder(uid, handle="stranger", amount=1000.0, value=10.0):
    """一条 topHolders 记录。形状照抄实测:user 是嵌套对象,id 是 UUID"""
    return {
        "user": {"id": uid, "userHandle": handle, "displayName": handle,
                 "address": "So1" + str(uid)},
        "tradeId": f"t-{uid}", "humanAmount": amount, "value": value,
        "price": 0.0001, "costBasis": 1.0, "isDev": False,
    }


def _meta(symbol="FOREST", supply="1000000000"):
    """filterTokens 的返回形状:总供应量埋在 .token.info.totalSupply"""
    return {"token": {"info": {"symbol": symbol, "totalSupply": supply}}}


class _FakeClient:
    """按数字链 ID 返回预设的持有人榜;exc 给了就每次都抛它"""

    def __init__(self, by_net=None, meta=None, *, exc=None, meta_exc=None):
        self.by_net = by_net or {}
        self.meta = meta if meta is not None else _meta()
        self.exc = exc
        self.meta_exc = meta_exc
        self.calls: list[tuple[str, str]] = []
        self.meta_calls: list[tuple[str, str]] = []

    def get_top_holders(self, token_address, network_id):
        self.calls.append((token_address, network_id))
        if self.exc is not None:
            raise self.exc
        return self.by_net.get(network_id, {})

    def get_token_meta(self, token_address, network_id):
        self.meta_calls.append((token_address, network_id))
        if self.meta_exc is not None:
            raise self.meta_exc
        return self.meta


def _bot(monkeypatch, tmp_path, client=None, members=()):
    from src import store
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "chips.db")
    store.init_db()
    with store.get_conn() as conn:
        for uid, handle in members:
            store.add_watch_user(conn, uid, handle, handle.upper())
    return CommandBot(client=client, notifier=None), store


def _line_with(out: str, needle: str) -> str:
    for ln in out.split("\n"):
        if needle in ln:
            return ln
    raise AssertionError(f"输出里没有含 {needle!r} 的行:\n{out}")


# ============================================================
# 核心诚实点:精确 vs 下界
# ============================================================
def test_精确时报确定的占比且不带任何下界记号(monkeypatch, tmp_path):
    """
    len(topHolders) == totalHolders —— 这份数据就是全部持有人,可以断言"持仓 X%"。
    此时**绝不能**出现 ≥、也绝不能出现"仅统计前 N 名"那句提示:
    它们是给下界用的,出现在精确结果上是另一种误导(把可信的数说成不可信)。
    """
    data = {"totalHolders": 2,
            "topHolders": [_holder("u-1", "alice", 20_000_000, 800.0),
                           _holder("x-9", "who", 5_000_000, 200.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    plat = _line_with(out, "FOMO 平台")
    assert plat == "🏦 FOMO 平台 · 持有人 2 · 持仓 2.500%", plat   # 25,000,000 / 1e9
    assert "≥" not in out, f"精确结果里不该出现下界记号:\n{out}"
    assert "仅统计前" not in out, f"精确结果里不该出现截断提示:\n{out}"
    _assert_chips_invariant(out)


def test_被截断时必须标成下界并写清覆盖了多少(monkeypatch, tmp_path):
    """
    这是本功能的**核心诚实点**:第三方工具在 CATE 上只统计 98/79891(覆盖 0.12%)
    却照样显示成"持仓占比"。我们必须让用户一眼看出这是下界,并给出覆盖了多少。
    """
    holders = [_holder(f"z-{i}", f"u{i}", 1_000_000, 30.0) for i in range(97)]
    holders.append(_holder("u-1", "alice", 3_000_000, 90.0))
    data = {"totalHolders": 79891, "topHolders": holders}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 79,891"
    # 100,000,000 / 1e9 = 10.0%
    assert _line_with(out, "持仓") == "   持仓 ≥ 10.0%   ⚠️ 仅统计前 98 名,真实值更高"
    # 名单侧同样是下界,而且措辞必须是"在前 98 名内"而不是"持有"
    assert _line_with(out, "你的名单") == "👥 你的名单 · 1 人在前 98 名内 · ≥0.30%"
    _assert_chips_invariant(out)


def test_只差一个也算截断而不是精确(monkeypatch, tmp_path):
    """
    判据是**相等**,不是"差不多"。少一条就意味着有人被服务端过滤掉了,
    我们算出来的就是下界 —— 差 1 和差 79793 在"能不能声称精确"上没有区别。

    ⚠️ 持仓量刻意取在展示下限之上(2,000,000 / 1e9 = 0.20%):低于下限时下界记号
       另有一套写法(见 test_下界低于展示下限时不出现方向相反的两个比较符),
       那条路径不该由这条用例顺带盯,否则它盯的到底是哪一件事就说不清了。
    """
    data = {"totalHolders": 3,
            "topHolders": [_holder("a", "a", 1_000_000.0, 1.0),
                           _holder("b", "b", 1_000_000.0, 1.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "≥" in out, f"少一条就该报下界:\n{out}"
    assert "仅统计前 2 名" in out, out
    assert "持仓 ≥" in out and "· 持仓 0" not in out


def test_服务端没给总数时不许声称精确(monkeypatch, tmp_path):
    """
    totalHolders 缺失 = 不知道总数 = 没有资格声称精确。
    宁可多打一个 ≥,也不能把下界谎报成实数。
    """
    data = {"topHolders": [_holder("a", "a", 1_000_000.0, 1.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "≥" in out, f"总数未知就必须报下界:\n{out}"
    assert "持有人 ≥1(服务端没给总数)" in out, out


# ============================================================
# 名单匹配:按 user.id,绝不按 handle
# ============================================================
def test_名单按user_id匹配而不是按handle(monkeypatch, tmp_path):
    """
    ⚠️ handle 会改名、大小写还不稳定。这个用例把两种错误同时摆出来:
       - alice 在 FOMO 上已经改名成 alice_v2,**id 没变** → 必须认出来
       - 一个陌生人恰好占用了 "bob" 这个 handle,**id 不在名单** → 绝不能算进来
       按 handle 匹配会同时答错这两条;按 id 匹配两条都对。
    """
    data = {"totalHolders": 2,
            "topHolders": [_holder("u-1", "alice_v2", 10_000_000, 500.0),
                           _holder("stranger-77", "bob", 90_000_000, 900.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client,
                members=[("u-1", "alice"), ("u-2", "bob")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert _line_with(out, "你的名单") == "👥 你的名单 · 1 人持有 · 1.000%"
    # 展示用的是**本地名单里**的 handle(用户认得的那个),不是 API 当前的名字
    assert "@alice · 10,000,000 枚 · $500.00" in out, out
    assert "@bob" not in out, f"handle 撞名的陌生人被算进了名单:\n{out}"
    assert "@alice_v2" not in out, out


def test_软删除的成员不再算进名单(monkeypatch, tmp_path):
    """/del 是软删除。退出名单的人还持有,那是他的事,不该再计入"你的名单" """
    data = {"totalHolders": 1, "topHolders": [_holder("u-2", "bob", 10_000_000, 500.0)]}
    client = _FakeClient({"1399811149": data})
    b, store_ = _bot(monkeypatch, tmp_path, client, members=[("u-2", "bob")])
    with store_.get_conn() as conn:
        store_.remove_watch_user(conn, "bob")

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "👥 你的名单 · 无人持有" in out, out
    assert "@bob" not in out


# ============================================================
# 分母缺失:整行消失,绝不打 0
# ============================================================
def test_分母拿不到时占比整段消失而不是显示0(monkeypatch, tmp_path):
    """
    ⚠️ 打一个 0% 就是凭空造出"他们几乎没有仓位"这个假事实。
       分母不知道时,持有人数与持仓数量照常给 —— 那两个是真的知道的。
    """
    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 12_345_678, 42.0)]}
    client = _FakeClient({"1399811149": data}, meta={})     # 接口没给 totalSupply
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "%" not in out, f"分母未知却打出了百分比:\n{out}"
    assert "0%" not in out and "0.00%" not in out
    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 1"
    assert _line_with(out, "你的名单") == "👥 你的名单 · 1 人持有"
    assert "@alice · 12,345,678 枚 · $42.00" in out, out
    # ⚠️ 注脚必须**指名 🏦**:它解释的是 FOMO 那半边的分母,而 💊 那半边用的是
    #    另一个来源(pump 的 coins-v3)。J1 之后 tail 排在 💊 整块之后,不指名就会
    #    被读成"pump 的占比算不出来"。
    assert "拿不到 🏦 那边的总供应量" in out, out
    _assert_chips_invariant(out)


def test_分母退到本地推算时必须标注这是推算值(monkeypatch, tmp_path):
    """
    本地 token_snapshot 的 市值÷价格 只是 FDV 反推,不是权威值 ——
    不标注的话用户会把它当成和接口一样可信。
    """
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 10_000_000, 42.0)]}
    client = _FakeClient({"1399811149": data}, meta={})
    b, store_ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])
    with store_.get_conn() as conn:
        store_.mark_stats_ready(conn, "u-1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u-1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="solana", token_address=CA_SOL, token_symbol="FOREST",
            amount_usd=1.0, badge_reason=REASON_LOCAL_STATS, user_handle="alice"))
        # 市值 1000 / 价格 0.000001 = 十亿枚
        store_.upsert_token_snapshots(conn, [("solana", CA_SOL, "FOREST", 0.000001, 1000.0)])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "持仓 1.000%" in out, out             # 10,000,000 / 1e9
    assert "推算" in out, f"推算值必须标注:\n{out}"
    _assert_chips_invariant(out)


def test_价格为0时不拿它当分母(monkeypatch, tmp_path):
    """除零之外,0 价格反推出来的"供应量"是无穷大,会把占比压成 0.0000% 这种假值"""
    from src.bot import _chips_local_supply
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    _, store_ = _bot(monkeypatch, tmp_path, None)
    with store_.get_conn() as conn:
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u-1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="solana", token_address=CA_SOL, token_symbol="F",
            amount_usd=1.0, badge_reason=REASON_LOCAL_STATS, user_handle="a"))
        store_.upsert_token_snapshots(conn, [("solana", CA_SOL, "F", 0.0, 1000.0)])

    assert _chips_local_supply(CA_SOL, "solana") is None


# ============================================================
# 名单成员多到放不下:必须说清还有几人
# ============================================================
def test_名单成员超上限时如实报出还有几人未显示(monkeypatch, tmp_path):
    """
    ⚠️ 这个数算错一个,用户对"名单里到底几个人在场"的判断就跟着错一个。
       20 人全部匹配、最多展示 10 行 → 剩下的正好是 10 人。
    """
    members = [(f"m-{i}", f"member{i}") for i in range(20)]
    holders = [_holder(f"m-{i}", f"member{i}", (20 - i) * 1_000_000.0, 5.0) for i in range(20)]
    client = _FakeClient({"1399811149": {"totalHolders": 20, "topHolders": holders}})
    b, _ = _bot(monkeypatch, tmp_path, client, members=members)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "…按持仓数量排序,还有 10 人未显示" in out, out
    assert out.count("@member") == 10, f"应当正好展示 10 位:\n{out}"
    # 排序:持仓最多的排最前
    assert "@member0 · 20,000,000 枚" in out and "@member10" not in out
    _assert_chips_invariant(out)


def test_人数正好放得下时不出现未显示提示(monkeypatch, tmp_path):
    """0 个未显示就不该有这行 —— 一句"还有 0 人未显示"是纯噪音,还会让人怀疑数据"""
    members = [(f"m-{i}", f"member{i}") for i in range(3)]
    holders = [_holder(f"m-{i}", f"member{i}", 1_000.0, 5.0) for i in range(3)]
    client = _FakeClient({"1399811149": {"totalHolders": 3, "topHolders": holders}})
    b, _ = _bot(monkeypatch, tmp_path, client, members=members)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "未显示" not in out, out
    assert out.count("@member") == 3


# ============================================================
# "没有"的各种形态
# ============================================================
def test_精确且名单无人时说无人持有(monkeypatch, tmp_path):
    data = {"totalHolders": 1, "topHolders": [_holder("x-1", "who", 1_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "👥 你的名单 · 无人持有" in out, out
    assert "0%" not in out, f"无人持有不等于持仓 0%,不该打这个数:\n{out}"


def test_被截断且名单无人时不许说无人持有(monkeypatch, tmp_path):
    """
    ⚠️ 名单成员没出现在前 N 名里 **不等于** 他没持有(可能只是仓位低于那道 $2 门槛)。
       说"无人持有"就是把"我们没看见"谎报成"不存在"。
    """
    holders = [_holder(f"z-{i}", f"u{i}", 1_000.0, 5.0) for i in range(50)]
    client = _FakeClient({"1399811149": {"totalHolders": 900, "topHolders": holders}})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "👥 你的名单 · 前 50 名内无人" in out, out
    assert "无人持有" not in out, f"截断时不能断言「没有」:\n{out}"


def test_一个持有人都没有时说清可能是链不对(monkeypatch, tmp_path):
    """空榜既可能是"还没人买"也可能是"猜错了链",措辞必须把这份不确定带出来"""
    client = _FakeClient({})            # 任何链都返回 {}
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "没查到持有人" in out, out
    assert "链" in out
    _assert_chips_invariant(out)


def test_陌生地址试遍所有链都没有时给出可操作提示(monkeypatch, tmp_path):
    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(CA_BSC)          # 0x 开头、本地不认识 → 依次试链

    assert "都没查到持有人" in out, out
    assert "/chips" in out, "必须告诉用户可以指定链"
    # 这条早退路径与 /ca 同一个排版:锚点在第一行(后面没有任何数据行可跟)
    assert "<code>" in out and "</code>" in out
    _assert_chips_invariant(out, anchored=False)


# ============================================================
# 缺字段:整段消失,不打 0 / N/A
# ============================================================
def test_数量与金额缺失时该段消失而不是显示0(monkeypatch, tmp_path):
    data = {"totalHolders": 1,
            "topHolders": [{"user": {"id": "u-1", "userHandle": "alice"}}]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    row = _line_with(out, "@alice")
    assert row == "   @alice", f"缺字段应当整段消失,实际: {row!r}"
    assert "枚" not in out and "$" not in out.split("\n")[3]


def test_持仓恰好为0仍如实显示0枚(monkeypatch, tmp_path):
    """⚠️ 0 是有意义的真实值(清光了),与"字段缺失"必须区分开"""
    data = {"totalHolders": 1,
            "topHolders": [_holder("u-1", "alice", 0.0, 0.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "@alice · 0 枚 · $0.00" in out, out
    assert "持仓 0%" in out, f"平台侧确实是 0,就该写 0:\n{out}"


# ============================================================
# 链解析:沿用 /ca 的三级做法
# ============================================================
def test_本地已认识的地址不再猜链(monkeypatch, tmp_path):
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 1_000.0, 5.0)]}
    client = _FakeClient({"56": data})
    b, store_ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])
    with store_.get_conn() as conn:
        store_.mark_stats_ready(conn, "u-1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u-1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=1.0, badge_reason=REASON_LOCAL_STATS, user_handle="alice"))

    out = b._cmd_chips(CA_BSC)          # 不给链

    assert client.calls == [(CA_BSC, "56")], "本地已认识这条链,不该再去猜别的链"
    assert "BNB Chain" in out


def test_用户指定的链优先于本地记录(monkeypatch, tmp_path):
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 1_000.0, 5.0)]}
    client = _FakeClient({"8453": data})
    b, store_ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])
    with store_.get_conn() as conn:
        store_.mark_stats_ready(conn, "u-1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u-1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="bsc", token_address=CA_BSC, token_symbol="ELOY",
            amount_usd=1.0, badge_reason=REASON_LOCAL_STATS, user_handle="alice"))

    out = b._cmd_chips(f"{CA_BSC} base")

    assert client.calls == [(CA_BSC, "8453")], "用户说了链就完全按他说的,不再猜"
    assert "Base" in out


def test_陌生的0x地址按顺序试链命中即停手(monkeypatch, tmp_path):
    data = {"totalHolders": 1, "topHolders": [_holder("x", "who", 1_000.0, 5.0)]}
    client = _FakeClient({"8453": data})        # 第 2 顺位 base 才有
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(CA_BSC)

    assert [n for _, n in client.calls] == ["56", "8453"], client.calls
    assert "Base" in out


# ============================================================
# 接口失败:分子分母互不牵连
# ============================================================
def test_分母挂了不影响分子(monkeypatch, tmp_path):
    """两个数据源互相独立 —— 供应量查询炸了,持有人数与数量照常给"""
    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 7_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data}, meta_exc=RuntimeError("boom"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "🏦 FOMO 平台 · 持有人 1" in out
    assert "@alice · 7,000 枚" in out
    assert "%" not in out
    _assert_chips_invariant(out)


def test_分子挂了给出可操作提示且不崩(monkeypatch, tmp_path):
    client = _FakeClient(exc=FomoAPIError("/hodlers/top HTTP 500: oops"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "FOMO 接口异常" in out, out
    assert "你的名单" in out, "名单侧也要说清为什么判断不了,而不是整块消失"
    _assert_chips_invariant(out)


def test_鉴权失败提示重新登录(monkeypatch, tmp_path):
    from src.auth import AuthError

    client = _FakeClient(exc=AuthError("token 过期"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "--login" in out, out
    # ⚠️ 绝不能把异常里的 token 原文带进回执
    assert "Bearer" not in out


def test_未知异常也不把线程带走(monkeypatch, tmp_path):
    client = _FakeClient(exc=ValueError("something weird"))
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "查询失败" in out, out


# ============================================================
# 入口:用法、菜单、分发
# ============================================================
def test_地址为空给出用法提示而不是崩溃(monkeypatch, tmp_path):
    b, _ = _bot(monkeypatch, tmp_path, None)

    out = b._cmd_chips("")

    assert "用法" in out and "/chips" in out
    _assert_chips_invariant(out, anchored=False)


def test_命令菜单与帮助里都有chips(monkeypatch, tmp_path):
    from src.bot import _COMMAND_MENU, _HELP

    assert "chips" in [name for name, _ in _COMMAND_MENU], "菜单里没有 = 用户敲 / 时看不到"
    assert "/chips" in _HELP


def test_分发能路由到chips(monkeypatch, tmp_path):
    data = {"totalHolders": 1, "topHolders": [_holder("x", "who", 1_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._dispatch("/chips", f"{CA_SOL} solana")

    assert "FOMO 平台" in out
    assert "未知命令" not in out


# ============================================================
# 出口不变式:任意脏输入都不能让整条消息 400 或超长
# ============================================================
_EVIL = ("&", "<", ">", '"', "'", "&amp;", "&lt;", "&am", "<code>", "</code>",
         "<b>", "</b>", "<a href='x'>", "<script>", "a>b", "\n", "\r\n", "\t",
         " ", " ", "A", "中", "…", "😀", "0", "$")


@pytest.mark.parametrize("seed", range(25))
def test_脏字段撑不破消息也撑不出非法HTML(monkeypatch, tmp_path, seed):
    """
    handle / ticker / 链名全都来自陌生人或服务端,长度与内容都不受任何天然约束。
    出口不变式的职责就是:**无论它们是什么**,消息都发得出去且是合法 HTML。
    """
    rng = random.Random(seed)

    def evil(n):
        return "".join(rng.choice(_EVIL) for _ in range(n))

    members = [(f"m-{i}", evil(rng.randint(1, 60))) for i in range(15)]
    holders = [_holder(f"m-{i}", "x", rng.random() * 1e12, rng.random() * 1e6)
               for i in range(15)]
    client = _FakeClient({"1399811149": {"totalHolders": 15, "topHolders": holders}},
                         meta=_meta(symbol=evil(rng.randint(1, 200)), supply="1000000000"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=members)

    out = b._cmd_chips(f"{CA_SOL} {evil(rng.randint(1, 40))}")

    _assert_chips_invariant(out)


def test_超长地址也不会撑破消息(monkeypatch, tmp_path):
    """normalize_token_address 对非 0x/42 位的输入原样透传,锚点自己就可能超预算"""
    client = _FakeClient({})
    b, _ = _bot(monkeypatch, tmp_path, client)

    out = b._cmd_chips("Z" * 6000)

    _assert_chips_invariant(out, anchored=False)
    assert len(out) <= _TG_HARD_LIMIT


# ============================================================
# HTML 转义:handle 与 ticker 是两条**各自独立**的路径
# ============================================================
# ⚠️ 这两条必须分开写、各自只放一个脏字段。上一轮的漏网正是「一个用例同时传了
#    handle 和 ticker、却只断言其中一个」,于是另一条路径去掉转义照样全绿。
def test_名单handle里的活标签必须被转义(monkeypatch, tmp_path):
    """
    ⚠️ handle 是陌生人自己起的名字,`<b>` 这类标签在装配出口的白名单里 ——
       没转义就会被原样放行成**活标签**,等于让名单成员往我们发给 Telegram 的消息里
       塞标记。而「标签配对闭合」这条出口不变式照样成立,所以只有这条断言守得住。
    ⚠️ 本用例的 ticker 是干净的,handle 是唯一的脏字段:哪条路径漏了转义一目了然。
    """
    data = {"totalHolders": 1,
            "topHolders": [_holder("u-1", "whatever", 1_000_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data})              # ticker 用默认的 FOREST
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "<b>evil</b>")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "@&lt;b&gt;evil&lt;/b&gt;" in out, f"handle 没转义:\n{out}"
    assert "<b>evil</b>" not in out, f"活标签被注入进消息里了:\n{out}"
    _assert_chips_invariant(out)


def test_ticker里的活标签必须被转义(monkeypatch, tmp_path):
    """
    ⚠️ ticker 来自 filterTokens 接口,同样不受任何约束。标题行本身就写在一对
       `<b>` 里,ticker 不转义就会变成 `<b>$<b>PWN</b></b>` —— 标签仍然配对闭合,
       出口不变式一个字都不会响,只有这条断言能逮到。
    ⚠️ 本用例的 handle 是干净的,ticker 是唯一的脏字段。
    """
    data = {"totalHolders": 1,
            "topHolders": [_holder("u-1", "alice", 1_000_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data}, meta=_meta(symbol="<b>PWN</b>"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "$&lt;b&gt;PWN&lt;/b&gt;" in out, f"ticker 没转义:\n{out}"
    assert "$<b>PWN</b>" not in out, f"活标签被注入进标题里了:\n{out}"
    _assert_chips_invariant(out)


# ============================================================
# 占比展示的下限:小仓位既不能被吞掉,也不能被伪造成 0
# ============================================================
def test_小仓位仍报出可读的数字(monkeypatch, tmp_path):
    """
    ⚠️ 展示下限调大一点点(比如到 0.01%),`0.0050%` 这种完全可读的真实值就会被
       吞成一句「低于下限」—— 用户读到的是「小到看不见」,而真相是「有,0.005%」。
       50,000 / 1e9 = 0.005%。
    """
    data = {"totalHolders": 1,
            "topHolders": [_holder("u-1", "alice", 50_000.0, 5.0)]}
    client = _FakeClient({"1399811149": data})              # 默认供应量 1e9
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 1 · 持仓 0.0050%", out
    assert "&lt;" not in out, f"0.005% 是报得出来的真实值,不该退成下限写法:\n{out}"
    _assert_chips_invariant(out)


def test_尘埃仓位报下限而不是伪造出来的0(monkeypatch, tmp_path):
    """
    ⚠️ 反方向:展示下限如果没了(或降到 0),8e-7% 会被四位小数印成 `0.0000%` ——
       一个看起来像真数据的假值,读出来就是「他一枚都没有」。
       8,000,000 / 1e15 = 0.0000008%。
    """
    data = {"totalHolders": 1,
            "topHolders": [_holder("u-1", "alice", 8_000_000.0, 2.4)]}
    client = _FakeClient({"1399811149": data}, meta=_meta(supply="1000000000000000"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "持仓 &lt;0.0001%" in out, f"尘埃仓位该报下限写法:\n{out}"
    assert "0.0000%" not in out, f"凭空造了一个『没有仓位』的假值:\n{out}"
    _assert_chips_invariant(out)


def test_下界低于展示下限时不出现方向相反的两个比较符(monkeypatch, tmp_path):
    """
    ⚠️ 截断态叠上尘埃仓位,曾经渲染成 `持仓 ≥ &lt;0.0001%` 与 `· ≥&lt;0.0001%` ——
       读出来是「不小于小于万分之一」,两个方向相反的比较符黏在一起,自相矛盾。
       改口说「不足 0.0001%」:方向只剩一个,陈述的是**已统计到的那部分**;
       「真实值更高」由旁边那句截断提示负责。
       98 × 8,000,000 / 1e15 = 0.0000784%,名单那一位单独是 0.0000008%,两边都在下限以下。
    """
    holders = [_holder(f"z-{i}", f"u{i}", 8_000_000.0, 2.4) for i in range(97)]
    holders.append(_holder("u-1", "alice", 8_000_000.0, 2.4))
    data = {"totalHolders": 79891, "topHolders": holders}
    client = _FakeClient({"1399811149": data}, meta=_meta(supply="1000000000000000"))
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    for ln in out.split("\n"):
        assert not ("≥" in ln and "&lt;" in ln), f"一行里出现了两个方向相反的比较符: {ln!r}"
    assert _line_with(out, "持仓") == "   持仓 不足 0.0001%   ⚠️ 仅统计前 98 名,真实值更高", out
    assert _line_with(out, "你的名单") == "👥 你的名单 · 1 人在前 98 名内 · 不足 0.0001%", out
    _assert_chips_invariant(out)


# ============================================================
# 持仓数量的写法
# ============================================================
def test_十亿以上的持仓换成B单位(monkeypatch, tmp_path):
    """
    ⚠️ memecoin 的供应量常在 1e9~1e15。`5,000,000,000 枚` 一个字段就吃掉十三个字符,
       一行塞不下 handle + 数量 + 金额三段。分母故意不给,把这条用例焦点收在数量上。
    """
    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 5e9, 5.0)]}
    client = _FakeClient({"1399811149": data}, meta={})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "@alice · 5.00B 枚 · $5.00" in out, out
    assert "5,000,000,000" not in out, f"没换单位,一行被它吃掉:\n{out}"
    _assert_chips_invariant(out)


def test_不足一枚的持仓保留小数而不是被抹成0(monkeypatch, tmp_path):
    """⚠️ 高价币真的可能只持有零点几枚。四舍五入成 `0 枚` 就是把「有」说成「没有」"""
    data = {"totalHolders": 1, "topHolders": [_holder("u-1", "alice", 0.5, 5.0)]}
    client = _FakeClient({"1399811149": data}, meta={})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "@alice · 0.5000 枚 · $5.00" in out, out
    assert "· 0 枚" not in out, f"半枚被抹成了 0:\n{out}"
    _assert_chips_invariant(out)


# ============================================================
# 求和:一个都没解析出来 ≠ 加起来是 0
# ============================================================
def test_求和在一个数都没有时返回None而不是0():
    """
    ⚠️ `sum([]) == 0` 会让「字段全缺」和「加起来真的是 0」变成同一个值,
       下游据此打出一个 0% 的占比 —— 那是凭空造出来的假事实。
       0 本身仍然是有意义的真实值,必须原样返回。
    """
    from src.bot import _chips_sum

    assert _chips_sum([]) is None, "空列表 = 什么都没解析出来,不是 0"
    assert _chips_sum([None, None]) is None, "全是缺失 = 什么都没解析出来,不是 0"
    assert _chips_sum([0.0]) == 0.0, "0 是真实值,照实返回"
    assert _chips_sum([None, 2.0, 3.0]) == 5.0, "缺的跳过,有的照加"


def test_持仓数量全缺时占比整段消失而不是打成0(monkeypatch, tmp_path):
    """求和退化成 0 之后,这里会渲染出一句「持仓 0%」—— 一个凭空造出来的事实"""
    data = {"totalHolders": 2,
            "topHolders": [{"user": {"id": "u-1"}, "value": 5.0},
                           {"user": {"id": "x-9"}, "value": 5.0}]}
    client = _FakeClient({"1399811149": data})              # 分母是有的,只缺分子
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 2", out
    assert "持仓 0%" not in out, f"字段全缺被当成了「加起来是 0」:\n{out}"
    assert "%" not in out, f"分子一个都没解析出来,不该有任何占比:\n{out}"
    _assert_chips_invariant(out)


# ============================================================
# 注脚:没有占比就不该解释占比
# ============================================================
def test_分子挂了就不该挂一句占比是近似值(monkeypatch, tmp_path):
    """
    ⚠️ 两条注脚都在解释「占比这个数怎么来的」。分子挂掉时消息里一个百分号都没有,
       却挂着「占比是近似值」,等于回答一个没人问的问题、还暗示上面有个近似的占比。
       复现条件:用户指定链 + 持有人榜抛异常 + 本地有行情(于是走本地推算)。
    """
    from src.models import EVENT_BUY, REASON_LOCAL_STATS, FomoEvent

    client = _FakeClient(exc=FomoAPIError("/hodlers/top HTTP 500: oops"), meta={})
    b, store_ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])
    with store_.get_conn() as conn:
        store_.mark_stats_ready(conn, "u-1")
        store_.insert_event(conn, FomoEvent(
            event_id="e1", event_type=EVENT_BUY, user_id="u-1",
            event_ts="2026-08-01T00:00:00+00:00", raw_json="{}",
            network_id="solana", token_address=CA_SOL, token_symbol="FOREST",
            amount_usd=1.0, badge_reason=REASON_LOCAL_STATS, user_handle="alice"))
        # 市值 1000 / 价格 0.000001 = 十亿枚 —— 本地推算这一路是能走通的
        store_.upsert_token_snapshots(conn, [("solana", CA_SOL, "FOREST", 0.000001, 1000.0)])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "FOMO 接口异常" in out, out
    assert "%" not in out, f"分子都没拿到,消息里不该有占比:\n{out}"
    assert "近似值" not in out, f"消息里没有占比,却解释了占比怎么来的:\n{out}"
    assert "推算" not in out, out
    _assert_chips_invariant(out)


# ============================================================
# 服务端自报数据不一致时,不许输出自相矛盾的话
# ============================================================
def test_总数比条数还少时以实际条数为准(monkeypatch, tmp_path):
    """
    ⚠️ 服务端说 totalHolders=0 却给了一条持有人明细。照抄这个 0 就会渲染成
       「持有人 0」,下面紧跟着列出一位持有人 —— 一条自己打自己脸的消息。
       手上数得出来的条数比服务端的自报更可信。
    ⚠️ 但也绝不能因此翻成「精确」:连总数都不可信,更没资格说「这就是全部」。
    """
    data = {"totalHolders": 0,
            "topHolders": [_holder("u-1", "alice", 200_000_000.0, 100.0)]}
    client = _FakeClient({"1399811149": data})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "持有人 0" not in out, f"说没有持有人,却列出了持有人:\n{out}"
    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 1", out
    assert "@alice" in out, out
    assert "仅统计前 1 名" in out, f"总数不可信时仍然只能报下界:\n{out}"
    _assert_chips_invariant(out)


def test_一条明细都没拿到时不说仅统计前0名(monkeypatch, tmp_path):
    """
    ⚠️ 服务端自报有 5 个持有人、却一条明细都没给(那道约 $2 的市值下限足以滤光所有人)。
       「仅统计前 0 名」和「前 0 名内无人」都是什么都没说的废话,读起来还像是数错了。
    """
    client = _FakeClient({"1399811149": {"totalHolders": 5, "topHolders": []}})
    b, _ = _bot(monkeypatch, tmp_path, client, members=[("u-1", "alice")])

    out = b._cmd_chips(f"{CA_SOL} solana")

    assert "仅统计前 0 名" not in out, f"自相矛盾的措辞:\n{out}"
    assert "前 0 名内" not in out, f"自相矛盾的措辞:\n{out}"
    assert _line_with(out, "FOMO 平台") == "🏦 FOMO 平台 · 持有人 5", out
    assert "没拿到任何持有人明细" in out, out
    _assert_chips_invariant(out)
