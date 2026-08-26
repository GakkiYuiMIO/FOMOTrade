"""
formatter.py 的单测 —— Telegram HTML 消息渲染(设计文档 §10)。

⚠️ formatter 由另一个 agent 并行实现,写这份测试时它可能还不存在。
   模块级 importorskip 保证:文件缺失时整份测试**优雅跳过**,而不是让 pytest 收集失败
   (收集失败会连带 test_models / test_store 一起报红,掩盖真正的问题)。

对应验收用例:#2(基线未就绪的 ⏳ 尾行)、#10(thesis 含 <script> 不 400)。
"""
# ruff: noqa: N802
# 测试函数名刻意用中文:pytest -v 的输出就是一份可读的验收清单。ruff 的 N802 只认 ASCII 小写。
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.models import (
    BADGE_ADD,
    BADGE_FIRST,
    EVENT_BUY,
    EVENT_SELL,
    EVENT_THESIS,
    EVENT_TRANSFER_IN,
    EVENT_TRANSFER_OUT,
)

from .conftest import CA_CATE, CA_TOAD, make_event

formatter = pytest.importorskip("src.formatter", reason="formatter.py 尚未实现")

if not hasattr(formatter, "render"):  # pragma: no cover - 契约未就绪时的保护
    pytest.skip("src.formatter 缺少 render(),跨模块契约未就绪", allow_module_level=True)

render = formatter.render


# ============================================================
# 行首 emoji 锚点(§10.1 视觉铁律)
# ============================================================
@pytest.mark.parametrize(
    ("kw", "emoji", "label"),
    [
        ({"event_type": EVENT_BUY, "badge": BADGE_FIRST}, "🌱", "首次建仓"),
        ({"event_type": EVENT_BUY, "badge": BADGE_ADD}, "🟢", "加仓"),
        ({"event_type": EVENT_SELL}, "🔴", "卖出"),
        ({"event_type": EVENT_THESIS, "thesis_text": "看好"}, "💭", "发表观点"),
        ({"event_type": EVENT_TRANSFER_IN}, "📥", "收到转入"),
        ({"event_type": EVENT_TRANSFER_OUT}, "📤", "转出"),
    ],
)
def test_标题行第一个字符是事件emoji(kw, emoji, label):
    """
    ⚠️ 全推场景一天几百条,快速滑动时人眼只稳定捕捉每条消息的**第一个字符**。
       行首被别的东西(横幅、【首次】前缀)占掉,整条扫描色带就废了。
    """
    msg = render(make_event(**kw))
    assert msg[0] == emoji, f"标题行首字符应是 {emoji},实际是 {msg[0]!r}"
    assert label in msg.splitlines()[0]


def test_首次建仓与加仓只差行首一个字符():
    """
    §10.1 的「单点替换」:🟢 → 🌱 在**同一列同一位置**,形状与颜色差异足够大,
    滑动时不需要阅读就能识别。多加横幅 / 中括号前缀都会破坏这个性质。
    """
    first = render(make_event(badge=BADGE_FIRST)).splitlines()[0]
    add = render(make_event(badge=BADGE_ADD)).splitlines()[0]
    assert first[0] == "🌱" and add[0] == "🟢"
    assert first[1:].replace("首次建仓", "加仓") == add[1:]


def test_徽章为空时渲染成加仓绝不显示首次():
    """
    ⚠️ 铁律 3:badge=None 表示"数据不足",不是"是首次"。
       宁可漏标不可错标 —— 一屏全 🌱 会让这个符号在用户心里当场作废,没有第二次机会。
    """
    msg = render(make_event(badge=None))
    assert msg[0] == "🟢"
    assert "🌱" not in msg
    assert "首次" not in msg


def test_方向不明时标题写交易且不显示徽章():
    """§九 降级矩阵:side 推断不出来时标题写「交易」。写成「买入」会给出一个可能反向的结论。"""
    msg = render(make_event(side_unknown=True, badge=BADGE_FIRST))
    assert "交易" in msg.splitlines()[0]
    assert "🌱" not in msg


# ============================================================
# 缺失即整行消失(铁律 2)
# ============================================================
def test_可选字段全缺失时对应行整行消失():
    """
    ⚠️ 打印 "None" / "N/A" / "--" 会让消息看起来像坏了。
       全推场景下这种噪音会迅速训练用户忽略整类消息。
    """
    msg = render(
        make_event(
            token_symbol=None, amount_usd=None, token_amount=None,
            holding_usd=None, avg_price=None, market_cap=None, api_trade_count=None,
        ),
        buyers=None, watchlist=None, holders=None,
    )
    for noise in ("None", "N/A", "n/a", "--", "undefined", "null"):
        assert noise not in msg, f"消息里出现了占位噪音 {noise!r}:\n{msg}"
    # 只剩标题 + 链 + CA 三行,但消息本身必须仍然可用
    assert msg.splitlines()[0].startswith("🟢")
    assert f"<code>{CA_TOAD}</code>" in msg


def test_零值是真实值不能当成缺失吞掉():
    """
    ⚠️ 判据必须是 `is None`。用 `if not x` 会把 0 一起吞掉 ——
       而「📦 剩余 $0.00」正是设计文档表达"清仓"的唯一方式(不设独立清仓事件类型)。
    """
    msg = render(make_event(event_type=EVENT_SELL, amount_usd=8355.49, holding_usd=0.0))
    assert "$0.00" in msg


def test_没有空行分组():
    """铁律 5:手机端空行照样占行高,全推场景一屏只放得下一条半消息。"""
    msg = render(
        make_event(badge=BADGE_FIRST, amount_usd=2500.0, holding_usd=2498.1, market_cap=1.914e7),
        buyers=1, watchlist=12, holders=1,
    )
    assert "\n\n" not in msg
    assert all(line.strip() for line in msg.splitlines())


# ============================================================
# 转义 —— 验收用例 #10
# ============================================================
def test_验收10_观点正文含script标签必须被转义():
    """
    验收用例 #10:thesis 正文含 `<script>` → 消息正常发出(已 escape),不 400。

    ⚠️ 这不只是稳定性问题,更是**可被投毒的攻击面**:
       任何人在 FOMO 上发一条带尖括号的观点,就能让被监控者的推送整条 400 失败,
       监控静默失效且没有任何告警。
    """
    msg = render(make_event(event_type=EVENT_THESIS, thesis_text="<script>alert(1)</script> 看好"))
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    assert "看好" in msg


@pytest.mark.parametrize("payload", ["<b>bold", "a & b", "1 < 2 > 0", '"quoted"'])
def test_用户可控文本一律转义(payload):
    """handle / symbol / 对手方名全是用户可控内容,一个裸 '<' 或 '&' 就 400。"""
    msg = render(make_event(handle=payload, token_symbol=payload))
    assert "<b>bold" not in msg
    assert "&amp;" in msg or "&" not in payload
    assert "&lt;" in msg or "<" not in payload


def test_观点正文的合法标签也被转义而不是原样透传():
    """TG 只认白名单标签,原样透传用户写的 <i> 会连带把我们自己的 <blockquote> 一起破坏。"""
    msg = render(make_event(event_type=EVENT_THESIS, thesis_text="<i>斜体</i>"))
    assert "<i>斜体</i>" not in msg
    assert "&lt;i&gt;" in msg


# ============================================================
# CA 行(§10.3 —— 中国网络下唯一 100% 可用的操作)
# ============================================================
def test_CA独占最后一行且被code包裹():
    """
    ⚠️ 点 <code> 实体 = 一键复制到剪贴板,是唯一不依赖网络的操作。
       TG 内置浏览器不走 MTProto 代理,链接在中国网络下大概率白屏 —— 链接不能是主路径。
    """
    msg = render(
        make_event(badge=BADGE_FIRST, amount_usd=2500.0, market_cap=1.914e7),
        buyers=1, watchlist=12, holders=1,
    )
    assert msg.splitlines()[-1] == f"<code>{CA_TOAD}</code>"


def test_CA绝不被截断():
    """截断了就复制不了,整条消息的实用价值归零 —— 宁可换行也不截断。"""
    msg = render(make_event(token_address=CA_TOAD))
    assert CA_TOAD in msg
    assert "…" not in msg.splitlines()[-1] and "..." not in msg.splitlines()[-1]


def test_CA行不加文字前缀也不用超链接包裹():
    """
    - 加「CA:」前缀会缩小 tap-to-copy 的命中区(命中区 = code 实体覆盖的字符范围)
    - 用 <a href> 包裹会把点击变成跳转(大概率失败),等于拿最可靠的操作换最不可靠的
    """
    last = render(make_event(token_address=CA_CATE)).splitlines()[-1]
    assert last.startswith("<code>") and last.endswith("</code>")
    assert "CA" not in last and "<a " not in last


def test_没有代币地址时不留空的code行():
    """空 <code></code> 在 TG 里是一个诡异的空白块,而且点它复制到的是空串。"""
    msg = render(make_event(token_address=None, network_id=None))
    assert "<code>" not in msg


# ============================================================
# 共识行(功能 B)
# ============================================================
def test_holders为None时仍持有段消失但买过段还在():
    """
    B-5:任一 active&ready 用户的 balances 拉取失败 → holders 整段消失,
    主指标 buyers **完全不受影响**。部分覆盖会让数字在 3 和 1 之间来回跳,比不显示糟得多。
    """
    msg = render(make_event(badge=BADGE_ADD), buyers=3, watchlist=12, holders=None)
    assert "3/12 人买过" in msg
    assert "仍持有" not in msg


def test_holders有值时补上仍持有段():
    msg = render(make_event(badge=BADGE_ADD), buyers=3, watchlist=12, holders=2)
    assert "👥 名单内 3/12 人买过 · 2 人仍持有" in msg


def test_holders为0照常显示():
    """卖出消息里的「0 人仍持有」本身就是强信号,不能被当成缺失吞掉。"""
    msg = render(make_event(event_type=EVENT_SELL), buyers=3, watchlist=12, holders=0)
    assert "0 人仍持有" in msg


@pytest.mark.parametrize(("buyers", "watchlist"), [(None, 12), (3, None), (None, None)])
def test_共识算不出来时整行消失(buyers, watchlist):
    """算不出来就一个字都不写。留半句「名单内 人买过」比不写糟得多。"""
    msg = render(make_event(), buyers=buyers, watchlist=watchlist, holders=2)
    assert "人买过" not in msg
    assert "仍持有" not in msg


def test_共识文案不做任何时效承诺():
    """
    B-7:分子分母会随 /add /del 跃迁(3/12 → 4/13)。
    写「刚刚买入」的话,下一次跃迁就把这句话变成了假话。
    """
    msg = render(make_event(), buyers=3, watchlist=12, holders=2)
    assert "买过" in msg
    assert "刚刚" not in msg


# ============================================================
# 基线未就绪 —— 验收用例 #2
# ============================================================
def test_验收2_基线未就绪时追加等待尾行():
    """
    验收用例 #2:推送照常发出,无徽章无共识,尾行 `⏳ 基线建立中`。
    ⚠️ 尾行必须在 CA 之后 —— CA 在最后一行是给复制用的,⏳ 是给人看的,
       但 ⏳ 绝不能占行首(§10.1:非事件 emoji 永不占行首)。
    """
    msg = render(make_event(badge=None), buyers=None, watchlist=None, holders=None,
                 baseline_pending=True)
    lines = msg.splitlines()
    assert "⏳" in lines[-1]
    assert "基线" in lines[-1]
    assert msg[0] == "🟢"          # 行首仍然是事件锚点,没被 ⏳ 挤掉


def test_基线就绪时没有等待尾行():
    msg = render(make_event(badge=BADGE_FIRST), buyers=1, watchlist=12, baseline_pending=False)
    assert "⏳" not in msg


# ============================================================
# 转账(B-8 / B-9)
# ============================================================
def test_转入必须标注非市场买入():
    """
    B-8:转入/空投不是买入。不标注的话,用户会把一笔白拿的仓位当成有人真金白银买了。
    """
    msg = render(make_event(event_type=EVENT_TRANSFER_IN, amount_usd=19200.0))
    assert "非市场买入" in msg
    assert "🌱" not in msg


def test_名单内部转账必须标出来():
    """B-9:不标出来用户无法辨别筹码是不是在名单内搬家 —— 那不是新增买盘。"""
    msg = render(make_event(event_type=EVENT_TRANSFER_IN, counterparty_handle="maxpain",
                            counterparty_is_watched=True))
    assert "maxpain" in msg
    assert "名单内转账" in msg


def test_转出不标非市场买入():
    """转出不存在"被误当成买入"的风险,多一行只会挤占屏幕。"""
    msg = render(make_event(event_type=EVENT_TRANSFER_OUT, amount_usd=100.0))
    assert "非市场买入" not in msg


# ============================================================
# 健壮性:render 绝不抛异常
# ============================================================
@pytest.mark.parametrize(
    "kw",
    [
        {"handle": None, "user_id": ""},
        {"amount_usd": float("nan")},
        {"amount_usd": float("inf")},
        {"token_amount": "not-a-number"},
        {"market_cap": "N/A"},
        {"token_symbol": "$$$"},
        {"event_type": "WEIRD_TYPE"},
        {"network_id": "42161"},
        {"thesis_text": "长" * 3000},
    ],
)
def test_脏数据不会让渲染抛异常(kw):
    """
    ⚠️ render 在 poller 的发送循环里被调用。一条脏数据把渲染炸掉会连带整个 tick 停摆 ——
       所有后续事件一起丢。宁可发一条降级消息。
    """
    msg = render(make_event(**kw), buyers=1, watchlist=2, holders=1)
    assert isinstance(msg, str) and msg
    assert "None" not in msg
    assert "nan" not in msg.lower() and "inf" not in msg.lower()


def test_超长观点正文被截断但CA仍然完整():
    """TG 单条上限 4096。截断必须发生在观点正文上,**绝不能截到 CA**。"""
    msg = render(make_event(event_type=EVENT_THESIS, thesis_text="观" * 3000))
    assert msg.splitlines()[-1] == f"<code>{CA_TOAD}</code>"
    assert len(msg) < 4000


# ============================================================
# 特别关注的醒目标识
# ============================================================
def test_星标不能顶掉行首的事件锚点():
    """
    ⚠️ 铁律 1:行首那个字符是聊天列表预览里唯一的扫描锚点。
       ⭐ 一旦顶到行首,所有特别关注的消息在列表预览里长得一模一样,
       买入卖出当场分不出来 —— 那正是最需要一眼看出方向的那批人。
    """
    ev = make_event(event_type=EVENT_BUY, badge=BADGE_FIRST, token_symbol="TOAD")
    msg = render(ev, starred=True)
    assert msg[0] == "🌱", f"行首必须仍是事件 emoji,实际 {msg[:4]!r}"
    assert "⭐" in msg.split("\n")[0], "星标要出现在标题行里"


def test_星标只改样式不改内容():
    """加星前后,除了 ⭐ 和币名的方括号,其余每一行必须逐字相同"""
    ev = make_event(event_type=EVENT_BUY, badge=BADGE_FIRST, token_symbol="TOAD")
    plain = render(ev, buyers=3, watchlist=10, holders=2)
    starred = render(ev, buyers=3, watchlist=10, holders=2, starred=True)
    assert plain != starred
    assert starred.replace("⭐ ", "").replace("【", "").replace("】", "") == plain


def test_未加星的消息完全不变():
    """默认参数必须与改造前逐字一致,否则等于给全部推送换了样式"""
    ev = make_event(event_type=EVENT_SELL, token_symbol="TOAD")
    assert render(ev) == render(ev, starred=False)
    assert "⭐" not in render(ev)
    assert "【" not in render(ev)


def test_星标币名照样转义():
    """样式包装绝不能绕过 escape —— 一个 '<' 就让整条 400"""
    ev = make_event(event_type=EVENT_BUY, token_symbol="<b>x")
    msg = render(ev, starred=True)
    assert "&lt;b&gt;x" in msg and "<b>x" not in msg.replace("<b>$", "")


# ============================================================
# 币龄
# ============================================================
@pytest.mark.parametrize(("age_sec", "want"), [
    (0, "1M"),                    # 刚创建也显示 1M,不显示 0M
    (59, "1M"),
    (60 * 8, "8M"),
    (3600 - 1, "59M"),
    (3600, "1H"),
    (3600 * 3 + 1800, "3H"),      # 天以内不给小数:3.5H 没有意义
    (86400 - 1, "23H"),
    (86400, "1D"),
    (86400 * 5, "5D"),
    (86400 * 30 - 1, "29D"),
    (86400 * 30, "1MO"),
    (86400 * 365 - 1, "12MO"),
    (86400 * 365, "1.0Y"),
    (86400 * 365 * 5.2, "5.2Y"),
])
def test_币龄格式(age_sec, want):
    now = 1_800_000_000
    assert formatter.fmt_token_age(now - age_sec, now=now) == want


def test_分钟用M月份用MO():
    """⚠️ 单独一个 M 在币圈语境里会被读成市值(market cap),月份必须是 MO"""
    now = 1_800_000_000
    assert formatter.fmt_token_age(now - 60 * 30, now=now).endswith("M")
    assert formatter.fmt_token_age(now - 86400 * 60, now=now).endswith("MO")


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), float("inf")])
def test_币龄脏值一律整行消失(bad):
    """宁可不显示,也不能出现「币龄 nanY」这种一眼假的东西"""
    assert formatter.fmt_token_age(bad) is None


def test_未来时间戳不显示负币龄():
    now = 1_800_000_000
    assert formatter.fmt_token_age(now + 86400, now=now) is None


def test_有币龄就出行没有就整行消失():
    import time as _t

    ev = make_event(event_type=EVENT_BUY, token_created_at=int(_t.time()) - 86400 * 5)
    assert "🕐 币龄 5D" in render(ev)
    assert "币龄" not in render(make_event(event_type=EVENT_BUY))


# ============================================================
# 转入告警:N 个名单成员「收到」了同一个币
# ============================================================
# ⚠️ 阈值/预算一律写死字面量,不从被测模块 import ——
#    从 formatter 里 import 预算再拿它去断言,等于用被测代码给自己打分。
_TG_HARD_LIMIT = 4096          # Telegram 单条消息硬上限(与 notifier 的实现无关的外部事实)
_SIG_CA = "547tWxWhym8U7Y7DvhGJktpkcs5eHeywvSYnhwvdpump"
# $fih 真实案例里三笔到账的发货地址(报文原件里那个)
_SIG_SENDER = "8FtY7n1ad4LvXqyw8FojCjc7aPLVyTgXXyMJPL2cZx72"
# 渲染时刻:最后一笔到账(00:37:40)之后整整 1 小时。写死时刻是为了让
# "多久之前到账"这一格可断言 —— 用 time.time() 的话断言只能写成模糊匹配。
_NOW_FIH = datetime(2026, 8, 26, 1, 37, 40, tzinfo=UTC).timestamp()

render_transfer_in_signal = formatter.render_transfer_in_signal


def _sig(**kw):
    base = {
        "network_id": "solana",
        "token_address": _SIG_CA,
        "token_symbol": "fih",
        "receiver_count": 3,
        # 时间取真实的 $fih 案例:00:32:17 / 00:35:11 / 00:37:40(前后 5 分 23 秒)
        "receivers": [
            {"who": "unipcs", "usd": 901.37, "mcap": 198_200.0, "hits": 1,
             "ts": "2026-08-26T00:32:17+00:00"},
            {"who": "Quanterty", "usd": 912.05, "mcap": 201_400.0, "hits": 1,
             "ts": "2026-08-26T00:35:11+00:00"},
            {"who": "PoorGoat_", "usd": 2439.09, "mcap": 203_000.0, "hits": 1,
             "ts": "2026-08-26T00:37:40+00:00"},
        ],
        "window_hours": 24,
        "buyers": ["CryptoTalkMan"],
        # 渲染时刻写死,否则"多久之前到账"那一格会随着测试运行的日期漂
        "now": _NOW_FIH,
    }
    base.update(kw)
    return render_transfer_in_signal(**base)


def _tags(msg: str) -> list[tuple[str, str]]:
    """按出现顺序抽出所有标签 —— (是否闭标签, 标签名)"""
    import re

    return re.findall(r"<(/?)([a-zA-Z]+)[^<>]*>", msg)


def _html_ok(msg: str) -> bool:
    """标签是否全部配对闭合 —— 未闭合标签让 TG 整条 400,用户什么都收不到"""
    stack: list[str] = []
    for slash, name in _tags(msg):
        if slash:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def test_转入告警一眼就能看出不是在FOMO上买的():
    """
    这条消息的第一职责:让人扫一眼**不会**读成"三个人在抢这个币"。
    「收到」= 从外部钱包转进来,与"他自己在 FOMO 上掏钱买"是两回事。
    """
    msg = _sig()
    head = msg.split("\n")[0]
    assert head.startswith("🚨"), "标题必须有醒目的行首锚点,且与买卖那六个都不重样"
    assert "收到" in head
    assert "不是在 FOMO 上买的" in msg, "必须显式否定一次,宁可啰嗦"
    # 行首锚点全局唯一:别撞上买/卖/观点/转账/跟单那几个
    assert head[0] not in "🌱🟢🔴💭📥📤🧪🛒"


def test_绝不断言收到者的意图():
    """
    ⚠️⚠️ 这条消息曾经写着「他们一分钱没花」。数据证不了这句话:
       报文里只有 fromAddress/toAddress、**没有 userId**(8404 条真实转账里
       userId 键出现 0 次),所以下面两件事在数据上一模一样 ——
         (a) 项目方/内部人在分发筹码
         (b) 本人把在 Jupiter/OKX 买的币充进 FOMO ← 这**恰恰是**花了钱的买入
       说"没花钱"就是在替别人断言意图,与刚在 /ca 修掉的假事实同级。

    ⚠️ 断言的是"这些说法**不出现**",不是"文案长什么样" —— 换个措辞不该让它变红,
       但只要有人把任何一句意图断言塞回来就必须红。
    """
    msg = _sig()
    for claim in ("没花", "一分钱", "免费", "白拿", "空投", "项目方", "内部人"):
        assert claim not in msg, f"消息里出现了数据证明不了的断言:{claim}"


def test_转入告警包含用户要的四项事实():
    """谁收到的 / 各自多少 / 什么市值 / 名单里有没有人真金白银买过"""
    msg = _sig()
    for who in ("unipcs", "Quanterty", "PoorGoat_"):
        assert who in msg
    assert "$901.37" in msg and "$2,439.09" in msg
    assert "$198.20K" in msg, "收到时的市值"
    assert "CryptoTalkMan" in msg and "真金白银" in msg
    assert msg.split("\n")[-1] == f"<code>{_SIG_CA}</code>", "CA 必须独占最后一行、纯 code"


def test_同一个发货地址发给多人时必须明确点出来():
    """
    ⚠️ 这是整条告警里**唯一可证**的证据,比"他们一分钱没花"有力得多:
       $fih 真实案例 —— 5 分 23 秒内,同一个 fromAddress 发给名单里三个人。
       三个人各自去别处买了同一个币、又在 5 分钟内先后充进 FOMO,可能;
       但同一个钱包在 5 分钟内给这三个人发货,是另一回事。
    ⚠️ 三项都要出现:几个人 / 同一个地址(截短显示)/ 多长时间窗内。
       少任何一项这句话都会退化成模糊印象。
    """
    msg = _sig(senders={
        "known": 3, "distinct": 1,
        "top": {"address": _SIG_SENDER, "receivers": 3,
                "first_ts": "2026-08-26T00:32:17+00:00",
                "last_ts": "2026-08-26T00:37:40+00:00"},
    })
    assert "同一个发货地址" in msg
    assert "3 人" in msg
    assert "8FtY7n…cZx72" in msg, "地址要截短显示(头 6 尾 5)"
    assert _SIG_SENDER not in msg, "整串 44 位地址塞进消息只会挤掉真正要看的行"
    assert "5 分 23 秒" in msg, "时间窗必须精确到可核对,「几分钟内」不算"


def test_发货地址各不相同时不许声称有聚类():
    """
    ⚠️ 聚类是**加强证据,不是触发条件**:地址各不相同照样是"N 个人同时收到同一个币",
       照样该告警 —— 但文案里一个字都不能暗示有共同发货方。
       而"三个人来自三个不同地址"本身也是有价值的信息(它把天平推向另一边),要说出来。
    """
    msg = _sig(senders={"known": 3, "distinct": 3, "top": None})
    assert "同一个发货地址" not in msg
    assert "各不相同" in msg
    assert "3 人「收到」同一个币" in msg, "没聚类不代表不告警"


def test_查不到发货地址时那一行整行消失():
    """
    ⚠️ 铁律 2:查不出来就整行消失,绝不打 N/A。
       尤其不能因为"没查到聚类"就写成「各不相同」—— 那是把"不知道"说成"知道是否定的",
       和印反话同级(老库没有 counterparty_address 这一列时正是这个场景)。
    """
    for blind in (None, {"known": 0, "distinct": 0, "top": None}):
        msg = _sig(senders=blind)
        assert "发货地址" not in msg
        assert "各不相同" not in msg
        assert "N/A" not in msg


def test_各自到账的时间必须出现():
    """
    ⚠️「5 分钟内到齐」和「散落在 20 小时里」是完全不同的信号,只报人数等于把这个差别抹平。
       这里三笔分别在渲染时刻之前 65 分 23 秒 / 62 分 29 秒 / 60 分 0 秒到账。
    """
    msg = _sig()
    assert "1 小时 5 分前" in msg, "unipcs 那笔:00:32:17,距渲染时刻 1 小时 5 分"
    assert "1 小时 0 分前" in msg or "1 小时前" in msg, "PoorGoat_ 那笔:整整 1 小时"


def test_到账时间取不到就整格消失而不是写刚刚():
    msg = _sig(receivers=[{"who": "unipcs", "usd": 901.37},
                          {"who": "Quanterty", "usd": 912.05, "ts": "看不懂的时间"}])
    assert "前" not in msg.split("\n")[3], f"实际渲染:{msg.split(chr(10))[3]}"
    assert "刚刚" not in msg and "N/A" not in msg


def test_没人真金白银买过与查不出来必须分开():
    """
    ⚠️ [] 与 None 语义完全不同:
       [] = 查过了、确实没人买 —— 这是有价值的信息(纯分发,没人跟进);
       None = 查不出来 —— 那一行必须整行消失,绝不能假装"没人买过"。
    """
    assert "还没有人" in _sig(buyers=[])
    none_msg = _sig(buyers=None)
    assert "还没有人" not in none_msg and "真金白银" not in none_msg


def test_缺失字段整格消失而不是打0():
    """铁律 2:缺失一律整格消失,绝不打 N/A / -- / 0"""
    msg = _sig(receivers=[{"who": "unipcs"}, {"who": "Quanterty"}, {"who": "PoorGoat_"}],
               token_symbol=None)
    assert "N/A" not in msg and "--" not in msg
    assert "$0" not in msg and "💎" not in msg
    assert "$None" not in msg and "None" not in msg
    assert "unipcs" in msg


def test_金额为0是真实值不该被吞掉():
    """⚠️ 判空一律 is None:0 是有意义的真实值,用真值判断会把它连同 None 一起吞掉"""
    msg = _sig(receivers=[{"who": "unipcs", "usd": 0.0}], receiver_count=1)
    assert "$0.00" in msg


def test_展示不下的收到者要如实说明():
    msg = _sig(receiver_count=41,
               receivers=[{"who": f"Holder{i}", "usd": 900.0} for i in range(10)])
    assert "还有 31 人未显示" in msg


def test_恶意handle不会撑破消息也不会切碎实体():
    """
    ⚠️ handle 与 ticker 由陌生人决定,长度不受任何天然约束。
       而 _esc 会把 ' 撑成 6 个字符 —— 一个 3000 字符的 handle 就能把消息顶破预算,
       notifier 超限时做的是**盲切**,切点落在 &#x27; 中间就是残缺实体 → 整条 400
       → 用户什么都收不到,只在日志里留一行。攻击者能自由控制长度,也就能自由挑切点。
    """
    evil = "'" * 3000 + "<script>x</script>"
    msg = _sig(
        token_symbol=evil,
        receivers=[{"who": evil, "usd": 900.0 + i, "mcap": 1e5} for i in range(10)],
        receiver_count=10,
        buyers=[evil] * 6,
    )
    assert len(msg) <= _TG_HARD_LIMIT, f"实际 {len(msg)} 字符,会被 notifier 盲切"
    assert _html_ok(msg), "标签必须全部配对闭合"
    assert "<script>" not in msg, "陌生人写的标签必须被转义成文本"
    # 每一个 & 都必须是一个**完整**实体的开头。残缺实体("&#x2" 这种)照样让整条 400
    import re
    assert re.search(r"&(?!(amp|lt|gt|quot|#x27|#39);)", msg) is None, "出现了残缺实体"
    assert msg.split("\n")[-1] == f"<code>{_SIG_CA}</code>", "锚点必须活到最后且完整闭合"


def test_收到者多到装不下时按整行砍且锚点必须活到最后():
    """
    ⚠️ 出口不变式是**本函数的职责**,不是调用方的:poller 现在只传 10 行,
       但"传多少行"是调用方的选择,而"消息发不发得出去"必须由渲染方自己保证。
       这里直接喂 60 个收到者(每个 handle 还都是最坏形态),模拟哪天有人把
       上限调大、或者换了个调用方忘了限量。

    ⚠️ 只能按**整行边界**砍。切进行内就会切碎 HTML 实体 → 整条 400 → 用户什么都收不到,
       而这活儿绝不能留给 notifier.send 去盲切,它切的是字节。
    """
    evil = "'" * 40
    msg = _sig(
        receiver_count=60,
        receivers=[{"who": f"{evil}{i}", "usd": 900.0 + i, "mcap": 1e5} for i in range(60)],
        buyers=[evil] * 6,
    )
    assert len(msg) <= _TG_HARD_LIMIT, f"实际 {len(msg)} 字符"
    assert _html_ok(msg)
    lines = msg.split("\n")
    assert lines[-1] == f"<code>{_SIG_CA}</code>", "锚点必须是最后一行且完整闭合"
    assert len(lines) < 63, "装不下的行必须真的被砍掉,而不是原样拼出去"
    import re
    assert re.search(r"&(?!(amp|lt|gt|quot|#x27|#39);)", msg) is None, "出现了残缺实体"


def test_超长CA也要收口而不是把消息撑破():
    """
    ⚠️ normalize_token_address 对非 0x/42 位的输入**原样透传**、不做长度校验。
       而 CA 是"砍无可砍时也要贴上去"的那一行 —— 不先收口的话它就是一颗炸弹。
    """
    msg = _sig(token_address="Z" * 9000)
    assert len(msg) <= _TG_HARD_LIMIT
    assert _html_ok(msg)
    assert msg.split("\n")[-1].startswith("<code>")
    assert msg.split("\n")[-1].endswith("</code>")


def test_未收录的链不出链接而不是拼一个404():
    """错的链接比没有链接更糟(§10.3)。GMGN 不支持 Monad,那个链接就该整个不出。"""
    msg = _sig(network_id="monad", token_address="0x" + "a" * 40)
    assert "gmgn.ai" not in msg
    assert "fomo.family" in msg


def test_没有链名时不出链接段():
    msg = _sig(network_id=None)
    assert "http" not in msg
    assert "🧬" not in msg
