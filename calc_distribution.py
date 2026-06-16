# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pandas",
#   "pyarrow",
#   "openpyxl",
#   "tabulate",
# ]
# ///
"""
OKX + Bybit 逐笔成交规模分布分析（全量数据）

对每个合约计算 4 种切片下的 P25/P50/P75/P90 分位数（USDT 口径）：
  ① 原始 · 全部
  ② 原始 · 去微单
  ③ 聚合 · 全部
  ④ 聚合 · 去微单

并输出：
  - 每个合约的 P25/P50/P75/P95/P99 分位数详细（sz / 币量 / USDT）
  - SWAP 合约微单占比统计
  - 推荐报单量参考表（聚合·去微单 P75，下限 $200）

聚合定义：同一秒内同价格的多笔成交合并为一笔（qty & notional 求和）
微单定义：
  - OKX SWAP：sz == 最小手（即 sz == 1 张）
  - Bybit linear：qty == 最小手
  - 现货：不做微单过滤（"去微单" = "全部"）

输出:
    终端打印 + data/distribution.xlsx + 生成 docs/trade_size_distribution.md

用法:
    uv run calc_distribution.py
"""

import argparse
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from tabulate import tabulate

# ── 配置 ─────────────────────────────────────────────────────────────────────

OUTPUT_XLSX = Path("data/distribution.xlsx")
OUTPUT_MD = Path("docs/trade_size_distribution.md")

# OKX SWAP 合约面值：sz（张数）× ctVal = 真实币量
OKX_CTVAL: dict[str, float] = {
    "BTC-USDT-SWAP":  0.01,
    "DOGE-USDT-SWAP": 1000.0,
    "AAVE-USDT-SWAP": 0.1,
    "LINK-USDT-SWAP": 1.0,
    "SUI-USDT-SWAP":  1.0,
    "ARB-USDT-SWAP":  10.0,
    "XRP-USDT-SWAP":  100.0,
    "PEPE-USDT-SWAP": 10_000_000.0,
}

SOURCES = [
    {
        "exchange": "OKX",
        "dir":      Path("data/okx"),
        "qty_col":  "sz",
        "px_col":   "px",
        "ts_col":   "ts",
        # SWAP 后缀 → 是合约；否则是现货
        "is_swap":  lambda s: s.endswith("-SWAP"),
        # 币种名提取
        "coin":     lambda s: s.split("-")[0],
        # 显示名
        "display":  lambda s: s,
    },
    {
        "exchange": "Bybit",
        "dir":      Path("data/bybit"),
        "qty_col":  "qty",
        "px_col":   "price",
        "ts_col":   "ts",
        "is_swap":  lambda s: s.endswith("_linear"),
        "coin":     lambda s: s.replace("USDT_spot", "").replace("USDT_linear", "").replace("1000PEPE", "PEPE"),
        "display":  lambda s: s,
    },
]

# 报单量下限
MIN_RECOMMEND_USDT = 200.0


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_items() -> list[dict]:
    """加载所有交易所所有合约，返回标准化 item 列表。"""
    items = []
    for src in SOURCES:
        if not src["dir"].exists():
            print(f"[警告] 目录不存在: {src['dir']}")
            continue
        files = sorted(src["dir"].glob("*.parquet"))
        if not files:
            print(f"[警告] 无 parquet 文件: {src['dir']}")
            continue
        for f in files:
            sheet = f.stem
            df = pd.read_parquet(f)
            if df.empty:
                continue

            df[src["ts_col"]]  = pd.to_numeric(df[src["ts_col"]],  errors="coerce")
            df[src["qty_col"]] = pd.to_numeric(df[src["qty_col"]], errors="coerce")
            df[src["px_col"]]  = pd.to_numeric(df[src["px_col"]],  errors="coerce")
            df = df.dropna(subset=[src["ts_col"], src["qty_col"], src["px_col"]])
            if df.empty:
                continue

            is_swap = src["is_swap"](sheet)
            # sz_raw = 原始 sz/qty 字段（OKX SWAP 为张数）
            sz_raw = df[src["qty_col"]].copy()
            # qty = 真实币量
            if src["exchange"] == "OKX" and is_swap and sheet in OKX_CTVAL:
                qty = sz_raw * OKX_CTVAL[sheet]
            else:
                qty = sz_raw.copy()
            px = df[src["px_col"]]
            ts = df[src["ts_col"]].astype("int64")

            work = pd.DataFrame({
                "ts":       ts,
                "sz_raw":   sz_raw,
                "qty":      qty,
                "px":       px,
                "notional": qty * px,
            })

            items.append({
                "exchange": src["exchange"],
                "sheet":    sheet,
                "is_swap":  is_swap,
                "coin":     src["coin"](sheet),
                "display":  f"{src['exchange']} {src['display'](sheet)}",
                "df":       work,
                "ts_min":   int(work["ts"].min()),
                "ts_max":   int(work["ts"].max()),
            })
    return items


# ── 聚合（同秒同价） ──────────────────────────────────────────────────────────

def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """同秒（ts // 1000）+ 同 px 合并：qty/notional 求和。"""
    df = df.copy()
    df["sec"] = (df["ts"] // 1000).astype("int64")
    agg = (
        df.groupby(["sec", "px"], as_index=False)
          .agg(ts=("ts", "first"),
               sz_raw=("sz_raw", "sum"),
               qty=("qty", "sum"),
               notional=("notional", "sum"))
    )
    return agg[["ts", "sz_raw", "qty", "px", "notional"]]


# ── 分位数 ────────────────────────────────────────────────────────────────────

def quantiles(s: pd.Series, qs=(0.25, 0.50, 0.75, 0.90)) -> dict:
    """返回 {p25: ..., p50: ..., ...}。空 series 返回 None。"""
    if s.empty:
        return {f"p{int(q*100)}": None for q in qs}
    return {f"p{int(q*100)}": float(s.quantile(q)) for q in qs}


def quantile_detail(s_sz, s_qty, s_usd, qs=(0.25, 0.50, 0.75, 0.95, 0.99)) -> dict:
    """返回 {p25: (sz, qty, usd), ...}"""
    out = {}
    for q in qs:
        key = f"p{int(q*100)}"
        if s_usd.empty:
            out[key] = (None, None, None)
        else:
            out[key] = (
                float(s_sz.quantile(q))  if not s_sz.empty else None,
                float(s_qty.quantile(q)) if not s_qty.empty else None,
                float(s_usd.quantile(q)) if not s_usd.empty else None,
            )
    return out


# ── 主分析 ────────────────────────────────────────────────────────────────────

def analyze(item: dict) -> dict:
    df_raw = item["df"]
    df_agg = aggregate(df_raw)

    is_swap = item["is_swap"]
    # 微单定义。语义：先在 raw 里剔除微单，再做聚合
    if is_swap:
        sz_min_raw = df_raw["sz_raw"].min()
        raw_no_micro = df_raw[df_raw["sz_raw"] > sz_min_raw]
        agg_no_micro = aggregate(raw_no_micro)
        micro_count_raw = int((df_raw["sz_raw"] == sz_min_raw).sum())
        micro_subset = df_raw[df_raw["sz_raw"] == sz_min_raw]
    else:
        # 现货：不做过滤
        sz_min_raw = None
        raw_no_micro = df_raw
        agg_no_micro = df_agg
        micro_count_raw = 0
        micro_subset = pd.DataFrame()

    cuts = {
        "原始·全部":   quantiles(df_raw["notional"]),
        "原始·去微单": quantiles(raw_no_micro["notional"]),
        "聚合·全部":   quantiles(df_agg["notional"]),
        "聚合·去微单": quantiles(agg_no_micro["notional"]),
    }
    counts = {
        "原始·全部":   len(df_raw),
        "原始·去微单": len(raw_no_micro),
        "聚合·全部":   len(df_agg),
        "聚合·去微单": len(agg_no_micro),
    }

    # 原始数据的 P25/P50/P75/P95/P99 sz/qty/usd 详细
    detail = quantile_detail(df_raw["sz_raw"], df_raw["qty"], df_raw["notional"])

    micro_info = None
    if is_swap and micro_count_raw > 0:
        micro_info = {
            "total":      len(df_raw),
            "micro_cnt":  micro_count_raw,
            "ratio":      micro_count_raw / len(df_raw),
            "sz_min":     sz_min_raw,
            "qty_min":    sz_min_raw * (OKX_CTVAL.get(item["sheet"], 1.0)
                                        if item["exchange"] == "OKX" else 1.0),
            "usd_median": float(micro_subset["notional"].median()),
            "usd_max":    float(micro_subset["notional"].max()),
        }

    return {
        "item": item,
        "cuts": cuts,
        "counts": counts,
        "detail": detail,
        "micro": micro_info,
        "agg_p75_no_micro": cuts["聚合·去微单"]["p75"],
    }


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def fmt_usd(v):
    if v is None:
        return "—"
    if v >= 10000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def fmt_num(v, digits=4):
    if v is None:
        return "—"
    if v == 0:
        return "0"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.{max(digits-2,0)}f}"
    return f"{v:.{digits}f}"


def time_range(items: list[dict], exchange: str) -> str:
    subs = [it for it in items if it["exchange"] == exchange]
    if not subs:
        return "无数据"
    ts_min = min(it["ts_min"] for it in subs)
    ts_max = max(it["ts_max"] for it in subs)
    dt_utc_min = pd.to_datetime(ts_min, unit="ms", utc=True)
    dt_utc_max = pd.to_datetime(ts_max, unit="ms", utc=True)
    dt_cst_min = dt_utc_min.tz_convert("Asia/Shanghai")
    dt_cst_max = dt_utc_max.tz_convert("Asia/Shanghai")
    dur = (dt_utc_max - dt_utc_min).total_seconds() / 3600
    return (f"{dt_utc_min.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{dt_utc_max.strftime('%Y-%m-%d %H:%M')} UTC "
            f"（北京 {dt_cst_min.strftime('%m-%d %H:%M')}~{dt_cst_max.strftime('%m-%d %H:%M')}，"
            f"{dur:.2f}h，{len(subs)} 合约）")


def _exch_window(items: list[dict], exchange: str) -> tuple[int, int] | None:
    subs = [it for it in items if it["exchange"] == exchange]
    if not subs:
        return None
    return min(it["ts_min"] for it in subs), max(it["ts_max"] for it in subs)


def build_markdown(results: list[dict], items: list[dict]) -> str:
    okx_range   = time_range(items, "OKX")
    bybit_range = time_range(items, "Bybit")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 两所窗口是否对齐（起止均在 1h 容差内）→ 决定横比注脚
    okx_w, by_w = _exch_window(items, "OKX"), _exch_window(items, "Bybit")
    aligned = bool(okx_w and by_w
                   and abs(okx_w[0] - by_w[0]) <= 3_600_000
                   and abs(okx_w[1] - by_w[1]) <= 3_600_000)

    lines = []
    lines.append("# OKX + Bybit 逐笔成交规模分布分析")
    lines.append("")
    lines.append(f"> 数据时间窗口：")
    lines.append(f"> - **OKX** `data/okx/`：{okx_range}")
    lines.append(f"> - **Bybit** `data/bybit/`：{bybit_range}")
    lines.append(f">")
    lines.append("> 微单定义：SWAP / linear 合约 sz == 最小手数（sz.min()）；现货不做微单过滤")
    lines.append("> 聚合方式：同一秒内同价格成交合并（qty & notional 求和）")
    lines.append(f"> 生成时间：{now_str}")
    lines.append("")
    lines.append("> OKX 10 个合约中 **BTC-USDT（现货）/ DOGE-USDT-SWAP / LINK-USDT-SWAP** 这 3 个"
                 "由 OKX CDN 日级成交归档补采（与 API 口径一致，已校验 sz 单位），窗口对齐其余合约。")
    lines.append("")
    if aligned:
        lines.append("> ✅ OKX 与 Bybit 已对齐到**同一时间窗口**（见上方），均为完整历史归档数据，"
                     "可直接横向比较。")
    else:
        lines.append("> ⚠️ 跨交易所横比有偏差：OKX 与 Bybit 两段数据采集时段不同（见上方时间窗口），"
                     "市场活跃度不一致。比较 OKX vs Bybit 数值时请保留这一点。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── 各合约详细 ────────────────────────────────────────────────────────────
    for r in results:
        it = r["item"]
        title = f"## {it['display']}（{r['counts']['原始·全部']:,} 笔）"
        lines.append(title)
        lines.append("")
        lines.append("| | 笔数 | P25 | P50 | P75 | P90 |")
        lines.append("|---|---|---|---|---|---|")
        for key in ["原始·全部", "原始·去微单", "聚合·全部", "聚合·去微单"]:
            q = r["cuts"][key]
            lines.append(
                f"| {key} | {r['counts'][key]:,} | "
                f"{fmt_usd(q['p25'])} | {fmt_usd(q['p50'])} | "
                f"{fmt_usd(q['p75'])} | {fmt_usd(q['p90'])} |"
            )
        lines.append("")

        # 分位数详细（基于原始数据）
        coin = it["coin"]
        lines.append(f"**分位数详细（P25/P50/P75/P95/P99，sz / {coin} 量 / USDT）**")
        lines.append("")
        lines.append(f"| 分位数 | sz | {coin} 量 | USDT |")
        lines.append("|---|---|---|---|")
        for q_key in ["p25", "p50", "p75", "p95", "p99"]:
            sz, qty, usd = r["detail"][q_key]
            lines.append(
                f"| {q_key.upper()} | {fmt_num(sz)} | {fmt_num(qty)} | {fmt_usd(usd)} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── 微单占比统计 ──────────────────────────────────────────────────────────
    micro_rows = [r for r in results if r["micro"]]
    if micro_rows:
        lines.append("## SWAP / linear 合约微单占比统计")
        lines.append("")
        lines.append("> 微单 = sz == 最小手数；现货不做此过滤")
        lines.append("")
        lines.append("| 合约 | 总笔数 | 微单笔数 | 微单占比 | 最小手 sz | 最小手 币量 | 微单中位 U | 微单最大 U |")
        lines.append("|---|---|---|---|---|---|---|---|")
        # 按微单占比降序
        for r in sorted(micro_rows, key=lambda x: -x["micro"]["ratio"]):
            it = r["item"]
            m = r["micro"]
            lines.append(
                f"| {it['display']} | {m['total']:,} | {m['micro_cnt']:,} | "
                f"**{m['ratio']*100:.1f}%** | {fmt_num(m['sz_min'])} | "
                f"{fmt_num(m['qty_min'])} {it['coin']} | "
                f"{fmt_usd(m['usd_median'])} | {fmt_usd(m['usd_max'])} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── 汇总对比（原始·全部 P50） ─────────────────────────────────────────────
    lines.append("## 汇总对比（原始·全部，P25/P50/P75/P90 USDT）")
    lines.append("")
    lines.append("| 合约 | 笔数 | P25 | P50 | P75 | P90 |")
    lines.append("|---|---|---|---|---|---|")
    # 按 P50 降序
    sorted_r = sorted(
        results,
        key=lambda r: -(r["cuts"]["原始·全部"]["p50"] or 0),
    )
    for r in sorted_r:
        q = r["cuts"]["原始·全部"]
        lines.append(
            f"| {r['item']['display']} | {r['counts']['原始·全部']:,} | "
            f"{fmt_usd(q['p25'])} | {fmt_usd(q['p50'])} | "
            f"{fmt_usd(q['p75'])} | {fmt_usd(q['p90'])} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── OKX vs Bybit 同币种对比（聚合·去微单 P75） ────────────────────────────
    by_key: dict[tuple, float | None] = {}
    for r in results:
        it = r["item"]
        by_key[(it["exchange"], it["coin"], it["is_swap"])] = r["agg_p75_no_micro"]
    okx_coins = {c for (e, c, s) in by_key if e == "OKX"}
    bybit_coins = {c for (e, c, s) in by_key if e == "Bybit"}
    shared = sorted(okx_coins & bybit_coins)
    if shared:
        lines.append("## OKX vs Bybit 同币种对比（聚合·去微单 P75，USDT）")
        lines.append("")
        lines.append("> 仅列两所都有的币种，同一时间窗口。Bybit/OKX = 倍数（>1 表示 Bybit 单笔更大）。")
        lines.append("")
        lines.append("| 币种 | 类型 | OKX | Bybit | Bybit/OKX |")
        lines.append("|---|---|---|---|---|")
        for coin in shared:
            for is_swap, label in [(False, "现货"), (True, "永续")]:
                o = by_key.get(("OKX", coin, is_swap))
                b = by_key.get(("Bybit", coin, is_swap))
                ratio = f"{b/o:.1f}×" if (o and b and o > 0) else "—"
                lines.append(f"| {coin} | {label} | {fmt_usd(o)} | {fmt_usd(b)} | {ratio} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── 推荐报单量参考（同币种现货 vs 合约取小，下限 $200） ────────────────────
    lines.append(f"## 推荐报单量参考（同币种 现货 vs 合约 取小，下限 ${MIN_RECOMMEND_USDT:.0f}）")
    lines.append("")
    lines.append("> 口径：聚合·去微单 P75（USDT）。同一交易所内同币种现货与合约两者取较小值，"
                 "再与 ${} 下限取较大值得到最终推荐。".format(int(MIN_RECOMMEND_USDT)))
    lines.append("")

    for exchange in ["OKX", "Bybit"]:
        rs = [r for r in results if r["item"]["exchange"] == exchange]
        if not rs:
            continue
        lines.append(f"### {exchange}")
        lines.append("")
        lines.append("| 币种 | 现货 聚合·去微单 P75 | 合约 聚合·去微单 P75 | 取小 | **最终推荐** |")
        lines.append("|---|---|---|---|---|")
        coins = sorted({r["item"]["coin"] for r in rs})
        for coin in coins:
            spot = next((r for r in rs if r["item"]["coin"] == coin and not r["item"]["is_swap"]), None)
            swap = next((r for r in rs if r["item"]["coin"] == coin and r["item"]["is_swap"]), None)
            spot_p75 = spot["agg_p75_no_micro"] if spot else None
            swap_p75 = swap["agg_p75_no_micro"] if swap else None
            vals = [v for v in [spot_p75, swap_p75] if v is not None]
            mn = min(vals) if vals else None
            if mn is None:
                final = None
                note = ""
            elif mn < MIN_RECOMMEND_USDT:
                final = MIN_RECOMMEND_USDT
                note = " ← 触底"
            else:
                final = mn
                note = ""
            lines.append(
                f"| {coin} | {fmt_usd(spot_p75)} | {fmt_usd(swap_p75)} | "
                f"{fmt_usd(mn)} | **{fmt_usd(final)}**{note} |"
            )
        lines.append("")

    return "\n".join(lines)


# ── 终端打印 ──────────────────────────────────────────────────────────────────

def print_summary(results: list[dict]) -> None:
    rows = []
    for r in results:
        it = r["item"]
        q = r["cuts"]["原始·全部"]
        rows.append({
            "交易所":    it["exchange"],
            "合约":      it["sheet"],
            "笔数":      f"{r['counts']['原始·全部']:,}",
            "P25":       fmt_usd(q["p25"]),
            "P50":       fmt_usd(q["p50"]),
            "P75":       fmt_usd(q["p75"]),
            "P90":       fmt_usd(q["p90"]),
            "聚合P75去微": fmt_usd(r["agg_p75_no_micro"]),
        })
    print()
    print(tabulate(rows, headers="keys", tablefmt="rounded_outline", showindex=False))


# ── Excel 输出 ────────────────────────────────────────────────────────────────

def write_xlsx(results: list[dict], path: Path) -> None:
    summary_rows = []
    detail_rows = []
    micro_rows = []
    for r in results:
        it = r["item"]
        base = {"交易所": it["exchange"], "合约": it["sheet"], "类型": "SWAP/Linear" if it["is_swap"] else "现货"}
        for key in ["原始·全部", "原始·去微单", "聚合·全部", "聚合·去微单"]:
            q = r["cuts"][key]
            summary_rows.append({
                **base,
                "切片": key,
                "笔数": r["counts"][key],
                "P25_USDT": q["p25"], "P50_USDT": q["p50"],
                "P75_USDT": q["p75"], "P90_USDT": q["p90"],
            })
        for q_key in ["p25", "p50", "p75", "p95", "p99"]:
            sz, qty, usd = r["detail"][q_key]
            detail_rows.append({
                **base,
                "分位": q_key.upper(),
                "sz": sz, f"币量": qty, "USDT": usd,
            })
        if r["micro"]:
            m = r["micro"]
            micro_rows.append({
                **base,
                "总笔数": m["total"],
                "微单笔数": m["micro_cnt"],
                "微单占比": m["ratio"],
                "最小手_sz": m["sz_min"],
                "最小手_币量": m["qty_min"],
                "微单中位U": m["usd_median"],
                "微单最大U": m["usd_max"],
            })

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="分位数_切片", index=False)
        pd.DataFrame(detail_rows).to_excel(writer, sheet_name="分位数_详细", index=False)
        if micro_rows:
            pd.DataFrame(micro_rows).to_excel(writer, sheet_name="微单占比", index=False)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    items = load_items()
    if not items:
        print("无数据")
        return

    print(f"已加载 {len(items)} 个合约，开始分析...")
    results = [analyze(it) for it in items]

    print_summary(results)

    write_xlsx(results, OUTPUT_XLSX)
    print(f"\n✓ 已保存 {OUTPUT_XLSX}")

    md = build_markdown(results, items)
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(md, encoding="utf-8")
    print(f"✓ 已生成 {OUTPUT_MD}")


if __name__ == "__main__":
    main()
