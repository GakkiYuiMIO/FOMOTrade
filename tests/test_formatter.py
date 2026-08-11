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
