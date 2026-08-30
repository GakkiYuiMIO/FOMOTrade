"""
配置加载模块 —— 从 .env 读取所有配置,pydantic 校验类型

⚠️ 整个项目只在本文件调用 os.getenv / load_dotenv,其他模块一律从 settings 取值。
⚠️ 本类必须独立继承 BaseSettings,绝不复用 claudeTrade 的 Settings ——
   那个类对 binance_api_key 有"必填 + 长度 + 占位符"三重校验,
   本项目根本不碰币安,继承过来会导致没配币安 Key 的机器直接启动失败。
"""
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

    # ---------- 派生属性 ----------
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
