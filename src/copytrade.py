"""
跟单信号判定 —— 「N 个关注的人买了同一个币」就建仓。

============ 这个模块的边界(重要)============
本文件是**纯函数**:输入一份候选事实,输出"跟/不跟 + 为什么"。
它不查库、不发请求、更不下单。理由和 formatter 一样 ——
判定逻辑是这个功能里唯一会让人亏钱的地方,它必须能被完整单测。

真实下单永远由**用户点击**触发(TG 确认按钮),本项目不做无人值守自动成交。

============ 这个信号的已知弱点(用真实数据量过)============
在用户自己 50 个币的记录上回测「第 N 个人买入时跟单、持有到现在」:

    ≥2 人   50 个样本   盈 24 / 亏 26   中位数 0.99x   平均 1.64x
    ≥3 人   27 个样本   盈 12 / 亏 15   中位数 0.98x   平均 1.10x
    ≥5 人    7 个样本   盈  4 / 亏  3   中位数 1.00x   平均 1.49x

⚠️ 阈值越高越晚,而这个信号的全部价值就在于早:
   $Plumber 在第 2 个人买入时跟是 31.62x,等到第 5 个人才跟是 **0.74x** ——
   同一个币,/hot 上写着「最高 99.9x」。
   原因是名单的买入不是均匀分布的:前 2 个人在 $41.9K/$98K 进,
   之后**断档 8 个半小时**,剩下 9 个人全挤在 $4.19M。
   「N 个人买入」成立的那一刻,往往正是拉升已经发生完的那一刻。
⚠️ 上面的数字**没算手续费、滑点、gas**,真实结果只会更差;
   而且是回看,样本仅约一天。这就是默认先走纸上跟单的原因。
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CopyConfig:
    """
    跟单参数。全部可通过 /copy 命令改,存在 runtime_state 里(见 store.copy_config)。

    ⚠️ enabled 默认 False:这个功能会花钱(哪怕只是纸上记账也会影响判断),
       绝不能因为升级了一个版本就自己跑起来。
    """

    enabled: bool = False
    # ⚠️ 默认只做纸上跟单。要接真实下单必须显式关掉它 ——
    #    "升级了一版就开始花钱"是绝对不能发生的事。
    paper_only: bool = True
    # ⚠️ 就算开了真实下单,默认也只**演练**:走完全部步骤但不点最后那个成交按钮。
    #    这是验证"自动化点对了没有"的唯一安全方式。确认无误后 /copy live 关掉它。
    dry_run_execute: bool = True
    min_buyers: int = 2                 # 几个名单成员买过就触发
    window_hours: int = 24              # 在多长的窗口内数这些人
    max_age_hours: int | None = 24      # 币龄上限;None = 不限
    max_entry_mcap: float | None = None  # 入场市值上限;None = 不限
    amount_usd: float = 50.0            # 每单金额
    daily_max: int = 10                 # 每天最多跟几单
    networks: tuple[str, ...] = ()      # 链白名单;空 = 不限
    starred_only: bool = False          # 只数 ⭐ 特别关注的人


# 判定结果的 reason 取值。⚠️ 每一条"没跟"都要能说出原因 ——
# 用户问"这个币为什么没跟"时,答不上来的过滤器等于没有
SKIP_DISABLED = "未启用"
SKIP_NOT_ENOUGH = "人数不够"
SKIP_ALREADY = "这个币已经跟过"
SKIP_TOO_OLD = "币龄超上限"
SKIP_NO_AGE = "拿不到币龄"
SKIP_MCAP = "入场市值超上限"
SKIP_NO_MCAP = "拿不到入场市值"
SKIP_NETWORK = "链不在白名单"
SKIP_DAILY = "已达当日上限"
TAKE = "命中"


@dataclass(frozen=True)
class Candidate:
    """一个币在**这一刻**的事实。全部由调用方从库里查好,本模块不碰 IO。"""

    network_id: str
    token_address: str
    token_symbol: str | None
    buyers: int                        # 窗口内买过的名单成员数(已按 starred_only 过滤)
    entry_mcap: float | None           # 现在的市值 = 若跟单的成本基准
    token_created_at: int | None       # unix 秒
    already_taken: bool                # 这个币是否已经跟过
    taken_today: int                   # 今天已经跟了几单


@dataclass(frozen=True)
class Decision:
    take: bool
    reason: str
    age_sec: int | None = None


def decide(c: Candidate, cfg: CopyConfig, now: float | None = None) -> Decision:
    """
    跟不跟。**顺序有意为之**:先判断"根本轮不到它"的条件,再判断门槛,
    这样 reason 报出来的是用户最关心的那个原因,而不是碰巧先命中的那个。

    ⚠️ 拿不到币龄 / 拿不到市值时一律**不跟**(而不是放行):
       这两个值恰恰是新币最容易缺的(市值来自 balances,而 balances 晚于 swaps 索引),
       放行等于"筛选器在最该起作用的时候自动失效"。
       宁可漏一单,不可在完全不知道买的是什么的情况下建仓。
    """
    now = time.time() if now is None else now

    if not cfg.enabled:
        return Decision(False, SKIP_DISABLED)
    if c.already_taken:
        return Decision(False, SKIP_ALREADY)
    if c.buyers < cfg.min_buyers:
        return Decision(False, SKIP_NOT_ENOUGH)
    if cfg.networks and c.network_id not in cfg.networks:
        return Decision(False, SKIP_NETWORK)

    age = None
    if cfg.max_age_hours is not None:
        if c.token_created_at is None:
            return Decision(False, SKIP_NO_AGE)
        age = int(now - c.token_created_at)
        if age < 0 or age > cfg.max_age_hours * 3600:
            return Decision(False, SKIP_TOO_OLD, age)
    elif c.token_created_at is not None:
        age = int(now - c.token_created_at)

    if cfg.max_entry_mcap is not None:
        if c.entry_mcap is None:
            return Decision(False, SKIP_NO_MCAP, age)
        if c.entry_mcap > cfg.max_entry_mcap:
            return Decision(False, SKIP_MCAP, age)

    # ⚠️ 当日上限放在**最后**:它是"今天不跟了"而不是"这个币不合格"。
    #    放前面的话,达到上限之后所有币的原因都变成"已达当日上限",
    #    看不出哪些币其实本来也不符合条件。
    if cfg.daily_max > 0 and c.taken_today >= cfg.daily_max:
        return Decision(False, SKIP_DAILY, age)

    return Decision(True, TAKE, age)


def pnl(entry_mcap: float | None, now_mcap: float | None,
        amount_usd: float) -> tuple[float, float] | None:
    """
    纸上盈亏:(当前价值, 倍数)。任一输入缺失返回 None(整段不显示)。

    ⚠️ 用**市值比**折算而不是记 token 数量:我们没有真实成交回执,
       记数量等于把滑点、手续费、成交价假装成理想值。
       市值比至少口径诚实 —— 它就是"这个币涨跌了多少",不假装是你的真实收益。
    """
    if not entry_mcap or not now_mcap or entry_mcap <= 0:
        return None
    x = now_mcap / entry_mcap
    return amount_usd * x, x
