"""
SUPREME CRYPTO + GOLD SIGNAL BOT
Signal-only: sends Telegram research alerts; NEVER places orders.
Python 3.11+
"""
import os
import time
import json
import logging
import html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np

TZ = ZoneInfo("Asia/Colombo")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

SCAN_TOP = int(os.getenv("SCAN_TOP", "25"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
STATE_FILE = os.getenv("STATE_FILE", "signal_state.json")
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1") == "1"
MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS_PER_RUN", "8"))

BINANCE_FUTURES = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
OKX = "https://www.okx.com"
YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/",
    "https://query2.finance.yahoo.com/v8/finance/chart/",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("supreme")
session = requests.Session()
session.headers.update({"User-Agent": "SupremeSignalBot/2.0", "Accept": "application/json"})

def now_local():
    return datetime.now(TZ)

def get_json(url, params=None, timeout=12):
    try:
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("retCode") not in (None, 0):
            raise RuntimeError(f"API error {data.get('retCode')}: {data.get('retMsg')}")
        return data
    except Exception as exc:
        log.warning("GET failed %s: %s", url, exc)
        return None

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = session.post(url, data={
            "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return True
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sent": {}}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)

def already_sent(state, key, cooldown_hours=6):
    stamp = state.get("sent", {}).get(key)
    if not stamp:
        return False
    try:
        sent_at = datetime.fromisoformat(stamp)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - sent_at < timedelta(hours=cooldown_hours)
    except ValueError:
        return False

def mark_sent(state, key):
    state.setdefault("sent", {})[key] = datetime.now(timezone.utc).isoformat()

# ---------- Crypto market data: Bybit -> OKX -> Binance Futures ----------
def bybit_symbols(limit):
    data = get_json(f"{BYBIT}/v5/market/tickers", {"category": "linear"})
    if not data:
        return []
    rows = data.get("result", {}).get("list", [])
    rows = [x for x in rows if x.get("symbol", "").endswith("USDT")
            and float(x.get("turnover24h", 0) or 0) > 0]
    rows.sort(key=lambda x: float(x.get("turnover24h", 0) or 0), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def okx_symbols(limit):
    data = get_json(f"{OKX}/api/v5/market/tickers", {"instType": "SWAP"})
    if not data or data.get("code") != "0":
        return []
    rows = [x for x in data.get("data", [])
            if x.get("instId", "").endswith("-USDT-SWAP")]
    rows.sort(key=lambda x: float(x.get("volCcy24h", 0) or 0), reverse=True)
    return [x["instId"] for x in rows[:limit]]

def binance_symbols(limit):
    info = get_json(f"{BINANCE_FUTURES}/fapi/v1/exchangeInfo")
    tickers = get_json(f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr")
    if not info or not tickers:
        return []
    tradable = {x["symbol"] for x in info.get("symbols", [])
                if x.get("quoteAsset") == "USDT"
                and x.get("contractType") == "PERPETUAL"
                and x.get("status") == "TRADING"}
    rows = [x for x in tickers if x.get("symbol") in tradable]
    rows.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def get_symbols():
    for name, fn in (("Bybit", bybit_symbols), ("OKX", okx_symbols), ("Binance", binance_symbols)):
        symbols = fn(SCAN_TOP)
        if symbols:
            log.info("Using %s public market data; %s symbols", name, len(symbols))
            return name, symbols
    log.error("All crypto market data providers failed; crypto scan skipped.")
    return None, []

def normalize_ohlcv(rows, source):
    # Standard internal columns: open/high/low/close/volume
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.dropna().sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(df) < 220:
        return None
    df.attrs["source"] = source
    return df

def bybit_klines(symbol, interval, limit=300):
    iv = {"1h": "60", "15m": "15", "5m": "5"}[interval]
    data = get_json(f"{BYBIT}/v5/market/kline",
                    {"category": "linear", "symbol": symbol, "interval": iv, "limit": limit})
    if not data:
        return None
    rows = data.get("result", {}).get("list", [])
    rows = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]
    return normalize_ohlcv(rows, "Bybit linear")

def okx_klines(symbol, interval, limit=300):
    bar = {"1h": "1H", "15m": "15m", "5m": "5m"}[interval]
    data = get_json(f"{OKX}/api/v5/market/candles",
                    {"instId": symbol, "bar": bar, "limit": str(limit)})
    if not data or data.get("code") != "0":
        return None
    rows = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in data.get("data", [])]
    return normalize_ohlcv(rows, "OKX swap")

def binance_klines(symbol, interval, limit=300):
    data = get_json(f"{BINANCE_FUTURES}/fapi/v1/klines",
                    {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return None
    rows = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in data]
    return normalize_ohlcv(rows, "Binance USD-M")

def exchange_symbol(symbol, provider):
    # Convert common BTCUSDT-style symbols to the exchange's instrument notation.
    if provider == "OKX":
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{base}-USDT-SWAP"
    return symbol

def get_crypto_klines(provider, symbol, interval):
    funcs = {"Bybit": bybit_klines, "OKX": okx_klines, "Binance": binance_klines}
    for name in (provider, "Bybit", "OKX", "Binance"):
        if name not in funcs:
            continue
        candidate = exchange_symbol(symbol, name)
        try:
            df = funcs[name](candidate, interval)
            if df is not None:
                if name != provider:
                    log.info("%s %s fallback to %s", symbol, interval, name)
                return df
        except Exception as exc:
            log.warning("%s %s %s fallback failed: %s", name, symbol, interval, exc)
    return None

# ---------- Gold: Twelve Data spot if key exists, else Yahoo GC=F proxy ----------
def gold_from_twelvedata():
    if not TWELVEDATA_API_KEY:
        return None
    data = get_json("https://api.twelvedata.com/time_series", {
        "symbol": "XAU/USD", "interval": "1h", "outputsize": 5000,
        "apikey": TWELVEDATA_API_KEY, "format": "JSON",
    })
    if not data or "values" not in data:
        log.warning("Twelve Data XAU/USD unavailable: %s", (data or {}).get("message", "no response"))
        return None
    rows = []
    for x in reversed(data["values"]):
        # Twelve Data volume is often absent for spot; use 1 as neutral placeholder.
        rows.append([pd.Timestamp(x["datetime"]).timestamp() * 1000,
                     x["open"], x["high"], x["low"], x["close"], x.get("volume") or 1])
    return normalize_ohlcv(rows, "Twelve Data XAU/USD spot")

def gold_from_yahoo():
    # GC=F is COMEX gold futures, NOT the XAU/USD spot feed. Label it honestly.
    for host in YAHOO_HOSTS:
        data = get_json(host + "GC=F", {"range": "6mo", "interval": "1h"})
        try:
            result = data["chart"]["result"][0]
            q = result["indicators"]["quote"][0]
            timestamps = result["timestamp"]
            rows = []
            for i, ts in enumerate(timestamps):
                vals = [q[k][i] if q.get(k) and i < len(q[k]) else None
                        for k in ("open", "high", "low", "close", "volume")]
                if all(v is not None for v in vals[:4]):
                    rows.append([ts * 1000, *vals[:4], vals[4] or 1])
            df = normalize_ohlcv(rows, "Yahoo COMEX GC=F futures proxy")
            if df is not None:
                return df
        except Exception as exc:
            log.warning("Yahoo GC=F parse failed: %s", exc)
    return None

def get_gold_data():
    spot = gold_from_twelvedata()
    return spot if spot is not None else gold_from_yahoo()

# ---------- Indicators and strategy ----------
def indicators(raw):
    d = raw.copy()
    close = d["close"]
    d["ema20"] = close.ewm(span=20, adjust=False).mean()
    d["ema50"] = close.ewm(span=50, adjust=False).mean()
    d["ema200"] = close.ewm(span=200, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    d["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    d["macd"] = fast - slow
    d["macd_signal"] = d["macd"].ewm(span=9, adjust=False).mean()
    prev = close.shift(1)
    tr = pd.concat([(d["high"] - d["low"]),
                    (d["high"] - prev).abs(),
                    (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    d["vol_avg"] = d["volume"].rolling(20).mean()
    return d.dropna().reset_index(drop=True)

def score_timeframe(raw):
    d = indicators(raw)
    if len(d) < 205:
        return None
    # Ignore the currently-forming candle: only closed candles are used.
    x = d.iloc[-2]
    score, reasons = 0, []
    if x.close > x.ema20 > x.ema50:
        score += 2; reasons.append("price > EMA20 > EMA50")
    elif x.close < x.ema20 < x.ema50:
        score -= 2; reasons.append("price < EMA20 < EMA50")
    if x.ema50 > x.ema200:
        score += 1; reasons.append("EMA50 > EMA200")
    elif x.ema50 < x.ema200:
        score -= 1; reasons.append("EMA50 < EMA200")
    if 52 <= x.rsi <= 68:
        score += 1; reasons.append("RSI bullish")
    elif 32 <= x.rsi <= 48:
        score -= 1; reasons.append("RSI bearish")
    if x.macd > x.macd_signal:
        score += 1; reasons.append("MACD bullish")
    elif x.macd < x.macd_signal:
        score -= 1; reasons.append("MACD bearish")
    vr = float(x.volume / x.vol_avg) if x.vol_avg and x.vol_avg > 0 else 0
    if vr >= 1.15:
        score += 1 if x.close > x.open else -1 if x.close < x.open else 0
        reasons.append(f"volume {vr:.2f}x")
    return {"score": score, "price": float(x.close), "atr": float(x.atr),
            "rsi": float(x.rsi), "ema20": float(x.ema20),
            "ema50": float(x.ema50), "ema200": float(x.ema200),
            "volume_ratio": vr, "df": d, "reasons": reasons}

def build_signal(asset, direction, score, fast, medium, slow, source, kind):
    price, atr = fast["price"], fast["atr"]
    if price <= 0 or atr <= 0:
        return None
    recent = fast["df"].iloc[-32:-2]
    if len(recent) < 15:
        return None
    if direction == "LONG":
        structural = float(recent["low"].min())
        sl = min(price - 1.5 * atr, structural - 0.10 * atr)
        risk = price - sl
        tps = [price + risk * m for m in (1.5, 2.0, 3.0)]
    else:
        structural = float(recent["high"].max())
        sl = max(price + 1.5 * atr, structural + 0.10 * atr)
        risk = sl - price
        tps = [price - risk * m for m in (1.5, 2.0, 3.0)]
    if risk <= 0 or risk / price > 0.04:
        return None
    # Avoid chasing a candle that has already moved too far from the fast EMA.
    if abs(price - fast["ema20"]) / price > 0.012:
        return None
    rr = abs(tps[1] - price) / risk
    if rr < 1.9:
        return None
    timestamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    safe_asset = html.escape(asset)
    reasons = ", ".join(fast["reasons"][:5])
    return (
        f"📡 <b>SUPREME {html.escape(kind)} SIGNAL</b>\n\n"
        f"<b>{safe_asset}</b>  |  <b>{direction}</b>\n"
        f"Data: {html.escape(source)}\n"
        f"Time (Sri Lanka): {timestamp}\n\n"
        f"<b>TRADE PLAN (indicative)</b>\n"
        f"Entry reference: <code>{price:.6f}</code>\n"
        f"Stop Loss: <code>{sl:.6f}</code>\n"
        f"TP1 (1.5R): <code>{tps[0]:.6f}</code>\n"
        f"TP2 (2R): <code>{tps[1]:.6f}</code>\n"
        f"TP3 (3R): <code>{tps[2]:.6f}</code>\n"
        f"Risk distance: {risk/price*100:.2f}%\n"
        f"Score: {score}/10 (confluence score, NOT win probability)\n"
        f"RSI: {fast['rsi']:.1f} | Volume: {fast['volume_ratio']:.2f}x\n"
        f"1H score: {slow['score']} | 15m score: {medium['score']} | 5m/1h entry score: {fast['score']}\n"
        f"Checks: {html.escape(reasons)}\n\n"
        f"<i>Signal-only research. Check live spread, contract/specification and price before any decision. "
        f"No orders are placed; losses are possible.</i>"
    )

def evaluate_three_timeframes(asset, fetcher, source, kind):
    frames = {}
    for tf in ("1h", "15m", "5m"):
        raw = fetcher(tf)
        if raw is None:
            return None
        # Freshness / candle integrity check.
        frames[tf] = score_timeframe(raw)
        if frames[tf] is None:
            return None
        time.sleep(0.1)
    h, m, f = frames["1h"], frames["15m"], frames["5m"]
    total = h["score"] + m["score"] + f["score"]
    if h["score"] >= 2 and m["score"] >= 2 and f["score"] >= 1 and total >= MIN_SCORE:
        direction = "LONG"
    elif h["score"] <= -2 and m["score"] <= -2 and f["score"] <= -1 and total <= -MIN_SCORE:
        direction = "SHORT"
    else:
        return None
    score = min(10, abs(total))
    return build_signal(asset, direction, score, f, m, h, source, kind)

# ---------- News headline caution ----------
NEWS_FEEDS = [
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bls.gov/feed/bls_latest.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
def news_headlines():
    # News is a context note only. It is not represented as a real-time event calendar.
    try:
        import feedparser
        out = []
        for url in NEWS_FEEDS:
            feed = feedparser.parse(url)
            out.extend([e.get("title", "").strip() for e in feed.entries[:5] if e.get("title")])
        return out[:12]
    except Exception as exc:
        log.warning("News feed unavailable: %s", exc)
        return []


def evaluate_gold_hourly(raw, source):
    # Real 1h candles aggregated into 4h bars using actual timestamps; no fake 5m data.
    d = raw.copy()
    d["dt"] = pd.to_datetime(d["time"], unit="ms", utc=True)
    d = d.set_index("dt")
    h4 = d.resample("4h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "time": "last"
    }).dropna().reset_index(drop=True)
    h1 = raw.copy()
    a1, a4 = score_timeframe(h1), score_timeframe(h4)
    if a1 is None or a4 is None:
        return None
    total = a1["score"] + a4["score"]
    if a4["score"] >= 2 and a1["score"] >= 2 and total >= 5:
        direction = "LONG"
    elif a4["score"] <= -2 and a1["score"] <= -2 and total <= -5:
        direction = "SHORT"
    else:
        return None
    return build_signal("XAUUSD", direction, min(10, abs(total)),
                        a1, a1, a4, source, "GOLD")

def main():
    log.info("SUPREME BOT start | Colombo time %s", now_local().isoformat())
    state = load_state()
    sent_count = 0

    # Gold: spot via Twelve Data key; otherwise explicitly labelled COMEX futures proxy.
    try:
        gold = get_gold_data()
        if gold is not None:
            source = gold.attrs.get("source", "Gold provider")
            gsignal = evaluate_gold_hourly(gold, source)
            if gsignal:
                key = "GOLD_XAUUSD_" + gsignal.splitlines()[2]
                if not already_sent(state, key) and send_telegram(gsignal):
                    mark_sent(state, key); sent_count += 1
            else:
                log.info("Gold: no confirmed 1H/4H setup.")
        else:
            log.warning("Gold data unavailable. Gold scan skipped.")
    except Exception:
        log.exception("Gold analysis failed")

    provider, symbols = get_symbols()
    log.info("Crypto scan provider=%s symbols=%s", provider, len(symbols))
    if provider:
        for symbol in symbols:
            try:
                signal = evaluate_three_timeframes(
                    symbol,
                    lambda tf, s=symbol: get_crypto_klines(provider, s, tf),
                    provider, "CRYPTO FUTURES"
                )
                if signal:
                    key = f"{provider}_{symbol}_{signal.splitlines()[2]}"
                    if not already_sent(state, key, cooldown_hours=6):
                        if send_telegram(signal):
                            mark_sent(state, key); sent_count += 1
                            time.sleep(1)
                if sent_count >= MAX_SIGNALS_PER_RUN:
                    break
            except Exception:
                log.exception("Analysis failed for %s", symbol)

    if NEWS_ENABLED:
        headlines = news_headlines()
        if headlines:
            log.info("News context headlines fetched: %s (not used as a trade trigger)", len(headlines))
    save_state(state)
    log.info("Scan complete. Telegram signals sent=%s", sent_count)

if __name__ == "__main__":
    main()
