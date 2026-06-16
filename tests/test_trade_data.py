import pandas as pd
from trade_data import to_notional, inst_for, okx_files_for


def test_to_notional_okx_swap_multiplies_ctval():
    raw = pd.DataFrame({"created_time": [1781136000000], "size": [2], "price": [60000.0]})
    out = to_notional("OKX", "BTC", "swap", raw)  # ctVal=0.01
    assert out["qty"].iloc[0] == 2 * 0.01
    assert out["notional"].iloc[0] == 2 * 0.01 * 60000.0


def test_to_notional_okx_spot_ignores_ctval():
    raw = pd.DataFrame({"created_time": [1781136000000], "size": [3], "price": [10.0]})
    out = to_notional("OKX", "LINK", "spot", raw)
    assert out["notional"].iloc[0] == 3 * 10.0


def test_to_notional_bybit_linear_ts_to_ms():
    raw = pd.DataFrame({"timestamp": [1781136000.5], "size": [4.0], "price": [2.0]})
    out = to_notional("Bybit", "XRP", "linear", raw)
    assert out["ts"].iloc[0] == 1781136000500
    assert out["notional"].iloc[0] == 8.0


def test_to_notional_bybit_spot_uses_volume_col():
    raw = pd.DataFrame({"timestamp": [1781136000000], "volume": [5.0], "price": [2.0]})
    out = to_notional("Bybit", "XRP", "spot", raw)
    assert out["notional"].iloc[0] == 10.0


def test_to_notional_binance_uses_quote_qty():
    raw = pd.DataFrame({"time": [1781136000000], "qty": [0.1], "price": [60000.0], "quote_qty": [5999.5]})
    out = to_notional("Binance", "BTC", "linear", raw)
    assert out["notional"].iloc[0] == 5999.5  # 直取 quote_qty，非 price*qty


def test_to_notional_empty_returns_none():
    assert to_notional("OKX", "BTC", "swap", pd.DataFrame()) is None
    assert to_notional("OKX", "BTC", "swap", None) is None


def test_inst_for():
    assert inst_for("OKX", "BTC", "swap") == "BTC-USDT-SWAP"
    assert inst_for("OKX", "BTC", "spot") == "BTC-USDT"
    assert inst_for("Bybit", "PEPE", "linear") == "1000PEPEUSDT"
    assert inst_for("Bybit", "BTC", "spot") == "BTCUSDT"
    assert inst_for("Binance", "ETH", "linear") == "ETHUSDT"


def test_okx_files_for_includes_nextday():
    assert okx_files_for(["2026-06-11"]) == ["2026-06-11", "2026-06-12"]


def test_build_notional_caches_and_skips_redownload(tmp_path, monkeypatch):
    import trade_data as td
    calls = {"n": 0}

    def fake_dl_bn(sym, date):
        calls["n"] += 1
        ms = int(pd.Timestamp(date + "T00:00:00Z").timestamp() * 1000)
        return pd.DataFrame({"time": [ms], "price": [10.0], "qty": [1.0], "quote_qty": [10.0]})

    monkeypatch.setattr(td, "dl_bn", fake_dl_bn)
    days = ["2026-06-13", "2026-06-14"]
    df1 = td.build_notional("Binance", "BTC", "linear", days, cache_dir=tmp_path)
    assert df1 is not None and len(df1) == 2
    assert calls["n"] == 2                       # 两天都下载

    df2 = td.build_notional("Binance", "BTC", "linear", days, cache_dir=tmp_path)
    assert calls["n"] == 2                       # 全缓存命中，零新下载
    assert len(df2) == 2

    df3 = td.build_notional("Binance", "BTC", "linear", days + ["2026-06-15"], cache_dir=tmp_path)
    assert calls["n"] == 3                       # 只新下 06-15
    assert len(df3) == 3

    # 缓存文件确实落盘
    assert (tmp_path / "Binance_BTCUSDT_linear.parquet").exists()
