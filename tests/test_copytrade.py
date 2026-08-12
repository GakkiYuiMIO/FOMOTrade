"""
跟单判定的单测。

⚠️ 这个文件盯的是**会让人亏钱的那一类错误**:
   过滤器在最该起作用的时候静默失效。
   比如"拿不到币龄就放行" —— 而币龄恰恰是新币最容易缺的字段,
   等于筛选器专挑最该拦的那些币放过去。
"""
# ruff: noqa: N802
from __future__ import annotations

import time

import pytest

from src.copytrade import (
    SKIP_ALREADY,
    SKIP_DAILY,
    SKIP_DAILY_SPEND,
    SKIP_DISABLED,
    SKIP_MCAP,
    SKIP_NETWORK,
    SKIP_NO_AGE,
    SKIP_NO_MCAP,
    SKIP_NOT_ENOUGH,
    SKIP_TOO_OLD,
    Candidate,
    CopyConfig,
    auto_blockers,
    decide,
    pnl,
)

NOW = 1_800_000_000


def _cand(**kw) -> Candidate:
    base = {
        "network_id": "solana", "token_address": "CA1", "token_symbol": "TOAD",
        "buyers": 3, "entry_mcap": 50_000.0,
        "token_created_at": NOW - 3600,      # 1 小时的新币
        "already_taken": False, "taken_today": 0,
    }
    return Candidate(**{**base, **kw})


def _cfg(**kw) -> CopyConfig:
    return CopyConfig(**{"enabled": True, **kw})


def replace_cfg(cfg: CopyConfig, **kw) -> CopyConfig:
    from dataclasses import replace

    return replace(cfg, **kw)


def test_默认不启用():
    """⚠️ 升级一版就自己开始跟单是绝对不能发生的事"""
    assert CopyConfig().enabled is False
    assert CopyConfig().paper_only is True, "默认必须是纸上跟单"
    assert CopyConfig().auto_execute is False, "无人值守默认必须是关的"
    assert decide(_cand(), CopyConfig(), now=NOW).reason == SKIP_DISABLED


# ============================================================
# 无人值守:开关与金额闸门
# ============================================================
def test_自动执行不能被real和live顺带打开():
    """
    ⚠️ 这是 auto_execute 存在的**全部理由**。验证自动化的流程是
       /copy real → /copy live,用户会照着做。要是自动成交挂在这两个标志上,
       他走完验证流程的那一刻就变成了无人值守 —— 而他根本没打算开这个。
    """
    walked_through = _cfg(paper_only=False, dry_run_execute=False)
    assert walked_through.auto_execute is False, "走完 real+live 不能顺带把无人值守打开"


def test_开自动之前必须先有当日金额上限():
    """⚠️ 有人盯着时「不限」可以;没人盯着时不行 —— daily_max 只数笔数,不数钱"""
    cfg = _cfg(paper_only=False, dry_run_execute=False, amount_usd=40.0)
    assert any("金额上限" in b for b in auto_blockers(cfg))
    assert auto_blockers(replace_cfg(cfg, daily_spend_usd=200.0)) == []


def test_上限比单笔还小要当场说明白():
    """一单都下不了却不报错 = 半夜查「为什么一单没跟」"""
    cfg = _cfg(paper_only=False, dry_run_execute=False,
               amount_usd=40.0, daily_spend_usd=10.0)
    assert any("比单笔" in b for b in auto_blockers(cfg))


def test_还在纸上或演练时不许开自动():
    assert any("纸上" in b for b in auto_blockers(_cfg(paper_only=True)))
    assert any("演练" in b for b in auto_blockers(_cfg(paper_only=False, dry_run_execute=True)))


def test_金额上限判的是这一单下完会不会超():
    """
    ⚠️ 判「现在超没超」会让最后一单把上限捅穿:
       上限 100、已花 99、单笔 40 → 放行到 139。
    """
    cfg = _cfg(amount_usd=40.0, daily_spend_usd=100.0)
    assert decide(_cand(spent_today=0.0), cfg, now=NOW).take
    assert decide(_cand(spent_today=60.0), cfg, now=NOW).take, "60+40=100 正好到顶,应放行"
    d = decide(_cand(spent_today=99.0), cfg, now=NOW)
    assert not d.take and d.reason == SKIP_DAILY_SPEND


def test_没设金额上限时不拦():
    """None = 不限。人工模式下这是合法配置(auto_blockers 才是拦它的地方)"""
    assert decide(_cand(spent_today=99999.0), _cfg(daily_spend_usd=None), now=NOW).take


def test_笔数上限和金额上限口径互不干扰():
    """纸上信号不该吃掉真金额度,失败单也不该占住金额上限"""
    cfg = _cfg(daily_max=10, amount_usd=40.0, daily_spend_usd=100.0)
    assert decide(_cand(taken_today=9, spent_today=0.0), cfg, now=NOW).take
    assert decide(_cand(taken_today=0, spent_today=80.0), cfg, now=NOW).reason == SKIP_DAILY_SPEND
    assert decide(_cand(taken_today=10, spent_today=0.0), cfg, now=NOW).reason == SKIP_DAILY


def test_人数够了才跟():
    assert decide(_cand(buyers=2), _cfg(min_buyers=2), now=NOW).take
    assert decide(_cand(buyers=1), _cfg(min_buyers=2), now=NOW).reason == SKIP_NOT_ENOUGH


def test_同一个币只跟一次():
    assert decide(_cand(already_taken=True), _cfg(), now=NOW).reason == SKIP_ALREADY


def test_币龄超上限不跟():
    old = _cand(token_created_at=NOW - 48 * 3600)
    assert decide(old, _cfg(max_age_hours=24), now=NOW).reason == SKIP_TOO_OLD
    assert decide(old, _cfg(max_age_hours=None), now=NOW).take, "不限时应当放行"


def test_拿不到币龄一律不跟():
    """
    ⚠️ 这条最要紧。币龄来自 balances,而 balances 快照晚于 swaps 索引 ——
       **最新的币最容易缺这个字段**。放行等于筛选器专挑最该拦的那些放过去。
    """
    c = _cand(token_created_at=None)
    assert decide(c, _cfg(max_age_hours=24), now=NOW).reason == SKIP_NO_AGE
    assert decide(c, _cfg(max_age_hours=None), now=NOW).take, "不设上限时才放行"


def test_入场市值超上限不跟():
    c = _cand(entry_mcap=5_000_000.0)
    assert decide(c, _cfg(max_entry_mcap=500_000), now=NOW).reason == SKIP_MCAP
    assert decide(c, _cfg(max_entry_mcap=None), now=NOW).take


def test_设了市值上限却拿不到市值时不跟():
    """同上:宁可漏一单,不可在不知道买的是什么的情况下建仓"""
    c = _cand(entry_mcap=None)
    assert decide(c, _cfg(max_entry_mcap=500_000), now=NOW).reason == SKIP_NO_MCAP


def test_未来时间戳当成不合格():
    """脏时间戳会算出负币龄,绝不能因为"负数 < 上限"就放行"""
    c = _cand(token_created_at=NOW + 86400)
    assert decide(c, _cfg(max_age_hours=24), now=NOW).reason == SKIP_TOO_OLD


def test_链白名单():
    c = _cand(network_id="bsc")
    assert decide(c, _cfg(networks=("solana",)), now=NOW).reason == SKIP_NETWORK
    assert decide(c, _cfg(networks=()), now=NOW).take, "空白名单 = 不限"


def test_每日上限():
    assert decide(_cand(taken_today=10), _cfg(daily_max=10), now=NOW).reason == SKIP_DAILY
    assert decide(_cand(taken_today=10), _cfg(daily_max=0), now=NOW).take, "0 = 不限"


def test_每日上限排在最后判():
    """
    ⚠️ 达到上限之后,如果先判它,所有币的原因都变成"已达当日上限",
       就看不出哪些币其实本来也不合格 —— 而那才是调参数时要看的。
    """
    bad = _cand(buyers=1, taken_today=99)
    assert decide(bad, _cfg(min_buyers=3, daily_max=10), now=NOW).reason == SKIP_NOT_ENOUGH


def test_纸上盈亏按市值比折算():
    assert pnl(50_000, 150_000, 100.0) == (300.0, 3.0)
    assert pnl(50_000, 25_000, 100.0) == (50.0, 0.5)


@pytest.mark.parametrize("entry,now_", [(None, 100), (100, None), (0, 100)])
def test_盈亏缺输入时不显示(entry, now_):
    """绝不显示成 0 —— 那会被读成「亏光了」"""
    assert pnl(entry, now_, 100.0) is None


def test_now_默认取当前时间():
    """不传 now 时必须走真实时间,否则线上永远按某个固定时刻判币龄"""
    c = _cand(token_created_at=int(time.time()) - 3600)
    assert decide(c, _cfg(max_age_hours=24)).take


# ============================================================
# TG 确认按钮
# ⚠️ 这条路径会**花钱**,每一条都是"点两次会不会买两次"这类问题
# ============================================================
class _Notif:
    def __init__(self):
        self.enabled = True
        self.sent: list[tuple[str, list | None]] = []
        self.answers: list[str] = []
        self.edits: list[str] = []

    def send(self, text, parse_mode="HTML", chat_id=None, buttons=None):
        self.sent.append((text, buttons))
        return True

    def answer_callback(self, cb_id, text=""):
        self.answers.append(text)
        return True

    def edit_message(self, chat_id, message_id, text):
        self.edits.append(text)
        return True

    def get_updates(self, offset=None, timeout=30):
        return []


def _bot(monkeypatch, tmp_path, notif):
    from src import store
    from src.bot import CommandBot

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cb.db")
    store.init_db()
    monkeypatch.setenv("FOMO_TELEGRAM_CHAT_ID", "999")
    b = CommandBot(client=None, notifier=notif)
    monkeypatch.setattr(type(b._settings), "admin_chat_id", property(lambda s: "999"))
    return b, store


def _pending(store, ca="GCa9TZMK9Q3VUSkh1234", sym="TOAD"):
    with store.get_conn() as c:
        store.record_copy_signal(c, network_id="solana", token_address=ca, token_symbol=sym,
                                 buyers=2, entry_mcap=50_000.0, age_sec=3600,
                                 amount_usd=50.0, status="pending")
    return f"solana:{ca[:12]}"


def test_非管理员点按钮一律拒绝(monkeypatch, tmp_path):
    """
    ⚠️ 消息可能被转发到别的群,那里的人点按钮同样会产生 callback。
       这条路径会花钱,门不能比命令层松。
    """
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    b._handle_callback({"id": "1", "data": f"buy:{key}",
                        "message": {"message_id": 1, "chat": {"id": "12345"}}})
    assert n.answers == ["无权限"]
    with store_.get_conn() as c:
        assert c.execute("SELECT status FROM copytrade_signals").fetchone()["status"] == "pending"


def test_copy_auto在条件不满足时拒绝开启且不落库(monkeypatch, tmp_path):
    """
    ⚠️ 「开完了才发现没生效」和「开完了半夜花超」是同一个失败:
       开关那一刻就得把还差什么说清楚,而且**不能把 auto 存进去**。
    """
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)

    out = b._cmd_copy("auto")
    assert "还不能开" in out
    for want in ("纸上", "金额上限"):
        assert want in out, f"该提示 {want}:{out}"
    with store_.get_conn() as c:
        assert store_.load_copy_config(c).auto_execute is False, "被拒绝时绝不能落库"


def test_copy_auto条件齐了才能开(monkeypatch, tmp_path):
    from dataclasses import replace as _replace

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    with store_.get_conn() as c:
        store_.save_copy_config(c, _replace(
            store_.load_copy_config(c), enabled=True, paper_only=False,
            dry_run_execute=False, amount_usd=40.0, daily_spend_usd=120.0))

    out = b._cmd_copy("auto")
    assert "无人值守" in out, out
    with store_.get_conn() as c:
        assert store_.load_copy_config(c).auto_execute is True


def test_面板不能把不限渲染成已限住(monkeypatch, tmp_path):
    """⚠️ 「今日 3/0 单」读起来像限住了,实际是闸门开着 —— 这是最危险的一种误读"""
    from dataclasses import replace as _replace

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    with store_.get_conn() as c:
        store_.save_copy_config(c, _replace(store_.load_copy_config(c),
                                            daily_max=0, daily_spend_usd=None))
    out = b._cmd_copy("")
    assert "/0 单" not in out, f"不限不能渲染成 x/0:{out}"
    assert "不限" in out


def test_点忽略只改状态不成交(monkeypatch, tmp_path):
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    b._handle_callback({"id": "1", "data": f"skip:{key}",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})
    with store_.get_conn() as c:
        assert c.execute("SELECT status FROM copytrade_signals").fetchone()["status"] == "rejected"
    assert n.edits and "已忽略" in n.edits[0]


def test_连点两次不会重复处理(monkeypatch, tmp_path):
    """连点、或消息被转发后两个人各点一次 —— 都必须只生效一次"""
    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    ev = {"id": "1", "data": f"skip:{key}", "message": {"message_id": 1, "chat": {"id": "999"}}}
    b._handle_callback(ev)
    b._handle_callback(ev)
    assert "已经处理过了" in n.answers[1]


def _click_buy(b, key):
    b._handle_callback({"id": "1", "data": f"buy:{key}",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})


def _status(store_):
    with store_.get_conn() as c:
        return c.execute("SELECT status FROM copytrade_signals").fetchone()["status"]


def _set_dry(store_, dry: bool):
    from dataclasses import replace as _replace
    with store_.get_conn() as c:
        store_.save_copy_config(c, _replace(store_.load_copy_config(c),
                                            paper_only=False, dry_run_execute=dry))


def test_演练模式绝不记成已成交(monkeypatch, tmp_path):
    """
    ⚠️ 演练根本没点成交按钮。记成 filled 会让 /paper 给一个
       **并不存在的仓位**算盈亏 —— 这是这个功能最坏的一种失效。
    """
    from src import bot as bot_mod
    from src.executor import BuyResult

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    _set_dry(store_, True)
    calls = []
    monkeypatch.setattr(bot_mod, "execute_buy",
                        lambda *a, **kw: calls.append(kw) or BuyResult(True, "演练通过"))
    _click_buy(b, key)

    assert calls and calls[0]["dry_run"] is True, "演练模式必须把 dry_run 传下去"
    assert _status(store_) != "filled", "演练绝不能记成已成交"
    assert "未成交" in n.edits[0]


def test_真实模式才记filled(monkeypatch, tmp_path):
    from src import bot as bot_mod
    from src.executor import BuyResult

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    _set_dry(store_, False)
    monkeypatch.setattr(bot_mod, "execute_buy", lambda *a, **kw: BuyResult(True, "已提交"))
    _click_buy(b, key)
    assert _status(store_) == "filled"


def test_执行器抛异常时记failed而不是filled(monkeypatch, tmp_path):
    """浏览器起不来、页面改版、余额不足 —— 一律当成没有成交"""
    from src import bot as bot_mod

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    _set_dry(store_, False)

    def boom(*a, **kw):
        raise RuntimeError("找不到成交按钮")

    monkeypatch.setattr(bot_mod, "execute_buy", boom)
    _click_buy(b, key)
    assert _status(store_) == "failed"
    assert "没有成交" in n.answers[0]
    assert "找不到成交按钮" in n.edits[0]


def test_执行期间再点一次不会重复下单(monkeypatch, tmp_path):
    """
    ⚠️ 抢占状态必须发生在**调用执行器之前**。否则第一次还在跑浏览器时
       第二次点进来,会看到状态仍是 pending,于是买第二单。
    """
    from src import bot as bot_mod
    from src.executor import BuyResult

    n = _Notif()
    b, store_ = _bot(monkeypatch, tmp_path, n)
    key = _pending(store_)
    _set_dry(store_, False)
    seen = []

    def slow(*a, **kw):
        seen.append(1)
        assert _status(store_) == "executing", "调用执行器之前必须已经把状态抢占掉"
        return BuyResult(True, "ok")

    monkeypatch.setattr(bot_mod, "execute_buy", slow)
    _click_buy(b, key)
    _click_buy(b, key)
    assert len(seen) == 1, "只能执行一次"


def test_找不到信号时不报错(monkeypatch, tmp_path):
    n = _Notif()
    b, _ = _bot(monkeypatch, tmp_path, n)
    b._handle_callback({"id": "1", "data": "buy:solana:deadbeef",
                        "message": {"message_id": 1, "chat": {"id": "999"}}})
    assert "找不到" in n.answers[0]


def test_callback_data不超TG的64字节上限():
    """超了 TG 直接 400,而消息会**没有按钮地发出去** —— 你以为能点,其实不能"""
    ca = "GCa9TZMK9Q3VUSkhZgX76YAQBjqQd1dPxkBnZojFpump"
    for prefix in ("buy", "skip"):
        data = f"{prefix}:solana:{ca[:12]}"
        assert len(data.encode()) <= 64, data


def test_读到成交才说已成交_否则说待核对(monkeypatch, tmp_path):
    """
    ⚠️ "点了"和"成了"是两件事。没读到仓位变化时说"已成交"会让人以为没事;
       说"失败"又会诱使人再点一次 = 买两次。措辞必须是第三种。
    """
    from src import bot as bot_mod
    from src.executor import BuyResult

    for confirmed, want in ((True, "已成交"), (False, "待核对")):
        n = _Notif()
        b, store_ = _bot(monkeypatch, tmp_path / str(confirmed), n)
        key = _pending(store_)
        _set_dry(store_, False)
        monkeypatch.setattr(bot_mod, "execute_buy",
                            lambda *a, **kw: BuyResult(True, "x", confirmed=confirmed))
        _click_buy(b, key)
        assert want in n.edits[0], f"confirmed={confirmed} 时回执应含 {want}:{n.edits[0]}"


# ============================================================
# 浏览器凭据:第二套登录态
# ============================================================
def test_profile为空时要指出cdp这条路不写它(monkeypatch, tmp_path):
    """
    ⚠️ `--login --cdp` attach 的是用户自己的 Chrome,从不写 PROFILE_DIR,
       而 README 恰恰把它推荐成「最可靠」。于是"登录成功了"和"能不能下单"
       悄悄分了岔 —— 提示里必须点名 --cdp,否则用户只会反复重登。
    """
    from src import executor as ex

    monkeypatch.setattr(ex, "PROFILE_DIR", tmp_path / "nope")
    ok, why = ex.profile_looks_present()
    assert ok is False and "profile 不存在" in why

    # 目录在、但没有 cookie —— 正是 cdp 登录之后的样子
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(ex, "PROFILE_DIR", tmp_path / "empty")
    ok, why = ex.profile_looks_present()
    assert ok is False and "--cdp" in why, f"必须点名 --cdp:{why}"


def test_profile有cookie时报出新鲜度(monkeypatch, tmp_path):
    from src import executor as ex

    ck = tmp_path / "p" / "Default" / "Network"
    ck.mkdir(parents=True)
    (ck / "Cookies").write_bytes(b"x")
    monkeypatch.setattr(ex, "PROFILE_DIR", tmp_path / "p")
    ok, why = ex.profile_looks_present()
    assert ok is True and "小时前" in why


# ============================================================
# 成交回读:从页面持仓块里解析结果
# ============================================================
_POS_TEXT = """661.89 Plumber
+$0.01
▲
0.78%
Invested
$1.90
Avg entry
$2.7M MC"""


class _FakePage:
    def __init__(self, text):
        self._text = text

    def evaluate(self, _js):
        return self._text


def test_持仓块能解析出投入数量和均价():
    from src.executor import _read_position

    p = _read_position(_FakePage(_POS_TEXT))
    assert p.invested == 1.90
    assert p.qty == 661.89
    assert p.symbol == "Plumber"
    assert p.avg_entry == "$2.7M"


def test_持仓块带千分位也能解析():
    """$1,234.56 / 45,678.90 —— 逗号不处理会解析成 1.0"""
    from src.executor import _read_position

    txt = _POS_TEXT.replace("661.89", "45,678.90").replace("$1.90", "$1,234.56")
    p = _read_position(_FakePage(txt))
    assert p.invested == 1234.56
    assert p.qty == 45678.90


def test_没持仓时返回None而不是空仓位():
    """⚠️ 返回一个 invested=None 的空对象会让"有没有仓位"这个判断变含糊"""
    from src.executor import _read_position

    assert _read_position(_FakePage(None)) is None
    assert _read_position(_FakePage("")) is None


def test_页面结构变了也不抛异常():
    """执行器已经点完了成交按钮,这时候抛异常只会让上层记成 failed —— 但钱已经出去了"""
    from src.executor import _read_position

    class _Boom:
        def evaluate(self, _js):
            raise RuntimeError("页面没了")

    assert _read_position(_Boom()) is None
    p = _read_position(_FakePage("Invested\nAvg entry"))  # 有锚点词但没数字
    assert p is not None and p.invested is None
