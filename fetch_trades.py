# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "pandas",
#   "pyarrow",
# ]
# ///
"""
OKX 逐笔成交数据抓取脚本（公开归档版）

为什么用归档而不是 history-trades API：
  API 虽支持翻页，但 BTC-USDT-SWAP 一天 60 万+ 笔、翻页要 20min，且需走代理。
  OKX 提供公开历史归档（每合约每天一个 zip，整天真实逐笔），直连可下、几秒一个。

归档来源（直连可达，无需代理）：
  https://www.okx.com/cdn/okex/traderecords/trades/daily/{YYYYMMDD}/{instId}-trades-{YYYY-MM-DD}.zip
  CSV 列: instrument_name,trade_id,side,price,size,created_time(ms)

⚠️ 文件按 **UTC+8（北京）日历日** 切：文件 `2026-05-26` 实含 UTC [05-25 16:00, 05-26 16:00]。
   故 --start/--end 是「CDN 文件日期」（≈北京日），--ts-start/--ts-end 的裁剪窗口按 UTC。
   归档有滞后，当天数据要等北京日结束后才生成（要实时用 API，不在本脚本范围）。

存储：data/okx/<instId>.parquet，每合约一个独立文件（原子写）。
语义：按 --start..--end 从归档重建（OKX tradeId 唯一，concat 后按它去重），
      可选 --ts-start/--ts-end 把结果裁到指定 UTC 窗口。

用法:
    uv run fetch_trades.py --start 2026-05-26                       # 抓 8 个币 spot+swap 这一天
    uv run fetch_trades.py --start 2026-05-26 \\
        --ts-start 1779749901212 --ts-end 1779780654289            # 裁到指定 UTC 窗口
    uv run fetch_trades.py --start 2026-05-26 --inst BTC-USDT-SWAP
"""

import argparse
import io
import logging
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 配置 ─────────────────────────────────────────────────────────────────────

DATA_DIR = Path("data/okx")

COINS = ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"]
DEFAULT_INSTRUMENTS = (
    [f"{c}-USDT"      for c in COINS]   # spot
    + [f"{c}-USDT-SWAP" for c in COINS] # perp
)

MAX_WORKERS = 5

FIELDS = ["tradeId", "ts", "px", "sz", "side"]


# ── 归档下载 ──────────────────────────────────────────────────────────────────

def archive_url(inst: str, date: str) -> str:
    return (f"https://www.okx.com/cdn/okex/traderecords/trades/daily/"
            f"{date.replace('-', '')}/{inst}-trades-{date}.zip")


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """CDN 列映射到统一 FIELDS（tradeId/ts(ms)/px/sz/side）。"""
    out = pd.DataFrame({
        "tradeId": pd.to_numeric(df["trade_id"]).astype("int64"),
        "ts":      pd.to_numeric(df["created_time"]).astype("int64"),
        "px":      pd.to_numeric(df["price"]),
        "sz":      pd.to_numeric(df["size"]),
        "side":    df["side"].astype(str),
    })
    return out.dropna(subset=["ts", "px", "sz"])


def download_day(inst: str, date: str, retries: int = 4) -> pd.DataFrame | None:
    """下载某合约某天归档；404（无文件）返回 None。"""
    url = archive_url(inst, date)
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=180, verify=False)
            if resp.status_code == 404:
                logging.info("[%s] %s 无归档(404)", inst, date)
                return None
            resp.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            return _normalize(df)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logging.warning("[%s] %s 下载失败（第 %d 次），%.0fs 后重试: %s",
                                inst, date, attempt + 1, wait, exc)
                time.sleep(wait)
            else:
                raise


# ── Parquet 写入（每合约一文件，原子写）──────────────────────────────────────

def save_parquet(inst: str, df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p   = DATA_DIR / f"{inst}.parquet"
    tmp = p.parent / (p.name + ".tmp")
    df.to_parquet(tmp, index=False, compression="snappy")
    os.replace(tmp, p)


# ── 单合约任务 ─────────────────────────────────────────────────────────────────

def process(inst: str, dates: list[str],
            window: tuple[int, int] | None) -> pd.DataFrame | None:
    """下载日期范围内全部归档，concat + 按 tradeId 去重，可选裁 UTC 窗口。"""
    frames = [df for d in dates
              if (df := download_day(inst, d)) is not None and len(df)]
    if not frames:
        logging.info("[%s] 无数据", inst)
        return None

    full = pd.concat(frames, ignore_index=True).drop_duplicates("tradeId")
    if window is not None:
        w0, w1 = window
        full = full[(full["ts"] >= w0) & (full["ts"] <= w1)]
    full = full.sort_values("ts").reset_index(drop=True)
    logging.info("[%s] ✓ %d 天 → %d 笔  %s ~ %s UTC", inst, len(frames), len(full),
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
    p = argparse.ArgumentParser(description="OKX 逐笔成交归档下载")
    p.add_argument("--start", required=True, help="起始 CDN 文件日期 YYYY-MM-DD（≈北京日）")
    p.add_argument("--end", default=None, help="结束文件日期（含），默认=--start")
    p.add_argument("--inst", nargs="+", metavar="INST_ID",
                   help=f"指定合约，默认全部 {len(DEFAULT_INSTRUMENTS)} 个")
    p.add_argument("--ts-start", default=None, help="可选：裁到 UTC 窗口起点（ms 或 ISO）")
    p.add_argument("--ts-end", default=None, help="可选：裁到 UTC 窗口终点（ms 或 ISO）")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args()

    dates = date_range(args.start, args.end or args.start)
    instruments = args.inst or DEFAULT_INSTRUMENTS
    window = None
    if args.ts_start and args.ts_end:
        window = (parse_ts(args.ts_start), parse_ts(args.ts_end))

    logging.info("归档下载 | %d 个合约 | 文件日期 %s..%s%s",
                 len(instruments), dates[0], dates[-1],
                 f" | 裁 UTC 窗口 {window[0]}~{window[1]}" if window else "")

    failed: list[str] = []
    written = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process, inst, dates, window): inst
                   for inst in instruments}
        for future in as_completed(futures):
            inst = futures[future]
            try:
                df = future.result()
            except Exception as exc:
                logging.error("[%s] 失败: %s", inst, exc)
                failed.append(inst)
                continue
            if df is None or df.empty:
                continue
            save_parquet(inst, df)
            written += 1

    logging.info("完成！%d 个合约已写入 → %s，%d 个失败", written, DATA_DIR, len(failed))
    if failed:
        logging.error("失败合约: %s", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
