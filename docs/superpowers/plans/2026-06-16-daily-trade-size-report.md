# 每日单笔报单量报告 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单笔报单量研究沉淀为每天自动跑的管线：用最近 3 个已发布 UTC 日的逐笔数据算 OKX/Bybit/Binance 各序列单笔 notional 的 P50/75/90（3 天中位）+ POV(P75)，每天北京 8 点推 Slack 并存档 JSON。

**Architecture:** 新建 `trade_data.py`（下载+notional 单一真相源，daily_stats.py 改为 import 它）与编排脚本 `daily_report.py`（窗口探测→统计→POV→Slack→JSON）。纯函数 TDD，联网函数 smoke 验证。

**Tech Stack:** Python 3.11+，uv 单文件脚本(PEP723)，pandas/numpy/requests/urllib3/tabulate，pytest 测试，crontab 调度，Slack incoming webhook。

> **Git 说明：** 本项目当前不是 git 仓库，且用户规则为「仅在明确要求时 commit」。各 Task 末尾用 **Checkpoint**（跑测试/smoke 确认）替代 commit，不执行 git。
>
> **测试运行命令（全程统一）：**
> ```
> uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/ -v
> ```

---

## 文件结构

- `trade_data.py`（新建）：下载（`dl_okx/dl_by/dl_bn`）+ 归档存在性探测（`_exists`）+ notional 构造（`to_notional`）+ 窗口下载（`build_notional`）+ 常量（`OKX_CTVAL/BYLIN`）。daily_stats.py 与 daily_report.py 共用。
- `daily_stats.py`（修改）：删除内部重复的下载/常量定义，改为 `from trade_data import ...`。对外行为（main/KEEP_DAYS）不变。
- `daily_report.py`（新建）：编排——`resolve_window` / `median_daily_percentiles` / `pov` / `hourly_median`（+ 各所小时线 fetch）/ `append_archive` / `format_report` / `post_slack` / `main`。
- `tests/test_trade_data.py`（新建）：`to_notional` 纯函数测试。
- `tests/test_daily_report.py`（新建）：`resolve_window` / `median_daily_percentiles` / `pov` / `append_archive` / `format_report` 纯函数测试。
- `docs/cron_setup.md`（新建）：crontab + 环境变量部署说明。

---

## Task 1: `trade_data.py` — 下载与 notional 核心

**Files:**
- Create: `trade_data.py`
- Test: `tests/test_trade_data.py`

- [ ] **Step 1: 写失败测试 `tests/test_trade_data.py`**

```python
import pandas as pd
from trade_data import to_notional, inst_for, okx_files_for


def test_to_notional_okx_swap_multiplies_ctval():
    raw = pd.DataFrame({"created_time": [1781136000000], "size": [2], "price": [60000.0]})
    out = to_notional("OKX", "BTC", "swap", raw)  # ctVal=0.01
    assert out["qty"].iloc[0] == 2 * 0.01
    assert out["notional"].iloc[0] == 2 * 0.01 * 60000.0


def test_to_notional_okx_spot_no_ctval():
    raw = pd.DataFrame({"created_time": [1781136000000], "size": [3], "price": [10.0]})
    out = to_notional("OKX", "LINK", "spot", raw)
    assert out["notional"].iloc[0] == 3 * 10.0


def test_to_notional_bybit_linear_ts_to_ms():
    raw = pd.DataFrame({"timestamp": [1781136000.5], "size": [4.0], "price": [2.0]})
    out = to_notional("Bybit", "XRP", "linear", raw)
    assert out["ts"].iloc[0] == 1781136000500
    assert out["notional"].iloc[0] == 8.0


def test_to_notional_bybit_spot_uses_volume_col():
    raw = pd.DataFrame({"timestamp": [1781136000000], "volume": [5.0], "price": [2.0]})
    out = to_notional("Bybit", "XRP", "spot", raw)
    assert out["notional"].iloc[0] == 10.0


def test_to_notional_binance_uses_quote_qty():
    raw = pd.DataFrame({"time": [1781136000000], "qty": [0.1], "price": [60000.0], "quote_qty": [5999.5]})
    out = to_notional("Binance", "BTC", "linear", raw)
    assert out["notional"].iloc[0] == 5999.5  # 直取 quote_qty，非 price*qty


def test_to_notional_empty_returns_none():
    assert to_notional("OKX", "BTC", "swap", pd.DataFrame()) is None
    assert to_notional("OKX", "BTC", "swap", None) is None


def test_inst_for():
    assert inst_for("OKX", "BTC", "swap") == "BTC-USDT-SWAP"
    assert inst_for("OKX", "BTC", "spot") == "BTC-USDT"
    assert inst_for("Bybit", "PEPE", "linear") == "1000PEPEUSDT"
    assert inst_for("Bybit", "BTC", "spot") == "BTCUSDT"
    assert inst_for("Binance", "ETH", "linear") == "ETHUSDT"


def test_okx_files_for_includes_nextday():
    assert okx_files_for(["2026-06-11"]) == ["2026-06-11", "2026-06-12"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_trade_data.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'trade_data'`

- [ ] **Step 3: 写 `trade_data.py`**

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas"]
# ///
"""共享：交易所逐笔归档下载 + notional 构造（OKX/Bybit/Binance）。
daily_stats.py 与 daily_report.py 都从这里 import —— 单一真相源，避免口径漂移。
口径完全等同原 daily_stats.py。"""
import datetime as dt
import gzip, io, time, zipfile
import requests, urllib3, pandas as pd

urllib3.disable_warnings()
PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
BYLIN = {"PEPE": "1000PEPEUSDT"}
OKX_CTVAL = {"BTC": 0.01, "SUI": 1, "AAVE": 0.1, "DOGE": 1000, "LINK": 1, "ARB": 10, "XRP": 100, "PEPE": 1e7}


def _get(url, retries=5):
    for a in range(retries):
        try:
            r = requests.get(url, timeout=180, verify=False, proxies=PROXY)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(2 ** a)


def _exists(url):
    """HEAD 探测归档是否存在（404→False）。部分服务器不支持 HEAD 时回退 GET。"""
    try:
        r = requests.head(url, timeout=30, verify=False, proxies=PROXY, allow_redirects=True)
        if r.status_code in (403, 405):
            r = requests.get(url, timeout=60, verify=False, proxies=PROXY, stream=True)
        return r.status_code == 200
    except Exception:
        return False


def _nextday(d):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()


def okx_files_for(days):
    return sorted({x for d in days for x in (d, _nextday(d))})


def inst_for(exch, coin, typ):
    is_contract = typ in ("swap", "linear")
    if exch == "OKX":
        return f"{coin}-USDT-SWAP" if is_contract else f"{coin}-USDT"
    if exch == "Bybit":
        return BYLIN.get(coin, f"{coin}USDT") if is_contract else f"{coin}USDT"
    return f"{coin}USDT"


def dl_okx(inst, date):
    c = _get(f"https://www.okx.com/cdn/okex/traderecords/trades/daily/{date.replace('-','')}/{inst}-trades-{date}.zip")
    if c is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(c))
    return pd.read_csv(z.open(z.namelist()[0]))


def dl_by(kind, sym, date):
    url = (f"https://public.bybit.com/trading/{sym}/{sym}{date}.csv.gz" if kind == "linear"
           else f"https://public.bybit.com/spot/{sym}/{sym}_{date}.csv.gz")
    c = _get(url)
    return None if c is None else pd.read_csv(gzip.open(io.BytesIO(c)))


def dl_bn(sym, date):
    c = _get(f"https://data.binance.vision/data/futures/um/daily/trades/{sym}/{sym}-trades-{date}.zip")
    if c is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(c))
    return pd.read_csv(z.open(z.namelist()[0]))


def to_notional(exch, coin, typ, raw):
    """一所一日原始逐笔 → 统一列 ts/price/size_raw/qty/notional。空/None 返回 None。"""
    if raw is None or raw.empty:
        return None
    is_contract = typ in ("swap", "linear")
    if exch == "OKX":
        ts = pd.to_numeric(raw["created_time"]).astype("int64")
        sz = pd.to_numeric(raw["size"]); px = pd.to_numeric(raw["price"])
        qty = sz * OKX_CTVAL[coin] if is_contract else sz
        return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": qty, "notional": qty * px})
    if exch == "Bybit":
        if is_contract:
            ts = (pd.to_numeric(raw["timestamp"]) * 1000).round().astype("int64"); sz = pd.to_numeric(raw["size"])
        else:
            ts = pd.to_numeric(raw["timestamp"]).astype("int64"); sz = pd.to_numeric(raw["volume"])
        px = pd.to_numeric(raw["price"])
        return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": sz * px})
    # Binance：quote_qty 即归档直给的精确 USDT 成交额
    ts = pd.to_numeric(raw["time"]).astype("int64")
    px = pd.to_numeric(raw["price"]); sz = pd.to_numeric(raw["qty"])
    return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": pd.to_numeric(raw["quote_qty"])})


def build_notional(exch, coin, typ, days):
    """下载 days(UTC列表) 的逐笔，返回带 day 列、已按 day 过滤的统一 notional DataFrame；无数据返回 None。
    OKX 按 UTC+8 切档，需当日+次日两文件。"""
    is_contract = typ in ("swap", "linear")
    inst = inst_for(exch, coin, typ)
    frames = []
    if exch == "OKX":
        for d in okx_files_for(days):
            f = to_notional(exch, coin, typ, dl_okx(inst, d))
            if f is not None:
                frames.append(f)
    elif exch == "Bybit":
        for d in days:
            f = to_notional(exch, coin, typ, dl_by("linear" if is_contract else "spot", inst, d))
            if f is not None:
                frames.append(f)
    else:
        for d in days:
            f = to_notional(exch, coin, typ, dl_bn(inst, d))
            if f is not None:
                frames.append(f)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).dropna()
    df["day"] = pd.to_datetime(df.ts, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    return df[df.day.isin(days)].sort_values("ts").reset_index(drop=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_trade_data.py -v`
Expected: PASS（9 个测试全过）

- [ ] **Step 5: Checkpoint** — 全部测试通过即视为完成本任务（不 commit）。

---

## Task 2: `daily_stats.py` 改为 import `trade_data`

**Files:**
- Modify: `daily_stats.py`（删除内部 `_get/dl_okx/dl_by/dl_bn/_nextday/okx_files_for/PROXY/BYLIN/OKX_CTVAL`，改 import）

- [ ] **Step 1: 记录改前基线（回归对照）**

Run: `uv run daily_stats.py 2>&1 | tail -40 > /tmp/daily_stats_before.txt`
说明：daily_stats 是增量脚本，已有 parquet 时多数序列会「skip」，输出快。保存输出做改后对照。

- [ ] **Step 2: 改 `daily_stats.py` 顶部**

删除这些行（22-50 行附近的）：`import gzip, io, time, zipfile`、`PROXY = ...`、`BYLIN = ...`、`OKX_CTVAL = ...`、函数 `_get`、`dl_okx`、`dl_by`、`dl_bn`、`_nextday`、`okx_files_for`。

在 import 区加：

```python
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
from tabulate import tabulate
from trade_data import _get, dl_okx, dl_by, dl_bn, okx_files_for, BYLIN, OKX_CTVAL
```

保留 `by_files_for`（daily_stats 私有）、`KEEP_DAYS`、`COINS`、`TYPES`、`PCTS`、`OUTDIR`、`build`、`main`。`build` 内部对 `dl_okx/dl_by/dl_bn/okx_files_for/OKX_CTVAL/BYLIN` 的调用保持不变（现在来自 import）。

- [ ] **Step 3: 跑回归对照**

Run: `uv run daily_stats.py 2>&1 | tail -40 > /tmp/daily_stats_after.txt && diff /tmp/daily_stats_before.txt /tmp/daily_stats_after.txt && echo SAME`
Expected: 打印 `SAME`（输出无差异）。若有差异，说明 import 漏了某符号，修正。

- [ ] **Step 4: 跑 Task 1 测试确保未破坏**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_trade_data.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint** — `SAME` + 测试通过即完成。

---

## Task 3: `daily_report.py` — 窗口探测 `resolve_window`

**Files:**
- Create: `daily_report.py`（先只放 header + `resolve_window` + 各所探测器）
- Test: `tests/test_daily_report.py`

- [ ] **Step 1: 写失败测试 `tests/test_daily_report.py`**

```python
import datetime as dt
from daily_report import resolve_window


def test_resolve_window_all_available():
    # 全部存在：从昨天往前取 3 个
    days = resolve_window(lambda ds: True, dt.date(2026, 6, 16), n=3)
    assert days == ["2026-06-13", "2026-06-14", "2026-06-15"]  # 升序，最新=昨天


def test_resolve_window_skips_missing():
    # 06-15 归档没出（如 T+1 还没发布），跳过往前凑
    missing = {"2026-06-15"}
    days = resolve_window(lambda ds: ds not in missing, dt.date(2026, 6, 16), n=3)
    assert days == ["2026-06-12", "2026-06-13", "2026-06-14"]


def test_resolve_window_insufficient_returns_partial():
    # 只有一天可用
    ok = {"2026-06-10"}
    days = resolve_window(lambda ds: ds in ok, dt.date(2026, 6, 16), n=3, max_back=8)
    assert days == ["2026-06-10"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'daily_report'`

- [ ] **Step 3: 建 `daily_report.py`（header + resolve_window + 探测器）**

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas", "numpy", "pyarrow", "tabulate"]
# ///
"""每日单笔报单量报告：最近3个已发布UTC日 → P50/75/90(3天中位)+POV(P75) → Slack + JSON 存档。
用法: SLACK_WEBHOOK_URL=... uv run daily_report.py
口径=trade_data 的原始·全部（每笔 notional 分位，不聚合不去微）。"""
import argparse, datetime as dt, json, os, sys
import numpy as np, pandas as pd, requests
from tabulate import tabulate
from trade_data import (PROXY, _exists, build_notional, inst_for)

COINS = {
    "OKX":     ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"],
    "Bybit":   ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"],
    "Binance": ["BTC", "ETH", "SOL", "XRP", "LINK", "DOGE"],
}
TYPES = {"OKX": ("spot", "swap"), "Bybit": ("spot", "linear"), "Binance": ("linear",)}
PCTS = (50, 75, 90)
ORDERS_PER_HOUR = 300   # 5 单/min × 60
ARCHIVE = "data/daily_report.json"
PROBE_COIN = "BTC"      # 用最活跃的币探测当日归档是否发布


def _okx_avail(ds):
    inst = inst_for("OKX", PROBE_COIN, "swap")
    nd = (dt.date.fromisoformat(ds) + dt.timedelta(days=1)).isoformat()
    base = "https://www.okx.com/cdn/okex/traderecords/trades/daily"
    return (_exists(f"{base}/{ds.replace('-','')}/{inst}-trades-{ds}.zip")
            and _exists(f"{base}/{nd.replace('-','')}/{inst}-trades-{nd}.zip"))


def _bybit_avail(ds):
    sym = inst_for("Bybit", PROBE_COIN, "linear")
    return _exists(f"https://public.bybit.com/trading/{sym}/{sym}{ds}.csv.gz")


def _binance_avail(ds):
    sym = inst_for("Binance", PROBE_COIN, "linear")
    return _exists(f"https://data.binance.vision/data/futures/um/daily/trades/{sym}/{sym}-trades-{ds}.zip")


AVAIL = {"OKX": _okx_avail, "Bybit": _bybit_avail, "Binance": _binance_avail}


def resolve_window(exists_fn, today_utc, n=3, max_back=12):
    """从昨天UTC往前找 n 个 exists_fn(date_str)==True 的日；返回升序列表（不足 n 个则返回已找到的）。"""
    found = []
    d = today_utc - dt.timedelta(days=1)
    for _ in range(max_back):
        ds = d.isoformat()
        if exists_fn(ds):
            found.append(ds)
            if len(found) == n:
                break
        d -= dt.timedelta(days=1)
    return sorted(found)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 5: Checkpoint** — 测试通过即完成。

---

## Task 4: 统计 `median_daily_percentiles`

**Files:**
- Modify: `daily_report.py`（追加函数）
- Test: `tests/test_daily_report.py`（追加测试）

- [ ] **Step 1: 追加失败测试到 `tests/test_daily_report.py`**

```python
import pandas as pd
from daily_report import median_daily_percentiles


def test_median_daily_percentiles_takes_median_over_days():
    # 3 天，每天 notional 全等于该天常数 10/20/30 → 各分位每天=该常数 → 跨天中位=20
    df = pd.DataFrame({
        "day": ["2026-06-11"] * 3 + ["2026-06-12"] * 3 + ["2026-06-13"] * 3,
        "notional": [10, 10, 10, 20, 20, 20, 30, 30, 30],
    })
    out = median_daily_percentiles(df, pcts=(50, 75, 90))
    assert out[50] == 20.0
    assert out[75] == 20.0
    assert out[90] == 20.0


def test_median_daily_percentiles_returns_floats():
    df = pd.DataFrame({"day": ["2026-06-11", "2026-06-11"], "notional": [100, 300]})
    out = median_daily_percentiles(df, pcts=(50,))
    assert out[50] == 200.0
    assert isinstance(out[50], float)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py::test_median_daily_percentiles_takes_median_over_days -v`
Expected: FAIL，`ImportError: cannot import name 'median_daily_percentiles'`

- [ ] **Step 3: 追加实现到 `daily_report.py`**

```python
def median_daily_percentiles(df, pcts=PCTS):
    """df 有 day, notional 列。每天算各分位，再对天取中位数。返回 {pct: float}。"""
    out = {}
    for p in pcts:
        per_day = df.groupby("day")["notional"].quantile(p / 100.0)
        out[p] = float(np.median(per_day.values))
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: PASS（含新 2 个）

- [ ] **Step 5: Checkpoint** — 测试通过即完成。

---

## Task 5: POV 与小时成交量

**Files:**
- Modify: `daily_report.py`（追加 `pov` + `hourly_median` + 各所小时线 fetch）
- Test: `tests/test_daily_report.py`（追加 `pov` 测试）

- [ ] **Step 1: 追加 `pov` 失败测试**

```python
from daily_report import pov


def test_pov_basic():
    # 300 单 × $1000 / 每小时 $30,000,000 = 0.01 (=1%)
    assert pov(1000.0, 30_000_000.0) == 300 * 1000.0 / 30_000_000.0


def test_pov_none_when_no_volume():
    assert pov(1000.0, None) is None
    assert pov(1000.0, 0) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py::test_pov_basic -v`
Expected: FAIL，`ImportError: cannot import name 'pov'`

- [ ] **Step 3: 追加 `pov` + `hourly_median` + 小时线 fetch 到 `daily_report.py`**

```python
def pov(p75, hourly_vol, orders_per_hour=ORDERS_PER_HOUR):
    """每小时下单额 / 每小时成交量。hourly_vol 缺失或非正 → None。"""
    if not hourly_vol or hourly_vol <= 0:
        return None
    return orders_per_hour * p75 / hourly_vol


def _okx_hourly(inst, days):
    r = requests.get("https://www.okx.com/api/v5/market/candles",
                     params={"instId": inst, "bar": "1H", "limit": 300},
                     proxies=PROXY, verify=False, timeout=20)
    rows = r.json()["data"]   # [ts, o,h,l,c, vol, volCcy, volCcyQuote, confirm]
    return [(int(x[0]), float(x[7])) for x in rows]


def _bybit_hourly(cat, sym, days):
    r = requests.get("https://api.bybit.com/v5/market/kline",
                     params={"category": cat, "symbol": sym, "interval": 60, "limit": 1000},
                     proxies=PROXY, verify=False, timeout=20)
    rows = r.json()["result"]["list"]  # [start, o,h,l,c, volume, turnover]
    return [(int(x[0]), float(x[6])) for x in rows]


def _binance_hourly(sym, days):
    start = int(pd.Timestamp(min(days) + "T00:00:00Z").timestamp() * 1000)
    end = int((pd.Timestamp(max(days) + "T00:00:00Z") + pd.Timedelta(days=1)).timestamp() * 1000)
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": sym, "interval": "1h", "startTime": start, "endTime": end - 1, "limit": 1000},
                     proxies=PROXY, verify=False, timeout=20)
    return [(int(k[0]), float(k[7])) for k in r.json()]


def hourly_median(exch, coin, typ, days):
    """该 instrument 在 days 窗口内 1H quoteVolume(USDT) 的中位。拉取失败/无数据返回 None。"""
    is_contract = typ in ("swap", "linear")
    lo = int(pd.Timestamp(min(days) + "T00:00:00Z").timestamp() * 1000)
    hi = int((pd.Timestamp(max(days) + "T00:00:00Z") + pd.Timedelta(days=1)).timestamp() * 1000)
    try:
        if exch == "OKX":
            pairs = _okx_hourly(inst_for("OKX", coin, typ), days)
        elif exch == "Bybit":
            pairs = _bybit_hourly("linear" if is_contract else "spot", inst_for("Bybit", coin, typ), days)
        else:
            pairs = _binance_hourly(inst_for("Binance", coin, typ), days)
    except Exception:
        return None
    vols = [v for ts, v in pairs if lo <= ts < hi]
    return float(np.median(vols)) if vols else None
```

- [ ] **Step 4: 跑 pov 测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: PASS

- [ ] **Step 5: Smoke 验证小时线（联网）**

Run:
```
uv run python -c "import sys; sys.argv=['x']; import daily_report as r; \
print('OKX BTC swap', r.hourly_median('OKX','BTC','swap',['2026-06-13','2026-06-14','2026-06-15'])); \
print('Bybit BTC spot', r.hourly_median('Bybit','BTC','spot',['2026-06-13','2026-06-14','2026-06-15'])); \
print('Binance BTC linear', r.hourly_median('Binance','BTC','linear',['2026-06-13','2026-06-14','2026-06-15']))"
```
Expected: 三个都是数亿~数十亿量级的正数（非 None）。若 OKX/Bybit 因 `limit` 不够覆盖窗口而返回 None，把窗口换成最近 3 天重试。

- [ ] **Step 6: Checkpoint** — 测试 PASS + smoke 三所都出正数即完成。

---

## Task 6: JSON 存档 `append_archive`

**Files:**
- Modify: `daily_report.py`（追加 `append_archive`）
- Test: `tests/test_daily_report.py`（追加测试）

- [ ] **Step 1: 追加失败测试**

```python
import json
from daily_report import append_archive


def test_append_archive_writes_and_reads(tmp_path):
    p = tmp_path / "rep.json"
    entry = {"run_at_utc": "2026-06-16T00:00:05Z", "rows": [{"coin": "BTC", "p75": 415.0}]}
    append_archive(str(p), "2026-06-16", entry)
    data = json.loads(p.read_text())
    assert data["2026-06-16"]["rows"][0]["p75"] == 415.0


def test_append_archive_keeps_old_and_overwrites_same_day(tmp_path):
    p = tmp_path / "rep.json"
    append_archive(str(p), "2026-06-15", {"v": 1})
    append_archive(str(p), "2026-06-16", {"v": 2})
    append_archive(str(p), "2026-06-16", {"v": 3})  # 覆盖同日
    data = json.loads(p.read_text())
    assert data["2026-06-15"]["v"] == 1
    assert data["2026-06-16"]["v"] == 3
    assert len(data) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py::test_append_archive_writes_and_reads -v`
Expected: FAIL，`ImportError: cannot import name 'append_archive'`

- [ ] **Step 3: 追加实现**

```python
from pathlib import Path


def append_archive(path, run_date, entry):
    """单 JSON 文件，按 run_date 为顶层 key 追加；同日覆盖。"""
    p = Path(path)
    data = json.loads(p.read_text()) if p.exists() else {}
    data[run_date] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint** — 测试通过即完成。

---

## Task 7: Slack 文案 `format_report` + 推送 `post_slack`

**Files:**
- Modify: `daily_report.py`（追加 `format_report` + `post_slack` + `_fu`）
- Test: `tests/test_daily_report.py`（追加 `format_report` 测试）

- [ ] **Step 1: 追加失败测试**

```python
from daily_report import format_report


def _row(exch, coin, typ, pov):
    return {"exch": exch, "coin": coin, "type": typ, "p50": 50.0, "p75": 415.0,
            "p90": 3380.0, "hourly_vol_median": 2.5e8, "pov_p75": pov}


def test_format_report_contains_sections_and_pov_percent():
    rows = [_row("OKX", "BTC", "合约", 0.000487), _row("Binance", "BTC", "永续", None)]
    windows = {"OKX": ["2026-06-13", "2026-06-14", "2026-06-15"], "Binance": ["2026-06-13", "2026-06-14", "2026-06-15"]}
    text = format_report("2026-06-16", windows, rows)
    assert "OKX" in text and "Binance" in text
    assert "BTC" in text
    assert "0.0487%" in text   # pov 小数 → 百分比展示
    assert "—" in text          # None POV 显示占位
    assert "```" in text        # 等宽 code block
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py::test_format_report_contains_sections_and_pov_percent -v`
Expected: FAIL，`ImportError: cannot import name 'format_report'`

- [ ] **Step 3: 追加实现**

```python
def _fu(v):
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}k"
    return f"${v:.0f}"


def _pov_str(p):
    return "—" if p is None else f"{p*100:.4f}%"


def format_report(run_date, windows, rows):
    """拼 Slack 文本：每所一张等宽表（code block 包裹）。"""
    headers = ["币", "类型", "P50", "P75", "P90", "小时量中位", "POV(P75,5单/min)"]
    parts = [f"*每日单笔报单量报告 · {run_date}*"]
    for exch in ("OKX", "Bybit", "Binance"):
        ex_rows = [r for r in rows if r["exch"] == exch]
        if not ex_rows:
            continue
        win = windows.get(exch, [])
        win_note = "/".join(d[5:] for d in win) if win else "无可用窗口"
        if len(win) < 3:
            win_note += " ⚠️窗口不足3天"
        table = [[r["coin"], r["type"], _fu(r["p50"]), _fu(r["p75"]), _fu(r["p90"]),
                  _fu(r["hourly_vol_median"]), _pov_str(r["pov_p75"])] for r in ex_rows]
        body = tabulate(table, headers=headers, tablefmt="simple")
        parts.append(f"*{exch}*（窗口 {win_note}）\n```\n{body}\n```")
    return "\n\n".join(parts)


def post_slack(webhook, text):
    """推送到 Slack incoming webhook（走代理，CN 需要）。"""
    r = requests.post(webhook, json={"text": text}, proxies=PROXY, verify=False, timeout=30)
    r.raise_for_status()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/test_daily_report.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Checkpoint** — 测试通过即完成。

---

## Task 8: `main()` 编排 + 错误处理 + 端到端 smoke

**Files:**
- Modify: `daily_report.py`（追加 `build_rows` + `main` + `__main__`）

- [ ] **Step 1: 追加编排实现到 `daily_report.py`**

```python
def build_rows(windows):
    """对每个序列下载→统计→POV，返回 rows 列表。单序列失败跳过不中断。"""
    rows = []
    for exch in ("OKX", "Bybit", "Binance"):
        days = windows.get(exch, [])
        if not days:
            continue
        for coin in COINS[exch]:
            for typ in TYPES[exch]:
                try:
                    df = build_notional(exch, coin, typ, days)
                    if df is None or df.empty:
                        print(f"  ✗ {exch} {coin} {typ}: 无数据", flush=True)
                        continue
                    pcts = median_daily_percentiles(df, PCTS)
                    hv = hourly_median(exch, coin, typ, days)
                    label = "现货" if typ == "spot" else ("永续" if exch == "Binance" else "合约")
                    rows.append({
                        "exch": exch, "coin": coin, "type": label,
                        "p50": pcts[50], "p75": pcts[75], "p90": pcts[90],
                        "notional_sum": float(df.notional.sum()), "trades": int(len(df)),
                        "hourly_vol_median": hv, "pov_p75": pov(pcts[75], hv),
                    })
                    print(f"  ✓ {exch} {coin} {typ}: {len(df):,} 笔", flush=True)
                except Exception as ex:
                    print(f"  ✗ {exch} {coin} {typ}: {repr(ex)[:80]}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-slack", action="store_true", help="只算+存档，不推 Slack")
    ap.add_argument("--archive", default=ARCHIVE)
    args = ap.parse_args()

    today_utc = dt.datetime.now(dt.timezone.utc).date()
    run_date = today_utc.isoformat()
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    try:
        print("探测各所可用归档窗口...", flush=True)
        windows = {e: resolve_window(AVAIL[e], today_utc) for e in ("OKX", "Bybit", "Binance")}
        for e, w in windows.items():
            print(f"  {e}: {w}", flush=True)
        rows = build_rows(windows)
        if not rows:
            raise RuntimeError("无任何序列出数（下载或窗口全失败）")
        entry = {"run_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "windows": windows, "rows": rows}
        append_archive(args.archive, run_date, entry)
        print(f"✓ 存档 {args.archive}（{len(rows)} 行）", flush=True)
        text = format_report(run_date, windows, rows)
        print("\n" + text, flush=True)
        if not args.no_slack:
            if not webhook:
                raise RuntimeError("缺 SLACK_WEBHOOK_URL 环境变量")
            post_slack(webhook, text)
            print("✓ 已推 Slack", flush=True)
    except Exception as ex:
        msg = f"⚠️ 每日报告失败 {run_date}: {repr(ex)[:200]}"
        print(msg, file=sys.stderr, flush=True)
        if webhook and not args.no_slack:
            try:
                post_slack(webhook, msg)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 端到端 smoke（不推 Slack）**

Run: `uv run daily_report.py --no-slack --archive /tmp/test_report.json`
Expected:
- 打印三所窗口（各 3 个最近 UTC 日）；
- 逐序列 `✓`（少量 `✗` 可接受，如某薄币某天无归档）；
- 打印三张等宽表，BTC/ETH POV 很小（<0.5%）、LINK 偏大；
- `✓ 存档 /tmp/test_report.json`。

- [ ] **Step 3: 校验存档 JSON 结构**

Run: `uv run python -c "import json; d=json.load(open('/tmp/test_report.json')); k=list(d)[0]; print('run_date',k); print('windows',d[k]['windows']); print('rows',len(d[k]['rows'])); print(d[k]['rows'][0])"`
Expected: 打印 run_date、三所窗口、行数、第一行含 exch/coin/type/p50/p75/p90/hourly_vol_median/pov_p75。

- [ ] **Step 4: POV 抽样手算核对**

从上一步输出取某行（如 OKX BTC 合约）的 `p75` 与 `hourly_vol_median`，手算 `300*p75/hourly_vol_median`，与该行 `pov_p75` 比对一致（差异 < 1e-9）。

- [ ] **Step 5: Checkpoint** — 端到端跑通、JSON 结构正确、POV 手算一致即完成。

---

## Task 9: Slack 实推验证 + crontab 部署

**Files:**
- Create: `docs/cron_setup.md`

- [ ] **Step 1: 取 webhook 实推一次**

前置：用户提供 `SLACK_WEBHOOK_URL`（incoming webhook）。
Run: `SLACK_WEBHOOK_URL='<用户提供>' uv run daily_report.py --archive /tmp/test_report.json`
Expected: 终端 `✓ 已推 Slack`；Slack 频道收到消息，三张表在 code block 里**等宽对齐可读**。若不齐，调 `tabulate` 的 `tablefmt`（`simple`→`plain`）或缩列。

- [ ] **Step 2: 写 `docs/cron_setup.md`**

```markdown
# 每日报告部署（crontab）

## 环境变量
webhook 不进仓库。在专用文件存放，例如 `~/.config/trade_size.env`：
```
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/XXX/YYY/ZZZ'
```

## crontab
北京 8 点 = UTC 00:00。`crontab -e` 加一行（用 uv 与脚本的绝对路径）：
```
0 0 * * * cd /Users/mac/dev/trade_size && . ~/.config/trade_size.env && /Users/mac/.local/bin/uv run daily_report.py >> data/daily_report.log 2>&1
```
（uv 路径用 `which uv` 确认；macOS 上 cron 需在「系统设置→隐私→完全磁盘访问」授权 cron/终端，否则可能无法访问工作目录。）

## 手动补跑
```
. ~/.config/trade_size.env && uv run daily_report.py
```
单测：`uv run --with pytest --with pandas --with numpy --with pyarrow --with requests --with urllib3 --with tabulate pytest tests/ -v`
```

- [ ] **Step 3: 装 crontab 并验证**

Run（确认 uv 路径）: `which uv`
然后按 `docs/cron_setup.md` 用真实 uv 路径 `crontab -e` 加行，`crontab -l` 确认已写入。
（不等到次日：Step 1 已验证脚本+Slack 链路；cron 仅触发。）

- [ ] **Step 4: Checkpoint** — Slack 收到对齐消息 + crontab 已装 + 部署文档落地即完成。

---

## 自查（spec 覆盖）

- 最近3个已发布UTC日 → Task 3 `resolve_window` + `_*_avail` 探测器 ✓
- P50/75/90 每天算再取3天中位 → Task 4 `median_daily_percentiles` ✓
- OKX/Bybit 现货+合约、Binance 仅永续 → Task 8 `COINS`/`TYPES` ✓
- 原始·全部口径、复用现有逻辑 → Task 1 `trade_data.to_notional`（等同原 daily_stats）+ Task 2 import ✓
- POV(P75, 5单/min)列 + 小时量中位 → Task 5 `pov`/`hourly_median` ✓
- 北京8点推 Slack → Task 7 `post_slack` + Task 9 crontab `0 0 * * *` UTC ✓
- 单 JSON 按 run_date 存档、可查历史 → Task 6 `append_archive` ✓
- 错误不静默（整轮失败也推 Slack）→ Task 8 `main` try/except ✓
- 不改 daily_stats 对外行为 → Task 2 回归 diff `SAME` ✓
