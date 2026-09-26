"""
用户标签(/tag):形状规则 / 存库与老库迁移 / /tag /untag 命令 / 推送标题 / 推送主路径。

用户要的(2026-09-26):给名单里的人打「盈利10w」「底部选手」这类标签,推送时一眼看出是什么类型的交易员。
选定的形态:推送**标题**里名字后面显示成 `#底部选手` —— Telegram 会把它变成可点击的话题。

⚠️ 断言写死字面量;全部离线、临时库,绝不碰 data/fomo.db。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。
from __future__ import annotations

import json
import sqlite3

import pytest

from src import store
from src.formatter import render, render_transfer_in_watch
from src.models import EVENT_TRANSFER_IN
from src.nameguard import safe_tag, safe_tags
from tests.conftest import make_event


@pytest.fixture
def db(tmp_path):
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp_path / "tags.db")
    store.init_db()
    try:
        yield store.DB_PATH
    finally:
        mp.undo()


def _bot(monkeypatch, tmp_path, members=(("u-1", "alice"), ("u-2", "bob"))):
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tags_bot.db")
    store.init_db()
    with store.get_conn() as conn:
        for uid, handle in members:
            store.add_watch_user(conn, uid, handle, handle.upper())
    return CommandBot(client=None, notifier=None)


def _tags_of(uid):
    with store.get_conn() as conn:
        return store.watch_tags(conn).get(uid, ())


# ============================================================
# 形状规则(nameguard.safe_tag)—— 照 Telegram 话题的规则定
# ============================================================
class Test形状:
    @pytest.mark.parametrize(("raw", "want"), [
        ("底部选手", "底部选手"), ("盈利10w", "盈利10w"), ("#底部选手", "底部选手"),
        ("  #Degen ", "Degen"), ("ok_1", "ok_1"), ("早期_埋伏", "早期_埋伏"),
        ("一二三四五六七八九十一二三四五六", "一二三四五六七八九十一二三四五六"),   # 恰好 16 个字
    ])
    def test_合格的(self, raw, want):
        assert safe_tag(raw) == want

    @pytest.mark.parametrize("raw", [
        "", "#", "胜率80%", "盈利 10w", "$盈利", "<b>x</b>", "a&b", "t.me/scam", "🔥大佬",
        "123", "1_2", "___", "底部​选手", "底部‮选手",
        "一二三四五六七八九十一二三四五六七",                                      # 17 个字
        None, 123, ["x"],
    ])
    def test_不合格的一律None(self, raw):
        """空格 / % / $ 会把 TG 话题截断;HTML、零宽、双向控制符、emoji 都不是字母数字"""
        assert safe_tag(raw) is None

    def test_序列逐个过_坏的只丢自己_去重不分大小写_最多5个(self):
        got = safe_tags(["底部选手", "胜率80%", "Degen", "degen", "a", "b", "c", "d"])
        assert got == ("底部选手", "Degen", "a", "b", "c")

    def test_去重不分大小写_两种先后顺序都去(self):
        """#Degen 与 #DEGEN 在 TG 里是同一个话题。⚠️ 两种顺序都要测:只测一种,判重漏掉 casefold 也照样绿"""
        assert safe_tags(["degen", "DEGEN"]) == ("degen",)
        assert safe_tags(["DEGEN", "degen"]) == ("DEGEN",)

    def test_单个字符串按一个标签处理_不拆成字符(self):
        assert safe_tags("底部选手") == ("底部选手",)

    @pytest.mark.parametrize("raw", [None, 5, [], ()])
    def test_空或怪输入是空元组(self, raw):
        assert safe_tags(raw) == ()


# ============================================================
# 存库
# ============================================================
class Test存库:
    def test_写入读出_只返回名单里的人(self, db):
        with store.get_conn() as conn:
            store.add_watch_user(conn, "u-1", "alice", "ALICE")
            store.add_watch_user(conn, "u-2", "bob", "BOB")
            store.set_watch_tags(conn, "u-1", ["底部选手", "盈利10w"])
            store.set_watch_tags(conn, "u-2", ["早期"])
            store.remove_watch_user(conn, "bob")
            assert store.watch_tags(conn) == {"u-1": ("底部选手", "盈利10w")}

    def test_清空存NULL不存空数组(self, db):
        with store.get_conn() as conn:
            store.add_watch_user(conn, "u-1", "alice", "ALICE")
            store.set_watch_tags(conn, "u-1", ["x1"])
            store.set_watch_tags(conn, "u-1", ())
            raw = conn.execute("SELECT tags FROM watch_users WHERE user_id='u-1'").fetchone()["tags"]
        assert raw is None

    def test_中文按原样存_不转义成unicode码(self, db):
        with store.get_conn() as conn:
            store.add_watch_user(conn, "u-1", "alice", "ALICE")
            store.set_watch_tags(conn, "u-1", ["底部选手"])
            raw = conn.execute("SELECT tags FROM watch_users WHERE user_id='u-1'").fetchone()["tags"]
        assert raw == '["底部选手"]'

    def test_库里的JSON坏了_那个人当没有标签_不炸(self, db):
        with store.get_conn() as conn:
            store.add_watch_user(conn, "u-1", "alice", "ALICE")
            conn.execute("UPDATE watch_users SET tags = '{坏的' WHERE user_id = 'u-1'")
            assert store.watch_tags(conn) == {}

    def test_老库自动补列_谁都没有标签(self, tmp_path, monkeypatch):
        """老库的 watch_users 没有 tags 列:init_db 必须补上,补完之后谁都没有标签(推送逐字节不变)"""
        path = tmp_path / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE watch_users (user_id TEXT PRIMARY KEY, handle TEXT NOT NULL, "
                    "display_name TEXT, added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
                    "removed_at TEXT, stats_ready INTEGER NOT NULL DEFAULT 0, note TEXT)")
        con.execute("INSERT INTO watch_users (user_id, handle, added_at) VALUES ('u-1', 'alice', 'x')")
        con.commit()
        con.close()
        monkeypatch.setattr(store, "DB_PATH", path)
        store.init_db()
        with store.get_conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(watch_users)")}
            assert "tags" in cols
            assert store.watch_tags(conn) == {}


# ============================================================
# /tag /untag
# ============================================================
class Test命令:
    def test_加标签_回执说加了什么和现在有什么(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        out = b._cmd_tag("alice 底部选手 盈利10w")
        assert out == "🏷 已给 ALICE 加上 #底部选手 #盈利10w\n现在:#底部选手 #盈利10w", out
        assert _tags_of("u-1") == ("底部选手", "盈利10w")

    def test_带井号也认_已有的不重复加(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice 底部选手")
        out = b._cmd_tag("alice #底部选手 早期")
        assert "已给 ALICE 加上 #早期" in out and "现在:#底部选手 #早期" in out, out

    def test_不合格的不加_合格的照加_说清规则(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        out = b._cmd_tag("alice 胜率80% 底部选手 <b>")
        assert _tags_of("u-1") == ("底部选手",)
        assert "⚠️ 这些不合格,没加:胜率80% &lt;b&gt;" in out, out
        assert "只能用中英文、数字和下划线" in out
        assert "<b>" not in out, "用户输入必须转义"

    def test_超过5个的不加_说清楚(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice a1 a2 a3 a4")
        out = b._cmd_tag("alice a5 a6 a7")
        assert _tags_of("u-1") == ("a1", "a2", "a3", "a4", "a5")
        assert "⚠️ 每人最多 5 个标签,这些没加上:#a6 #a7" in out, out

    def test_名单里没有这个人(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        out = b._cmd_tag("nobody 底部选手")
        assert out.startswith("❓ 名单里没有") and "/add" in out, out

    def test_只写handle是看他的标签(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        assert "还没有标签" in b._cmd_tag("alice")
        b._cmd_tag("alice 底部选手")
        assert b._cmd_tag("alice") == "🏷 ALICE:#底部选手"

    def test_不带参数是按标签分组的总览_人多的排前面(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        assert "还没有人打过标签" in b._cmd_tag("")
        # ⚠️ 数据刻意让「人多的」按字面排在后面(盈 > 底),否则按字面排序也能蒙对
        b._cmd_tag("alice 底部选手 盈利10w")
        b._cmd_tag("bob 盈利10w")
        lines = b._cmd_tag("").split("\n")
        assert lines[0] == "🏷 <b>标签</b>(2 人 · 2 个标签)", lines
        assert lines[1] == "#盈利10w · ALICE、BOB", lines
        assert lines[2] == "#底部选手 · ALICE", lines

    def test_去掉指定标签(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice 底部选手 盈利10w")
        out = b._cmd_untag("alice #盈利10w")
        assert out == "🏷 已去掉 ALICE 的 #盈利10w\n现在:#底部选手", out
        assert _tags_of("u-1") == ("底部选手",)

    def test_不写标签是全部清空(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice 底部选手 盈利10w")
        out = b._cmd_untag("alice")
        assert out == "🏷 已清空 ALICE 的标签(原来是 #底部选手 #盈利10w)", out
        assert _tags_of("u-1") == ()

    def test_去一个他没有的标签(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice 底部选手")
        assert "没有这些标签" in b._cmd_untag("alice 早期")
        assert _tags_of("u-1") == ("底部选手",)

    def test_list里显示标签(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        b._cmd_tag("alice 底部选手")
        line = next(ln for ln in b._cmd_list().split("\n") if "@alice" in ln)
        assert "<b>ALICE</b> @alice #底部选手 · " in line, line

    def test_命令分发认得tag和untag(self, monkeypatch, tmp_path):
        b = _bot(monkeypatch, tmp_path)
        assert "已给 ALICE 加上 #底部选手" in b._dispatch("/tag", "alice 底部选手")
        assert "已清空" in b._dispatch("/untag", "alice")


# ============================================================
# 推送标题
# ============================================================
class Test推送标题:
    def test_标签紧跟名字_在动作之前(self):
        head = render(make_event(user_handle="lasercat"), tags=["底部选手", "盈利10w"]).split("\n")[0]
        assert head == "🟢 <b>maxpain</b> (@lasercat) #底部选手 #盈利10w · 加仓 · <b>$TOAD</b>", head

    def test_没有标签时标题逐字节不变(self):
        ev = make_event(user_handle="lasercat")
        assert render(ev).split("\n")[0] == render(ev, tags=()).split("\n")[0] == \
            "🟢 <b>maxpain</b> (@lasercat) · 加仓 · <b>$TOAD</b>"

    def test_行首仍是事件emoji_星标在它后面(self):
        head = render(make_event(), starred=True, tags=["底部选手"]).split("\n")[0]
        assert head.startswith("🟢 ⭐ <b>maxpain</b> #底部选手 · "), head

    def test_库里混进坏标签_渲染入口逐个丢(self):
        """⚠️ 标签只有管理员能写,但库可能被手改 —— 渲染入口是最后一道门"""
        head = render(make_event(), tags=["<b>x</b>", "胜率80%", "ok_1", "123"]).split("\n")[0]
        assert head == "🟢 <b>maxpain</b> #ok_1 · 加仓 · <b>$TOAD</b>", head

    def test_标题后缀自己也过门禁_不只靠渲染入口(self):
        """
        刻意保留的第二道(与 _launchpad_line 再过一次 safe_launchpad 同一个做法):
        入口那道被谁绕开了,这里仍然不让坏标签进标题。
        """
        from src.formatter import _tags_suffix
        assert _tags_suffix(["<b>x</b>", "胜率80%", "ok"]) == " #ok"
        assert _tags_suffix(None) == ""

    def test_转入逐条推送也带(self):
        ev = make_event(EVENT_TRANSFER_IN, event_id="TIN:1", user_handle="lasercat", amount_usd=500.0)
        head = render_transfer_in_watch(ev, tags=["底部选手"]).split("\n")[0]
        assert head.startswith("📥 <b>maxpain</b> (@lasercat) #底部选手 · "), head


# ============================================================
# 推送主路径(poller)
# ============================================================
class Test推送主路径:
    def test_打了标签的人的推送标题带标签(self, monkeypatch, tmp_path):
        from tests.test_mcap_filter import _swap, _tick
        from tests.test_poller import _add_ready

        monkeypatch.setattr(store, "DB_PATH", tmp_path / "push.db")
        store.init_db()
        _add_ready("uA", "alice")
        _add_ready("uB", "bob")
        with store.get_conn() as conn:
            store.set_watch_tags(conn, "uA", ["底部选手"])
        _, tg = _tick({"uA": [_swap("a1", "SMOL", mcap=300_000)],
                       "uB": [_swap("b1", "WHALE", mcap=300_000)]})
        heads = {m.split("\n")[0] for m in tg.sent}
        assert any("#底部选手" in h and "$SMOL" in h for h in heads), heads
        assert not any("#" in h.split(" · ")[0] for h in heads if "$WHALE" in h), heads

    def test_标签读不出来_推送照发_只是不带标签(self, monkeypatch, tmp_path):
        from tests.test_mcap_filter import _swap, _tick
        from tests.test_poller import _add_ready

        monkeypatch.setattr(store, "DB_PATH", tmp_path / "push2.db")
        store.init_db()
        _add_ready("uA", "alice")

        def boom(conn):
            raise sqlite3.OperationalError("no such column: tags")
        monkeypatch.setattr(store, "watch_tags", boom)
        _, tg = _tick({"uA": [_swap("a1", "SMOL", mcap=300_000)]})
        assert len(tg.sent) == 1 and "#" not in tg.sent[0].split("\n")[0], tg.sent


def test_JSON与门禁的往返():
    """存进去的就是 safe_tags 认的样子:读回来过一遍门禁不丢东西"""
    tags = ["底部选手", "盈利10w", "Degen"]
    assert safe_tags(json.loads(json.dumps(tags, ensure_ascii=False))) == tuple(tags)
