"""
💊 pump 筹码那半边的**红队**:把每一条已知必拦的攻击串打进每一个槽位,断言零泄漏。

============ 为什么单开一个文件 ============
/mint-positions 的一行里有 **五个**攻击者可控的自由文本字段:
  · `userName`      —— 谁都能把自己的 pump 用户名改成 `已清仓 · 亏损 99%`;
  · `callout.thesis`—— 完全自由的正文,想写什么写什么;
  · `walletAddress` —— 上游给的任意串(不是我们校验过的地址);
  · `xUsername` / `accountKind` —— 同理。
本功能**只渲染 userName**(其余四个一个都不显示,理由见 pumpchips 模块头与
formatter.render_pump_chip_row)。但"我们没打算显示它"不是一条能靠人维持的不变量 ——
下一个人加一行 `if thesis: ...` 就破了。所以这里把攻击串打进**每一个**槽位,
断言它们一个字节都到不了回执里。

============ 攻击串从哪来 ============
直接引用 tests/test_nameguard_shape.py 的 `_MUST_BLOCK`。
⚠️ 那是**测试侧**的数据(不是从被测模块 import 阈值/正则/函数来判自己),
   而且它是全项目共享的一张"这些必须拦住"的清单 —— 在这里重抄一份的话,
   下次往那张清单里加一条新洞时,这半边不会跟着变严。
"""
# ruff: noqa: N802
from __future__ import annotations

import html

import pytest

from tests.test_nameguard_shape import _MUST_BLOCK
from tests.test_pumpchips import CA_CAP, UID_1000X, FakePump, _coin, _page, _pos

_EVILS = [raw for raw, _ in _MUST_BLOCK]
_IDS = [rule[:24] for _, rule in _MUST_BLOCK]


def _receipt(*, local_name="1000XCryptoD", api_name="1000XCryptoD",
             thesis=None, wallet="5f1AoBaq", x_username="x", kind="person") -> str:
    """
    走**生产代码路径**渲染 💊 那半边(不建库、不打网络):
    bot._pump_chips_lines → pumpchips.fetch_chips → formatter.render_pump_chip_row。
    """
    from src.bot import _pump_chips_lines

    row = _pos(UID_1000X, name=api_name, held=5e7, pnl=235.08)
    row["walletAddress"] = wallet
    row["xUsername"] = x_username
    row["accountKind"] = kind
    if thesis is not None:
        row["callout"] = {"calloutId": "c1", "thesis": thesis, "likes": 3,
                          "multiple": 1.5, "viewCount": 9}
    pump = FakePump({0: _page(1, [row])}, _coin())
    return "\n".join(_pump_chips_lines(lambda: pump, CA_CAP, {UID_1000X: local_name}))


def _assert_no_leak(out: str, evil: str) -> None:
    """
    零泄漏的判据:原串、转义后的串、以及"抽掉全部空白"的形态都不许出现。

    ⚠️ 抽掉空白那一道是必需的:`t. me/scam` 读者一眼仍读得出,
       只比对原串的话它换个写法就溜过去了。
    """
    flat = "".join(out.split())
    for form in (evil, html.escape(evil), html.escape(evil, quote=False)):
        assert form not in out, f"原样泄漏:{form!r}\n{out}"
        assert "".join(form.split()) not in flat or not form.strip(), \
            f"抽掉空白后泄漏:{form!r}\n{out}"


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_userName槽位零泄漏_本地名(evil):
    """
    ⚠️⚠️ 这是**唯一会被渲染**的槽位,也是最危险的一个:那一行长得像一条记录
       (名字 · 数量 · 盈亏),混进一句话就是一条伪造的记录。
    ⚠️ 被拦下时退回「未知用户」,而数量与盈亏是真值、照常显示 ——
       整行删掉等于把「名单里有人持有」谎报成「没人持有」。
    """
    out = _receipt(local_name=evil)
    _assert_no_leak(out, evil)
    assert "未知用户" in out
    assert "50,000,000 枚" in out, "名字被拦不该把真值一起带走"


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_userName槽位零泄漏_接口给的名字(evil):
    """⚠️ 本地名单里没存名字时会退回接口给的 userName —— 那条路同样要过门禁"""
    out = _receipt(local_name="", api_name=evil)
    _assert_no_leak(out, evil)
    assert "未知用户" in out


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_thesis槽位零泄漏(evil):
    """
    ⚠️ 观点正文**根本不显示**(/chips 报的是筹码不是观点,pump 观点有自己的推送)。
       这条钉住"它确实没被显示",顺带钉住"将来谁想显示它必须先过门禁"。
    """
    out = _receipt(thesis=evil)
    _assert_no_leak(out, evil)


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_walletAddress槽位零泄漏(evil):
    """⚠️ 钱包地址同样不显示:这一行已经有名字了,再挂一个 44 字符的地址只会把行撑爆"""
    out = _receipt(wallet=evil)
    _assert_no_leak(out, evil)


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_xUsername与accountKind槽位零泄漏(evil):
    """⚠️ 两个顺带的字段,同样一个都不显示"""
    out = _receipt(x_username=evil, kind=evil)
    _assert_no_leak(out, evil)


def test_五个槽位同时投毒也零泄漏(monkeypatch):
    """⚠️ 一次全上:任何一处漏了都会在这里露出来"""
    for evil in _EVILS:
        out = _receipt(local_name=evil, api_name=evil, thesis=evil,
                       wallet=evil, x_username=evil, kind=evil)
        _assert_no_leak(out, evil)


def test_回执里只出现我们自己写的那几个字段():
    """
    ⚠️⚠️ 反方向的不变量:pump 那几行里除了**名字、数量、盈亏百分比**之外,
       不该出现任何来自接口的字符串。这条用一个"每个字段都是可识别的哨兵值"的
       响应来验 —— 哪个字段被顺手渲染出去了,一眼看得见。
    """
    out = _receipt(local_name="正常名字", api_name="哨兵_apiname",
                   thesis="哨兵_thesis", wallet="哨兵_wallet",
                   x_username="哨兵_x", kind="哨兵_kind")
    assert "「正常名字」" in out
    for sentinel in ("哨兵_apiname", "哨兵_thesis", "哨兵_wallet",
                     "哨兵_x", "哨兵_kind"):
        assert sentinel not in out, f"{sentinel} 被渲染出去了:\n{out}"
