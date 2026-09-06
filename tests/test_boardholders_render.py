"""
🏅 盈利榜持有人 —— 渲染层(formatter 的那一块)。

⚠️ 断言写死字面量:措辞、名次、数字全部手抄在这个文件里,
   **不从 src.formatter import 任何常量 / 阈值 / 格式化函数来判自己**。
⚠️ 走的是**生产渲染入口**(render / render_pump_trade / render_transfer_in_watch),
   门禁(@_guard_untrusted → safe_board_rows)因此是真的经过了。
"""
# ruff: noqa: N802
from __future__ import annotations

import json
import pathlib

import pytest

from src import boardholders as bh
from src.formatter import render, render_pump_trade, render_transfer_in_watch
from tests.conftest import make_event

FIX = pathlib.Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


BOARD = bh.parse_board(load("fomo_leaderboard_24h.json"))


def args(name: str) -> dict:
    """走生产取值路径拿到 render 的那两个参数。"""
    blk = bh.match_block(BOARD, load(f"fomo_top_holders_{name}.json"), 150)
    return {} if blk is None else blk.render_args()


def ev(**kw):
    return make_event(network_id="robinhood",
                      token_address="0x385f4f8ae47651ce5f58f5265395a669f8281e18",
                      token_symbol="MEME", amount_usd=1234.5, **kw)


# ============================================================
# 两套口径的完整文案
# ============================================================
class Test两套口径:
    def test_下界那套(self):
        """robinhood 的 MEME:97/15305 → `≥10 人` + 「没显示≠没有」。"""
        out = render(ev(), **args("robinhood_meme")).split("\n")
        assert "🏅 盈利榜持有人 ≥10 人" in out
        assert ("   #1 「unipcs」 · 7,330,874 枚 · 粉丝 542,169 · 全平台24h +$6.00M"
                in out)
        assert ("   #28 「Aurelius0121」 · 17,395,231 枚 · 粉丝 275,827 · "
                "全平台24h +$579.47K" in out)
        assert "   另有 7 人在榜" in out
        assert ("   ⚠️ 只比对了前 97/15,305 名持有人,榜只到前 150 名 —— 没显示≠没有"
                in out)

    def test_精确那套(self):
        """robinhood 的 CLEAT:87/87 → `1 人`(**不带 ≥**)+「已全数比对」。"""
        out = render(ev(), **args("robinhood_cleat")).split("\n")
        assert "🏅 盈利榜持有人 1 人" in out
        assert "🏅 盈利榜持有人 ≥1 人" not in out
        assert ("   #144 「deliveryydriver」 · 10,609,159 枚 · 粉丝 1,051 · "
                "全平台24h +$167.94K" in out)
        assert "   ⚠️ 87 名持有人已全数比对,榜只到前 150 名,榜外的不算" in out
        assert "没显示≠没有" not in "\n".join(out)

    def test_两套写法一眼可辨(self):
        """⚠️ 这是本功能的核心诚实点:精确不带 ≥、下界带 ≥,且注脚措辞完全不同。"""
        lower = render(ev(), **args("robinhood_meme"))
        exact = render(ev(), **args("robinhood_cleat"))
        assert "≥" in lower and "≥" not in exact
        assert "只比对了前" in lower and "只比对了前" not in exact
        assert "已全数比对" in exact and "已全数比对" not in lower

    def test_榜的那半句在精确文案里也在(self):
        """⚠️ 持有人拿全了,**榜仍然只有 150 名** —— 那个下界永远存在。"""
        assert "榜只到前 150 名" in render(ev(), **args("robinhood_cleat"))

    def test_第一百名之后的人也印得出来(self):
        """⚠️⚠️ base 的 EVAL 命中 #124 / #134 —— 夹在 100 时这两行根本不存在。"""
        out = render(ev(), **args("base_eval"))
        assert "#124 「BertLuvv」" in out
        assert "#134 「EricCryptoman」" in out


# ============================================================
# 零命中 → 整块不出现
# ============================================================
class Test零命中:
    def test_不传参数时整块不出现(self):
        out = render(ev())
        assert "🏅" not in out
        assert "盈利榜持有人" not in out

    def test_绝不打零人(self):
        """⚠️⚠️ 「0 人」是一句"我们查过了、确实没有"的断言 —— 我们没有资格说。"""
        for a in ({}, args("fomo_top_holders_monad_gmonad".replace(
                "fomo_top_holders_", "")), args("empty")):
            out = render(ev(), **a)
            assert "0 人" not in out
            assert "🏅" not in out

    def test_命中数为零的口径也不出块(self):
        out = render(ev(), board_holders=(), board_scope=(0, 97, 15305, False, 150))
        assert "🏅" not in out

    def test_口径缺失时整块不出现(self):
        """⚠️ 有行没口径 = 说不出"这个数是精确还是下界" → 宁可整块不说。"""
        rows = ((1, "unipcs", 1.0, 10, 100.0),)
        for scope in (None, (), (1, 2), "x"):
            assert "🏅" not in render(ev(), board_holders=rows, board_scope=scope)


# ============================================================
# 一行里四段各自独立
# ============================================================
class Test一行里的四段:
    def test_数量取不到就少那一段(self):
        out = render(ev(), board_holders=((7, "alice", None, 100, 5000.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #7 「alice」 · 粉丝 100 · 全平台24h +$5.00K" in out.split("\n")

    def test_粉丝取不到就少那一段(self):
        out = render(ev(), board_holders=((7, "alice", 12.0, None, 5000.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #7 「alice」 · 12 枚 · 全平台24h +$5.00K" in out.split("\n")

    def test_盈亏取不到就少那一段(self):
        out = render(ev(), board_holders=((7, "alice", 12.0, 100, None),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #7 「alice」 · 12 枚 · 粉丝 100" in out.split("\n")

    def test_粉丝为零照常显示(self):
        """⚠️ 0 个粉丝是真实值(新号),不是缺失 —— 判空一律 is None。"""
        out = render(ev(), board_holders=((7, "alice", 12.0, 0, 5000.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "粉丝 0" in out

    def test_盈亏为负照实显示(self):
        """⚠️ 24h 榜实测 150/150 全是正的,但真出现负数也照实印,绝不吞符号。"""
        out = render(ev(), board_holders=((7, "alice", 12.0, 100, -323150.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "全平台24h -$323.15K" in out

    def test_名次不是正整数时整行丢弃(self):
        for bad in (0, -1, "3", 1.5, True, None):
            out = render(ev(), board_holders=((bad, "alice", 12.0, 100, 5000.0),),
                         board_scope=(1, 90, 90, True, 150))
            assert "🏅" not in out, bad

    def test_一行都渲染不出来时整块消失(self):
        out = render(ev(), board_holders=((0, "alice", 1.0, 1, 1.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "盈利榜持有人" not in out


# ============================================================
# 截断
# ============================================================
class Test截断:
    def test_最多列三行(self):
        out = render(ev(), **args("solana_stonk")).split("\n")
        rows = [ln for ln in out if ln.startswith("   #")]
        assert len(rows) == 3

    def test_人数说的是命中总数不是行数(self):
        """⚠️⚠️ solana 的 STONK 命中 13 人,只列 3 行 —— 头一句必须还是 13。"""
        out = render(ev(), **args("solana_stonk"))
        assert "🏅 盈利榜持有人 ≥13 人" in out
        assert "   另有 10 人在榜" in out

    def test_不超过三人时没有另有那一句(self):
        assert "另有" not in render(ev(), **args("base_eval"))

    def test_按名次升序(self):
        out = render(ev(), **args("robinhood_meme")).split("\n")
        ranks = [int(ln.split("#")[1].split(" ")[0].replace(",", ""))
                 for ln in out if ln.startswith("   #")]
        assert ranks == sorted(ranks) == [1, 28, 41]


# ============================================================
# ⚠️⚠️ 绝不拿币上的盈亏冒充全平台 24h
# ============================================================
class Test绝不混用两种盈亏:
    def test_印出来的是榜上的那个数(self):
        """
        实测 MEME 第一大持有人(榜 #28):全平台 24h **+$579.47K**,
        这个币上 pnl **-$125,514.89**。印错就是"符号都反了"的错误信息。
        """
        raw = load("fomo_top_holders_robinhood_meme.json")
        assert raw["topHolders"][0]["pnl"] == -125514.89
        out = render(ev(), **args("robinhood_meme"))
        assert "#28 「Aurelius0121」" in out
        assert "全平台24h +$579.47K" in out
        assert "125,514" not in out
        assert "-$125.51K" not in out

    def test_全平台三个字不许省(self):
        """⚠️ 同一条推送里紧挨着就有一个"这个币上的"未实现盈亏,两个数完全不是一回事。"""
        out = render(ev(unrealized_pnl=-999.0), **args("robinhood_meme"))
        assert "全平台24h" in out
        assert "未实现盈亏" in out          # 两行同时在,措辞必须区分得开


# ============================================================
# 门禁:handle 是本人可控的
# ============================================================
class Test门禁:
    def test_恶意_handle_退回未知用户而不是整行消失(self):
        """
        ⚠️ 这一行的其余三段是真值,而 `#28` 本身就能在榜上定位到人 ——
           整行删掉会让上面那句「N 人」与列出的行数对不上。
        """
        out = render(ev(),
                     board_holders=((28, "已清仓 · 亏损 99%", 11020000.0, 218710,
                                     323150.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "已清仓" not in out
        assert "亏损 99%" not in out
        assert "   #28 未知用户 · 11,020,000 枚 · 粉丝 218,710 · 全平台24h +$323.15K" \
            in out.split("\n")

    @pytest.mark.parametrize("evil", [
        "t.me/scamgroup", "Send SOL to my wallet now", "已清仓 · 亏损 99%",
        "javascript:alert(1)", "0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18",
        "假「名字」", "13800138000",
    ])
    def test_几种典型攻击串一个都进不来(self, evil):
        """⚠️ 形状不合格 → 退回「未知用户」(这一行的其余三段是真值)。"""
        out = render(ev(), board_holders=((5, evil, 1.0, 1, 1.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert evil not in out
        assert "未知用户" in out

    @pytest.mark.parametrize("evil", [
        "uni​pcs",      # 零宽空格 → 归一化后正好是榜上第 1 名 unipcs
        "uni­pcs",      # 软连字符(类别同样是 Cf)
        "unip﻿cs",      # BOM
        "uni⁠pcs",      # word joiner
        "币‮ pmup",     # bidi 覆盖(它也是 Cf)
    ], ids=["零宽空格", "软连字符", "BOM", "word-joiner", "bidi"])
    def test_隐形字符的_handle_整行丢弃而不是退回未知用户(self, evil):
        """
        ⚠️⚠️ 这一类**不是**"名字印不出来",是**定向冒名**:删掉隐形字符之后剩下的串
           正好等于**另一个真人**的 handle(`uni​pcs` → `unipcs`,榜上第 1 名)。
           显示归一化后的名字 = 显示一个作者从没写过的名字,而且既冒名又搜不到人。
           处置与 nameguard 既有的 bidi 口径同一条:**整行丢弃**。
           这里只有这一行,所以整块消失(绝不退回「未知用户」)。
        """
        out = render(ev(), board_holders=((5, evil, 1.0, 1, 1.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert evil not in out
        assert "unipcs" not in out
        assert "未知用户" not in out
        assert "🏅" not in out
        assert "#5" not in out

    def test_隐形字符只丢它自己那一行(self):
        """⚠️ 独立性:同一块里干净的那一行照常显示,「N 人」由「另有」如实兜住。"""
        out = render(ev(), board_holders=((1, "uni​pcs", 1.0, 1, 1.0),
                                          (7, "ether_monk", 2.0, 3, 4.0)),
                     board_scope=(2, 90, 90, True, 150))
        assert "「ether_monk」" in out
        assert "unipcs" not in out
        assert "🏅 盈利榜持有人 2 人" in out
        assert "   另有 1 人在榜" in out

    def test_带下划线的真实_handle_照常显示(self):
        """⚠️ 实测榜上有 `The__Solstice` / `ether_monk` / `397397` 这类;丢掉它们
        等于印一条**认不出人**的记录 —— safe_display 会丢 9.33%,这道门丢 0.00%。"""
        for good in ("The__Solstice", "ether_monk", "397397", "0xnobi", "m0f0"):
            out = render(ev(), board_holders=((5, good, 1.0, 1, 1.0),),
                         board_scope=(1, 90, 90, True, 150))
            assert f"「{good}」" in out, good

    def test_容器内的分隔符伪造不出字段(self):
        out = render(ev(), board_holders=((5, "a · b", 1.0, 1, 1.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "a · b" not in out

    def test_不用_at_前缀_免得变成_telegram_提及(self):
        """⚠️ `@xxx` 在 Telegram 里是可点的用户名提及 —— 等于替陌生账号做链接。"""
        out = render(ev(), board_holders=((5, "unipcs", 1.0, 1, 1.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "@unipcs" not in out
        assert "「unipcs」" in out

    def test_行数在门禁那一侧也有第二道上限(self):
        rows = tuple((i + 1, f"u{i}", 1.0, 1, 1.0) for i in range(50))
        out = render(ev(), board_holders=rows, board_scope=(50, 90, 90, True, 150))
        assert len([ln for ln in out.split("\n") if ln.startswith("   #")]) == 3


# ============================================================
# 独立性:这一块绝不带走整条推送
# ============================================================
class Test独立性:
    @pytest.mark.parametrize("bad", [
        "字符串不是行", 123, [None], [("只有一列",)], [(1, 2)],
        [(1, "a", 2, 3)], object(),
    ])
    def test_脏数据进来推送照发(self, bad):
        out = render(ev(), board_holders=bad, board_scope=(1, 1, 1, True, 150))
        assert "🟢" in out and "$MEME" in out
        assert "消息渲染异常" not in out

    def test_口径是脏数据推送照发(self):
        rows = ((1, "unipcs", 1.0, 1, 1.0),)
        for scope in (object(), {"a": 1}, (1, 2, 3, 4, 5, 6, 7)):
            out = render(ev(), board_holders=rows, board_scope=scope)
            assert "🟢" in out and "消息渲染异常" not in out

    def test_门禁自己炸了也只丢这一块(self):
        class Boom:
            def __iter__(self):
                raise RuntimeError("上游给了个会炸的东西")

        out = render(ev(), board_holders=Boom(), board_scope=(1, 1, 1, True, 150))
        assert "🟢" in out and "🏅" not in out

    def test_其余各行一字不动(self):
        """⚠️ 加了这一块之后,没有这一块的那条消息必须与改造前逐字节一致。"""
        base = render(ev(unrealized_pnl=12.0, market_cap=1e6), buyers=2, watchlist=9)
        with_blk = render(ev(unrealized_pnl=12.0, market_cap=1e6), buyers=2,
                          watchlist=9, **args("robinhood_cleat"))
        added = [ln for ln in with_blk.split("\n") if ln not in base.split("\n")]
        assert all(ln.startswith(("🏅", "   ")) for ln in added), added
        assert set(base.split("\n")) <= set(with_blk.split("\n"))


# ============================================================
# 另外两条推送
# ============================================================
class Test另外两条推送:
    def test_pump_成交也带这一块(self):
        out = render_pump_trade(username="bob", side="buy", token_symbol="MEME",
                                coin_mint="0xabc", amount_usd=10.0,
                                network_id="solana", **args("robinhood_cleat"))
        assert "🏅 盈利榜持有人 1 人" in out
        assert "#144 「deliveryydriver」" in out

    def test_转入逐条也带这一块(self):
        out = render_transfer_in_watch(make_event(event_type="TRANSFER_IN"),
                                       **args("robinhood_cleat"))
        assert "🏅 盈利榜持有人 1 人" in out

    def test_两条推送零命中时同样整块不出现(self):
        assert "🏅" not in render_pump_trade(username="bob", side="buy",
                                             token_symbol="M", coin_mint="0xabc")
        assert "🏅" not in render_transfer_in_watch(
            make_event(event_type="TRANSFER_IN"))

    def test_pump_成交里的脏数据也不影响推送(self):
        out = render_pump_trade(username="bob", side="buy", token_symbol="MEME",
                                coin_mint="0xabc", amount_usd=10.0,
                                board_holders="脏", board_scope=None)
        assert "bob" in out and "$MEME" in out


# ============================================================
# ⚠️⚠️ 人数类槽位的**上界**(与 🧑‍🤝‍🧑 那一行同一条阈值)
# ============================================================
# 干净树上走生产渲染路径复现过这两条:
#     totalHolders = 13800138000 → ⚠️ 只比对了前 1/13,800,138,000 名持有人…
#     followers    = 13800138000 → #1「unipcs」· 粉丝 13,800,138,000 …
# 而**同一份 formatter** 早就为 🧑‍🤝‍🧑 那一行写了 10 亿的上界(同一个数走那一行是整行消失)。
# 两处处置不一致本身就是洞:攻击者换个槽位就行。
# ⚠️ 判据不是「防御性编程」:一串 10~11 位数字正是手机号 / QQ 号的形态,
#    印出去既是假事实、又是一条可拨可加的目标。
class Test人数槽位的上界:
    def test_粉丝数超上界时那一段消失(self):
        out = render(ev(), board_holders=((1, "unipcs", 7330874.0, 13800138000,
                                           323150.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "13,800,138,000" not in out
        assert "13800138000" not in out
        assert "   #1 「unipcs」 · 7,330,874 枚 · 全平台24h +$323.15K" in out.split("\n")

    def test_粉丝数正好在上界上照常显示(self):
        """⚠️ 上界是 10 亿(含),不是「大数一律不显示」—— 边界两侧各钉一次。"""
        out = render(ev(), board_holders=((1, "unipcs", None, 1000000000, None),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」 · 粉丝 1,000,000,000" in out.split("\n")

    def test_粉丝数比上界大一就消失(self):
        out = render(ev(), board_holders=((1, "unipcs", None, 1000000001, None),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」" in out.split("\n")
        assert "1,000,000,001" not in out
        assert "粉丝" not in out

    def test_持有人总数超上界时口径整行消失(self):
        """
        ⚠️⚠️ 与 🧑‍🤝‍🧑 那一行逐字同一条处置:这一行**说不出口**,
           少一行是「我们没说」,印出去是「我们说错了」。
        ⚠️ 刻意**不**退回「(平台没给总数)」—— 平台给了,只是给的是个说不出口的数。
        """
        out = render(ev(), board_holders=((1, "unipcs", 1.0, 1, 1.0),),
                     board_scope=(1, 97, 13800138000, False, 150))
        lines = out.split("\n")
        assert "13,800,138,000" not in out
        assert "🏅 盈利榜持有人 ≥1 人" in lines          # 这一块本身还在
        assert "「unipcs」" in out
        assert not any("只比对了" in ln for ln in lines)
        assert not any("平台没给总数" in ln for ln in lines)

    def test_总数正好在上界上照常显示(self):
        out = render(ev(), board_holders=((1, "unipcs", 1.0, 1, 1.0),),
                     board_scope=(1, 97, 1000000000, False, 150))
        assert "   ⚠️ 只比对了前 97/1,000,000,000 名持有人,榜只到前 150 名 —— 没显示≠没有" \
            in out.split("\n")

    def test_总数比上界大一就整行消失(self):
        out = render(ev(), board_holders=((1, "unipcs", 1.0, 1, 1.0),),
                     board_scope=(1, 97, 1000000001, False, 150))
        assert not any("只比对了" in ln for ln in out.split("\n"))

    def test_两个槽位与持有人那一行用的是同一条阈值(self):
        """
        ⚠️⚠️ 同一个 formatter 里**同样形态**的数必须**同样处置**。这条把三处放在
           一起打同一个数:🧑‍🤝‍🧑 整行消失、粉丝那一段消失、口径那一行消失。
           哪天有人给 🏅 这边另写一份阈值,三条断言就不会再同时成立。
        """
        phone = 13800138000
        out = render(ev(), token_holders=phone,
                     board_holders=((1, "unipcs", 1.0, phone, 1.0),),
                     board_scope=(1, 97, phone, False, 150))
        assert "13,800,138,000" not in out
        assert "🧑‍🤝‍🧑" not in out
        assert "粉丝" not in out
        assert "只比对了" not in out


# ============================================================
# ⚠️⚠️ 0 与 None 分得开:0 是真实值
# ============================================================
class Test零与缺失分得开:
    def test_全平台盈亏正好为零照常印出来(self):
        """
        ⚠️⚠️ 一个人今天不赚不亏是**真事**,不是「拿不到」。把 `if d is None`
           改成 `if not d`,这一段会静默消失 —— 而读者看到的是「这个人没有盈亏数据」,
           那是一句假话。(上一版全量一条都不红。)
        """
        out = render(ev(), board_holders=((1, "unipcs", 1.0, 1, 0.0),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」 · 1 枚 · 粉丝 1 · 全平台24h +$0.00" in out.split("\n")

    def test_全平台盈亏拿不到时才少那一段(self):
        out = render(ev(), board_holders=((1, "unipcs", 1.0, 1, None),),
                     board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」 · 1 枚 · 粉丝 1" in out.split("\n")
        assert "全平台24h" not in out

    def test_粉丝数为零与拿不到是两件事(self):
        zero = render(ev(), board_holders=((1, "unipcs", None, 0, None),),
                      board_scope=(1, 90, 90, True, 150))
        none = render(ev(), board_holders=((1, "unipcs", None, None, None),),
                      board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」 · 粉丝 0" in zero.split("\n")
        assert "   #1 「unipcs」" in none.split("\n")
        assert "粉丝" not in none

    def test_持仓数量为零与拿不到是两件事(self):
        """⚠️ 0 枚 = 清仓,是有意义的真实值。"""
        zero = render(ev(), board_holders=((1, "unipcs", 0.0, None, None),),
                      board_scope=(1, 90, 90, True, 150))
        assert "   #1 「unipcs」 · 0 枚" in zero.split("\n")


# ============================================================
# ⚠️⚠️ 两道门各自钉住(纵深防御,任一单侧降级都必须红)
# ============================================================
# 复验实测:只把 nameguard 那一侧降级成 safe_ident → 全量 4587 全绿;
#           只把 formatter 那一侧降级成 safe_ident → 全量 4587 全绿;
#           **两处同时降级**才 35 failed。
# ⇒ 227 条红队用例对**单点**降级完全不敏感:任一道门可以被悄悄拿掉而 CI 全绿。
# 原因很直白:红队全部走 render() 这个入口,两道门是**串联**的,拿掉一道另一道还在。
# 所以每一道门必须**各自**有一条只驱动它自己的用例。
#
# ⚠️ 两道门都留着(不是冗余可删):
#   · nameguard.safe_board_rows 是**渲染入口的收口**(_guard_untrusted 那张表),
#     它同时还做「行数上限 10」与「隐形字符整行丢弃」两件 formatter 那边没有的事;
#   · formatter._board_row 那一道是**幂等的第二道**,守的是「有人绕过入口直接调
#     _board_holders_lines / 新加一条渲染入口忘了登记字段」这条真实的路
#     (本仓库已经出现过一次:token_symbol 曾被登记成「已审查」,门在旁边被原样打开)。
class Test两道门各自钉住:
    def test_门禁那一侧独占用例(self):
        """
        ⚠️⚠️ 只驱动 nameguard.safe_board_rows。降级成 safe_ident 时 ——
           safe_ident **允许空格**,整句话会原样穿过去 —— 这条当场红。
        """
        from src.nameguard import safe_board_rows

        got = safe_board_rows([(28, "Send SOL to my wallet now", 1.0, 2, 3.0)])
        assert got == ((28, None, 1.0, 2, 3.0),)

        ok = safe_board_rows([(28, "The__Solstice", 1.0, 2, 3.0)])
        assert ok == ((28, "The__Solstice", 1.0, 2, 3.0),)

    def test_渲染那一侧独占用例(self):
        """
        ⚠️⚠️ 只驱动 formatter._board_row(绕开渲染入口那道收口)。
           降级成 safe_ident 时同样是整句话穿过去 —— 这条当场红。
        """
        from src.formatter import _board_row

        assert _board_row((28, "Send SOL to my wallet now", None, None, None)) == \
            "#28 未知用户"
        assert _board_row((28, "The__Solstice", None, None, None)) == \
            "#28 「The__Solstice」"

    def test_门禁那一侧的行数上限是十(self):
        """
        ⚠️⚠️ 上一版这个 10 **零覆盖**:改成 99999 全量一条都不红(渲染那一侧的 3
           把它盖住了)。它是「万一渲染那一侧被改」时最后一道量的闸。
        """
        from src.nameguard import safe_board_rows

        rows = [(i + 1, f"u{i}", 1.0, 1, 1.0) for i in range(50)]
        got = safe_board_rows(rows)
        assert len(got) == 10
        assert [r[0] for r in got] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    def test_渲染那一侧的行数上限是三(self):
        from src.formatter import _board_holders_lines

        rows = tuple((i + 1, f"u{i}", None, None, None) for i in range(50))
        body = [ln for ln in _board_holders_lines(rows, (50, 90, 90, True, 150))
                if ln.startswith("   #")]
        assert len(body) == 3


# ============================================================
# ⚠️⚠️ 隐形字符的**两道**处置各自钉住
# ============================================================
# 变异跑抓到:只把 safe_username 那一条 Cf 判断退回「只查 bidi」时,全量**一条都不红** ——
# 因为 safe_board_rows 的「整行丢弃」跑在前面,把它整个盖住了。
# 这正是 K5 那条教训的同一形状:串联的两道门,后面那道永远测不到。
# 所以 safe_username 必须有一条**只驱动它自己**的用例。
class Test隐形字符的两道处置:
    def test_门禁函数本身对任何隐形字符都返回空(self):
        """
        ⚠️⚠️ 只驱动 nameguard.safe_username。退回「只查 bidi」时:
           零宽空格 / 软连字符 / BOM / word-joiner 这四条全部漏过去,
           而漏过去的结果是 **"unipcs"** —— 榜上第 1 名的 handle,一次定向冒名。
        ⚠️ 这道门还给 /chips 的 💊 pump 成员行用(那一行也长得像一条记录),
           所以它自己必须是硬的,不能靠上游某个调用方替它把关。
        """
        from src.nameguard import safe_username

        for evil in ("uni​pcs", "uni­pcs", "unip﻿cs", "uni⁠pcs",
                     "uni‍pcs", "币‮ pmup"):
            assert safe_username(evil) is None, repr(evil)

    def test_干净的handle照常放行(self):
        """⚠️ 反向:代价必须是 0,否则这道门就把真人打成「未知用户」了。"""
        from src.nameguard import safe_username

        for good in ("unipcs", "The__Solstice", "ether_monk", "397397", "0xnobi"):
            assert safe_username(good) == good, good

    def test_门禁在成员行那一侧同样生效(self):
        """⚠️ 同一道门守着 💊 pump 成员行 —— 那一行的处置是退回「未知用户」。"""
        from src.formatter import render_pump_chip_row

        out = render_pump_chip_row(pump_username="uni​pcs", amount_held=1.0, pnl_pct=2.0)
        assert "unipcs" not in out
        assert "未知用户" in out
