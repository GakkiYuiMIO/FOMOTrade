# 无人值守自动跟单 —— 改造清单

来源:2026-08-12 对抗性审计(30 条发现 → 反驳 20 条 → 存活 10 条),
加我自己复核的 4 条事实。结论:**executor.py 本身几乎不用动**,
要补的全是它周围那圈原本由「人的手指」承担的护栏。

⚠️ **A 档 7 项全部做完之前,不要打开自动执行。**
⚠️ 做完之后开关仍然**默认关闭** —— 什么时候开是用户的决定,不是我的。

---

## A 档 · 不做就会花错钱(硬门槛)

- [x] **A1. 独立开关 `auto_execute`,不复用 `paper_only` / `dry_run_execute`** ✅ 2026-08-12
      `CopyConfig.auto_execute=False` + `auto_blockers()` + `/copy auto|manual`。
      开自动时当场列出还差什么,**被拒绝时不落库**。
      改 `copytrade.py` CopyConfig / `bot.py` _COPY_FIELDS / `/copy` 面板文案。
      判定必须是 `enabled and not paper_only and auto_execute` 三者同真。
      **为什么**:我已经让用户之后要跑 `/copy real` + `/copy live`(验证顺序)。
      如果自动挂在这两个标志上,他照我说的做的那一刻就变成无人值守了。
      (审计说「库里很可能已经是 False」——**实际不是**,现在还是 paper_only=true。
       但结论不变,理由换成上面这条。)

- [x] **A2. `_check_copytrade` 加 catchup 守卫** ✅ 2026-08-12(带对照组测试)
      `poller.py:1520` 开头 `if self._catchup_since: return`。
      **为什么**:自己 grep 验证过,`_catchup_since` 只在 543/586/1343/1447 出现,
      **全在推送侧**。停机 ≥45min 重启后,推送降级成汇总,跟单却拿到整夜积压,
      而 `entry_mcap` 取的是**现在**的市值 → 按今天的价买昨晚的信号。
      ⚠️ 现在有一层**意外**阻尼(冷启动轮 balances 只拉头 10 人 → 多数币
      `created_at=None` → SKIP_NO_AGE)。那是巧合不是防线:`max_age_hours`
      一设成「不限」就没了。

- [ ] **A3. 真实成交路径的 `entry_mcap` 不能用 `token_snapshot` 旧值**
      `poller.py:1549` 现在是 snapshot 优先;snapshot 对已清仓的币不再更新。
      改成本轮 `_token_meta` 优先,拿不到就跳过(不回落)。
      **为什么**:自动模式下它同时是**筛选闸门**和**台账成本**,不再只是卡片上一个数字。

- [ ] **A4. `set_copy_status` 改真正的 CAS;自动路径不用 `pending`**
      `store.py` UPDATE 加 `AND status = ?` + 用 rowcount 判抢占;
      自动路径落 `auto_executing`,且不挂 TG 按钮(避免人机双入口)。
      **为什么**:改造后 poller 主线程与 bot 线程同时写这张表,而 `get_conn()`
      每次开独立连接,只有 WAL,没有跨线程互斥。真实后果不是买两次
      (profile 锁天然互斥),**是状态互相覆盖**:钱花了、台账写「未成交」、
      TG 弹 ❌ 反过来诱导手动重试。

- [~] **A5. per-token try;失败必须写 `failed` + 发 TG**
      ✅ per-token try 已做(抽出 `_copy_one`,单币异常不影响本轮其余币)。
      ⬜ 「写 failed + 发 TG」等 B1 接上真实执行后一起做 —— 现在还没有会花钱的失败。
      `poller.py:1538` 循环体内每币独立 try;`poller.py:632` 换 `logger.exception` 带 symbol/CA。
      **为什么**:三重叠加 ——
      ① 循环体无内层 try,executor 有 10 处 raise,一次异常掀掉整轮剩余的币;
      ② `record_copy_signal` 先 commit 再买入 → 异常后该行以 pending 永久留库,
         下轮主键冲突 continue,**这个币再也不会被跟**;
      ③ 全项目唯一写 `failed` 的地方在即将删掉的回调里,日志无 TG sink →
         「一次真实花钱失败」被伪装成「跟单信号判定失败(不影响推送)」。

- [x] **A6. 按天的**金额**闸门 + 修 `daily_max=0` + 配置校验** ✅ 2026-08-12
      `daily_spend_usd` + `/copy spend` + `copy_spent_today`(只数会出账的状态)
      + 逐字段类型收敛 + 面板不再把「不限」渲染成「3/0 单」。
      判定是「这一单下完会不会超」,不是「现在超没超」。
      ⚠️ 顺手修了一个原本就存在的洞:两个上限的分子是循环**外**查的,
      一轮命中多个币时不自增 = 一个 tick 就能捅穿当日上限。
      加 `daily_spend_usd`;`copy_taken_today` 统一口径(现在 failed/演练/rejected 也吃额度);
      `load_copy_config` 加类型收敛(JSON 里 null 会让 `None > 0` 抛 TypeError → 静默停摆)。
      **为什么**:`copytrade.py:134` 是 `if cfg.daily_max > 0 and ...`,
      `/copy daily 0` 是「不限」不是「停」,而面板显示「今日 3/0 单」读起来像已限住。

- [ ] **A7. 下单前自检浏览器凭据 + 失效告警**
      抽 `check_login()`;纳入 `--check`;启动时 + 切 auto 时各跑一次。
      **为什么**:PROFILE_DIR 是**第二套凭据**,全项目没有任何代码刷新它。
      ⚠️ 另查到:`--login --cdp` 分支在 `auth.py:444` 就 return,而
      `PROFILE_DIR.mkdir` 在 `:452` —— **走 cdp 这个目录一次都不会写**,
      而 README 恰恰把 `--cdp` 推荐成「最可靠」。
      (现状:profile 124MB、cookie 今天 15:09 写过,是好的。这是**将来**的坑。)

## B 档 · 不做会漏单 / 拖垮监控

- [ ] **B1. 执行搬出 tick 线程 → 单线程队列**
      单笔买入 = 冷启有头 Chromium + 8s 渲染 + ≤20s 报价 + ≤45s 回读。
      15s 间隔 + `max_instances=1` → 连续 4~5 轮被跳过,推送整体延后一分多钟,
      而跟单要抢的恰恰是这一分钟。单线程是必须的:并发开浏览器会撞 profile 锁。
- [ ] **B2. 「立刻停手」开关,循环内每圈复查 cfg;executor 等待点插 stop 检查**
- [ ] **B3. 遍历顺序按信号质量排,不要 `sorted(keys)` 的 CA 字典序**
- [ ] **B4. 自动成交的 TG 回执,尤其 `confirmed=False` 那一档**(最需要人介入却没人看)
- [ ] **B5. 启动对账:陈旧 `executing` / 残留 `pending` 标成 unknown 并推一条**

## C 档 · 锦上添花

- [ ] C1 `/copy` 面板一眼看出「现在是无人值守」
- [ ] C2 补测试:catchup 轮不执行 / CAS 抢占 / 单币异常不影响后续 / 每日金额闸门
- [ ] C3 `/paper` 标注行情新鲜度
- [ ] C4 每日对账推送(几单、多少钱、几单 confirmed=False)

---

## 复盘

(改造完成后填)
