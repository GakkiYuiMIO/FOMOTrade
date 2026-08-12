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
⚠️ 点下去之后**有没有二次确认弹窗未知** —— 那要真买一次才知道。
   所以这里按"点下去就是成交"设计:最后那一步之前的所有校验都必须已经通过。
"""
from __future__ import annotations

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


@dataclass
class BuyResult:
    ok: bool
    message: str
    screenshot: str | None = None


class ExecutorError(Exception):
    """执行器无法安全地继续。⚠️ 一律当成"没有成交"处理"""


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

    with sync_playwright() as p:
        ctx = None
        for ch in _LOGIN_CHANNELS:
            try:
                kw = dict(base)
                if ch:
                    kw["channel"] = ch
                ctx = p.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        if ctx is None:
            # ⚠️ 最常见的原因是**这个 profile 已经被另一个浏览器占用**
            #    (比如你自己开着 --login 那个窗口)。说清楚,别让人去查代理和网络。
            raise ExecutorError(
                f"浏览器起不来(profile 可能正被另一个窗口占用,关掉再试): {last}"
            ) from last
        try:
            return _do_buy(ctx, url, sym, amount_usd, token_address,
                           dry_run=dry_run, screenshot_dir=screenshot_dir)
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

    logger.warning("执行真实买入 | {} ${:.2f}", sym or ca[:8], amount_usd)
    submit.click()
    page.wait_for_timeout(6000)
    if shot:
        page.screenshot(path=shot)
    # ⚠️ 这里**不敢断言成交成功**:链上确认要时间,页面上的 toast 文案也未实测。
    #    所以回执写"已提交,请到 APP 核对",而不是"已成交" ——
    #    在没有回执的情况下报成功,是这个功能最坏的一种失效。
    return BuyResult(True, f"已提交买入 ${amount_usd:.2f},**请到 APP 核对是否成交**", shot)
