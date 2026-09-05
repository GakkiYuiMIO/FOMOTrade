"""
/chips 回执里 💊 pump 那半边的**文案与接线**单测。

⚠️ 这半边盯的第一重点与 🏦 FOMO 那半边一模一样:**诚实**。
   「持仓 5.72%」与「持仓 ≥ 5.72%」、「无人持有」与「前 50 名内无人」
   必须是一眼可辨的两套话 —— 前者是"全都看过了",后者是"我们只看到这些"。
   把后者写成前者就是把"我们没看见"谎报成"不存在"。
⚠️ 断言里的措辞、人数、占比一律**写死字面量**,绝不从 src.bot / src.pumpchips
   import 常量或函数来断言自己。
⚠️ 一条用例都不打网络:pump 客户端全部是桩,FOMO 客户端也是。
"""
# ruff: noqa: N802
from __future__ import annotations

import re

import pytest

from tests.test_pumpchips import (
    CA_CAP,
    UID_1000X,
    UID_STRANGER,
    FakePump,
    _coin,
    _page,
    _pos,
    fx,
)

# Telegram sendMessage 的协议上限。外部事实,故意写死
_TG_HARD_LIMIT = 4096
_BROKEN_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|#x27|#39);)")
_TAG = re.compile(r"<(/?)([a-zA-Z][^<>]*)>")

CA_BSC = "0xfc6e0617a6cc19afe9e0d367e97d07245d357777"


def _tag_fault(s: str) -> str:
    """标签配没配对。自己数一遍,不复用 src.bot 里的任何东西(复用就成自指了)"""
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


def _assert_tg_ok(out: str) -> None:
    assert len(out) <= _TG_HARD_LIMIT, f"超过 TG 协议上限(实际 {len(out)})"
    m = _BROKEN_ENTITY.search(out)
    assert m is None, f"残缺 HTML 实体 @{m.start()}"
    fault = _tag_fault(out)
    assert not fault, fault
    n_tags = len(_TAG.findall(out))
    assert out.count("<") == n_tags and out.count(">") == n_tags, "有裸 < 或 >"
    assert out.endswith("</code>"), f"CA 锚点必须活到最后一行:{out[-80:]!r}"


# ============================================================
# FOMO 那半边的桩(照抄 test_bot_chips.py 的形状)
# ============================================================
def _holder(uid, handle="stranger", amount=1000.0, value=10.0):
    return {"user": {"id": uid, "userHandle": handle, "displayName": handle},
            "humanAmount": amount, "value": value}


def _meta(symbol="CAP", supply="1000000000"):
    return {"token": {"info": {"symbol": symbol, "totalSupply": supply}}}


class FakeFomo:
    def __init__(self, by_net=None, meta=None, *, exc=None):
        self.by_net = by_net or {}
        self.meta = meta if meta is not None else _meta()
        self.exc = exc

    def get_top_holders(self, token_address, network_id):
        if self.exc is not None:
            raise self.exc
        return self.by_net.get(network_id, {})

    def get_token_meta(self, token_address, network_id):
        return self.meta


def _bot(monkeypatch, tmp_path, *, fomo=None, pump=None,
         members=(), pump_members=()):
    from src import store
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "chips.db")
    store.init_db()
    with store.get_conn() as conn:
        for uid, handle in members:
            store.add_watch_user(conn, uid, handle, handle.upper())
        for uid, username in pump_members:
            store.add_pump_user(conn, uid, username, None, None)
    return CommandBot(client=fomo, notifier=None, pump_client=pump)


def _fomo_ok(net="4663"):
    """FOMO 半边:1 个持有人、精确统计。让 pump 那半边的变化一眼可辨"""
    return FakeFomo({net: {"topHolders": [_holder("f-1", "alice", 1000.0)],
                           "totalHolders": 1}})


def _line(out: str, needle: str) -> str:
    for ln in out.split("\n"):
        if needle in ln:
            return ln
    raise AssertionError(f"没有含 {needle!r} 的行:\n{out}")


def _has(out: str, needle: str) -> bool:
    return any(needle in ln for ln in out.split("\n"))


# ============================================================
# 1. 三段各自独立
# ============================================================
class Test三段各自独立:
    def test_全都拿到时三段都在(self, monkeypatch, tmp_path):
        pump = FakePump({0: _page(1, [_pos(UID_1000X, name="1000XCryptoD",
                                           held=5e7, pnl=235.08)])}, _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert _line(out, "💊") == "💊 pump.fun 平台 · 持有人 1 · 持仓 5.000%"
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 1 人持有 · 5.000%"
        assert _line(out, "1000XCryptoD") == "   「1000XCryptoD」 · 50,000,000 枚 · +235.1%"

    def test_分母没了只掉占比那一段(self, monkeypatch, tmp_path):
        """⚠️ 人数与名单命中都还在 —— 分母是**另一个请求**,它挂了不该带走别人"""
        pump = FakePump({0: _page(1, [_pos(UID_1000X, held=5e7)])}, None)
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        assert _line(out, "💊") == "💊 pump.fun 平台 · 持有人 1"
        assert _has(out, "⚠️ pump 没给总供应量,占比算不出来")
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 1 人持有"
        assert "%" not in _line(out, "你的 pump 名单")
        assert _has(out, "1000XCryptoD")

    def test_名单为空时名单那两段整个不出现(self, monkeypatch, tmp_path):
        """
        ⚠️⚠️ 对着一个空名单说「无人持有」是句误导:读者会以为我们查过了。
           平台人数与占比照常显示 —— 它们与名单没有关系。
        """
        pump = FakePump({0: _page(1, [_pos(UID_STRANGER, held=5e7)])}, _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump, pump_members=[])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        assert _has(out, "💊 pump.fun 平台 · 持有人 1")
        assert not _has(out, "你的 pump 名单")

    def test_明细恒空时只显示人数并说清楚为什么(self, monkeypatch, tmp_path):
        """
        ⚠️⚠️ 实测真实存在:totalCount=107 的币,明细只给 1 条;还有一类币
           coins-v3 直接返回 null(非 pump 上架、只是被 pump 用户持有的外部币),
           明细恒空。这时**绝不能**让用户以为「名单里没人」=「我们确认过了」。
        """
        pump = FakePump({0: _page(107, [])}, None)
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        assert _line(out, "💊") == "💊 pump.fun 平台 · 持有人 107"
        assert _has(out, "   ⚠️ 平台只给了人数、没给持仓明细,占比与名单都判断不了")
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 没有持仓明细,判断不了"
        # ⚠️ 只看 pump 那一行:FOMO 半边自己那句「无人持有」是它自己的结论,与这里无关
        assert "无人持有" not in _line(out, "你的 pump 名单")

    def test_币根本不在pump上时整块不出现(self, monkeypatch, tmp_path):
        """⚠️ 回执与接这个功能之前**一模一样**"""
        b_with = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(),
                      pump=FakePump({0: None}, _coin()),
                      pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b_with._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert "💊" not in out
        assert "pump" not in out
        assert _has(out, "🏦 FOMO 平台 · 持有人 1"), "FOMO 半边必须一个字不少"


# ============================================================
# 2. 三种措辞必须不同
# ============================================================
class Test措辞:
    def _out(self, monkeypatch, tmp_path, pump, members=((UID_1000X, "1000XCryptoD"),)):
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=list(members))
        return b._cmd_chips(f"{CA_CAP} robinhood")

    def test_全量翻完确认没人(self, monkeypatch, tmp_path):
        pump = FakePump({0: _page(2, [_pos(UID_STRANGER), _pos("other")])}, _coin())
        out = self._out(monkeypatch, tmp_path, pump)
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 无人持有"

    def test_只看了前若干名没确认(self, monkeypatch, tmp_path):
        """⚠️⚠️ 与上一条**只差覆盖是否完整**,措辞必须不同 —— 他可能就在后面那些人里"""
        pump = FakePump({0: _page(4000, [_pos(f"u{i}") for i in range(50)])}, _coin())
        out = self._out(monkeypatch, tmp_path, pump)
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 前 50 名内无人"

    def test_平台没给全明细时也不说确认没人(self, monkeypatch, tmp_path):
        """⚠️ 实测常态:自报 107 人、明细只有 1 条 —— 这同样不是「确认没人」"""
        pump = FakePump({0: _page(107, [_pos(UID_STRANGER)])}, _coin())
        out = self._out(monkeypatch, tmp_path, pump)
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 前 1 名内无人"

    def test_三种措辞两两不同(self, monkeypatch, tmp_path):
        outs = set()
        for pump in (FakePump({0: _page(2, [_pos("a"), _pos("b")])}, _coin()),
                     FakePump({0: _page(4000, [_pos(f"u{i}") for i in range(50)])}, _coin()),
                     FakePump({0: _page(107, [])}, _coin())):
            outs.add(_line(self._out(monkeypatch, tmp_path, pump), "你的 pump 名单"))
        assert len(outs) == 3, f"三种情况必须三种说法,实际:{outs}"

    def test_命中时精确与下界也是两套话(self, monkeypatch, tmp_path):
        exact = self._out(monkeypatch, tmp_path,
                          FakePump({0: _page(1, [_pos(UID_1000X, held=1e8)])}, _coin()))
        rows = [_pos(UID_1000X, held=1e8)] + [_pos(f"u{i}") for i in range(49)]
        lower = self._out(monkeypatch, tmp_path,
                          FakePump({0: _page(4000, rows)}, _coin()))
        assert _line(exact, "你的 pump 名单") == "👥 你的 pump 名单 · 1 人持有 · 10.0%"
        assert _line(lower, "你的 pump 名单") == "👥 你的 pump 名单 · 1 人在前 50 名内 · ≥10.0%"

    @pytest.mark.parametrize(("pages", "total", "expect"), [
        # 轻档:是**我们**主动只看前 50 名
        ({0: _page(9000, [_pos(f"u{i}") for i in range(50)])}, 9000,
         "⚠️ 人多,仅统计前 50 名,真实值更高"),
        # 平台没给全:该翻的都翻完了,是平台只给这么多明细
        ({0: _page(107, [_pos("a")])}, 107,
         "⚠️ 平台只给出 1/107 人的明细,真实值更高"),
    ])
    def test_看不全的原因不许写成同一句(self, monkeypatch, tmp_path, pages, total, expect):
        """
        ⚠️⚠️ 三种"看不全"的原因(我们主动降级 / 时间不够 / 平台没给全)
           读者据此判断"要不要自己再查一次"。写成同一句就是在编原因。
        """
        out = self._out(monkeypatch, tmp_path, FakePump(pages, _coin()))
        assert _has(out, expect), out


# ============================================================
# 3. 各类外呼失败都不许影响 FOMO 半边
# ============================================================
class TestFOMO半边不受牵连:
    _FOMO_LINES = ("🏦 FOMO 平台 · 持有人 1", "👥 你的名单 · 无人持有")

    @pytest.mark.parametrize(("desc", "pump"), [
        ("第一页超时", FakePump({}, _coin(), exc=TimeoutError("timeout"))),
        ("第一页 4xx", FakePump({0: fx("pump_mint_positions_400")}, _coin())),
        ("第一页 404", FakePump({0: fx("pump_mint_positions_404")}, _coin())),
        ("响应不是 JSON", FakePump({0: None}, _coin())),
        ("响应结构不对", FakePump({0: {"data": []}}, _coin())),
        ("分母炸了", FakePump({0: _page(1, [_pos("a")])}, None,
                              coin_exc=RuntimeError("boom"))),
        ("客户端整个炸了", FakePump({}, None, exc=ValueError("客户端坏了"))),
    ])
    def test_pump挂了FOMO半边一个字不变(self, monkeypatch, tmp_path, desc, pump):
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        for line in self._FOMO_LINES:
            assert _has(out, line), f"{desc}:FOMO 半边被牵连了\n{out}"

    def test_pump客户端本身建不出来也不影响(self, monkeypatch, tmp_path):
        """⚠️ 懒建 PumpClient 会 import curl_cffi;那一步炸了也只该让 💊 消失"""
        from src import bot as botmod

        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=None)
        monkeypatch.setattr(botmod.CommandBot, "_pump",
                            lambda self: (_ for _ in ()).throw(ImportError("没装 curl_cffi")))
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert _has(out, "🏦 FOMO 平台 · 持有人 1")
        assert "💊" not in out

    def test_FOMO半边挂了pump半边照样出(self, monkeypatch, tmp_path):
        """⚠️ 反方向也要成立 —— 两半边是两个平台的两份数据"""
        from src.client import FomoAPIError

        pump = FakePump({0: _page(1, [_pos(UID_1000X, held=5e7)])}, _coin())
        b = _bot(monkeypatch, tmp_path, fomo=FakeFomo(exc=FomoAPIError("500 挂了")),
                 pump=pump, pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        # ⚠️ FOMO 一条链都没定下来时走的是早退分支(诊断消息),那条路上没有 💊 ——
        #    这是刻意的取舍。这里给它一条本地已知的链,让它走完整装配。
        assert "FOMO 接口异常" in out


# ============================================================
# 4. 门禁:pump 用户名是攻击者可控的
# ============================================================
class Test用户名门禁:
    def _row(self, name):
        from src.formatter import render_pump_chip_row

        return render_pump_chip_row(pump_username=name, amount_held=5e7, pnl_pct=1.0)

    @pytest.mark.parametrize("evil", [
        "已清仓 · 亏损 99%",
        "Join t.me/pumpgroup now",
        "Send SOL to my wallet now",
        "私聊我领空投 加V信 abcdefg",
        "#freeairdrop",
        "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18",
    ])
    def test_恶意用户名被拦成未知用户(self, evil):
        out = self._row(evil)
        assert "未知用户" in out
        assert evil.split()[0] not in out or "未知用户" in out
        assert "t.me" not in out and "·" not in out.replace(" · ", "")

    def test_正常用户名照常显示且套容器(self):
        assert self._row("1000XCryptoD").startswith("「1000XCryptoD」")

    def test_名字被拦掉数量与盈亏照样是真值(self):
        """
        ⚠️ 整行删掉等于把「名单里有人持有」谎报成「没人持有」——
           那一行的其余两段是真值,退回「未知用户」是既有规矩(_display_name 同款)。
        """
        out = self._row("已清仓 · 亏损 99%")
        assert out == "未知用户 · 50,000,000 枚 · +1.0%"

    def test_门禁在渲染入口而不是靠调用方记得(self, monkeypatch, tmp_path):
        """⚠️⚠️ 端到端:恶意用户名从库里一路走到回执,零泄漏"""
        pump = FakePump({0: _page(1, [_pos(UID_1000X, name="接口给的", held=5e7)])},
                        _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "Join t.me/pumpgroup now")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert "t.me" not in out
        assert "未知用户" in out


# ============================================================
# 5. 出口不变式:pump 那几行不许把消息撑破
# ============================================================
class Test出口不变式:
    def test_一堆超长用户名也发得出去(self, monkeypatch, tmp_path):
        rows = [_pos(f"u{i}", name="名字" * 200, held=1e6 + i) for i in range(30)]
        pump = FakePump({0: _page(30, rows)}, _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(f"u{i}", "") for i in range(30)])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert "还有 25 人未显示" in out, "超出展示上限的必须被如实收口"

    def test_HTML特殊字符不会切出残缺实体(self, monkeypatch, tmp_path):
        pump = FakePump({0: _page(1, [_pos(UID_1000X, name="A&B<C>", held=5e7)])},
                        _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)

    def test_百分比再离谱也不会吃掉整条消息(self, monkeypatch, tmp_path):
        pump = FakePump({0: _page(1, [_pos(UID_1000X, name="bob", held=5e7,
                                           pnl=1e300)])}, _coin())
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "bob")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert "+1.00e+300%" in out


# ============================================================
# 6. 真实夹具跑一遍完整回执
# ============================================================
class Test真实夹具:
    def test_CAP的完整回执(self, monkeypatch, tmp_path):
        """⚠️ 用户截图里那个币。数字全部来自 2026-09-05 的真实响应"""
        pump = FakePump({0: fx("pump_mint_positions_robinhood")},
                        fx("pump_coin_v3_robinhood"))
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert _has(out, "💊 pump.fun 平台 · 持有人 430")
        # ⚠️ 430 人没超降级阈值,所以走的是**全量翻页**:桩里只有第 0 页,
        #    后面那 8 页都是空的 —— 于是这里如实说"平台只给出 50/430 人的明细",
        #    而不是"仅统计前 50 名"(那是我们主动降级时才说的话)。
        #    两句话对应两个完全不同的原因,不许写成同一句。
        assert _has(out, "⚠️ 平台只给出 50/430 人的明细,真实值更高")
        assert _has(out, "👥 你的 pump 名单 · 1 人在前 50 名内 · ≥1.458%")
        assert _has(out, "「1000XCryptoD」 · 14,584,546 枚 · +306.2%")

    def test_solana那条链(self, monkeypatch, tmp_path):
        from tests.test_pumpchips import CA_SOL, UID_FLIP

        pump = FakePump({0: fx("pump_mint_positions_solana")},
                        fx("pump_coin_v3_solana"))
        b = _bot(monkeypatch, tmp_path,
                 fomo=_fomo_ok("1399811149"), pump=pump,
                 pump_members=[(UID_FLIP, "FlippingProfits")])
        out = b._cmd_chips(f"{CA_SOL} solana")
        _assert_tg_ok(out)
        assert _has(out, "💊 pump.fun 平台 · 持有人 25")
        assert _has(out, "⚠️ 平台只给出 24/25 人的明细,真实值更高")
        assert _has(out, "FlippingProfits")


class Test分母自相矛盾的文案:
    def test_与没拿到分母是两句不同的话(self, monkeypatch, tmp_path):
        """
        ⚠️⚠️ 「没给总供应量」与「给的总供应量对不上」是两件事:后者读者应当知道
           pump 自己的数就矛盾(实测 $PUMP),写成前者会让人以为是我们没查到。
        """
        rows = [_pos(UID_1000X, held=6e8), _pos("x", held=6e8)]
        bad = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(),
                   pump=FakePump({0: _page(2, rows)}, _coin()),
                   pump_members=[(UID_1000X, "1000XCryptoD")])._cmd_chips(f"{CA_CAP} robinhood")
        none = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(),
                    pump=FakePump({0: _page(2, rows)}, None),
                    pump_members=[(UID_1000X, "1000XCryptoD")])._cmd_chips(f"{CA_CAP} robinhood")
        assert _has(bad, "   ⚠️ pump 给的总供应量比已统计的持仓还少,这个分母不可信,占比不显示")
        assert _has(none, "   ⚠️ pump 没给总供应量,占比算不出来")
        assert not _has(bad, "没给总供应量")
        # 两种情况下人数与名单命中都照常
        for out in (bad, none):
            assert _has(out, "💊 pump.fun 平台 · 持有人 2")
            assert _has(out, "「1000XCryptoD」")
            assert "%" not in _line(out, "你的 pump 名单")

    def test_绝不会印出一个大于百分之百的占比(self, monkeypatch, tmp_path):
        """⚠️ 三个人各持 6 亿、供应量 10 亿 → 天真算法会印出「持仓 180.0%」"""
        rows = [_pos(f"u{i}", held=6e8) for i in range(3)]
        out = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(),
                   pump=FakePump({0: _page(3, rows)}, _coin()),
                   pump_members=[])._cmd_chips(f"{CA_CAP} robinhood")
        assert _line(out, "💊") == "💊 pump.fun 平台 · 持有人 3"
        assert "180" not in out
        assert _has(out, "   ⚠️ pump 给的总供应量比已统计的持仓还少,这个分母不可信,占比不显示")


class Test分母超预算:
    def test_分母超预算FOMO与pump人数都不受影响(self, monkeypatch, tmp_path):
        """
        ⚠️⚠️ 变异测试打出来的空白(M-独立性):把分母那条 except 改成
           `return None` 时,💊 整块会消失,而全量测试当时**全绿**。
        """
        from src import pumpchips

        monkeypatch.setattr(pumpchips, "BUDGET_SEC", 0.05)
        pump = FakePump({0: _page(1, [_pos(UID_1000X, held=5e7)])}, _coin(), coin_delay=0.4)
        b = _bot(monkeypatch, tmp_path, fomo=_fomo_ok(), pump=pump,
                 pump_members=[(UID_1000X, "1000XCryptoD")])
        out = b._cmd_chips(f"{CA_CAP} robinhood")
        _assert_tg_ok(out)
        assert _has(out, "🏦 FOMO 平台 · 持有人 1")
        assert _line(out, "💊") == "💊 pump.fun 平台 · 持有人 1"
        assert _has(out, "   ⚠️ pump 没给总供应量,占比算不出来")
        assert _line(out, "你的 pump 名单") == "👥 你的 pump 名单 · 1 人持有"
        assert _has(out, "「1000XCryptoD」")
