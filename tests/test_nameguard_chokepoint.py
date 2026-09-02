"""
不可信字段的**单一收口**(formatter.UNTRUSTED_FIELDS + formatter.IDENT_FIELDS)。

============ 这个文件为什么存在 ============
过去 poller / pumpfun / formatter **各自记得**给哪些字段套门禁,于是漏了两个:
  · 🏢 行的交易所名(Yahoo fullExchangeName)全程零门禁;
  · 🌊 行的对手全名(pool_quote_name)在渲染层没有门禁。
"记得给新字段加过滤"不是一个能靠人维持的不变量。现在渲染入口按一张表统一过一遍,
而这个文件钉住那张表:

  1. **逐字**断言表里登记了哪几个字段、各用哪一道门 —— 少登记一个 / 换成弱一点的门,
     这里当场红。⚠️ 这条不能靠"渲染结果里没有攻击串"来代替:
     几个单行渲染函数**自己也各调了一次** safe_display(刻意保留的第二道),
     所以少登记一个字段时推送看起来仍然是干净的 —— 唯一会红的就是这条断言。
  2. 反射检查**每一个**渲染函数的每一个参数:要么在门禁表里,要么在"已逐个复核"的
     名单里。新加一个外部来源字段两边都不登记 → 红。

⚠️⚠️ **判卷的逻辑必须在测试这一侧。** 上一轮这条不变量写的是
   `assert formatter.unclassified_render_params(*_RENDERERS) == set()` ——
   而那个函数就在 src/formatter.py 里,与它要检查的两张表同一个模块:
   把它内部条件改成 `if False and ...`,全量 1768 条测试 **0 红**(实测过)。
   那是被测模块给自己判卷。现在改成:测试**自己**用 inspect 取签名,
   和下面这份写死在测试文件里的清单逐字对比。被测模块只提供数据。

⚠️ 断言写死字面量:门禁函数按**名字**比对,不 import 阈值 / 正则。
"""
# ruff: noqa: N802
from __future__ import annotations

import inspect

import pytest

from src import formatter
from src.formatter import (
    IDENT_FIELDS,
    UNTRUSTED_FIELDS,
    render,
    render_alpha_listing,
    render_pump_callout,
    render_pump_trade,
    render_transfer_in_signal,
    render_transfer_in_watch,
)

# ⚠️ 六个渲染函数**全部**列在这里。少列一个 = 那个函数的参数从此不受这条不变量约束,
#    所以下面另有一条断言:formatter 里所有 `render*` 公开函数都必须出现在这个元组里。
_RENDERERS = (render, render_pump_trade, render_transfer_in_watch,
              render_transfer_in_signal, render_alpha_listing, render_pump_callout)

# ============================================================
# 写死在**测试这一侧**的字段清单 —— D1 不变量的判卷依据
# ============================================================
# ⚠️⚠️ 这三份清单是**手抄**的,不从 src.formatter import 任何集合。
#    改渲染函数签名 = 改这里,而不能反过来。
_EXPECT_UNTRUSTED = {
    "token_name": "safe_display",        # DexScreener baseToken.name,谁都能给自己的币起名
    "token_name_zh": "safe_display",     # 维基 / Google 译文,外呼走代理,代理能篡改
    "pool_quote_name": "safe_display",   # 底池对手全名(DexScreener 或 Yahoo longName)
    "stock_company_zh": "safe_display",  # 公司名译文,来源同 token_name_zh
    "stock_exchange": "safe_exchange",   # Yahoo fullExchangeName,**封闭枚举**
}
_EXPECT_IDENT = {
    "token_symbol": "safe_ident",        # 币符号,陌生人可控($t.me/pumpgrp 曾原样进标题)
    "pool_quote_symbol": "safe_ident",   # 底池对手符号,同时是 🏢 行的 ticker
    "username": "safe_ident",            # pump 用户名,本人可控
    "symbol": "safe_ident",              # 币安 Alpha 的币符号
}
# 已逐个复核、**不需要**门禁的参数。分三类:
#   a. 不是文本(数字 / 布尔 / 时间戳 / 列表 / 事件对象);
#   b. 内部枚举或已归一化的标识(network_id / side / chain_display);
#   c. 地址 / 正文类字段 —— 外部可控,但走 _clip(叠平空白 → 限长 → 转义)这条既有通道。
# ⚠️ `name`(币安 Alpha 的币名)是一个**已知缺口**,不是遗漏:见 formatter 里那段注释
#    与 README「已知取舍」。
_EXPECT_REVIEWED = {
    "ev", "buyers", "watchlist", "holders", "baseline_pending", "starred", "now",
    "network_id", "token_address", "receiver_count", "receivers",
    "window_hours", "senders",
    "side", "coin_mint", "amount_usd", "price_usd", "holding_usd",
    "is_cleared", "unrealized_pnl_usd", "unrealized_pnl_pct", "realized_pnl_usd",
    "realized_pnl_pct", "market_cap_usd", "ath_market_cap_usd", "holders_in_list",
    "traded_at", "chain_display", "tx",
    "name", "listing_time_ms", "chain_name", "contract_address", "market_cap", "sector",
    "thesis", "multiple", "likes", "view_count", "created_at",
}


def test_收口表逐字对上():
    """
    ⚠️⚠️ 这两张表是"结构上不可能漏"的那个结构本身。改它 = 改这条断言,
       而不能反过来。
    ⚠️ stock_exchange 走的是 safe_exchange(封闭枚举),**不是** safe_display ——
       换成任何一个别的名字,这条当场红。
    """
    assert {n: fn.__name__ for n, fn in UNTRUSTED_FIELDS.items()} == _EXPECT_UNTRUSTED
    assert {n: fn.__name__ for n, fn in IDENT_FIELDS.items()} == _EXPECT_IDENT
    assert set(UNTRUSTED_FIELDS) & set(IDENT_FIELDS) == set(), "一个字段只能有一道门"


def test_没有任何渲染参数是没登记过的():
    """
    ⚠️⚠️ D1 不变量。**判卷逻辑全在这一侧**:这里自己 inspect 取签名,与上面三份
       手抄清单比对。下一个人给渲染函数加一个外部来源字段(社媒链接 / 发射台名 /
       持有人数 …)而忘了登记时,这条当场红。
    ⚠️ 那个曾经被当断言用的 formatter.unclassified_render_params **已经删掉了** ——
       它与它要检查的表在同一个模块,用它当断言等于自己给自己判卷(把它内部条件改成
       `if False and ...` 全量 0 红,这是实测过的);删掉之后不可能有人再退回去用它。
    """
    known = set(_EXPECT_UNTRUSTED) | set(_EXPECT_IDENT) | _EXPECT_REVIEWED
    unknown = {}
    for fn in _RENDERERS:
        extra = sorted(n for n in inspect.signature(fn).parameters if n not in known)
        if extra:
            unknown[fn.__name__] = extra
    assert unknown == {}, f"这些渲染参数既没登记门禁也没登记'已复核':{unknown}"


def test_登记了门禁的字段确实出现在渲染函数里():
    """⚠️ 反方向:表里登记了一个**根本不存在**的参数名,等于一条死规则,同样要红。"""
    params = {n for fn in _RENDERERS for n in inspect.signature(fn).parameters}
    for name in list(_EXPECT_UNTRUSTED) + list(_EXPECT_IDENT):
        assert name in params, f"{name} 登记了门禁,却不是任何渲染函数的参数"


def test_所有公开渲染函数都在被检查的名单里():
    """
    ⚠️⚠️ 上面那条不变量只覆盖 _RENDERERS 里列的函数。谁新写一个 render_xxx 而忘了
       加进来,不变量就绕过去了 —— 这条把 formatter 里所有 `render*` 公开函数
       抓出来逐个比对。
    ⚠️ 例外只有两个:render_alpha_batch(参数是一串 (symbol, 数量) 元组,不是关键字字段)
       与 render_copy_signal(参数是 cand/d/cfg 三个内部对象)。
    """
    found = {n for n in dir(formatter)
             if n.startswith("render") and callable(getattr(formatter, n))}
    listed = {fn.__name__ for fn in _RENDERERS} | {"render_alpha_batch", "render_copy_signal"}
    assert found - listed == set(), f"这些渲染函数没进 D1 不变量的名单:{sorted(found - listed)}"


def test_每个渲染函数都真的挂了装饰器():
    """⚠️ 表里登记了、函数上却没挂装饰器,等于没登记。"""
    for fn in _RENDERERS:
        assert hasattr(fn, "__guarded_fields__"), fn.__name__
        for name in fn.__guarded_fields__:
            assert name in _EXPECT_UNTRUSTED or name in _EXPECT_IDENT, (fn.__name__, name)


def test_入口过一遍之后下游拿到的就是干净值():
    """
    ⚠️ 收口点在**入口**:上游传进来的原始值一律不直接使用。
       这里用一个"下游看得见"的方式证明它:把渲染函数换成一个只把参数记下来的假函数,
       走同一个装饰器,看它收到的是不是已经过完门禁的值。
    """
    got = {}

    def fake(token_name=None, pool_quote_name=None, stock_exchange=None,
             token_symbol=None, ev=None):
        got.update(token_name=token_name, pool_quote_name=pool_quote_name,
                   stock_exchange=stock_exchange, token_symbol=token_symbol)

    formatter._guard_untrusted(fake)(
        token_name="Join t.me/freeairdrop",
        pool_quote_name="  USA   Rare Earth, Inc. ",
        stock_exchange="立即访问 t.me/free-airdrop",
        token_symbol="t.me/pumpgrp")
    assert got == {"token_name": None,
                   "pool_quote_name": "USA Rare Earth, Inc.",
                   "stock_exchange": None,
                   "token_symbol": None}


def test_译文字段拿得到自己的原文():
    """
    ⚠️⚠️ 译文那道门要看**原文**才判得出"凭空多出的英文串"。少了这一步,
       'Tesla xStock' → '特斯拉 xStock' 这类正常译文会被整段丢弃
       (真网络实测:xStock 全家族 100% 丢译名)。
    ⚠️ 配对关系(token_name_zh ← token_name)写死在这里,改 formatter 就得改这条。
    """
    got = {}

    def fake(token_name=None, token_name_zh=None,
             pool_quote_name=None, stock_company_zh=None):
        got.update(token_name_zh=token_name_zh, stock_company_zh=stock_company_zh)

    guarded = formatter._guard_untrusted(fake)
    guarded(token_name="Tesla xStock", token_name_zh="特斯拉 xStock",
            pool_quote_name="Circle Internet Group", stock_company_zh="圆环 Internet 集团")
    assert got == {"token_name_zh": "特斯拉 xStock", "stock_company_zh": "圆环 Internet 集团"}

    # 对照组:原文里**没有**那个英文串 → 照旧整段丢弃
    got.clear()
    guarded(token_name="Nice Coin", token_name_zh="好币 airdrop",
            pool_quote_name="Nice Corp", stock_company_zh="好公司 freegift")
    assert got == {"token_name_zh": None, "stock_company_zh": None}


def test_那个自己给自己判卷的函数已经不存在了():
    """
    ⚠️⚠️ D1 不变量曾经写成 `assert formatter.unclassified_render_params(...) == set()`,
       而那个函数就在被测模块里 —— 把它内部条件改成 `if False and ...`,全量 0 红。
       现在判卷逻辑在测试这一侧,那个函数已删。这条钉住"别再加回去"。
    """
    assert not hasattr(formatter, "unclassified_render_params")


def test_门禁自己炸了也只是那一段不显示():
    """⚠️ 门禁抛异常不能把整条推送带走 —— 一律当作"不合格"。"""
    class Boom:
        def __str__(self):
            raise RuntimeError("上游给了个会炸的对象")

    assert formatter._guard_one("token_name", Boom()) is None
    assert formatter._guard_one("token_symbol", Boom()) is None


@pytest.mark.parametrize("fn_name", ["render", "render_pump_trade",
                                     "render_transfer_in_watch", "render_transfer_in_signal",
                                     "render_alpha_listing", "render_pump_callout"])
def test_装饰器不改变签名(fn_name):
    """⚠️ 反射检查要看得到真实签名,不能是 (*args, **kwargs) —— 否则不变量全空转。"""
    fn = getattr(formatter, fn_name)
    names = list(inspect.signature(fn).parameters)
    assert names and names != ["args", "kwargs"], fn_name
