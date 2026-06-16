import datetime as dt
import pandas as pd
from daily_report import resolve_window


def test_resolve_window_all_available():
    # 全部存在：从昨天往前取 3 个
    days = resolve_window(lambda ds: True, dt.date(2026, 6, 16), n=3)
    assert days == ["2026-06-13", "2026-06-14", "2026-06-15"]  # 升序，最新=昨天


def test_resolve_window_skips_missing():
    # 06-15 归档没出（如 T+1 还没发布），跳过往前凑
    missing = {"2026-06-15"}
    days = resolve_window(lambda ds: ds not in missing, dt.date(2026, 6, 16), n=3)
    assert days == ["2026-06-12", "2026-06-13", "2026-06-14"]


def test_resolve_window_insufficient_returns_partial():
    # 只有一天可用
    ok = {"2026-06-10"}
    days = resolve_window(lambda ds: ds in ok, dt.date(2026, 6, 16), n=3, max_back=8)
    assert days == ["2026-06-10"]


from daily_report import median_daily_percentiles


def test_median_daily_percentiles_takes_median_over_days():
    # 3 天，每天 notional 全等于该天常数 10/20/30 → 各分位每天=该常数 → 跨天中位=20
    df = pd.DataFrame({
        "day": ["2026-06-11"] * 3 + ["2026-06-12"] * 3 + ["2026-06-13"] * 3,
        "notional": [10, 10, 10, 20, 20, 20, 30, 30, 30],
    })
    out = median_daily_percentiles(df, pcts=(50, 75, 90))
    assert out[50] == 20.0
    assert out[75] == 20.0
    assert out[90] == 20.0


def test_median_daily_percentiles_returns_floats():
    df = pd.DataFrame({"day": ["2026-06-11", "2026-06-11"], "notional": [100, 300]})
    out = median_daily_percentiles(df, pcts=(50,))
    assert out[50] == 200.0
    assert isinstance(out[50], float)


from daily_report import pov


def test_pov_basic():
    # 300 单 × $1000 / 每小时 $30,000,000 = 0.01 (=1%)
    assert pov(1000.0, 30_000_000.0) == 300 * 1000.0 / 30_000_000.0


def test_pov_none_when_no_volume():
    assert pov(1000.0, None) is None
    assert pov(1000.0, 0) is None


import json
from daily_report import append_archive


def test_append_archive_writes_and_reads(tmp_path):
    p = tmp_path / "rep.json"
    entry = {"run_at_utc": "2026-06-16T00:00:05Z", "rows": [{"coin": "BTC", "p75": 415.0}]}
    append_archive(str(p), "2026-06-16", entry)
    data = json.loads(p.read_text())
    assert data["2026-06-16"]["rows"][0]["p75"] == 415.0


def test_append_archive_keeps_old_and_overwrites_same_day(tmp_path):
    p = tmp_path / "rep.json"
    append_archive(str(p), "2026-06-15", {"v": 1})
    append_archive(str(p), "2026-06-16", {"v": 2})
    append_archive(str(p), "2026-06-16", {"v": 3})  # 覆盖同日
    data = json.loads(p.read_text())
    assert data["2026-06-15"]["v"] == 1
    assert data["2026-06-16"]["v"] == 3
    assert len(data) == 2


from daily_report import format_report


def _row(exch, coin, typ, pov):
    return {"exch": exch, "coin": coin, "type": typ, "p50": 50.0, "p75": 415.0,
            "p90": 3380.0, "hourly_vol_median": 2.5e8, "pov_p75": pov}


def test_format_report_contains_sections_and_pov_percent():
    rows = [_row("OKX", "BTC", "合约", 0.000487), _row("Binance", "BTC", "永续", None)]
    windows = {"OKX": ["2026-06-13", "2026-06-14", "2026-06-15"], "Binance": ["2026-06-13", "2026-06-14", "2026-06-15"]}
    text = format_report("2026-06-16", windows, rows)
    assert "OKX" in text and "Binance" in text
    assert "BTC" in text
    assert "0.0487%" in text   # pov 小数 → 百分比展示
    assert "—" in text          # None POV 显示占位
    assert "```" in text        # 等宽 code block
