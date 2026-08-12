"""
跟单信号判定 —— 「N 个关注的人买了同一个币」就建仓。

============ 这个模块的边界(重要)============
本文件是**纯函数**:输入一份候选事实,输出"跟/不跟 + 为什么"。
它不查库、不发请求、更不下单。理由和 formatter 一样 ——
判定逻辑是这个功能里唯一会让人亏钱的地方,它必须能被完整单测。

下单有两条路,默认走第一条:
  1. **人工确认**(默认):推一条带 [确认买入] 按钮的消息,用户点了才下单。
  2. **无人值守**(auto_execute):信号命中直接下单,没有人在中间看一眼。

⚠️ 第 2 条把「人的手指」这道护栏拆掉了,所以它有独立开关 auto_execute,
   且**不复用** paper_only / dry_run_execute —— 那两个是「验证自动化点对了没有」
   的流程开关,用户会按顺序把它们一个个关掉。要是自动成交挂在它们上面,
   用户走完验证流程的那一刻就变成了无人值守,而他根本没打算开这个。
   开自动必须显式再点一次头,见 auto_blockers()。

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
    # ⚠️ 无人值守自动成交:命中即下单,中间没有人看一眼。
    #    独立开关、默认 False、且与上面两个**互不蕴含** —— 理由见模块头。
    #    开之前必须过 auto_blockers() 那几道。
    auto_execute: bool = False
    min_buyers: int = 2                 # 几个名单成员买过就触发
    window_hours: int = 24              # 在多长的窗口内数这些人
    max_age_hours: int | None = 24      # 币龄上限;None = 不限
    max_entry_mcap: float | None = None  # 入场市值上限;None = 不限
    amount_usd: float = 50.0            # 每单金额
    daily_max: int = 10                 # 每天最多跟几单;0 = 不限(与 age/mcap 的 off 同义)
    # 每天最多花多少美元。None = 不限。
    # ⚠️ 有人盯着的时候「不限」是可以的(每单都要点一次);无人值守时不行 ——
    #    daily_max 只数**笔数**,而笔数 × 单笔金额才是钱。改了 amount_usd 忘了改
    #    daily_max,当天敞口就是静默翻倍。所以 auto_execute 强制要求这一项有值。
    daily_spend_usd: float | None = None
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
SKIP_DAILY_SPEND = "已达当日金额上限"
TAKE = "命中"


def auto_blockers(cfg: CopyConfig) -> list[str]:
    """
    还差什么才能开无人值守。返回空列表 = 可以开。

    ⚠️ 这是**开关那一刻**的检查,不是下单时的检查 —— 目的是让用户在按下
       「开自动」时就看见还差哪几步,而不是开完之后在某个凌晨静默地花错钱。
    ⚠️ 顺序即引导顺序:先验证自动化点对了地方(real → live),再谈上限。
    """
    out = []
    if not cfg.enabled:
        out.append("跟单本身没开(/copy on)")
    if cfg.paper_only:
        out.append("还在纸上跟单(/copy real)")
    if cfg.dry_run_execute:
        out.append("还在演练模式,不会真的点成交(/copy live)")
    if cfg.amount_usd <= 0:
        out.append("单笔金额是 0(/copy amount <美元>)")
    if cfg.daily_spend_usd is None:
        # 无人值守下"不限"不是一个可接受的选项 —— 见 daily_spend_usd 的注释
        out.append("没设当日金额上限(/copy spend <美元>)")
    elif cfg.daily_spend_usd < cfg.amount_usd:
        # 上限比单笔还小 = 一单都下不了。与其让人半夜查"为什么一单没跟",不如现在说
        out.append(f"当日上限 ${cfg.daily_spend_usd:g} 比单笔 ${cfg.amount_usd:g} 还小")
    return out


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
    # 今天已经花掉多少美元。⚠️ 只统计**真的会出账**的那些状态,
    #    别把演练/已忽略/失败的也算进来 —— 口径不一致会让上限提前封死。
    spent_today: float = 0.0


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

    # ⚠️ 入场市值**无条件必需**,不是只有配了上限才检查。
    #    它同时是筛选闸门和台账成本:拿不到就意味着"不知道在什么价位建的仓" ——
    #    /paper 算不出盈亏,max_entry_mcap 也形同虚设。
    #    与"拿不到币龄就不跟"是同一条原则:宁可漏一单,不可蒙着眼建仓。
    #    (实测缺失率约 2%:本轮 balances 覆盖 84%,够新的快照再补上大半。)
    if c.entry_mcap is None:
        return Decision(False, SKIP_NO_MCAP, age)
    if cfg.max_entry_mcap is not None and c.entry_mcap > cfg.max_entry_mcap:
        return Decision(False, SKIP_MCAP, age)

    # ⚠️ 当日上限放在**最后**:它是"今天不跟了"而不是"这个币不合格"。
    #    放前面的话,达到上限之后所有币的原因都变成"已达当日上限",
    #    看不出哪些币其实本来也不符合条件。
    if cfg.daily_max > 0 and c.taken_today >= cfg.daily_max:
        return Decision(False, SKIP_DAILY, age)

    # ⚠️ 金额上限判"这一单下完会不会超",不是"现在超没超" ——
    #    后者会让最后一单把上限捅穿(上限 $100、已花 $99、单笔 $40 → 放行到 $139)。
    if (cfg.daily_spend_usd is not None
            and c.spent_today + cfg.amount_usd > cfg.daily_spend_usd + 1e-9):
        return Decision(False, SKIP_DAILY_SPEND, age)

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
