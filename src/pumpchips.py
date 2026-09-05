"""
pump.fun 平台筹码 —— /chips 回执里 💊 那半边的**取值与聚合**(不产出任何文案)。

============ 这个模块回答的是哪三个问题 ============
  1. 这个币在 pump 平台上有多少人**托管持仓**?          → totalCount
  2. 这些人合计占总供应量的多少?                        → Σ amountHeld ÷ 总供应量
  3. 我自己的 pump 名单(/pump add 那份)里谁在里面?     → 按 userId 匹配

三件事**各自独立**:任何一件拿不到,只让它自己那一段消失,不牵连另外两件,
更不牵连 /chips 里 🏦 FOMO 那半边与整条回执(见 bot._cmd_chips 的接法)。

============ 只用匿名公开端点,全程不登录 ============
  GET https://frontend-api-v3.pump.fun/mint-positions/{mint}
      ?sortBy=TOP&pageSize=50&updatesLimit=0&page=0
  GET https://frontend-api-v3.pump.fun/coins-v3/{mint}          (分母)

实测:两条都**不需要任何头**,连 curl_cffi 的 impersonate 都不需要。
⚠️⚠️ `/followed-holders/{mint}` 与 `/following-positions/alerts` **要登录**(401),
   本模块一条都不碰;pump 的 NATS WebSocket 需要公共订阅令牌,同样不碰。
   这个功能不使用任何凭据、不调用任何交易/下单接口。

============ ⚠️⚠️ totalCount ≠ 我们能枚举出来的人数 ============
这是本模块最重要的一条实测事实,也是全部文案的支点(2026-09-05 逐个翻页验的):

    microdoge  totalCount=107   翻完只有   1 行(page1 起全空)
    Addog      totalCount=233   翻完只有 214 行
    GMERALD    totalCount=76    翻完只有  74 行
    $CAP       totalCount=430   翻完只有 426 行
    USER(base) totalCount=3478  翻完只有 3475 行

⚠️ 这几个数**录制于 2026-09-05,会变**:同一天晚些时候复测,$CAP 的 totalCount
   已经是 424。夹具(tests/fixtures/pump_mint_positions_robinhood.json)与
   README 里的 430 都是**这一刻的快照**,三处必须是同一个数;
   谁重新录夹具,这里与 README 一起改。

也就是说服务端自报的人数里,有一部分**永远不会出现在明细里**(仓位太小、
账号状态、或者别的我们看不见的过滤)。所以:

  · 人数报 totalCount(那是平台自己的口径,可信);
  · 占比只能由**我们真正拿到明细的那些人**算出来 —— 它是**下界**;
  · 判据只有一条:`covered == total` 时才敢说「持仓 X%」,否则只能说「≥X%」。

这与 /chips 的 🏦 FOMO 半边(bot._chips_exact)是**同一条判据、同一套措辞**,
不是巧合:两半边印在同一条消息里,口径不一致就是在误导。

============ ⚠️⚠️ 两个"持有人数"绝不能混 ============
  `/token-holders/{mint}/count` 的 `holderCount` 是**链上地址数**;
  `/mint-positions.totalCount` 是 **pump 平台托管持仓人数**。
同一个币实测差一个数量级(Hr8CpESJ:链上 178 / 平台 167;65Nt7Tdis:链上 8561 /
平台 2035),而且链上那个**只有 Solana 有**,bsc/base/robinhood/eth 全部 404。
本模块**只用 totalCount**,一个字节都不碰那个端点 —— 拿链上数冒充平台数就是推错信息,
两个数相加或相除更是无中生有。

============ 翻页与预算 ============
翻几页由 totalCount 决定:`ceil(total / 50)`。**不能**用"这一页不满 50 就是最后一页"
当终止条件 —— Addog 的 page0 只有 46 行,page1 还有 45 行(见上表)。

  · totalCount > FULL_SCAN_MAX  → **轻档**:只取前 50 名,文案里明说是下界;
  · 否则                        → 全量翻页,线程池并发 WORKERS;
  · 无论哪一档,整块有一个**墙钟预算** BUDGET_SEC —— /chips 是同步命令,用户在等。
    超了就用已经拿到的部分,并把 partial 标出来让文案说清楚。

⚠️ 线程池里**每线程一个 curl_cffi Session**:libcurl 的 easy handle 不能被多线程
   同时使用(共用是概率性的崩溃/串包)。这一点由 PumpClient._session 的
   threading.local 保证,本模块只负责不去破坏它 —— 绝不把 session 抓出来传进线程。

⚠️⚠️ 限流:这条注释上一版写的是"实测累计 300+ 请求零 429、零封禁",而 2026-09-05
   出了一次真事故 —— 一条 /chips 按当时的策略要发 **61 个请求**(60 页 + 分母)、并发 10,
   约 12 请求/秒持续 5 秒,把 frontend-api-v3.pump.fun 打到**主机级拒连**
   (**不是 429**,是连不上);而 pump 的**推送监控**用的是同一个主机、同一个出口 IP,
   那段时间跟着一起瞎掉。所以"没观察到 429"这句话是对的,却完全不足以说明安全 ——
   这个主机的防护不走 429 那条路。
   现在两道闸一起管:FULL_SCAN_MAX=600(一条命令 ≤ 13 个请求)+ pumpfun 里那把
   **进程级**限速闸(峰值 6.7 请求/秒,命令侧与推送侧共用同一把)。
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from loguru import logger

# ⚠️ 字段解析器复用 pumpfun 里已经踩过坑的那两个:_as_float 判空一律 is None
#    (0 是"已清仓"这个真实值)、还会把 NaN / Inf 过滤掉。在这里另写一份迟早走岔。
from src.pumpfun import MINT_POSITIONS_MAX_PAGE_SIZE, _as_float, _as_text

# 超过这么多人就降级成轻档(只取前 50 名)。
# ⚠️⚠️ **本轮(J2)从 3000 降到 600**,理由是一次真实事故,不是墙钟:
#    3000 对应 ceil(3000/50) = 60 页,加上分母就是**一条同步命令 61 个请求**,
#    并发 10 → 实测 ~12 请求/秒持续 5 秒,把 frontend-api-v3.pump.fun 打到
#    **主机级拒连**(不是 429,是连不上);而 pump 的**推送监控**用的是同一个主机、
#    同一个出口 IP,那段时间里跟着一起瞎掉。
#    「一条命令看得更全」换「推送监控失明」是笔亏本买卖 —— 轻档只是把
#    「仅统计前 50 名」这句诚实的下界说出来,而推送失明是**用户根本不知道**的静默损失。
# ⚠️ 600 = 12 页 + 1 个分母 = **一条 /chips 最多 13 个请求**(旧值是 61)。
#    12 页这个密度 2026-09-05 复测过:串行 + 0.3 秒间隔打 9 页 + 1 个分母,
#    10 个请求 14.3 秒全部 200、零拒连;再叠上 pumpfun 那道进程级限速闸
#    (峰值 6.7 请求/秒),并发档的峰值密度低于出事那次的一半。
# ⚠️ 600 人以上的币会降级成轻档 —— 实测这类币(USER 3478 人、65Nt7Tdis 2035 人)
#    本来也从来没有 exact 过(见模块头那张表:自报人数与明细条数常年对不上),
#    降级损失的是"下界更低",不是"从精确掉成下界"。
FULL_SCAN_MAX = 600
# 翻页并发。⚠️ **本轮(J2)从 10 降到 4**:真正的吞吐上限现在是 pumpfun 那道进程级
#    限速闸(峰值 6.7 请求/秒),再多的线程只会排在闸前面干等,一秒都省不下来,
#    却会在闸出问题时把突发放大回事故那天的量级。
#    4 个线程 × 单请求实测 0.47~3.3 秒,12 页的墙钟在 8 秒预算内仍有余量。
WORKERS = 4
# 整块的墙钟预算(秒)。/chips 是同步命令,用户在等回执 —— 超了就用已经拿到的部分,
# 并在文案里说明是部分结果。⚠️ 它**只管 pump 这一块**:超预算绝不影响
# 🏦 FOMO 那半边(那半边在这之前就已经拼好了)。
BUDGET_SEC = 8.0

# 轻档翻几页。⚠️ 1 不是"随便取的小数",它就是"服务端一页的上限" ——
#    sortBy=TOP 保证这一页正好是**持仓最多的前 50 名**,文案里那句"前 N 名"
#    的依据就在这里。
LIGHT_PAGES = 1


@dataclass(frozen=True)
class MintPosition:
    """/mint-positions 里的一行,只留 /chips 要用的字段。"""

    user_id: str
    # ⚠️ pump 用户名,**攻击者可控**(谁都能把自己的用户名改成 `已清仓 · 亏损 99%`)。
    #    本模块只负责把它原样带出来,门禁在渲染入口(formatter.render_pump_chip_row
    #    的 pump_username → safe_display + 「」容器)。绝不在这里"顺手清洗一下" ——
    #    那正是本项目上一轮的教训:清洗散落在各处 = 迟早漏一个。
    user_name: str | None
    amount_held: float | None
    pnl_pct: float | None

    @property
    def holds_now(self) -> bool:
        """
        现在还持有吗。

        ⚠️⚠️ `amountHeld == 0` 是**已清仓**,不是"还持有 0 枚"。把它算成持有会让
           「你的名单 · 1 人持有」指着一个早就卖光的人(实测夹具里就有
           amountHeld=0.001 这种粉尘残仓,再往下就是 0)。
        ⚠️ 判空用 is None:拿不到数量 = 不知道,同样不能声称他在持有。
        """
        return self.amount_held is not None and self.amount_held > 0


@dataclass(frozen=True)
class PumpChips:
    """一个币在 pump 平台上的筹码分布。纯数据,不含任何文案。"""

    # 平台自报的托管持仓人数。⚠️ 拿不到 → 整个 💊 块不出现(调用方据此判断)
    total: int | None
    # 我们**真正拿到明细**的人数。⚠️ 它与 total 常常不等,见模块头那张表
    covered: int
    # covered == total,也就是"这就是全部" —— 只有为真才敢说「持仓 X%」
    exact: bool
    # 该翻的页都翻完了(没降级成轻档、也没撞墙钟预算)
    full_scan: bool
    # 撞了墙钟预算,手上只是一部分页
    partial: bool
    # 名单命中,按持仓数量从多到少。[{user_id, name, amount, pnl_pct}]
    matched: list[dict] = field(default_factory=list)
    # 平台侧占比(百分数)。分子是**已拿到明细**那些人的合计,分母是总供应量
    plat_pct: float | None = None
    # 名单侧占比(百分数)。与平台侧**同源同口径**,两个数可以直接相比
    watch_pct: float | None = None
    # 分母**自相矛盾**(已统计的持仓比总供应量还多),两个占比都不显示。
    # ⚠️ 它与"分母压根没拿到"是两件事:前者要告诉用户"这个数对不上",
    #    后者只是"没拿到"。文案不同,见 bot._pump_chips_platform_lines。
    bad_supply: bool = False
    # ⚠️⚠️ **我们这边**有几页没取到(请求挂了 / 限速闸没排上 / 响应结构不对)。本轮 J3 新增。
    #    上一版把这件事**吞掉不留痕**,于是"我们自己的请求失败"被归因成
    #    "平台只给出 N/M 人的明细" —— 那是在**编原因**:读者据此以为
    #    "再查也没用,平台就这么多",而真相是再查一次很可能就全了。
    # ⚠️ 它同时是「前 N 名内无人」那句话的**否决位**:页失败时我们手上这批人
    #    根本不是一个连续的"前 N 名"(第 1 页挂了、第 2 页拿到了),
    #    那句话在这种情况下是**假陈述**。见 bot._pump_chips_watch_lines。
    failed_pages: int = 0
    # 这一块**计划**发出的请求数(第一页 + 分母 + 要翻的页)。
    # ⚠️ 只用于报告与日志,**不进文案**;撞预算时还没开跑的那几个会被取消,
    #    所以它是上界而不是实发数 —— 别拿它当计费依据。
    requests: int = 1


# ============================================================
# 解析(纯函数)
# ============================================================
def parse_mint_positions(payload) -> tuple[int | None, list[MintPosition]] | None:
    """
    一页 /mint-positions 响应 → (totalCount, [MintPosition])。**结构不对返回 None**。

    ⚠️ None 与 (0, []) 是两件完全不同的事:
       前者 = "我们不知道"(请求失败 / 响应不是我们认识的形状)→ 💊 整块不出现;
       后者 = "平台上真的一个人都没有"(实测 200 `{"positions":[],"totalCount":0}`)
              → 照实显示「持有人 0」。混成一件就是把"没查到"谎报成"没有"。
    ⚠️ totalCount 缺失但 positions 有内容时仍然可用(total=None),下游据此
       只报"至少 covered 人",不硬造一个总数。两者**都**没有才是 None。
    ⚠️ 缺 userId 的行直接丢:名单匹配没有键、去重也没有键。实测一行都没缺过。
    """
    if not isinstance(payload, dict):
        return None
    raw_total = payload.get("totalCount")
    total = (int(raw_total) if isinstance(raw_total, (int, float))
             and not isinstance(raw_total, bool) else None)
    raw_rows = payload.get("positions")
    rows = raw_rows if isinstance(raw_rows, list) else []
    out: list[MintPosition] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped += 1
            continue
        uid = _as_text(row.get("userId"))
        if uid is None:
            dropped += 1
            continue
        out.append(MintPosition(
            user_id=uid,
            user_name=_as_text(row.get("userName")),
            amount_held=_as_float(row.get("amountHeld")),
            pnl_pct=_as_float(row.get("pnlPercentage")),
        ))
    if dropped:
        logger.warning("pump mint-positions 有 {} 行缺 userId,已跳过(本页共 {} 行)",
                       dropped, len(rows))
    if total is None and not out:
        return None
    return total, out


def parse_supply(payload) -> float | None:
    """
    /coins-v3/{mint} 响应 → 总供应量(人类单位)。拿不到返回 None(占比那段消失)。

    ⚠️⚠️ 必须除以 10**base_decimals:`total_supply_str` 是**最小单位**的整数串
       (实测 $CAP = 1e27,base_decimals=18 → 真实供应量 1e9;Solana 上的币
       是 1e15 / decimals 6 → 1e9)。不除的话占比会小十八个数量级,
       直接压成 0.0000%,看起来像"没人持有"。
    ⚠️⚠️ **非 pump 上架的币这里返回的是字面 `null`**(HTTP 200,响应体就四个字节)——
       它只是被 pump 用户持有的外部币,pump 自己没有它的元数据。
       这时占比整段消失,人数照显示,而且文案要说清楚"为什么没有占比",
       绝不能让用户以为是我们算出来 0。
    ⚠️ 供应量 ≤ 0 一律当没拿到:0 会让占比除零,负数是脏数据。
    ⚠️ 用 Decimal 走一趟?不必 —— total_supply_str 最大也就 1e27 量级,
       float 表示得下(2^53 之外只丢末几位有效数字,而占比只保留到小数点后四位)。
    """
    if not isinstance(payload, dict):
        return None
    supply = _as_float(payload.get("total_supply_str"))
    decimals = _as_float(payload.get("base_decimals"))
    if supply is None or decimals is None:
        return None
    if decimals < 0 or decimals > 36:      # 明显是脏数据,别拿它去做 10**x
        return None
    value = supply / (10.0 ** int(decimals))
    return value if value > 0 else None


# ============================================================
# 聚合(纯函数)
# ============================================================
def _sum_amounts(rows: list[MintPosition]) -> float | None:
    """
    持仓数量求和,**一个都没解析出来时返回 None 而不是 0**。

    ⚠️ sum([]) == 0 会让"字段全缺"和"加起来真的是 0"变成同一个值,下游据此
       打出一个 0.000% 的占比 —— 那是凭空造出来的假事实。
       (与 bot._chips_sum 同一条规矩;两半边的口径必须一致。)
    """
    vals = [r.amount_held for r in rows if r.amount_held is not None]
    return sum(vals) if vals else None


def _ratio(amount: float | None, supply: float | None) -> float | None:
    """占比(百分数)。任何一半缺失就返回 None,让那一段整段消失 —— 绝不用 0 顶替。"""
    if amount is None or supply is None or supply <= 0:
        return None
    return amount / supply * 100.0


def supply_usable(plat_amount: float | None, supply: float | None) -> bool:
    """
    这个分母**可信吗**。不可信 → 两个占比都消失,人数与名单命中照常。

    ⚠️⚠️ 判据是一条**不可能为真的算式**:我们统计到的持仓合计是**全部持有人的一个子集**,
       它绝不可能超过总供应量。超了只有一个解释 —— 分母不是我们以为的那个数。
    ⚠️⚠️ 这不是防御性编程,是 2026-09-05 真网络实测打出来的:
       `$PUMP`(pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn)的 coins-v3 给
       total_supply_str=399,462,335,624,000,000 / base_decimals=6 → 399.46B,
       而**光是前 49 名**的 amountHeld 合计就有 588.75B。算出来是「持仓 147.4%」——
       一个一眼就假的数字。($PUMP 的真实总量是 1T,那个字段给的显然不是总量。)
    ⚠️ 一条一眼假的信息比没有这一行糟得多(铁律 2 的同一条精神):宁可不报占比,
       也不能报一个 147% 的"占比"。同样的单位错配守卫在 formatter._pump_mcap_line
       上也有一份,是本项目的既有做法。
    ⚠️ 恰好相等仍然算可信:一个人持有全部供应量是真实可能发生的(刚发的币)。
    """
    if plat_amount is None or supply is None or supply <= 0:
        return False
    return plat_amount <= supply


def _match_members(rows: list[MintPosition], members: dict[str, str]) -> list[dict]:
    """
    从持仓明细里认出 pump 名单成员。返回 [{user_id, name, amount, pnl_pct}],按数量降序。

    ⚠️⚠️ 匹配键是 **mint-positions[].userId ↔ pump_watch_users.user_id**
       (两边就是同一套 UUID,实测逐个对得上,不用做钱包映射)。
       **绝不按 userName 匹配**:用户名本人随时可以改,而且谁都能把自己的用户名
       改成名单里某个人的旧名字 —— 按名字匹配会同时产生"改过名的人认不出来"(漏)
       与"陌生人冒名顶替被算进名单"(错)两类错误。这与 bot._chips_match_members
       是同一条教训(store.normalize_handle 存在的全部理由)。
    ⚠️ 已清仓的行(amountHeld == 0)不算命中,见 MintPosition.holds_now。
    ⚠️ 展示名优先用**本地名单里存的那个**(与 🏦 FOMO 半边用本地 handle 同一条理由:
       同一个人在同一条回执的两半边必须是同一个名字),本地没有再退回接口给的
       userName。两者都是攻击者可控的自由文本,**都**在渲染入口过 safe_display。
    """
    out: list[dict] = []
    for r in rows:
        if r.user_id not in members or not r.holds_now:
            continue
        out.append({"user_id": r.user_id,
                    "name": members[r.user_id] or r.user_name,
                    "amount": r.amount_held,
                    "pnl_pct": r.pnl_pct})
    out.sort(key=lambda m: (m["amount"] is not None, m["amount"] or 0.0), reverse=True)
    return out


def _dedupe(rows: list[MintPosition]) -> list[MintPosition]:
    """
    按 userId 去重,先到的那条留下。

    ⚠️ 翻页期间数据会动,同一个人可能在两页里各出现一次(排序键是持仓量,
       别人买入就会把他往后挤)。不去重的话他的持仓被算两遍,占比凭空变大。
    """
    seen: set[str] = set()
    out: list[MintPosition] = []
    for r in rows:
        if r.user_id in seen:
            continue
        seen.add(r.user_id)
        out.append(r)
    return out


# ============================================================
# 取值(带并发与墙钟预算)
# ============================================================
def _page(client, mint: str, page: int) -> list[MintPosition] | None:
    """
    翻一页。**成功返回行(可能是 0 行),失败返回 None** —— 两者绝不能混。

    ⚠️⚠️ 上一版失败时返回 `[]`,于是"我们这边挂了"与"平台这一页真的没人"
       变成同一个值,**不留任何痕迹**。下游的 _pump_coverage_warn 因此把
       第四种原因静默并进第三种,打出「平台只给出 N/M 人的明细」——
       那句话在页失败时是**编出来的原因**(它自己的 docstring 写着
       "把三件事写成同一句就是在编原因")。
    ⚠️ 失败仍然**不炸掉整块**:调用方把 None 记成一页没取到,少几个人只是占比更低。
    ⚠️ `parse_mint_positions` 返回 None(响应结构不对 / 客户端返回 None,包括
       限速闸没排上)同样算"没取到" —— 从读者的角度它与请求挂了是同一件事。
    """
    try:
        parsed = parse_mint_positions(client.fetch_mint_positions(mint, page))
    except Exception as e:  # noqa: BLE001
        logger.warning("pump mint-positions 第 {} 页失败(记成一页没取到): {}", page, e)
        return None
    if parsed is None:
        logger.warning("pump mint-positions 第 {} 页响应不可用(记成一页没取到)", page)
        return None
    return parsed[1]


def _supply(client, mint: str) -> float | None:
    """
    分母那一路。**整段吞异常**:它与人数/明细是两个互相独立的来源,
    分母挂掉只该让占比消失,绝不能把已经拿到手的人数一起拖走。
    """
    try:
        return parse_supply(client.fetch_coin_payload(mint))
    except Exception as e:  # noqa: BLE001
        logger.warning("pump coins-v3 取总供应量失败(占比这一段消失): {}", e)
        return None


def fetch_chips(client, mint: str, members: dict[str, str], *,
                workers: int | None = None, budget_sec: float | None = None,
                full_scan_max: int | None = None) -> PumpChips | None:
    """
    一个 mint → 它在 pump 平台上的筹码分布。**拿不到 totalCount 就返回 None**
    (调用方据此让整个 💊 块消失,回执与接这个功能之前一模一样)。

    ⚠️⚠️ 三段各自独立,写在这里而不是靠调用方记得:
       · 人数     —— 第一页拿到就有,后面全挂也不影响它;
       · 占比     —— 分母(coins-v3)自己一条 try,拿不到就是 None;
                     分子是"已拿到明细的人"的合计,一页都没拿到时是 None 而不是 0;
       · 名单命中 —— 只依赖明细;明细恒空时 matched 是空**而且** covered==0,
                     调用方据此说"判断不了"而不是"没人"。
    ⚠️⚠️ 墙钟预算是**整块**的,而且**从第一页就开始管**(本轮 J4 修):
       上一版 deadline 虽然在第一页之前就设好了,第一页却是**同步发出、不受预算约束**的,
       deadline 要等它返回之后才第一次被检查 —— 于是生产上界其实是
       pumpfun._TIMEOUT_SEC(20 秒 curl 超时),不是这里承诺的 8 秒。
       实测:预算 0.5 秒、第一页耗时 3 秒 → 整整等满 3.00 秒。
       现在第一页也走线程池 + result(timeout=…),超预算就当"这个币这次没查到"
       (返回 None → 💊 整块不出现),绝不让一条同步命令替 curl 数到 20。
    ⚠️ 线程池只传 client(它内部按 threading.local 每线程建一个 Session),
       绝不把 Session 抓出来跨线程共用 —— libcurl 的 easy handle 不能这么用。
    """
    # ⚠️ 三个策略参数**在调用时**才取模块常量,不写成默认参数值 ——
    #    默认参数在 def 那一刻就绑死了,`monkeypatch.setattr(pumpchips, "BUDGET_SEC", …)`
    #    对它一点作用都没有(实测:预算那条端到端用例当场证明了这一点)。
    #    一个"改了不生效"的常量比没有常量更糟:读代码的人会以为它管用。
    workers = WORKERS if workers is None else workers
    budget_sec = BUDGET_SEC if budget_sec is None else budget_sec
    full_scan_max = FULL_SCAN_MAX if full_scan_max is None else full_scan_max

    deadline = time.monotonic() + budget_sec
    requests = 1
    failed_pages = 0
    partial = False
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        # ⚠️⚠️ 第一页也走池子 + 预算(J4)。它同步发出去的那一版等于把上界交给了
        #    curl 的 20 秒超时,而这里承诺的是 8 秒。
        try:
            first = parse_mint_positions(
                ex.submit(_first_page, client, mint).result(
                    timeout=max(0.0, deadline - time.monotonic())))
        except Exception:  # noqa: BLE001
            logger.warning("pump mint-positions 第一页没在 {} 秒预算内拿到"
                           "(💊 整块不显示) | {}", budget_sec, mint[:16])
            return None
        if first is None:
            return None                # 这个币根本不在 pump 上 / 请求挂了 → 整块不出现
        total, rows = first

        want_pages = 1
        if total is not None and total > 0:
            want_pages = math.ceil(total / MINT_POSITIONS_MAX_PAGE_SIZE)
        light = total is not None and total > full_scan_max
        if light:
            want_pages = LIGHT_PAGES

        # 分母与第 1..n-1 页一起丢进同一个池子:它们互不依赖,串行只是白等一个往返。
        jobs: list = []
        if want_pages > 1:
            jobs = list(range(1, want_pages))
        fut_supply = ex.submit(_supply, client, mint)
        requests += 1
        futs = {}
        if time.monotonic() >= deadline and jobs:
            # 第一页就把预算吃光了 —— 一页都不再发,如实标 partial
            partial = True
            jobs = []
        for p in jobs:
            futs[ex.submit(_page, client, mint, p)] = p
        requests += len(futs)
        if futs:
            try:
                for fut in as_completed(futs, timeout=max(0.0, deadline - time.monotonic())):
                    got = fut.result()
                    # ⚠️ None = 这一页**我们这边**没取到(见 _page)。留痕,不当空页。
                    if got is None:
                        failed_pages += 1
                    else:
                        rows.extend(got)
            except TimeoutError:
                partial = True
                logger.warning("pump 筹码翻页超出 {} 秒预算,用已拿到的部分 | {}",
                               budget_sec, mint[:16])
        try:
            supply = fut_supply.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001
            supply = None
            logger.warning("pump 总供应量没在预算内拿到,占比这一段消失 | {}", mint[:16])
    finally:
        # ⚠️ cancel_futures=True + wait=False:超预算之后还没开跑的任务直接取消,
        #    已经在跑的让它自己结束(curl 自带 20 秒超时),绝不在这里等它。
        ex.shutdown(wait=False, cancel_futures=True)
    if failed_pages:
        logger.warning("pump 筹码有 {} 页我们这边没取到(文案里如实说出来) | {}",
                       failed_pages, mint[:16])

    rows = _dedupe(rows)
    covered = len(rows)
    # ⚠️⚠️ exact **必须在覆盖 total 之前算好**(与 bot._chips_stats 同一条,那边也踩过):
    #    服务端自报的总数比我们数出来的还少 = 这份数据自相矛盾。以**手上真有的条数**
    #    为准(我们数得出来的比它的自报更可信),但**绝不因此声称精确** ——
    #    先覆盖再比较的话 covered == total 恒成立,每一条自相矛盾的响应
    #    都会被渲染成「持仓 X%」这种"精确"口径。
    exact = total is not None and covered == total
    if total is not None and total < covered:
        total = covered
    matched = _match_members(rows, members)
    plat_amount = _sum_amounts(rows)
    # ⚠️⚠️ 分母不可信时**两个占比一起消失**(不是只掉平台那个):它们共用同一个分母,
    #    留下名单那个等于用一个已知有问题的数去报另一个数。人数与名单命中不受影响。
    # ⚠️ 分子是 None(一条明细都没拿到)时**判不了**分母可不可信 —— 那不是矛盾,
    #    只是没东西可比。少了这一半守卫,每一个"有人数、明细恒空"的币都会被
    #    误判成"分母对不上"(实测 SHFL 就走了这条路)。
    bad_supply = (supply is not None and plat_amount is not None
                  and not supply_usable(plat_amount, supply))
    if bad_supply:
        logger.warning("pump 给的总供应量({})比已统计的持仓({})还少,占比不显示 | {}",
                       supply, plat_amount, mint[:16])
        supply = None
    return PumpChips(
        total=total,
        covered=covered,
        exact=exact,
        full_scan=not light and not partial,
        partial=partial,
        matched=matched,
        plat_pct=_ratio(plat_amount, supply),
        watch_pct=_ratio(_sum_amounts([r for r in rows if r.user_id in members
                                       and r.holds_now]), supply),
        bad_supply=bad_supply,
        failed_pages=failed_pages,
        requests=requests,
    )


def _first_page(client, mint: str):
    """第一页。⚠️ 这一路的异常也要吞掉:调用方只该看到 None,不该看到栈。"""
    try:
        return client.fetch_mint_positions(mint, 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("pump mint-positions 第一页失败(💊 整块不显示): {}", e)
        return None
