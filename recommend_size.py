# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas", "tabulate"]
# ///
"""
资金费套利「固定单笔 + 节奏」推荐（逐币逐所）。

把三样合一：
  1) funding edge（INC=开仓 / DEC=平仓，bps，你提供）→ 定执行预算
  2) 盘口深度（data/depth_log.csv 的 d1..dN 冲击曲线）→ 定「单笔能多大」与成本
  3) 7 天小时成交量（K线，实时取）→ 定「多久打一笔」(节奏) 与可累积量

口径：
  · 固定单笔 Q = 使「往返执行成本 ≤ α×(INC+DEC)」的最大量；
    再不超过「每分钟成交量 × cap」（单笔不做成流量的一大口）。
  · 往返成本 = 开仓[slip(现货ask)+slip(永续bid)] + 平仓[slip(现货bid)+slip(永续ask)]，
    slip(side,Q) = 沿盘口冲击曲线吃到 $Q 时的 bps（现货腿通常是瓶颈，永续近乎免费）。
  · 节奏 T = Q / (POV × 每秒成交量)：每笔约占 POV 的流量，每 T 秒一笔。
  · 每小时可累积 ≈ POV × 小时成交量。

⚠️ funding 表需每轮临近时刷新（FUNDING 里改）；成交量用 7d 中位，淡时段(≈半量)单笔/节奏要相应缩。
   深度来自 collect_depth.py 采集的 data/depth_log.csv（已存真实 USDT）。
   注意 funding 与深度的币种/交易所要都齐才出结果（缺则跳过）。

用法: uv run recommend_size.py [--alpha 0.5] [--pov 0.10] [--cap 0.5]
"""
import argparse
import requests
import urllib3
import pandas as pd
from tabulate import tabulate
from trade_data import PROXY

urllib3.disable_warnings()
BPS = list(range(1, 21))

# 每轮刷新：(交易所, 币) -> (INC 开仓edge bps, DEC 平仓edge bps)
FUNDING = {
    ("Bybit", "AAVE"): (12.54, 3.37), ("Bybit", "ARB"): (15.56, 3.17),
    ("Bybit", "BTC"): (6.34, 3.54), ("Bybit", "DOGE"): (8.70, 2.23),
    ("Bybit", "LINK"): (9.47, 2.34), ("Bybit", "PEPE"): (10.52, 5.11),
    ("Bybit", "XRP"): (7.80, 2.55),
    ("OKX", "BTC"): (7.30, 2.19), ("OKX", "DOGE"): (8.89, 2.23),
    ("OKX", "LINK"): (11.40, -0.35), ("OKX", "XRP"): (7.84, 3.19),
}
BYLIN = {"PEPE": "1000PEPEUSDT"}


def venues():
    return [
        ("OKX",   lambda c: f"{c}-USDT",      lambda c: f"{c}-USDT-SWAP"),
        ("Bybit", lambda c: f"{c}USDT_spot",  lambda c: f"{BYLIN.get(c, c + 'USDT')}_linear"),
    ]


def load_depth(csv):
    df = pd.read_csv(csv)
    dcols = [f"d{b}" for b in BPS]
    cache = {}

    def cum(exch, inst, side):
        key = (exch, inst, side)
        if key not in cache:
            x = df[(df.exchange == exch) & (df.inst == inst) & (df.side == side)]
            cache[key] = None if x.empty else [x[c].median() for c in dcols]
        return cache[key]
    return cum


def slip(c, Q):
    """沿冲击曲线吃到 $Q 的 bps；c=各 bps 累计深度(USDT)。"""
    if Q <= 0 or c is None:
        return 0.0
    pb, pc = 0.0, 0.0
    for i, b in enumerate(BPS):
        if Q <= c[i]:
            return b if c[i] <= pc else pb + (Q - pc) / (c[i] - pc) * (b - pb)
        pb, pc = b, c[i]
    return 99.0


def hourly_vol(exch, coin, days):
    """7d(默认) 小时现货成交额(USDT) 中位。"""
    try:
        if exch == "OKX":
            r = requests.get("https://www.okx.com/api/v5/market/candles",
                             params={"instId": f"{coin}-USDT", "bar": "1H", "limit": min(days * 24, 300)},
                             proxies=PROXY, verify=False, timeout=15)
            v = [float(x[7]) for x in r.json()["data"]]
        else:
            r = requests.get("https://api.bybit.com/v5/market/kline",
                             params={"category": "spot", "symbol": f"{coin}USDT", "interval": 60, "limit": min(days * 24, 1000)},
                             proxies=PROXY, verify=False, timeout=15)
            v = [float(x[6]) for x in r.json()["result"]["list"]]
        return pd.Series(v).median() if v else None
    except Exception:
        return None


def fu(v):
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}k"
    return f"${v:.0f}"


def tstr(sec):
    if sec <= 0: return "—"
    return f"{sec/60:.0f}分" if sec >= 60 else f"{sec:.0f}秒"


def max_clip_by_cost(legs, budget):
    """二分找「往返成本 ≤ budget」的最大单笔。legs=(现货ask,永续bid,现货bid,永续ask)。"""
    sa, pb, sb, pa = legs
    series = [s for s in legs if s]
    if budget <= 0 or not series:
        return 0.0
    lo, hi = 0.0, min(s[-1] for s in series)
    for _ in range(45):
        m = (lo + hi) / 2
        cost = slip(sa, m) + slip(pb, m) + slip(sb, m) + slip(pa, m)
        if cost <= budget:
            lo = m
        else:
            hi = m
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=0.5, help="执行成本占总 edge 的比例上限")
    ap.add_argument("--pov", type=float, default=0.10, help="单笔目标参与率(占流量)")
    ap.add_argument("--cap", type=float, default=0.5, help="单笔上限=每分钟量×cap")
    ap.add_argument("--csv", default="data/depth_log.csv")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    cum = load_depth(args.csv)
    print(f"资金费套利 固定单笔+节奏 | α={args.alpha} POV={args.pov:.0%} cap={args.cap:.0%}/分钟 | 深度={args.csv}")
    for exch, sf, pf in venues():
        rows = []
        for (e, c), (inc, dec) in FUNDING.items():
            if e != exch:
                continue
            legs = (cum(exch, sf(c), "ask"), cum(exch, pf(c), "bid"),
                    cum(exch, sf(c), "bid"), cum(exch, pf(c), "ask"))
            if any(l is None for l in legs):
                continue
            Qd = max_clip_by_cost(legs, args.alpha * (inc + dec))
            hv = hourly_vol(exch, c, args.days)
            cap = args.cap * (hv / 60) if hv else Qd
            Q = min(Qd, cap)
            limited = "量限" if cap < Qd else "深度限"
            oc = slip(legs[0], Q) + slip(legs[1], Q)
            cc = slip(legs[2], Q) + slip(legs[3], Q)
            T = Q / (args.pov * hv / 3600) if (hv and Q > 0) else 0
            rows.append({"币": c, "固定单笔": fu(Q), "瓶颈": limited,
                         "开仓成本": f"{oc:.1f}bp", "平仓成本": f"{cc:.1f}bp",
                         "节奏(每隔)": tstr(T), "每小时可累积": fu(args.pov * hv) if hv else "—"})
        print(f"\n===== {exch} =====")
        print(tabulate(rows, headers="keys", tablefmt="rounded_outline"))


if __name__ == "__main__":
    main()
