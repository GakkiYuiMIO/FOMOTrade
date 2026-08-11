# bot.ps1 —— FOMO 监控快捷启动脚本(绕过 poetry,直接用 .venv 里的 python)
#
# ⚠️ -X utf8 必须带:Windows 控制台默认 GBK,中文日志和 emoji 会直接抛 UnicodeEncodeError,
#    而且是在跑到一半的时候炸,排查起来毫无线索。
#
# 用法:
#   .\bot.ps1 --login                 首次登录(有头浏览器,程序不经手你的密码)
#   .\bot.ps1 --probe                 打全部端点 + dump 原始 JSON + 字段核对表(Phase 0 必跑)
#   .\bot.ps1 --probe --handle xxx    指定样本用户(缺省时从排行榜自动挑)
#   .\bot.ps1 --check                 连通性自检(配置 / 登录态 / Telegram / 数据库)
#   .\bot.ps1 --init-db               只建表
#   .\bot.ps1 --dry-run               跑一次 tick,只打印不推送
#   .\bot.ps1 --run                   正式运行(轮询 + Telegram 命令层)
#
# 退出码:0 成功 / 1 失败 / 2 未登录(--probe)

& "$PSScriptRoot\.venv\Scripts\python.exe" -X utf8 -m src.cli @args
