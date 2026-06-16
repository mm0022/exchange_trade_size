# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "openpyxl"]
# ///
"""
从损坏的 data/trades.xlsx 中扫描存活的 worksheet XML，恢复成新的可读 xlsx。

损坏原因：fetch_trades.py 写 xlsx 时未原子化，进程被 kill 后中央目录/元数据缺失。
存活 7 个 sheet，对应合约通过价格 + 时间戳特征识别。

用法:
    uv run recover_okx.py
"""

import re
import struct
import zlib
from pathlib import Path

import pandas as pd

SRC = Path("data/trades.xlsx.bak")   # 备份的损坏文件
DST = Path("data/trades.xlsx")       # 覆盖输出

SHEET_TO_INST = {
    "sheet1": "AAVE-USDT",
    "sheet2": "SUI-USDT",
    "sheet3": "LINK-USDT",
    "sheet4": "BTC-USDT-SWAP",
    "sheet5": "DOGE-USDT",
    "sheet6": "SUI-USDT-SWAP",
    "sheet7": "AAVE-USDT-SWAP",
}

ROW_RE = re.compile(r"<row [^>]*>(.*?)</row>", re.DOTALL)
NUM_CELL_RE = re.compile(r"<c [^>]*t=\"n\"[^>]*><v>([^<]+)</v></c>")
STR_CELL_RE = re.compile(r"<c [^>]*t=\"inlineStr\"[^>]*><is><t>([^<]*)</t></is></c>")


def extract_sheet(buf: bytes, sheet_key: str) -> pd.DataFrame:
    """从原始 xlsx 字节流里抽出指定 worksheet 的所有行。"""
    positions = [m.start() for m in re.finditer(b"PK\x03\x04", buf)]
    target = f"xl/worksheets/{sheet_key}.xml"

    for p in positions:
        fnlen = struct.unpack("<H", buf[p + 26:p + 28])[0]
        exlen = struct.unpack("<H", buf[p + 28:p + 30])[0]
        csize = struct.unpack("<I", buf[p + 18:p + 22])[0]
        name = buf[p + 30:p + 30 + fnlen].decode("utf-8")
        if name != target:
            continue
        data_start = p + 30 + fnlen + exlen
        raw = zlib.decompress(buf[data_start:data_start + csize], -15).decode("utf-8")

        rows_data = []
        for row_match in ROW_RE.finditer(raw):
            row_xml = row_match.group(1)
            # 提所有数字 cell + 所有 inline string cell，按 cell ref 排序
            cells = []
            for cm in re.finditer(
                r'<c r="([A-Z]+\d+)"[^>]*t="(n|inlineStr)"[^>]*>(.*?)</c>',
                row_xml,
                re.DOTALL,
            ):
                ref, ctype, inner = cm.group(1), cm.group(2), cm.group(3)
                col_letter = re.match(r"([A-Z]+)", ref).group(1)
                if ctype == "n":
                    v = re.search(r"<v>([^<]+)</v>", inner)
                    cells.append((col_letter, v.group(1) if v else None))
                else:  # inlineStr
                    t = re.search(r"<t[^>]*>([^<]*)</t>", inner)
                    cells.append((col_letter, t.group(1) if t else ""))
            rows_data.append(cells)

        if not rows_data:
            return pd.DataFrame()

        # 第一行是表头
        header = [v for _, v in rows_data[0]]
        body = []
        for row in rows_data[1:]:
            d = {col: val for col, val in row}
            body.append([d.get(letter) for letter in ["A", "B", "C", "D", "E"]])

        df = pd.DataFrame(body, columns=header)
        # 数值列转 numeric
        for c in ["tradeId", "ts", "px", "sz"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    return pd.DataFrame()


def main() -> None:
    if not SRC.exists():
        print(f"找不到备份文件 {SRC}")
        return

    buf = SRC.read_bytes()
    print(f"读取备份 {SRC} ({len(buf):,} 字节)")

    recovered = {}
    for sheet_key, inst in SHEET_TO_INST.items():
        df = extract_sheet(buf, sheet_key)
        if df.empty:
            print(f"  [跳过] {sheet_key} ({inst}) 无数据")
            continue
        ts_min = pd.to_datetime(df["ts"].min(), unit="ms", utc=True)
        ts_max = pd.to_datetime(df["ts"].max(), unit="ms", utc=True)
        dur = (ts_max - ts_min).total_seconds() / 3600
        print(f"  {inst:<18} {len(df):>7,} 行  "
              f"{ts_min.strftime('%m-%d %H:%M')} ~ {ts_max.strftime('%m-%d %H:%M')} UTC "
              f"({dur:.2f}h)")
        recovered[inst] = df

    if not recovered:
        print("无可恢复 sheet")
        return

    DST.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(DST, engine="openpyxl") as writer:
        for inst, df in recovered.items():
            df.to_excel(writer, sheet_name=inst, index=False)

    total = sum(len(df) for df in recovered.values())
    print(f"\n✓ 已恢复 {len(recovered)} 个 sheet / {total:,} 行 至 {DST}")


if __name__ == "__main__":
    main()
