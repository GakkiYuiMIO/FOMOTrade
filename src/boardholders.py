"""
🏅 盈利榜持有人 —— 「这个币的持有人里,有谁正挂在 FOMO 24h 盈利榜上」。

推送里长这样(命中 2 人、持有人被截断时):

    🏅 盈利榜持有人 ≥2 人
       #28 「frankdegods」 · 11,020,000 枚 · 粉丝 218,710 · 全平台24h +$323.15K
       #118 「cryptoalbus」 · 20,170,000 枚 · 粉丝 2,150 · 全平台24h +$122.86K
       ⚠️ 只比对了前 97/15,305 名持有人、榜只到前 150 名 —— 没显示≠没有

一个都没命中 → **整块不出现**(绝不打「0 人」:那是一句"我们查过了、确实没有"的
断言,而我们手上只有前 ~100 名持有人 × 榜前 150 名,根本没有资格这么说)。

============ 数据来自哪儿(两个端点,都要登录态)============
■ 榜单  GET /v2/leaderboard/24h?limit=150   (client.get_leaderboard)
    ⚠️⚠️ 真实信封比别的端点**多套一层**(2026-09-06 实测):
       `responseObject` 是 dict、里面只有一个键 `leaderboard`,值才是那 150 行。
       解包由 client._unwrap → _as_list 兜住;它一坏,这一块**静默永不出现**。
    ⚠️⚠️ 服务端真实上限 **150**,不是 100:limit=150 → 150 行;
       151/160/175/199/500/1000 一律**静默返回 150**(不报错)。
       client 上一版把它夹在 100,于是第 101~150 名永远看不见 —— 本轮放开了,
       并在 client.LEADERBOARD_MAX_LIMIT 上把那条错抄的注释改正了。
       实测代价立竿见影:base 上的 EVAL 命中的两人是 **#124 / #134**,
       robinhood 上的 CLEAT 命中的是 **#144** —— 夹在 100 时这三个人一个都看不到。
    ⚠️ **无分页**:offset / page / skip / cursor 四种参数实测全部被静默忽略
       (四种都原样返回第 1 页)。150 就是这个榜的全部。
    ⚠️⚠️ **排名 = 返回顺序的 1-based 下标**。响应里**没有** rank 字段 ——
       别去找,也别改成 0-based(那会让每个人的名次都少 1)。
    ⚠️ 24h 榜带的盈亏字段叫 **pnl24h**(7d 榜是 pnl7d、30d 榜是 pnl30d)。
       实测 150/150 行都有值且全部为正(最小 +$162,095),所以"盈利榜"名副其实。

■ 持有人  GET /hodlers/top   (client.get_top_holders)
    ⚠️⚠️ networkId **必须是数字**:传 "robinhood" 服务端直接 400
       (`Expected number, received nan`)。本模块用 models.NETWORK_CHAIN_ID 转,
       认不出的链一个请求都不发。
    ⚠️ 条数硬钳 100、**没有分页**、还叠了一道约 $2 的持仓市值下限
       (见 client.EP_TOP_HOLDERS 上面那一大段)。实测返回条数在 68~100 之间浮动。

============ ⚠️⚠️ 两个下界(这一块全部文案的支点)============
a. **持有人只看得到前 ~100 名**:实测 robinhood 的 MEME `totalHolders=15305`、
   返回 **97** 条。所以「盈利榜持有人 N 人」是在**这 97 名持有人里**数出来的,
   不是全体 15305 人。
b. **榜只有前 150 名**:第 151 名开外的盈利大户,即使他就在持有人里也认不出来。
⇒ 两条合起来:这个数只可能**偏小**,是个**下界**。文案必须让读者读到
  「没显示 ≠ 没有」,而不是「查过了,只有这 2 个」。
⚠️ `len(topHolders) == totalHolders` 时(a)消失,这时换**精确**那套写法 ——
   与 bot._chips_exact 同一条判据、同一套"两眼可辨"的措辞。
   实测样本:robinhood 的 CLEAT 87/87 精确、Gmonad 1/1 精确;
   而 MEME 97/15305、PONS 100/28415 都是下界。

============ ⚠️⚠️ 绝不拿币上的盈亏冒充全平台 24h ============
`topHolders[].pnl` / `unrealizedPnl` / `realizedPnl` 是**这个币上的**盈亏,
`holder.user` 里**没有** pnl24h / pnl7d / pnl30d(实测 user 的键只有
activated/address/clan/…/followers/…/totalVolume/twitter/userHandle/verified)。
全平台 24h 盈亏**只有排行榜里有**。把币上的 pnl 印成「全平台24h」是一条
彻头彻尾的错误信息(实测 MEME 第一名:全平台24h **+$6.00M**,而他在这个币上
**-$125,514**,符号都是反的)。有一条测试用 AST 钉死本模块不许读那三个键。

============ 截图里那两行为什么**不做** ============
用户给的样例还有两行:「Top10 全平台24H PnL: +$500.31K」与「盈利 7 人」。
两行都需要**任意用户**的全平台 24h 盈亏,而上面已经说清:那个数只有榜上前 150 名才有。
对前 10 大持有人求和,实际只能加上"恰好在榜上的那 0~3 个人"。
⚠️⚠️ 决定:**不做**。理由不是"不够准",而是**它连个界都不是**:
   榜是按 24h 盈利排的前 150,所以掉出榜的人满足 pnl24h ≤ 榜末(+$162,095),
   但**下不封底** —— 他可能是 -$2,000,000。于是"榜上那几个人的和"既不是
   真实 Top10 和的上界、也不是下界,是一个**方向都说不出来**的数。
   标一句"仅榜上 N/10 人"也救不了它:读者拿到的仍然是一个不能用的数字,
   而它长得像一个能用的数字 —— 那正是 §10.3 说的"错的比没有更糟"。
   「盈利 N 人」同理(榜外的人是赚是亏我们一无所知)。
   我们能诚实说出口的就是这一块本身:**这几个人,各自的名次与全平台 24h 盈亏**。

============ 请求预算(这是**推送路径**,比 /chips 严得多)============
■ 榜单是**全局的**(与币无关)→ **进程级**缓存,一次拉 150 行给所有推送共用。
  TTL = _BOARD_TTL_SEC(300 秒)。取 5 分钟的理由:24h 榜是一个 24 小时滚动窗口,
  前 150 名的集合是**十分钟量级**才会换人的量;而推送侧 15~27 秒一个 tick,
  5 分钟 TTL 把榜单请求压到 ≤12 次/小时。失败另记一个更短的负缓存
  (_BOARD_ERROR_TTL_SEC = 60 秒),免得一次抖动把这一块按住整整 5 分钟。
  ⚠️ 闸做成**进程级单例**(与 tokeninfo._GATE 同一条理由):poller 与 pumpfun
     各持一个 lookup 实例,实例级缓存等于把请求数悄悄翻倍。
■ 持有人是**按币的** → 每条推送 1 个请求,并且有
    · 每 tick 次数上限 _HOLDERS_PER_ROUND(6);
    · 每 tick 墙钟闸 _ROUND_WALL_CLOCK_SEC(6.0 秒,把榜单那次也算进去,
      而且**拉榜那一次在发出去之前**就要过这道闸)。
  任一触发 → 后面的币这一块不显示,**绝不阻塞推送**。
■ ⚠️⚠️ 这一块的两个请求都走 client 的**装饰性策略**(fast_fail=True):
  **单次尝试、5 秒超时、不按 Retry-After 睡、不退避重试**,见
  client._DECORATIVE_TIMEOUT_SEC。上一版复用主路径那套重试机(3 次尝试 / 12 秒超时 /
  429 按服务端 Retry-After 退避、夹在 60s),离线量化的最坏值是:
      429 + Retry-After: 60 → 请求 3 次,sleep=[62.49, 41.78] 合计 104.27s,
                              叠上 3×12.0s 连接超时 = **单个请求阻塞 140.3 秒**;
      503                  → sleep=[1.53, 3.48] 合计 5.01s + 3×12s 超时。
  而这一段是**同步**跑在 poller._dispatch 发消息**之前**的。
  ⇒ 现在这一整块每 tick 的最坏阻塞 = 墙钟预算 6.0s + 一次超时 5.0s = **11.0 秒**,
    有测试钉住。失败就是这一块本轮不显示 —— 下一 tick(15~27 秒)会再来。
  ⚠️ 例外:FOMO_CLIENT_IMPL=playwright 时,某个线程**第一次**发请求还要付
     chromium 冷启动(page.goto 的超时是 60s)。这条如实记在 README「已知代价」。
■ ⚠️⚠️ 榜单那把 single-flight 闸是 **acquire(blocking=False)**:拿不到就说明
  别的 job 正在拉,本轮这一块直接不显示 —— **绝不等**(一条装饰行不配让另一个
  job 等),也**绝不写负缓存**(拿不到锁 ≠ 上游挂了)。见 _BoardCache。
■ 转入类推送(/tin 逐条)走 `cached()`:**只读内存缓存、一个请求都不发**。
  ⚠️ 如实记下代价(与 tokeninfo 的 H6 同一条):持有人响应的内存缓存只有 90 秒,
     一条转入基本不可能正好命中 —— 所以**转入推送实际上几乎永远没有这一块**。
     刻意不落库:持有人榜是快变量,印一个几小时前的名次就是印一句假话。

============ ⚠️⚠️ 401/403 绝不处置登录态 ============
两个请求都传 `auth_invalidate=False`(见 client._fetch_ok):401/403 时
**不调 tokens.invalidate()、不重试**,直接抛 AuthError 回来,由本模块吞成
"这一块本轮不显示"。理由:这一块是推送里可有可无的一行,而登录态是全进程共用的 ——
让一行装饰去把好端端的 access token 标记失效(甚至打出"请重新 --login" 的告警),
是拿主路径的命赌装饰。真过期时主路径(swaps / feed)自己会 401 → 自己续期,
下一轮这一块就自愈了。

⚠️⚠️ 本模块**对外的每一个方法都不抛异常**:任何失败(超时 / 4xx / 5xx / JSON 坏 /
   结构不对 / 登录态问题)都只让这一块整块消失,推送的其余部分一字不动。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import NamedTuple

from loguru import logger

from src.models import NETWORK_CHAIN_ID, normalize_token_address

# 用哪个榜。⚠️ 周期与盈亏字段**必须成对改** —— 24h 榜里只有 pnl24h,
#    改成 7d 却还读 pnl24h 会得到一整列 None(那一段静默消失,没有任何报错)。
BOARD_PERIOD = "24h"
BOARD_PNL_FIELD = "pnl24h"
# 一次拉多少行。⚠️ 服务端真实上限就是 150(见模块头),别再夹回 100。
BOARD_LIMIT = 150

# 榜单进程级缓存的两档 TTL(成功 / 失败),理由见模块头「请求预算」。
_BOARD_TTL_SEC = 300.0
_BOARD_ERROR_TTL_SEC = 60.0

# 每 tick 最多为几个币查持有人 + 整轮墙钟上限(与 tokeninfo / namecn 同一套形状)。
_HOLDERS_PER_ROUND = 6
_ROUND_WALL_CLOCK_SEC = 6.0

# 持有人响应的内存缓存(只给 cached() 那条只读路径兜底,见模块头)。
_HOLDERS_TTL_SEC = 90.0
_HOLDERS_CACHE_MAX = 2000

# 一条推送最多列几个人。
# ⚠️⚠️ 这里曾经写着"实测一个币最多命中过 **31** 人(robinhood 的 PONS)",而同一轮的
#    报告表里写的是 **42** —— 两个数对不上,而且**两个都无法从仓库里复现**
#    (那次探测没有留下夹具)。所以本轮把口径统一成"仓库里能证明的那个数":
#    committed 的 8 份真实 /hodlers/top 夹具里,命中人数最多的是 solana 的 STONK,
#    **13 人**(见 tests/test_boardholders.py::test_三条链各自都能对上)。
#    13 行铺开已经能把主体挤下屏幕,所以 MAX_ROWS 的判据本来就不是那个最大值,
#    而是"这一块是推送的**配角**"。多出来的用「另有 N 人在榜」一句带过
#    (那句话的数字仍是命中总数、仍是真值)。
#    ⚠️ 别再往这条注释里写一个没有夹具兜底的"实测最大值"。
MAX_ROWS = 3


class BoardRow(NamedTuple):
    """榜单里的一行。rank 是**返回顺序的 1-based 下标**(接口不给 rank 字段)。"""

    rank: int
    handle: str | None
    followers: int | None
    pnl24h: float | None


@dataclass(frozen=True)
class BoardBlock:
    """
    一个币的这一整块。rows 已按名次升序,**未截断**(截断在 render_args 里做,
    这样「N 人」那个数字永远是命中总数,而不是"我们列了几行")。
    """

    rows: tuple[tuple, ...]        # (rank, handle, amount, followers, pnl24h)
    covered: int                   # 这次比对了多少名持有人 = len(topHolders)
    total: int | None              # 服务端自报的 totalHolders;没给就是 None
    exact: bool                    # covered == total → 持有人这一侧是全量
    board_size: int                # 榜单实际行数(实测 150)

    def render_args(self) -> dict:
        """
        → render / render_pump_trade / render_transfer_in_watch 的两个关键字参数。

        ⚠️ 零命中返回 **{}**:调用方拿不到键,那一块整块消失(绝不打「0 人」)。
        ⚠️ board_scope 里的 hits 是**命中总数**,与 board_holders 的行数**故意不同** ——
           行数被 MAX_ROWS 截断,而那句「N 人」说的是真值。
        """
        if not self.rows:
            return {}
        return {
            "board_holders": self.rows[:MAX_ROWS],
            "board_scope": (len(self.rows), self.covered, self.total,
                            self.exact, self.board_size),
        }


# ============================================================
# 解析(纯函数,可脱网完整单测)
# ============================================================
def _num(v):
    """→ float;bool / 非数字 → None(0 是真实值,照常返回)。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _count(v) -> int | None:
    """粉丝数 → int。⚠️ 0 是真实值(新号确实可能 0 个粉丝),照常返回;负数 → None。"""
    n = _num(v)
    if n is None or n < 0:
        return None
    try:
        return int(n)
    except (ValueError, OverflowError):
        return None


def parse_board(payload) -> dict[str, BoardRow]:
    """
    榜单响应 → {userId: BoardRow}。

    ⚠️⚠️ **rank 是列表下标 + 1**,而且**每一项都占一个名次** —— 解析不出 id 的那一项
       也要把名次消耗掉,否则它后面所有人的名次都会整体前移一位。
    ⚠️ 认不出 id 的项不进索引(没法与持有人对齐),但名次照样消耗。
    """
    out: dict[str, BoardRow] = {}
    items = payload if isinstance(payload, list) else []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        uid = item.get("id")
        if uid is None or not str(uid).strip():
            continue
        handle = item.get("userHandle")
        out[str(uid)] = BoardRow(
            rank=i + 1,
            handle=handle if isinstance(handle, str) else None,
            followers=_count(item.get("followers")),
            # ⚠️⚠️ 只从**榜单行**读 pnl24h。持有人行里的 pnl 是**这个币上的**盈亏,
            #    是另一个量(见模块头),绝不许在这里出现。
            pnl24h=_num(item.get(BOARD_PNL_FIELD)),
        )
    return out


def match_block(board: dict[str, BoardRow], data, board_size: int) -> BoardBlock | None:
    """
    榜单索引 + 一份 /hodlers/top 响应 → BoardBlock;一个都没命中 → **None**(整块消失)。

    ⚠️⚠️ 对齐键是 **topHolders[].user.id ↔ 榜单行的 id**(两边是同一套 UUID,实测直接
       等值匹配得上,不用做地址映射)。**绝不按 handle 匹配** —— handle 随时可改、
       大小写还不稳定,按它匹配会同时产生"改过名的认不出"与"陌生人占了旧 handle
       被算成同一个人"两类错误(store.normalize_handle 存在的全部理由)。
    ⚠️ 同一个人在 topHolders 里出现两次(不同 tradeId)只算一次 —— 取名次靠前那条
       的第一次出现,持仓数量用**第一次出现**那条(它是按持仓市值降序的头一笔)。
    """
    if not isinstance(data, dict):
        return None
    holders = [h for h in (data.get("topHolders") or []) if isinstance(h, dict)]
    covered = len(holders)
    total = _count(data.get("totalHolders"))
    # ⚠️ 服务端自报的总数比它自己给的条数还少 —— 这份响应自相矛盾。以**手上真有的
    #    条数**为准(我们数得出来的比自报更可信),但**绝不因此声称精确**:
    #    exact 在覆盖之前就已经算好(与 bot._chips_stats 同一条处置)。
    exact = total is not None and covered == total
    if total is not None and total < covered:
        total = covered

    rows: list[tuple] = []
    seen: set[str] = set()
    for h in holders:
        user = h.get("user")
        if not isinstance(user, dict):
            continue
        uid = user.get("id")
        if uid is None:
            continue
        key = str(uid)
        if key in seen:
            continue
        row = board.get(key)
        if row is None:
            continue
        seen.add(key)
        rows.append((row.rank, row.handle, _num(h.get("humanAmount")),
                     row.followers, row.pnl24h))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return BoardBlock(rows=tuple(rows), covered=covered, total=total,
                      exact=exact, board_size=board_size)


# ============================================================
# 榜单的**进程级**缓存
# ============================================================
class _BoardCache:
    """
    「24h 榜 150 行」的进程级单例缓存。

    ⚠️⚠️ 做成模块级单例而不是每个 lookup 一个:poller 与 pumpfun 各持一个
       BoardHoldersLookup,实例级缓存等于把这个**与币无关**的全局请求打两遍。
    ⚠️ 失败也进缓存(更短的 TTL):一次超时不该让后面每一条推送都再去试一次,
       那正好是上游抖动时最不该做的事。
    ⚠️ 拿不到就返回 None,调用方那一块整块消失。**绝不抛。**

    ============ ⚠️⚠️ 两把锁,而且**锁内绝不做网络请求** ============
    上一版把 `client.get_leaderboard(...)` 整个放在 `with self._lock:` 里面。
    实测(假 client 模拟一次 8 秒拉榜,poller 先进锁、pump.fun watcher 后到):
    **pump.fun 那个 job 被挡住 7.70 秒,而它自己一轮的墙钟预算才 6.0 秒** ——
    记账是事后的,等待本身**没有任何上限**。一条装饰行不配让另一个 job 等。

    所以拆成两把:
      · `_lock`  —— 只护**内存状态**(缓存值 / 到期时刻),锁内全是赋值,微秒级;
      · `_fetch` —— single-flight,**`acquire(blocking=False)`,拿不到就走人**。
        拿不到锁 = "别的 job 正在拉这份全局榜单",本轮这一块不显示,下一 tick
        大概率直接命中缓存。
    ⚠️⚠️ 拿不到锁 **≠ 失败**:绝不写负缓存 —— 那会把"别人正在拉"错记成"上游挂了",
       白白按住这一块 60 秒。
    ⚠️ 这条"等不到就放弃"的口径与仓库既有的 tokeninfo._RateGate(_GATE_MAX_WAIT_SEC)
       是同一条,只是这里的上界直接取 0。
    """

    def __init__(self, ttl: float = _BOARD_TTL_SEC,
                 error_ttl: float = _BOARD_ERROR_TTL_SEC,
                 clock=time.monotonic) -> None:
        self._ttl = float(ttl)
        self._error_ttl = float(error_ttl)
        self._clock = clock
        # ⚠️ 只护内存状态,锁内不许出现任何 IO。
        self._lock = threading.Lock()
        # ⚠️ single-flight 闸:同一时刻只有一个 job 在真拉榜,别人**不等**。
        self._fetch = threading.Lock()
        self._until = 0.0
        self._board: dict[str, BoardRow] | None = None
        self._size = 0

    def _cached(self):
        """→ (命中吗, 榜单索引, 行数)。锁内只读内存,不做任何 IO。"""
        with self._lock:
            if self._clock() < self._until:
                return True, self._board, self._size
        return False, None, 0

    def get(self, client) -> tuple[dict[str, BoardRow] | None, int, bool]:
        """
        → (榜单索引 或 None, 榜单行数, 是不是这次真发了请求)。

        ⚠️ 调用方仍然要把这一段的耗时记进本轮墙钟账(见 BoardHoldersLookup._lookup),
           但现在这一段**只可能**是"一次单尝试的短超时请求"或"立刻返回" ——
           不再可能是"排在另一个 job 的重试机后面等两分钟"。
        """
        hit, board, size = self._cached()
        if hit:
            return board, size, False

        # ⚠️⚠️ **不等**:拿不到就说明别的 job 正在拉这份全局榜单。
        if not self._fetch.acquire(blocking=False):
            logger.debug("盈利榜正被另一个 job 拉取,本轮这一块不显示(不等、也不写负缓存)")
            return None, 0, False
        try:
            # 二次检查:排在前面那个 job 可能刚好在我们拿到闸之前填好了缓存。
            hit, board, size = self._cached()
            if hit:
                return board, size, False

            # ⚠️⚠️ 四件事一个都不许改:
            #   · BOARD_LIMIT = 150(服务端真实上限,别再夹回 100);
            #   · auth_invalidate=False(401/403 绝不处置全进程共用的登录态);
            #   · fast_fail=True(装饰性调用**单次尝试、5 秒超时、不按 Retry-After 睡**,
            #     见 client._DECORATIVE_TIMEOUT_SEC —— 复用主路径重试机时这一个请求
            #     最坏能阻塞 140.3 秒,而这一段跑在发消息**之前**);
            #   · BOARD_PERIOD 与 BOARD_PNL_FIELD 成对(24h 榜里只有 pnl24h)。
            try:
                rows = client.get_leaderboard(BOARD_PERIOD, BOARD_LIMIT,
                                              auth_invalidate=False, fast_fail=True)
            except Exception as e:  # noqa: BLE001
                # ⚠️ AuthError 也在这里被吞掉:这一块绝不能把"登录态失效"这件事
                #    捅到推送主路径去(poller 对 AuthError 的处置是**停机**)。
                logger.warning("盈利榜拉取失败,这一块本轮不显示({}s 内不再重试) | {}",
                               int(self._error_ttl), e)
                with self._lock:
                    self._board, self._size = None, 0
                    self._until = self._clock() + self._error_ttl
                return None, 0, True
            board = parse_board(rows)
            size = len(rows) if isinstance(rows, list) else 0
            with self._lock:
                self._board, self._size = board, size
                self._until = self._clock() + self._ttl
            logger.debug("盈利榜已刷新 | 行数={} 可对齐={} TTL={}s", size, len(board),
                         int(self._ttl))
            return board, size, True
        finally:
            self._fetch.release()

    def reset(self) -> None:
        """只给测试用:把缓存清空。"""
        with self._lock:
            self._until = 0.0
            self._board, self._size = None, 0


# 进程级的那一份。⚠️ 测试要换掉它就注入自己的(BoardHoldersLookup(board_cache=...))。
_BOARD = _BoardCache()


# ============================================================
# 带缓存 + 预算的批量查询
# ============================================================
class BoardHoldersLookup:
    """
    「这一批币里,每个币的持有人有谁在 24h 盈利榜上」。

    ⚠️⚠️ 对外的三个方法(begin_round / lookup / cached)**都不抛异常**:
       任何失败都只让这一块消失,推送的其余部分一字不动。
    """

    def __init__(self, client=None, board_cache=None,
                 holders_ttl: float = _HOLDERS_TTL_SEC,
                 per_round: int = _HOLDERS_PER_ROUND,
                 wall_clock_sec: float = _ROUND_WALL_CLOCK_SEC,
                 clock=time.monotonic) -> None:
        # ⚠️ client 允许为 None:pump.fun 那个 watcher 手上只有 PumpClient,
        #    没有 FOMO client。第一次真要发请求时才 build_client() —— 名单为空 /
        #    功能没触发时一个连接都不建。自己建的那个由自己 close()。
        self._client = client
        self._owns_client = client is None
        self._board = board_cache if board_cache is not None else _BOARD
        self._holders_ttl = float(holders_ttl)
        self._per_round = int(per_round)
        self._wall = float(wall_clock_sec)
        self._clock = clock
        # (net, addr) → (取到的时刻, BoardBlock 或 None)
        self._cache: dict[tuple[str, str], tuple[float, BoardBlock | None]] = {}
        self._used = 0
        self._spent = 0.0

    # ---- 生命周期 ----------------------------------------------------------
    def begin_round(self) -> None:
        """
        每 tick 开头调一次:**两本账都归零**(与 tokeninfo / namecn 同一套)。

        ⚠️⚠️ 两行缺一不可,尤其是 `_spent`:少了它,墙钟账**跨 tick 累加** ——
           几个 tick 之后 self._spent 就永远 >= _ROUND_WALL_CLOCK_SEC,
           这一块从此**永久消失**,而且不会有任何报错、任何日志说它错了。
           (变异跑证实过:删掉 `self._spent = 0.0`,上一版全量 4587 条一条都不红。)
        """
        self._used = 0
        self._spent = 0.0

    def close(self) -> None:
        """⚠️ 只关自己建的那个 client —— 注入进来的那个归调用方管。"""
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    # ---- 对外 --------------------------------------------------------------
    def lookup(self, pairs) -> dict[tuple[str, str], BoardBlock]:
        """
        [(链, 地址), …] → {(链, 归一化地址): BoardBlock}。没命中的键**不出现**。

        ⚠️ 整段包在 try 里:这一块没有任何理由让一条推送发不出去。
        """
        try:
            return self._lookup(pairs)
        except Exception as e:  # noqa: BLE001
            logger.warning("盈利榜持有人查询失败,这一块本轮不显示 | {}", e)
            return {}

    def cached(self, network_id, address) -> BoardBlock | None:
        """
        **只读内存缓存、绝不发请求**。给转入(/tin)推送用 —— 与
        tokeninfo.TokenExtraLookup.cached / dexscreener.PoolQuoteLookup.cached 同一口径。

        ⚠️ 缓存只有 90 秒,一条转入基本不可能正好命中(如实记在模块头)。
        """
        try:
            net = (network_id or "").strip()
            key = normalize_token_address(address)
            if not net or key is None:
                return None
            return self._cache_get((net, key))[1]
        except Exception as e:  # noqa: BLE001
            logger.warning("盈利榜持有人读缓存失败 | {} | {}", network_id, e)
            return None

    # ---- 主流程 ------------------------------------------------------------
    def _lookup(self, pairs) -> dict[tuple[str, str], BoardBlock]:
        keys: list[tuple[str, str]] = []
        for net_raw, addr in pairs or ():
            net = (net_raw or "").strip()
            k = normalize_token_address(addr)
            # ⚠️ 认不出链就**一个请求都不发**:networkId 必须是数字,
            #    传链名服务端直接 400(见模块头)。
            if not net or k is None or net not in NETWORK_CHAIN_ID:
                continue
            if (net, k) not in keys:
                keys.append((net, k))
        if not keys:
            return {}

        out: dict[tuple[str, str], BoardBlock] = {}
        need: list[tuple[str, str]] = []
        for nk in keys:
            hit, blk = self._cache_get(nk)
            if hit:
                if blk is not None:
                    out[nk] = blk
            else:
                need.append(nk)
        if not need:
            return out

        client = self._ensure_client()
        if client is None:
            return out
        # ⚠️⚠️ 拉榜那一次**也要在发出去之前**过墙钟闸。上一版只在事后记账 ——
        #    墙钟闸只在"发下一个请求之前"检查,对**已经发出去的那一次**毫无约束,
        #    而拉榜那一次**根本不在闸的管辖内**。
        # ⚠️ 走 _wall_ok 而不是 _take:次数闸 _HOLDERS_PER_ROUND 数的是"为几个**币**
        #    查了持有人",榜单与币无关,不该占掉其中一格。
        if not self._wall_ok("leaderboard"):
            return out
        # ⚠️ 拉榜的耗时同样计入本轮墙钟账 —— 它跟持有人请求一样挂在 tick 上。
        t0 = self._clock()
        try:
            board, size, _ = self._board.get(client)
        finally:
            self._spent += self._clock() - t0
        if not board:
            # 榜单没拿到(失败,或者一行都解析不出来)—— 这一块整体没有意义了
            return out
        for nk in need:
            if not self._take(nk[1]):
                break                      # 预算用尽:剩下的币这一块本轮不显示
            blk = self._fetch_one(client, nk, board, size)
            self._cache_put(nk, blk)
            if blk is not None:
                out[nk] = blk
        return out

    def _fetch_one(self, client, nk: tuple[str, str], board, size: int) -> BoardBlock | None:
        net, addr = nk
        try:
            # ⚠️⚠️ networkId 传**数字**(NETWORK_CHAIN_ID),不是链名 —— 传链名 400。
            # ⚠️⚠️ auth_invalidate=False:401/403 绝不处置全进程共用的登录态(见模块头)。
            data = self._timed(lambda: client.get_top_holders(
                addr, NETWORK_CHAIN_ID[net], auth_invalidate=False, fast_fail=True))
        except Exception as e:  # noqa: BLE001
            logger.warning("持有人榜拉取失败,这个币的盈利榜持有人不显示 | {} {} | {}",
                           net, addr[:16], e)
            return None
        try:
            return match_block(board, data, size)
        except Exception as e:  # noqa: BLE001
            logger.warning("持有人榜解析失败,这个币的盈利榜持有人不显示 | {} {} | {}",
                           net, addr[:16], e)
            return None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from src.client import build_client

            self._client = build_client()
        except Exception as e:  # noqa: BLE001
            logger.warning("盈利榜持有人:client 初始化失败,这一块不显示 | {}", e)
            return None
        return self._client

    # ---- 预算 --------------------------------------------------------------
    def _wall_ok(self, tag: str) -> bool:
        """
        只判墙钟闸。⚠️ **拉榜那一次也要过这道**(它不占次数闸的格子,见 _lookup)。

        ⚠️⚠️ 这道闸判的是"**在发之前**还剩不剩预算",它管不住**已经发出去的那一次**
           要跑多久 —— 那一半由 client 那边的装饰性策略兜住(单次尝试 + 5 秒超时,
           见 client._DECORATIVE_TIMEOUT_SEC)。两条合起来,这一整块每 tick 的
           最坏阻塞 = _ROUND_WALL_CLOCK_SEC + 一次超时 = 6.0 + 5.0 = **11.0 秒**
           (上一版是 3 次尝试 × 12 秒超时 + 最长 104 秒退避 = 140.3 秒/**单个请求**)。
        """
        if self._spent >= self._wall:
            logger.debug("盈利榜持有人墙钟预算用尽({:.1f}s >= {:.1f}s),本轮跳过 | {}",
                         self._spent, self._wall, tag[:16])
            return False
        return True

    def _take(self, tag: str) -> bool:
        """次数闸 + 墙钟闸,任一触发就这一轮不再查(与 tokeninfo._take 同一套)。"""
        if not self._wall_ok(tag):
            return False
        if self._used >= self._per_round:
            logger.debug("盈利榜持有人次数预算用尽({}/{}),本轮跳过 | {}",
                         self._used, self._per_round, tag[:16])
            return False
        self._used += 1
        return True

    def _timed(self, fn):
        """调一次外部接口并把耗时记进本轮墙钟账。⚠️ 失败也要记 —— 超时最费时间。"""
        t0 = self._clock()
        try:
            return fn()
        finally:
            self._spent += self._clock() - t0

    # ---- 缓存 --------------------------------------------------------------
    def _cache_get(self, nk) -> tuple[bool, BoardBlock | None]:
        hit = self._cache.get(nk)
        if hit is None or time.time() - hit[0] >= self._holders_ttl:
            return False, None
        return True, hit[1]

    def _cache_put(self, nk, value: BoardBlock | None) -> None:
        self._cache[nk] = (time.time(), value)
        if len(self._cache) > _HOLDERS_CACHE_MAX:
            for k, _ in sorted(self._cache.items(),
                               key=lambda kv: kv[1][0])[:_HOLDERS_CACHE_MAX // 4]:
                del self._cache[k]
