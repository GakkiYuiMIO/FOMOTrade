"""
store.py 的单测 —— 功能 A(首次买入徽章)与功能 B(共识计数)的全部判定逻辑。

每条测试都对应设计文档里一个**必现 bug**,不是凑覆盖率的。
跑在 sqlite3 内存库上,不碰 data/fomo.db。

对应验收用例:#1 #2 #3 #4 #5 #7 #9。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。ruff 的 N802 只认 ASCII 小写。
from __future__ import annotations

import sqlite3

import pytest

from src import store
from src.models import (
    BADGE_ADD,
    BADGE_FIRST,
    EVENT_SELL,
    EVENT_TRANSFER_IN,
    REASON_LOCAL_STATS,
    REASON_NO_BASELINE,
    REASON_NO_SIDE,
    REASON_NO_TOKEN_KEY,
    REASON_NOT_BUY,
    REASON_QUOTE_TOKEN,
    now_iso,
)

from .conftest import (
    CA_CATE,
    CA_TOAD,
    CA_WSOL,
    TS_EARLY,
    TS_LATE,
    TS_MID,
    add_user,
    force_stats,
    make_event,
)


# ============================================================
# 名单管理 —— 验收用例 #9
# ============================================================
@pytest.mark.parametrize("raw", ["@Maxpain", "maxpain", "  @MAXPAIN  ", "@@maxpain"])
def test_验收9_handle归一化到同一个值(raw):
    """
    验收用例 #9:同一人用 @Maxpain / maxpain 各 add 一次。

    ⚠️ 不归一化会产生两行 watch_users,共识把一个人算两次 ——
       "3/12 人买过"里的 3 可能其实只有 2 个真人。
    """
    assert store.normalize_handle(raw) == "maxpain"


def test_验收9_同一user_id重复add只有一行且不重置基线(conn):
    """
    验收用例 #9:第二次 /add 回复"已在监控中"。

    ⚠️ 两个必现 bug:
       1) 产生两行 → 共识把一个人算两次
       2) 重置 stats_ready → 已建好的基线白白作废,该用户退回不打徽章的状态,
          而且下一 tick 还要重新拉 500 条历史
    """
    need_seed, msg = store.add_watch_user(conn, "u1", "@Maxpain", "MaxPain")
    assert need_seed is True and msg.startswith("✅")
    store.mark_stats_ready(conn, "u1")

    need_seed2, msg2 = store.add_watch_user(conn, "u1", "maxpain", "MaxPain")
    assert need_seed2 is False, "已就绪的用户不应该再建一次基线"
    assert "已在监控中" in msg2

    rows = conn.execute("SELECT * FROM watch_users").fetchall()
    assert len(rows) == 1
    assert rows[0]["stats_ready"] == 1, "重复 /add 把已建好的基线重置了"

    # handle 存**原始大小写**:它要显示给人看、还要拿去搜,大小写是它本身的一部分。
    # 去重的保证是 user_id 主键,不是把 handle 压成小写。
    # 重复 /add 会用新传入的值刷新名字(用户在 FOMO 上改名时靠这一步跟上),
    # 所以这里是第二次传入的 "maxpain"。
    assert rows[0]["handle"] == "maxpain"
    need_seed3, _ = store.add_watch_user(conn, "u1", "@GakkiYuiTifa", "新名字")
    assert need_seed3 is False, "刷新名字不该触发重建基线"
    row = store.get_watch_user(conn, "u1")
    assert (row["handle"], row["display_name"]) == ("GakkiYuiTifa", "新名字"), "改名后没跟上"
    assert row["stats_ready"] == 1, "刷新名字把基线重置了"

    # 查找必须忽略大小写,否则用户按自己记的写法输入就找不到人
    for typed in ("@GAKKIYUITIFA", "gakkiyuitifa", " GakkiYuiTifa "):
        assert store.find_user_by_handle(conn, typed) is not None, f"按 {typed!r} 查不到"


def test_del后再add必须重建基线(conn):
    """
    边界 A-6:/del → 两个月后 /add 回来。空窗期的买入本地一条记录都没有,
    不重建基线的话,空窗期建的仓位在下次加仓时会被误标 🌱。
    """
    add_user(conn, "u1", "maxpain", ready=True)
    ok, _ = store.remove_watch_user(conn, "u1")
    assert ok is True

    need_seed, _ = store.add_watch_user(conn, "u1", "maxpain", "MaxPain")
    assert need_seed is True
    row = store.get_watch_user(conn, "u1")
    assert row["active"] == 1
    assert row["stats_ready"] == 0, "/del 后回归必须把 stats_ready 归零重建基线"
    assert row["removed_at"] is None


def test_移除不在名单里的人给出明确失败(conn):
    ok, msg = store.remove_watch_user(conn, "nobody")
    assert ok is False and "未在监控名单中" in msg


def test_按handle也能移除(conn):
    """用户在 TG 里敲的是 /del maxpain,不是 user_id。"""
    add_user(conn, "u1", "maxpain")
    ok, _ = store.remove_watch_user(conn, "maxpain")
    assert ok is True
    assert store.get_watch_user(conn, "u1")["active"] == 0


def test_ready用户口径只认active且stats_ready(conn):
    """共识分母的唯一口径。分子必须用同一个谓词,否则会算出分子 > 分母。"""
    add_user(conn, "u1", "a", ready=True)
    add_user(conn, "u2", "b", ready=False)
    add_user(conn, "u3", "c", ready=True)
    store.remove_watch_user(conn, "u3")
    assert store.ready_user_ids(conn) == ["u1"]


def test_待建基线的用户每次只取一个(conn):
    """批量 /add 10 人 = 10 个 tick 内全部就绪,期间照常推送、只是不打徽章。"""
    add_user(conn, "u1", "a", ready=True)
    add_user(conn, "u2", "b", ready=False)
    add_user(conn, "u3", "c", ready=False)
    row = store.pick_one_pending_user(conn)
    assert row["user_id"] in ("u2", "u3")
    assert row["stats_ready"] == 0


# ============================================================
# 冷启动保护 —— 验收用例 #1
# ============================================================
def test_验收1_add时四类游标全部立刻设为当前时刻(conn):
    """
    验收用例 #1:空库 + /add 一个有 3 年历史的人 → **0 条历史事件推送**。

    游标是"不推历史"的**唯一**机制(刻意不引入 watermark / suppressed 第二套锁)。
    少设一类游标,那一类的三年历史就会在下一 tick 全量推进 TG。
    """
    store.add_watch_user(conn, "u1", "maxpain", "MaxPain")
    for kind in store.CURSOR_KINDS:
        assert store.get_cursor(conn, "u1", kind) is not None, f"{kind} 游标没设,历史会被全量推送"
    assert len(store.CURSOR_KINDS) == 4


def test_游标可以单独推进(conn):
    store.add_watch_user(conn, "u1", "maxpain", "MaxPain")
    store.set_cursor(conn, "u1", "swaps", "2026-08-11T10:00:00+00:00")
    assert store.get_cursor(conn, "u1", "swaps") == "2026-08-11T10:00:00+00:00"
    assert store.get_cursor(conn, "u1", "thesis") != "2026-08-11T10:00:00+00:00"


# ============================================================
# 事件落库
# ============================================================
def test_同一event_id第二次插入返回False(conn):
    """
    ⚠️ 调用方靠这个返回值决定要不要 upsert_stats。
       无脑累加的话,重复轮询会让 buy_count 一路虚增(20s 一次,一天 4320 次)。
    """
    ev = make_event(event_id="BUY:same")
    assert store.insert_event(conn, ev) is True
    assert store.insert_event(conn, ev) is False
    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 1


def test_验收4_四条拆单全部落库且共识只算一人(conn):
    """
    验收用例 #4:一笔买入被路由拆成 4 条等额同秒 swap。

    期望:4 条都落库(金额不少报)、buy_count=4、但**共识里该用户只算 1 人**。
    ⚠️ 共识如果在 fomo_events 上聚合,这里会算成 4 人 —— 必须在 user_token_stats 上聚合,
       它的主键 (user, network, token) 天然去重。
    """
    add_user(conn, "u1", "maxpain")
    for i in range(4):
        ev = make_event(event_id=f"BUY:h:split{i}", amount_usd=625.0)
        assert store.insert_event(conn, ev) is True
        store.upsert_stats(conn, ev)

    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 4
    total = conn.execute("SELECT SUM(amount_usd) s FROM fomo_events").fetchone()["s"]
    assert total == 2500.0, "拆单被误去重,金额少报了"

    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["buy_count"] == 4
    buyers, size = store.count_consensus(conn, make_event())
    assert (buyers, size) == (1, 1), "拆单把一个人算成了多人"


def test_只有第一条拆单能拿到首次徽章(conn):
    """
    验收用例 #4 的另一半:🌱 只出现 1 次。
    第 2~4 条判定时 buy_count 已 >0,自然退化为 🟢 —— 前提是 judge_badge 在 upsert_stats **之前** 调。
    """
    add_user(conn, "u1", "maxpain")
    badges = []
    for i in range(4):
        ev = make_event(event_id=f"BUY:h:split{i}")
        badge, reason = store.judge_badge(conn, ev)   # ⚠️ 顺序:先判定
        badges.append(badge)
        store.insert_event(conn, ev)
        store.upsert_stats(conn, ev)                  # ⚠️ 再累加
    assert badges == [BADGE_FIRST, BADGE_ADD, BADGE_ADD, BADGE_ADD]


def test_mark_sent写入共识时点值(conn):
    """共识数是时点值、事后无法重算,而"共识数 vs 后续涨幅"是明确的回溯需求。"""
    ev = make_event(event_id="BUY:x")
    store.insert_event(conn, ev)
    row = conn.execute("SELECT sent FROM fomo_events WHERE event_id='BUY:x'").fetchone()
    assert row["sent"] == 0, "落库时必须是未发送 —— 发之前就标已发,崩溃时会永久丢消息"

    store.mark_sent(conn, "BUY:x", cs_buyers=3, cs_watchlist=12)
    row = conn.execute("SELECT * FROM fomo_events WHERE event_id='BUY:x'").fetchone()
    assert (row["sent"], row["cs_buyers"], row["cs_watchlist"]) == (1, 3, 12)


def test_已发送的事件不再进补发队列(conn):
    store.insert_event(conn, make_event(event_id="BUY:a"))
    store.insert_event(conn, make_event(event_id="BUY:b"))
    store.mark_sent(conn, "BUY:a", 1, 1)
    ids = [r["event_id"] for r in store.load_unsent_recent(conn, minutes=10)]
    assert ids == ["BUY:b"]


def test_ingested_at的格式与SQLite的datetime不可直接比较(conn):
    """
    ⚠️ 契约问题(根因,不依赖时钟):
       models.now_iso() 产出 '2026-08-11T03:24:15+00:00'(ISO,**T** 分隔 + 偏移量),
       而 SQLite 的 datetime('now', ...) 产出 '2026-08-11 03:14:15'(**空格** 分隔、无偏移)。
       字符串比较到第 11 个字符时 'T'(0x54) > ' '(0x20) 恒成立 ——
       只要日期相同,再老的事件也会被判成"在窗口内"。
    """
    same_day_cutoff = conn.execute("SELECT datetime('2026-08-11 23:59:59')").fetchone()[0]
    very_old_same_day = "2026-08-11T00:00:01+00:00"
    assert very_old_same_day >= same_day_cutoff, (
        "如果这条断言不再成立,说明 now_iso() 或 SQLite 的输出格式变了,"
        "load_unsent_recent 的窗口 bug 可能已被顺带修复"
    )


def test_补发窗口只捞最近十分钟(conn):
    """
    C-2 的补发窗口回归测试。

    曾经的 bug:下界用 SQL 的 datetime('now', ?) 算,与 ingested_at 的 ISO 格式不同源,
    字符串比较恒为真 → 「10 分钟窗口」退化成「同一 UTC 日全部」。
    后果不是"多补几条"这么轻:_dispatch 对每条待发事件 sleep 3.5s,
    一条因 HTML 400 永远发不出去的消息会被**每 20 秒重试一次、持续一整天**,
    积到几百条时单个 tick 要跑几十分钟,正常推送全部被饿死。

    修法是把下界改成 models.iso_minutes_ago() —— 与 now_iso() 同一个生产者,
    格式必然一致。这条测试钉死它不许再退回去。
    """
    store.insert_event(conn, make_event(event_id="BUY:stale"))
    # 把 ingested_at 改成 3 小时前,并保持 models.now_iso() 的格式(T 分隔 + +00:00 偏移)
    conn.execute(
        "UPDATE fomo_events SET ingested_at = "
        "replace(datetime('now', '-3 hours'), ' ', 'T') || '+00:00' WHERE event_id = 'BUY:stale'"
    )
    assert store.load_unsent_recent(conn, minutes=10) == []

    # 窗口内的事件必须还捞得到 —— 否则"修好了"可能只是把窗口收缩到了 0
    store.insert_event(conn, make_event(event_id="BUY:fresh"))
    ids = [r["event_id"] for r in store.load_unsent_recent(conn, minutes=10)]
    assert ids == ["BUY:fresh"]


# ============================================================
# 【功能 A】徽章判定 —— 验收用例 #2 #3 #7
# ============================================================
def test_验收2_基线未就绪不打徽章(conn):
    """
    验收用例 #2:/add 后基线未就绪时发生买入 → 推送照常发出,但无徽章无共识。

    ⚠️ 基线没建好时 buy_count 必然是 0,不拦住的话**一屏全是 🌱** ——
       这个符号在用户心里当场作废,没有第二次机会。
    """
    add_user(conn, "u1", "maxpain", ready=False)
    badge, reason = store.judge_badge(conn, make_event())
    assert badge is None
    assert reason == REASON_NO_BASELINE


def test_验收2_stats_ready为0时upsert_stats直接跳过(conn):
    """
    验收用例 #2 的关键一半:该事件**不写 stats**。

    ⚠️ 不跳过的话,gap 期间写入的 buy_count>0 会让 seeding 的
       "当前持有但窗口内无买入记录 → 老仓位"判据失效(A-2 判据被顶掉),
       基线建完之后该币的下一次买入必然错标 🌱。
    """
    add_user(conn, "u1", "maxpain", ready=False)
    ev = make_event()
    store.insert_event(conn, ev)          # 事件照常落库
    store.upsert_stats(conn, ev)          # 但绝不能污染基线

    assert store.stats_row_count(conn, "u1") == 0
    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 1


def test_验收2_未在名单里的用户也不写stats(conn):
    """名单外用户(比如刚 /del 掉的)本 tick 的残留事件不能悄悄建 stats 行。"""
    ev = make_event(user_id="ghost")
    store.upsert_stats(conn, ev)
    assert store.stats_row_count(conn, "ghost") == 0


def test_基线就绪且无记录时标首次(conn):
    add_user(conn, "u1", "maxpain")
    assert store.judge_badge(conn, make_event()) == (BADGE_FIRST, REASON_LOCAL_STATS)


def test_验收3_基线窗口外的老仓位再买入是加仓不是首次(conn):
    """
    验收用例 #3:回填窗口(默认 500 条 swaps)之外的老仓位,当时持有,现在再次买入。

    ⚠️ 这是 seed_holding 存在的全部理由。没有它,所有"三年前买的、窗口内查不到"的币
       都会在下次加仓时被标 🌱 —— 而这恰恰是老玩家最常见的场景。
    """
    add_user(conn, "u1", "maxpain")
    store.seed_holding(conn, "u1", "solana", CA_TOAD)   # /add 时 balances 兜底写入

    badge, reason = store.judge_badge(conn, make_event())
    assert badge == BADGE_ADD, "窗口外的老仓位被误标成首次建仓"
    assert reason == REASON_LOCAL_STATS

    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["buy_count"] == 1
    assert st["first_buy_at"] is None, "老仓位的首次买入时间未知,必须留 NULL 而不是填 now"


def test_seed_holding不覆盖已回填的真实笔数(conn):
    """
    ⚠️ 必须是 INSERT OR IGNORE。写成 REPLACE/UPSERT 会把分页拉到的真实笔数(比如 7)
       覆盖成 1,first_buy_at 也一并丢掉。
    """
    store.upsert_seed(conn, "u1", "solana", CA_TOAD, buy_count=7, first_buy_at=TS_EARLY)
    store.seed_holding(conn, "u1", "solana", CA_TOAD)
    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["buy_count"] == 7
    assert st["first_buy_at"] == TS_EARLY


def test_upsert_seed重复回填取较大笔数与较早时间(conn):
    """seeding 可能因异常重跑。重跑不能让笔数翻倍,也不能把最早时间改晚。"""
    store.upsert_seed(conn, "u1", "solana", CA_TOAD, buy_count=3, first_buy_at=TS_MID)
    store.upsert_seed(conn, "u1", "solana", CA_TOAD, buy_count=3, first_buy_at=TS_EARLY)
    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["buy_count"] == 3, "重跑 seeding 把笔数累加了"
    assert st["first_buy_at"] == TS_EARLY


@pytest.mark.parametrize(
    ("kw", "expected_reason", "why"),
    [
        ({"event_type": EVENT_SELL}, REASON_NOT_BUY, "卖出不打徽章"),
        ({"event_type": EVENT_TRANSFER_IN}, REASON_NOT_BUY, "转入是白拿的,不是市场买入"),
        ({"side_unknown": True}, REASON_NO_SIDE, "方向不明:宁可漏标不可错标"),
        ({"token_address": None}, REASON_NO_TOKEN_KEY, "构造不出聚合键"),
        ({"network_id": None}, REASON_NO_TOKEN_KEY, "禁止用 CA 长度猜链"),
        ({"token_address": CA_WSOL}, REASON_QUOTE_TOKEN, "计价币不参与功能 A/B"),
    ],
)
def test_徽章前置门逐个命中(conn, kw, expected_reason, why):
    """
    四道前置门 + not_buy。任一命中 → badge=None → 渲染成 🟢,**绝不显示 🌱**。
    reason 必须区分开,否则线上排查时分不清"真的判过"和"数据不足"。
    """
    add_user(conn, "u1", "maxpain")
    badge, reason = store.judge_badge(conn, make_event(**kw))
    assert badge is None, why
    assert reason == expected_reason, why


def test_前置门优先级_计价币先于基线判定(conn):
    """
    门的顺序决定 reason 的取值。计价币门在基线门之前 ——
    未就绪用户买 SOL 报 no_baseline 会误导排查方向(以为是基线问题,其实是计价币)。
    """
    add_user(conn, "u1", "maxpain", ready=False)
    _, reason = store.judge_badge(conn, make_event(token_address=CA_WSOL))
    assert reason == REASON_QUOTE_TOKEN


def test_前置门优先级_非买入先于一切(conn):
    """一条方向不明的卖出应报 not_buy,而不是 no_side。"""
    add_user(conn, "u1", "maxpain")
    _, reason = store.judge_badge(conn, make_event(event_type=EVENT_SELL, side_unknown=True))
    assert reason == REASON_NOT_BUY


@pytest.mark.parametrize(
    ("event_type", "reason", "expected"),
    [
        ("BUY", REASON_LOCAL_STATS, True),
        ("BUY", "api_veto", True),          # 被 API 否决成 ADD,但仍是一笔真买入,要计数
        ("BUY", REASON_QUOTE_TOKEN, False),
        ("BUY", REASON_NO_BASELINE, False),
        ("BUY", REASON_NO_SIDE, False),
        ("BUY", REASON_NO_TOKEN_KEY, False),
        ("SELL", REASON_LOCAL_STATS, False),
        ("TRANSFER_IN", REASON_LOCAL_STATS, False),
    ],
)
def test_是否计入stats的判据(event_type, reason, expected):
    """
    ⚠️ B-8:转入/空投绝不能改 buy_count。一批空投给 5 个人就会显示"5 人买过",
       徽章打在一笔没花钱的仓位上,功能 A/B 同时失真。
    """
    assert store.should_count(make_event(event_type=event_type), reason) is expected


# ============================================================
# upsert_stats 的时间与计数语义
# ============================================================
def test_first_buy_at取MIN_乱序拉到更早的买入不会把时间改晚(conn):
    """
    A-8:后拿到更早的买入。first_buy_at 取 MIN 才不会把时间改晚 ——
    改晚之后 /who 的排序全错,而且再也无法恢复(原值已被覆盖)。
    """
    add_user(conn, "u1", "maxpain")
    store.upsert_stats(conn, make_event(event_id="BUY:1", event_ts=TS_LATE))
    store.upsert_stats(conn, make_event(event_id="BUY:2", event_ts=TS_EARLY))
    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["first_buy_at"] == TS_EARLY
    assert st["buy_count"] == 2


def test_ts_fallback的事件不写first_buy_at(conn):
    """
    兜底时间戳 = 拉取时刻,不是事件时刻。写进去会污染排序,
    而且它必然比真实时间晚 —— 一旦占住 MIN() 的位置就再也修不回来了。
    """
    add_user(conn, "u1", "maxpain")
    store.upsert_stats(conn, make_event(event_ts=now_iso(), ts_fallback=True))
    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["first_buy_at"] is None
    assert st["buy_count"] == 1, "时间戳兜底不影响计数,只影响时间"


def test_后续真实时间戳可以补上空的first_buy_at(conn):
    """先来一笔兜底时间戳、后来一笔真实时间戳时,NULL 要能被填上(COALESCE 分支)。"""
    add_user(conn, "u1", "maxpain")
    store.upsert_stats(conn, make_event(event_id="BUY:1", ts_fallback=True))
    store.upsert_stats(conn, make_event(event_id="BUY:2", event_ts=TS_MID))
    st = store.get_stats(conn, "u1", "solana", CA_TOAD)
    assert st["first_buy_at"] == TS_MID


def test_无聚合键的事件不写stats(conn):
    """没有 (network, token) 就没有主键,写进去只会得到一行垃圾数据。"""
    add_user(conn, "u1", "maxpain")
    store.upsert_stats(conn, make_event(network_id=None))
    store.upsert_stats(conn, make_event(token_address=None))
    assert store.stats_row_count(conn, "u1") == 0


def test_不同链的同名币是两个独立聚合键(conn):
    """
    B-2:Solana 的 $CATE 与 Base 的 $CATE 是两个完全不同的币。
    键里少了 network_id 就会把两条链的持有者合并计数。
    """
    add_user(conn, "u1", "maxpain")
    store.upsert_stats(conn, make_event(event_id="BUY:1", network_id="solana", token_address=CA_TOAD))
    store.upsert_stats(conn, make_event(event_id="BUY:2", network_id="base", token_address=CA_CATE))
    assert store.stats_row_count(conn, "u1") == 2


# ============================================================
# 【功能 B】共识计数 —— 验收用例 #5 #7
# ============================================================
def test_验收5_分子分母同谓词且分子必定不大于分母(conn):
    """
    验收用例 #5:名单 12 人,其中 4 人基线未就绪。

    ⚠️ 分母必须是 8(只数 active & ready)。分子若用了别的谓词(比如只看 active),
       会算出 "9/8" 这种分子大于分母的输出 —— 那一刻这个数字在用户心里当场作废。
    这里刻意给 4 个未就绪用户也塞了 stats 行(真实场景里 /del→/add 会留下这种残留),
    用来证明分子的 JOIN 确实把他们挡在外面。
    """
    for i in range(1, 9):                     # u01..u08 已就绪
        add_user(conn, f"u{i:02d}", f"user{i:02d}", ready=True)
    for i in range(9, 13):                    # u09..u12 未就绪
        add_user(conn, f"u{i:02d}", f"user{i:02d}", ready=False)

    for i in range(1, 6):                     # 5 个已就绪用户买过
        force_stats(conn, f"u{i:02d}", "solana", CA_TOAD)
    for i in range(9, 13):                    # 4 个未就绪用户也有残留 stats 行
        force_stats(conn, f"u{i:02d}", "solana", CA_TOAD)

    buyers, size = store.count_consensus(conn, make_event(user_id="u01"))
    assert size == 8, "分母口径错了:必须是 active=1 AND stats_ready=1"
    assert buyers == 5, "未就绪用户的 stats 行混进了分子"
    assert buyers <= size


def test_验收5_软删除的人不进分子也不进分母(conn):
    """
    B-6:/del 之后共识数下降是**正确行为** ——
    语义是"我**现在**关注的这批人里有几个买过"。
    """
    for i in range(1, 4):
        add_user(conn, f"u{i}", f"user{i}", ready=True)
        force_stats(conn, f"u{i}", "solana", CA_TOAD)
    assert store.count_consensus(conn, make_event(user_id="u1")) == (3, 3)

    store.remove_watch_user(conn, "u3")
    assert store.count_consensus(conn, make_event(user_id="u1")) == (2, 2)


def test_buy_count为0的行不算买过(conn):
    """
    seed 出来的 buy_count=0 行(理论上不该有,但 schema 默认值就是 0)不能算进分子,
    否则"买过"的语义会被悄悄放宽成"出现过"。
    """
    add_user(conn, "u1", "a")
    add_user(conn, "u2", "b")
    force_stats(conn, "u1", "solana", CA_TOAD, buy_count=1)
    force_stats(conn, "u2", "solana", CA_TOAD, buy_count=0)
    assert store.count_consensus(conn, make_event(user_id="u1")) == (1, 2)


def test_验收7_计价币不算共识(conn):
    """
    验收用例 #7:swap 另一侧是 SOL。

    ⚠️ 不排除的话,名单里每个人都"买过 SOL",共识数恒等于名单人数,
       功能 B 整体从信号变成噪音。
    """
    add_user(conn, "u1", "maxpain")
    assert store.count_consensus(conn, make_event(token_address=CA_WSOL)) == (None, None)


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"network_id": None}, "无聚合键"),
        ({"token_address": None}, "无聚合键"),
        ({"side_unknown": True}, "方向不明"),
    ],
)
def test_数据不足时共识行整段消失(conn, kw, why):
    """(None, None) 让 formatter 整行跳过。宁可少一行,也不能给一个错的数字。"""
    add_user(conn, "u1", "maxpain")
    assert store.count_consensus(conn, make_event(**kw)) == (None, None), why


def test_基线未就绪的人自己的事件不显示共识(conn):
    """他本人不在分母里,却给他看一个分母,数字自相矛盾。"""
    add_user(conn, "u1", "maxpain", ready=False)
    assert store.count_consensus(conn, make_event(user_id="u1")) == (None, None)


def test_共识在stats上聚合而不是在事件上聚合(conn):
    """
    ⚠️ 铁律 4:必须在 user_token_stats 上聚合。
       在 fomo_events 上聚合的话,一个人的 4 条拆单会被算成 4 个人。
    """
    add_user(conn, "u1", "maxpain")
    for i in range(4):
        ev = make_event(event_id=f"BUY:h:s{i}")
        store.insert_event(conn, ev)
        store.upsert_stats(conn, ev)
    assert store.count_consensus(conn, make_event())[0] == 1


def test_买家名单按最早买入时间排序且时间未知的排最后(conn):
    """/who <CA>:first_buy_at 为 NULL 的老仓位不能排到最前面冒充"最早买家"。"""
    for i in range(1, 4):
        add_user(conn, f"u{i}", f"user{i}")
    force_stats(conn, "u1", "solana", CA_TOAD, first_buy_at=TS_MID)
    force_stats(conn, "u2", "solana", CA_TOAD, first_buy_at=TS_EARLY)
    force_stats(conn, "u3", "solana", CA_TOAD, first_buy_at=None)
    assert [r["handle"] for r in store.list_buyers(conn, "solana", CA_TOAD)] == ["user2", "user1", "user3"]


# ============================================================
# 事务
# ============================================================
def test_事务内抛异常后数据不落库(conn):
    """
    ⚠️ C-1:"事件插入 + stats 更新"必须原子。崩在两者之间会让该事件永远被
       INSERT OR IGNORE 跳过、stats 永远不更新 —— 后续必然错标 🌱,而且无法自愈。
    """
    add_user(conn, "u1", "maxpain")
    ev = make_event(event_id="BUY:boom")

    with pytest.raises(RuntimeError):
        with store.tx(conn):
            store.insert_event(conn, ev)
            store.upsert_stats(conn, ev)
            raise RuntimeError("模拟落库中途崩溃")

    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 0
    assert store.stats_row_count(conn, "u1") == 0


def test_事务正常结束时数据提交(conn):
    add_user(conn, "u1", "maxpain")
    with store.tx(conn):
        store.insert_event(conn, make_event(event_id="BUY:ok"))
    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 1


def test_回滚后连接仍可继续使用(conn):
    """ROLLBACK 之后没把事务状态清干净的话,下一个 tick 的 BEGIN IMMEDIATE 会直接报错。"""
    with pytest.raises(RuntimeError):
        with store.tx(conn):
            raise RuntimeError("boom")
    with store.tx(conn):
        store.insert_event(conn, make_event(event_id="BUY:after"))
    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 1


# ============================================================
# 表结构
# ============================================================
def test_建表幂等(conn):
    """init_db 每次启动都会跑,不幂等就没法重启。"""
    store.init_db(conn)
    store.init_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"watch_users", "fomo_events", "fomo_cursors", "user_token_stats"} <= names


def test_stats主键三元组唯一(conn):
    """
    主键 (user_id, network_id, token_address) 是"共识不会把一个人算两次"的**结构性保证**,
    不是靠 SQL 里写 DISTINCT 兜的。
    """
    force_stats(conn, "u1", "solana", CA_TOAD)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO user_token_stats "
            "(user_id, network_id, token_address, buy_count, updated_at) VALUES (?,?,?,1,'x')",
            ("u1", "solana", CA_TOAD),
        )


# ============================================================
# 特别关注(纯展示开关)
# ============================================================
def test_特别关注开关(conn):
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    assert store.starred_user_ids(conn) == set()

    ok, msg = store.set_starred(conn, "alice", True)       # handle 大小写不敏感
    assert ok and "⭐" in msg
    assert store.starred_user_ids(conn) == {"u1"}

    ok, msg = store.set_starred(conn, "alice", True)       # 幂等
    assert not ok and "已经" in msg

    ok, _ = store.set_starred(conn, "u1", False)           # 也认 user_id
    assert ok and store.starred_user_ids(conn) == set()


def test_给名单外的人加星标要报错而不是静默(conn):
    """静默成功会让 /list 的星标数对不上,用户以为设好了其实没有"""
    ok, msg = store.set_starred(conn, "nobody", True)
    assert not ok and "没有" in msg


def test_移出名单的人不再算特别关注(conn):
    """软删除后 starred 位还在,但 starred_user_ids 只认 active —— 否则数字对不上"""
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    store.set_starred(conn, "alice", True)
    store.remove_watch_user(conn, "alice")
    assert store.starred_user_ids(conn) == set()


def test_老库升级后自动补starred列(conn):
    """CREATE TABLE IF NOT EXISTS 不会给已存在的表补列,必须靠 _migrate"""
    conn.execute("ALTER TABLE watch_users DROP COLUMN starred")
    cols = lambda: {r["name"] for r in conn.execute("PRAGMA table_info(watch_users)")}  # noqa: E731
    assert "starred" not in cols()
    store.init_db(conn)
    assert "starred" in cols()
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    assert store.starred_user_ids(conn) == set()
