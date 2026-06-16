# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas", "pyarrow", "tabulate"]
# ///
"""
逐笔成交「按天」统计（多日，从公开归档下载）。

范围（按所独立，见 KEEP_DAYS / COINS / TYPES）：
  OKX / Bybit：UTC 日 04-29 / 05-18 / 05-25 / 05-26；8 币 × {现货, 合约}。
  Binance    ：UTC 日 06-11 ~ 06-14；6 币（含 ETH/SOL）× {仅永续}。
每币每天统计：成交额(USDT)、笔数、单笔 P50/P75 notional、微单占比（仅合约）。
原始逐笔存盘到 data/daily/<exch>_<sheet>.parquet（不再算完即弃）；统计表存 data/daily_stats.csv。

归档（走代理 127.0.0.1:7890）：
  OKX CDN（按 UTC+8 日切）：trades/daily/{YYYYMMDD}/{inst}-trades-{date}.zip
  Bybit linear：public.bybit.com/trading/{sym}/{sym}{date}.csv.gz（按 UTC 日）
  Bybit spot  ：public.bybit.com/spot/{sym}/{sym}_{date}.csv.gz
  Binance perp（按 UTC 日）：data.binance.vision/data/futures/um/daily/trades/{sym}/{sym}-trades-{date}.zip
notional：OKX 永续 = size(张)×ctVal×price；Binance = quote_qty（归档直给）；其余 = 币量×price。
微单：合约 size==当日最小 size 视为微单；现货不计。
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd
from tabulate import tabulate
from trade_data import dl_okx, dl_by, dl_bn, okx_files_for, BYLIN, OKX_CTVAL

# 各所要统计的 UTC 日（增量累积，按所独立）
KEEP_DAYS = {
    "OKX":     ["2026-04-29", "2026-05-18", "2026-05-25", "2026-05-26"],
    "Bybit":   ["2026-04-29", "2026-05-18", "2026-05-25", "2026-05-26"],
    "Binance": ["2026-06-11", "2026-06-12", "2026-06-13", "2026-06-14"],
}

def by_files_for(days):  return sorted(days)
# 各所币种（Binance 仅 6 币、含新增 ETH/SOL，且只做永续）
COINS = {
    "OKX":     ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"],
    "Bybit":   ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"],
    "Binance": ["BTC", "ETH", "SOL", "XRP", "LINK", "DOGE"],
}
TYPES = {"OKX": ("spot", "swap"), "Bybit": ("spot", "linear"), "Binance": ("linear",)}
PCTS = [50, 75, 80, 85, 90, 95]   # 单笔 notional 分位
OUTDIR = Path("data/daily"); OUTDIR.mkdir(parents=True, exist_ok=True)


def build(exch, coin, typ):
    """增量累积：已有的 UTC 日跳过，只下缺的天，合并存盘。"""
    is_contract = typ in ("swap", "linear")
    if exch == "OKX":
        inst = f"{coin}-USDT-SWAP" if is_contract else f"{coin}-USDT"
        sheet = inst
    elif exch == "Bybit":
        sym = BYLIN.get(coin, f"{coin}USDT") if is_contract else f"{coin}USDT"
        sheet = f"{sym}_{'linear' if is_contract else 'spot'}"
    else:  # Binance（只做永续）
        sym = f"{coin}USDT"
        sheet = f"{sym}_linear"
    outp = OUTDIR / f"{exch}_{sheet}.parquet"
    existing = pd.read_parquet(outp) if outp.exists() else None
    have = set(existing["day"].unique()) if existing is not None else set()
    need = [d for d in KEEP_DAYS[exch] if d not in have]
    if not need:
        return sheet, existing

    frames = []
    if exch == "OKX":
        for d in okx_files_for(need):
            raw = dl_okx(inst, d)
            if raw is None or raw.empty: continue
            ts = pd.to_numeric(raw["created_time"]).astype("int64")
            sz = pd.to_numeric(raw["size"]); px = pd.to_numeric(raw["price"])
            qty = sz * OKX_CTVAL[coin] if is_contract else sz
            frames.append(pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": qty, "notional": qty * px}))
    elif exch == "Bybit":
        for d in by_files_for(need):
            raw = dl_by("linear" if is_contract else "spot", sym, d)
            if raw is None or raw.empty: continue
            if is_contract:
                ts = (pd.to_numeric(raw["timestamp"]) * 1000).round().astype("int64"); sz = pd.to_numeric(raw["size"])
            else:
                ts = pd.to_numeric(raw["timestamp"]).astype("int64"); sz = pd.to_numeric(raw["volume"])
            px = pd.to_numeric(raw["price"])
            frames.append(pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": sz * px}))
    else:  # Binance 永续：quote_qty 即归档直给的 USDT notional，按 UTC 日切（无需拼接）
        for d in by_files_for(need):
            raw = dl_bn(sym, d)
            if raw is None or raw.empty: continue
            ts = pd.to_numeric(raw["time"]).astype("int64")
            px = pd.to_numeric(raw["price"]); sz = pd.to_numeric(raw["qty"])
            frames.append(pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": pd.to_numeric(raw["quote_qty"])}))
    if not frames:
        return (sheet, existing)
    new = pd.concat(frames, ignore_index=True).dropna()
    new["day"] = pd.to_datetime(new.ts, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    new = new[new.day.isin(need)]
    df = pd.concat([existing, new], ignore_index=True) if existing is not None else new
    df = df.sort_values("ts").reset_index(drop=True)
    df.to_parquet(outp, index=False, compression="snappy")
    return sheet, df


def main():
    tasks = [(e, c, t) for e in ("OKX", "Bybit", "Binance") for c in COINS[e] for t in TYPES[e]]
    rows = []
    def run(task):
        e, c, t = task
        try:
            sheet, df = build(e, c, t)
            if df is None: return None
            out = []
            for day, g in df.groupby("day"):
                micro = (g.size_raw == g.size_raw.min()).mean() if t in ("swap", "linear") else None
                rec = {"所": e, "币": c, "类型": "合约" if t in ("swap", "linear") else "现货", "日": day[5:],
                       "成交额": g.notional.sum(), "笔数": len(g)}
                for p in PCTS:
                    rec[f"P{p}单笔"] = g.notional.quantile(p / 100)
                rec["微单占比"] = micro
                out.append(rec)
            print(f"  ✓ {e} {c} {t}: {len(df):,} 笔", flush=True)
            return out
        except Exception as ex:
            print(f"  ✗ {e} {c} {t}: {repr(ex)[:70]}", flush=True); return None

    print(f"下载+统计 {len(tasks)} 个序列（OKX/Bybit 各 8 币现货+合约 + Binance 6 币永续）...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(run, tasks):
            if r: rows.extend(r)

    st = pd.DataFrame(rows).sort_values(["所", "币", "类型", "日"])
    st.to_csv("data/daily_stats.csv", index=False)

    def fu(v): return f"${v/1e6:.1f}M" if v >= 1e6 else (f"${v/1e3:.0f}k" if v >= 1e3 else f"${v:.0f}")
    disp = st.copy()
    disp["成交额"] = disp["成交额"].map(fu)
    for p in PCTS:
        disp[f"P{p}单笔"] = disp[f"P{p}单笔"].map(fu)
    disp["笔数"] = disp["笔数"].map(lambda x: f"{x:,}")
    disp["微单占比"] = disp["微单占比"].map(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
    print("\n", tabulate(disp, headers="keys", tablefmt="rounded_outline", showindex=False))
    print(f"\n✓ 原始存 data/daily/，统计存 data/daily_stats.csv（{len(st)} 行）")


if __name__ == "__main__":
    main()
