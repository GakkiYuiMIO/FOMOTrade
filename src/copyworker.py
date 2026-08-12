"""
跟单买入队列 —— 无人值守自动成交的唯一执行入口。

============ 为什么必须是一条线程 ============
executor 每次买入都要 launch_persistent_context(PROFILE_DIR)。
同一个 profile 目录**同一时刻只能被一个浏览器进程占用**,第二个会直接起不来。
所以单线程不是"保守起见",是 profile 锁的物理约束 ——
并发提交只会换来一堆"浏览器起不来",不会换来更快。

============ 为什么必须离开 tick 线程 ============
一笔买入 = 冷启一整套有头 Chromium + 8s 渲染 + ≤20s 等报价 + ≤45s 等成交回读。
轮询间隔 15s、max_instances=1 —— 就地执行会让主推送整体延后一分多钟,
而跟单要抢的恰恰是这一分钟。

============ 积压时宁可丢弃 ============
排在后面的单子等到能执行时,价格早就不是判定那一刻看到的了。
**"晚三分钟买进去"不是"打了折扣的成功",它是另一笔交易。**
所以队列有上限、出队还要再查一次时效,超时就不买 —— 并且记 failed、发 TG,
而不是安静地排着或安静地丢掉。

============ 关不掉的那一单 ============
Ctrl+C 时**不等**在途的买入(那可能要一分钟),线程是 daemon,进程直接走。
代价是那一单的状态停在 auto_executing —— 这正是启动对账(B5)要认领的东西。
⚠️ 绝不能在这里"顺手"把在途单标成 failed:点击可能已经发生,钱可能已经出去。
"""
from __future__ import annotations

import html
import queue
import threading
import time
from dataclasses import dataclass

from loguru import logger

from src import store
from src.config import PROBE_DIR

# 队列深度。⚠️ 不是内存考虑 —— 见模块头「积压时宁可丢弃」。
# 3 的依据:一笔约 30~75s,排到第 4 位时最早那笔已经等了两三分钟,早就没有意义了。
MAX_QUEUE = 3
# 从入队到真正开始执行的时效上限。超过就不买。
MAX_STALE_SEC = 120
# 队列空转时的醒来间隔 —— 只影响 close() 的响应速度
_POLL_SEC = 0.5

# 状态流转:auto_queued →(抢占)auto_executing →(终态)filled / failed
ST_QUEUED = "auto_queued"
ST_EXECUTING = "auto_executing"


@dataclass(frozen=True)
class BuyJob:
    network_id: str
    token_address: str
    symbol: str
    amount_usd: float
    dry_run: bool
    queued_at: float          # time.monotonic()

    @property
    def key(self) -> tuple[str, str]:
        return (self.network_id, self.token_address)


def _default_execute(job: BuyJob, should_stop=None):
    from src.executor import buy as execute_buy

    return execute_buy(job.network_id, job.token_address, job.symbol, job.amount_usd,
                       dry_run=job.dry_run, screenshot_dir=str(PROBE_DIR),
                       should_stop=should_stop)


class CopyWorker:
    """
    单线程买入队列。线程懒建(第一次 submit 时才起),close() 之后不再接单。

    execute 可注入 —— 单测绝不能真开浏览器。
    注入的可调用对象签名固定为 `(job, should_stop) -> BuyResult`。
    """

    def __init__(self, notifier, execute=None):
        self._notifier = notifier
        self._execute = execute or _default_execute
        self._q: queue.Queue[BuyJob] = queue.Queue(maxsize=MAX_QUEUE)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------------- 提交 ----------------
    def submit(self, job: BuyJob) -> bool:
        """
        入队。返回 False 表示**没有接下这一单**(队列满 / 已关闭),调用方要记 failed。

        ⚠️ 绝不阻塞:这个方法跑在轮询 tick 里,阻塞它等于把监控停下来等下单。
        """
        if self._stop.is_set():
            return False
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="fomo-copybuy", daemon=True)
                self._thread.start()
        try:
            self._q.put_nowait(job)
            return True
        except queue.Full:
            # ⚠️ 满了说明前面那几单还在跑。这时候排队等 = 等到价格全变了才买
            logger.warning("买入队列已满({} 单在排),丢弃 | {} {}",
                           MAX_QUEUE, job.symbol, job.token_address[:10])
            return False

    def close(self) -> None:
        """停止接单并让线程退出。⚠️ **不等**在途的那一单 —— 见模块头"""
        self._stop.set()

    @property
    def pending(self) -> int:
        return self._q.qsize()

    # ---------------- 执行 ----------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=_POLL_SEC)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except Exception as e:  # noqa: BLE001
                # ⚠️ 这条线程死了就等于跟单静默停摆,任何异常都不能让它退出
                logger.exception("买入队列处理异常(线程继续) | {} | {}", job.symbol, e)

    def _process(self, job: BuyJob) -> None:
        net, ca = job.key
        waited = time.monotonic() - job.queued_at

        # ---- 时效:排太久就不买了 ----
        # ⚠️ 这不是"顺手加的保护"。跟单的全部价值在于早 ——
        #    $Plumber 第 2 个人进是 31.62x,第 5 个人进是 0.74x。
        if waited > MAX_STALE_SEC:
            self._finish(job, "failed",
                         f"排队 {waited:.0f}s 超时未执行,已放弃(价格早已不是判定时那个)",
                         emoji="⌛", title="超时未执行")
            return

        # ---- 急停:每一单执行前都重查一次配置 ----
        # ⚠️ 配置是 tick 里读的快照,而这一单可能是一分钟前入的队。
        #    /copy off 要能拦住**还没开始跑**的单,否则"停手"只对未来生效。
        with store.get_conn() as conn:
            cfg = store.load_copy_config(conn)
            if not (cfg.enabled and cfg.auto_execute and not cfg.paper_only):
                self._finish(job, "failed", "执行前发现跟单已被关闭,未下单",
                             emoji="🛑", title="已取消")
                return
            # ---- 抢占:只有从 auto_queued 抢到的才有权执行 ----
            if not store.set_copy_status(conn, net, ca, ST_EXECUTING, expect=ST_QUEUED):
                logger.info("这一单已经不是待执行状态,跳过 | {} {}", job.symbol, ca[:10])
                return

        logger.warning("开始自动买入 | {} ${:.2f} · 排队 {:.0f}s", job.symbol, job.amount_usd, waited)
        try:
            # ⚠️ 急停传进执行器,让它在**点成交之前**的几个节点上还能被拦下。
            #    过了那一下就拦不住了,也不该拦 —— 钱已经出去,这时候"停"
            #    只会让程序不去读回执:钱花了、状态没写、你还以为停住了。
            res = self._call_execute(job)
        except Exception as e:  # noqa: BLE001
            logger.exception("自动买入失败 | {} {}", job.symbol, ca[:10])
            self._finish(job, "failed", str(e)[:300], emoji="❌", title="未成交")
            return

        # ⚠️ 演练模式绝不能记 filled:那会让 /paper 给一个并不存在的仓位算盈亏
        if job.dry_run:
            self._finish(job, "rejected", res.message, emoji="🧪", title="演练通过 · 未成交")
        elif res.confirmed:
            self._finish(job, "filled", res.message, emoji="✅", title="已成交")
        else:
            # ⚠️ 「点了但没读到仓位变化」是独立的一档:报成功会让人以为没事,
            #    报失败又会诱使人手动再买一次。这一档恰恰最需要人去看一眼。
            self._finish(job, "filled", res.message, emoji="⚠️", title="已点击 · 结果待核对")

    def _call_execute(self, job: BuyJob):
        """调执行器,把急停回调传进去。注入的 execute 必须收 (job, should_stop)"""
        return self._execute(job, self._should_stop)

    def _should_stop(self) -> bool:
        """
        执行途中要不要停手。**只在点成交之前被查**(见 executor._abort_if_stopped)。

        ⚠️ 每次都重查一遍配置,不用 tick 里那份快照:「立刻停手」的意思就是
           `/copy off` 敲下去之后**正在跑的这一单**也要停,而不是只对下一单生效。
           代价是几次 runtime_state 单行读,可以忽略。
        ⚠️ 查库出错时返回 False(继续买):停手判断本身挂掉不该变成"永远停手",
           那会让跟单在数据库抖一下之后静默失效。真要停,用 close()。
        """
        if self._stop.is_set():
            return True
        try:
            with store.get_conn() as conn:
                cfg = store.load_copy_config(conn)
            return not (cfg.enabled and cfg.auto_execute and not cfg.paper_only)
        except Exception as e:  # noqa: BLE001
            logger.warning("急停判断查库失败,按继续处理: {}", e)
            return False

    def _finish(self, job: BuyJob, status: str, note: str, *, emoji: str, title: str) -> None:
        """写终态 + 发回执。⚠️ 无人值守下没人点按钮,不发就等于什么都没发生过"""
        net, ca = job.key
        try:
            with store.get_conn() as conn:
                # 只覆盖自己抢到的那个中间态;抢占之前就失败的单还停在 auto_queued
                store.set_copy_status(conn, net, ca, status, note[:200],
                                      expect=(ST_EXECUTING, ST_QUEUED))
        except Exception as e:  # noqa: BLE001
            logger.exception("写跟单终态失败 | {} {} | {}", job.symbol, ca[:10], e)

        # ⚠️ 必须转义:note 里可能带执行器抛出的原始异常文本,一个裸 '<' 就让 TG 400,
        #    而回执发不出去 = 无人值守下这一单等于没发生过
        esc = html.escape
        try:
            self._notifier.send(
                f"{emoji} <b>{esc(title)}</b> · 自动跟单 · ${esc(job.symbol)}\n"
                f"{esc(note[:300])}\n<code>{esc(ca)}</code>"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("跟单回执发送失败(状态已记录): {}", e)
