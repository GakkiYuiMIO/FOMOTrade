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
    EVENT_BUY,
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
    CA_USDC,
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


def test_前置门优先级_方向不明先于非买入(conn):
    """
    ⚠️ 这条断言与它替换掉的那条(「非买入先于一切」)**方向相反**,是刻意改的。

    badge_reason 是「这条记录的方向到底可不可信」唯一被落库的痕迹:
    side_unknown 自己不进表,补发时靠 poller._event_from_row 反查
    reason == 'no_side' 还原。而 poller 在转账方向判不出时会把它**兜底成
    TRANSFER_IN** —— 若 event_type 那道门排在前面,这条记录落库时 reason='not_buy',
    与一笔真·收到转入完全无法区分。后果有两个,都很实:
      1) 「N 个人收到同一个币」会把一笔可能是**转出**的记录算成"收到";
      2) 补发出去的消息会把它渲染成「📥 收到转入」,而它可能正好是反的。

    对存量数据零影响:judge_badge 只在落库那一刻跑一次、历史行永不重算,
    而所有按 badge_reason 过滤的 SQL 都同时带 event_type='BUY'。
    """
    add_user(conn, "u1", "maxpain")
    _, reason = store.judge_badge(conn, make_event(event_type=EVENT_SELL, side_unknown=True))
    assert reason == "no_side"

    # 真·收到转入(方向确定)仍然报 not_buy —— 两者必须泾渭分明
    _, real_in = store.judge_badge(conn, make_event(event_type=EVENT_TRANSFER_IN))
    assert real_in == "not_buy"

    # 而方向判不出、被兜底成 TRANSFER_IN 的那条,报的是 no_side
    _, guessed_in = store.judge_badge(
        conn, make_event(event_type=EVENT_TRANSFER_IN, side_unknown=True))
    assert guessed_in == "no_side"
    assert guessed_in != real_in, "兜底成 TRANSFER_IN 的记录必须能与真·收到转入区分开"


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


# ============================================================
# 转入逐条推送(/tin)—— 会真的改采集频率和推送量的开关
# ============================================================
def test_转入推送标记默认关_开了才落库(conn):
    """
    默认必须是 0:这个开关一开就是"每轮多一个请求 + 逐条推送",
    悄悄默认打开等于把全名单 760 条/天喷给用户。
    """
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    assert store.transfer_watch_user_ids(conn) == set(), "新加的人默认必须是关的"

    ok, msg = store.set_transfer_watch(conn, "alice")       # handle 大小写不敏感
    assert ok and "已开启" in msg
    assert store.transfer_watch_user_ids(conn) == {"u1"}
    # 真的落到了列上,不是只活在内存里
    row = conn.execute("SELECT watch_transfer_in FROM watch_users WHERE user_id='u1'").fetchone()
    assert row["watch_transfer_in"] == 1


def test_tin是开关_再发一次就关掉(conn):
    """/tin 只有一个参数,开与关全靠它自己翻面 —— 翻不动的话用户没有别的办法关掉"""
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    ok, _ = store.set_transfer_watch(conn, "alice")
    assert ok and store.transfer_watch_user_ids(conn) == {"u1"}

    ok, msg = store.set_transfer_watch(conn, "u1")          # 也认 user_id
    assert ok and "已关闭" in msg
    assert store.transfer_watch_user_ids(conn) == set()


def test_显式设成已经是的状态要如实回执(conn):
    """幂等的那一支必须说人话:开关命令唯一的反馈就是回执"""
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True)
    ok, msg = store.set_transfer_watch(conn, "alice", True)
    assert not ok and "本来就是开" in msg


def test_给名单外的人开转入推送要给出可操作的提示(conn):
    """静默失败会让用户一直等一条永远不会来的推送"""
    ok, msg = store.set_transfer_watch(conn, "nobody")
    assert not ok
    assert "没有" in msg and "/add" in msg, f"要引导他先 /add,实际:{msg}"


def test_移出名单的人不再算转入推送(conn):
    """poller 根本不会去拉他的转账,还留在名单里只会让人数对不上"""
    store.add_watch_user(conn, "u1", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True)
    store.remove_watch_user(conn, "alice")
    assert store.transfer_watch_user_ids(conn) == set()


def test_转入推送有人数上限_超了要在开的时候就拦住(conn):
    """
    ⚠️ 上限必须在**开之前**拦:poller 那边超限只会退回轮转 + 刷日志,
       用户在 TG 里什么都看不到,还以为开成功了。
    """
    for i in range(3):
        store.add_watch_user(conn, f"u{i}", f"H{i}", f"H{i}")
        store.set_transfer_watch(conn, f"u{i}", True, max_on=3)
    assert len(store.transfer_watch_user_ids(conn)) == 3

    store.add_watch_user(conn, "u9", "H9", "H9")
    ok, msg = store.set_transfer_watch(conn, "u9", True, max_on=3)
    assert not ok and "最多" in msg
    assert store.transfer_watch_user_ids(conn) == {"u0", "u1", "u2"}, "第 4 个人绝不能被开进去"
    # 关掉一个之后就该放行 —— 上限是"同时开几个",不是"这辈子开过几个"
    store.set_transfer_watch(conn, "u0", False, max_on=3)
    ok, _ = store.set_transfer_watch(conn, "u9", True, max_on=3)
    assert ok


def test_软删除再加回来不许把转入推送顶到上限之外(conn):
    """
    ⚠️⚠️ 实测出来的绕过路径(/del 与 /add 直落这两个函数,store 层就能完整复现):
       开满 12 → /del 掉一个(软删除)→ 名额看着空出来 → 再 /tin 开第 13 个 →
       /add 把删掉那个加回来。软删除时若把标记位留着,而 add_watch_user 的
       ON CONFLICT 分支又不碰它,active 且开着的人数就变成 13。
    ⚠️ 后果不是"多推几条":poller 每轮都会撞上超限分支、**整体退回轮转**,
       这个功能的时效承诺当场作废,而 /tin 清单显示的还是「13/12 人」—— 全程零报错。
    ⚠️ 12 写死(poller 的单轮请求预算),不从被测模块 import。
    """
    for i in range(12):
        store.add_watch_user(conn, f"u{i:02d}", f"h{i:02d}", f"H{i:02d}")
        ok, msg = store.set_transfer_watch(conn, f"u{i:02d}", True, max_on=12)
        assert ok, f"前提不成立,第 {i} 个没开上:{msg}"
    store.add_watch_user(conn, "u99", "h99", "H99")
    assert not store.set_transfer_watch(conn, "u99", True, max_on=12)[0], "满员时该拦住"

    store.remove_watch_user(conn, "h00")                  # /del —— 软删除
    ok, msg = store.set_transfer_watch(conn, "u99", True, max_on=12)
    assert ok, f"名额确实空出来了,这时候该开得成:{msg}"
    store.add_watch_user(conn, "u00", "h00", "H00")       # /add —— 用户改主意,加回来

    on = store.transfer_watch_user_ids(conn)
    assert len(on) <= 12, f"上限被绕过了:active 且开着的有 {len(on)} 人 —— {sorted(on)}"
    assert "u00" not in on, \
        "回归的人必须重新 /tin:这一位会真的多花请求、多发消息,不能静默恢复"


def test_老库升级后自动补转入推送列(conn):
    """
    CREATE TABLE IF NOT EXISTS 不会给已存在的表补列。补不上的话,升级后第一条
    /tin(以及每一轮采集时的读取)直接 no such column —— 整个 tick 崩掉。
    """
    conn.execute("ALTER TABLE watch_users DROP COLUMN watch_transfer_in")
    cols = lambda: {r["name"] for r in conn.execute("PRAGMA table_info(watch_users)")}  # noqa: E731
    assert "watch_transfer_in" not in cols()
    # 老库里本来就有的人,升级后必须原样还在、且是"关"的状态
    conn.execute("INSERT INTO watch_users (user_id, handle, added_at) VALUES ('old','Bob','x')")

    store.init_db(conn)

    assert "watch_transfer_in" in cols()
    assert store.transfer_watch_user_ids(conn) == set(), "升级不能把任何人默认打开"
    assert store.get_watch_user(conn, "old") is not None, "迁移不许动老数据"
    ok, _ = store.set_transfer_watch(conn, "Bob")
    assert ok and store.transfer_watch_user_ids(conn) == {"old"}


# ============================================================
# 每人各自的转入门槛(/tin <handle> <金额>)
# ⚠️ 门槛一律**写死字面量**,不从 config / store import —— 从被测模块取一个数
#    再拿它断言,等于用被测代码给自己打分。
# ============================================================
def _min_col(conn, uid: str):
    """直接读列,绕开所有读取函数 —— 只有这样才能证明"真的落库了"而不是活在内存里"""
    return conn.execute(
        "SELECT transfer_in_min_usd FROM watch_users WHERE user_id = ?", (uid,)
    ).fetchone()["transfer_in_min_usd"]


def test_两个人的门槛各存各的_互不影响(conn):
    """
    用户原话就是这个:「人物A 我可以设置 30000,人物B 我可以设置 200」。
    一个全局值做不到 —— 名单里既有一动就六位数的巨鲸,也有几百块一笔的人。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.add_watch_user(conn, "uB", "Bob", "Bob")

    ok_a, msg_a = store.set_transfer_watch(conn, "alice", True, min_usd=30000.0)
    ok_b, msg_b = store.set_transfer_watch(conn, "bob", True, min_usd=200.0)

    assert ok_a and ok_b, (msg_a, msg_b)
    assert _min_col(conn, "uA") == 30000.0, f"A 的门槛没落库:{_min_col(conn, 'uA')}"
    assert _min_col(conn, "uB") == 200.0, "B 的门槛被 A 覆盖了 —— 那就还是全局值"
    assert store.transfer_watch_user_ids(conn) == {"uA", "uB"}, "门槛不该影响开关本身"


def test_门槛零要真的落成零_不能被真值判断吃掉(conn):
    """
    ⚠️ 铁律:0 是有意义的真实值。「这个人的转入全推」用 0 表达,
       写成 `min_usd or None` / `if min_usd:` 会把它悄悄变成 NULL(= 跟着全局 $100 走),
       用户以为设成了全推,实际每天少收 7 条。
    ⚠️ 必须同时钉死 **列是 0.0 而不是 NULL** 和 **解析结果是 0.0 而不是全局默认**:
       只测一个的话另一处被真值判断吃掉仍然全绿。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    ok, msg = store.set_transfer_watch(conn, "alice", True, min_usd=0.0)

    assert ok, msg
    got = _min_col(conn, "uA")
    assert got is not None, "门槛 0 被当成「没设过」写成了 NULL —— 那就是回到全局门槛了"
    assert got == 0.0
    assert store.resolve_transfer_min_usd(got, 100.0) == 0.0, \
        "0 被真值判断吃掉、落回了全局默认 100"


def test_没单独设过门槛的人是NULL_判定时落回全局默认(conn):
    """NULL 才是「跟着全局走」。写成 0 的话这个人立刻变成全推(8.1 条/天)"""
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True)

    assert _min_col(conn, "uA") is None, "没设过门槛的人不许被写上任何数"
    assert store.resolve_transfer_min_usd(None, 100.0) == 100.0
    assert store.resolve_transfer_min_usd(None, 5000.0) == 5000.0, \
        "NULL 必须**跟着**全局值走,而不是被冻结成某个数"


def test_带金额时已经开着的人只改门槛_绝不被关掉(conn):
    """
    ⚠️ 语法上的关键决定:带金额 = 只开不切。
       写成"带金额也切换"的话,「已开在 $30000、再发 /tin alice 200」既能读成关掉
       也能读成改门槛,用户无从预期 —— 而关掉的后果是从此一条都收不到。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True, min_usd=30000.0)

    ok, msg = store.set_transfer_watch(conn, "alice", True, min_usd=200.0)

    assert ok, msg
    assert store.transfer_watch_user_ids(conn) == {"uA"}, f"人被关掉了:{msg}"
    assert _min_col(conn, "uA") == 200.0
    assert "200" in msg and "30,000" in msg, f"回执要说清「现在是多少、之前是多少」:{msg}"


def test_不带金额仍然是开关_而且不动已经设好的门槛(conn):
    """
    不带金额时若也当成"设置",就再也没有任何写法能关掉了。
    ⚠️ 同时钉死:切换开关**不许顺手把用户设过的门槛清掉** —— 他没下过这个指令。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True, min_usd=30000.0)

    ok, off = store.set_transfer_watch(conn, "alice")
    assert ok and "已关闭" in off, off
    assert store.transfer_watch_user_ids(conn) == set()
    assert _min_col(conn, "uA") == 30000.0, "关掉时把门槛清了 —— 下次打开推送量会静默涨回去"
    assert "30,000" in off, f"「门槛留着」必须写进回执,否则就是暗的:{off}"

    ok, on = store.set_transfer_watch(conn, "alice")
    assert ok and "已开启" in on, on
    assert _min_col(conn, "uA") == 30000.0, "重新打开必须还是他自己设的那个数"
    assert "30,000" in on, f"重新打开的回执要写清现在按多少判:{on}"


def test_门槛可以清回跟随全局(conn):
    """
    显式设过之后必须有路回到「跟着 .env 变」—— 否则那是个单向的死角。
    ⚠️ min_usd=None 是**合法取值**(清回 NULL),不是"没传"。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True, min_usd=30000.0)

    ok, msg = store.set_transfer_watch(conn, "alice", True, min_usd=None,
                                       default_min_usd=100.0)

    assert ok, msg
    assert _min_col(conn, "uA") is None, "没清成 NULL 就还是被冻结在某个数上"
    assert "默认" in msg, f"回执要说清它现在跟着全局走:{msg}"


def test_清单文案要分得出默认与显式设成同一个值(conn):
    """
    ⚠️⚠️ 两者语义**不同**:跟着全局的那个会随 .env 里的
       FOMO_TRANSFER_WATCH_MIN_USD 一起变,显式设成 100 的那个不会。
       都印成 `$100.00` 的话,用户没法判断改 .env 会不会影响到这个人。
    """
    followed = store.fmt_transfer_min_usd(None, 100.0)
    explicit = store.fmt_transfer_min_usd(100.0, 100.0)

    assert "100.00" in followed and "100.00" in explicit, (followed, explicit)
    assert followed != explicit, \
        f"「跟着全局」与「显式设成同一个数」印成了同一行字:{followed}"
    assert "默认" in followed and "默认" not in explicit, (followed, explicit)


def test_人数上限拦下来时门槛也不许写进去(conn):
    """半开半设的状态比"什么都没发生"更难解释:清单上看不到他,库里却留着一个数"""
    for i in range(3):
        store.add_watch_user(conn, f"u{i}", f"H{i}", f"H{i}")
        store.set_transfer_watch(conn, f"u{i}", True, max_on=3)
    store.add_watch_user(conn, "u9", "H9", "H9")

    ok, msg = store.set_transfer_watch(conn, "u9", True, min_usd=30000.0, max_on=3)

    assert not ok and "最多" in msg, msg
    assert _min_col(conn, "u9") is None, "被上限拦住的人不许留下半截设置"


def test_软删除清开关但留门槛_两条相反的结论出自同一条原则(conn):
    """
    ⚠️⚠️ 这两列在 /del 时的处置**刻意相反**,不是漏改:
      - watch_transfer_in 清零:留着会绕过人数上限,而且 /add 回归的那一刻采集频率
        与推送量会在用户没下指令的情况下自己变回去(既有决定,见 remove_watch_user)。
      - transfer_in_min_usd 留着:开关关着时它完全不参与判定,既不花请求也不发消息;
        清掉它才是那种静默变化 —— 回归后推送量会从 $30000 的量级涨回全局 $100。
    同一条原则(往推送变多的方向永远不许静默发生)作用在语义不同的两列上,
    所以结论相反。改任何一边之前先读懂这条。
    """
    store.add_watch_user(conn, "uA", "Alice", "Alice")
    store.set_transfer_watch(conn, "alice", True, min_usd=30000.0)

    store.remove_watch_user(conn, "alice")               # /del —— 软删除

    assert store.transfer_watch_user_ids(conn) == set(), "开关必须清零(既有决定)"
    assert _min_col(conn, "uA") == 30000.0, \
        "门槛被一并清了 —— 回归后推送量会静默涨回全局默认,而用户没下过这个指令"

    store.add_watch_user(conn, "uA", "Alice", "Alice")   # /add —— 加回来
    ok, msg = store.set_transfer_watch(conn, "alice")    # 必须重新 /tin(开关那条规矩)
    assert ok and "已开启" in msg, msg
    assert "30,000" in msg, f"重新打开的回执要写清现在按多少判:{msg}"


def test_老库升级补门槛列_已经开着的那个人行为逐字节不变(conn):
    """
    ⚠️⚠️ 生产库现在**真的有一个人**开着 watch_transfer_in=1。补列写成
       `NOT NULL DEFAULT 0` 就是把他静默改成全推(1.1 → 8.1 条/天);
       写成 `DEFAULT 100` 则是把当时的全局值**冻结**进库,以后改 .env 对他不再生效。
       两种都是没人下过的指令。正确的是 NULL —— 落回全局默认 = 升级前后一模一样。
    ⚠️ 100 / 5000 都是写死的字面量,不从 config import。
    """
    conn.execute("ALTER TABLE watch_users DROP COLUMN transfer_in_min_usd")
    cols = lambda: {r["name"] for r in conn.execute("PRAGMA table_info(watch_users)")}  # noqa: E731
    assert "transfer_in_min_usd" not in cols(), "前提不成立"
    # 老库里那个已经开着的人
    conn.execute("INSERT INTO watch_users (user_id, handle, added_at, active, watch_transfer_in) "
                 "VALUES ('kitty','RoaringKittyy','x',1,1)")

    store.init_db(conn)

    assert "transfer_in_min_usd" in cols(), "补列没补上,升级后第一条 /tin 直接 no such column"
    assert store.transfer_watch_user_ids(conn) == {"kitty"}, "迁移把已经开着的人弄丢了"
    assert _min_col(conn, "kitty") is None, \
        "迁移给他写上了一个具体的数 —— 这是没人下过的指令(冻结 or 改成全推)"
    assert store.resolve_transfer_min_usd(_min_col(conn, "kitty"), 100.0) == 100.0, \
        "迁移后他的判定门槛变了 —— 行为不再与升级前相同"
    assert store.resolve_transfer_min_usd(_min_col(conn, "kitty"), 5000.0) == 5000.0, \
        "他必须仍然**跟着**全局值走,而不是被冻结在 100"


# ============================================================
# /hot 买入榜:按倍数排序 + 首买人 + 基准市值
# ============================================================
def _hot_buy(conn, uid, ca, ts, mcap=None, usd=100.0, handle=None):
    """写一条"算数"的买入。⚠️ badge_reason 必须落在 COUNTABLE_REASONS 里,否则不进榜"""
    from src.models import FomoEvent

    ev = FomoEvent(
        event_id=f"{uid}-{ca}-{ts}", event_type=EVENT_BUY, user_id=uid, event_ts=ts,
        raw_json="{}", network_id="solana", token_address=ca, token_symbol=ca.upper(),
        amount_usd=usd, market_cap=mcap, badge_reason=REASON_LOCAL_STATS,
        user_handle=handle or uid,
    )
    store.insert_event(conn, ev)


def _ready(conn, uid, handle):
    store.add_watch_user(conn, uid, handle, handle)
    store.mark_stats_ready(conn, uid)


def test_买入榜按倍数排序(conn):
    """
    榜要回答的是"名单挖到了什么金狗",涨幅才是答案 ——
    按人数排的话,一个 60 倍但只有一个人买的币会沉到看不见的地方。
    """
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    # low:两个人买、基准 100 万 → 现在 200 万 = 2x
    _hot_buy(conn, "u1", "low", "2026-08-12T01:00:00+00:00", mcap=1_000_000)
    _hot_buy(conn, "u2", "low", "2026-08-12T02:00:00+00:00", mcap=1_500_000)
    # high:只有一个人买、基准 1 万 → 现在 50 万 = 50x
    _hot_buy(conn, "u1", "high", "2026-08-12T03:00:00+00:00", mcap=10_000)
    store.upsert_token_snapshots(conn, [
        ("solana", "low", "LOW", 1.0, 2_000_000),
        ("solana", "high", "HIGH", 1.0, 500_000),
    ])

    rows = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")
    assert [r["token_address"] for r in rows] == ["high", "low"]
    assert round(rows[0]["mult"]) == 50
    assert round(rows[1]["mult"]) == 2


def test_算不出倍数的排最后而不是最前(conn):
    """
    ⚠️ SQLite 里 NULL 在 DESC 排序中会排到最后,但不能依赖它 ——
       没有基准市值的币冒到榜首,整个榜就废了。
    """
    _ready(conn, "u1", "alice")
    _hot_buy(conn, "u1", "nomcap", "2026-08-12T01:00:00+00:00", mcap=None, usd=99999.0)
    _hot_buy(conn, "u1", "small", "2026-08-12T02:00:00+00:00", mcap=100_000)
    store.upsert_token_snapshots(conn, [("solana", "small", "SMALL", 1.0, 110_000)])

    rows = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")
    assert [r["token_address"] for r in rows] == ["small", "nomcap"]
    assert rows[-1]["mult"] is None


def test_首买人取真正最早那笔(conn):
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _hot_buy(conn, "u2", "t", "2026-08-12T05:00:00+00:00", mcap=200, handle="bob")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=100, handle="alice")

    r = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")[0]
    assert r["first_ts"] == "2026-08-12T01:00:00+00:00"
    # "谁先买的"由 token_buyers 给(展示用的就是它),不再另出一个 first_buyer 列
    who = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")
    assert who[0]["who"] == "alice"


def test_基准市值跳过最早那笔的空市值(conn):
    """
    市值只来自 balances,而 balances 快照晚于 swaps 索引 ——
    "名单第一个人抢到新币"那一刻他还没出现在自己的持仓里,那一行 market_cap 就是 NULL。
    不往后找的话,🚀 倍数对**最该显示的那些币**整体消失。
    """
    _ready(conn, "u1", "alice")
    _ready(conn, "u2", "bob")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=None, handle="alice")
    _hot_buy(conn, "u2", "t", "2026-08-12T02:00:00+00:00", mcap=50_000, handle="bob")
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 1.0, 150_000)])

    r = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")[0]
    assert r["first_mcap"] == 50_000, "基准市值要跳过空值往后找"
    who = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")
    assert who[0]["who"] == "alice", "首买人仍是真正最早那个(与基准市值不同行)"
    assert round(r["mult"]) == 3


def test_买入榜不算已删除和未就绪的人(conn):
    """与 count_consensus / list_buyers 同一谓词,三处人数不能互相矛盾"""
    _ready(conn, "u1", "alice")
    store.add_watch_user(conn, "u2", "bob", "bob")          # stats_ready = 0
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=100)
    _hot_buy(conn, "u2", "t", "2026-08-12T02:00:00+00:00", mcap=100)

    r = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")[0]
    assert r["buyers"] == 1


def test_买家行给出累计买入额而不是首笔(conn):
    """
    一个人分五笔建仓,只报首笔会把他的实际投入低报成五分之一 ——
    而"谁下的注最大"正是这一行的价值。
    """
    _ready(conn, "u1", "alice")
    for i, usd in enumerate([100.0, 200.0, 700.0]):
        _hot_buy(conn, "u1", "t", f"2026-08-12T0{i+1}:00:00+00:00", mcap=1000, usd=usd)

    rows = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["usd"] == 1000.0
    assert rows[0]["buys"] == 3
    assert rows[0]["ts"] == "2026-08-12T01:00:00+00:00", "ts 是他**第一笔**的时间"


def test_买家按买入先后排不是按金额(conn):
    """🥇🥈🥉 标的是"谁先摸到",不是"谁买得多" —— 两者经常不一致"""
    _ready(conn, "u1", "early_small")
    _ready(conn, "u2", "late_whale")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=1000, usd=10.0,
             handle="early_small")
    _hot_buy(conn, "u2", "t", "2026-08-12T09:00:00+00:00", mcap=1000, usd=99999.0,
             handle="late_whale")

    rows = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")
    assert [r["who"] for r in rows] == ["early_small", "late_whale"]


def test_买家行给出各自的进场市值(conn):
    """"谁在什么位置进的"是这一行的核心 —— 先摸到的和追高的差着一个数量级"""
    _ready(conn, "u1", "early")
    _ready(conn, "u2", "late")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=40_000, handle="early")
    _hot_buy(conn, "u2", "t", "2026-08-12T09:00:00+00:00", mcap=4_000_000, handle="late")

    rows = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")
    assert [(r["who"], r["mcap"]) for r in rows] == [("early", 40_000), ("late", 4_000_000)]


def test_进场市值跳过自己那笔的空市值(conn):
    """
    市值只来自 balances,而 balances 快照晚于 swaps 索引 ——
    抢到新币的那一刻本人还没出现在自己的持仓里,那行 market_cap 就是 NULL。
    不往后找的话,恰恰是"抢得最早的人"没有进场市值可显示。
    """
    _ready(conn, "u1", "alice")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=None, handle="alice")
    _hot_buy(conn, "u1", "t", "2026-08-12T02:00:00+00:00", mcap=60_000, handle="alice")

    r = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")[0]
    assert r["mcap"] == 60_000
    assert r["ts"] == "2026-08-12T01:00:00+00:00", "ts 仍是他真正的第一笔"
    assert r["buys"] == 2


def test_全程没有市值时进场市值为空(conn):
    """拿不到就整段消失,绝不显示 $0.00 —— 那会被读成「零市值买入」"""
    _ready(conn, "u1", "alice")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=None, handle="alice")

    r = store.token_buyers(conn, "solana", "t", "2026-08-12T00:00:00+00:00")[0]
    assert r["mcap"] is None


def test_峰值市值只增不减(conn):
    """
    没有峰值的话,"$41.9K → $2.9M" 会被读成"起点→最高",
    于是一个在 $4.19M 进场的买家看着像不可能 —— 而真相是币冲到 4.19M 后回落了。
    """
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 1.0, 1_000_000)])
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 4.0, 4_000_000)])
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 2.0, 2_000_000)])  # 回落

    r = conn.execute("SELECT * FROM token_snapshot WHERE token_address='t'").fetchone()
    assert r["market_cap"] == 2_000_000, "现在市值跟着最新一次走"
    assert r["max_market_cap"] == 4_000_000, "峰值只增不减"


def test_没有市值的那轮不会把峰值抹掉(conn):
    """清仓后的币仍会被 trades 带着进来,但那时没有市值 —— 不能让它清掉已有峰值"""
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 4.0, 4_000_000)])
    store.upsert_token_snapshots(conn, [("solana", "t", "T", None, None)])

    r = conn.execute("SELECT * FROM token_snapshot WHERE token_address='t'").fetchone()
    assert r["max_market_cap"] == 4_000_000
    assert r["market_cap"] == 4_000_000


def test_迁移用历史买入记录回填峰值(conn):
    """
    已经冲高回落的币恰恰是最该看到峰值的那些,而它多半不会再冲了 ——
    不回填的话它永远没有峰值可显示。fomo_events.market_cap 天然采样了上涨过程。
    """
    _ready(conn, "u1", "alice")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=100_000)
    _hot_buy(conn, "u1", "t", "2026-08-12T02:00:00+00:00", mcap=4_000_000)   # 冲到这里
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 1.0, 900_000)])  # 现在回落到 900K
    # 模拟老库:把列清空后重跑迁移
    conn.execute("UPDATE token_snapshot SET max_market_cap = NULL")
    conn.execute("ALTER TABLE token_snapshot DROP COLUMN max_market_cap")
    store.init_db(conn)

    r = conn.execute("SELECT * FROM token_snapshot WHERE token_address='t'").fetchone()
    assert r["max_market_cap"] == 4_000_000


def test_hot_tokens带出峰值(conn):
    _ready(conn, "u1", "alice")
    _hot_buy(conn, "u1", "t", "2026-08-12T01:00:00+00:00", mcap=100_000)
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 1.0, 500_000)])
    store.upsert_token_snapshots(conn, [("solana", "t", "T", 1.0, 300_000)])

    r = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")[0]
    assert r["now_mcap"] == 300_000
    assert r["peak_mcap"] == 500_000


def test_倍数按峰值算不按现价(conn):
    """
    这个榜要回答"名单挖到了什么金狗",而金狗的价值在于它**跑出来过**多少。
    按现价排的话,一个冲到 100x 又回落到 60x 的币会排在稳在 70x 的币后面 ——
    而前者才是那次真正抓住了的机会。
    """
    _ready(conn, "u1", "alice")
    # spike:1 万进,冲到 100 万(100x)后回落到 60 万
    _hot_buy(conn, "u1", "spike", "2026-08-12T01:00:00+00:00", mcap=10_000)
    store.upsert_token_snapshots(conn, [("solana", "spike", "SPIKE", 1.0, 1_000_000)])
    store.upsert_token_snapshots(conn, [("solana", "spike", "SPIKE", 1.0, 600_000)])
    # steady:1 万进,稳在 70 万(70x)
    _hot_buy(conn, "u1", "steady", "2026-08-12T02:00:00+00:00", mcap=10_000)
    store.upsert_token_snapshots(conn, [("solana", "steady", "STEADY", 1.0, 700_000)])

    rows = store.hot_tokens(conn, "2026-08-12T00:00:00+00:00")
    assert [r["token_address"] for r in rows] == ["spike", "steady"], \
        "冲到 100x 又回落的币,应当排在稳在 70x 的前面"
    assert round(rows[0]["mult"]) == 100, "倍数取峰值,不是现价"
    assert rows[0]["now_mcap"] == 600_000, "现价照常带出来,回撤自己看得见"


# ============================================================
# 跟单配置:类型收敛与额度口径
# ============================================================
def _save_raw_copy_cfg(conn, d: dict) -> None:
    import json

    with store.tx(conn):
        store.set_state(conn, "copytrade_config", json.dumps(d))


def test_配置里的null不会让跟单静默停摆(conn):
    """
    ⚠️ daily_max 若读成 None,decide() 里的 `None > 0` 会抛 TypeError,
       被 tick 那层 try 吞掉 —— 表现是跟单**整个不工作**,
       而日志里只有一句"判定失败(不影响推送)"。这种失效方式最难查。
    """
    _save_raw_copy_cfg(conn, {"enabled": True, "daily_max": None, "min_buyers": None})
    cfg = store.load_copy_config(conn)
    assert cfg.daily_max == 10, "非 nullable 字段的 null 必须退回默认值"
    assert cfg.min_buyers == 2
    assert cfg.enabled is True, "⚠️ 单个字段坏掉不该把整份配置退回默认"


def test_不限的null要保住不能被顶成默认值(conn):
    """max_age_hours / max_entry_mcap / daily_spend_usd 的 None 是**合法值**"""
    _save_raw_copy_cfg(conn, {"enabled": True, "max_age_hours": None,
                              "max_entry_mcap": None, "daily_spend_usd": None})
    cfg = store.load_copy_config(conn)
    assert cfg.max_age_hours is None, "用户显式设的「不限」不能被默认值 24 顶掉"
    assert cfg.max_entry_mcap is None
    assert cfg.daily_spend_usd is None


def test_配置里的字符串数字能收回来(conn):
    _save_raw_copy_cfg(conn, {"enabled": True, "amount_usd": "40", "daily_max": "3"})
    cfg = store.load_copy_config(conn)
    assert cfg.amount_usd == 40.0 and isinstance(cfg.amount_usd, float)
    assert cfg.daily_max == 3


def test_状态抢占用CAS而不是先查后改(conn):
    """
    ⚠️ poller(主线程)与 bot(daemon 线程)是两条独立连接,「先 SELECT 判断、
       再 UPDATE」中间没有事务保护。抢不到的一方必须**看得出来自己没抢到**。
    """
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="pending")

    assert store.set_copy_status(conn, "solana", "ca1", "executing", expect="pending") is True
    assert store.set_copy_status(conn, "solana", "ca1", "executing", expect="pending") is False, \
        "第二次抢占必须失败 —— 否则连点两次就是买两次"
    with conn:
        st = conn.execute("SELECT status FROM copytrade_signals").fetchone()["status"]
    assert st == "executing"


def test_失败回执不能盖掉已经成交的状态(conn):
    """钱花出去了、台账写「未成交」、TG 还弹 ❌ 诱导人再点一次 —— 最坏的一种失效"""
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="pending")
    store.set_copy_status(conn, "solana", "ca1", "executing", expect="pending")
    store.set_copy_status(conn, "solana", "ca1", "filled", "已成交 $40", expect="executing")

    # 另一条路径的失败回执迟到了
    assert store.set_copy_status(conn, "solana", "ca1", "failed", "炸了",
                                 expect="executing") is False
    st = conn.execute("SELECT status FROM copytrade_signals").fetchone()["status"]
    assert st == "filled", "已成交的状态不能被迟到的失败回执盖掉"


def test_不给expect时保持原来的无条件覆盖语义(conn):
    """启动对账那类场景需要无条件改 —— 别把老调用点的行为改掉"""
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="filled")
    assert store.set_copy_status(conn, "solana", "ca1", "unknown") is True


def test_快照太旧就查不到市值(conn):
    from datetime import UTC, datetime, timedelta

    with store.tx(conn):
        conn.execute(
            "INSERT INTO token_snapshot(network_id, token_address, symbol, price_usd,"
            " market_cap, updated_at) VALUES (?,?,?,?,?,?)",
            ("solana", "ca1", "X", 1.0, 50_000.0,
             (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")))
    assert store.fresh_snapshot_mcap(conn, "solana", "ca1") is None
    assert store.fresh_snapshot_mcap(conn, "solana", "ca1", max_age_min=60 * 24) == 50_000.0


def test_启动对账_在途的不能自动判成失败(conn):
    """
    ⚠️ auto_executing 的含义是「抢占之后」—— 而点击就发生在那之后。
       自动判 failed 等于宣称「没花钱」,而钱可能已经出去了。
       只能标成 unknown 并让人去核对。
    """
    for st in ("auto_executing", "executing"):
        store.record_copy_signal(
            conn, network_id="solana", token_address=f"ca_{st}", token_symbol="X",
            buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status=st)

    rows, dropped = store.reconcile_inflight(conn)
    assert {r["token_address"] for r in rows} == {"ca_auto_executing", "ca_executing"}
    assert dropped == 0
    got = {r["token_address"]: r["status"] for r in
           conn.execute("SELECT token_address, status FROM copytrade_signals")}
    assert set(got.values()) == {"unknown"}, f"必须是 unknown 而不是 failed:{got}"


def test_启动对账_还在排队的可以判成未执行(conn):
    """worker 会先 CAS 成 auto_executing 再执行,所以停在 auto_queued 就一定没开始跑"""
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="auto_queued")
    rows, dropped = store.reconcile_inflight(conn)
    assert rows == [] and dropped == 1
    st = conn.execute("SELECT status, note FROM copytrade_signals").fetchone()
    assert st["status"] == "failed" and "未执行" in st["note"]


def test_启动对账_不碰已经终结的行(conn):
    for st in ("filled", "failed", "paper", "rejected"):
        store.record_copy_signal(
            conn, network_id="solana", token_address=f"ca_{st}", token_symbol="X",
            buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status=st)
    rows, dropped = store.reconcile_inflight(conn)
    assert rows == [] and dropped == 0
    got = {r["token_address"]: r["status"] for r in
           conn.execute("SELECT token_address, status FROM copytrade_signals")}
    assert got == {f"ca_{s}": s for s in ("filled", "failed", "paper", "rejected")}


def test_太老的待确认信号会作废(conn):
    """
    ⚠️ TG 里的按钮不会过期。三天前那条消息上的 [确认买入] 现在点下去,
       买的是今天的价、依据的是三天前的判定 —— 而这个信号的全部前提是「刚刚」。
    """
    from src.models import iso_minutes_ago

    store.record_copy_signal(
        conn, network_id="solana", token_address="old", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="pending")
    with store.tx(conn):
        conn.execute("UPDATE copytrade_signals SET triggered_at = ? WHERE token_address = 'old'",
                     (iso_minutes_ago(60 * 48),))
    store.record_copy_signal(
        conn, network_id="solana", token_address="fresh", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="pending")

    assert store.expire_stale_pending(conn, max_age_hours=24) == 1
    got = {r["token_address"]: r["status"] for r in
           conn.execute("SELECT token_address, status FROM copytrade_signals")}
    assert got == {"old": "expired", "fresh": "pending"}


def test_日报把待核对单独算一格(conn):
    """
    ⚠️ 「待核对」是**钱可能出去了但程序不知道**的那些。
       混在总数里等于没报 —— 而这是唯一需要人动手去核对的一格。
    """
    from src.models import now_iso

    day = now_iso()[:10]
    for i, (st, note) in enumerate([
        ("filled", "已成交 $40"),
        ("filled", "已点击成交,但 45s 内没读到仓位变化"),
        ("unknown", "进程重启时仍在执行中"),
        ("paper", None),
        ("failed", "浏览器起不来"),
    ]):
        store.record_copy_signal(
            conn, network_id="solana", token_address=f"ca{i}", token_symbol="X",
            buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status=st)
        if note:
            store.set_copy_status(conn, "solana", f"ca{i}", st, note)

    s = store.copy_day_summary(conn, day)
    assert s["total"] == 5
    assert s["unclear"] == 2, "没读到仓位变化的 + 结果未知的,都要算进待核对"
    # paper 和 failed 不出账;两条 filled + unknown 出账
    assert s["spent_usd"] == 120.0, f"实得 {s['spent_usd']}"


def test_日报只统计那一天(conn):
    from src.models import iso_minutes_ago, now_iso

    store.record_copy_signal(
        conn, network_id="solana", token_address="today", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="filled")
    store.record_copy_signal(
        conn, network_id="solana", token_address="old", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="filled")
    with store.tx(conn):
        conn.execute("UPDATE copytrade_signals SET triggered_at = ? WHERE token_address='old'",
                     (iso_minutes_ago(60 * 72),))
    assert store.copy_day_summary(conn, now_iso()[:10])["total"] == 1


def test_台账带出行情时间(conn):
    """/paper 靠它标注「这个 2.5x 可能是三天前的 2.5x」"""
    store.record_copy_signal(
        conn, network_id="solana", token_address="ca1", token_symbol="X",
        buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=40.0, status="filled")
    store.upsert_token_snapshots(conn, [("solana", "ca1", "X", 1.0, 5000.0)])
    r = store.copy_ledger(conn)[0]
    assert r["now_mcap"] == 5000.0
    assert r["mcap_at"], "必须带出快照时间,否则 /paper 分不出实时价和冻住的价"


def test_金额上限只数真的会出账的状态(conn):
    """纸上信号不该吃真金额度;失败单也不该占住上限(executor 的 raise 全在点击之前)"""
    for i, (st, amt) in enumerate([("paper", 40.0), ("filled", 40.0),
                                   ("failed", 40.0), ("rejected", 40.0),
                                   ("pending", 25.0)]):
        store.record_copy_signal(
            conn, network_id="solana", token_address=f"ca{i}", token_symbol="X",
            buyers=2, entry_mcap=1000.0, age_sec=60, amount_usd=amt, status=st)

    assert store.copy_spent_today(conn) == 65.0, "只该数 filled(40) + pending(25)"
    assert store.copy_taken_today(conn) == 5, "笔数口径含全部,与金额口径故意不同"


def test_名单盈亏快照按人覆盖写(conn):
    """同一个人只保留最新一条,不是每次都追加 —— 否则一天就是几百行垃圾"""
    store.save_user_pnl(conn, [
        {"user_id": "u1", "pnl_24h": 100.0, "pnl_7d": 200.0, "pnl_30d": 300.0},
        {"user_id": "u2", "pnl_24h": -50.0, "pnl_7d": None, "pnl_30d": None},
    ])
    store.save_user_pnl(conn, [
        {"user_id": "u1", "pnl_24h": 999.0, "pnl_7d": 200.0, "pnl_30d": 300.0},
    ])
    got = {r["user_id"]: r["pnl_24h"] for r in store.load_user_pnl(conn)}
    assert got == {"u1": 999.0, "u2": -50.0}


def test_盈亏为None时不写成0(conn):
    """⚠️ 0 是「不赚不亏」,None 是「拿不到」。写成 0 会污染排行"""
    store.save_user_pnl(conn, [
        {"user_id": "u1", "pnl_24h": None, "pnl_7d": None, "pnl_30d": None}])
    assert store.load_user_pnl(conn)[0]["pnl_24h"] is None


# ============================================================
# 【价格历史】(feat/price-history)
# ============================================================
def test_价格采样落库后能读到(conn):
    store.save_price_samples(conn, [
        ("solana", "ca1", "2026-08-23T00:00:00+00:00", 1.5, 1_000_000.0),
    ])
    rows = store.load_price_history(conn, "solana", "ca1", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["price_usd"] == pytest.approx(1.5)
    assert rows[0]["market_cap"] == pytest.approx(1_000_000.0)


def test_同一代币同一时刻重复采样不重复(conn):
    """⚠️ 主键 (network_id, token_address, sampled_at) 保证幂等,不是靠调用方去重"""
    ts = "2026-08-23T00:05:00+00:00"
    store.save_price_samples(conn, [("solana", "ca1", ts, 1.0, 100.0)])
    store.save_price_samples(conn, [("solana", "ca1", ts, 999.0, 999.0)])  # 同一时刻,数值不同也照样忽略
    rows = store.load_price_history(conn, "solana", "ca1", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1, "同一时刻重复采样必须被主键挡住,不能变成两行"
    assert rows[0]["price_usd"] == pytest.approx(1.0), "先到者为准(INSERT OR IGNORE)"


def test_价格与市值为None时存NULL不存0(conn):
    """⚠️ 0 是真实价格/市值,None 是「这一刻没采到」—— formatter.py 头部同一条规矩"""
    store.save_price_samples(conn, [
        ("solana", "ca1", "2026-08-23T00:00:00+00:00", None, None),
    ])
    row = store.load_price_history(conn, "solana", "ca1", "2020-01-01T00:00:00+00:00")[0]
    assert row["price_usd"] is None
    assert row["market_cap"] is None


def test_价格历史按时间升序且遵守since(conn):
    store.save_price_samples(conn, [
        ("solana", "ca1", "2026-08-23T00:10:00+00:00", 3.0, None),
        ("solana", "ca1", "2026-08-23T00:00:00+00:00", 1.0, None),
        ("solana", "ca1", "2026-08-23T00:05:00+00:00", 2.0, None),
    ])
    rows = store.load_price_history(conn, "solana", "ca1", "2026-08-23T00:01:00+00:00")
    assert [r["price_usd"] for r in rows] == [2.0, 3.0], "升序且必须排除 since 之前的点"


def test_价格历史只返回指定代币(conn):
    store.save_price_samples(conn, [
        ("solana", "ca1", "2026-08-23T00:00:00+00:00", 1.0, None),
        ("solana", "ca2", "2026-08-23T00:00:00+00:00", 2.0, None),
        ("base", "ca1", "2026-08-23T00:00:00+00:00", 3.0, None),
    ])
    rows = store.load_price_history(conn, "solana", "ca1", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["price_usd"] == pytest.approx(1.0)


def test_清理只删超过保留期的行并返回删除数(conn):
    from src.models import iso_minutes_ago

    store.save_price_samples(conn, [("solana", "old", "2020-01-01T00:00:00+00:00", 1.0, None)])
    with store.tx(conn):
        conn.execute(
            "UPDATE token_price_history SET sampled_at = ? WHERE token_address = 'old'",
            (iso_minutes_ago(60 * 24 * 10),),  # 10 天前,超出 3 天保留期
        )
    store.save_price_samples(conn, [("solana", "fresh", "2026-08-23T00:00:00+00:00", 1.0, None)])
    with store.tx(conn):
        conn.execute(
            "UPDATE token_price_history SET sampled_at = ? WHERE token_address = 'fresh'",
            (iso_minutes_ago(60),),  # 1 小时前,在保留期内
        )

    deleted = store.prune_price_history(conn, keep_days=3)
    assert deleted == 1
    left = {r["token_address"] for r in conn.execute("SELECT token_address FROM token_price_history")}
    assert left == {"fresh"}


def test_清理分批执行不会一次性超过批数上限(conn):
    """
    ⚠️ 稳态下这张表是百万行量级,单次 prune 调用必须有界 ——
       用一个小批大小把上限逻辑压到能在单测里验证:命中批数上限时
       应该只删掉 批大小×批数上限 行,剩下的留到下一次调用。
    """
    from src.models import iso_minutes_ago

    monkey_batch, monkey_max = store._PRUNE_BATCH_SIZE, store._PRUNE_MAX_BATCHES
    old_ts = iso_minutes_ago(60 * 24 * 10)
    try:
        store._PRUNE_BATCH_SIZE = 3
        store._PRUNE_MAX_BATCHES = 2
        rows = [("solana", f"ca{i}", f"2020-01-01T00:00:{i:02d}+00:00", 1.0, None)
                for i in range(10)]
        store.save_price_samples(conn, rows)
        with store.tx(conn):
            conn.execute("UPDATE token_price_history SET sampled_at = ?", (old_ts,))
            # 让 sampled_at 各不相同,避免主键冲突;10 行全部过期
            for i in range(10):
                conn.execute(
                    "UPDATE token_price_history SET sampled_at = ? WHERE token_address = ?",
                    (iso_minutes_ago(60 * 24 * 10 + i), f"ca{i}"),
                )
        deleted = store.prune_price_history(conn, keep_days=3)
        assert deleted == 6, "批大小 3 × 批数上限 2 = 6,不该一次删完 10 行"
        remaining = conn.execute("SELECT COUNT(*) n FROM token_price_history").fetchone()["n"]
        assert remaining == 4
    finally:
        store._PRUNE_BATCH_SIZE, store._PRUNE_MAX_BATCHES = monkey_batch, monkey_max


# ============================================================
# 【转入告警】N 个名单成员收到同一个币
# ============================================================
# ⚠️ 门槛数字一律**写死字面量**,不从被测模块 import ——
#    从 store 里 import 阈值再拿它去断言,等于用被测代码给自己打分:
#    默认值改了测试照样绿,而这几个数字正是这个功能的全部风险所在。
_CA_FIH2 = "547tWxWhym8U7Y7DvhGJktpkcs5eHeywvSYnhwvdpump"
_WIN = "2026-08-01T00:00:00+00:00"


def _ev_of(conn, uid, handle, *, kind, usd=1000.0, ca=_CA_FIH2, net="solana",
           ts=TS_MID, reason="not_buy", active=True, ready=True, mcap=None,
           addr=None, tag=""):
    """
    写一条『某人对这个币做了 kind』的库存。
    kind 是 BUY / SELL / TRANSFER_IN / TRANSFER_OUT 之一 —— 这几类必须能同库共存,
    「收到」的口径才可能被真的检验(见 test_买入卖出转出绝不能被算成收到)。
    """
    store.add_watch_user(conn, uid, handle, handle)
    if ready:
        store.mark_stats_ready(conn, uid)
    if not active:
        conn.execute("UPDATE watch_users SET active = 0 WHERE user_id = ?", (uid,))
    ev = make_event(
        event_type=kind, event_id=f"{kind}:{uid}-{ca[:6]}-{usd}{tag}",
        user_id=uid, handle=handle, network_id=net, token_address=ca,
        token_symbol="fih", event_ts=ts, amount_usd=usd, market_cap=mcap,
        counterparty_address=addr,
    )
    ev.badge_reason = reason
    store.insert_event(conn, ev)
    conn.execute("UPDATE fomo_events SET user_handle = ? WHERE event_id = ?",
                 (handle, ev.event_id))
    return ev


def _recv(conn, uid, handle, **kw):
    """写一条『某人收到了这个币』的库存。reason 默认取 judge_badge 对真·收到转入给出的值"""
    return _ev_of(conn, uid, handle, kind=EVENT_TRANSFER_IN, **kw)


def test_收到计数_三个人各收到一笔(conn):
    for i in range(3):
        _recv(conn, f"u{i}", f"Holder{i}")
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 3


def test_买入卖出转出绝不能被算成收到(conn):
    """
    ⚠️⚠️ 这条守的是整个功能的**立身之本**:「收到」与「买入」是相反的含义,
       分不开的话这条告警就是在把"三个人在抢这个币"和"有人在给三个人发币"混成一句话。

    ⚠️ 在这条测试之前,没有任何一条用例在**同一个币、同一个时间窗**里同时放过
       TRANSFER_IN 和 BUY/SELL/TRANSFER_OUT —— 于是 `WHERE e.event_type = ?`
       那一句从来没被真正执行过:把它改成 `WHERE (e.event_type = ? OR 1=1)`
       整套测试照样全绿。这里把六个人六种事件摆在同一个窗口里,
       口径一松就会从 3 变成 6。

    ⚠️ 六个人必须是**不同的人**:COUNT(DISTINCT user_id) 会把"同一个人又买又收"
       吸收掉,那样过滤松了也看不出来。
    """
    for i in range(3):
        _recv(conn, f"r{i}", f"Recv{i}", usd=1000.0 + i)
    # 同一个币、同一个窗口、金额同样够门槛、badge_reason 也都在允许范围内 ——
    # 唯一的区别就是 event_type
    _ev_of(conn, "b1", "Buyer1", kind="BUY", usd=5000.0, reason="local_stats")
    _ev_of(conn, "s1", "Seller1", kind="SELL", usd=5000.0, reason="not_buy")
    _ev_of(conn, "o1", "Sender1", kind="TRANSFER_OUT", usd=5000.0, reason="not_buy")

    assert conn.execute("SELECT COUNT(*) n FROM fomo_events").fetchone()["n"] == 6, \
        "前提不成立:六条事件没有都落进库,这条测试挡不住任何东西"
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 3, \
        "只有 TRANSFER_IN 算『收到』;买入/卖出/转出混进来就是把相反的含义算成同一件事"
    rows = store.transfer_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert {r["who"] for r in rows} == {"Recv0", "Recv1", "Recv2"}, \
        f"明细里混进了非收到者:{[r['who'] for r in rows]}"


def test_收到计数不得复用买入侧的可计数过滤(conn):
    """
    ⚠️⚠️ 这条测试守的是本功能**最容易静默失效**的地方。

    买入侧 count_recent_buyers 带着 `badge_reason IN COUNTABLE_REASONS`;
    而 judge_badge 对非 BUY 事件一律返回 REASON_NOT_BUY,TRANSFER_IN 永远进不了
    COUNTABLE_REASONS。照抄那一句的话,count_recent_receivers **恒返回 0 且不报错** ——
    功能整个不工作,日志里一行异常都没有,测试也全绿。

    所以这里刻意用 judge_badge 真正会给出的 reason 建库存:一旦有人把那句过滤加回去,
    下面这个断言会立刻变红。
    """
    for i in range(3):
        ev = _recv(conn, f"u{i}", f"Holder{i}")
        assert ev.badge_reason not in ("local_stats", "api_veto"), \
            "库存必须落在 COUNTABLE_REASONS 之外,否则这条测试挡不住那句过滤"
    # 再确认一次:judge_badge 对一笔真·收到转入给出的就是这个 reason
    add_user(conn, "probe", "probe")
    _, reason = store.judge_badge(conn, make_event(event_type=EVENT_TRANSFER_IN))
    assert reason == "not_buy"
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 3


def test_收到计数必须自己滤掉计价币(conn):
    """
    ⚠️ 买入侧是靠 COUNTABLE_REASONS 顺带把 quote_token 滤掉的;这里没有那一层,
       而 USDC/USDT/WETH 的内部划转量极大(名单里天天有人搬稳定币),
       不滤就是刷屏 —— 而且刷的全是零信号价值的记账动作。
    """
    for i in range(4):
        _recv(conn, f"u{i}", f"Holder{i}", ca=CA_USDC, usd=50_000.0)
    assert store.count_recent_receivers(conn, "solana", CA_USDC, _WIN, 500.0) == 0
    assert store.transfer_receivers(conn, "solana", CA_USDC, _WIN, 500.0) == []


def test_收到计数排除方向判不出的兜底记录(conn):
    """
    poller 在方向判不出时把记录**兜底成 TRANSFER_IN**(badge_reason='no_side')——
    那可能实际是一笔**转出**。算成"收到"就是在报相反的事实。
    """
    _recv(conn, "u0", "Holder0")
    _recv(conn, "u1", "Holder1")
    _recv(conn, "u2", "Holder2", reason="no_side")
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 2


def test_收到计数的金额门槛(conn):
    """
    $500 这条线是这个功能能不能上线的分水岭(实测不设门槛 138.8 次/天)。
    ⚠️ 判据是 amount_usd >= ?;金额为 NULL 的行在 SQL 里比较结果是 NULL(不成立),
       天然被排除 —— 这正是要的语义:金额未知就不能声称它达标。
    """
    _recv(conn, "u0", "Holder0", usd=901.37)
    _recv(conn, "u1", "Holder1", usd=499.99)
    _recv(conn, "u2", "Holder2", usd=None)
    _recv(conn, "u3", "Holder3", usd=500.0)
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 2
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 0.0) == 3, \
        "门槛降到 0 也只有三条有金额的行 —— 金额为 NULL 的那条永远不该被算进来"


def test_收到计数去重到人(conn):
    """一个人被连着发五笔,不构成"五个人收到"。"""
    for k in range(5):
        _recv(conn, "u0", "Holder0", usd=1000.0 + k)
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 1


def test_收到计数排除已移出名单的人(conn):
    """/del 是软删除,历史事件仍在表里。不 JOIN active=1 的话早就不看的人还在充人数。"""
    _recv(conn, "u0", "Holder0")
    _recv(conn, "u1", "Holder1")
    _recv(conn, "u2", "Holder2", active=False)
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 2


def test_收到计数不看stats_ready(conn):
    """
    ⚠️ 与买入侧刻意不同,这是个判断而不是疏忽:stats_ready 的语义是
       "这个人的历史买入基线已经回填好了",它保护的是徽章判定与共识分子分母 ——
       而"他收到过这个币"是一条与基线毫无关系的事实,一笔转账就是一笔转账。
       带上它的唯一效果是让刚 /add 进来的人在这个信号里凭空消失,
       而分发信号最该抓的恰恰是新加进来的人。
    """
    _recv(conn, "u0", "Holder0", ready=False)
    _recv(conn, "u1", "Holder1", ready=False)
    _recv(conn, "u2", "Holder2", ready=False)
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 3


def test_收到计数带时间窗(conn):
    _recv(conn, "u0", "Holder0", ts=TS_LATE)
    _recv(conn, "u1", "Holder1", ts=TS_LATE)
    _recv(conn, "u2", "Holder2", ts=TS_EARLY)
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, TS_MID, 500.0) == 2


def test_收到者明细与计数口径完全一致(conn):
    """
    消息里写着 5 人、底下只列得出 3 个名字,是最伤信任的一种不一致。
    两个函数的谓词必须逐条对齐。
    """
    _recv(conn, "u0", "Holder0", usd=901.37, mcap=198_200.0)
    _recv(conn, "u1", "Holder1", usd=100.0)          # 灰尘,两边都该排除
    _recv(conn, "u2", "Holder2", usd=2439.09, reason="no_side")   # 方向不明,两边都该排除
    _recv(conn, "u3", "Holder3", usd=912.05, active=False)        # 已移出名单
    _recv(conn, "u4", "Holder4", usd=912.05)
    n = store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0)
    rows = store.transfer_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert n == 2
    assert len(rows) == n
    assert {r["who"] for r in rows} == {"Holder0", "Holder4"}
    assert rows[0]["mcap"] == 198_200.0
    assert rows[1]["mcap"] is None, "拿不到市值就是 NULL,绝不拿别的数去凑"


def test_收到者明细不许在查询层截断(conn):
    """
    ⚠️⚠️ transfer_receivers 曾经带着 `limit: int = 12`、poller 传 10。
       于是收到者超过 10 人时,poller 拿这 10 行求和写进
       transfer_in_signals.total_usd、也渲进消息,却把它摆在
       count_recent_receivers 数出来的**全量人数**旁边 ——
       「25 人收到 · 合计 $X」里的 X 只是其中 10 个人的合计,是一句假话。
       而这个功能抓的恰恰是分发事件,超过 10 人本来就正常
       (config 里那段实测记录:一个平台级批量发放的币能有 41 人收到)。

    ⚠️ 这条盯的是"查询层一行都不许少",不是"消息里列几行" ——
       后者是展示层的事,由 formatter 截断并如实写"还有 N 人未显示"。
    ⚠️ 门槛写死 25 与那串金额的字面量和,不从 store 里取任何常量:
       把上限 import 回来当门槛,等于用被测代码给自己打分。
    """
    usd = [901.37 + i for i in range(25)]
    for i, u in enumerate(usd):
        _recv(conn, f"u{i}", f"Holder{i}", usd=u, tag=f"-{i}")

    n = store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0)
    rows = store.transfer_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert n == 25, "前提不成立:25 个人没有都落进库"
    assert len(rows) == 25, \
        f"查询层又截断了 —— 只回了 {len(rows)} 行,合计将只是其中一部分人的合计"
    assert len(rows) == n, "明细行数与计数必须恒等(一人一行,GROUP BY user_id)"
    # 合计必须是**全部 25 个人**的和,不是排在前面那几个人的
    assert round(sum(r["usd"] for r in rows), 2) == round(sum(usd), 2)
    assert {r["who"] for r in rows} == {f"Holder{i}" for i in range(25)}


def test_收到者明细的签名里不许再有limit(conn):
    """
    ⚠️ 上一条测的是行为,这条测的是**接口**:只要 limit 还是个能传的参数,
       调用方迟早会顺手传一个(上一次就是 poller 传了 10)——
       而它一传,"合计"和台账就又变成"其中几个人的合计"。
       LIMIT 是展示层的关注点,不该出现在这个查询的签名里。
    """
    import inspect

    params = inspect.signature(store.transfer_receivers).parameters
    assert "limit" not in params, \
        f"limit 又回到查询层了:{list(params)} —— 截断归 formatter,查询必须给全量"


# ---- 发货地址聚类:这条告警里唯一可证的证据 ------------------------------
_ADDR_A = "8FtY7n1ad4LvXqyw8FojCjc7aPLVyTgXXyMJPL2cZx72"   # $fih 真实报文里的发货地址
_ADDR_B = "3nQmLpZq7Rt2Vx9Kd8Hs1Wf4Yc6Ub5Ne0Ja7Mg2Pk3S"
_ADDR_C = "9zTbCwEr4Yu6Io8Pa1Sd3Fg5Hj7Kl9Zx2Cv4Bn6Mq8W"


def test_同一个发货地址发给多人时要能查出来(conn):
    """
    $fih 的真实形态:5 分 23 秒内同一个 fromAddress 发给名单里三个人
    (00:32:17 / 00:35:11 / 00:37:40)。这是数据能证明的事实,
    而「他们一分钱没花」不是 —— 报文里根本没有 userId,区分不了
    "项目方在分发"和"本人把别处买的币充进来"。
    """
    _recv(conn, "u0", "unipcs", usd=901.37, addr=_ADDR_A, ts="2026-08-26T00:32:17+00:00")
    _recv(conn, "u1", "Quanterty", usd=912.05, addr=_ADDR_A, ts="2026-08-26T00:35:11+00:00")
    _recv(conn, "u2", "PoorGoat_", usd=2439.09, addr=_ADDR_A, ts="2026-08-26T00:37:40+00:00")
    got = store.transfer_senders(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert got["known"] == 3
    assert got["distinct"] == 1
    assert got["top"]["address"] == _ADDR_A
    assert got["top"]["receivers"] == 3
    assert got["top"]["first_ts"] == "2026-08-26T00:32:17+00:00"
    assert got["top"]["last_ts"] == "2026-08-26T00:37:40+00:00"


def test_地址各不相同时不许报出聚类(conn):
    """聚类是加强证据,不是触发条件 —— 但没有聚类时绝不能编一个出来"""
    _recv(conn, "u0", "Holder0", addr=_ADDR_A)
    _recv(conn, "u1", "Holder1", addr=_ADDR_B)
    _recv(conn, "u2", "Holder2", addr=_ADDR_C)
    got = store.transfer_senders(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert got == {"known": 3, "distinct": 3, "top": None}


def test_同一个人被连发多笔不构成聚类也不拉长时间跨度(conn):
    """
    ⚠️ 一个人被同一个地址连发五笔,那是**一个人**,不是"发给了五个人";
       而且时间跨度必须按人算 —— 按笔算的话他自己的补发就能把
       「5 分 23 秒内发给 3 个人」拉成「4 小时内」,把最有力的那句话稀释掉。
    """
    _recv(conn, "u0", "Holder0", addr=_ADDR_A, ts="2026-08-26T00:32:17+00:00", usd=901.0)
    _recv(conn, "u0", "Holder0", addr=_ADDR_A, ts="2026-08-26T04:00:00+00:00", usd=902.0,
          tag="-late")
    _recv(conn, "u1", "Holder1", addr=_ADDR_A, ts="2026-08-26T00:37:40+00:00", usd=903.0)
    got = store.transfer_senders(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert got["known"] == 2, "两个人就是两个人,连发五笔也还是两个人"
    assert got["top"]["receivers"] == 2
    assert got["top"]["last_ts"] == "2026-08-26T00:37:40+00:00", \
        "时间跨度按『每人最早一笔』算,不能被同一个人的补发拉长"


def test_地址聚类的谓词必须与收到计数逐条一致(conn):
    """
    金额不够 / 方向不明 / 已移出名单 —— 这三种行在收到计数里被排除,
    在聚类里也必须被排除。否则消息里会写着"其中 5 人来自同一个地址",
    而上面只列得出 3 个名字。
    """
    _recv(conn, "u0", "Holder0", addr=_ADDR_A)
    _recv(conn, "u1", "Holder1", addr=_ADDR_A)
    _recv(conn, "u2", "Dust", addr=_ADDR_A, usd=1.0)                  # 灰尘
    _recv(conn, "u3", "Unknown", addr=_ADDR_A, reason="no_side")      # 方向不明
    _recv(conn, "u4", "Gone", addr=_ADDR_A, active=False)             # 已移出名单
    got = store.transfer_senders(conn, "solana", _CA_FIH2, _WIN, 500.0)
    assert got["known"] == 2
    assert got["top"]["receivers"] == 2
    assert store.count_recent_receivers(conn, "solana", _CA_FIH2, _WIN, 500.0) == 2


def test_没有发货地址的行不算进已知(conn):
    """
    ⚠️ 老库(counterparty_address 这一列刚补上)与上游没给地址的记录,地址都是 NULL。
       把它们算进 known 会让消息说出「这 3 人的发货地址各不相同」——
       而事实是我们**根本不知道**。把"不知道"说成"知道是否定的",与印反话同级。
    """
    _recv(conn, "u0", "Holder0")            # addr 默认 None
    _recv(conn, "u1", "Holder1")
    _recv(conn, "u2", "Holder2")
    assert store.transfer_senders(conn, "solana", _CA_FIH2, _WIN, 500.0) == \
        {"known": 0, "distinct": 0, "top": None}


def test_金额门槛没有默认值必须由调用方传(conn):
    """
    ⚠️ store 里曾经有一个 TRANSFER_MIN_USD = 500.0 的"兜底默认值",而生产路径永远传
       settings.fomo_transfer_alert_min_usd —— 同一个阈值两份写法,改了配置这边不动,
       读代码的人还以为 500 生效着。唯一真源只能有一个,所以这里的 min_usd 必须是必传的。
    """
    import pytest as _pytest

    assert not hasattr(store, "TRANSFER_MIN_USD"), \
        "阈值的第二份定义又回来了 —— 唯一真源是 config.fomo_transfer_alert_min_usd"
    for fn in (store.count_recent_receivers, store.transfer_receivers, store.transfer_senders):
        with _pytest.raises(TypeError):
            fn(conn, "solana", _CA_FIH2, _WIN)


def test_转入告警去重台账与跟单台账互不吃行(conn):
    """
    ⚠️ 两张表的主键都是 (network_id, token_address),复用**一张**的话两类信号会互相吃掉:
       跟单先占了行,转入告警就永远发不出;反过来转入告警占了行,
       这个币的跟单信号会被当成"已经跟过"而静默跳过 —— 而后者是要花钱的那一侧。
    """
    store.record_copy_signal(conn, network_id="solana", token_address=_CA_FIH2,
                             token_symbol="fih", buyers=3, entry_mcap=1.0,
                             age_sec=None, amount_usd=10.0, status="paper")
    assert store.record_transfer_in_signal(
        conn, network_id="solana", token_address=_CA_FIH2, token_symbol="fih",
        receivers=3, total_usd=4252.5) is True, "跟单台账占了行不该挡住转入告警"

    # 反向:转入告警占了行,跟单照样能记
    conn.execute("DELETE FROM copytrade_signals")
    assert store.record_copy_signal(
        conn, network_id="solana", token_address=_CA_FIH2, token_symbol="fih",
        buyers=3, entry_mcap=1.0, age_sec=None, amount_usd=10.0, status="paper") is True


def test_同一个币只记一次转入告警(conn):
    """靠主键冲突去重,不是先 SELECT 再 INSERT —— 后者在两个 tick 撞上时会推两条"""
    kw = {"network_id": "solana", "token_address": _CA_FIH2, "token_symbol": "fih",
          "receivers": 3, "total_usd": 4252.5}
    assert store.record_transfer_in_signal(conn, **kw) is True
    assert store.record_transfer_in_signal(conn, **kw) is False
    assert conn.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 1


def test_台账能退回去以便重推(conn):
    """
    ⚠️ 主键的语义是"这个币这辈子只告警一次",可它是在**推送之前**写的 ——
       一次 TG 400 就等于这个币的告警永久丢失。所以必须能退回去。
    """
    kw = {"network_id": "solana", "token_address": _CA_FIH2, "token_symbol": "fih",
          "receivers": 3, "total_usd": 4252.5}
    assert store.record_transfer_in_signal(conn, **kw) is True
    assert store.drop_transfer_in_signal(conn, "solana", _CA_FIH2) is True
    assert conn.execute("SELECT COUNT(*) n FROM transfer_in_signals").fetchone()["n"] == 0
    # 退回之后必须能重新占位,否则"退回"只是把行删了、告警照样发不出
    assert store.record_transfer_in_signal(conn, **kw) is True
    # 退一个不存在的行不该报错,也不该谎报成功
    assert store.drop_transfer_in_signal(conn, "solana", "不存在的CA") is False
