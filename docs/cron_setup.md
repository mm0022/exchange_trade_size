# 每日报告部署（crontab）

`daily_report.py`：每天用最近 3 个已发布 UTC 日的逐笔数据，算 OKX/Bybit/Binance 各序列单笔 notional 的 P50/75/90（3 天中位）+ POV(P75, 5 单/min)，推 Slack 并存档 `data/daily_report.json`。逐笔历史增量缓存在 `data/daily/*.parquet`（与 `daily_stats.py` 共用，已存的天不重复下载）。

## 1. 环境变量（webhook 不进仓库）

把 Slack incoming webhook 放在专用文件，例如 `~/.config/trade_size.env`：

```sh
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/XXX/YYY/ZZZ'
```

## 2. crontab

北京时间 8 点 = UTC 00:00。`crontab -e` 加一行（uv 与脚本均用绝对路径）：

```cron
0 0 * * * cd /Users/mac/dev/trade_size && . ~/.config/trade_size.env && /Users/mac/.local/bin/uv run daily_report.py >> data/daily_report.log 2>&1
```

- uv 路径：本机为 `/Users/mac/.local/bin/uv`（`which uv` 确认）。
- macOS 注意：cron 需在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里授权 `cron`（或 `/usr/sbin/cron`），否则可能无法访问工作目录/网络代理。

## 3. 手动补跑 / 自测

```sh
# 正常跑（推 Slack）
. ~/.config/trade_size.env && /Users/mac/.local/bin/uv run daily_report.py

# 只算+存档，不推 Slack
uv run daily_report.py --no-slack

# 指定存档路径
uv run daily_report.py --archive data/daily_report.json

# 单元测试
uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/ -v
```

## 4. 失败行为

- 单序列下载失败：打印 `✗`、跳过该行，不中断整轮。
- K 线拉取失败：该行 POV = `—`，并打印 stderr 诊断（不静默）。
- 整轮失败（窗口全空/全失败）：推一条 Slack 错误提示并以退出码 1 结束，便于 cron/日志排查。
- 窗口探测凑不齐 3 天：用能凑到的天，Slack 文案标注「⚠️窗口不足3天」。
