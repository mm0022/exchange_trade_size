# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "pyarrow", "requests", "urllib3", "tabulate"]
# ///
"""
多日「聚合·去微单 P75」摘要构建器（双模式）。

为推荐表提供稳健口径：对每个合约逐日重算聚合·去微单 P75，跨多日取中位
（避免单日撞极端值）。两种逐日切法对比：
  - session : 22:58~07:30 UTC（8.5h，与对齐分析同时段）
  - fullday : 整 UTC 自然日 00:00~24:00（24h）

- 复用 calc_distribution.analyze，口径与报告完全一致（含 OKX_CTVAL）。
- 一次下载（归档文件 05-25~05-30）同时算两种，原始数据用完即弃。
- 落地: data/multiday_p75.json
    {"session_dates":[...],"fullday_dates":[...],
     "session_p75":{sheet:[...]},"fullday_p75":{sheet:[...]}}

用法: uv run build_multiday_p75.py
"""
import gzip, io, json, importlib.util, zipfile
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests, urllib3, pandas as pd

urllib3.disable_warnings()
_spec = importlib.util.spec_from_file_location("cd", "calc_distribution.py")
cd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cd)

SESSION_DATES = ["2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
FULLDAY_DATES = ["2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
CAL_FILES     = ["2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-05-30"]

OKX_COINS = ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"]
BY_COINS  = ["AAVE", "ARB", "BTC", "DOGE", "LINK", "PEPE", "SUI", "XRP"]
BY_LIN    = {"PEPE": "1000PEPEUSDT"}
def bylin(c): return BY_LIN.get(c, f"{c}USDT")

# (sheet, kind, key, is_swap)
SPECS = []
for c in OKX_COINS:
    SPECS += [(f"{c}-USDT", "okx", f"{c}-USDT", False),
              (f"{c}-USDT-SWAP", "okx", f"{c}-USDT-SWAP", True)]
for c in BY_COINS:
    SPECS += [(f"{c}USDT_spot", "by_spot", f"{c}USDT", False),
              (f"{bylin(c)}_linear", "by_lin", bylin(c), True)]


def dl_day(kind, key, date):
    """下载单日归档 → DataFrame[ts(ms),sz_raw,px]；404 返回 None。"""
    if kind == "okx":
        url = f"https://www.okx.com/cdn/okex/traderecords/trades/daily/{date.replace('-','')}/{key}-trades-{date}.zip"
        r = requests.get(url, timeout=180, verify=False)
        if r.status_code == 404: return None
        r.raise_for_status(); raw = pd.read_csv(zipfile.ZipFile(io.BytesIO(r.content)).open(0))
        ts = pd.to_numeric(raw["created_time"]).astype("int64"); sz = pd.to_numeric(raw["size"]); px = pd.to_numeric(raw["price"])
    elif kind == "by_lin":
        url = f"https://public.bybit.com/trading/{key}/{key}{date}.csv.gz"
        r = requests.get(url, timeout=180, verify=False)
        if r.status_code == 404: return None
        r.raise_for_status(); raw = pd.read_csv(gzip.open(io.BytesIO(r.content)))
        ts = (pd.to_numeric(raw["timestamp"])*1000).round().astype("int64"); sz = pd.to_numeric(raw["size"]); px = pd.to_numeric(raw["price"])
    else:  # by_spot
        url = f"https://public.bybit.com/spot/{key}/{key}_{date}.csv.gz"
        r = requests.get(url, timeout=180, verify=False)
        if r.status_code == 404: return None
        r.raise_for_status(); raw = pd.read_csv(gzip.open(io.BytesIO(r.content)))
        ts = pd.to_numeric(raw["timestamp"]).astype("int64"); sz = pd.to_numeric(raw["volume"]); px = pd.to_numeric(raw["price"])
    return pd.DataFrame({"ts": ts, "sz_raw": sz.astype("float32"), "px": px.astype("float32")}).dropna()


def ms(date, hms): return int(dt.datetime.fromisoformat(f"{date}T{hms}").replace(tzinfo=dt.timezone.utc).timestamp()*1000)
def nextday(d): return (dt.date.fromisoformat(d)+dt.timedelta(days=1)).isoformat()


def p75(big, sheet, kind, is_swap, w0, w1):
    df = big[(big.ts >= w0) & (big.ts < w1)]
    if df.empty: return None
    sz, px = df["sz_raw"], df["px"]
    ctv = cd.OKX_CTVAL.get(sheet, 1.0) if kind == "okx" else 1.0
    qty = sz * ctv
    work = pd.DataFrame({"ts": df.ts.values, "sz_raw": sz.values, "qty": qty.values,
                         "px": px.values, "notional": (qty*px).values})
    item = {"df": work, "is_swap": is_swap, "sheet": sheet,
            "exchange": "OKX" if kind == "okx" else "Bybit"}
    return cd.analyze(item)["cuts"]["聚合·去微单"]["p75"]


def main():
    print(f"双模式多日 P75 | {len(SPECS)} 合约 | session×{len(SESSION_DATES)} + fullday×{len(FULLDAY_DATES)}", flush=True)
    sess_out, full_out = {}, {}
    for sheet, kind, key, is_swap in SPECS:
        with ThreadPoolExecutor(max_workers=6) as ex:
            dls = list(ex.map(lambda fd: dl_day(kind, key, fd), CAL_FILES))
        parts = [d for d in dls if d is not None and len(d)]
        if not parts:
            sess_out[sheet] = [None]*len(SESSION_DATES); full_out[sheet] = [None]*len(FULLDAY_DATES)
            print(f"  {sheet:<22} 无数据", flush=True); continue
        big = pd.concat(parts, ignore_index=True)
        sess_out[sheet] = [p75(big, sheet, kind, is_swap, ms(s, "22:58:21"), ms(nextday(s), "07:30:54")) for s in SESSION_DATES]
        full_out[sheet] = [p75(big, sheet, kind, is_swap, ms(d, "00:00:00"), ms(nextday(d), "00:00:00")) for d in FULLDAY_DATES]
        del big, parts
        sfmt = [f'{x:,.0f}' if x else '—' for x in sess_out[sheet]]
        print(f"  {sheet:<22} sess={sfmt}", flush=True)
    res = {"session_dates": SESSION_DATES, "fullday_dates": FULLDAY_DATES,
           "session_p75": sess_out, "fullday_p75": full_out}
    with open("data/multiday_p75.json", "w") as f:
        json.dump(res, f, ensure_ascii=False)
    print(f"\n✓ 写入 data/multiday_p75.json（{len(sess_out)} 合约 × 2 模式）", flush=True)


if __name__ == "__main__":
    main()
