"""
不可信字段的**单一收口**(formatter.UNTRUSTED_FIELDS)。

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
  2. 反射检查四个渲染函数的**每一个**关键字参数:要么在表里(不可信),
     要么在"已逐个复核"的名单里。新加一个外部来源字段两边都不登记 → 红。

⚠️ 断言写死字面量:门禁函数按**名字**比对,不 import 阈值 / 正则。
"""
# ruff: noqa: N802
from __future__ import annotations

from src import formatter
from src.formatter import (
    UNTRUSTED_FIELDS,
    render,
    render_pump_trade,
    render_transfer_in_signal,
    render_transfer_in_watch,
    unclassified_render_params,
)

_RENDERERS = (render, render_pump_trade, render_transfer_in_watch, render_transfer_in_signal)


def test_收口表逐字对上():
    """
    ⚠️⚠️ 这张表是"结构上不可能漏"的那个结构本身。改它 = 改这条断言,
       而不能反过来。五个字段各自的来源:
         token_name       DexScreener baseToken.name(谁都能给自己的币起名)
         token_name_zh    维基 / Google 译文(外呼走代理,代理能篡改)
         pool_quote_name  底池对手全名(DexScreener 或 Yahoo longName)
         stock_company_zh 公司名译文(来源同 token_name_zh)
         stock_exchange   Yahoo fullExchangeName(按交易所代码的形态收,不是形状白名单)
    """
    assert {name: fn.__name__ for name, fn in UNTRUSTED_FIELDS.items()} == {
        "token_name": "safe_display",
        "token_name_zh": "safe_display",
        "pool_quote_name": "safe_display",
        "stock_company_zh": "safe_display",
        "stock_exchange": "safe_exchange",
    }


def test_没有任何渲染参数是没登记过的():
    """⚠️ 下一个人给渲染函数加一个外部来源字段(社媒链接 / 发射台名 / 持有人数…)
       而忘了登记时,这条当场红,不会静默绕过门禁。"""
    assert unclassified_render_params(*_RENDERERS) == set()


def test_每个渲染函数都真的挂了装饰器():
    """⚠️ 表里登记了、函数上却没挂装饰器,等于没登记。"""
    for fn in _RENDERERS:
        assert hasattr(fn, "__guarded_fields__"), fn.__name__
        for name in fn.__guarded_fields__:
            assert name in UNTRUSTED_FIELDS


def test_入口过一遍之后下游拿到的就是干净值():
    """
    ⚠️ 收口点在**入口**:上游传进来的原始值一律不直接使用。
       这里用一个"下游看得见"的方式证明它:把渲染函数换成一个只把参数记下来的假函数,
       走同一个装饰器,看它收到的是不是已经过完门禁的值。
    """
    got = {}

    def fake(token_name=None, pool_quote_name=None, stock_exchange=None, ev=None):
        got.update(token_name=token_name, pool_quote_name=pool_quote_name,
                   stock_exchange=stock_exchange)

    formatter._guard_untrusted(fake)(
        token_name="Join t.me/freeairdrop",
        pool_quote_name="  USA   Rare Earth, Inc. ",
        stock_exchange="立即访问 t.me/free-airdrop")
    assert got == {"token_name": None,
                   "pool_quote_name": "USA Rare Earth, Inc.",
                   "stock_exchange": None}


def test_门禁自己炸了也只是那一段不显示():
    """⚠️ 门禁抛异常不能把整条推送带走 —— 一律当作"不合格"。"""
    class Boom:
        def __str__(self):
            raise RuntimeError("上游给了个会炸的对象")

    assert formatter._guard_one("token_name", Boom()) is None
