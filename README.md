# FOMOTrade

监控 [fomo.family](https://fomo.family) 上指定用户的**买入 / 卖出 / 观点 / 转入 / 转出**，实时推送到 Telegram Bot。

在同类监控的基础上多做两件事：

- 🌱 **首次建仓识别** —— 这个人是第一次碰这个币，还是在加仓
- 👥 **共识计数** —— 你的监控名单里有多少人买过这个币、还有多少人仍持有

> ⚠️ 本项目**只读** FOMO 接口，不做任何交易操作，不触碰资产。
> ⚠️ 仅供个人研究使用。加密货币交易风险极高，本项目不构成任何投资建议。

---

## 推送长什么样

```
🌱 cryptodude · 首次建仓 · $CATE
💰 买入 $8,200.00
📦 持仓 $8,190.44
💎 市值 $4.02M
👥 名单内 3/12 人买过 · 2 人仍持有
🧬 Base
0x8f2a4c19e7b3d5a0f16c88be2d7419aa3c05e6f1
```

「3 人买过 · 2 人仍持有」的差值本身就是信号——有人已经退出了。

CA 独占最后一行且是纯 `<code>`：在手机上**点一下就复制**，不依赖网络。
（Telegram 内置浏览器不走 MTProto 代理，国内点 GMGN 链接大概率白屏，所以链接不能是主路径。）

---

## 快速开始

### 1. 安装依赖

```powershell
poetry install
```

> **已知问题**：如果 poetry 报 `All attempts to connect to pypi.tuna.tsinghua.edu.cn failed`
> 但 pip 访问同一镜像正常，那是 poetry 自身 HTTP 栈的问题（在代理环境下已复现）。
> 用 pip 装即可，效果等价：
>
> ```powershell
> python -m venv .venv
> .\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

装浏览器（登录取 token 用，只需一次）：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 2. 配置

```powershell
Copy-Item .env.example .env
notepad .env
```

至少要填 `FOMO_TELEGRAM_BOT_TOKEN` 和 `FOMO_TELEGRAM_CHAT_ID`。

### 3. 登录 FOMO

```powershell
.\bot.ps1 --login
```

会弹出一个浏览器窗口，**你自己登录**（程序不经手你的密码）。登录态存在
`data/fomo_session.json`，之后自动续期，不用反复登录。

### 4. 探测 API（重要，第一次必须跑）

```powershell
.\bot.ps1 --probe
```

本项目的 API 字段是从 fomo.family 前端逆向出来的，**需要用真实响应校准一次**。
这条命令会把各端点的原始 JSON dump 到 `data/fomo_probe/`，并打印一张字段核对表。
详见 [tasks/design.md](tasks/design.md) 第十一节。

### 5. 自检 & 试跑

```powershell
.\bot.ps1 --check      # 配置 / 登录态 / TG / DB 连通性自检
.\bot.ps1 --dry-run    # 跑一轮，只打印不推送
```

### 6. 正式运行

```powershell
.\bot.ps1 --run
```

---

## Telegram 命令

在 Bot 里直接发（只有 `.env` 里配的 admin chat 能用）：

| 命令 | 说明 |
|---|---|
| `/add <handle>` | 加入监控。历史基线会在后台建立，期间照常推送、只是不打徽章 |
| `/del <handle>` | 移除监控（软删除，历史数据保留） |
| `/list` | 查看名单和各人的基线状态 |
| `/status` | 运行状态：名单人数、今日事件数、最近一次轮询时间 |
| `/who <CA>` | 名单里谁买过这个币 |
| `/help` | 命令说明 |

> 新 `/add` 一个人**不会推送他的历史交易**——游标在加入的瞬间就设成了当前时刻。

---

## 设计文档

完整设计见 [tasks/design.md](tasks/design.md)，包含：

- 逆向得到的 FOMO API 端点与鉴权方式
- 「首次买入」和「共识计数」的精确语义与取舍理由
- 完整 SQLite DDL 与判定逻辑
- 边界情况处置表（拆单、多链同名币、冷启动、动态增删名单…）
- 消息模板与排版铁律
- 17 项待验证的 API 字段清单

实现进度见 [tasks/todo.md](tasks/todo.md)。

---

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q      # 跑测试
.\.venv\Scripts\python.exe -m ruff check .   # 静态检查
```

测试不碰网络、不碰真实数据库，全部用内存 SQLite。

---

## 安全

- 登录环节由你自己在浏览器里完成，**程序不经手密码**
- `data/fomo_session.json` 含 Privy refresh token，已在 `.gitignore` 里；**泄漏等于账号被接管**
- 日志里的 token 一律脱敏
- 本项目不发起任何交易、转账、授权操作
