# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "urllib3", "pandas"]
# ///
"""共享：交易所逐笔归档下载 + notional 构造（OKX/Bybit/Binance）。
daily_stats.py 与 daily_report.py 都从这里 import —— 单一真相源，避免口径漂移。
口径完全等同原 daily_stats.py。"""
import datetime as dt
import gzip, io, time, tomllib, zipfile
from pathlib import Path
import requests, urllib3, pandas as pd

urllib3.disable_warnings()


def _load_config():
    """读 config.toml（与本文件同目录，cwd 无关）；缺失则返回空 dict 用默认值。"""
    p = Path(__file__).resolve().parent / "config.toml"
    if p.exists():
        with open(p, "rb") as f:
            return tomllib.load(f)
    return {}


_CFG = _load_config()
_proxy = _CFG.get("proxy", "http://127.0.0.1:7890")   # 缺省=本机 clash；config 里设 "" 则直连
PROXY = {"http": _proxy, "https": _proxy} if _proxy else None
SLACK_WEBHOOK_URL = _CFG.get("slack_webhook_url", "")
OUTDIR = Path("data/daily")
BYLIN = {"PEPE": "1000PEPEUSDT"}
OKX_CTVAL = {"BTC": 0.01, "SUI": 1, "AAVE": 0.1, "DOGE": 1000, "LINK": 1, "ARB": 10, "XRP": 100, "PEPE": 1e7}


def _get(url, retries=5):
    for a in range(retries):
        try:
            r = requests.get(url, timeout=180, verify=False, proxies=PROXY)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(2 ** a)


def _exists(url):
    """HEAD 探测归档是否存在（404→False）。部分服务器不支持 HEAD 时回退 GET。"""
    try:
        r = requests.head(url, timeout=30, verify=False, proxies=PROXY, allow_redirects=True)
        if r.status_code in (403, 405):
            r = requests.get(url, timeout=60, verify=False, proxies=PROXY, stream=True)
        return r.status_code == 200
    except Exception:
        return False


def _nextday(d):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()


def okx_files_for(days):
    return sorted({x for d in days for x in (d, _nextday(d))})


def inst_for(exch, coin, typ):
    is_contract = typ in ("swap", "linear")
    if exch == "OKX":
        return f"{coin}-USDT-SWAP" if is_contract else f"{coin}-USDT"
    if exch == "Bybit":
        return BYLIN.get(coin, f"{coin}USDT") if is_contract else f"{coin}USDT"
    return f"{coin}USDT"


def dl_okx(inst, date):
    c = _get(f"https://www.okx.com/cdn/okex/traderecords/trades/daily/{date.replace('-','')}/{inst}-trades-{date}.zip")
    if c is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(c))
    return pd.read_csv(z.open(z.namelist()[0]))


def dl_by(kind, sym, date):
    url = (f"https://public.bybit.com/trading/{sym}/{sym}{date}.csv.gz" if kind == "linear"
           else f"https://public.bybit.com/spot/{sym}/{sym}_{date}.csv.gz")
    c = _get(url)
    return None if c is None else pd.read_csv(gzip.open(io.BytesIO(c)))


def dl_bn(sym, date):
    c = _get(f"https://data.binance.vision/data/futures/um/daily/trades/{sym}/{sym}-trades-{date}.zip")
    if c is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(c))
    return pd.read_csv(z.open(z.namelist()[0]))


def to_notional(exch, coin, typ, raw):
    """一所一日原始逐笔 → 统一列 ts/price/size_raw/qty/notional。空/None 返回 None。"""
    if raw is None or raw.empty:
        return None
    is_contract = typ in ("swap", "linear")
    if exch == "OKX":
        ts = pd.to_numeric(raw["created_time"]).astype("int64")
        sz = pd.to_numeric(raw["size"]); px = pd.to_numeric(raw["price"])
        if is_contract and coin not in OKX_CTVAL:
            raise ValueError(f"OKX_CTVAL 未收录合约乘数: {coin}")
        qty = sz * OKX_CTVAL[coin] if is_contract else sz
        return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": qty, "notional": qty * px})
    if exch == "Bybit":
        if is_contract:
            ts = (pd.to_numeric(raw["timestamp"]) * 1000).round().astype("int64"); sz = pd.to_numeric(raw["size"])
        else:
            ts = pd.to_numeric(raw["timestamp"]).astype("int64"); sz = pd.to_numeric(raw["volume"])
        px = pd.to_numeric(raw["price"])
        return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": sz * px})
    # Binance：quote_qty 即归档直给的精确 USDT 成交额
    ts = pd.to_numeric(raw["time"]).astype("int64")
    px = pd.to_numeric(raw["price"]); sz = pd.to_numeric(raw["qty"])
    return pd.DataFrame({"ts": ts, "price": px, "size_raw": sz, "qty": sz, "notional": pd.to_numeric(raw["quote_qty"])})


def sheet_for(exch, coin, typ):
    """与 daily_stats.py 一致的 parquet sheet 命名（OKX=inst；Bybit={sym}_{linear|spot}；Binance={sym}_linear）。"""
    inst = inst_for(exch, coin, typ)
    if exch == "OKX":
        return inst
    if exch == "Bybit":
        return f"{inst}_{'linear' if typ in ('swap', 'linear') else 'spot'}"
    return f"{inst}_linear"  # Binance


def _download_days(exch, coin, typ, days):
    """只下载+构造 notional，不读写缓存。返回 days 内 DataFrame 或 None。
    OKX 按 UTC+8 切档，需当日+次日两文件。"""
    is_contract = typ in ("swap", "linear")
    inst = inst_for(exch, coin, typ)
    frames = []
    if exch == "OKX":
        for d in okx_files_for(days):
            f = to_notional(exch, coin, typ, dl_okx(inst, d))
            if f is not None:
                frames.append(f)
    elif exch == "Bybit":
        for d in days:
            f = to_notional(exch, coin, typ, dl_by("linear" if is_contract else "spot", inst, d))
            if f is not None:
                frames.append(f)
    else:
        for d in days:
            f = to_notional(exch, coin, typ, dl_bn(inst, d))
            if f is not None:
                frames.append(f)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).dropna()
    df["day"] = pd.to_datetime(df.ts, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    return df[df.day.isin(days)]


def build_notional(exch, coin, typ, days, cache_dir=OUTDIR):
    """增量缓存：已存的 UTC 日从本地 parquet 读，只下缺的天，合并存盘（历史累加）。
    与 daily_stats.py 共用 data/daily/<exch>_<sheet>.parquet（schema 一致）。
    返回 days 窗口内 DataFrame（按 ts 升序）；窗口内无任何数据返回 None。"""
    cache_dir = Path(cache_dir)
    outp = cache_dir / f"{exch}_{sheet_for(exch, coin, typ)}.parquet"
    existing = pd.read_parquet(outp) if outp.exists() else None
    have = set(existing["day"].unique()) if existing is not None else set()
    need = [d for d in days if d not in have]
    if need:
        new = _download_days(exch, coin, typ, need)
        if new is not None and not new.empty:
            cache_dir.mkdir(parents=True, exist_ok=True)
            combined = pd.concat([existing, new], ignore_index=True) if existing is not None else new
            combined = combined.sort_values("ts").reset_index(drop=True)
            combined.to_parquet(outp, index=False, compression="snappy")
            existing = combined
    if existing is None:
        return None
    out = existing[existing["day"].isin(days)].sort_values("ts").reset_index(drop=True)
    return out if not out.empty else None
