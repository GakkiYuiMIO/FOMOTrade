# FOMOTrade

监控 [fomo.family](https://fomo.family) 上你关注的交易者，他们一有买卖、转入、发观点，就实时推送到你的 Telegram。

- 🌱 区分**首次建仓**和**加仓**
- 👥 显示你的监控名单里**有几个人买过、几个人还拿着**
- 📥 名单里多人**同时收到同一个币**时预警（常见于项目方分发筹码）
- 🎯 同时支持监控 **pump.fun** 上的用户买卖与观点
- 🆕 **币安 Alpha** 新币上架提醒
- 💬 在 Telegram 里发命令管理名单、查合约、看筹码分布

> ⚠️ 加密货币风险极高，本项目仅供学习研究，不构成任何投资建议。
> 监控与推送功能只读取数据。可选的跟单功能**默认关闭**，详见 [跟单（可选，默认关闭）](#跟单可选默认关闭)。

---

## 目录

- [功能介绍](#功能介绍)
- [搭建步骤（Windows）](#搭建步骤windows)
- [Mac / Linux](#mac--linux)
- [常用配置](#常用配置)
- [常见问题](#常见问题)
- [安全说明](#安全说明)

---

## 功能介绍

### 交易推送

名单里的人每买入或卖出一笔，你会收到这样一条消息：

```
🌱 alice · 首次建仓 · $CUM · 「Cummingtonite」
📝 「Cummingtonite」 = 「镁铁闪石」
💰 买入 $2,089.50
📦 持仓 $2,601.97
📊 均价 $0.00006718
📈 未实现盈亏 +$515.43 (+24.70%)
💎 市值 $83.77K
🚀 发射台 · LONG
🧑‍🤝‍🧑 持有人 1,194
🌊 底池 · USAR · 「USA Rare Earth, Inc.」
🏢 USAR = 「美国稀土公司」 · 纳斯达克(NasdaqGM)上市
🏅 盈利榜持有人 ≥2 人
   #18 「trader_a」 · 5,030,790 枚 · 粉丝 11,642 · 全平台24h +$642.57K
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Robinhood Chain
🔗 网站 · Twitter
🔗 FOMO
0x7a6a3b93cb3ffead8b180b5f537e0ce7832d1e18
```

每一行的含义：

| 行 | 说明 |
|---|---|
| 🌱 / 🟢 / 🔴 | 首次建仓 / 加仓 / 卖出 |
| 📝 | 币的英文全名与中文名（自动翻译） |
| 💰 📦 📊 📈 | 本笔金额、当前持仓、均价、盈亏 |
| 💎 | 当前市值 |
| 🚀 | 发射平台（Pump.fun、LONG、Pons 等） |
| 🧑‍🤝‍🧑 | 持有人数 |
| 🌊 🏢 | 底池对手是股票代币时，显示是哪只股票、哪家公司 |
| 🏅 | 持有这个币的人里，有谁在 FOMO 24 小时盈利榜上 |
| 👥 | 你的名单里有几人买过、几人还拿着 |
| 🔗 | 项目官网、Twitter、Telegram 等链接 |
| 最后一行 | 合约地址，**手机上点一下就能复制** |

拿不到的信息那一行会直接不显示，不会出现空白或 N/A。

### 筹码分发预警

名单里有 **3 个以上**（可配置）的人在一段时间内**收到**同一个币（从外部钱包转入，不是自己买），会推送一条预警。这通常意味着项目方在分发筹码，和买入是相反的信号。

### 指定用户的转入推送

有些人用 GMGN、DEBOT 等外部工具交易，在 FOMO 上显示的是「转入」而不是「买入」。用 `/tin` 点名这些人，他们每收到一笔转入就单独推送，可以给每个人单独设金额门槛。

### pump.fun 监控（默认关闭）

监控 pump.fun 上指定用户的**每一笔成交**和**发的观点（callout）**。需要在 `.env` 里打开，再用 `/pump add <用户名>` 添加。

### 币安 Alpha 上新

币安 Alpha 有新代币上架时推送提醒（默认开启）。

### 市值区间过滤

只想看小盘币？在 `.env` 里设 `FOMO_BUY_PUSH_MAX_MARKET_CAP=500K`，就只推市值 50 万美元以下的买入。卖出照常推送。

### Telegram 命令

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令 |
| `/top` | 查看 FOMO 盈利榜（可加 `24h` / `7d` / `30d`） |
| `/add <handle>` | 把某人加入监控名单 |
| `/del <handle>` | 移出监控名单 |
| `/following <handle>` | 批量导入某人关注的所有人 |
| `/list` | 查看监控名单 |
| `/star <handle>` | 特别关注，推送时加 ⭐ 标识；`/unstar` 取消 |
| `/hot` | 名单成员最近都在买什么（可加 `今日` / `3日` / `7日`） |
| `/who <合约地址>` | 名单里谁买过这个币 |
| `/ca <合约地址>` | 查这个币：FOMO 用户的观点、持仓与盈亏 |
| `/chips <合约地址>` | 筹码分布：FOMO 平台与 pump.fun 上各持有多少，你的名单里有谁 |
| `/tin <handle> <金额>` | 开启某人的转入推送，并设他的金额门槛（如 `30000`、`3w`） |
| `/pump add <用户名>` | 添加 pump.fun 监控对象；`/pump` 查看名单 |
| `/status` | 运行状态 |
| `/copy` · `/paper` | 跟单设置与台账（见下文） |

### 网页看板

```powershell
.\bot.ps1 --web
```

浏览器打开 <http://127.0.0.1:8420>，可以查看历史数据。只在本机可访问。

### 跟单（可选，默认关闭）

当名单里多人买入同一个币时，可以跟着买。

- **默认完全关闭**，需要 `/copy on` 手动打开
- 打开后**默认只记纸面台账**（不花钱），用 `/paper` 查看假如跟了现在赚亏多少
- 要真实下单，必须再依次用 `/copy real` 和 `/copy live` 显式开启
- 默认每一单都要在 Telegram 里**点确认**才会执行

> ⚠️ 真实下单会通过浏览器在 FOMO 上点买入按钮，**点下去就成交，无法撤回**。开启前请先用纸面模式充分观察。

---

## 搭建步骤（Windows）

整个过程大约 15 分钟。

### 第 0 步：准备

你需要：

| 需要 | 说明 |
|---|---|
| Windows 10 / 11 | |
| [Python 3.11 或更高](https://www.python.org/downloads/) | 安装时**务必勾选 `Add python.exe to PATH`** |
| [Google Chrome](https://www.google.com/chrome/) | 登录 FOMO 时用 |
| [Git](https://git-scm.com/download/win) | 可选，不装的话用下载 ZIP 的方式 |
| 一个 Telegram 账号 | |
| 一个 FOMO 账号 | |

装完 Python 后，打开 **PowerShell**（开始菜单搜 `PowerShell`），输入下面的命令确认装好了：

```powershell
python --version
```

显示 `Python 3.11.x` 或更高就可以。

### 第 1 步：下载项目

**方式一：用 Git**

```powershell
git clone https://github.com/GakkiYuiMIO/FOMOTrade.git
cd FOMOTrade
```

**方式二：下载 ZIP**

在 GitHub 页面点绿色的 `Code` 按钮 → `Download ZIP`，解压后在 PowerShell 里进入解压的文件夹：

```powershell
cd C:\你解压的路径\FOMOTrade-main
```

> 之后所有命令都在这个文件夹里执行。

### 第 2 步：安装依赖

依次执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

> 国内网络下载慢的话，第二条命令换成：
>
> ```powershell
> .\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

如果执行 `.ps1` 脚本时提示「禁止运行脚本」，先执行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 第 3 步：创建 Telegram Bot

**3.1 获取 Bot Token**

1. 在 Telegram 里搜索 **@BotFather**，打开对话
2. 发送 `/newbot`
3. 按提示给 Bot 起个名字，再起一个以 `bot` 结尾的用户名（如 `my_fomo_monitor_bot`）
4. BotFather 会回复一串 Token，形如 `1234567890:AAH...`，**复制保存好**

**3.2 获取你的 chat_id**

1. 在 Telegram 里搜索你刚创建的 Bot，点 **Start**，随便发一条消息（这一步必须做，否则 Bot 无法给你发消息）
2. 在浏览器打开下面这个地址（把 `<TOKEN>` 换成上一步的 Token）：

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

3. 在返回的内容里找 `"chat":{"id":123456789`，这串数字就是你的 chat_id

> 看不到内容的话，回 Telegram 再给 Bot 发一条消息，然后刷新网页。

### 第 4 步：填写配置

复制配置模板并打开：

```powershell
Copy-Item .env.example .env
notepad .env
```

找到这两行，填入第 3 步拿到的值，保存：

```ini
FOMO_TELEGRAM_BOT_TOKEN=1234567890:AAH...
FOMO_TELEGRAM_CHAT_ID=123456789
```

其他配置先保持默认，以后按需调整（见 [常用配置](#常用配置)）。

### 第 5 步：登录 FOMO

```powershell
.\bot.ps1 --login
```

会弹出一个浏览器窗口，**在里面正常登录你的 FOMO 账号**。登录成功后程序会自动保存登录状态，窗口可以关闭。

> 程序不会让你在命令行输入密码，登录全程在浏览器里由你自己完成。登录状态会自动续期，一般不需要重复登录。

### 第 6 步：检查配置

```powershell
.\bot.ps1 --check
```

它会检查配置、登录状态、Telegram 和数据库是否都正常。有问题会直接告诉你哪一项没通过。

### 第 7 步：启动

```powershell
.\bot.ps1 --run
```

看到 `FOMO 监控启动` 就说明跑起来了。**这个窗口不要关**，关掉监控就停了。要停止时按 `Ctrl + C`。

### 第 8 步：开始使用

回到 Telegram，给你的 Bot 发：

1. `/top` —— 看看 FOMO 盈利榜，挑几个你想跟的人
2. `/add 他的handle` —— 把他加入监控
3. 之后他一有买卖，你就会收到推送

> 监控名单是空的时候不会有任何推送，**先 `/add` 几个人**。

---

## Mac / Linux

步骤和 Windows 一样，只是命令写法不同：

```bash
git clone https://github.com/GakkiYuiMIO/FOMOTrade.git
cd FOMOTrade
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env        # 然后用编辑器填写 Token 和 chat_id

.venv/bin/python -X utf8 -m src.cli --login
.venv/bin/python -X utf8 -m src.cli --check
.venv/bin/python -X utf8 -m src.cli --run
```

> 登录这一步需要弹出浏览器，请在带桌面的电脑上操作。

---

## 常用配置

所有配置都在 `.env` 里，改完**重启程序**生效。完整说明见 `.env.example` 里的注释。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `FOMO_TELEGRAM_BOT_TOKEN` | — | **必填**，Bot Token |
| `FOMO_TELEGRAM_CHAT_ID` | — | **必填**，接收推送的 chat_id |
| `FOMO_POLL_INTERVAL_SEC` | `15` | 多少秒检查一次，名单人多时可调大 |
| `FOMO_PROXY` | 空 | 代理地址，如 `http://127.0.0.1:7890` |
| `FOMO_BUY_PUSH_MAX_MARKET_CAP` | 空 | 只推市值低于此值的买入，如 `500K`、`1.5M` |
| `FOMO_BUY_PUSH_MIN_MARKET_CAP` | 空 | 只推市值高于此值的买入，如 `50K` |
| `FOMO_BUY_PUSH_UNKNOWN_MARKET_CAP` | `true` | 设了上面两项时，拿不到市值的买入是否照推 |
| `FOMO_TRANSFER_ALERT_RECEIVERS` | `3` | 筹码分发预警：几个人收到同一个币才预警 |
| `FOMO_TRANSFER_ALERT_MIN_USD` | `500` | 筹码分发预警：单人到账金额门槛（美元） |
| `FOMO_ALPHA_ENABLED` | `true` | 币安 Alpha 上新提醒 |
| `FOMO_PUMP_ENABLED` | `false` | pump.fun 买卖监控 |
| `FOMO_PUMP_MIN_USD` | `50` | pump.fun 单笔成交低于此金额不推 |
| `FOMO_PUMP_CALLOUT_ENABLED` | `false` | pump.fun 观点推送 |

---

## 常见问题

**登录时 Google 提示「This browser or app may not be secure」**

在登录窗口里改用**邮箱验证码**登录，不走 Google 就没有这个问题。

**日志里出现「登录态失效，停止轮询」**

重新登录一次，然后再启动：

```powershell
.\bot.ps1 --login
.\bot.ps1 --run
```

**关机或断网一段时间后再启动，会不会刷屏？**

不会。程序检测到中断较久时，会把这段时间的记录保存下来但不逐条推送，避免一次性推出几百条旧消息。

**推送太多了**

- 用 `FOMO_BUY_PUSH_MAX_MARKET_CAP` 只看小盘
- 用 `/del` 移除不想看的人
- 用 `/star` 给重点关注的人加标识，方便区分

**一直没有推送**

1. 先发 `/list` 确认监控名单里有人
2. 发 `/status` 看运行状态
3. 看运行窗口里有没有报错

**请求一直失败 / 被拦截**

先运行诊断：

```powershell
.\bot.ps1 --probe
```

如果提示被 Cloudflare 拦截，在 `.env` 里改成 `FOMO_CLIENT_IMPL=playwright` 再试。需要代理的话填写 `FOMO_PROXY`。

**怎么更新到最新版本？**

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

---

## 安全说明

- `data/fomo_session.json` 保存的是你的 **FOMO 登录状态**，拿到它就等于能登录你的账号。**不要发给任何人**
- `.env` 里有你的 **Bot Token**，同样不要泄露
- 这两个文件已在 `.gitignore` 中，不会被 Git 提交
- Bot 只响应你在 `.env` 里填写的 chat_id 发来的命令，别人加了你的 Bot 也无法操作

---

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # 运行测试
.\.venv\Scripts\python.exe -m ruff check .     # 代码检查
```

## 许可证

[MIT](LICENSE)
