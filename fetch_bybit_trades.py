# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "pandas",
#   "pyarrow",
# ]
# ///
"""
Bybit 逐笔成交数据抓取脚本（公开归档版）

为什么用归档而不是 recent-trade：
  recent-trade 接口不支持翻页、每次只返回最新 ≤1000 条，拿不到历史，
  数据完整度完全取决于轮询脚本何时在跑（会出现大段空洞 + 限流截断）。
  Bybit 提供公开历史归档（每合约每天一个 csv.gz，整天真实逐笔），可下载任意历史。

归档来源（直连可达，无需代理）：
  linear : https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz
  spot   : https://public.bybit.com/spot/{SYMBOL}/{SYMBOL}_{YYYY-MM-DD}.csv.gz

存储：data/bybit/<symbol>_<category>.parquet，每合约一个独立文件（原子写）。
语义：按 --start..--end 的 UTC 日期范围从归档重建（归档不可变、按天不重叠），
      可选 --ts-start/--ts-end 把结果裁到指定窗口。

用法:
    uv run fetch_bybit_trades.py --start 2026-05-25 --end 2026-05-26
    uv run fetch_bybit_trades.py --start 2026-05-25 --end 2026-05-26 \\
        --ts-start 1779749901212 --ts-end 1779780654289     # 裁到指定窗口
    uv run fetch_bybit_trades.py --start 2026-05-26 --inst BTCUSDT linear
"""

import argparse
import gzip
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 配置 ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path("data/bybit")

COINS = ["AAVE", "ARB", "BTC", "DOGE", "LINK", "PEPE", "SUI", "XRP"]

# 部分低价币的永续 symbol 与现货不同（Bybit 用 1000x 计价）
LINEAR_SYMBOL_MAP = {
    "PEPE": "1000PEPEUSDT",
}

def linear_symbol(coin: str) -> str:
    return LINEAR_SYMBOL_MAP.get(coin, f"{coin}USDT")

# (symbol, category) 列表
DEFAULT_INSTRUMENTS: list[tuple[str, str]] = (
    [(f"{c}USDT", "spot")           for c in COINS]
    + [(linear_symbol(c), "linear") for c in COINS]
)

def sheet_name(symbol: str, category: str) -> str:
    return f"{symbol}_{category}"

MAX_WORKERS = 4   # 归档文件较大，控制并发内存

FIELDS = ["execId", "ts", "price", "qty", "side"]


# ── 归档下载 ──────────────────────────────────────────────────────────────────

def archive_url(symbol: str, category: str, date: str) -> str:
    if category == "linear":
        return f"https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz"
    return f"https://public.bybit.com/spot/{symbol}/{symbol}_{date}.csv.gz"


def _to_fields(df: pd.DataFrame, category: str) -> pd.DataFrame:
    """把归档原始列映射到统一 FIELDS（execId/ts(ms)/price/qty/side）。"""
    if category == "linear":
        # 列: timestamp(秒级浮点) side size price ... trdMatchID ...
        ts = (pd.to_numeric(df["timestamp"]) * 1000).round().astype("int64")
        out = pd.DataFrame({
            "execId": df["trdMatchID"].astype(str),
            "ts":     ts,
            "price":  pd.to_numeric(df["price"]),
            "qty":    pd.to_numeric(df["size"]),
            "side":   df["side"].astype(str),
        })
    else:
        # spot 列: id timestamp(ms) price volume side rpi
        out = pd.DataFrame({
            "execId": df["id"].astype(str),
            "ts":     pd.to_numeric(df["timestamp"]).astype("int64"),
            "price":  pd.to_numeric(df["price"]),
            "qty":    pd.to_numeric(df["volume"]),
            "side":   df["side"].astype(str),
        })
    return out.dropna(subset=["ts", "price", "qty"])


def download_day(symbol: str, category: str, date: str,
                 retries: int = 4) -> pd.DataFrame | None:
    """下载某合约某天归档；404（当天无文件）返回 None。"""
    url = archive_url(symbol, category, date)
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=120, verify=False)
            if resp.status_code == 404:
                logging.info("[%s/%s] %s 无归档(404)", symbol, category, date)
                return None
            resp.raise_for_status()
            df = pd.read_csv(gzip.open(io.BytesIO(resp.content)))
            return _to_fields(df, category)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logging.warning("[%s/%s] %s 下载失败（第 %d 次），%.0fs 后重试: %s",
                                symbol, category, date, attempt + 1, wait, exc)
                time.sleep(wait)
            else:
                raise


# ── Parquet 写入（每合约一文件，原子写）──────────────────────────────────────

def save_parquet(sname: str, df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p   = DATA_DIR / f"{sname}.parquet"
    tmp = p.parent / (p.name + ".tmp")
    df.to_parquet(tmp, index=False, compression="snappy")
    os.replace(tmp, p)


# ── 单合约任务 ─────────────────────────────────────────────────────────────────

def process(symbol: str, category: str, dates: list[str],
            window: tuple[int, int] | None) -> pd.DataFrame | None:
    """下载日期范围内全部归档，concat（按天不重叠，无需去重），可选裁窗口。"""
    sname = sheet_name(symbol, category)
    frames = [df for d in dates
              if (df := download_day(symbol, category, d)) is not None and len(df)]
    if not frames:
        logging.info("[%s] 无数据", sname)
        return None

    full = pd.concat(frames, ignore_index=True)
    if window is not None:
        w0, w1 = window
        full = full[(full["ts"] >= w0) & (full["ts"] <= w1)]
    full = full.sort_values("ts").reset_index(drop=True)
    logging.info("[%s] ✓ %d 天 → %d 笔  %s ~ %s UTC", sname, len(frames), len(full),
                 datetime.fromtimestamp(full["ts"].min()/1000, tz=timezone.utc).strftime("%m-%d %H:%M"),
                 datetime.fromtimestamp(full["ts"].max()/1000, tz=timezone.utc).strftime("%m-%d %H:%M"))
    return full


# ── 入口 ──────────────────────────────────────────────────────────────────────

def parse_ts(s: str) -> int:
    """支持毫秒时间戳或 UTC ISO 字符串。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    dt = pd.to_datetime(s.rstrip("Z"), utc=True)
    return int(dt.timestamp() * 1000)


def date_range(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    if d1 < d0:
        raise ValueError("--end 早于 --start")
    out, d = [], d0
    while d <= d1:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bybit 逐笔成交归档下载")
    p.add_argument("--start", required=True, help="起始 UTC 日期 YYYY-MM-DD")
    p.add_argument("--end", default=None, help="结束 UTC 日期（含），默认=--start")
    p.add_argument("--inst", nargs=2, action="append", metavar=("SYMBOL", "CATEGORY"),
                   help="指定合约，例: --inst BTCUSDT linear")
    p.add_argument("--ts-start", default=None, help="可选：裁到窗口起点（ms 或 UTC ISO）")
    p.add_argument("--ts-end", default=None, help="可选：裁到窗口终点（ms 或 UTC ISO）")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args()

    dates = date_range(args.start, args.end or args.start)
    instruments = [tuple(i) for i in args.inst] if args.inst else DEFAULT_INSTRUMENTS
    window = None
    if args.ts_start and args.ts_end:
        window = (parse_ts(args.ts_start), parse_ts(args.ts_end))

    logging.info("归档下载 | %d 个合约 | 日期 %s..%s%s",
                 len(instruments), dates[0], dates[-1],
                 f" | 裁窗口 {window[0]}~{window[1]}" if window else "")

    failed: list[str] = []
    written = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process, sym, cat, dates, window): (sym, cat)
                   for sym, cat in instruments}
        for future in as_completed(futures):
            sym, cat = futures[future]
            sname = sheet_name(sym, cat)
            try:
                df = future.result()
            except Exception as exc:
                logging.error("[%s] 失败: %s", sname, exc)
                failed.append(sname)
                continue
            if df is None or df.empty:
                continue
            save_parquet(sname, df)
            written += 1

    logging.info("完成！%d 个合约已写入 → %s，%d 个失败", written, DATA_DIR, len(failed))
    if failed:
        logging.error("失败合约: %s", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
