# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas",
#   "openpyxl",
#   "tabulate",
# ]
# ///
"""
OKX 原始逐笔 vs 同秒同价聚合后 中位数对比

处理逻辑：
  原始：每条 API 记录为一笔成交
  聚合：同一秒内价格相同的多笔成交合并为一笔（qty 求和）

两种口径都计算 1h / 2h / 3h 的中位数（币量 & U量），放同一张表对比。

输出：
  终端打印 + data/agg_compare.xlsx
"""

import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
from tabulate import tabulate

# ── 配置 ─────────────────────────────────────────────────────────────────────

EXCEL_PATH  = Path("data/trades.xlsx")
OUTPUT_PATH = Path("data/agg_compare.xlsx")

# OKX SWAP 合约面值：sz（张数）× ctVal = 真实币量
OKX_CTVAL: dict[str, float] = {
    "BTC-USDT-SWAP":  0.01,
    "DOGE-USDT-SWAP": 1000.0,
    "AAVE-USDT-SWAP": 0.1,
    "LINK-USDT-SWAP": 1.0,
    "SUI-USDT-SWAP":  1.0,
}

WINDOWS_H = [1, 2, 3]


# ── 数据加载 & 预处理 ─────────────────────────────────────────────────────────

def load_okx(sheet: str) -> pd.DataFrame:
    """读取 OKX 某合约，返回含 [ts, qty, px, notional] 的 DataFrame。"""
    df = pd.read_excel(EXCEL_PATH, sheet_name=sheet)
    if df.empty:
        return df

    df["ts"]  = pd.to_numeric(df["ts"],  errors="coerce")
    df["sz"]  = pd.to_numeric(df["sz"],  errors="coerce")
    df["px"]  = pd.to_numeric(df["px"],  errors="coerce")
    df = df.dropna(subset=["ts", "sz", "px"])

    # SWAP 合约张数 → 真实币量
    ctval = OKX_CTVAL.get(sheet, 1.0)
    df["qty"] = df["sz"] * ctval
    df["notional"] = df["qty"] * df["px"]
    return df[["ts", "qty", "px", "notional"]]


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """
    同秒（ts // 1000）、同价格（px）的成交聚合为一笔：
      qty      = sum（数量相加）
      notional = sum（成交额相加，等价于 agg_qty × px）
    """
    df = df.copy()
    df["sec"] = (df["ts"] // 1000).astype("int64")
    agg = (
        df.groupby(["sec", "px"], as_index=False)
        .agg(ts=("ts", "first"), qty=("qty", "sum"), notional=("notional", "sum"))
    )
    return agg[["ts", "qty", "px", "notional"]]


# ── 中位数计算 ────────────────────────────────────────────────────────────────

def window_medians(df: pd.DataFrame, windows_h: list[int],
                   now_ms: int) -> dict[str, dict]:
    """返回 {窗口: {qty_med, usd_med}}。"""
    result = {}
    for h in windows_h:
        cutoff = now_ms - h * 3600 * 1000
        sub = df[df["ts"] >= cutoff]
        result[h] = {
            "qty_med": round(sub["qty"].median(),      6) if not sub.empty else None,
            "usd_med": round(sub["notional"].median(), 4) if not sub.empty else None,
        }
    return result


# ── 构建对比表 ────────────────────────────────────────────────────────────────

def build_table(sheets: list[str], now_ms: int,
                windows_h: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    返回两张表：
      df_qty  — 币量中位数对比
      df_usd  — U量中位数对比
    """
    qty_rows, usd_rows = [], []

    for sheet in sheets:
        df_raw = load_okx(sheet)
        if df_raw.empty:
            continue
        df_agg = aggregate(df_raw)

        med_raw = window_medians(df_raw, windows_h, now_ms)
        med_agg = window_medians(df_agg, windows_h, now_ms)

        raw_cnt = len(df_raw)
        agg_cnt = len(df_agg)
        ratio   = f"{agg_cnt/raw_cnt*100:.1f}%" if raw_cnt else "—"

        # 币量行
        qty_row = {"合约": sheet, "原始条数": raw_cnt,
                   "聚合后条数": agg_cnt, "压缩率": ratio}
        usd_row = {"合约": sheet, "原始条数": raw_cnt,
                   "聚合后条数": agg_cnt, "压缩率": ratio}

        for h in windows_h:
            r = med_raw[h]
            a = med_agg[h]
            qty_row[f"{h}h_原始(币)"] = r["qty_med"] if r["qty_med"] is not None else "—"
            qty_row[f"{h}h_聚合(币)"] = a["qty_med"] if a["qty_med"] is not None else "—"
            usd_row[f"{h}h_原始(U)"]  = r["usd_med"] if r["usd_med"] is not None else "—"
            usd_row[f"{h}h_聚合(U)"]  = a["usd_med"] if a["usd_med"] is not None else "—"

        qty_rows.append(qty_row)
        usd_rows.append(usd_row)

    return pd.DataFrame(qty_rows), pd.DataFrame(usd_rows)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not EXCEL_PATH.exists():
        print(f"文件不存在: {EXCEL_PATH}")
        return

    now_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n基准时间: {now_str}   窗口: {WINDOWS_H}h\n")

    xl     = pd.ExcelFile(EXCEL_PATH)
    sheets = xl.sheet_names

    df_qty, df_usd = build_table(sheets, now_ms, WINDOWS_H)

    # 终端打印
    for df, title in [(df_qty, "币量中位数对比（原始 vs 同秒同价聚合）"),
                      (df_usd, "U量中位数对比（原始 vs 同秒同价聚合）")]:
        print(f"\n{'═'*70}\n  {title}\n{'═'*70}")
        print(tabulate(df, headers="keys", tablefmt="rounded_outline",
                       showindex=False, floatfmt=".4f"))

    # 写 Excel
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        for df, sheet in [(df_qty, "币量中位数对比"), (df_usd, "U量中位数对比")]:
            df.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.sheets[sheet]
            for col in ws.columns:
                w = max(len(str(c.value or "")) for c in col) + 3
                ws.column_dimensions[col[0].column_letter].width = max(w, 12)

    print(f"\n✓ 已保存至 {OUTPUT_PATH}\n")


if __name__ == "__main__":
    main()
