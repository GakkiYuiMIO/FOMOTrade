"""
🏅 盈利榜持有人的**红队**:把每一条已知必拦的攻击串打进每一个槽位,断言零泄漏。

============ 为什么单开一个文件 ============
这一块的数据里有**三个**攻击者可控的自由文本字段:
  · 榜单行的 `userHandle` —— 谁都能把自己的 FOMO 用户名改成 `已清仓 · 亏损 99%`;
  · `displayName`(榜单行与持有人行各有一个)—— 同样本人可改,而且它允许中文/空格/emoji;
  · 持有人行的 `comment` —— 完全自由的正文,想写什么写什么。
本功能**只渲染 userHandle**(另外两个一个都不显示,理由见 nameguard.safe_board_rows
与 boardholders 模块头)。但"我们没打算显示它"不是一条能靠人维持的不变量 ——
下一个人加一行 `if comment: ...` 就破了。所以这里把攻击串打进**每一个**槽位,
断言它们一个字节都到不了推送里。

⚠️ 而且这一行长得像**一条记录**(`#28 「name」 · 数量 · 粉丝 · 全平台24h +$X`):
   混进一句话就是一条伪造的记录 —— 与 /chips 的 pump 成员行同一个威胁模型
   (见 tests/test_pumpchips_redteam.py)。

============ 攻击串从哪来 ============
直接引用 tests/test_nameguard_shape.py 的 `_MUST_BLOCK`(全项目共享的那张
"这些必须拦住"的清单)。⚠️ 在这里重抄一份的话,下次往那张清单里加一条新洞时,
这一块不会跟着变严。
"""
# ruff: noqa: N802
from __future__ import annotations

import html
import json
import pathlib
import unicodedata

import pytest

from src import boardholders as bh
from src.formatter import render, render_pump_trade, render_transfer_in_watch
from tests.conftest import make_event
from tests.test_nameguard_shape import _MUST_BLOCK

FIX = pathlib.Path(__file__).parent / "fixtures"
_EVILS = [raw for raw, _ in _MUST_BLOCK]
_IDS = [rule[:24] for _, rule in _MUST_BLOCK]

UID = "36adb85a-c0fd-5fa8-916d-8fdc32fe4237"     # 榜单第 1 名(unipcs)的真实 UUID


def _board(handle="unipcs", display="Unipcs"):
    """一行榜单。⚠️ 结构与真实响应逐字段一致(见 fixtures/fomo_leaderboard_24h.json)。"""
    return [{"id": UID, "userHandle": handle, "displayName": display,
             "followers": 542169, "pnl24h": 6002861.53}]


def _holders(display="Unipcs", comment=None, handle="unipcs"):
    """一份 /hodlers/top 响应,只放一个持有人 —— 就是榜上那个人。"""
    return {"totalHolders": 1,
            "topHolders": [{"humanAmount": 7330873.77, "value": 1.0,
                            "pnl": -125514.89, "unrealizedPnl": -125514.89,
                            "realizedPnl": 0, "comment": comment,
                            "showComment": comment is not None,
                            "user": {"id": UID, "userHandle": handle,
                                     "displayName": display,
                                     "followers": 542169}}]}


def _push(board_rows, holders_payload) -> str:
    """走**生产代码路径**渲染(不打网络):parse_board → match_block → render。"""
    blk = bh.match_block(bh.parse_board(board_rows), holders_payload, 150)
    args = {} if blk is None else blk.render_args()
    return render(make_event(network_id="robinhood",
                             token_address="0x" + "a" * 40,
                             token_symbol="MEME", amount_usd=1.0), **args)


def _leaked(evil: str, out: str) -> bool:
    """
    攻击串有没有漏进推送。

    ⚠️ 三种形态都算泄漏:原样、HTML 转义后、以及**去掉零宽/控制符之后**的样子
       (`t.​me/scam` 原样不在,但删掉零宽空格就是一个真的可点域名)。
    """
    stripped = "".join(c for c in evil if c.isprintable() and c not in "​‮")
    for form in (evil, html.escape(evil), stripped, html.escape(stripped)):
        if form and len(form) >= 3 and form in out:
            return True
    return False


def _has_invisible(s: str) -> bool:
    """含不含 Unicode 的**格式控制**字符(零宽空格 / 软连字符 / BOM / bidi …)。"""
    return any(unicodedata.category(ch) == "Cf" for ch in s)


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_userHandle_槽位零泄漏(evil):
    """
    ⚠️ 这是**唯一会被渲染**的那个槽位 —— 拦不住就直接印进推送。

    ⚠️⚠️ 拦住之后有**两套**处置,取决于攻击串是哪一类:
      · 含**隐形字符**(Cf:零宽空格 / 软连字符 / BOM / bidi)→ **整行丢弃**。
        这类串归一化之后正好会变成**另一个真人的 handle**(`uni​pcs` → `unipcs`,
        而 unipcs 就是这个榜的第 1 名),退回「未知用户」等于把一次定向冒名
        留在一个长得像"记录"的地方。这里只有这一行,所以整块消失。
      · 其余(形状不合格:带空格 / 域名 / 太长 …)→ 退回「未知用户」,
        这一行的其余三段是真值,`#1` 本身就能在榜上定位到人。
    """
    out = _push(_board(handle=evil), _holders(handle=evil))
    assert not _leaked(evil, out), out
    if _has_invisible(evil):
        assert "🏅" not in out, out
        assert "未知用户" not in out, out
    else:
        assert "未知用户" in out
        assert "#1 " in out


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_displayName_槽位零泄漏(evil):
    """
    ⚠️⚠️ 本功能**不渲染** displayName(理由见 nameguard.safe_board_rows:
       同一份 150 行语料里它在封闭形状门禁下丢 21.33%,要显示它就只能把门降级)。
       这条钉住"不渲染"这件事本身 —— 谁哪天加上去,它当场红。
    """
    out = _push(_board(display=evil), _holders(display=evil))
    assert not _leaked(evil, out), out
    assert "「unipcs」" in out          # 正常的 handle 照常显示


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_comment_槽位零泄漏(evil):
    """⚠️ 持有人的留言:本功能刻意不显示(一行已经够长,而它是完全自由的正文)。"""
    out = _push(_board(), _holders(comment=evil))
    assert not _leaked(evil, out), out


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_三个槽位一起上也零泄漏(evil):
    """⚠️ 笛卡尔积的对角线:同时投毒,任何一处漏了都算。"""
    out = _push(_board(handle=evil, display=evil),
                _holders(display=evil, comment=evil, handle=evil))
    assert not _leaked(evil, out), out


@pytest.mark.parametrize("evil", _EVILS, ids=_IDS)
def test_另外两条推送同样零泄漏(evil):
    """⚠️ pump 成交 / 转入逐条走的是同一块渲染代码,但入口不同 —— 各钉一遍。"""
    blk = bh.match_block(bh.parse_board(_board(handle=evil, display=evil)),
                         _holders(display=evil, comment=evil, handle=evil), 150)
    args = {} if blk is None else blk.render_args()
    a = render_pump_trade(username="bob", side="buy", token_symbol="MEME",
                          coin_mint="0xabc", amount_usd=1.0, **args)
    b = render_transfer_in_watch(make_event(event_type="TRANSFER_IN"), **args)
    assert not _leaked(evil, a), a
    assert not _leaked(evil, b), b


def test_夹具里的真实数据不含攻击串():
    """
    ⚠️ 反向体检:真实响应里如果本来就有一条 _MUST_BLOCK 样本,上面那些用例
       就可能是被真实数据"喂饱"的假绿。这条确认夹具是干净的。
    """
    raw = (FIX / "fomo_leaderboard_24h.json").read_text(encoding="utf-8")
    for evil in _EVILS:
        assert evil not in raw, evil


def test_一百五十个真实_handle_一个都没被误伤():
    """
    ⚠️⚠️ 红队的另一半:门太严同样是故障。实测 150 行榜单里
       safe_username 丢 **0 条**;换成 safe_display 会丢 14 条(9.33%),
       那 14 条读者看到的是一行**认不出人**的记录。
    """
    board_raw = json.loads((FIX / "fomo_leaderboard_24h.json").read_text(
        encoding="utf-8"))
    lost = []
    for row in board_raw:
        out = _push([row], {"totalHolders": 1,
                            "topHolders": [{"humanAmount": 1.0,
                                            "user": {"id": row["id"]}}]})
        # ⚠️ 两种误伤都要抓:退回「未知用户」,以及**整行被丢掉**(那时整块消失)。
        if "未知用户" in out or "🏅" not in out:
            lost.append(row["userHandle"])
    assert lost == [], lost
    assert len(board_raw) == 150
