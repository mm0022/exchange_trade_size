# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3"]
# ///
"""
盘口深度采集器（存真实 USDT 深度）。

每隔 --interval 秒并发轮询 OKX + Bybit 共 32 个合约（8 币 × 两所 × 现货/永续）的
实时盘口，计算「mid ±N bps 内可吃量(USDT)」买卖两侧，增量写 CSV（原始盘口不存）。

✅ OKX 永续盘口 size 是张数 → 采集时即 ×ctVal 存**真实 USDT**（修正旧版未乘 ctVal
   导致 PEPE 永续被四舍五入归零的 bug）。OKX 现货 + Bybit 本就是币量口径，不乘。
直连无代理；盘口只有实时快照、无历史，只能从当下往后采。

输出: data/depth_log.csv
  列: ts,exchange,inst,category,side,mid,spread_bps,d1..d20（dN=±Nbps 内可吃 USDT）

用法: uv run collect_depth.py --hours 8 --interval 5
"""
import argparse, csv, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import requests, urllib3
from trade_data import PROXY

urllib3.disable_warnings()

OKX_COINS = ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"]
BY_COINS  = ["AAVE", "ARB", "BTC", "DOGE", "LINK", "PEPE", "SUI", "XRP"]
BY_LIN    = {"PEPE": "1000PEPEUSDT"}
def bylin(c): return BY_LIN.get(c, f"{c}USDT")

# OKX 永续合约面值（张→币）：USDT 深度 = 张数 × ctVal × 价格
OKX_CTVAL = {"BTC-USDT-SWAP": 0.01, "SUI-USDT-SWAP": 1, "AAVE-USDT-SWAP": 0.1,
             "DOGE-USDT-SWAP": 1000, "LINK-USDT-SWAP": 1, "ARB-USDT-SWAP": 10,
             "XRP-USDT-SWAP": 100, "PEPE-USDT-SWAP": 1e7}

# (exchange, inst_label, category, fetch_kind, key)
INSTRUMENTS = []
for c in OKX_COINS:
    INSTRUMENTS += [("OKX", f"{c}-USDT", "spot", "okx", f"{c}-USDT"),
                    ("OKX", f"{c}-USDT-SWAP", "swap", "okx", f"{c}-USDT-SWAP")]
for c in BY_COINS:
    INSTRUMENTS += [("Bybit", f"{c}USDT_spot", "spot", "by", ("spot", f"{c}USDT")),
                    ("Bybit", f"{bylin(c)}_linear", "linear", "by", ("linear", bylin(c)))]

BPS = list(range(1, 21))   # 1..20
OUT = Path("data/depth_log.csv")
WORKERS = 8


def fetch_book(kind, key):
    if kind == "okx":
        r = requests.get("https://www.okx.com/api/v5/market/books",
                         params={"instId": key, "sz": 400}, timeout=12, verify=False, proxies=PROXY)
        d = r.json()["data"][0]
        return ([[float(p), float(s)] for p, s, *_ in d["bids"]],
                [[float(p), float(s)] for p, s, *_ in d["asks"]])
    cat, sym = key
    r = requests.get("https://api.bybit.com/v5/market/orderbook",
                     params={"category": cat, "symbol": sym, "limit": 200 if cat == "spot" else 500},
                     timeout=12, verify=False, proxies=PROXY)
    d = r.json()["result"]
    return ([[float(p), float(s)] for p, s in d["b"]],
            [[float(p), float(s)] for p, s in d["a"]])


def depth_within(levels, mid, bps, is_ask, scale):
    lim = mid * (1 + bps / 1e4) if is_ask else mid * (1 - bps / 1e4)
    tot = 0.0
    for p, s in levels:
        if (is_ask and p <= lim) or (not is_ask and p >= lim):
            tot += p * s
        else:
            break
    return tot * scale


def poll_one(spec):
    """返回两行（bid/ask）或 None。"""
    exch, inst, cat, kind, key = spec
    try:
        bids, asks = fetch_book(kind, key)
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2
        spread_bps = (asks[0][0] - bids[0][0]) / mid * 1e4
        scale = OKX_CTVAL[inst] if (exch == "OKX" and cat == "swap") else 1.0
        base = [exch, inst, cat]
        meta = [f"{mid:.8g}", f"{spread_bps:.3f}"]
        rb = base + ["bid"] + meta + [f"{depth_within(bids, mid, b, False, scale):.2f}" for b in BPS]
        ra = base + ["ask"] + meta + [f"{depth_within(asks, mid, b, True, scale):.2f}" for b in BPS]
        return (inst, rb, ra)
    except Exception as e:
        return ("ERR", inst, repr(e)[:80])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    new_file = not OUT.exists()
    f = open(OUT, "a", newline="")
    w = csv.writer(f)
    if new_file:
        w.writerow(["ts", "exchange", "inst", "category", "side", "mid", "spread_bps"]
                   + [f"d{b}" for b in BPS])
        f.flush()

    deadline = time.monotonic() + args.hours * 3600
    poll = 0
    print(f"开始采集 | {len(INSTRUMENTS)} 合约 | {args.interval}s/轮 | {args.hours}h | bps 1-20 | 真实USDT(已乘ctVal) → {OUT}", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while time.monotonic() < deadline:
            poll += 1
            t0 = time.monotonic()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ok = 0
            for res in ex.map(poll_one, INSTRUMENTS):
                if res is None:
                    continue
                if res[0] == "ERR":
                    print(f"  [{ts}] {res[1]} 失败: {res[2]}", flush=True); continue
                _, rb, ra = res
                w.writerow([ts] + rb); w.writerow([ts] + ra); ok += 1
            f.flush()
            if poll % 60 == 1:
                print(f"  poll#{poll} {ts}  成功 {ok}/{len(INSTRUMENTS)}", flush=True)
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    f.close()
    print(f"采集结束，共 {poll} 轮 → {OUT}", flush=True)


if __name__ == "__main__":
    main()
