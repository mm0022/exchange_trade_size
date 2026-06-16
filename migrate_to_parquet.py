# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "openpyxl", "pyarrow"]
# ///
"""
把现有 xlsx 数据迁移到 parquet（每合约一文件）。

Bybit:
    输入: data/bybit_trades.xlsx       列: ts/price/qty
    输出: data/bybit/<sheet>.parquet
OKX:
    输入: data/trades_old.xlsx         列: tradeId/ts/px/sz/side
    输出: data/okx/<sheet>.parquet
    说明: trades.xlsx 主文件已丢失、.bak/repaired 中央目录损坏，
          trades_old.xlsx 是 recover_okx.py 抢救出的 7 个存活合约，
          是目前唯一可读的 OKX 干净源。

每合约若目标 parquet 已存在且不旧于源文件则跳过（幂等，避免重复读大 xlsx）。
"""
from pathlib import Path
import pandas as pd


def _fresh(src: Path, dst: Path) -> bool:
    """目标已存在且不旧于源 → 视为已迁移。"""
    return dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime


def migrate(src: Path, dst_dir: Path, num_cols: list[str]) -> int:
    """把 src 各 sheet 迁到 dst_dir/<sheet>.parquet。num_cols 为需转 numeric 的列。"""
    if not src.exists():
        print(f"{src} 不存在，跳过")
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)

    xl = pd.ExcelFile(src)
    total = 0
    for sheet in xl.sheet_names:
        out = dst_dir / f"{sheet}.parquet"
        if _fresh(src, out):
            print(f"  {sheet:<22} 已存在，跳过")
            continue
        df = pd.read_excel(xl, sheet_name=sheet)
        if df.empty:
            print(f"  {sheet}: 空，跳过")
            continue
        for c in num_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["ts"] = df["ts"].astype("int64")
        df = df.dropna(subset=[c for c in num_cols if c in df.columns]) \
               .sort_values("ts").reset_index(drop=True)
        df.to_parquet(out, index=False, compression="snappy")
        print(f"  {sheet:<22} {len(df):>9,} 行 → {out} ({out.stat().st_size/1024:.0f} KB)")
        total += len(df)
    return total


def main():
    print("== Bybit ==")
    nb = migrate(Path("data/bybit_trades.xlsx"), Path("data/bybit"),
                 num_cols=["ts", "price", "qty"])
    print(f"  小计 {nb:,} 行\n")

    print("== OKX ==")
    no = migrate(Path("data/trades_old.xlsx"), Path("data/okx"),
                 num_cols=["tradeId", "ts", "px", "sz"])
    print(f"  小计 {no:,} 行\n")

    print(f"✓ 迁移完毕，共 {nb + no:,} 行")


if __name__ == "__main__":
    main()
