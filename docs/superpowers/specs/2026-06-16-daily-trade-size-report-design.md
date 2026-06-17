# 每日单笔报单量报告 · 设计文档

> 日期：2026-06-16
> 状态：已与用户确认设计，待 spec 评审 → 实现计划

## 1. 目标

把当前「一次性、固定日期」的研究脚本，沉淀成一个**每天自动跑**的报告管线：

- 每天用**过去 3 个已发布的完整 UTC 日**的逐笔成交数据；
- 分别算出 **OKX / Bybit**（现货+合约）和 **Binance 永续**每个序列的单笔 notional **P50 / P75 / P90**（每天算分位，再取 3 天中位数）；
- 每行附一列 **POV**：按「每分钟 5 单」估算，单笔下单额占该序列每小时成交量的百分比；
- 每天**北京时间 8 点**把报告推送到 **Slack**；
- 每天的结果**存档到一个 JSON**，便于后续查任意历史日。

## 2. 范围

| 所 | 币种 | 类型 |
|---|---|---|
| OKX | BTC, SUI, AAVE, DOGE, LINK, ARB, PEPE, XRP | 现货 + 合约(swap) |
| Bybit | BTC, SUI, AAVE, DOGE, LINK, ARB, PEPE, XRP | 现货 + 合约(linear) |
| Binance | BTC, ETH, SOL, XRP, LINK, DOGE | 仅永续(linear) |

- 币种/类型清单沿用现有 `daily_stats.py` 的 `COINS` / `TYPES`。
- **口径 = 「一笔吃单规模」（聚合单）**：原始逐笔会把一个 taker 吃单拆成多条 fill，P75 低估真实下单量 2-9×，故改为按「一个逻辑吃单」算分位：
  - **Binance**：直接用官方 `aggTrades` 日归档（一行=一个 taker 吃单聚合），与原始逐笔一样增量下载+缓存（`Binance_agg_{sym}_linear.parquet`，notional=price×quantity）。
  - **OKX/Bybit**：无 aggTrades 归档，对已缓存的原始逐笔做「同秒同价聚合」——同一 UTC 秒、同价的逐笔合并为一单（sum notional）作为近似。注：同秒同价对高频币（BTC/ETH）会把恰好同秒同价的不同单也并到一起，略高估真实 aggTrades 口径。
- 单条 notional 构造：OKX 永续 `size(张)×ctVal×price`、其余 `币量×price`（OKX/Bybit 同秒同价聚合的输入）；Binance aggTrades 用 `price×quantity`（归档无 quote_qty 列）。

## 3. 架构

新建编排脚本 **`daily_report.py`**，复用 `daily_stats.py` 里经验证的下载与 notional 逻辑。旧脚本（`daily_stats.py` 及各 `calc_*`、`fetch_*`）原样保留作历史参考，不改其语义。

```
daily_report.py
├── 窗口探测      resolve_window(exch) -> [d1,d2,d3]   # 最近3个已发布完整UTC日
├── 下载+notional （复用 daily_stats 的 dl_okx/dl_by/dl_bn + notional 构造）
├── 逐序列统计    per_series_stats() -> {P50,P75,P90 (3天中位), 笔数, 成交额}
├── 小时量        hourly_median(exch, coin, type) -> 中位小时 quoteVolume(USDT)
├── POV           pov = 300 * P75 / hourly_median
├── 出表          三张表 (OKX/Bybit/Binance)，等宽 tabulate
├── Slack 推送    post_slack(text)  # webhook from env SLACK_WEBHOOK_URL
└── JSON 存档      append_archive(data/daily_report.json)
```

### 模块职责与接口

- **窗口探测 `resolve_window(exch)`**：从「昨天 UTC」往前逐日探测归档是否存在（HEAD/GET 404 判断），各所**独立**探测（发布节奏不同），凑齐 3 个完整日。返回 3 个 `YYYY-MM-DD`（UTC）。
  - OKX 按 UTC+8 切档，单个 UTC 日需当日+次日两个归档文件齐了才算「完整可用」（沿用 `okx_files_for` 逻辑）。
- **下载 / notional**：复用 `daily_stats.py` 的 `dl_okx` / `dl_by` / `dl_bn` 及 `build` 内的 notional 构造。为复用，将这部分重构为可被 import 的函数（见 §7）。
- **逐序列统计 `per_series_stats(df_by_day)`**：对窗口内**每个 UTC 日**算该序列 notional 的 P50/P75/P90，再对 3 天取**中位数**（`numpy.median`，3 个值取中间那个）。同时给出 3 天合计成交额、合计笔数（旁列）。
- **小时量 `hourly_median(exch, coin, typ, window)`**：取该 instrument 在**与该所解析出的同一 3 个 UTC 日窗口**内的小时线（K 线 1H quoteVolume，USDT），按 window 起止时间过滤，返回约 72 根小时量的**中位**。OKX/Bybit 现货与合约分别取各自 K 线；Binance 取永续 K 线。
  - OKX：`/api/v5/market/candles` instId=现货 `{c}-USDT` / 合约 `{c}-USDT-SWAP`，quoteVolume 取 `volCcyQuote`（index 7）。
  - Bybit：`/v5/market/kline` category=spot/linear，quoteVolume 取 turnover（index 6）。
  - Binance：`fapi/v1/klines` 永续，quoteVolume index 7。
  - 走代理 `127.0.0.1:7890`（与现有脚本一致）。
- **POV**：`pov = 300 * P75 / hourly_median`（300 = 5 单/min × 60）。每行一列，仅基于 P75。`hourly_median` 缺失（拉不到 K 线）则 POV 记 `null`。
- **Slack 推送 `post_slack(text)`**：从环境变量 `SLACK_WEBHOOK_URL` 读 webhook（不硬编码、不进 git）。三张表拼成等宽 code block（Slack 不渲染 markdown 表格，用 ``` 包裹 + `tabulate(tablefmt="plain"|"simple")`）。
- **JSON 存档 `append_archive()`**：见 §5。

## 4. 数据流

1. 入口确定 run 时刻（UTC now），各所 `resolve_window` 得到 3 个 UTC 日。
2. 对每个 (exch, coin, typ) 序列：下载窗口内逐笔 → 构造 notional → 按天分组 → 每天算 P50/75/90 → 取 3 天中位。
3. 拉该序列小时线 → 中位小时量 → 算 POV(P75)。
4. 汇总成行，三所各一张表。
5. 拼 Slack 文本 → 推送。
6. 追加写 `data/daily_report.json`。

## 5. JSON 存档结构

单文件 `data/daily_report.json`，顶层是按 run_date 索引的对象（便于查任意历史日、且重复跑同一天会覆盖而非重复）：

```json
{
  "2026-06-16": {
    "run_at_utc": "2026-06-16T00:00:05Z",
    "windows": { "OKX": ["...","...","..."], "Bybit": [...], "Binance": [...] },
    "rows": [
      {
        "exch": "OKX", "coin": "BTC", "type": "合约",
        "p50": 54.0, "p75": 415.0, "p90": 3380.0,
        "notional_sum": 1.17e10, "trades": 4760141,
        "hourly_vol_median": 255700000.0,
        "pov_p75": 0.000487
      }
    ]
  }
}
```

- 数值存**原始数（未格式化）**，便于后续程序化查询/对比。
- `pov_p75` 存小数（0.000487 = 0.0487%），展示时再 ×100。
- 重复跑同一 run_date 覆盖该键。

## 6. 调度

crontab 一行，北京 8 点 = UTC 00:00：

```cron
0 0 * * * cd /Users/mac/dev/trade_size && SLACK_WEBHOOK_URL=... /path/to/uv run daily_report.py >> data/daily_report.log 2>&1
```

- webhook 通过环境变量注入（cron 行里设或从 `~/.config` 等读，**不进仓库**）。
- 失败重试沿用现有 `_get` 的指数退避；整轮失败也要推一条 Slack 错误提示（避免静默失败）。

## 7. 代码复用与改动

- 将 `daily_stats.py` 中可复用的部分（`_get`, `dl_okx`, `dl_by`, `dl_bn`, `OKX_CTVAL`, `BYLIN`, notional 构造）**重构为可 import 的函数/模块**（如抽到 `trade_data.py` 或直接 `from daily_stats import ...`）。优先 import，避免复制粘贴漂移。
- `daily_report.py` 为新增文件，不改 `daily_stats.py` 的对外行为（其 `main()` 与固定 `KEEP_DAYS` 保留）。
- 不做无关重构、不删旧脚本。

## 8. 错误处理

- 单序列下载失败：记录、跳过该行，不中断整轮（沿用现有 `run` 的 try/except）。
- 小时线拉取失败：该行 POV = null，其余照出。
- 窗口探测凑不齐 3 天：用能凑到的（≥1 天）并在 Slack 文案标注「窗口不足」。
- Slack 推送失败：脚本退出码非 0，错误进 log，便于 cron 排查。
- 整轮异常：捕获后也尝试推一条 Slack 错误提示。

## 9. 验证

- **窗口探测**：对已知日期断言探测结果落在「昨天 UTC 往前的已发布日」，且 OKX 完整性（当日+次日齐）正确。
- **统计口径回归**：对某个历史窗口（如 Binance 06-11~06-13），新脚本算出的 P50/75/90 与现有 `daily_stats.py`/文档数值一致（同口径应一致）。
- **POV 抽样核对**：手算某币 `300×P75 / 中位小时量`，与脚本输出一致。
- **JSON 往返**：写入后读回，结构、数值无损。
- **Slack**：先用测试 webhook 跑一次，确认表格在 Slack 里等宽可读。

## 9b. 增量缓存（2026-06-16 追加需求）

> 用户要求：历史逐笔数据存本地、不重复拉取。

- `trade_data.build_notional` 改为**增量缓存 + 持久化**，与 `daily_stats.py` **共用同一批** `data/daily/<exch>_<sheet>.parquet`（schema 一致：ts/price/size_raw/qty/notional/day）。
- 逻辑（完全对齐 daily_stats.build 的增量口径）：读本地 parquet → `have`=已存的 UTC 日 → `need`=窗口里缺的天 → 只下 `need` → 合并存盘（历史累加）→ 返回窗口内切片。
- sheet 命名：OKX=`inst_for`（如 `BTC-USDT-SWAP`）；Bybit=`{sym}_{linear|spot}`；Binance=`{sym}_linear`——与 daily_stats 现有文件名一致，可直接复用已有缓存（如 Binance 06-13/06-14 已在缓存）。
- 滚动 3 天窗口天天重叠 2 天 → 每天实际只新下 1 天；二次运行同一窗口**零下载**。
- 新增缓存行为测试（monkeypatch `dl_*` 计下载次数，验证二次运行不重下、新增天只下增量）。

## 10. 非目标（YAGNI）

- 不做盘口深度/funding 合成（那是 `recommend_size.py` 的事，本报告只出成交分位 + POV）。
- 不做盘口深度/funding 合成以外，聚合口径已采用（见 §2：Binance aggTrades、OKX/Bybit 同秒同价）——非原始逐笔。
- 不做可视化图表、不做 Web 界面。
- 不重写/删除现有历史脚本。
