# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas",
#   "openpyxl",
#   "tabulate",
# ]
# ///
"""
计算各币对逐笔成交的时间窗口中位数（两版）

  版本 A：币量中位数  —— 单笔成交的数量（BTC/DOGE/...）中位数
  版本 B：U量中位数   —— 单笔成交额（qty × price，USDT）中位数

读取:
    data/trades.xlsx       (OKX,   字段: sz / px)
    data/bybit_trades.xlsx (Bybit, 字段: qty / price)

输出:
    - 终端打印两张汇总表格
    - data/median_summary.xlsx（两个 sheet）

用法:
    uv run calc_median.py
    uv run calc_median.py --windows 1 2 3 4
"""

import argparse
from datetime import datetime, timezone
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

OUTPUT_PATH = Path("data/median_summary.xlsx")

# OKX SWAP 合约面值：sz（张数）× ctVal = 真实币量
OKX_CTVAL: dict[str, float] = {
    "BTC-USDT-SWAP":  0.01,
    "DOGE-USDT-SWAP": 1000.0,
    "AAVE-USDT-SWAP": 0.1,
    "LINK-USDT-SWAP": 1.0,
    "SUI-USDT-SWAP":  1.0,
}


# ── 核心计算 ──────────────────────────────────────────────────────────────────

def prepare(df: pd.DataFrame, qty_col: str, px_col: str,
            ts_col: str, sheet: str = "", exchange: str = "") -> pd.DataFrame:
    """清洗数值、计算 notional，返回含 [ts, qty, notional] 的 DataFrame。"""
    work = df[[ts_col, qty_col, px_col]].copy()
    work[ts_col]  = pd.to_numeric(work[ts_col],  errors="coerce")
    work[qty_col] = pd.to_numeric(work[qty_col], errors="coerce")
    work[px_col]  = pd.to_numeric(work[px_col],  errors="coerce")
    work = work.dropna()
    # OKX SWAP：sz 是张数，× ctVal 换算为真实币量
    if exchange == "OKX" and sheet in OKX_CTVAL:
        work[qty_col] = work[qty_col] * OKX_CTVAL[sheet]
    work["notional"] = work[qty_col] * work[px_col]
    work = work.rename(columns={ts_col: "ts", qty_col: "qty"})
    return work[["ts", "qty", "notional"]]


def window_median(work: pd.DataFrame, col: str,
                  windows_h: list[int], now_ms: int) -> dict:
    result = {}
    for h in windows_h:
        cutoff = now_ms - h * 3600 * 1000
        subset = work.loc[work["ts"] >= cutoff, col]
        label  = f"{h}h"
        result[label] = round(float(subset.median()), 6) if not subset.empty else None
    return result


def load_source(source: dict) -> list[dict]:
    path = source["path"]
    if not path.exists():
        print(f"[警告] 文件不存在，跳过: {path}")
        return []
    xl   = pd.ExcelFile(path)
    rows = []
    for sheet in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=sheet)
        if df.empty:
            continue
        rows.append({"instrument": sheet, "df": df})
    return rows


# ── 入口 ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--windows", nargs="+", type=int, default=[1, 2, 3],
                   metavar="H", help="时间窗口（小时），默认 1 2 3")
    return p.parse_args()


def make_table(records: list[dict], windows_h: list[int],
               value_col: str, unit: str) -> pd.DataFrame:
    """把 records 整理成 DataFrame，value_col 为 'qty' 或 'notional'。"""
    rows = []
    for r in records:
        row = {"交易所": r["exchange"], "合约": r["instrument"]}
        for h in windows_h:
            label = f"{h}h"
            val   = r[value_col].get(label)
            row[f"{h}h中位数({unit})"] = val if val is not None else "—"
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    args      = parse_args()
    windows_h = sorted(args.windows)
    now_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n计算基准时间: {now_str}   |   时间窗口: {windows_h}h\n")

    records = []
    for source in SOURCES:
        for item in load_source(source):
            try:
                work = prepare(item["df"],
                               source["qty_col"], source["px_col"], source["ts_col"],
                               sheet=item["instrument"], exchange=source["exchange"])
            except KeyError as e:
                print(f"[警告] {source['exchange']} / {item['instrument']} 缺少字段 {e}，跳过")
                continue

            records.append({
                "exchange":   source["exchange"],
                "instrument": item["instrument"],
                "qty":        window_median(work, "qty",      windows_h, now_ms),
                "notional":   window_median(work, "notional", windows_h, now_ms),
            })

    if not records:
        print("无数据。")
        return

    df_qty = make_table(records, windows_h, "qty",      "币量")
    df_usd = make_table(records, windows_h, "notional", "U")

    # ── 终端打印 ──────────────────────────────────────────────────────────────
    print("═" * 60)
    print("  版本 A：单笔成交 币量 中位数")
    print("═" * 60)
    print(tabulate(df_qty, headers="keys", tablefmt="rounded_outline",
                   showindex=False, floatfmt=".6f"))

    print()
    print("═" * 60)
    print("  版本 B：单笔成交 U量（USDT） 中位数")
    print("═" * 60)
    print(tabulate(df_usd, headers="keys", tablefmt="rounded_outline",
                   showindex=False, floatfmt=".4f"))

    # ── 写 Excel ─────────────────────────────────────────────────────────────
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        for df, sheet in [(df_qty, "币量中位数"), (df_usd, "U量中位数")]:
            df.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.sheets[sheet]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col) + 4
                ws.column_dimensions[col[0].column_letter].width = max_len

    print(f"\n✓ 已保存至 {OUTPUT_PATH}（两个 sheet：币量中位数 / U量中位数）\n")


if __name__ == "__main__":
    main()
