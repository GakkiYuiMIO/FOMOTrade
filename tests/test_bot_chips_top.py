"""
/chips 的 🔝 FOMO 前 10 名持有人(名字 · 占比 · 粉丝 · 投资组合 · 7天盈亏)。

⚠️ 位置是这一块的硬约束:在「🏦 FOMO 平台」之后、「👥 你的名单」之前 ——
   名单表头下面紧跟的是成员明细,插到那里就会把前 10 名读成"你名单里的人"。
⚠️ 全部离线、临时库;数字写死字面量。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

from src.holdercard import HolderCard
from tests.test_bot_chips import CA_SOL, _assert_chips_invariant, _bot, _FakeClient


def _h(uid, handle, amount, *, followers=None, private=None) -> dict:
    u = {"id": uid, "userHandle": handle, "displayName": handle, "address": "So1" + uid}
    if followers is not None:
        u["followers"] = followers
    if private is not None:
        u["private"] = private
    return {"user": u, "tradeId": f"t-{uid}", "humanAmount": amount, "value": amount / 1000,
            "price": 0.001, "costBasis": 1.0, "isDev": False}


class _CardClient(_FakeClient):
    """持有人榜 + 两个资产接口。cards: uid → (balances 响应, 快照) 或 "boom"(抛异常)"""

    def __init__(self, by_net, cards=None, **kw):
        super().__init__(by_net, **kw)
        self.cards = cards or {}
        self.card_calls: list[str] = []

    def get_balances_raw(self, uid, *, auth_invalidate=True, fast_fail=False):
        self.card_calls.append(uid)
        v = self.cards.get(uid)
        if v == "boom":
            raise RuntimeError("上游炸了")
        if v == "snapboom":
            return {"balances": [], "otherEquity": 100.0}
        return (v or ({"balances": []}, None))[0]

    def get_pnl_snapshot(self, uid, snapshot_id, *, auth_invalidate=True, fast_fail=False):
        v = self.cards.get(uid)
        if v == "snapboom":
            raise RuntimeError("快照炸了")
        return None if v in (None, "boom") else v[1]


def _card(portfolio, past_pnl, live_pnl=0.0):
    """造一份资产响应:投资组合 = otherEquity,此刻累计 = otherPnlV2(持仓数组留空,只为好算)"""
    return ({"balances": [], "otherEquity": portfolio, "otherPnlV2": live_pnl}, {"pnl": past_pnl})


def _out(monkeypatch, tmp_path, holders, *, total=None, cards=None, client_cls=_CardClient,
         members=(), meta=None):
    data = {"totalHolders": total if total is not None else len(holders), "topHolders": holders}
    kw = {} if meta is None else {"meta": meta}
    client = (client_cls({"1399811149": data}, cards, **kw) if client_cls is _CardClient
              else client_cls({"1399811149": data}, **kw))
    b, _ = _bot(monkeypatch, tmp_path, client, members=members)
    return b._cmd_chips(f"{CA_SOL} solana"), client


def _idx(lines, needle):
    return next(i for i, ln in enumerate(lines) if needle in ln)


class Test内容:
    def test_一行里有名字占比粉丝投资组合7天盈亏(self, monkeypatch, tmp_path):
        holders = [_h("u1", "badabeepp", 21_505_000, followers=15904)]
        out, _ = _out(monkeypatch, tmp_path, holders,
                      cards={"u1": _card(180722.6, past_pnl=-1140.16, live_pnl=50000.0)})
        assert "🔝 FOMO 前 1 名持有人(按持仓数量)" in out, out
        # 21,505,000 / 1e9 = 2.1505%;7 天 = 50000 − (−1140.16) = 51140.16
        assert ("   #1 「badabeepp」 · 2.151% · 粉丝 15,904 · 投资组合 $180.72K · 7天盈亏 +$51.14K"
                in out.split("\n")), out
        _assert_chips_invariant(out)

    def test_按持仓数量排序_只取前10(self, monkeypatch, tmp_path):
        holders = [_h(f"u{i}", f"user{i}", (i + 1) * 1000) for i in range(12)]
        out, client = _out(monkeypatch, tmp_path, holders)
        lines = out.split("\n")
        assert "🔝 FOMO 前 10 名持有人(按持仓数量)" in lines
        rows = [ln for ln in lines if ln.startswith("   #")]
        assert [r.split("「")[1].split("」")[0] for r in rows] == \
            [f"user{i}" for i in range(11, 1, -1)], rows
        assert sorted(client.card_calls) == sorted(f"u{i}" for i in range(2, 12)), \
            "只该给前 10 名拉资产"

    def test_分母拿不到时退回数量(self, monkeypatch, tmp_path):
        from tests.test_bot_chips import _meta
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "alice", 5_000_000)],
                      meta=_meta(supply=None))
        row = next(ln for ln in out.split("\n") if ln.startswith("   #1"))
        assert "5,000,000 枚" in row and "%" not in row, row

    def test_粉丝0是真实值照实写(self, monkeypatch, tmp_path):
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "newbie", 1000, followers=0)])
        assert "粉丝 0" in out

    def test_用户名过不了门禁退回未知用户_其余照常(self, monkeypatch, tmp_path):
        holders = [_h("u1", "t.me/scamgroup", 1000, followers=5)]
        out, _ = _out(monkeypatch, tmp_path, holders)
        row = next(ln for ln in out.split("\n") if ln.startswith("   #1"))
        assert "t.me" not in out and row.startswith("   #1 未知用户"), row
        assert "粉丝 5" in row


class Test位置:
    def test_在FOMO平台之后_名单表头之前_成员明细仍紧跟名单表头(self, monkeypatch, tmp_path):
        holders = [_h("u1", "alice", 3000), _h("u2", "bob", 1000)]
        out, _ = _out(monkeypatch, tmp_path, holders, members=[("u1", "alice")])
        lines = out.split("\n")
        plat, top, watch = _idx(lines, "🏦 FOMO 平台"), _idx(lines, "🔝"), _idx(lines, "👥 你的名单")
        assert plat < top < watch, out
        assert lines[watch + 1].startswith("   @alice"), \
            f"名单表头下面必须紧跟成员明细,不许被前 10 名隔开:\n{out}"
        _assert_chips_invariant(out)


class Test降级:
    def test_有人没取到时说出来(self, monkeypatch, tmp_path):
        holders = [_h("u1", "alice", 3000), _h("u2", "bob", 1000)]
        out, _ = _out(monkeypatch, tmp_path, holders,
                      cards={"u1": _card(100.0, 0.0), "u2": "boom"})
        assert "   ⚠️ 有 1 人的投资组合/7天盈亏没取到(接口超时或失败)" in out.split("\n"), out
        bob = next(ln for ln in out.split("\n") if "「bob」" in ln)
        assert "投资组合" not in bob

    def test_private的人不拉资产_也不算没取到(self, monkeypatch, tmp_path):
        holders = [_h("u1", "alice", 3000, private=True), _h("u2", "bob", 1000)]
        out, client = _out(monkeypatch, tmp_path, holders,
                           cards={"u1": _card(999.0, 0.0), "u2": _card(100.0, 0.0)})
        assert client.card_calls == ["u2"], client.card_calls
        alice = next(ln for ln in out.split("\n") if "「alice」" in ln)
        assert "投资组合" not in alice and "7天盈亏" not in alice
        assert "没取到" not in out

    def test_客户端不支持资产接口时整块照出_只是没那两段_也不报没取到(self, monkeypatch, tmp_path):
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "alice", 3000, followers=7)],
                      client_cls=_FakeClient)
        assert "   #1 「alice」 · 0.0003% · 粉丝 7" in out.split("\n"), out
        assert "没取到" not in out

    def test_持有人榜挂了_不出这一块(self, monkeypatch, tmp_path):
        from src.client import FomoAPIError
        client = _CardClient({}, exc=FomoAPIError("boom"))
        b, _ = _bot(monkeypatch, tmp_path, client)
        out = b._cmd_chips(f"{CA_SOL} solana")
        assert "🔝" not in out

    def test_没有持有人明细时不出这一块(self, monkeypatch, tmp_path):
        out, _ = _out(monkeypatch, tmp_path, [], total=5)
        assert "🔝" not in out

    def test_渲染这一块炸了_只少这几行_整条照发(self, monkeypatch, tmp_path):
        from src import formatter

        def boom(**kw):
            raise RuntimeError("渲染炸了")
        monkeypatch.setattr(formatter, "render_chips_top_row", boom)
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "alice", 3000)])
        assert "🔝" not in out and "🏦 FOMO 平台" in out
        _assert_chips_invariant(out)


def test_HolderCard是冻结的():
    """它会进缓存被多个线程共享,必须不可变"""
    import dataclasses

    import pytest
    c = HolderCard(1.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.portfolio_usd = 3.0  # type: ignore[misc]


class Test没取到的计数:
    def test_快照请求失败也算没取到(self, monkeypatch, tmp_path):
        """⚠️ 否则读者分不出「这人没有 7 天记录」还是「我们没查到」"""
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "alice", 3000)], cards={"u1": "snapboom"})
        assert "   ⚠️ 有 1 人的投资组合/7天盈亏没取到(接口超时或失败)" in out.split("\n"), out
        row = next(ln for ln in out.split("\n") if "「alice」" in ln)
        assert "投资组合 $100.00" in row and "7天盈亏" not in row

    def test_快照是空的不算没取到(self, monkeypatch, tmp_path):
        out, _ = _out(monkeypatch, tmp_path, [_h("u1", "alice", 3000)],
                      cards={"u1": ({"balances": [], "otherEquity": 100.0}, {})})
        assert "没取到" not in out

    def test_超时的人也算没取到(self, monkeypatch, tmp_path):
        import time

        from src.bot import CommandBot
        from src.holdercard import HolderCardLookup

        class Slow(_CardClient):
            def get_balances_raw(self, uid, **kw):
                if uid == "slow":
                    time.sleep(0.6)
                return super().get_balances_raw(uid, **kw)
        orig = CommandBot.__init__

        def init(self, *a, **kw):
            orig(self, *a, **kw)
            self._cards = HolderCardLookup(self._client, budget_sec=0.2)
        monkeypatch.setattr(CommandBot, "__init__", init)
        client = Slow({"1399811149": {"totalHolders": 2, "topHolders": [_h("fast", "alice", 3000),
                                                                           _h("slow", "bob", 1000)]}},
                      {"fast": _card(1.0, 0.0), "slow": _card(2.0, 0.0)})
        from tests.test_bot_chips import _bot as mk
        b, _ = mk(monkeypatch, tmp_path, client)
        out2 = b._cmd_chips(f"{CA_SOL} solana")
        assert "有 1 人的投资组合/7天盈亏没取到" in out2, out2


# ============================================================
# 那一行本身(formatter.render_chips_top_row)
# ============================================================
class Test那一行:
    @staticmethod
    def _row(**kw):
        from src.formatter import render_chips_top_row
        base = {"rank": 1, "fomo_handle": "alice"}
        base.update(kw)
        return render_chips_top_row(**base)

    def test_7天亏损带负号(self):
        assert self._row(pnl_7d_usd=-4550.0) == "#1 「alice」 · 7天盈亏 -$4.55K"

    def test_只有投资组合没有7天(self):
        assert self._row(portfolio_usd=839.84) == "#1 「alice」 · 投资组合 $839.84"

    def test_占比优先_没有占比才写数量(self):
        assert self._row(share_pct=2.1505, amount_held=21_505_000) == "#1 「alice」 · 2.151%"
        assert self._row(amount_held=21_505_000) == "#1 「alice」 · 21,505,000 枚"

    def test_极小占比是转义过的小于号(self):
        assert self._row(share_pct=0.00001) == "#1 「alice」 · &lt;0.0001%"

    import pytest as _pytest

    @_pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), "abc", True, None])
    def test_粉丝数不合法就整段不出现(self, bad):
        assert self._row(followers=bad) == "#1 「alice」"

    @_pytest.mark.parametrize("rank", [None, 0, -1, True, "1"])
    def test_名次不合法就不写名次(self, rank):
        assert self._row(rank=rank) == "「alice」"
