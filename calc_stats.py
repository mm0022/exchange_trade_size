# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas",
#   "openpyxl",
#   "tabulate",
# ]
# ///
"""
成交统计脚本（两套分析）

  ① 小时分桶中位数   — 每个整点小时段（如 08:00~09:00）的单笔成交 中位数
  ② 分钟分桶统计     — 每分钟的单笔成交 均值 & 95% 分位数

币量 和 U量（qty × price）各出一版。

读取:
    data/trades.xlsx       (OKX)
    data/bybit_trades.xlsx (Bybit)

输出:
    终端打印摘要 + data/stats.xlsx（4 个 sheet）

用法:
    uv run calc_stats.py
    uv run calc_stats.py --tz 8      # 时区偏移，默认 UTC+8
"""

import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
from tabulate import tabulate

# ── 配置 ─────────────────────────────────────────────────────────────────────

SOURCES = [
    {
        "exchange": "OKX",
        "path":     Path("data/trades.xlsx"),
        "qty_col":  "sz",
        "px_col":   "px",
        "ts_col":   "ts",
    },
    {
        "exchange": "Bybit",
        "path":     Path("data/bybit_trades.xlsx"),
        "qty_col":  "qty",
        "px_col":   "price",
        "ts_col":   "ts",
    },
]

OUTPUT_PATH = Path("data/stats.xlsx")

# OKX SWAP 合约面值：sz（张数）× ctVal = 真实币量
OKX_CTVAL: dict[str, float] = {
    "BTC-USDT-SWAP":  0.01,
    "DOGE-USDT-SWAP": 1000.0,
    "AAVE-USDT-SWAP": 0.1,
    "LINK-USDT-SWAP": 1.0,
    "SUI-USDT-SWAP":  1.0,
}


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_all(tz_offset: int) -> list[dict]:
    """加载所有交易所所有合约，返回标准化的 item 列表。"""
    tz = timezone(timedelta(hours=tz_offset))
    items = []

    for source in SOURCES:
        path = source["path"]
        if not path.exists():
            print(f"[警告] 文件不存在，跳过: {path}")
            continue

        xl = pd.ExcelFile(path)
        for sheet in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet)
            if df.empty:
                continue

            df[source["ts_col"]]  = pd.to_numeric(df[source["ts_col"]],  errors="coerce")
            df[source["qty_col"]] = pd.to_numeric(df[source["qty_col"]], errors="coerce")
            df[source["px_col"]]  = pd.to_numeric(df[source["px_col"]],  errors="coerce")
            df = df.dropna(subset=[source["ts_col"], source["qty_col"], source["px_col"]])
            if df.empty:
                continue

            df["dt"] = pd.to_datetime(df[source["ts_col"]], unit="ms", utc=True).dt.tz_convert(tz)

            # OKX SWAP 合约：sz 是张数，需要 × ctVal 才是真实币量
            raw_qty = df[source["qty_col"]]
            if source["exchange"] == "OKX" and sheet in OKX_CTVAL:
                df["qty"] = raw_qty * OKX_CTVAL[sheet]
            else:
                df["qty"] = raw_qty

            df["notional"] = df["qty"] * df[source["px_col"]]
            df["hour_bucket"] = df["dt"].dt.floor("h")
            df["min_bucket"]  = df["dt"].dt.floor("min")

            items.append({
                "exchange":   source["exchange"],
                "instrument": f"{source['exchange']}|{sheet}",  # 唯一名
                "df":         df[["dt", "qty", "notional", "hour_bucket", "min_bucket"]],
            })

    return items


# ── 分桶聚合 ──────────────────────────────────────────────────────────────────

def bucket_pivot(items: list[dict], bucket_col: str,
                 value_col: str, agg_func) -> pd.DataFrame:
    """
    对所有 items 按 bucket_col 分组，对 value_col 应用 agg_func，
    返回 pivot：行=bucket，列=instrument。
    """
    frames = []
    for item in items:
        s = item["df"].groupby(bucket_col)[value_col].agg(agg_func).rename(item["instrument"])
        frames.append(s)
    if not frames:
        return pd.DataFrame()
    pivot = pd.concat(frames, axis=1, sort=True).sort_index()
    return pivot


def fmt_hour_index(pivot: pd.DataFrame) -> pd.DataFrame:
    """将 hour_bucket 索引格式化为 'MM-DD HH:MM~HH:MM'。"""
    p = pivot.copy()
    p.index = [
        f"{t.strftime('%m-%d %H:%M')}~{(t + timedelta(hours=1)).strftime('%H:%M')}"
        for t in p.index
    ]
    p.index.name = "时间段(小时)"
    return p


def fmt_min_index(pivot: pd.DataFrame) -> pd.DataFrame:
    """将 min_bucket 索引格式化为 'MM-DD HH:MM'。"""
    p = pivot.copy()
    p.index = [t.strftime("%m-%d %H:%M") for t in p.index]
    p.index.name = "时间段(分钟)"
    return p


# ── 打印 ──────────────────────────────────────────────────────────────────────

def print_pivot(pivot: pd.DataFrame, title: str, float_fmt: str = ".4f",
                max_rows: int = 20) -> None:
    if pivot.empty:
        print(f"\n[{title}] 无数据")
        return

    display = pivot.head(max_rows).copy().astype(object)
    for col in pivot.columns:
        display[col] = pivot.head(max_rows)[col].apply(
            lambda x: format(x, float_fmt) if pd.notna(x) else "—"
        )

    total = len(pivot)
    print(f"\n{'═'*70}")
    print(f"  {title}  （共 {total} 行，显示前 {min(max_rows, total)} 行）")
    print(f"{'═'*70}")
    print(tabulate(display.reset_index(), headers="keys",
                   tablefmt="rounded_outline", showindex=False))
    if total > max_rows:
        print(f"  … 完整数据见 {OUTPUT_PATH}")


# ── 写 Excel ──────────────────────────────────────────────────────────────────

def write_sheet(writer: pd.ExcelWriter, pivot: pd.DataFrame, sheet: str) -> None:
    if pivot.empty:
        return
    pivot.reset_index().to_excel(writer, sheet_name=sheet, index=False)
    ws = writer.sheets[sheet]
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col) + 3
        ws.column_dimensions[col[0].column_letter].width = max(max_len, 13)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--tz", type=int, default=8, help="时区偏移（小时），默认 8")
    return p.parse_args()


def main() -> None:
    args      = parse_args()
    tz_label  = f"UTC+{args.tz}" if args.tz >= 0 else f"UTC{args.tz}"
    print(f"\n时区: {tz_label}\n")

    items = load_all(args.tz)
    if not items:
        print("无数据。")
        return

    print(f"已加载 {len(items)} 个合约")

    # ── ① 小时分桶中位数 ──────────────────────────────────────────────────────
    h_qty_median = fmt_hour_index(bucket_pivot(items, "hour_bucket", "qty",      "median"))
    h_usd_median = fmt_hour_index(bucket_pivot(items, "hour_bucket", "notional", "median"))

    print_pivot(h_qty_median, "① 小时分桶  单笔币量  中位数")
    print_pivot(h_usd_median, "① 小时分桶  单笔U量   中位数")

    # ── ② 分钟分桶：均值 & 95% 分位数 ────────────────────────────────────────
    p95 = lambda s: s.quantile(0.95)

    m_qty_mean = fmt_min_index(bucket_pivot(items, "min_bucket", "qty",      "mean"))
    m_qty_p95  = fmt_min_index(bucket_pivot(items, "min_bucket", "qty",      p95))
    m_usd_mean = fmt_min_index(bucket_pivot(items, "min_bucket", "notional", "mean"))
    m_usd_p95  = fmt_min_index(bucket_pivot(items, "min_bucket", "notional", p95))

    print_pivot(m_qty_mean, "② 分钟分桶  单笔币量  均值")
    print_pivot(m_qty_p95,  "② 分钟分桶  单笔币量  95% 分位数")
    print_pivot(m_usd_mean, "② 分钟分桶  单笔U量   均值")
    print_pivot(m_usd_p95,  "② 分钟分桶  单笔U量   95% 分位数")

    # ── 写 Excel（6 个 sheet）─────────────────────────────────────────────────
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        write_sheet(writer, h_qty_median, "小时_币量中位数")
        write_sheet(writer, h_usd_median, "小时_U量中位数")
        write_sheet(writer, m_qty_mean,   "分钟_币量均值")
        write_sheet(writer, m_qty_p95,    "分钟_币量P95")
        write_sheet(writer, m_usd_mean,   "分钟_U量均值")
        write_sheet(writer, m_usd_p95,    "分钟_U量P95")

    print(f"\n✓ 已保存至 {OUTPUT_PATH}（6 个 sheet）\n")


if __name__ == "__main__":
    main()
