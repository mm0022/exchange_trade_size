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


def test_same_sec_price_agg_merges_same_second_same_price():
    import trade_data as td
    # 两笔同秒(ts同整秒)同价 → 合并为一单 notional=300; 另一笔不同价单独成单
    df = pd.DataFrame({
        "ts":   [1781136000100, 1781136000900, 1781136000500],
        "price":[10.0, 10.0, 11.0],
        "notional":[100.0, 200.0, 55.0],
        "day": ["2026-06-13", "2026-06-13", "2026-06-13"],
    })
    out = td.same_sec_price_agg(df)
    vals = sorted(out["notional"].tolist())
    assert vals == [55.0, 300.0]
    assert set(out.columns) >= {"day", "notional"}


def test_same_sec_price_agg_separates_different_second():
    import trade_data as td
    df = pd.DataFrame({
        "ts":   [1781136000100, 1781136001100],   # 不同秒
        "price":[10.0, 10.0],
        "notional":[100.0, 200.0],
        "day": ["2026-06-13", "2026-06-13"],
    })
    out = td.same_sec_price_agg(df)
    assert sorted(out["notional"].tolist()) == [100.0, 200.0]


def test_agg_to_notional_price_times_qty():
    import trade_data as td
    raw = pd.DataFrame({"transact_time":[1781136000082], "price":[60000.0],
                        "quantity":[0.5], "is_buyer_maker":[True],
                        "agg_trade_id":[1],"first_trade_id":[1],"last_trade_id":[2]})
    out = td._agg_to_notional(raw)
    assert out["notional"].iloc[0] == 30000.0
    assert out["ts"].iloc[0] == 1781136000082
    assert set(out.columns) == {"ts","price","qty","notional","is_buyer_maker"}


def test_build_agg_binance_caches(tmp_path, monkeypatch):
    import trade_data as td
    calls = {"n": 0}
    def fake(sym, date):
        calls["n"] += 1
        ms = int(pd.Timestamp(date + "T00:00:00Z").timestamp()*1000)
        return pd.DataFrame({"transact_time":[ms],"price":[10.0],"quantity":[2.0],
                             "is_buyer_maker":[True],"agg_trade_id":[1],"first_trade_id":[1],"last_trade_id":[1]})
    monkeypatch.setattr(td, "dl_bn_agg", fake)
    d=["2026-06-13","2026-06-14"]
    a=td.build_agg_binance("BTC",d,cache_dir=tmp_path); assert len(a)==2 and calls["n"]==2
    b=td.build_agg_binance("BTC",d,cache_dir=tmp_path); assert calls["n"]==2 and len(b)==2
    c=td.build_agg_binance("BTC",d+["2026-06-15"],cache_dir=tmp_path); assert calls["n"]==3 and len(c)==3
    assert a["notional"].iloc[0]==20.0


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
