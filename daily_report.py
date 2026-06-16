# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas", "numpy", "pyarrow", "tabulate"]
# ///
"""每日单笔报单量报告：最近3个已发布UTC日 → P50/75/90(3天中位)+POV(P75) → Slack + JSON 存档。
用法: SLACK_WEBHOOK_URL=... uv run daily_report.py
口径=trade_data 的原始·全部（每笔 notional 分位，不聚合不去微）。"""
import argparse, datetime as dt, json, os, sys
from pathlib import Path
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


def median_daily_percentiles(df, pcts=PCTS):
    """df 有 day, notional 列。每天算各分位，再对天取中位数。返回 {pct: float}。"""
    out = {}
    for p in pcts:
        per_day = df.groupby("day")["notional"].quantile(p / 100.0)
        out[p] = float(np.median(per_day.values))
    return out


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
    """该 instrument 在 days(精确天集合) 内 1H quoteVolume(USDT) 的中位。
    按天集合过滤（windows 可能不连续，避免把缺口天的 K 线并进来）。拉取失败/无数据返回 None。"""
    is_contract = typ in ("swap", "linear")
    try:
        if exch == "OKX":
            pairs = _okx_hourly(inst_for("OKX", coin, typ), days)
        elif exch == "Bybit":
            pairs = _bybit_hourly("linear" if is_contract else "spot", inst_for("Bybit", coin, typ), days)
        else:
            pairs = _binance_hourly(inst_for("Binance", coin, typ), days)
    except Exception as ex:
        print(f"  · hourly_median {exch} {coin} {typ} K线拉取失败: {repr(ex)[:80]}", file=sys.stderr, flush=True)
        return None
    dayset = set(days)
    vols = [v for ts, v in pairs
            if pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%d") in dayset]
    return float(np.median(vols)) if vols else None


def append_archive(path, run_date, entry):
    """单 JSON 文件，按 run_date 为顶层 key 追加；同日覆盖。"""
    p = Path(path)
    data = json.loads(p.read_text()) if p.exists() else {}
    data[run_date] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


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
