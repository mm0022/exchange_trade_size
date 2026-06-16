# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "tabulate"]
# ///
"""
盘口深度分析（资金费套利口径，同交易所）。

策略：多现货 + 空永续（delta 中性，吃资金费）。
  开仓 = 买现货(吃现货 ask) + 卖永续(吃永续 bid) → 单笔 ≤ min(现货ask, 永续bid)
  平仓 = 卖现货(吃现货 bid) + 买永续(吃永续 ask) → 单笔 ≤ min(现货bid, 永续ask)
现货腿通常远薄于永续 → 套利单笔上限由现货决定。

数据来自 data/depth_log.csv（collect_depth.py 采集，dN=±Nbps 内可吃量 USDT）。
采集器已在源头对 OKX 永续乘 ctVal 存真实 USDT，本脚本直接用、无需再修正。

输出每币每所：开仓/平仓单笔上限（整晚中位 + P25 保守值）+ 瓶颈腿。
用法: uv run analyze_depth.py --bps 3
"""
import argparse
import pandas as pd
from tabulate import tabulate

COINS = ["BTC", "SUI", "AAVE", "DOGE", "LINK", "ARB", "PEPE", "XRP"]
BYLIN = {"PEPE": "1000PEPEUSDT"}

# (exchange, 现货 inst, 永续 inst)
def venues():
    return [
        ("OKX",   lambda c: f"{c}-USDT",      lambda c: f"{c}-USDT-SWAP"),
        ("Bybit", lambda c: f"{c}USDT_spot",  lambda c: f"{BYLIN.get(c, c + 'USDT')}_linear"),
    ]


def fu(v):
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.0f}k"
    return f"${v:.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bps", type=int, default=3, help="每条腿的价差容忍档(bps)")
    ap.add_argument("--csv", default="data/depth_log.csv")
    args = ap.parse_args()
    col = f"d{args.bps}"

    df = pd.read_csv(args.csv)  # 采集器已存真实 USDT（OKX 永续已乘 ctVal）

    def leg(exch, inst, side):
        x = df[(df.exchange == exch) & (df.inst == inst) & (df.side == side)][["ts", col]]
        return x.rename(columns={col: f"{side}"})

    print(f"资金费套利单笔上限（多现货+空永续，同所）| 每腿 ±{args.bps}bps | {df.ts.nunique()} 次采样")
    for exch, spotf, perpf in venues():
        rows = []
        for c in COINS:
            sa = leg(exch, spotf(c), "ask").rename(columns={"ask": "s_ask"})
            sb = leg(exch, spotf(c), "bid").rename(columns={"bid": "s_bid"})
            pb = leg(exch, perpf(c), "bid").rename(columns={"bid": "p_bid"})
            pa = leg(exch, perpf(c), "ask").rename(columns={"ask": "p_ask"})
            m = sa.merge(sb, on="ts").merge(pb, on="ts").merge(pa, on="ts")
            if m.empty:
                continue
            m["open"] = m[["s_ask", "p_bid"]].min(axis=1)   # 开仓
            m["close"] = m[["s_bid", "p_ask"]].min(axis=1)  # 平仓
            bottleneck = "现货" if m.s_ask.median() < m.p_bid.median() else "永续"
            rows.append({
                "币": c, "瓶颈腿": bottleneck,
                "开仓中位": fu(m["open"].median()), "开仓P25": fu(m["open"].quantile(.25)),
                "平仓中位": fu(m["close"].median()), "平仓P25": fu(m["close"].quantile(.25)),
                "建议单笔": fu(min(m["open"].quantile(.25), m["close"].quantile(.25))),
            })
        print(f"\n===== {exch} =====")
        print(tabulate(rows, headers="keys", tablefmt="rounded_outline"))


if __name__ == "__main__":
    main()
