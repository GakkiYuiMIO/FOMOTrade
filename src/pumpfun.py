"""
pump.fun 指定用户监控 —— 被 /pump 点名的人在 pump.fun 上**成交**或**发观点**就推一条 Telegram。

⚠️⚠️ 这个信号**只做通知,永远不接跟单执行器**。不进 CopyConfig、不写 copytrade_signals。
   本模块不 import copytrade / copyworker / executor 里的任何东西,也永远不该 import。

════════ 两个 watcher / 两个开关 / 两个 job ════════
  · PumpWatcher        —— 买卖(portfolio 发现 + swap-api 拿真相),FOMO_PUMP_ENABLED
  · PumpCalloutWatcher —— 观点(callout),FOMO_PUMP_CALLOUT_ENABLED
两者**共用同一份名单**(pump_watch_users,由 /pump add 维护),但各自独立播种、
独立去重、独立限额。分开的理由见 PumpCalloutWatcher 的类注释。

════════ 两段式:portfolio 做发现,swap-api 拿真相 ════════
每轮:
  1. 每个被盯的人打一次 /user-portfolio?filter=ALL&sortBy=RECENCY&page=0   (1 请求/人)
  2. 与上一轮快照 diff → 得出「哪些 mint 变了」
  3. 只对变动的 mint 打 POST /coins/{mint}/trades/batch(带我们盯的地址)
     → 拿到**真实的逐笔成交**(签名 / 时刻 / 方向 / 单价 / 金额)
  4. 逐笔推送
⚠️⚠️ 第 3 步是这个设计的全部意义:推送里说的是**真实成交**,
   不是「两次快照之间的净变化」。快照只回答"该去问哪个 mint",它自己的差值**绝不推**。
   (净变化分不清"买了 100 又卖了 60"和"买了 40",而 swap-api 分得清。)

════════ 三条实测得来的硬事实(2026-08-31 本人跑真实接口验证) ════════
1. **逐笔成交必须同时传 SVM 与 EVM 两个 canonical 钱包。**
   EVM 链上的成交记在 canonical_evm_wallet 名下:1000XCryptoD 在 BSC 的 QQQB 买入,
   传 SVM 地址(5f1AoB…)得到 `[]`,传 EVM 地址(0x1160…)才拿到那笔 $1.46 的 buy。
   只传一个 = 整条 EVM 侧的成交永久静默丢失。
2. **portfolio 用哪个钱包查都一样。**同一个人 SVM / EVM 两个视图在
   `filter=ALL&sortBy=RECENCY&page=0&pageSize=30` 下返回**逐行相同**的 30 行
   (chainId 分布、updatedAt、连 walletAddress 字段都被回显成 SVM 那个)。
   所以这里**固定用 SVM**(拿不到才退到 `address`):它是接口自己回显的那一个,
   跟着接口的口径走,而不是我们另立一套。
3. **`summary.positionCount` 是脏值,绝不能当总数、绝不能当翻页终止条件。**
   实测同一次枚举三页分别报 236/250/229;本轮 hexiecs 的两个视图一个报 374、
   另一个也报 374 但 totalValueUsd 差了 0.35 —— 这个字段每次都在动。
   本模块**只拉 page 0**,压根不翻页,所以它连出场的机会都没有(见 fetch_portfolio)。

════════ 为什么不复用 src/client.py ════════
FomoClient 绑死 prod-api.fomo.family 且携带 Privy 登录态,它抛的 AuthError 会被
cli._tick_job 捕获后 **sched.shutdown()** —— pump.fun 抖一下绝不该停掉整个监控。
所以这里自建 curl_cffi 客户端,且**所有异常一律自己吞掉**(见 run_once)。

════════ 安全边界 ════════
只打 pump.fun **公开免鉴权**的端点:GET /users/{…}、GET /user-portfolio/{…}、
GET /coins-v3/{mint}、GET /callout/list/{…}、
POST /coins/{mint}/trades/batch(前端匿名就在调这个)。
**不登录、不带任何凭据/令牌、不调任何交易或下单接口。**
⚠️ /callout/list 的响应里有 hasLiked / hasReposted,匿名下实测恒为 null ——
   它们一旦不是 null 就说明我们带上了身份,那是事故,见 Callout.viewer_state_leaked。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from loguru import logger

from src import store
from src.config import get_settings
from src.formatter import render_pump_callout, render_pump_trade
from src.models import NETWORK_DISPLAY

# ============================================================
# 端点
# ============================================================
FRONTEND_BASE = "https://frontend-api-v3.pump.fun"
SWAP_BASE = "https://swap-api.pump.fun"

# portfolio 一页拉多少行。
# ⚠️ 这个值**不影响请求数**(永远只拉 page 0),只影响"一轮之内能看见多少个变动"。
#    sortBy=RECENCY 保证最近动过的排在最前,所以它的真正含义是
#    「一个人在一个巡检间隔里最多能有多少个币发生变动而不被我们漏掉」。
#    50 对 60 秒的间隔宽出一个数量级(实测最活跃的 1000XCryptoD 一小时内动了 4 个)。
# ⚠️ 别把它调到很大来"求稳":响应体是线性增长的,而漏掉的那些下一轮只要还在动
#    就会重新冒到 page 0 的前面来。
PORTFOLIO_PAGE_SIZE = 50

# 一轮最多发几条消息(所有人合计)。
# ⚠️ 这不是防御性编程:一个人批量清仓几十个币是真实会发生的,逐条推就是几十条,
#    用户当场静音,这个功能就死了。超限时**剩下的 mint 本轮不处理、快照也不前移**,
#    下一轮它们仍然与快照不一致,会被重新选中 —— 信息不丢,只是慢一轮。
# ⚠️⚠️ 「所有人合计」是字面意思:它是**整轮一个预算**,一路从 _check 传到 _push,
#    不是每层各带一个。曾经每层各判一次,于是"处理下一个 mint 之前 sent<20"通过后,
#    那个 mint 内部每个人还能各发满 20 条 —— 实测两个 mint 发了 39 条、
#    一个 mint 上两个人发了 40 条,而日志还在说「本轮先推最早的 20 笔」。
MAX_PUSH_PER_ROUND = 20

# 市值缓存的存活时间(秒)。
# ⚠️ 它的**主职责是"同一轮里同一个 mint 只查一次"**:一轮最长十几秒(全是网络 IO),
#    60 秒对此有 3 倍以上余量,任何一条路径重复问同一个 mint 都会被它挡住。
# ⚠️ 跨轮命中只在把巡检间隔调到 60 秒以下时才发生(下限 30 秒,见 config)。
#    那种配置下一个持续变动的 mint 会每隔一轮才重新问一次,拿到的市值最多旧 60 秒 ——
#    这一行回答的是"这币现在多大"($2.91K 还是 $445K,量级问题),
#    60 秒的漂移不会改变答案;而**取错字段**(SOL 当美元)会让答案差两个数量级。
#    请求增速恰恰在巡检最密的时候被压下来,正是最需要它的地方。
# ⚠️ 绝不缓存"失败":失败缓存下来就是让一次网络抖动把市值行按住一整个 TTL。
COIN_STATS_TTL_SEC = 60.0

_TIMEOUT_SEC = 20.0

# pump.fun 的 chainId → 本仓库内部链标识。
# ⚠️⚠️ 只收录 **models 确实认识**的链(NETWORK_DISPLAY / NETWORK_SLUG / GMGN_SLUG)。
#    这张表是**权威**,不是"锦上添花的快捷方式":把某条链从表里删掉,它的链接行就真的
#    消失(有测试钉着)。所以别在下游补一层"通用别名表兜底"—— 那会让这张表的每一条
#    都被遮蔽成死重量,两份真相各自 drift。要加链就改这里 + models 的两张 slug 表。
# ⚠️ **刻意不复用 models.normalize_network**:那张别名表里 hyperliquid 是 "1337",
#    而 pump.fun 给的是 999。拿它去归一化会把 999 原样透传成链标识 "999",
#    于是 Hyperliquid 的链接行整行消失 —— 而 Hyperliquid 在两张 slug 表里都有。
#    pump 的 chainId 空间是 pump 的,该有一张自己的表。
_CHAIN_ID_TO_NETWORK = {
    "1399811149": "solana",
    "8453": "base",
    "56": "bsc",
    "1": "ethereum",
    "143": "monad",
    "4663": "robinhood",
    "999": "hyperliquid",
}
# models 不认识的链:只给一个展示名,**不给链接**(拼一个 GMGN 不支持的链只会 404,
# 而错的链接比没有链接更糟)。链名本身是有价值的 —— 「他在 Arbitrum 上买的」
# 与「他在 Solana 上买的」是两件事,不该因为出不了链接就把链也一起抹掉。
_CHAIN_ID_DISPLAY_ONLY = {
    "10": "Optimism",
    "42161": "Arbitrum",
    "137": "Polygon",
    "43114": "Avalanche",
}

# 限流余额低于此就告警。pump 的两个端点分别是 30/分 与 60/分,
# 剩不到 5 说明我们打得太密(或者别的进程在共用这个出口 IP),该让用户知道。
_RATE_LIMIT_WARN = 5

# /users/{key} 里 key 的合法字符集 —— **它来自 Telegram 消息,是不可信输入**。
# 覆盖三种真实形态:pump 用户名(hexiecs / 1000XCryptoD / brc20_niubi)、
# base58 的 SVM 地址、`0x` 开头的 EVM 地址。长度 64 比最长的 base58 地址还宽。
# ⚠️ 刻意不含 `.` `/` `?` `#` `%` 与空白:它们是唯一能改变 URL 结构的东西
#    (见 resolve_user 里那个路径穿越的实例)。
_USER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ============================================================
# 数据结构
# ============================================================
@dataclass(frozen=True)
class PumpProfile:
    """/users/{名字或地址} 的结果,只留落库要用的字段。"""

    user_id: str
    username: str | None
    svm_wallet: str | None
    evm_wallet: str | None

    @property
    def portfolio_wallet(self) -> str | None:
        """
        拿哪个钱包去查持仓。见模块顶部实测事实 2:两个视图返回逐行相同的数据,
        固定用 SVM —— 它是接口自己回显在 `walletAddress` 里的那一个。
        """
        return self.svm_wallet or self.evm_wallet


@dataclass(frozen=True)
class Position:
    """portfolio 里的一行。只留 diff 与推送用得上的字段。"""

    chain_id: str
    coin_mint: str
    amount_held: float | None
    realized_pnl_usd: float | None
    updated_at: str | None
    symbol: str | None = None
    # ↓ 以下四个**只进推送、不进快照**:diff 判据只认 amount_held / realized_pnl_usd
    #   (见 _position_changed —— value_usd 随行情每轮都在动,拿它当触发器等于每轮
    #    把整页 mint 全问一遍)。零额外请求:portfolio 本来就把它们返回了。
    value_usd: float | None = None           # 当前这笔仓位值多少美元
    cost_basis_usd: float | None = None      # **当前持有量**的成本(不是累计买入额)
    pnl_percentage: float | None = None      # pump 自己算的百分比,语义见 realized_pnl_pct
    is_exited: bool | None = None            # pump 自己的清仓标记

    @property
    def key(self) -> tuple[str, str]:
        return (self.chain_id, self.coin_mint)

    @property
    def is_cleared(self) -> bool:
        """
        这一行是不是已经清仓了。

        ⚠️ isExited 是 pump 自己的标记,优先信它;缺失时才退到 amountHeld == 0
           (filter=ALL 下两者实测总是同进同出)。
        ⚠️ amount_held is None 时**不推断**:那是"这一轮没拿到量",不是"清仓了"。
        """
        if self.is_exited is not None:
            return bool(self.is_exited)
        return self.amount_held is not None and self.amount_held == 0

    @property
    def unrealized_pnl_usd(self) -> float | None:
        """
        未实现盈亏 = 当前市值 − 当前持有量的成本。

        ⚠️⚠️ **绝不能直接用报文里的 `pnlUsd`** —— 它是**总**盈亏(已实现 + 未实现)。
           2026-08-31 的真实 portfolio 六行逐行验过,恒等式
           `pnlUsd == (valueUsd − costBasisUsd) + realizedPnlUsd` 差值**恰好为 0**:
           QQQB 那行 pnlUsd = -12,346.57,其中已实现占了 -444.51,
           把它当未实现打出去,账面浮亏就凭空多报了 444 美元。
        ⚠️ 这不是"本地推算"的例外(对照 _avg_price_line 那条禁令):均价要拿
           按 TEXT 存的 token 数量做算术,这里是**同一行报文里两个美元字段相减**,
           而且结果被上游自己的 pnlUsd 对得上 —— 是校验过的透传,不是发明数字。
        """
        if self.value_usd is None or self.cost_basis_usd is None:
            return None
        return self.value_usd - self.cost_basis_usd

    @property
    def unrealized_pnl_pct(self) -> float | None:
        """
        未实现盈亏率 = 未实现 / 当前持有量的成本。

        ⚠️⚠️ **绝不能直接用报文里的 `pnlPercentage`**:它的基数是 amountBoughtUsd
           (累计买入额),分子是**总**盈亏 —— 两头都跟"未实现"对不上。
        ⚠️ 成本为 0(整仓靠转入拿到的)时返回 None:除零之外,"成本 0 赚无穷倍"
           本身也不是个能摆给人看的数。金额那一段照常显示。
        """
        pnl = self.unrealized_pnl_usd
        if pnl is None or self.cost_basis_usd is None or self.cost_basis_usd == 0:
            return None
        return pnl / self.cost_basis_usd * 100.0

    @property
    def realized_pnl_pct(self) -> float | None:
        """
        已实现盈亏率 —— 直接透传报文的 `pnlPercentage`,但**只在清仓路径上取用**。

        ⚠️ 依据:清仓时 valueUsd == costBasisUsd == 0,未实现恒为 0、
           pnlUsd 恒等于 realizedPnlUsd,那一刻 pnlPercentage 说的就是已实现的百分比
           (夹具里 GTA +35.84% / 指甲刀 -88.78% 两行都对得上)。
           这里把那个前提**再自查一次**:未实现算出来不是 0 就不给百分比 ——
           前提不成立时宁可只报金额,也不贴一个语义已经漂了的百分比。
        """
        pnl = self.unrealized_pnl_usd
        if pnl is None or pnl != 0:
            return None
        return self.pnl_percentage

    @property
    def network_id(self) -> str | None:
        """内部链标识。未收录的链返回 None —— 链接那一行会整行消失,其余照推。"""
        return _CHAIN_ID_TO_NETWORK.get(self.chain_id)

    @property
    def chain_display(self) -> str | None:
        """链展示名。两张表都没有就 None(那一行整行消失,绝不打「链 42161」这种半成品)。"""
        net = self.network_id
        if net is not None:
            return NETWORK_DISPLAY.get(net)
        return _CHAIN_ID_DISPLAY_ONLY.get(self.chain_id)


@dataclass(frozen=True)
class CoinStats:
    """
    GET /coins-v3/{mint} 里我们要的那几个字段。

    ⚠️⚠️ 两条链返回的**字段集不同**(Solana 多 bonding_curve / complete /
       market_cap_quote…,EVM 多 canonical_pool_liquidity_usd),所以这里
       只取两条链都有的字段,并且任何一个取不到就是 None(对应行整行消失)。
    ⚠️ symbol / chain_id 是**观点推送**要的:/callout/list 只给 coinMint,
       既没有币的符号也没有链 —— 而没有链就没有链接行。这两个字段搭在
       已有的市值请求上,零额外成本(同一个响应、同一份 TTL 缓存)。
    """

    market_cap_usd: float | None
    ath_market_cap_usd: float | None
    symbol: str | None = None
    # pump 给的是 CAIP-2 形态:`solana:5eykt4Us…` / `eip155:56`。原样存,
    # 归一化交给 network_id —— 存归一化后的值会让"上游给了什么"这件事丢失。
    chain_id: str | None = None

    @property
    def network_id(self) -> str | None:
        """
        CAIP-2 链标识 → 本仓库内部链标识。认不出来返回 None(链接行整行消失)。

        ⚠️⚠️ 刻意复用 _CHAIN_ID_TO_NETWORK 这张**唯一权威表**(见它的注释),
           而不是另起一张 CAIP-2 的表:两张表会各自 drift,
           于是同一条链在买卖推送里有链接、在观点推送里没有。
        ⚠️ Solana 那一支必须单独处理:portfolio 给的是数字 chainId 1399811149,
           而 coins-v3 给的是 `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` ——
           同一条链在 pump 自己的两个端点里就是两种写法,别指望它们能对上。
        """
        cid = (self.chain_id or "").strip()
        if not cid:
            return None
        family, _, rest = cid.partition(":")
        if family == "solana":
            return "solana"
        if family == "eip155" and rest:
            return _CHAIN_ID_TO_NETWORK.get(rest)
        return None

    @property
    def chain_display(self) -> str | None:
        """链展示名。两张表都没有就 None(那一行整行消失,绝不打「链 42161」这种半成品)。"""
        net = self.network_id
        if net is not None:
            return NETWORK_DISPLAY.get(net)
        family, _, rest = (self.chain_id or "").strip().partition(":")
        return _CHAIN_ID_DISPLAY_ONLY.get(rest) if family == "eip155" else None


@dataclass(frozen=True)
class Trade:
    """swap-api 的一笔成交。"""

    tx: str
    slot_index_id: str
    traded_at: str          # 已归一化成 store.now_iso() 的形态(UTC ISO,秒精度)
    traded_ts: float        # unix 秒,给新鲜窗口用
    side: str | None        # "buy" / "sell" / None(上游给了没见过的值)
    user_address: str       # 已归一化(EVM 小写 / Solana 原样)
    amount_usd: float | None
    price_usd: float | None

    def ledger_key(self, user_id: str, coin_mint: str) -> tuple[str, str, str, str]:
        """
        已推台账的主键,与 store.pump_pushed_trades 的 PRIMARY KEY 逐字段对齐。

        ⚠️ 带 slot_index_id:一个 tx 里可以有同一个 mint 的多笔成交(拆单),
           只按 tx 去重会把后面几笔静默吃掉。
        """
        return (user_id, coin_mint, self.tx, self.slot_index_id)


@dataclass(frozen=True)
class Callout:
    """
    /callout/list 的一条观点。

    ⚠️⚠️ pump.fun 上这个功能叫 **callout**,正文字段叫 **thesis**。
       老的币评论 API 已经整个下线(`reply_count` 只是币对象里的遗留字段),
       别去找 /replies /comments —— 8 条路径 2026-08-31 实测全 404。
    ⚠️ `/callout/recent` 在前端 bundle 里有,但线上返回
       `400 Validation failed (uuid is expected)` —— 被 `/callout/:calloutId`
       抢了路由,**实际不可用**,不要用。
    """

    callout_id: str          # UUID,全局唯一 —— 去重台账的主键
    user_id: str | None      # ⚠️ 这里是**钱包地址**,不是 pump 的 userId(UUID)
    coin_mint: str | None
    thesis: str | None
    created_at: str          # 已归一化成 store.now_iso() 的形态(UTC ISO,秒精度)
    created_ts: float        # unix **秒**,给新鲜窗口用
    market_cap_usd: float | None   # 发表**那一刻**的市值,不是现在的
    multiple: float | None         # 发表至今的倍数(现价 / 发表时价格)
    likes: float | None
    view_count: float | None
    # ⚠️⚠️ 这两个是**按观看者**的状态,匿名请求下上游给的是 `null`。
    #    必须存成三态(True / False / None)并且一律 is None 判空 ——
    #    真值判断会把 null 读成 False,也就是把"不知道"说成"没点过赞"。
    has_liked: bool | None
    has_reposted: bool | None

    @property
    def viewer_state_leaked(self) -> bool:
        """
        匿名请求却拿到了观看者状态 = 这次请求**带上了身份**。

        ⚠️⚠️ 本模块的硬约束是「不登录、不带任何凭据」。hasLiked / hasReposted
           在匿名下实测恒为 null(2026-08-31 亲测),它们一旦变成 true/false,
           就说明有 cookie 或令牌混进了这条链路 —— 那是要立刻看见的事故,
           而不是一个可以顺手用起来的字段。
        ⚠️ 判据必须是 `is not None`:**False 同样算泄漏**(它一样是身份态)。
           写成真值判断的话 False 会被当成"没泄漏"漏过去,而 None 会被当成 False。
        """
        return self.has_liked is not None or self.has_reposted is not None


# ============================================================
# 解析
# ============================================================
def _as_text(v) -> str | None:
    """任意输入 → 非空字符串,空串/空白一律 None(缺失整行消失,绝不打 N/A)。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_float(v) -> float | None:
    """
    任意输入 → float。判空一律 is None:**0 是有意义的真实值**
    (amountHeld=0 就是"已清仓",拿真值判断会把清仓读成"没这个字段")。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    # NaN / Inf 一路传下去会渲染成 "nan",一条一眼假的信息比没有这一行糟得多
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _pick(row: dict, *names):
    """
    按顺序取第一个**存在**的键。

    ⚠️⚠️ 这个函数存在的唯一理由:pump 的两个成交端点**字段大小写不一样**——
       POST /v1/…/trades/batch 给 `amountUSD` / `priceUSD` / `amountSOL`,
       GET  /v2/…/trades       给 `amountUsd` / `priceUsd` / `amountSol`。
       (2026-08-31 同一个 mint 两个端点各打一次,亲眼比对过。)
       只兼容一种写法的话,另一种会静默取到 None,推送里就是「$None」或整行消失 ——
       **不报错、不告警,只是金额永远没了**。
    ⚠️ 用 `in` 判存在而不是真值判断:金额真的是 0 的成交存在(粉尘级成交),
       真值判断会跳过它去看下一个键名,拿到 None。
    """
    for n in names:
        if n in row:
            return row[n]
    return None


def normalize_wallet(addr) -> str | None:
    """
    钱包地址归一化,口径与 models.normalize_token_address 一致:
    EVM(0x + 42 位)大小写不敏感 → lower();Solana 是 base58、**大小写敏感,绝不 lower**。

    ⚠️ 刻意不直接复用 models.normalize_token_address:那个函数的契约写的是"代币地址",
       这里归一化的是**钱包地址**。逻辑此刻相同,但两者哪天要分开演进时,
       复用会让一处改动静默改掉另一处的语义。四行代码换一条清晰的边界。
    """
    a = (addr or "").strip()
    if not a:
        return None
    if a.startswith("0x") and len(a) == 42:
        return a.lower()
    return a


def _iso_utc(ts: float) -> str:
    """
    unix 秒 → 与 store.now_iso() **同形态**的 UTC ISO 串(秒精度)。

    ⚠️ 必须统一形态:台账的 traded_at 要和"新鲜窗口下界"做**字符串比较**
       (pump 给的是 `…T01:59:02.000Z`,now_iso 给的是 `…T02:24:52+00:00`)。
       两种形态混在一列里,字典序在同一秒上就会翻车,清理与去重都跟着错。
    """
    return datetime.fromtimestamp(ts, UTC).replace(microsecond=0).isoformat()


def _parse_ts(v) -> float | None:
    """ISO 时间串 → unix 秒。解析不出来返回 None(绝不拿 0 或当前时间冒充)。"""
    s = _as_text(v)
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _as_bool(v) -> bool | None:
    """
    只认**真正的 bool**,其余一律 None(= 不知道)。

    ⚠️ 与 parse_positions 里 isExited 那一处同一条教训:上游给字符串 "false" 时
       `bool("false")` 是 True。认不出来就说不知道,绝不猜。
    """
    return v if isinstance(v, bool) else None


def _parse_callout_ts(v) -> float | None:
    """
    观点的时刻 → unix **秒**。解析不出来返回 None(绝不拿 0 或当前时间冒充)。

    ⚠️⚠️ **pump 的时间戳格式在同一个功能里就有两种**(2026-08-31 实测):
       · `/callout/list` 的 `createdAt` 给的是 **epoch 毫秒**(1788150521329);
       · 同一条记录里的 `maxMultiplierAt`、以及 `updates[]` 与别的 callout 端点,
         给的是 **ISO 字符串**(`2026-08-30T21:25:07.466Z`)。
       当成秒直接用就差 1000 倍:1788150521329 秒 ≈ 公元 58000 年,
       新鲜窗口会把每一条都判成"来自未来、永远新鲜",于是一开机就把
       这个人的全部历史观点倒出来。反过来当成毫秒则每条都"老得掉出窗口"、
       一条都不推。两个方向都不报错,只是行为完全错。
    ⚠️ 所以这里**按类型分派**,不靠数量级去猜:数字 = 毫秒,字符串 = ISO。
       (拿"大于某个阈值就当毫秒"来猜的话,阈值本身就是个会过期的魔法数。)
    ⚠️ bool 是 int 的子类,必须先挡掉 —— True 会被当成 1 毫秒。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):   # NaN / Inf
            return None
        return v / 1000.0
    # 字符串:先当 ISO 解,解不动再退到"纯数字的毫秒"(上游偶尔把数字包成字符串)
    s = _as_text(v)
    if s is None:
        return None
    iso = _parse_ts(s)
    if iso is not None:
        return iso
    try:
        return float(s) / 1000.0
    except (TypeError, ValueError):
        return None


def parse_callouts(payload) -> list[Callout] | None:
    """
    /callout/list 响应 → 观点列表。结构不对返回 None(与「拉取失败」同义)。

    ⚠️⚠️ 返回 None 和返回 [] 语义不同:None = 这轮不知道他发过什么
       (播种位与台账都不能动);[] = 他确实一条观点都没有。
       **空结果在上游是 `200 {"callouts": [], "nextPageToken": ""}`,不是 404**
       (实测),所以 [] 是一个会真实出现的正常返回,不能当成异常。
    ⚠️ 缺 calloutId 或缺时刻的行直接丢:没有 calloutId 就没有去重主键(下一轮必然
       重推),没有时刻就判不了新鲜窗口。两者在实测响应里一条都不缺。
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("callouts")
    if not isinstance(rows, list):
        logger.warning("pump.fun callout 的 callouts 不是数组: {}", type(rows).__name__)
        return None

    out: list[Callout] = []
    dropped = 0
    for row in rows:
        c = _parse_callout(row)
        if c is None:
            dropped += 1
            continue
        out.append(c)
    if dropped:
        logger.warning("pump.fun callout 有 {} 行缺 calloutId/createdAt,已跳过(总 {} 行)",
                       dropped, len(rows))
    return out


def _parse_callout(row) -> Callout | None:
    if not isinstance(row, dict):
        return None
    cid = _as_text(row.get("calloutId"))
    ts = _parse_callout_ts(row.get("createdAt"))
    if cid is None or ts is None:
        return None
    try:
        created_at = _iso_utc(ts)
    except (OverflowError, OSError, ValueError):
        # ⚠️ createdAt 是**裸数字**,它没有 ISO 那种天然的取值范围约束:
        #    上游给个 1e30 就能让 fromtimestamp 直接抛,而本模块所有 parse_*
        #    的契约都是"不抛,拿不到就丢这一行"。
        logger.warning("pump.fun 观点 {} 的 createdAt 超出可表示范围,已丢弃", cid)
        return None
    return Callout(
        callout_id=cid,
        # ⚠️ 这个字段名叫 userId,值却是**钱包地址**(实测)。只留着排查用,
        #    归属一律靠"我们是拿谁的键去问的",绝不拿它反查人。
        user_id=_as_text(row.get("userId")),
        coin_mint=normalize_wallet(_as_text(row.get("coinMint"))),
        thesis=_as_text(row.get("thesis")),
        created_at=created_at,
        created_ts=ts,
        market_cap_usd=_as_float(row.get("marketCap")),
        multiple=_as_float(row.get("multiple")),
        likes=_as_float(row.get("likes")),
        view_count=_as_float(row.get("viewCount")),
        has_liked=_as_bool(row.get("hasLiked")),
        has_reposted=_as_bool(row.get("hasReposted")),
    )


def parse_profile(payload) -> PumpProfile | None:
    """/users/{…} 响应 → PumpProfile。缺 userId 就是没查到(返回 None)。"""
    if not isinstance(payload, dict):
        return None
    uid = _as_text(payload.get("userId"))
    if uid is None:
        return None
    return PumpProfile(
        user_id=uid,
        username=_as_text(payload.get("username")),
        # canonical_svm_wallet 缺失时退到 address(它就是主钱包)
        svm_wallet=normalize_wallet(payload.get("canonical_svm_wallet")
                                    or payload.get("address")),
        evm_wallet=normalize_wallet(payload.get("canonical_evm_wallet")),
    )


def parse_positions(payload) -> list[Position] | None:
    """
    /user-portfolio 响应 → 持仓列表。结构不对返回 None(与「拉取失败」同义)。

    ⚠️⚠️ 返回 None 和返回 [] 语义不同:None = 这轮不知道他持仓长什么样
       (快照绝不能动,否则会把一整轮的变动静默吃掉);[] = 确实一个持仓都没有。
    ⚠️⚠️ **完全不看 `summary.positionCount`**。那是脏值(实测同一次枚举三页
       分别报 236/250/229),拿它当总数或翻页终止条件都会错。
       我们只要 `positions` 这个数组本身,它有几行就是几行。
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("positions")
    if not isinstance(rows, list):
        logger.warning("pump.fun portfolio 的 positions 不是数组: {}", type(rows).__name__)
        return None

    out: list[Position] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped += 1
            continue
        mint = normalize_wallet(_as_text(row.get("coinMint")))
        chain = _as_text(row.get("chainId"))
        if mint is None or chain is None:
            # 没有 (链, mint) 就没有身份,连"这一行有没有变"都判不了
            dropped += 1
            continue
        coin = row.get("coin")
        exited = row.get("isExited")
        out.append(Position(
            chain_id=chain,
            coin_mint=mint,
            amount_held=_as_float(row.get("amountHeld")),
            realized_pnl_usd=_as_float(row.get("realizedPnlUsd")),
            updated_at=_as_text(row.get("updatedAt")),
            symbol=_as_text(coin.get("symbol")) if isinstance(coin, dict) else None,
            value_usd=_as_float(row.get("valueUsd")),
            cost_basis_usd=_as_float(row.get("costBasisUsd")),
            pnl_percentage=_as_float(row.get("pnlPercentage")),
            # ⚠️ 只认真正的 bool:上游给字符串 "false" 时 bool("false") 是 True,
            #    那会把一个还持着货的人渲染成「已清仓」。认不出来就 None(退到 amountHeld)
            is_exited=exited if isinstance(exited, bool) else None,
        ))
    if dropped:
        logger.warning("pump.fun portfolio 有 {} 行缺 coinMint/chainId,已跳过(总 {} 行)",
                       dropped, len(rows))
    return out


def parse_coin(payload) -> CoinStats | None:
    """
    /coins-v3/{mint} 响应 → CoinStats。结构不对返回 None。

    ⚠️⚠️⚠️ **市值只认 `usd_market_cap`,绝不碰 `market_cap`。**
       同一个响应里 `market_cap` 在 **Solana 上是 SOL 计价**:
       2026-08-31 实测 PUNCHMA 拿到 market_cap=28.0355 / usd_market_cap=2906.09,
       而 28.0355 恰好等于 bonding curve 自己算出来的 SOL 市值
       (virtual_sol_reserves / 1e9 × total_supply / virtual_token_reserves,逐位相同)。
       EVM 上两者相等(KISS 两个都是 445526.71),所以**在 EVM 上测不出这个 bug**——
       只用 EVM 验一遍就上线的话,Solana 的每一条推送都会把市值说小两个数量级。
       同理不拿 `market_cap_usd` 兜底:它在 Solana 上取的 SOL 价与 usd_market_cap
       不是同一个(2891.09 vs 2906.09),两个来源混用只会让同一个币每轮跳来跳去。

    ⚠️ `ath_market_cap` 是**美元**,与 usd_market_cap 可比。推导:PUNCHMA 的
       ath_market_cap = 18489.20,而它 `complete=False`(还在 bonding curve 上),
       curve 的 SOL 市值上限只有约 411 SOL —— 18489 若是 SOL 计价则根本不可能达到。
       所以它只能是美元。(渲染层还会再挡一次单位错配,见 _pump_mcap_line。)
    """
    if not isinstance(payload, dict):
        return None
    return CoinStats(
        market_cap_usd=_as_float(payload.get("usd_market_cap")),
        ath_market_cap_usd=_as_float(payload.get("ath_market_cap")),
        symbol=_as_text(payload.get("symbol")),
        chain_id=_as_text(payload.get("chain_id")),
    )


def parse_trades(payload) -> dict[str, list[Trade]] | None:
    """
    trades/batch 响应 → {归一化地址: [成交, …]}。结构不对返回 None。

    ⚠️ 缺 tx 或缺时刻的行直接丢:没有 tx 就没有去重主键(下一轮必然重推),
       没有时刻就判不了新鲜窗口。两者在实测响应里一条都不缺。
    """
    if not isinstance(payload, dict):
        return None
    out: dict[str, list[Trade]] = {}
    for addr, rows in payload.items():
        key = normalize_wallet(addr)
        if key is None or not isinstance(rows, list):
            continue
        bucket = out.setdefault(key, [])
        for row in rows:
            t = _parse_trade(row)
            if t is not None:
                bucket.append(t)
    return out


def _parse_trade(row) -> Trade | None:
    if not isinstance(row, dict):
        return None
    tx = _as_text(row.get("tx"))
    ts = _parse_ts(row.get("timestamp"))
    if tx is None or ts is None:
        return None
    side = _as_text(row.get("type"))
    return Trade(
        tx=tx,
        # 上游没给就存空串 —— 台账主键退化成"按 tx 去重",仍然不会重推
        slot_index_id=_as_text(row.get("slotIndexId")) or "",
        traded_at=_iso_utc(ts),
        traded_ts=ts,
        side=side.lower() if side else None,
        user_address=normalize_wallet(row.get("userAddress")) or "",
        # ⚠️ 两种大小写都要认,见 _pick 的说明
        amount_usd=_as_float(_pick(row, "amountUSD", "amountUsd")),
        price_usd=_as_float(_pick(row, "priceUSD", "priceUsd")),
    )


# ============================================================
# HTTP 客户端 —— 只用公开免登录端点,异常一律吞掉
# ============================================================
class PumpClient:
    """
    ⚠️ 本类的每个 fetch_* / resolve_* **都不抛异常**,失败一律返回 None 并记日志。
       它跑在调度器的 worker 线程与 bot 线程里,一个逃逸的异常最坏会被
       APScheduler 记成 job 崩溃 —— 而这个功能没有任何理由影响别的 job。
    ⚠️ 每线程一个 Session:libcurl 的 easy handle **不能被多线程同时使用**,
       共用一个是概率性的崩溃/串包(与 client.HttpFomoClient 同一条理由)。
    """

    def __init__(self, proxy: str | None = None) -> None:
        s = get_settings()
        self._proxy = s.fomo_proxy if proxy is None else proxy
        self._tl = threading.local()

    def _session(self):
        sess = getattr(self._tl, "session", None)
        if sess is None:
            # 延迟 import:curl_cffi 带原生库,顶层 import 会让不用它的路径也被拖累
            from curl_cffi import requests as cffi_requests

            proxies = None if not self._proxy else {"http": self._proxy, "https": self._proxy}
            # ⚠️ impersonate="chrome" 是必需的:pump.fun 前面有 Cloudflare,认 TLS 指纹
            sess = cffi_requests.Session(impersonate="chrome", proxies=proxies,
                                         timeout=_TIMEOUT_SEC)
            self._tl.session = sess
        return sess

    def close(self) -> None:
        sess = getattr(self._tl, "session", None)
        self._tl.session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _check_rate(tag: str, headers) -> None:
        """限流余额见底就告警。⚠️ 只记日志,绝不因此改变行为(那会变成自己给自己限流)。"""
        try:
            remaining = int((headers or {}).get("x-ratelimit-remaining"))
        except (TypeError, ValueError):
            return
        if remaining <= _RATE_LIMIT_WARN:
            logger.warning("pump.fun 限流余额只剩 {} | {} —— 建议调大 FOMO_PUMP_INTERVAL_SEC",
                           remaining, tag)

    def _json(self, tag: str, method: str, url: str, **kw):
        """一次请求 → JSON。任何失败(网络/超时/非 2xx/结构变更)一律 None。"""
        try:
            resp = getattr(self._session(), method)(url, **kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("pump.fun {} 请求失败(下一轮重试): {}", tag, e)
            self.close()          # 连接可能已经废了,整池丢弃重建
            return None
        self._check_rate(tag, dict(resp.headers or {}))
        # ⚠️ 必须收整个 2xx,不能只认 200:trades/batch 是 **201 Created**(实测)。
        #    只认 200 的话逐笔成交这一路永远拿不到数据,而且不报错。
        if not (200 <= resp.status_code < 300):
            logger.warning("pump.fun {} 返回 HTTP {}", tag, resp.status_code)
            return None
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("pump.fun {} 响应不是 JSON: {}", tag, e)
            return None

    def resolve_user(self, key: str) -> PumpProfile | None:
        """
        名字或钱包 → 档案。查不到 / 失败 / 键非法一律 None(调用方据此回一句"找不到")。

        ⚠️⚠️ key **直接来自 Telegram 消息**,原样拼进 URL 路径就是一个路径穿越口子:
           `/pump add ../../following-positions/alerts` 会拼出
           `…/users/../../following-positions/alerts`,curl 把 `..` 正规化掉之后
           打的正是那个**需要登录**的端点(实测 401)。
           所以先过白名单(非法的键一个字节都不发出去)、再 quote 一层。
        ⚠️ 白名单收得比 pump 实际允许的字符窄一点是有意的:合法的键(用户名、
           base58 的 SVM 地址、0x 开头的 EVM 地址)全在这个集合里,
           而任何能改变 URL 结构的字符(`/` `?` `#` `.` `%` 空白)一个都不在。
        """
        k = (key or "").strip()
        if not _USER_KEY_RE.match(k):
            logger.warning("pump.fun 用户键含非法字符,不发请求: {!r}", k[:80])
            return None
        # quote 是第二层:白名单已经保证这里没有需要转义的字符,
        # 留着它是为了将来放宽白名单时不至于又开一次同一个口子
        payload = self._json(f"users/{k}", "get",
                             f"{FRONTEND_BASE}/users/{quote(k, safe='')}")
        return parse_profile(payload)

    def fetch_portfolio(self, wallet: str) -> list[Position] | None:
        """
        最近变动的一页持仓。

        ⚠️⚠️ `filter=ALL` + `sortBy=RECENCY` 两个参数都是关键,少一个这个设计就不成立:
           · filter=ALL 才会带出 isExited=true / amountHeld=0 的行 ——
             **清仓因此是正面可观测的**,不用靠"这一行消失了"去猜(而我们只拉一页,
             "消失"本来就区分不出"卖光了"和"被别的币挤出这一页")。
           · sortBy=RECENCY 保证最近动过的排在最前 —— 只拉 page 0 就够,
             不用为了找变动去翻完 1905 行持仓(实测 1000XCryptoD 就这么多)。
        ⚠️ **只拉 page 0,永不翻页**。所以 summary.positionCount 那个脏值
           连出场机会都没有(它既不当总数,也不当终止条件)。
        """
        payload = self._json(
            f"user-portfolio/{wallet[:8]}…", "get", f"{FRONTEND_BASE}/user-portfolio/{wallet}",
            params={"filter": "ALL", "sortBy": "RECENCY",
                    "page": 0, "pageSize": PORTFOLIO_PAGE_SIZE},
        )
        return parse_positions(payload)

    def fetch_coin(self, mint: str) -> CoinStats | None:
        """
        某个 mint 的当前市值与历史最高市值。失败一律 None(市值那一行整行消失)。

        ⚠️ 这是本模块**唯一**按 mint 计费的额外请求(60 次/分),
           所以调用方必须带缓存 —— 见 PumpWatcher._coin_stats。
        ⚠️ 两条链共用这一个端点(实测 Solana 的 PUNCHMA 与 Robinhood 的
           0x04a2df…2b36 都是 200),不需要按链分支。
        """
        payload = self._json(f"coins-v3/{mint[:8]}…", "get",
                             f"{FRONTEND_BASE}/coins-v3/{mint}")
        return parse_coin(payload)

    def fetch_callouts(self, user_key: str, limit: int = 30) -> list[Callout] | None:
        """
        这个人最近的观点,时间倒序。失败 / 键非法一律 None(与「本轮不知道」同义)。

        ⚠️⚠️ **按人查、每人每轮 1 个请求**,这是这个功能能成立的全部原因。
           反过来那条路(先拿新币、再筛出名单里的人)走不通:pump 上新币
           17.2 个/分钟,光是枚举就把限流吃光了。
        ⚠️ key 与 resolve_user 走**同一道白名单**:它最终来自 Telegram 消息
           (经由库里的 user_id / 钱包),原样拼进 URL 路径就是路径穿越口子 ——
           `…/callout/list/../../following-positions/alerts` 会打到那个**需要登录**
           的端点上。所以先过白名单、再 quote 一层。
           ⚠️ 白名单额外放行 `.`?**不放**。这里的键是 UUID 或钱包地址,
              两者都只用 [A-Za-z0-9_-],而 `.` 正是路径穿越唯一需要的字符。
        ⚠️ 空结果是 `200 {"callouts": [], "nextPageToken": ""}`(实测),不是 404;
           非法 id 才是 `400 "id must be a user id (UUID) or wallet address"`。
           所以 [] 与 None 在这里是两件**都会真实发生**的事,不能混。
        ⚠️ 只拉第一页(pageToken 传空串)。它已经按 TIMESTAMP DESC 排好序,
           而我们只关心"上一轮之后的新观点";翻页只会把请求数乘上去,
           而漏掉的那些下一轮还在第一页里(观点实测约 3.2 条/天/人,
           一页 30 条对 60 秒的巡检间隔宽出四个数量级)。
        """
        k = (user_key or "").strip()
        if not _USER_KEY_RE.match(k):
            logger.warning("pump.fun 观点用户键含非法字符,不发请求: {!r}", k[:80])
            return None
        payload = self._json(
            f"callout/list/{k[:8]}…", "get",
            f"{FRONTEND_BASE}/callout/list/{quote(k, safe='')}",
            params={"limit": limit, "sortBy": "TIMESTAMP",
                    "sortOrder": "DESC", "pageToken": ""},
        )
        return parse_callouts(payload)

    def fetch_trades(self, mint: str, addresses: list[str]) -> dict[str, list[Trade]] | None:
        """
        某个 mint 上、这几个地址的逐笔成交。

        ⚠️ addresses 必须**同时含 SVM 与 EVM** 两个 canonical 钱包 ——
           EVM 链上的成交只挂在 EVM 地址下(见模块顶部实测事实 1)。
        """
        if not addresses:
            return {}
        payload = self._json(
            f"trades/batch/{mint[:8]}…", "post",
            f"{SWAP_BASE}/v1/coins/{mint}/trades/batch",
            json={"userAddresses": list(addresses)},
        )
        return parse_trades(payload)


# ============================================================
# 巡检
# ============================================================
@dataclass(frozen=True)
class _Watched:
    """名单里的一个人 + 他的两个钱包(已归一化)。"""

    user_id: str
    username: str | None
    wallets: tuple[str, ...]
    portfolio_wallet: str | None
    seeded: bool           # 买卖监控的播种位
    callout_seeded: bool   # 观点监控的播种位。⚠️ 两路分别播种,绝不共用一位


class PumpWatcher:
    """
    独立的第四个 APScheduler job。**不塞进 poller.tick()**,理由:
      1) tick 里抛 AuthError 会 sched.shutdown() 停掉整个监控(cli.py 那段 except)——
         pump.fun 抖一下绝不该有权力停掉 FOMO 推送链路;
      2) tick 实测 5~18 秒 / 轮询间隔 27 秒,余量本来就只剩几秒;
      3) 节奏不匹配:pump 持仓变动是小时级事件,27 秒打一次纯属白挨限流。
    """

    def __init__(self, notifier, client: PumpClient | None = None) -> None:
        s = get_settings()
        self._notifier = notifier
        self._client = client if client is not None else PumpClient()
        self._min_usd = s.fomo_pump_min_usd
        self._max_mints = s.fomo_pump_max_mints
        self._max_age = s.fomo_pump_trade_max_age_sec
        # mint → (取到的时刻, 市值)。⚠️ 挂在 watcher 而不是 client 上:
        #    cli 全程只建一个 watcher,缓存才跨得了轮;而 bot 侧那个 client 是另一个实例,
        #    两边共用一份缓存反而会让"这一轮打了几个请求"变得不可预测。
        self._coin_cache: dict[str, tuple[float, CoinStats]] = {}

    # ---- 对外唯一入口 ----------------------------------------------------
    def run_once(self) -> int:
        """
        调度器直接调这个,返回本轮真正发出去的消息条数(调度器不看返回值,给测试用)。

        ⚠️⚠️ **本方法绝不抛异常**。它是这个功能与调度器之间的唯一接触面:
           异常逃逸出去,轻则 APScheduler 打一堆 job 崩溃栈,重则被上层某个 except
           当成"该停机了"—— 而这个功能的任何故障都不该影响别的 job。
        """
        try:
            return self._check()
        except Exception as e:  # noqa: BLE001
            logger.exception("pump.fun 巡检异常,下一轮继续: {}", e)
            return 0

    def close(self) -> None:
        """退出时释放连接池。⚠️ 同样不抛 —— 它跑在 cmd_run 的 finally 里。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 内部 ------------------------------------------------------------
    def _check(self) -> int:
        # ⚠️ 数据库连接**只在这一小段里持有**:下面全是网络 IO,最坏十几秒。
        #    WAL 下读不挡写,但一条连接横跨整段网络 IO 只会让 WAL 迟迟无法 checkpoint。
        with store.get_conn() as conn:
            watched = [_to_watched(r) for r in store.list_pump_users(conn)]
        if not watched:
            return 0

        # 地址 → 认领这个地址的人。**这是"只推我们盯的人"的那道闸**:swap-api 的
        # batch 响应是按地址分组的,任何不在这张表里的键一律丢弃。
        # ⚠️⚠️ 值是**列表**而不是单个人:曾经"重复地址只认先加进来的那个",
        #    后加的那个人于是在归属阶段拿到空列表 → _push 报「本来就没有该推的」→
        #    **快照照常前移** → 这笔变动对他永久消失,下一轮也不重来。
        #    名单里两个人共用一个钱包 = 同一个钱包的两个身份,两个都是用户自己加的,
        #    两条都发出去(消息重复看得见,静默丢数据看不见)。
        by_addr: dict[str, list[_Watched]] = {}
        for w in watched:
            for a in w.wallets:
                holders = by_addr.setdefault(a, [])
                if holders:
                    logger.warning("pump.fun 名单里 {} 与 {} 共用地址 {} —— "
                                   "这个地址上的成交两个人各推一条",
                                   holders[0].username or holders[0].user_id,
                                   w.username or w.user_id, a)
                holders.append(w)
        all_addrs = list(by_addr)

        # 第一段:每人一个请求,拿最近变动的一页持仓,diff 出候选
        # by_mint:  mint → [(人, 该人这个 mint 的最新持仓行), …]
        # observed: (chain_id, mint) → {本轮**亲眼看到**他还拿着的 user_id}
        # ⚠️⚠️ observed 是「👥 名单内至少 N 人持有」那句话的**唯一**依据。
        #    绝不从 pump_positions 快照表补人:那张表只 upsert 不删除,而 portfolio
        #    只拉 page 0 的 50 行 —— 某人在某个币上清了仓、那次清仓又恰好没被看见
        #    (进程停过,或他持仓多到 50 行放不下),他那行 amount_held 会永远停在旧值,
        #    于是三个月前的快照被当成"他现在还拿着"推出去。那是**主动说了一句假话**,
        #    比少报严重得多(少报只是"我们没看见")。
        observed: dict[tuple[str, str], set[str]] = {}
        by_mint: dict[str, list[tuple[_Watched, Position]]] = {}
        for w in watched:
            seen, changed = self._scan(w)
            for p in seen:
                if _holds_now(p):
                    observed.setdefault(p.key, set()).add(w.user_id)
            for pos in changed:
                by_mint.setdefault(pos.coin_mint, []).append((w, pos))
        if not by_mint:
            return 0

        # ⚠️ 请求预算的闸:变动的 mint 数直接等于本轮的额外请求数。
        #    超限的部分本轮**不处理、快照也不前移**,下一轮会被重新选中(信息不丢)。
        mints = sorted(by_mint)
        if len(mints) > self._max_mints:
            logger.warning(
                "pump.fun 本轮有 {} 个 mint 变动,超过单轮上限 {} —— 只处理前 {} 个,"
                "其余快照不前移、下一轮继续", len(mints), self._max_mints, self._max_mints,
            )
            mints = mints[:self._max_mints]

        # 第二段:只对变动的 mint 问真实成交
        cutoff_ts = time.time() - self._max_age
        cutoff_iso = _iso_utc(cutoff_ts)
        with store.get_conn() as conn:
            # ⚠️ 先清后读:掉出新鲜窗口的行永远不会再成为候选,留着只让表无限长大;
            #    清完再读,读到的就恰好是还有意义的那些。
            with store.tx(conn):
                store.prune_pump_pushed(conn, cutoff_iso)
            done = store.pump_pushed_since(conn, cutoff_iso)

        # ⚠️⚠️ budget 是**整轮唯一的一份推送预算**,一路传到 _push 那层做截断。
        #    在这里判一次、下面每层再各判一次自己的 20 = 上限根本不是上限。
        sent = 0
        budget = MAX_PUSH_PER_ROUND
        for mint in mints:
            if budget <= 0:
                logger.warning("pump.fun 本轮已发满单轮上限 {} 条 —— 剩下的 mint "
                               "快照不前移、下一轮继续", MAX_PUSH_PER_ROUND)
                break
            n, used = self._handle_mint(mint, by_mint[mint], by_addr, all_addrs,
                                        cutoff_ts, done, budget, observed)
            sent += n
            budget -= used
        return sent

    def _scan(self, w: _Watched) -> tuple[list[Position], list[Position]]:
        """
        一个人这一轮的 (本轮观测到的全部持仓行, 其中发生变动的那些)。

        ⚠️ 拉取失败 → 两个都是空,且快照**一动不动**。动了就等于把这段时间的变动
           静默吃掉;而"没拉到"绝不能被下游读成"他一个币都不拿"—— 空列表在这里的
           含义只是"本轮没有任何正面观测",人数那句话据此**少算**他,不会多算。
        ⚠️ 播种轮:变动那半必然是空(一条都不推),但**观测到的那半照常返回** ——
           他的持仓是本轮亲眼看到的,拿来数人数与"要不要推他自己的成交"无关。
        """
        if w.portfolio_wallet is None:
            logger.warning("pump.fun 用户 {} 没有可用钱包,跳过", w.username or w.user_id)
            return [], []
        positions = self._client.fetch_portfolio(w.portfolio_wallet)
        if positions is None:
            return [], []

        with store.get_conn() as conn:
            prev = store.pump_positions(conn, w.user_id)
            if not w.seeded:
                # ⚠️⚠️ 冷启动静默播种:第一轮**一条都不推**,只把当前持仓记为已知。
                #    不播种的话 hexiecs 那 50 行(1000XCryptoD 更是 1905 个持仓)
                #    会在第一轮全部当成"刚变动"推出去,用户当场静音,功能第一天就死。
                with store.tx(conn):
                    store.upsert_pump_positions(conn, w.user_id, [_snap_row(p) for p in positions])
                    store.mark_pump_seeded(conn, w.user_id)
                logger.info("pump.fun 播种 {}:记下 {} 行持仓为已知,本轮一条都不推",
                            w.username or w.user_id, len(positions))
                return positions, []

        return positions, [p for p in positions if _position_changed(prev.get(p.key), p)]

    def _handle_mint(self, mint: str, holders: list[tuple[_Watched, Position]],
                     by_addr: dict[str, list[_Watched]], all_addrs: list[str],
                     cutoff_ts: float, done: set, budget: int,
                     observed: dict[tuple[str, str], set[str]]) -> tuple[int, int]:
        """
        一个变动的 mint:问逐笔成交 → 过滤 → 推送 → 只对**推干净了**的人前移快照。

        返回 (真正发出去的条数, 消耗掉的推送预算)。
        ⚠️ 消耗的是**尝试发的条数**而不是发成功的条数:预算存在的理由是防刷屏,
           而"发过去被 TG 拒了"同样占掉了这一轮的额度,不该让下一个人再借一次。

        ⚠️⚠️ 快照前移与推送成功是绑在一起的:任何一笔该推而没推成的,
           这个人这个 mint 的快照就**不前移**,下一轮它仍然与快照不一致、会被重新选中。
           反过来写(先前移快照)= 一次 TG 400 就把这笔成交永久判死。
        """
        batch = self._client.fetch_trades(mint, all_addrs)
        if batch is None:
            # 这个 mint 本轮问不到 → 快照不动,下一轮重来
            return 0, 0

        # 归属:只认在我们名单里的地址。
        # ⚠️⚠️ 这道闸是"未被盯的人的成交绝不外泄"的唯一保障 —— batch 响应里
        #    出现任何我们没问过/不认识的地址,一律丢弃并留痕。
        per_user: dict[str, list[Trade]] = {}
        for addr, trades in batch.items():
            ws = by_addr.get(addr)
            if not ws:
                logger.warning("pump.fun trades/batch 返回了名单外的地址 {} —— 已丢弃", addr)
                continue
            for t in trades:
                # 报文自己带的 userAddress 与分组键对不上 = 上游把数据串了,同样丢弃
                if t.user_address and t.user_address != addr:
                    logger.warning("pump.fun 成交 {} 的 userAddress 与分组键不符,已丢弃", t.tx)
                    continue
                # 共用这个地址的人各拿一份(绝大多数情况 ws 就一个人)
                for w in ws:
                    per_user.setdefault(w.user_id, []).append(t)

        sent = 0
        used = 0
        for w, rows in _group_by_user(mint, holders):
            # ⚠️ 这里**刻意不加**「预算见底就 break」那道闸:_push 拿到 0 额度自然就
            #    一条不发、all_ok=False、快照不前移,结果完全一样;而 break 会连
            #    "本来就没有该推的"那些人也一起卡住 —— 他们的快照本该照常前移。
            #    (多一道等价的闸还有个隐性代价:两道闸互相遮蔽,改坏一道测试照样绿。)
            # 推送用第一行(取符号与链名),快照对该人这个 mint 的**全部**行一起前移。
            # ⚠️ rows 有多行只在一种退化情况下发生:同一个人在两条链上持有同一个地址串
            #    (swap-api 的 URL 里只有 mint、没有 chainId,它自己也不区分)。
            #    那时逐行推等于把同一笔成交说两遍,所以只推一次。
            fresh = self._pick_fresh(per_user.get(w.user_id, []), w, mint, cutoff_ts, done)
            room = budget - used
            # ⚠️ 市值与人数**只在真要发消息时才求**:变动的 mint 里有相当一部分
            #    最后一条都推不出来(金额没过门槛 / 台账里已推过 / 掉出新鲜窗口),
            #    无条件先问一次 coins-v3 就是拿限流额度换一个没人会看到的数。
            #    额度已经见底时同理 —— 那些成交要留到下一轮才推。
            need = bool(fresh) and room > 0
            stats = self._coin_stats(mint) if need else None
            n_holders = len(observed.get(rows[0].key, ())) if need else None
            ok, n, tried = self._push(w, rows[0], fresh, room, stats, n_holders)
            sent += n
            used += tried
            if ok:
                # 推干净了(或本来就没有该推的)→ 这个人这个 mint 的快照可以前移
                with store.get_conn() as conn, store.tx(conn):
                    store.upsert_pump_positions(conn, w.user_id, [_snap_row(p) for p in rows])
        return sent, used

    def _pick_fresh(self, trades: list[Trade], w: _Watched, mint: str,
                    cutoff_ts: float, done: set) -> list[Trade]:
        """
        该推的那几笔:新鲜窗口内 + 过金额门槛 + 不在已推台账里。按时刻正序。

        ⚠️ 新鲜窗口不是"防御性编程":swap-api 返回的是这个人在这个 mint 上的**一段历史**。
           没有窗口的话,一个持有半年的币今天动一下,半年前的成交会被整段推出来。
        ⚠️ 金额门槛用 `is not None and >=`:拿不到金额就证明不了它过线,
           不推并留痕(实测响应里这个字段一条都不缺,真出现说明上游变了)。
           **绝不用真值判断** —— 金额恰好是 0 的成交存在,那是真实值不是缺失。
        ⚠️⚠️ 台账只挡**跨轮**重复(done 是轮首读的一份快照),所以这里必须自己挡
           **轮内**重复:上游在同一个数组里给出两条同 tx 同 slotIndexId 的行,
           或者同一笔成交同时挂在这个人的两个钱包名下,不去重就是两条一模一样的消息。
        """
        out = []
        seen = set()
        for t in trades:
            if t.traded_ts <= cutoff_ts:
                continue
            if t.amount_usd is None:
                logger.warning("pump.fun 成交 {} 取不到成交金额,无法判门槛 —— 本笔不推", t.tx)
                continue
            if t.amount_usd < self._min_usd:
                continue
            key = t.ledger_key(w.user_id, mint)
            if key in done or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return sorted(out, key=lambda x: (x.traded_ts, x.tx))

    def _coin_stats(self, mint: str) -> CoinStats | None:
        """市值(带 TTL 缓存)。实现见 _coin_stats_cached —— 观点那个 watcher 共用同一份逻辑。"""
        return _coin_stats_cached(self._client, self._coin_cache, mint)

    def _push(self, w: _Watched, pos: Position, fresh: list[Trade], budget: int,
              stats: CoinStats | None = None,
              holders_in_list: int | None = None) -> tuple[bool, int, int]:
        """
        逐笔推送。返回 (是否全都推成功了, 真正发出去的条数, 尝试发的条数)。

        budget 是**本轮剩下的全局额度**(不是这个人这个币的额度)——
        超出的部分本轮不发,靠 all_ok=False 让快照不前移、下一轮接着推。

        stats / holders_in_list 允许为 None(市值问不到 / 没算)——
        对应的行整行消失,成交本身照推。**市值绝不是推送的前置条件。**

        ⚠️⚠️ **推送成功才记台账**(与 poller._dispatch 的 `ok = notifier.send(...)`
           / `if ok:` 同一条铁律)。反过来写的话,一次 TG 400 或网络抖动
           就是这笔成交的永久丢失 —— 而且没有任何日志能让人发现。
        """
        all_ok = True
        sent = 0
        if len(fresh) > budget:
            # 一个人在一个币上一轮之内有几十笔(高频拆单)是可能的。截断只截**本轮**:
            # 返回 all_ok=False 让快照不前移,剩下的下一轮接着推(已推的那些被台账挡住),
            # 每轮都在推进,不会卡死也不会丢。
            logger.warning("pump.fun {} 在 {} 上有 {} 笔待推,本轮只剩 {} 条额度 —— "
                           "本轮先推最早的 {} 笔,快照不前移、下一轮继续",
                           w.username or w.user_id, pos.coin_mint, len(fresh),
                           budget, budget)
            fresh, all_ok = fresh[:budget], False
        for t in fresh:
            text = render_pump_trade(
                username=w.username,
                side=t.side,
                token_symbol=pos.symbol,
                coin_mint=pos.coin_mint,
                amount_usd=t.amount_usd,
                price_usd=t.price_usd,
                # 持仓与盈亏全部来自 portfolio 那一行 —— **零额外请求**
                holding_usd=pos.value_usd,
                is_cleared=pos.is_cleared,
                unrealized_pnl_usd=pos.unrealized_pnl_usd,
                unrealized_pnl_pct=pos.unrealized_pnl_pct,
                realized_pnl_usd=pos.realized_pnl_usd,
                realized_pnl_pct=pos.realized_pnl_pct,
                market_cap_usd=None if stats is None else stats.market_cap_usd,
                ath_market_cap_usd=None if stats is None else stats.ath_market_cap_usd,
                holders_in_list=holders_in_list,
                traded_at=t.traded_at,
                network_id=pos.network_id,
                chain_display=pos.chain_display,
                tx=t.tx,
            )
            if not self._notifier.send(text):
                logger.error("pump.fun 成交推送失败(下一轮重试) | user={} mint={} tx={}",
                             w.username or w.user_id, pos.coin_mint, t.tx)
                all_ok = False
                continue
            sent += 1
            with store.get_conn() as conn, store.tx(conn):
                store.record_pump_pushed(
                    conn, [(w.user_id, pos.coin_mint, t.tx, t.slot_index_id, t.traded_at)])
        return all_ok, sent, len(fresh)


# ============================================================
# pump.fun 观点(callout)巡检
# ============================================================
# 一轮最多发几条观点(**所有人合计**)。
# ⚠️⚠️ 「所有人合计」是字面意思,与买卖那边的 MAX_PUSH_PER_ROUND 是同一条教训:
#    它是**整轮一个预算**,一路扣减着传到 _push 那层做截断,
#    不是每个人各带一个。曾经每层各判一次,于是"处理下一个人之前 sent<20"通过后,
#    那个人自己还能再发满 20 条 —— 实测两个 mint 发了 39 条,而日志还在说
#    「本轮先推最早的 20 笔」。这里从第一天就写成一份预算,并且有用例钉着
#    **真实上界**(两个人各 8 条、上限 10 → 总共只能发 10 条),而不是钉文档里那个数。
# ⚠️ 比买卖那边的 20 小一半是有依据的:观点是天级事件(实测最活跃的 hexiecs
#    约 3.2 条/天),一轮真要冒出 10 条以上只可能是上游异常或我们自己算错了窗口 ——
#    那种时候更该把闸关小,而不是把用户的通知栏填满。
MAX_CALLOUT_PUSH_PER_ROUND = 10

# 一次问多少条观点。⚠️ 只拉第一页、不翻页(见 PumpClient.fetch_callouts)。
CALLOUT_PAGE_SIZE = 30


class PumpCalloutWatcher:
    """
    pump.fun **观点**巡检 —— 与买卖那个 watcher 是两个独立的 job、两个独立的开关。

    ⚠️⚠️ 与 PumpWatcher 分开的理由,每一条都够独立成立:
      1) **含义不同**:一个是"他真金白银动手了",一个是"他公开说了句话"。
         真实存在只想要其中一路的人,合成一个开关等于逼他们连噪音一起收。
      2) **成本模型不同**:观点是纯按人计费(每人每轮 1 个请求),
         买卖还要按变动的 mint 计费 —— 两者的限流账不是一本。
      3) **故障不该互相牵连**:portfolio 挂了不该让观点也停,反之亦然。
    ⚠️⚠️ 与 PumpWatcher 同一条铁律:**只做通知,永远不接跟单执行器。**
       一条 callout 就是一句话,它不构成任何关于他仓位的证据。
    """

    def __init__(self, notifier, client: PumpClient | None = None) -> None:
        s = get_settings()
        self._notifier = notifier
        self._client = client if client is not None else PumpClient()
        self._max_age = s.fomo_pump_callout_max_age_sec
        # mint → (取到的时刻, 市值)。⚠️ 与 PumpWatcher 那份**各用各的**:
        #    两个 job 跑在不同线程上,共用一个 dict 就要上锁,而它们各自的命中率
        #    本来就高(同一轮里同一个 mint 只查一次),共用换不到什么。
        self._coin_cache: dict[str, tuple[float, CoinStats]] = {}

    # ---- 对外唯一入口 ----------------------------------------------------
    def run_once(self) -> int:
        """
        调度器直接调这个,返回本轮真正发出去的消息条数(调度器不看返回值,给测试用)。

        ⚠️⚠️ **本方法绝不抛异常**,与 PumpWatcher.run_once 同一条契约:
           异常逃逸出去,轻则 APScheduler 打一堆 job 崩溃栈,重则被上层某个 except
           当成"该停机了"—— 而这个功能的任何故障都不该影响别的 job。
        """
        try:
            return self._check()
        except Exception as e:  # noqa: BLE001
            logger.exception("pump.fun 观点巡检异常,下一轮继续: {}", e)
            return 0

    def close(self) -> None:
        """退出时释放连接池。⚠️ 同样不抛 —— 它跑在 cmd_run 的 finally 里。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 内部 ------------------------------------------------------------
    def _check(self) -> int:
        with store.get_conn() as conn:
            watched = [_to_watched(r) for r in store.list_pump_users(conn)]
        if not watched:
            return 0

        cutoff_ts = time.time() - self._max_age
        cutoff_iso = _iso_utc(cutoff_ts)
        with store.get_conn() as conn:
            # ⚠️ 先清后读:掉出新鲜窗口的观点永远不会再成为候选,留着只让表无限长大;
            #    清完再读,读到的就恰好是还有意义的那些。
            with store.tx(conn):
                store.prune_pump_callouts_pushed(conn, cutoff_iso)
            done = store.pump_callouts_pushed_since(conn, cutoff_iso)

        # ⚠️⚠️ budget 是**整轮唯一的一份推送预算**,谁先用谁扣掉。
        sent = 0
        budget = MAX_CALLOUT_PUSH_PER_ROUND
        for w in watched:
            if budget <= 0:
                # ⚠️ 剩下的人本轮**连请求都不打**:发不出去的东西没必要先问回来。
                #    他们的台账没被动过,那些观点下一轮仍然是候选(直到掉出新鲜窗口),
                #    播种位也没被动过 —— 信息不丢,只是慢一轮。
                # ⚠️ 代价说清楚:名单顺序固定(按 added_at),所以**总是同几个人被跳过**。
                #    观点是天级事件(实测最活跃的约 3.2 条/天),一轮凑满 10 条本身
                #    就说明上游异常;为这种情况加一套轮转反而会让"谁被推了"变得不可预测。
                logger.warning("pump.fun 观点本轮已发满单轮上限 {} 条 —— "
                               "剩下的人下一轮再扫", MAX_CALLOUT_PUSH_PER_ROUND)
                break
            rows = self._client.fetch_callouts(w.user_id, CALLOUT_PAGE_SIZE)
            if rows is None:
                # 本轮问不到 → 台账与播种位都**一动不动**,下一轮重来
                continue
            self._warn_if_identity_leaked(w, rows)
            if not w.callout_seeded:
                self._seed(w, rows)
                continue
            fresh = self._pick_fresh(rows, cutoff_ts, done)
            n, tried = self._push(w, fresh, budget)
            sent += n
            budget -= tried
        return sent

    @staticmethod
    def _warn_if_identity_leaked(w: _Watched, rows: list[Callout]) -> None:
        """
        匿名请求却拿回了观看者状态 = 这条链路上混进了身份。

        ⚠️⚠️ 这是**安全告警**,不是数据处理:本模块的硬约束是"不登录、不带任何凭据",
           而 hasLiked / hasReposted 在匿名下实测恒为 null。它们一旦是 true/false,
           就说明有 cookie 或令牌被带上了 —— 要立刻在日志里看得见。
        ⚠️ 只记日志、**不改变行为**(与 _check_rate 同一条):告警不该顺手把功能关掉。
        """
        leaked = [c.callout_id for c in rows if c.viewer_state_leaked]
        if leaked:
            logger.warning(
                "pump.fun 观点响应带回了观看者状态(hasLiked/hasReposted 非 null)—— "
                "说明这次请求带上了身份,本模块必须是匿名的 | user={} 条数={} 首条={}",
                w.username or w.user_id, len(leaked), leaked[0])

    def _seed(self, w: _Watched, rows: list[Callout]) -> None:
        """
        冷启动静默播种:第一轮**一条都不推**,只把当前这一页观点记成已知。

        ⚠️⚠️ 不播种的话,开关打开的第一轮会把这个人的整页历史观点全推出去
           (实测 hexiecs 光最近 5 条就跨了 37.6 小时)—— 用户当场静音,
           功能第一天就死。与买卖那边的持仓播种是同一条。
        ⚠️ 播种写进去的行含义是「这条在我们开始盯之前就已经存在,永远不推」,
           与"推过了"占同一张表、同一个主键 —— 下游只问一件事:要不要推。
        ⚠️ 写台账与置播种位必须在**同一个事务**里:拆开的话中途崩一次就会留下
           "播种位置了但台账没写" —— 下一轮把这一整页当成新观点全推一遍。
        """
        with store.get_conn() as conn, store.tx(conn):
            store.record_pump_callouts_pushed(
                conn, [(c.callout_id, w.user_id, c.coin_mint, c.created_at) for c in rows])
            store.mark_pump_callout_seeded(conn, w.user_id)
        logger.info("pump.fun 观点播种 {}:记下 {} 条为已知,本轮一条都不推",
                    w.username or w.user_id, len(rows))

    def _pick_fresh(self, rows: list[Callout], cutoff_ts: float, done: set) -> list[Callout]:
        """
        该推的那几条:新鲜窗口内 + 不在台账里。按发表时刻正序(先发的先推)。

        ⚠️ 新鲜窗口不是"防御性编程":/callout/list 返回的是这个人的**一段历史**。
           没有窗口的话,进程停一天再起来就是把几十条隔夜观点一次性倒出来。
        ⚠️⚠️ 台账只挡**跨轮**重复(done 是轮首读的一份快照),所以这里必须自己挡
           **轮内**重复:同一页里出现两条同 calloutId 的行(上游翻页边界抖动)
           不去重就是两条一模一样的消息。
        """
        out: list[Callout] = []
        seen: set[str] = set()
        for c in rows:
            if c.created_ts <= cutoff_ts:
                continue
            if c.callout_id in done or c.callout_id in seen:
                continue
            seen.add(c.callout_id)
            out.append(c)
        return sorted(out, key=lambda x: (x.created_ts, x.callout_id))

    def _push(self, w: _Watched, fresh: list[Callout], budget: int) -> tuple[int, int]:
        """
        逐条推送。返回 (真正发出去的条数, 尝试发的条数)。

        budget 是**本轮剩下的全局额度**(不是这个人的额度)—— 超出的部分本轮不发,
        台账没记,下一轮它们仍然是候选,会被接着推。

        ⚠️⚠️ **推送成功才记台账**(与 poller._dispatch 的 `ok = notifier.send(...)`
           / `if ok:` 同一条铁律)。反过来写的话,一次 TG 400 或网络抖动
           就是这条观点的永久丢失 —— 而且没有任何日志能让人发现。
        ⚠️ 返回的"尝试发的条数"而不是"发成功的条数":预算存在的理由是防刷屏,
           而"发过去被 TG 拒了"同样占掉了这一轮的额度。
        """
        if len(fresh) > budget:
            logger.warning("pump.fun {} 有 {} 条观点待推,本轮只剩 {} 条额度 —— "
                           "先推最早的 {} 条,其余下一轮继续",
                           w.username or w.user_id, len(fresh), budget, budget)
            fresh = fresh[:budget]
        sent = 0
        for c in fresh:
            # ⚠️ 币的符号与链**只能**从 coins-v3 拿:/callout/list 只给 coinMint。
            #    带 TTL 缓存,且只对**真要发**的那几条问 —— 掉出窗口 / 已推过的
            #    那些一个请求都不该花(与买卖那边"市值只在真要发消息时才求"同一条)。
            stats = self._coin_stats(c.coin_mint) if c.coin_mint else None
            text = render_pump_callout(
                username=w.username,
                thesis=c.thesis,
                coin_mint=c.coin_mint,
                token_symbol=None if stats is None else stats.symbol,
                # ⚠️ 用 callout 自己的 marketCap(**发表那一刻**的市值),
                #    绝不拿 coins-v3 的当前市值顶替 —— 那是另一个时刻的另一个数。
                market_cap_usd=c.market_cap_usd,
                multiple=c.multiple,
                likes=c.likes,
                view_count=c.view_count,
                created_at=c.created_at,
                network_id=None if stats is None else stats.network_id,
                chain_display=None if stats is None else stats.chain_display,
            )
            if not self._notifier.send(text):
                logger.error("pump.fun 观点推送失败(下一轮重试) | user={} callout={}",
                             w.username or w.user_id, c.callout_id)
                continue
            sent += 1
            with store.get_conn() as conn, store.tx(conn):
                store.record_pump_callouts_pushed(
                    conn, [(c.callout_id, w.user_id, c.coin_mint, c.created_at)])
        return sent, len(fresh)

    def _coin_stats(self, mint: str) -> CoinStats | None:
        """市值/符号/链(带 TTL 缓存)。实现与买卖那个 watcher 共用同一份逻辑。"""
        return _coin_stats_cached(self._client, self._coin_cache, mint)


# ============================================================
# 小工具
# ============================================================
def _coin_stats_cached(client, cache: dict[str, tuple[float, CoinStats]],
                       mint: str) -> CoinStats | None:
    """
    /coins-v3 的 TTL 缓存。两个 watcher 共用这一份实现(各自带各自的 dict)。

    ⚠️ 刻意做成函数而不是各写一遍:这里有一条**很容易在复制时丢掉**的不变式 ——
    ⚠️⚠️ **失败绝不入缓存**:缓存一次失败 = 让一次网络抖动把市值行按住整个 TTL,
       而且没有任何日志会说"这行是被缓存按住的"。下一次调用重新问一遍才对
       (而"下一次"最快也是下一轮,不会打成风暴)。
    """
    now = time.time()
    hit = cache.get(mint)
    if hit is not None and now - hit[0] < COIN_STATS_TTL_SEC:
        return hit[1]
    stats = client.fetch_coin(mint)
    if stats is None:
        return None
    # 顺手清掉过期项 —— 名单和变动的 mint 都是长尾,不清的话这个 dict 只增不减
    for k, (ts, _v) in list(cache.items()):
        if now - ts >= COIN_STATS_TTL_SEC:
            del cache[k]
    cache[mint] = (now, stats)
    return stats


def _to_watched(row) -> _Watched:
    """DB 行 → _Watched。两个钱包都归一化后去重(有人可能两边填了同一个)。"""
    wallets: list[str] = []
    for col in ("svm_wallet", "evm_wallet"):
        a = normalize_wallet(row[col])
        if a is not None and a not in wallets:
            wallets.append(a)
    return _Watched(
        user_id=row["user_id"],
        username=row["username"],
        wallets=tuple(wallets),
        portfolio_wallet=normalize_wallet(row["svm_wallet"]) or normalize_wallet(row["evm_wallet"]),
        seeded=row["seeded"] == 1,
        callout_seeded=row["callout_seeded"] == 1,
    )


def _group_by_user(mint: str, holders: list[tuple[_Watched, Position]]):
    """
    [(人, 持仓), …] → [(人, 该人这个 mint 的全部持仓行), …],保持原有顺序。

    ⚠️ 存在的理由:同一个人可能在两条链上持有同一个地址串,而 swap-api 的
       trades/batch URL 里只有 mint、没有 chainId —— 它返回的是同一批成交。
       不合并的话同一笔成交会被推两遍(两条链名各一遍),其中至少一条是错的。
    """
    order: list[str] = []
    buckets: dict[str, tuple[_Watched, list[Position]]] = {}
    for w, pos in holders:
        if w.user_id not in buckets:
            order.append(w.user_id)
            buckets[w.user_id] = (w, [])
        else:
            logger.warning("pump.fun {} 在多条链上持有同一个地址串 {} —— 只推一条,"
                           "快照两条都前移", w.username or w.user_id, mint)
        buckets[w.user_id][1].append(pos)
    return [buckets[uid] for uid in order]


def _holds_now(p: Position) -> bool:
    """
    这一行**本轮**能不能支撑「他还拿着这个币」这句断言。

    ⚠️⚠️ 三个条件缺一不可,而且每一个都对应一种"说不出口"的情况:
       · amount_held is None —— 这一轮没拿到量,是"不知道",不是"拿着";
       · amount_held == 0    —— 0 是真实值,含义恰恰是"已清仓";
       · is_cleared          —— 上游自己打了 isExited 标记。量与标记打架时
         (量 > 0 却标着已清仓)两边都不可信,那就一句都不说 —— 这个数字
         宁可少算,也绝不能多算一个人。
    ⚠️ 用 is None 判空而不是真值判断:真值判断会把"清仓"和"没拿到量"混成一件事。
    """
    return p.amount_held is not None and p.amount_held > 0 and not p.is_cleared


def _snap_row(p: Position):
    """Position → store.upsert_pump_positions 的一行(store 层不认业务类型,只收元组)。"""
    return (p.chain_id, p.coin_mint, p.amount_held, p.realized_pnl_usd, p.updated_at)


def _position_changed(prev, now: Position) -> bool:
    """
    这一行相对上一轮变了没有。

    ⚠️⚠️ 判据是 **amountHeld / realizedPnlUsd**,刻意**不用 updatedAt**:
       持仓行的 updatedAt 会随行情刷新而动(valueUsd / tokenPriceUsd 都在里面),
       拿它当触发器等于每轮把整页 mint 全问一遍 —— 白烧限流预算。
       而 amountHeld 与 realizedPnlUsd 只在仓位真的动了(买/卖/转)时才变。
    ⚠️ 不做 epsilon 容差:同一份报文解析出的是同一个 float(实测两次枚举逐字节相同),
       而真实成交必然改变它。加容差只会把小额成交静默吃掉。
    ⚠️ 判空一律 is None:amountHeld=0 是"已清仓",是真实值不是缺失 ——
       用真值判断会让"从 0 变成 100"和"从 100 变成 0"都读成"没变"。
    """
    if prev is None:
        return True                      # 上一轮没有这一行 = 新出现的持仓
    return (prev["amount_held"] != now.amount_held
            or prev["realized_pnl_usd"] != now.realized_pnl_usd)
