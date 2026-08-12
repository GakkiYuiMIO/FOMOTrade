"""
真实买入执行器 —— 用 Playwright 驱动 fomo.family 的买入面板。

============ 这个文件会花真钱。读之前先记住三条 ============
1. **只在用户点了 TG 确认按钮之后才会被调用。** 没有任何自动触发路径。
2. **dry_run 默认 True**:走完全部步骤但**不点最后那个成交按钮**,只截图。
   这是用户验证"自动化到底点对了没有"的唯一安全方式。
3. 每一步都要**读回页面状态确认**,而不是"点了就当成了"。
   盲点一串按钮在别的场景里最多是失败,在这里是**买错币或买错金额**。

============ 为什么必须走浏览器 ============
FOMO 的 swap 是**链上交易**(swap 记录里有 recipient / platformFeeAddress),
由 Privy 嵌入式钱包在浏览器里签名。data/fomo_session.json 里只有 API 的读 token,
**签不了交易**。所以没有"调个下单接口"这条路,只能驱动真实网页。

============ 页面结构(2026-08-12 实测)============
    [Buy] [Sell]                  ← tab
    <input placeholder="0">       ← 金额
    [$10] [$100] [$500] [$1000]
    $47.90 available   [Max]
    [ Buy <Symbol> ]              ← 最终成交按钮
    ─────────────────────────────
    <button> 661.89 Plumber  +$0.01 ▲0.78%
             Invested   $1.90
             Avg entry  $2.7M MC   ← 持仓块,成交回读就读它

⚠️ **点下去就是成交,没有二次确认弹窗**(2026-08-12 由用户真实买入 $1.90 验证)。
   一度以为点完还有个滑块要拖 —— 没有。所以"点最后那个按钮"= 钱已经出去了,
   在此之前的所有校验必须已经全部通过,点下去之后没有任何后悔的余地。

⚠️ 持仓块**在买入之前就可能存在**(你已经持有这个币的时候)。
   所以"看到持仓块"**不能**当作成交证据 —— 必须点之前存一份 Invested,
   点之后等它**变大**。把"页面上有仓位"当成"我这单成了",在已有仓位时永远误报成功。
"""
from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass

from loguru import logger

from src.config import PROFILE_DIR, get_settings
from src.models import NETWORK_SLUG

TOKEN_URL = "https://fomo.family/tokens/{slug}/{ca}"

# 页面加载后等 React 渲染的时间。⚠️ 宁可等久一点:抢在面板渲染完之前操作,
# 会点到"看起来在同一个位置、其实是别的东西"上。
_RENDER_MS = 8000
_STEP_TIMEOUT_MS = 30_000
# 等报价算出来、成交按钮变可点的时间。实测填完金额约 1~2s 变绿,给足余量
_QUOTE_TIMEOUT_MS = 20_000
# 点完之后等页面上的持仓数字变化的时间。链上确认 + 前端刷新,实测几秒;给足余量。
# ⚠️ 等超时**不等于没成交** —— 只是没等到证据,回执必须照实这么说。
_FILL_TIMEOUT_MS = 45_000
_FILL_POLL_MS = 1000

# 持仓块:文字里同时有 Invested 和 Avg entry 的那个 button
_POSITION_JS = """() => {
  const b = [...document.querySelectorAll('button')].filter(e => {
    const t = e.innerText || '';
    return t.includes('Invested') && t.includes('Avg entry');
  });
  return b.length ? b[b.length - 1].innerText : null;
}"""
_RE_INVESTED = re.compile(r"Invested\s*\$\s*([\d,]+(?:\.\d+)?)", re.I)
_RE_QTY = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s+(\S+)", re.M)
_RE_ENTRY = re.compile(r"Avg entry\s*(\$[\d.,]+\s*[KMB]?)\s*MC", re.I)


@dataclass
class Position:
    """页面上读到的持仓快照。字段可能缺 —— 缺就是 None,不要拿 0 顶替"""
    invested: float | None = None
    qty: float | None = None
    symbol: str | None = None
    avg_entry: str | None = None


@dataclass
class BuyResult:
    ok: bool
    message: str
    screenshot: str | None = None
    # 是否**读到了**成交证据(Invested 变大)。⚠️ False 只代表"没等到证据",
    # 不代表"没成交" —— 上层措辞必须区分这两件事。
    confirmed: bool = False


class ExecutorError(Exception):
    """执行器无法安全地继续。⚠️ 一律当成"没有成交"处理"""


def _read_position(page) -> Position | None:
    """读右侧面板的持仓块。读不到返回 None(没持仓,或结构变了)"""
    try:
        txt = page.evaluate(_POSITION_JS)
    except Exception:  # noqa: BLE001
        return None
    if not txt:
        return None
    pos = Position()
    if m := _RE_INVESTED.search(txt):
        with suppress(ValueError):
            pos.invested = float(m.group(1).replace(",", ""))
    if m := _RE_QTY.search(txt):
        with suppress(ValueError):
            pos.qty = float(m.group(1).replace(",", ""))
        pos.symbol = m.group(2)
    if m := _RE_ENTRY.search(txt):
        pos.avg_entry = re.sub(r"\s+", "", m.group(1))
    return pos


def buy(network_id: str, token_address: str, token_symbol: str | None,
        amount_usd: float, *, dry_run: bool = True,
        screenshot_dir: str | None = None) -> BuyResult:
    """
    在 fomo.family 上买入。**只应由 TG 确认按钮的回调调用。**

    dry_run=True(默认)会走完所有步骤但**不点成交按钮**,并截图 ——
    先用它确认自动化点对了地方,再谈真实成交。
    """
    slug = NETWORK_SLUG.get((network_id or "").strip())
    if not slug:
        # ⚠️ 拼一个平台不支持的链只会打开 404,而 404 页上没有买入面板 ——
        #    与其在后面某一步含糊地失败,不如在这里说清楚
        raise ExecutorError(f"不支持的链: {network_id}")
    if amount_usd <= 0:
        raise ExecutorError(f"金额不合法: {amount_usd}")

    url = TOKEN_URL.format(slug=slug, ca=token_address)
    sym = (token_symbol or "").lstrip("$").strip()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p)
        try:
            return _do_buy(ctx, url, sym, amount_usd, token_address,
                           dry_run=dry_run, screenshot_dir=screenshot_dir)
        finally:
            ctx.close()


def _launch(p):
    """按 PROFILE_DIR 起一个有头浏览器。⚠️ 起不来一律抛 ExecutorError = 没有成交"""
    from src.auth import _DROP_DEFAULT_ARGS, _LOGIN_CHANNELS, _STEALTH_ARGS

    settings = get_settings()
    base: dict = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": False,          # ⚠️ 有头:出问题时你能直接看见页面停在哪一步
        "locale": "en-US",
        "args": list(_STEALTH_ARGS),
        "ignore_default_args": list(_DROP_DEFAULT_ARGS),
    }
    if settings.fomo_proxy:
        base["proxy"] = {"server": settings.fomo_proxy}

    last: Exception | None = None
    for ch in _LOGIN_CHANNELS:
        try:
            kw = dict(base)
            if ch:
                kw["channel"] = ch
            return p.chromium.launch_persistent_context(**kw)
        except Exception as e:  # noqa: BLE001
            last = e
    # ⚠️ 最常见的原因是**这个 profile 已经被另一个浏览器占用**
    #    (比如你自己开着 --login 那个窗口)。说清楚,别让人去查代理和网络。
    raise ExecutorError(
        f"浏览器起不来(profile 可能正被另一个窗口占用,关掉再试): {last}"
    ) from last


def profile_looks_present() -> tuple[bool, str]:
    """
    **不开浏览器**的粗查:PROFILE_DIR 里到底有没有东西。

    ⚠️ 只用来快速否定,不能用来肯定 —— cookie 文件在、内容过期是常态。
       要确认真的还登录着必须 check_login()(那个会开浏览器,十几秒)。
    ⚠️ 存在的理由是 `--login --cdp` 这条路:它 attach 到你自己的 Chrome,
       **从头到尾不写 PROFILE_DIR**(见 auth._open_login_context 的 cdp 分支)。
       而 README 恰恰把 --cdp 推荐成"最可靠" —— 于是登录看起来成功了,
       买入执行器却拿到一个空 profile。无人值守下这会静默失败好几天。
    """
    if not PROFILE_DIR.exists():
        return False, f"浏览器 profile 不存在({PROFILE_DIR})"
    cookies = PROFILE_DIR / "Default" / "Network" / "Cookies"
    if not cookies.exists():
        return False, ("浏览器 profile 里没有 cookie —— "
                       "如果你是用 `--login --cdp` 登录的,那条路**不写这个目录**,"
                       "买入执行器用不了。请再跑一次不带 --cdp 的 `--login`")
    import time as _t

    age_h = (_t.time() - cookies.stat().st_mtime) / 3600
    return True, f"profile 存在,cookie 最后更新于 {age_h:.1f} 小时前"


def check_login(timeout_ms: int = 45_000) -> tuple[bool, str]:
    """
    真开一次浏览器,确认 fomo.family 还认这个 profile。返回 (是否登录着, 说明)。

    ⚠️ 这是**第二套凭据**,和 data/fomo_session.json 那套 API token 完全独立,
       而且全项目没有任何代码会自动续期它。API 那侧失效有 TG 告警 + 停机,
       浏览器这侧原本一点提示都没有 —— 无人值守时会静默失血好几天。
    """
    ok, why = profile_looks_present()
    if not ok:
        return False, why

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto("https://fomo.family/", wait_until="domcontentloaded")
            page.wait_for_timeout(_RENDER_MS)
            if page.locator("text=/Sign in|Log in/i").count() > 0:
                return False, "浏览器登录态已失效,跑一次 `.\\bot.ps1 --login`(别带 --cdp)"
            return True, "浏览器登录态正常"
        except Exception as e:  # noqa: BLE001
            # ⚠️ 查不出来 ≠ 没登录。报成"未登录"会让人白跑一趟登录流程
            return False, f"登录态查不出来(网络/代理?): {e}"
        finally:
            ctx.close()


def _do_buy(ctx, url: str, sym: str, amount_usd: float, ca: str,
            *, dry_run: bool, screenshot_dir: str | None) -> BuyResult:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(_STEP_TIMEOUT_MS)
    logger.info("执行器打开 {}", url)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(_RENDER_MS)

    shot = None
    if screenshot_dir:
        shot = f"{screenshot_dir}/buy_{ca[:8]}.png"

    # ---- 校验 1:确实登录着 ----
    # 未登录时页面照样渲染,但买入面板换成了登录引导 —— 盲点下去会点到登录按钮
    if page.locator("text=/Sign in|Log in/i").count() > 0:
        raise ExecutorError("未登录(profile 里的会话过期了),先跑 .\\bot.ps1 --login")

    # ---- 校验 2:确实在这个币的页面上 ----
    # ⚠️ 搜索框的自动跳转、重定向都可能把你带到别的币。**买错币是不可逆的**,
    #    所以这里按 CA 核对当前 URL,不匹配就立刻停。
    if ca.lower() not in page.url.lower():
        raise ExecutorError(f"页面跑到了别的地址({page.url}),已中止")

    # ---- 切到 Buy tab ----
    buy_tab = page.get_by_role("button", name="Buy", exact=True).first
    if buy_tab.count():
        buy_tab.click()
        page.wait_for_timeout(500)

    # ---- 填金额 ----
    amount_input = page.locator('input[placeholder="0"]').first
    if not amount_input.count():
        raise ExecutorError("找不到金额输入框(页面结构可能改了)")
    amount_input.fill(f"{amount_usd:.2f}")
    page.wait_for_timeout(1500)

    # ---- 校验 3:读回输入框,确认金额真的进去了 ----
    # ⚠️ 这一步不能省。受控组件、千分位格式化、失焦重写都可能让 fill 的值
    #    与页面实际值不一致 —— 而"以为填了 50、实际是 5000"这种事必须在点下去之前发现。
    got = (amount_input.input_value() or "").replace(",", "").replace("$", "").strip()
    try:
        if abs(float(got) - amount_usd) > 0.01:
            raise ExecutorError(f"金额没填对:期望 {amount_usd},页面上是 {got!r}")
    except ValueError as e:
        raise ExecutorError(f"金额没填对:页面上是 {got!r}") from e

    # ---- 找成交按钮 ----
    # 文案是 "Buy <Symbol>"。⚠️ 用 has-text 而不是 get_by_role(name=...):
    #    accessible name 会被按钮内的图标/隐藏文字影响,而这里只想按可见文字找。
    name = f"Buy {sym}" if sym else "Buy"
    submit = page.locator("button", has_text=name).last
    if not submit.count():
        raise ExecutorError(f"找不到成交按钮({name!r});页面结构可能改了")

    # ⚠️ 这个按钮在**报价算出来之前是 disabled 的**(实测:填金额前 disabled=True,
    #    填完约 1~2 秒后变 False)。查得太早会看到灰的,误报成"余额不足"。
    #    所以这里**等它变可点**,而不是立刻断言。
    try:
        submit.wait_for(state="visible", timeout=_STEP_TIMEOUT_MS)
        page.wait_for_function(
            """(txt) => {
                 const b = [...document.querySelectorAll('button')]
                   .filter(e => (e.innerText || '').trim().includes(txt)).pop();
                 return b && !b.disabled;
               }""",
            arg=name, timeout=_QUOTE_TIMEOUT_MS,
        )
    except Exception as e:  # noqa: BLE001
        # 真的一直是灰的:余额不足 / 金额超限 / 报价拉不到。说清楚,别让人以为是脚本坏了
        raise ExecutorError(
            f"成交按钮一直不可点({_QUOTE_TIMEOUT_MS // 1000}s):"
            f"多半是余额不足、金额超限,或报价没拉到"
        ) from e

    if shot:
        page.screenshot(path=shot)

    if dry_run:
        logger.info("[dry-run] 一切就绪但**不点成交** | {} ${:.2f}", sym or ca[:8], amount_usd)
        return BuyResult(True, f"演练通过:${amount_usd:.2f} 已填入、成交按钮可点(未点)", shot)

    # ---- 点之前先把现有仓位记下来 ----
    # ⚠️ 必须在点击**之前**读。已经持有这个币的时候,点完再读只会看到一个
    #    "有仓位"的页面,分不清是这一单买的还是本来就有的。
    before = _read_position(page)
    base = before.invested if before and before.invested is not None else 0.0

    logger.warning("执行真实买入 | {} ${:.2f}(点下去即成交)", sym or ca[:8], amount_usd)
    submit.click()

    # ---- 等成交证据:Invested 变大 ----
    after: Position | None = None
    waited = 0
    while waited < _FILL_TIMEOUT_MS:
        page.wait_for_timeout(_FILL_POLL_MS)
        waited += _FILL_POLL_MS
        cur = _read_position(page)
        if cur and cur.invested is not None and cur.invested > base + 0.005:
            after = cur
            break

    if shot:
        page.screenshot(path=shot)

    if after is not None:
        delta = after.invested - base
        bits = [f"已成交 ${delta:.2f}"]
        if after.qty is not None and after.symbol:
            bits.append(f"持仓 {after.qty:,.2f} {after.symbol}")
        if after.avg_entry:
            bits.append(f"均价 {after.avg_entry} MC")
        logger.info("成交已回读 | {} 投入 {:.2f} → {:.2f}", sym or ca[:8], base, after.invested)
        return BuyResult(True, " · ".join(bits), shot, confirmed=True)

    # ⚠️ 没等到证据**不等于没成交** —— 链可能还在确认、前端可能没刷新。
    #    这种时候报"失败"会诱使你再点一次 = 买两次。所以只说"没等到",并让你自己核对。
    logger.warning("买入已点击但 {}s 内没读到仓位变化 | {}", _FILL_TIMEOUT_MS // 1000, sym or ca[:8])
    return BuyResult(
        True,
        f"已点击成交 ${amount_usd:.2f},但 {_FILL_TIMEOUT_MS // 1000}s 内没读到仓位变化。"
        f"**可能已成交,请到 APP 核对后再决定是否重试** —— 别直接再点一次",
        shot,
    )
