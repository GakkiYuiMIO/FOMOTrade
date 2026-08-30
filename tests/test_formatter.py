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


# ---- 截断归展示层:合计是全量的,列出来的只是前几个 ----------------------------
def _receiver_row_count(msg: str) -> int:
    """消息里真正渲染出来的收到者行数 —— 只认行首的 👤(尾段那几行都不是这个锚点)"""
    return sum(1 for ln in msg.split("\n") if ln.startswith("👤 "))


def test_收到者二十五人时合计是全部人的而不是列出来那几行的():
    """
    ⚠️⚠️ 这条盯的是**合计陈述了一个错误的事实**:
       poller 曾经用 `transfer_receivers(..., limit=10)` 取明细、对这 10 行求和,
       却把结果摆在 count_recent_receivers 数出来的**全量人数**旁边 ——
       渲染成「25 人收到 · 合计 $X」,而 X 只是其中 10 个人的合计。
       与"已清仓的人显示 +$0.00""他们一分钱没花"同级:数字本身没错,
       它被摆的位置让它变成了假话。

    ⚠️ 现在契约反过来:调用方给**全量**,合计对全量求和,
       "列几行"由本模块决定并如实写出未显示人数。
    ⚠️ 期望值是测试自己按那串金额算的,不从 formatter import 任何常量/门槛。
    """
    usd = [901.37 + i for i in range(25)]
    msg = _sig(receiver_count=25,
               receivers=[{"who": f"Holder{i}", "usd": u} for i, u in enumerate(usd)])

    # 25 个人的真实合计:901.37 + 902.37 + … + 925.37 = 22834.25
    assert "合计 $22,834.25" in msg, \
        f"合计不是全部 25 人的(应为 ${sum(usd):,.2f}),实际那一行:" \
        f"{[ln for ln in msg.split(chr(10)) if ln.startswith('👥')]}"
    assert "25 人收到" in msg, "人数那一半必须还是全量,否则这条测试没在测两者一致"

    # 展示层照样要收口:不能真往一条 TG 消息里塞 25 行
    shown = _receiver_row_count(msg)
    assert shown == 10, f"消息里列出了 {shown} 行收到者"
    assert "Holder0" in msg and "Holder9" in msg, "列的是按到账时间排在最前面的那几个"
    assert "Holder10" not in msg and "Holder24" not in msg
    # 未显示人数必须扣掉**真正渲染出来的**行数,差一个都是在报假数
    assert "…还有 15 人未显示" in msg, \
        f"未显示人数算错了:{[ln for ln in msg.split(chr(10)) if '未显示' in ln]}"


def test_名单全员收到时尾段一行都不许被挤掉():
    """
    ⚠️ 名单当前 91 人,一次平台级批量发放就能让全员都"收到"(config 里记着实测:
       一个美股代币化的币有 41 人收到)。91 行收到者会把整条消息撑到 5000+ 字符,
       而 _fit_signal 只会**从尾巴往前砍** —— 于是最先没的恰恰是这条告警里
       最有价值的几行:发货地址证据、真金白银买过的人、链接。
       用户收到一条"91 个人收到了"然后什么都没有。
    ⚠️ 所以截断必须发生在**收到者明细这一段**,而不是靠出口那道闸去兜底。
       出口闸是"消息发得出去"的保险,保证不了"消息里还剩什么"(与 /ca 同一条教训)。
    ⚠️ 4096 是 Telegram 的硬上限(与本模块的实现无关的外部事实),不从被测模块 import。
    """
    msg = _sig(
        receiver_count=91,
        receivers=[{"who": f"Holder{i:02d}", "usd": 900.0 + i, "mcap": 1.98e5,
                    "ts": "2026-08-26T00:32:17+00:00"} for i in range(91)],
        senders={"known": 91, "distinct": 1,
                 "top": {"address": _SIG_SENDER, "receivers": 91,
                         "first_ts": "2026-08-26T00:32:17+00:00",
                         "last_ts": "2026-08-26T00:37:40+00:00"}},
    )
    lines = msg.split("\n")
    assert len(msg) <= _TG_HARD_LIMIT, f"实际 {len(msg)} 字符,会被 notifier 盲切"
    assert _html_ok(msg), "标签必须全部配对闭合"
    assert _receiver_row_count(msg) == 10, \
        f"收到者明细没有在展示层收口,列了 {_receiver_row_count(msg)} 行"
    assert "…还有 81 人未显示" in msg
    # 尾段四行:这条告警的证据与出口,一行都不能被收到者挤掉
    assert "同一个发货地址" in msg, "唯一可证的硬证据被挤掉了"
    assert "真金白银" in msg, "买家对照被挤掉了"
    assert "🧬 Solana" in msg, "链名被挤掉了"
    assert "fomo.family" in msg, "链接被挤掉了"
    assert lines[-1] == f"<code>{_SIG_CA}</code>", "锚点必须活到最后且完整闭合"


def test_长handle吃掉版面时未显示人数要把被跳过的也算进去():
    """
    ⚠️ 两道闸各管一件事:行数上限管"不塞 91 行",预算管"10 行也可能吃光字符"
       (_esc 把一个 `'` 撑成 6 个字符,handle 由陌生人决定)。
       被预算跳过的那几行**也是未显示**,拿行数上限去减就会少报 ——
       消息会说"还有 15 人未显示",而实际没显示的是 20 人。
    ⚠️ 断言方式是"消息自己对自己自洽":从渲染结果里数出真正列了几行,
       再要求那句话正好等于 25 减去它。不引用被测模块的任何常量。
    ⚠️ 这里的 ticker / handle / 买家名全取最坏形态(_esc 把 `'` 撑成 6 个字符),
       目的就是把版面挤到"10 行放不下"—— 出口不变式是**渲染方**的职责,
       调用方传多少行、传多长的名字都不该让这条消息说假话。
    """
    evil = "'" * 24                      # 转义后 144 字符/个,10 行就吃掉四千
    msg = _sig(
        token_symbol="'" * 40,
        receiver_count=25,
        receivers=[{"who": f"{evil}{i}", "usd": 900.0 + i, "mcap": 1.98e5,
                    "ts": "2026-08-26T00:32:17+00:00"} for i in range(25)],
        buyers=[evil] * 12,
    )
    shown = _receiver_row_count(msg)
    assert 0 < shown < 10, f"前提不成立:预算没有真的把行挤掉(shown={shown})"
    assert f"…还有 {25 - shown} 人未显示" in msg, \
        f"实际列了 {shown} 行,那句话却是:" \
        f"{[ln for ln in msg.split(chr(10)) if '未显示' in ln]}"
    assert len(msg) <= _TG_HARD_LIMIT
    assert _html_ok(msg)
    assert msg.split("\n")[-1] == f"<code>{_SIG_CA}</code>"


def test_一个人名字长不许把排在他后面的短名字连带丢掉():
    """
    ⚠️ _receiver_rows 里装不下的那一行必须 `continue` 而不是 `break`。
       break 会让一个长 handle 把排在它**后面**、本来完全塞得下的短行全部连带丢掉 ——
       一个人名字长,后面所有人就都消失了。(与 bot._ca_assemble 同一条教训。)

    ⚠️ 这条**必须直接测那个循环**,不能走整条消息渲染:实测走 _sig() 时 room 有
       八百多,三个长行全都塞得下,那个分支根本不会被走到 —— 我第一版就是这么写的,
       把 continue 改成 break 之后 631 条全绿。空转测试就是这么来的。
    ⚠️ room=300 是实测挑的:长行成本 190、短行 51。
       continue → 长行 1 个 + 短行 2 个 = 3 行;break → 只有 1 行。
       门槛全是写死的字面量,不从被测模块 import 任何常量。
    """
    def _r(who):
        return {"who": who, "usd": 900.0, "mcap": 1.98e5, "hits": 1,
                "ts": "2026-08-26T00:32:17+00:00"}

    evil = "'" * 24                       # 转义后 144 字符 → 整行 190
    recv = [_r(f"{evil}{i}") for i in range(3)] + [_r(f"Short{i}") for i in range(3, 8)]

    # 前提自检:长行确实塞不下第二个,短行确实塞得下 —— 前提垮了这条测试就没意义
    costs = [len(formatter._receiver_row(r, None)) + 1 for r in recv]
    assert costs[0] > 150 and costs[-1] < 60, f"前提不成立,行成本变了:{costs}"
    assert costs[0] * 2 > 300, "前提不成立:两个长行居然塞得进 room"

    body, shown = formatter._receiver_rows(recv, None, 300)

    kept = [ln for ln in body if "Short" in ln]
    assert kept, (
        "长 handle 把排在它后面、本来塞得下的短行连带丢掉了 —— "
        f"这正是 continue 改成 break 的后果。渲染出的行数={shown},内容={body}"
    )
    assert shown == len(body), "shown 与实际行数对不上,「还有 N 人未显示」会算错"


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


# ============================================================
# 指定用户的转入 —— 逐条推送(/tin)
# ============================================================
# 渲染时刻:比事件时间晚 3 分 20 秒。写死才能断言"多久之前到账"那一格。
_TIN_TS = "2026-08-26T00:37:40+00:00"
_NOW_TIN = datetime(2026, 8, 26, 0, 41, 0, tzinfo=UTC).timestamp()

render_transfer_in_watch = formatter.render_transfer_in_watch


def _tin(**kw):
    """一条形态完整的"被点名的人收到了一笔币",字段取 $fih 那笔真实转账"""
    base = {
        "event_type": EVENT_TRANSFER_IN,
        "event_id": "TRANSFER_IN:x1",
        "user_id": "uA",
        "handle": "PoorGoat",
        "user_handle": "PoorGoat_",
        "network_id": "solana",
        "token_address": _SIG_CA,
        "token_symbol": "fih",
        "event_ts": _TIN_TS,
        "amount_usd": 2439.09,
        "token_amount": "12000000",
        "market_cap": 198_200.0,
        "counterparty_address": _SIG_SENDER,
    }
    base.update(kw)
    return render_transfer_in_watch(make_event(**base), now=_NOW_TIN)


def test_转入推送只摆事实_绝不替用户断言这是买入():
    """
    ⚠️⚠️ 这个功能存在的前提是"有些人在别处成交,币是转进来的" —— 但报文里
       **只有 fromAddress/toAddress**,没有任何证据说明这笔转账来自哪个工具、
       是不是买入、花没花钱。用户自己知道这个人用什么工具,他读得出来;
       我们替他写出来就是编(本项目已经因为「他们一分钱没花」被打回过一次)。
    ⚠️ 断言的是"这些说法**不出现**",换个措辞不该让它变红;
       但只要有人把任何一句意图断言塞进模板就必须红。
    """
    msg = _tin()
    for claim in ("买入", "DEBOT", "建仓", "抄底", "没花", "一分钱",
                  "免费", "白拿", "空投", "非市场"):
        assert claim not in msg, f"消息里出现了数据证明不了的断言:{claim}"


def test_转入推送把可证的事实一条不落地摆出来():
    """谁 / 什么币 / 多少枚 / 多少美元 / 收到时市值 / 发货地址 / 多久之前 / 链 / CA"""
    msg = _tin()
    assert msg.split("\n")[0].startswith("📥"), "行首锚点复用 📥(收到转入),不新增第七个"
    assert "PoorGoat" in msg and "@PoorGoat_" in msg
    assert "$fih" in msg
    assert "12,000,000" in msg, "多少枚"
    assert "$2,439.09" in msg, "多少美元"
    assert "收到时市值 $198.20K" in msg
    assert "8FtY7n…cZx72" in msg, "发货地址要截短显示"
    assert "3 分 20 秒前到账" in msg, "多久之前"
    assert "🧬 Solana" in msg
    assert msg.split("\n")[-1] == f"<code>{_SIG_CA}</code>", "CA 独占最后一行、纯 code"


def test_拿不到收到时市值就整行消失_绝不用现在的市值冒充():
    """
    poller 只在转账足够新时才把本轮观测到的市值填进去(那时它才**等于**收到时的市值)。
    拿不到时这一格必须整格消失 —— 写一个"现在的市值"进去就是陈述假事实。
    """
    msg = _tin(market_cap=None)
    assert "市值" not in msg, "市值那一行必须整行消失"
    assert "$2,439.09" in msg, "其余的事实照常显示"


def test_发货地址取不到就少那一行而不是打问号():
    msg = _tin(counterparty_address=None)
    assert "📮" not in msg
    assert "8FtY7n" not in msg


def test_转入推送里名单内转账必须标出来():
    """B-9:筹码在名单内部搬家与从外面进货是两回事,不标出来用户分不出"""
    msg = _tin(counterparty_handle="unipcs", counterparty_is_watched=True)
    assert "unipcs" in msg and "名单内转账" in msg


def test_转入推送的超长昵称和ticker不许把整条消息挤没():
    """
    ⚠️ handle / ticker 由陌生人和服务端决定,长度不受任何约束。
       出口不变式与 /ca、与分发预警同一套:≤ 预算、HTML 合法、CA 锚点完整。
    ⚠️ 而且**不能靠出口那道闸兜底**:出口只会整行整行往回砍,标题一旦自己就超预算,
       砍到最后只剩一个 CA —— 消息里连是谁、收到了什么都没有了。
    ⚠️ 4096 是 Telegram 的硬上限(外部事实),不从被测模块 import。
    """
    msg = _tin(handle="'" * 400, user_handle="x" * 400, token_symbol="&" * 400)
    assert len(msg) <= _TG_HARD_LIMIT, f"实际 {len(msg)} 字符,会被 notifier 盲切"
    assert _html_ok(msg), "标签必须全部配对闭合,残缺实体 = 整条 400"
    assert msg.split("\n")[-1] == f"<code>{_SIG_CA}</code>", "锚点必须活到最后且完整"
    assert "$2,439.09" in msg, "把名字截短就够了,不该连事实一起丢掉"
    assert "&amp;amp;" not in msg, "币名被转义了两次(先 escape 又进了会 escape 的模板)"


def test_方向不明时标题不许写收到转入():
    """把一笔实际是转出的记录渲染成「收到转入」是彻底的错误信息"""
    msg = _tin(side_unknown=True)
    assert "收到转入" not in msg
    assert "交易" in msg.split("\n")[0]


def test_特别关注的人转入推送也带星标():
    ev = make_event(event_type=EVENT_TRANSFER_IN, user_id="uA", handle="PoorGoat",
                    token_symbol="fih", event_ts=_TIN_TS, amount_usd=2439.09)
    plain = render_transfer_in_watch(ev, now=_NOW_TIN)
    starred = render_transfer_in_watch(ev, starred=True, now=_NOW_TIN)
    assert "⭐" not in plain and "⭐" in starred
    assert starred[0] == plain[0] == "📥", "星标绝不能顶掉行首的事件锚点"
