"""
配置加载模块 —— 从 .env 读取所有配置,pydantic 校验类型

⚠️ 整个项目只在本文件调用 os.getenv / load_dotenv,其他模块一律从 settings 取值。
⚠️ 本类必须独立继承 BaseSettings,绝不复用 claudeTrade 的 Settings ——
   那个类对 binance_api_key 有"必填 + 长度 + 占位符"三重校验,
   本项目根本不碰币安,继承过来会导致没配币安 Key 的机器直接启动失败。
"""
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录(本文件的上一级)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# 数据目录(DB / 登录态 / probe dump 都放这里,整个目录已在 .gitignore 里)
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# 登录态文件:含 Privy refresh token,泄漏 = 账号被接管
SESSION_FILE = DATA_DIR / "fomo_session.json"
# probe 原始响应 dump 目录
PROBE_DIR = DATA_DIR / "fomo_probe"
# 登录用的浏览器持久化 profile。
# 用持久化 profile 而不是每次开一个全新的临时 profile,有两个实际好处:
#   1) 全新 profile 本身就是"自动化"的特征之一,Google OAuth 会因此拒绝登录
#   2) 登录态留在 profile 里,下次 --login 通常不用重新走一遍第三方授权
PROFILE_DIR = DATA_DIR / "playwright_profile"


# ============================================================
# 买入推送的市值区间(FOMO 买入 + pump.fun 买入成交)
# ============================================================
# 市值写法的**白名单**正则。与 bot._TIN_MIN_RE 同一条理由:刻意不用裸 float() ——
#    float("nan") / float("inf") / float("1e999") 全都不报错,而 `mcap <= nan` 恒为 False,
#    配错一个字符 = 从此一条买入都不推,且没有任何报错。
# ⚠️ 数字位写 [0-9] 而不是 \d:\d 连全角「５」都收,看不出区别的字符就能悄悄变成另一个值。
_MCAP_RE = re.compile(
    r"^\$?\s*(?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    r"(?:\.(?P<frac>[0-9]+))?\s*(?P<unit>[kKmMbBwW万])?$"
)
# ⚠️⚠️ 这里**收 M**,与 /tin 的金额(刻意不收 m)口径不同,理由是语境不同:
#    /tin 是聊天框里敲的美元金额,M 在金融里既被写成 million 也被写成千(罗马数字),
#    猜错一次就是差 1000 倍。而**市值**这个语境里 K/M/B 没有歧义 —— 本项目自己的推送
#    就印成「💎 市值 $573.66K / $19.14M」,用户是照着推送里看到的写法填进来的;
#    不收 M 反而逼人把 1.5M 写成 1500000,多敲一个 0 就是差 10 倍且同样静默。
#    防手滑的兜底不靠拒收,靠**启动日志把解析后的区间原样印出来**(≤ $1.50M),一眼能对上。
# ⚠️ 用 Decimal 相乘:float("1.1") * 1000 = 1100.0000000000002,
#    边界「≤ 1.1K」会把市值恰好 1100 的币筛掉 —— 差一个 ulp 的静默漏推。
_MCAP_UNITS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "w": 10_000, "万": 10_000}


def parse_market_cap(v) -> float | None:
    """
    市值配置值 → 美元数。None / 空串 = 不设(不限);写坏了**抛 ValueError**(启动即报错)。

    认:500000 · 500,000 · $500000 · 500K · 1.5M · 2B · 50w · 50万 · 0。
    ⚠️ 写坏了必须抛、不许回落成「不限」:一个拼错的上限静默变成不限,
       用户会以为自己在看小盘,实际收的是全量 —— 反过来也一样糟。
    ⚠️ 0 是合法值(下限 0 = 不限下限的显式写法;上限 0 = 只推市值恰好为 0 的),不是"没设"。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"市值不能是布尔值: {v!r}")
    if isinstance(v, int | float):
        f = float(v)
    else:
        s = str(v).strip()
        if s == "":
            return None
        m = _MCAP_RE.match(s)
        if m is None:
            raise ValueError(f"认不出的市值写法: {s!r}(例: 500K / 1.5M / 500000;不能为负)")
        d = Decimal(m.group("int").replace(",", ""))
        if m.group("frac"):
            d += Decimal("0." + m.group("frac"))
        unit = m.group("unit")
        if unit:
            d *= _MCAP_UNITS[unit.casefold()]
        f = float(d)
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"市值必须是 ≥ 0 的有限数: {v!r}")
    return f


@dataclass(frozen=True)
class MarketCapRange:
    """
    买入推送的市值区间。**全项目唯一的判据**:FOMO 与 pump.fun 两边都只调 allows()。

    ⚠️ 只管「这个市值在不在区间里」,**不管事件类型** —— 只筛买入这件事由两个调用方
       各自在调用前判(两边的方向字段不是一个类型:EVENT_BUY 与 "buy")。
    """

    min_usd: float | None
    max_usd: float | None
    push_unknown: bool

    @property
    def enabled(self) -> bool:
        """上下限都没设 = 功能关闭。⚠️ push_unknown 单独设不算开启(见 allows)。"""
        return self.min_usd is not None or self.max_usd is not None

    def allows(self, market_cap: float | None) -> bool:
        """
        这个市值的买入该不该推。

        ⚠️ 关闭时恒 True —— 包括市值缺失:只设 push_unknown=false 不设区间时,
           行为必须与没有这个功能逐字节一致(否则「没配区间」也会悄悄筛掉三成买入)。
        ⚠️ 判空必须 is None:0 是真实值。0 < 下限就该筛掉,绝不能落进「无市值照推」那一支。
        ⚠️ 两端**含边界**(≥ 下限、≤ 上限):中文「500K 以下」通常含 500K 本身。
        """
        if not self.enabled:
            return True
        if market_cap is None:
            return self.push_unknown
        if self.min_usd is not None and market_cap < self.min_usd:
            return False
        if self.max_usd is not None and market_cap > self.max_usd:
            return False
        return True


class FomoSettings(BaseSettings):
    """全局配置,启动时一次加载"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Telegram ----------
    fomo_telegram_bot_token: SecretStr | None = Field(None, description="Bot Token")
    fomo_telegram_chat_id: str | None = Field(None, description="推送目标 chat_id")
    # 只有这个 chat_id 发的命令才会被执行;留空则回落到 fomo_telegram_chat_id
    fomo_telegram_admin_chat_id: str | None = Field(None)

    # ---------- FOMO 客户端 ----------
    # http = curl_cffi 直连(轻量);playwright = 页面上下文内 fetch(兜底,吃内存)
    # 先跑 --probe 确认 http 能不能过 Cloudflare
    fomo_client_impl: str = Field("http")

    # ---------- 轮询 ----------
    fomo_poll_interval_sec: int = Field(15, ge=5, description="轮询间隔(秒)")
    # ---------- 价格历史采集(供仪表盘画 sparkline) ----------
    # 采样降频(轮数)。15s × 60 = 15 分钟一次。
    # ⚠️ 这个数字最初错定成了 5 分钟(20 轮),依据是"要看清日内插针"——
    #    但那是拿全尺寸图表的精度在想问题。这份数据的第一用途是卡片上的
    #    sparkline:一条约 120px 宽的迷你走势线,渲染时本来就要把点位降采样到
    #    几十个,3 天窗口给 864 个点(5 分钟粒度)纯属浪费,画不出来的细节
    #    只会白白吃磁盘。15 分钟粒度下 3 天是 288 个点,对 sparkline 绰绰有余;
    #    日后如果真要支持"点开看全尺寸日内图"这种更细的场景,再单独加一档
    #    更高频的采样,而不是把这个默认值继续往小调。
    #    做成配置项而不是硬编码常量,是为了日后调整采样密度时不用改代码 ——
    #    参见 poller._maybe_sample_price_history。
    fomo_price_history_sample_ticks: int = Field(
        60, ge=1, description="价格历史采样间隔(轮数),默认 60 轮≈15 分钟(按 sparkline 渲染精度定的)"
    )
    # 保留天数。这些是 memecoin:买卖判定本身只看 24 小时窗口,copytrade 的
    # max_age_hours 同样以 24 小时为界,3 天足够覆盖一个币"冒头→暴涨→归零"的
    # 整个可交易生命周期,再长只是白占磁盘。同样做成配置项,方便日后放宽。
    fomo_price_history_retain_days: int = Field(
        3, ge=1, description="价格历史保留天数,超过自动清理"
    )
    fomo_web_port: int = Field(8420, ge=1024, le=65535, description="网页版端口(仅本机)")
    fomo_backfill_max_items: int = Field(
        500, ge=0, description="/add 时回填多少条历史 swaps 建立首次买入判定基线"
    )
    # 推送**稳态**速率(秒/条)。突发由 poller._throttle_send 的令牌桶吸收:
    # 连着来 5 条可以立刻发完,再多才按这个速率匀速。
    fomo_send_interval_sec: float = Field(
        1.5, ge=0, description="推送稳态间隔(秒/条),突发另由令牌桶吸收"
    )
    # 拉取并发度。调度单元是**一个请求**不是一个用户(见 poller._fetch_snapshots)。
    # ⚠️ 实测有明确的拐点:69 人 × 3 端点在 12 线程 15.1s、24 线程 10.8s,
    #    但 36 线程反而掉到 66.6s —— 服务端/代理已经打满,再加只会换来 504 和重试风暴。
    # ⚠️ 真实峰值并发**不止这个数**:观点扫描跑在后台线程,与这里的池子同时在打,
    #    峰值 = 本值 + poller._THESIS_WORKERS。调这个值时按和算,别只看这一处 ——
    #    12+8=20 的时候实测已经会撞限流(同一毫秒 15 个请求一起 429)。
    fomo_fetch_workers: int = Field(12, ge=1, le=32, description="拉取并发线程数")
    # 距上一轮超过这么久(分钟)就认定"中间停过机":本轮事件照常入库,
    # 但不逐条推送,改发一条汇总。
    # 默认 45 分钟 —— 比正常轮询间隔(20-35s)大两个量级,不会被网络抖动误触发;
    # 又足够短,睡一觉起来必然命中。设成 0 可关闭该行为。
    fomo_catchup_threshold_min: int = Field(
        45, ge=0, description="超过这么久没跑就走停机汇总模式(分钟);0=关闭"
    )

    # ---------- 转入告警(「N 个名单成员收到同一个币」)----------
    # ⚠️⚠️ 这三项**刻意不放进 CopyConfig**。CopyConfig 是跟单开关(默认关、要花钱、
    #    由 /copy 命令改),而这个信号只推一条通知、永远不下单 ——
    #    「有人收到了免费筹码」与「有人自己掏钱买入」是相反的含义,混在同一个配置对象里
    #    迟早会有人顺手把它接进执行器。放在 .env,与轮询节奏那些参数同级。
    fomo_transfer_alert_receivers: int = Field(
        3, ge=2, description="同一个币有几个名单成员『收到』才告警"
    )
    fomo_transfer_alert_window_hours: int = Field(
        24, ge=1, description="统计『收到』的时间窗(小时),与买入信号保持一致"
    )
    # ⚠️ 别把它当成可有可无的调味料 —— 它是这个功能能不能上线的分水岭。
    #    实测(91 人 × 8404 条真实 transfers,在全员完整覆盖的 11.9 小时窗口上离线重放):
    #      不设门槛 → 138.8 次/天(用户会直接静音);$100 → 10.1;$500 → 6.0;$1000 → 4.0
    #    噪音几乎全是空投灰尘和平台级批量发放(美股代币化,单个币能有 41 人收到)。
    #    $500 与 $300 命中数相同,取 $500 更抗噪;再往上就会把真信号($fih 最低那笔
    #    $901.37)一起筛掉。
    # ⚠️ 这里是这个阈值的**唯一真源**。store 侧刻意不留默认值(count_recent_receivers 的
    #    min_usd 是必传参数)—— 两份默认值同时存在时,改了一份另一份不动,
    #    而读代码的人无从知道线上到底按哪个跑。
    fomo_transfer_alert_min_usd: float = Field(
        500.0, ge=0, description="单人到账金额下限(美元),低于此不计入『收到』人数"
    )

    # ---------- 指定用户的转入逐条推送(/tin)----------
    # ⚠️⚠️ **这是另一个功能的门槛,与上面那个 alert_min_usd 毫无关系,绝不许复用。**
    #    上面那个问的是「同一个币被**几个人**收到」(聚合信号,$500 是为了压掉平台级
    #    批量发放的噪音);这个问的是「**这一个人**又进货了没有」——
    #    被 /tin 点名的只有个位数人,噪音基数小两个量级,门槛自然该低得多。
    #    共用一个值意味着调其中一个功能的灵敏度会**静默**改掉另一个。
    # ⚠️ 同样**刻意不放进 CopyConfig**(理由见上面转入告警那段):CopyConfig 是会花钱的
    #    跟单开关,而这里只推一条通知、永远不下单。
    # ⚠️ 默认 $100 的依据 —— 人均条/天(本地库 19.1 天 · 95 个有转入记录的人 ·
    #    15,410 条 TRANSFER_IN,2026-08-30 复核):
    #      不设门槛 8.49 · ≥$100 1.10 · ≥$500 0.73 · ≥$1000 0.64
    #    最活跃的 @unipcs 在 ≥$500 也只有 2.72 条/天。$100 已经把空投灰尘压干净,
    #    再往上就开始筛掉真的小额建仓 —— 而"他又进货了"正是这个功能要看的。
    fomo_transfer_watch_min_usd: float = Field(
        100.0, ge=0, description="/tin 名单成员的单笔到账金额下限(美元),低于此不逐条推送"
    )

    # ---------- 币安 Alpha 新上架推送 ----------
    # ⚠️⚠️ 与转入告警同一条理由:**刻意不放进 CopyConfig**。
    #    「币安上了个新币」和「名单里的人掏钱买了」是完全不同的含义,
    #    放进那个对象里迟早会有人顺手把它接进执行器 —— 这个信号永远只推通知。
    fomo_alpha_enabled: bool = Field(True, description="币安 Alpha 新上架推送总开关")
    # 5 分钟。⚠️ 不能跟 FOMO 轮询用同一个节奏:Alpha 上新实测约 3~4 天一个
    #    (近 7 天 2 个、近 30 天 8 个),27 秒一次纯粹是白打币安接口、白挨风控。
    #    下限 60s 是防手滑写个 5 进去。
    fomo_alpha_interval_sec: int = Field(
        300, ge=60, description="币安 Alpha 巡检间隔(秒),上新是天级事件,不需要快"
    )
    # 要标注哪些板块,格式 `rankType:tabId:显示名`,多个用逗号分隔。
    # 默认 60:61 = App 钱包页「市場焦點」里的「股票 Meme 幣」(实测 21 个成员)。
    # ⚠️ 板块目录是币安运营编排的,没有任何文档,随时可能改名或下线 ——
    #    所以它只是推送里的一行标注,拿不到就整行消失,绝不影响主干。
    # ⚠️ 显示名里不能带逗号(那是条目分隔符);冒号可以,只切前两个。
    fomo_alpha_sectors: str = Field(
        "60:61:股票 Meme 幣",
        description="板块标注配置,格式 rankType:tabId:显示名,逗号分隔;留空则不标注",
    )

    # ---------- pump.fun 指定用户买卖监控 ----------
    # ⚠️⚠️ 与转入告警、币安 Alpha 同一条理由:**刻意不放进 CopyConfig**。
    #    CopyConfig 是会花钱的跟单开关,放进去迟早有人顺手把它接进执行器;
    #    这个信号永远只推通知,绝不参与下单判定。
    # ⚠️⚠️ 默认 **False**(与 alpha 的 True 刻意不同):这是新加的功能,
    #    老库升级后行为必须逐字节不变 —— 谁也不该因为拉了个新版本就突然多收一路推送。
    #    要用就自己在 .env 里打开,并且先 /pump add 把人加进来(名单空 = 一个请求都不打)。
    fomo_pump_enabled: bool = Field(False, description="pump.fun 指定用户买卖推送总开关")
    # 60 秒。⚠️ 不能跟 FOMO 轮询共用 27 秒:pump 持仓变动是小时级事件,
    #    27 秒一次纯粹是白挨 pump 的限流(portfolio 端点 60 次/分)。
    #    下限 30s 是防手滑写个 5 进去把限流打穿。
    fomo_pump_interval_sec: int = Field(
        60, ge=30, description="pump.fun 巡检间隔(秒)"
    )
    # 单笔成交的美元门槛。低于此**不推**。
    # ⚠️ 与 fomo_transfer_watch_min_usd 毫无关系,绝不复用:那个问的是 FOMO 平台上
    #    「这个人又收到货了没有」,这个问的是「他在 pump.fun 上真金白银成交了多少」——
    #    共用一个值意味着调其中一个功能的灵敏度会**静默**改掉另一个。
    # ⚠️ $50 的来由:实测 pump.fun 逐笔里存在 $0.0000028 这种粉尘级成交
    #    (探针实测的 PUNCHMA 卖出就是),不设门槛会被灰尘刷屏。
    fomo_pump_min_usd: float = Field(
        50.0, ge=0, description="pump.fun 单笔成交金额下限(美元),低于此不推送"
    )
    # 一轮最多对几个"变动的 mint"去问逐笔成交。
    # ⚠️ 这是**请求预算的闸**,不是防御性编程:变动的 mint 数直接等于本轮的额外请求数。
    #    一个人某天批量清仓几十个币时,不夹这道闸就是一轮打几十个请求。
    #    超限的部分本轮不查(下一轮它们仍然与快照不一致,还会被选中),不会丢。
    # ⚠️⚠️ **上界 15 不是拍脑袋,是从最密配置反推的**:
    #    一个变动的 mint 现在要花 2 个请求(swap-api 的逐笔 + frontend-api-v3 的市值),
    #    补市值之前只花 1 个 —— 同一个 max_mints 的真实成本已经翻了一倍。
    #    frontend-api-v3 实测 60/分(2026-08-31 亲测 /callout/list 与
    #    /user-portfolio 的 x-ratelimit-limit 都是 60),巡检间隔下限 30 秒 = 2 轮/分,
    #    于是 mint 驱动的那一路每分钟要吃掉 2 × max_mints 个额度。
    #    取 15 = 让它最多吃掉一半(30/分),另一半留给**按人计费**的两路
    #    (每人每轮 1 个 portfolio + 1 个 callout)。
    # ⚠️ 它挡不住的那一半要说清楚:名单人数 P 不在这个上界的管辖范围内
    #    (P 大到一定程度照样打穿),那一路只有 _RATE_LIMIT_WARN 在日志里喊。
    #    没有上界时手写一个 max_mints=60 + 间隔 30 就是 120/分,直接打穿 ——
    #    先把配置能写出来的那个洞堵上。
    fomo_pump_max_mints: int = Field(
        8, ge=1, le=15, description="pump.fun 单轮最多处理多少个变动的 mint"
    )
    # 成交的新鲜窗口(秒)。只推这个窗口之内的成交。
    # ⚠️⚠️ 必须有:swap-api 返回的是该 mint 上这个人的**一段历史**,不是"刚刚那笔"。
    #    没有窗口的话,一个持有半年的币今天动一下,半年前的成交会被整段推出来。
    # ⚠️ 代价要说清楚:进程停机超过这个窗口再启动,停机期间的成交会被判成"过旧"而不推
    #    (快照仍然照常前移)。这是刻意的取舍 —— 停机一天之后收到几百条隔夜成交,
    #    用户会当场静音,那比漏推更糟。
    fomo_pump_trade_max_age_sec: int = Field(
        7200, ge=60, description="pump.fun 成交新鲜窗口(秒),超过此年龄的成交不推送"
    )

    # ---------- pump.fun 观点(callout)监控 ----------
    # ⚠️⚠️ **与买卖那个开关刻意分开,不共用 fomo_pump_enabled。**
    #    这两路是**含义不同的两个信号**:一个是"他真金白银动手了"(成交),
    #    一个是"他公开说了句话"(观点)。真实存在只想要其中一路的人:
    #    只看观点的人嫌逐笔成交太吵(一个人一天能有几十笔),
    #    只看成交的人则认为喊单不构成任何证据。共用一个开关意味着
    #    想要一路就必须连另一路一起收,而"关掉噪音"的唯一办法会变成整个功能关掉。
    # ⚠️ 两个开关也让**成本**可分别控制:观点是按人计费(每人每轮 1 个请求),
    #    买卖是按人 + 按变动 mint 计费,两者的限流账不是一本。
    # ⚠️⚠️ 默认 **False**,与 fomo_pump_enabled 同一条理由:老库升级后行为逐字节不变,
    #    谁也不该因为拉了个新版本就突然多收一路推送。要用就自己在 .env 里打开。
    fomo_pump_callout_enabled: bool = Field(
        False, description="pump.fun 观点(callout)推送总开关"
    )
    # 观点的新鲜窗口(秒)。⚠️ 与成交那个是**两个值**,绝不复用:
    #    成交是分钟级事件、窗口短一点无所谓;观点是天级事件(实测最活跃的
    #    hexiecs 也只有约 3.2 条/天),窗口太短会让一次十几分钟的重启
    #    就把当天唯一一条观点判成"过旧"。
    # ⚠️ 但它同样**必须有**:/callout/list 给的是这个人的一段历史,
    #    没有窗口的话进程停一天再起来就是把几十条隔夜观点一次性倒出来。
    #    默认 7200(2 小时)与成交那边取同一个量级 —— 停机超过它就不补推,
    #    这是刻意的取舍(用户当场静音比漏推更糟)。
    fomo_pump_callout_max_age_sec: int = Field(
        7200, ge=60, description="pump.fun 观点新鲜窗口(秒),超过此年龄的观点不推送"
    )

    # ---------- 买入推送的市值区间(「只想看 500K 市值以下的」)----------
    # ⚠️⚠️ **只筛买入**:FOMO 的 BUY 与 pump.fun 的买入成交。卖出一律照推 ——
    #    跟的人 100K 买入(推了)、涨到 200 万时卖出,这条卖出恰恰最该看到。
    #    转入(/tin)、转入聚合、观点、币安 Alpha、盈利榜……一律不受影响。
    # ⚠️⚠️ **只抑制推送,不影响落库**:被筛掉的买入照常入库、照常计入「👥 名单内 N 人买过」
    #    与首次建仓判定,只是不发那条消息(并当场标成已处理,补发队列不会再捞它)。
    # ⚠️⚠️ **跟单信号不受影响**:它有自己的判据(CopyConfig),这组配置一行都不接进去。
    # ⚠️⚠️ **FOMO 与 pump.fun 刻意共用这一组值**。这与 fomo_pump_min_usd 那段
    #    「两个功能共用一个值 = 调一个会静默改另一个」并不矛盾:那条说的是两个功能
    #    **各自的灵敏度**(金额门槛的量级与噪音来源完全不同),而这里表达的是
    #    **同一个用户偏好**(我只关心小盘)—— 它跟着人走,不跟着平台走。
    #    分成两组的话,调小一边忘了另一边,才是真正的静默漂移。
    # ⚠️ 两个都不设 = 功能关闭,行为与没有这个功能逐字节一致(默认)。
    # ⚠️ 写法见 parse_market_cap:500K / 1.5M / 500000 都认;写坏了、为负、下限 > 上限
    #    一律**启动即报错** —— 绝不让一个配错的区间静默把所有买入筛没。
    # ⚠️ 实测(2026-09-10 生产库只读):有市值的买入里 < $500K 占 27.6%,中位数 $2.02M;
    #    设 500K 上限时有市值的买入推送从 ~1900 降到 ~300 条/天。
    fomo_buy_push_min_market_cap: float | None = Field(
        None, description="买入推送的市值下限(美元,含边界),不设=不限。例 50K"
    )
    fomo_buy_push_max_market_cap: float | None = Field(
        None, description="买入推送的市值上限(美元,含边界),不设=不限。例 500K"
    )
    # 拿不到市值的买入推不推。默认推。
    # ⚠️⚠️ 缺市值的比例很高(实测买入 31.6%),而且**新币/小币最容易缺** ——
    #    它们恰恰是设 500K 上限的人最想看的,默认筛掉等于把目标人群一起扔了。
    # ⚠️ 只在设了上下限时才生效;单独设它不开启任何筛选(启动日志会喊一句)。
    fomo_buy_push_unknown_market_cap: bool = Field(
        True, description="设了市值区间时,拿不到市值的买入是否照推"
    )
    # 买入推送的**单笔金额**下限(美元,含边界)。不设 = 不限(默认)。例:只推 ≥ $100 的买入就写 100。
    # ⚠️ 只筛 FOMO 的买入,卖出照推;被筛掉的照常入库、计入「👥 名单内 N 人买过」,只是不推。
    # ⚠️ pump.fun 有自己的单笔门槛 fomo_pump_min_usd(买卖都管),不受这一项影响。
    # ⚠️ 写法与市值那两项共用 parse_market_cap:100 / 1K / 1,000 都认,写坏了启动即报错。
    # 实测(2026-09-11 生产库只读,近 7 天已推送买入 ≈1796 条/天,缺金额 0 条):≥$100 → ≈1453 条/天。
    fomo_buy_push_min_usd: float | None = Field(
        None, description="买入推送的单笔金额下限(美元,含边界),不设=不限。例 100"
    )

    # ---------- 网络 ----------
    fomo_proxy: str | None = Field(None, description="代理 URL,例 http://127.0.0.1:7897")

    # ---------- 日志 ----------
    log_level: str = Field("INFO")

    @field_validator("fomo_client_impl")
    @classmethod
    def _check_impl(cls, v: str) -> str:
        """客户端实现只能是这两个之一,拼错了要立刻炸,不要跑到一半才发现"""
        v = v.strip().lower()
        if v not in ("http", "playwright"):
            raise ValueError(f"FOMO_CLIENT_IMPL 只能是 http / playwright,当前值: {v}")
        return v

    @field_validator("fomo_buy_push_min_market_cap", "fomo_buy_push_max_market_cap", "fomo_buy_push_min_usd",
                     mode="before")
    @classmethod
    def _parse_mcap(cls, v):
        """500K / 1.5M 这种写法在类型校验之前解析掉;空串 = 不设(.env 里写了键没写值)"""
        return parse_market_cap(v)

    @field_validator("fomo_buy_push_max_market_cap")
    @classmethod
    def _check_mcap_range(cls, hi, info):
        """
        下限 > 上限 = 空区间 = 所有买入静默筛没。启动就炸,不要跑起来才发现收不到推送。

        ⚠️⚠️ 刻意写成**字段**校验器而不是 model_validator:后者报错时 pydantic 会把
           **整份输入**(含 fomo_telegram_bot_token)的 repr 印进 ValidationError,
           启动失败的那条栈就成了凭据泄漏(实测第一版就印出了 `{'fomo_telegram_bot_token…`)。
           字段校验器的 input_value 只有上限这一个数。
        ⚠️ 依赖字段声明顺序:min 声明在 max 之前,info.data 里才有它;
           min 自己没通过校验时不在 info.data 里,这里跳过(那边已经报错了)。
        """
        lo = info.data.get("fomo_buy_push_min_market_cap")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(
                f"FOMO_BUY_PUSH_MIN_MARKET_CAP({lo:,.2f}) 大于 "
                f"FOMO_BUY_PUSH_MAX_MARKET_CAP({hi:,.2f}) —— 这个区间里一个币都没有"
            )
        return hi

    # ---------- 派生属性 ----------
    @property
    def buy_push_mcap(self) -> MarketCapRange:
        """买入推送的市值区间。FOMO(poller)与 pump.fun(pumpfun)共用这一个对象的判据"""
        return MarketCapRange(
            min_usd=self.fomo_buy_push_min_market_cap,
            max_usd=self.fomo_buy_push_max_market_cap,
            push_unknown=self.fomo_buy_push_unknown_market_cap,
        )

    @property
    def tg_token(self) -> str | None:
        return (
            self.fomo_telegram_bot_token.get_secret_value()
            if self.fomo_telegram_bot_token
            else None
        )

    @property
    def tg_enabled(self) -> bool:
        return bool(self.tg_token and self.fomo_telegram_chat_id)

    @property
    def admin_chat_id(self) -> str | None:
        """命令白名单 chat_id,未单独配置时等于推送目标"""
        return self.fomo_telegram_admin_chat_id or self.fomo_telegram_chat_id

    @property
    def alpha_sectors(self) -> list[tuple[int, int, str]]:
        """
        `FOMO_ALPHA_SECTORS` → [(rankType, tabId, 显示名), …]。

        ⚠️ 写坏的条目**跳过、不抛** —— 板块标注是锦上添花,一个手滑的配置
           绝不该让整个进程起不来(更不该让主干的上新推送跟着死)。
           跳过的条目在启动日志里看不见,但它本来也只是少一行标注。
        ⚠️ 显示名允许带冒号(只切前两个),但不能带逗号 —— 逗号是条目分隔符。
        """
        out: list[tuple[int, int, str]] = []
        for chunk in (self.fomo_alpha_sectors or "").split(","):
            parts = chunk.strip().split(":", 2)
            if len(parts) != 3:
                continue
            try:
                rank_type, tab_id = int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                continue
            label = parts[2].strip()
            if label:
                out.append((rank_type, tab_id, label))
        return out

    @property
    def proxies(self) -> dict | None:
        """httpx / curl_cffi 通用的代理字典;未配置返回 None"""
        if not self.fomo_proxy:
            return None
        return {"http": self.fomo_proxy, "https": self.fomo_proxy}


@lru_cache(maxsize=1)
def get_settings() -> FomoSettings:
    """单例配置,多次调用返回同一实例"""
    return FomoSettings()  # type: ignore[call-arg]


def mask(secret: str | None, keep: int = 8) -> str:
    """
    凭据脱敏,用于日志。沿用 claudeTrade lessons.md L1 的规矩:
    任何 token / key 打进日志前必须先过这个函数。
    """
    if not secret:
        return "<empty>"
    return f"{secret[:keep]}***" if len(secret) > keep else "***"
