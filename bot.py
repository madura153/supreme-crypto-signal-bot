
import os
import time
import html
import json
import logging
import requests
import feedparser
import numpy as np
import pandas as pd

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==========================================================
# SUPREME CRYPTO + GOLD SIGNAL BOT
# Signal-only system. It does NOT place trades.
# ==========================================================

TZ = ZoneInfo("Asia/Colombo")
BINANCE = "https://fapi.binance.com"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))
MIN_SCORE = 7
TOP_COINS = 20
TIMEFRAMES = ["1h", "15m", "5m"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 SupremeSignalBot/1.0"})


def now_sl():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def get_json(url, params=None):
    try:
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning("Data request failed: %s", e)
        return None


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials are not configured.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Telegram send failed: %s", e)
        return False


# ---------------- BINANCE DATA ----------------

def get_top_symbols():
    info = get_json(f"{BINANCE}/fapi/v1/exchangeInfo")
    tickers = get_json(f"{BINANCE}/fapi/v1/ticker/24hr")

    if not info or not tickers:
        return []

    tradable = {
        s["symbol"] for s in info["symbols"]
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    }

    rows = [
        x for x in tickers
        if x.get("symbol") in tradable
        and float(x.get("quoteVolume", 0)) > 0
    ]

    rows.sort(
        key=lambda x: float(x.get("quoteVolume", 0)),
        reverse=True
    )

    # Keep BTC and ETH in the research universe.
    selected = [x["symbol"] for x in rows[:TOP_COINS]]
    for major in ["BTCUSDT", "ETHUSDT"]:
        if major in tradable and major not in selected:
            selected.append(major)

    return selected[:TOP_COINS]


def get_klines(symbol, interval, limit=250):
    data = get_json(
        f"{BINANCE}/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    if not data or len(data) < 100:
        return None

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df.dropna().reset_index(drop=True)


def get_futures_context(symbol):
    funding = get_json(
        f"{BINANCE}/fapi/v1/premiumIndex",
        {"symbol": symbol}
    )
    oi = get_json(
        f"{BINANCE}/fapi/v1/openInterest",
        {"symbol": symbol}
    )

    result = {
        "funding": None,
        "open_interest": None
    }

    if funding:
        result["funding"] = float(funding.get("lastFundingRate", 0))

    if oi:
        result["open_interest"] = float(oi.get("openInterest", 0))

    return result


# ---------------- INDICATORS ----------------

def add_indicators(df):
    d = df.copy()

    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    d["ema200"] = d["close"].ewm(span=200, adjust=False).mean()

    delta = d["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    d["rsi"] = 100 - (100 / (1 + rs))
    d["rsi"] = d["rsi"].fillna(50)

    fast = d["close"].ewm(span=12, adjust=False).mean()
    slow = d["close"].ewm(span=26, adjust=False).mean()
    d["macd"] = fast - slow
    d["macd_signal"] = d["macd"].ewm(
        span=9, adjust=False
    ).mean()

    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    d["atr"] = tr.ewm(span=14, adjust=False).mean()
    d["volume_avg"] = d["volume"].rolling(20).mean()

    # Supertrend calculation
    mid = (d["high"] + d["low"]) / 2
    upper = mid + 3 * d["atr"]
    lower = mid - 3 * d["atr"]

    st = pd.Series(index=d.index, dtype=float)
    direction = pd.Series(index=d.index, dtype=int)

    for i in range(len(d)):
        if i == 0 or pd.isna(d["atr"].iloc[i]):
            st.iloc[i] = np.nan
            direction.iloc[i] = 0
            continue

        prev_st = st.iloc[i-1]
        prev_dir = direction.iloc[i-1]
        close = d["close"].iloc[i]

        if prev_dir >= 0:
            if close < lower.iloc[i]:
                direction.iloc[i] = -1
                st.iloc[i] = upper.iloc[i]
            else:
                direction.iloc[i] = 1
                st.iloc[i] = max(lower.iloc[i], prev_st) if not pd.isna(prev_st) else lower.iloc[i]
        else:
            if close > upper.iloc[i]:
                direction.iloc[i] = 1
                st.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st.iloc[i] = min(upper.iloc[i], prev_st) if not pd.isna(prev_st) else upper.iloc[i]

    d["supertrend"] = st
    d["st_direction"] = direction

    return d.dropna().reset_index(drop=True)


def market_structure(df):
    recent = df.tail(30)
    previous = df.iloc[-60:-30]

    if len(previous) < 20:
        return "Insufficient history"

    recent_high = recent["high"].max()
    recent_low = recent["low"].min()
    previous_high = previous["high"].max()
    previous_low = previous["low"].min()

    if recent_high > previous_high and recent_low > previous_low:
        return "Higher highs / higher lows"
    if recent_high < previous_high and recent_low < previous_low:
        return "Lower highs / lower lows"

    return "Range / mixed structure"


def analyze_timeframe(df):
    d = add_indicators(df)
    x = d.iloc[-1]

    score = 0
    reasons = []

    if x["close"] > x["ema20"] > x["ema50"]:
        score += 1
        reasons.append("Price above EMA20/50")

    if x["ema50"] > x["ema200"]:
        score += 1
        reasons.append("EMA50 above EMA200")

    if x["rsi"] > 52 and x["rsi"] < 72:
        score += 1
        reasons.append("RSI bullish")

    if x["rsi"] < 48 and x["rsi"] > 28:
        score -= 1
        reasons.append("RSI bearish")

    if x["macd"] > x["macd_signal"]:
        score += 1
        reasons.append("MACD bullish")
    else:
        score -= 1
        reasons.append("MACD bearish")

    if x["st_direction"] > 0:
        score += 1
        reasons.append("Supertrend bullish")
    else:
        score -= 1
        reasons.append("Supertrend bearish")

    if x["volume"] > x["volume_avg"]:
        score += 1
        reasons.append("Volume above average")

    if x["close"] < x["ema20"] < x["ema50"]:
        score -= 1
        reasons.append("Price below EMA20/50")

    return {
        "data": d,
        "score": score,
        "price": float(x["close"]),
        "atr": float(x["atr"]),
        "rsi": float(x["rsi"]),
        "ema20": float(x["ema20"]),
        "ema50": float(x["ema50"]),
        "ema200": float(x["ema200"]),
        "macd": float(x["macd"]),
        "macd_signal": float(x["macd_signal"]),
        "volume_ratio": float(x["volume"] / x["volume_avg"])
            if x["volume_avg"] else 0,
        "supertrend": "Bullish" if x["st_direction"] > 0 else "Bearish",
        "structure": market_structure(d),
        "reasons": reasons
    }


# ---------------- GLOBAL MARKET + NEWS ----------------

def yahoo_quote(symbol):
    data = get_json(
        f"{YAHOO}{symbol}",
        {"range": "5d", "interval": "1h"}
    )
    try:
        result = data["chart"]["result"][0]
        closes = [
            x for x in result["indicators"]["quote"][0]["close"]
            if x is not None
        ]
        if len(closes) < 2:
            return None
        return {
            "price": float(closes[-1]),
            "change": (
                float(closes[-1]) / float(closes[-2]) - 1
            ) * 100
        }
    except Exception:
        return None


def global_market():
    tickers = {
        "DXY": "DX-Y.NYB",
        "S&P500": "^GSPC",
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "XAUUSD": "XAUUSD=X"
    }

    result = {}
    for name, ticker in tickers.items():
        result[name] = yahoo_quote(ticker)
        time.sleep(0.2)

    return result


NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bls.gov/feed/bls_latest.rss"
]


def get_news():
    headlines = []

    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for item in feed.entries[:8]:
                title = item.get("title", "").strip()
                if title:
                    headlines.append(title)
        except Exception as e:
            logging.warning("News feed error: %s", e)

    return headlines[:25]


def news_risk(headlines):
    high_risk_words = [
        "war", "attack", "sanction", "tariff", "crisis",
        "emergency", "rate decision", "interest rate",
        "inflation", "cpi", "fomc", "fed", "hack",
        "exploit", "bankruptcy", "default", "liquidation"
    ]

    found = []
    for headline in headlines:
        lower = headline.lower()
        if any(word in lower for word in high_risk_words):
            found.append(headline)

    return found[:5]


# ---------------- GOLD DATA ----------------

def gold_data():
    # Prefer XAUUSD spot. If unavailable, use COMEX gold futures.
    for ticker, label in [
        ("XAUUSD=X", "Yahoo XAUUSD spot reference"),
        ("GC=F", "COMEX Gold Futures reference")
    ]:
        data = get_json(
            f"{YAHOO}{ticker}",
            {"range": "6mo", "interval": "1h"}
        )

        try:
            result = data["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]

            df = pd.DataFrame({
                "open": quote["open"],
                "high": quote["high"],
                "low": quote["low"],
                "close": quote["close"],
                "volume": quote.get("volume")
            }).dropna()

            if len(df) >= 100:
                for c in ["open", "high", "low", "close", "volume"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")

                return add_indicators(df), label
        except Exception:
            continue

    return None, None


# ---------------- SIGNAL CALCULATION ----------------

def make_levels(price, atr, direction, df):
    recent = df.tail(30)

    if direction == "BUY":
        swing = float(recent["low"].min())
        sl = min(price - 1.5 * atr, swing - 0.15 * atr)
        risk = price - sl
        if risk <= 0:
            return None
        return {
            "entry": price,
            "sl": sl,
            "tp1": price + 1.5 * risk,
            "tp2": price + 2.0 * risk,
            "tp3": price + 3.0 * risk,
            "risk": risk
        }

    swing = float(recent["high"].max())
    sl = max(price + 1.5 * atr, swing + 0.15 * atr)
    risk = sl - price
    if risk <= 0:
        return None

    return {
        "entry": price,
        "sl": sl,
        "tp1": price - 1.5 * risk,
        "tp2": price - 2.0 * risk,
        "tp3": price - 3.0 * risk,
        "risk": risk
    }


def fmt_price(value):
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def research_summary(name, analysis, context, global_data, headlines):
    btc = global_data.get("BTC")
    dxy = global_data.get("DXY")
    sp = global_data.get("S&P500")

    btc_text = (
        f"{btc['change']:+.2f}% (recent hourly change)"
        if btc else "Unavailable"
    )
    dxy_text = (
        f"{dxy['change']:+.2f}% (recent hourly change)"
        if dxy else "Unavailable"
    )
    sp_text = (
        f"{sp['change']:+.2f}% (recent hourly change)"
        if sp else "Unavailable"
    )

    funding = context.get("funding")
    oi = context.get("open_interest")

    return (
        f"<b>MARKET RESEARCH</b>\n"
        f"Structure: {html.escape(analysis['structure'])}\n"
        f"RSI: {analysis['rsi']:.1f}\n"
        f"EMA20 / 50 / 200: "
        f"{fmt_price(analysis['ema20'])} / "
        f"{fmt_price(analysis['ema50'])} / "
        f"{fmt_price(analysis['ema200'])}\n"
        f"MACD: {analysis['macd']:.5f}\n"
        f"Supertrend: {analysis['supertrend']}\n"
        f"Volume ratio: {analysis['volume_ratio']:.2f}x\n"
        f"BTC recent change: {btc_text}\n"
        f"DXY recent change: {dxy_text}\n"
        f"S&P 500 recent change: {sp_text}\n"
        f"Funding: {funding if funding is not None else 'N/A'}\n"
        f"Open Interest: {oi if oi is not None else 'N/A'}\n"
    )


def create_signal(name, direction, score, analysis, context,
                  global_data, headlines, gold=False, gold_source=None):

    levels = make_levels(
        analysis["price"], analysis["atr"],
        direction, analysis["data"]
    )

    if not levels:
        return None

    current = datetime.now(TZ)
    valid_until = current + timedelta(minutes=20)

    method = "MARKET" if abs(
        analysis["price"] - analysis["ema20"]
    ) / analysis["price"] < 0.002 else "LIMIT"

    if gold:
        header = "🥇 <b>GOLD MARKET SIGNAL</b> 🥇"
        asset = f"<b>XAUUSD / GOLD</b>"
        source_text = f"Price source: {html.escape(gold_source or 'Unknown')}\n"
    else:
        header = "📊 <b>CRYPTO FUTURES SIGNAL</b>"
        asset = f"<b>{html.escape(name)}</b>"
        source_text = ""

    message = (
        f"{header}\n\n"
        f"{asset}\n"
        f"<b>{direction} / {'LONG' if direction == 'BUY' else 'SHORT'}</b>\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <code>{fmt_price(levels['entry'])}</code>\n"
        f"Entry method: {method}\n"
        f"Stop Loss: <code>{fmt_price(levels['sl'])}</code>\n"
        f"TP1: <code>{fmt_price(levels['tp1'])}</code>\n"
        f"TP2: <code>{fmt_price(levels['tp2'])}</code>\n"
        f"TP3: <code>{fmt_price(levels['tp3'])}</code>\n"
        f"Risk/Reward: 1 : 1.5 / 2 / 3\n"
        f"Setup score: {score}/10 (not win probability)\n"
        f"Signal time: {current.strftime('%Y-%m-%d %H:%M:%S')} SL\n"
        f"Entry valid until: {valid_until.strftime('%H:%M:%S')} SL\n"
        f"{source_text}\n"
        f"{research_summary(name, analysis, context, global_data, headlines)}\n"
        f"<b>NEWS / MACRO CHECK</b>\n"
    )

    risks = news_risk(headlines)
    if risks:
        message += "\n".join(
            "• " + html.escape(x) for x in risks[:3]
        )
    else:
        message += "No high-risk headline detected in the checked feeds."

    message += (
        "\n\n<b>Risk note:</b> Suggested risk budget is "
        f"{RISK_PERCENT:.2f}% of account equity. "
        "Position size must be calculated from the actual stop distance.\n"
        "<i>Research-based signal only. No guaranteed outcome. "
        "Verify price and spread before entry.</i>"
    )

    return message


# ---------------- ANALYZE CRYPTO ----------------

def analyze_crypto(symbol, global_data, headlines):
    results = {}

    for tf in TIMEFRAMES:
        raw = get_klines(symbol, tf)
        if raw is None:
            return None
        results[tf] = analyze_timeframe(raw)
        time.sleep(0.15)

    score = sum(x["score"] for x in results.values())

    # 1H and 15M must agree; 5M is used for entry confirmation.
    long_ok = (
        results["1h"]["score"] >= 2
        and results["15m"]["score"] >= 2
        and results["5m"]["score"] >= 1
    )

    short_ok = (
        results["1h"]["score"] <= -2
        and results["15m"]["score"] <= -2
        and results["5m"]["score"] <= -1
    )

    if long_ok:
        direction = "BUY"
    elif short_ok:
        direction = "SELL"
    else:
        return None

    # BTC trend is an additional market filter.
    btc = global_data.get("BTC")
    if btc and direction == "BUY" and btc["change"] < -1.5:
        return None
    if btc and direction == "SELL" and btc["change"] > 1.5:
        return None

    if abs(score) < MIN_SCORE:
        return None

    context = get_futures_context(symbol)
    analysis = results["5m"]

    return create_signal(
        symbol, direction, min(abs(score), 10),
        analysis, context, global_data, headlines
    )


# ---------------- ANALYZE GOLD ----------------

def analyze_gold(global_data, headlines):
    df, source = gold_data()
    if df is None:
        logging.warning("Gold price data unavailable.")
        return None

    # Resample Yahoo hourly candles to approximate 4H and 1H views.
    hourly = df.copy()
    hourly["time"] = pd.date_range(
        end=pd.Timestamp.now(),
        periods=len(hourly),
        freq="h"
    )
    hourly = hourly.set_index("time")

    four_hour = hourly.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    if len(four_hour) < 100:
        return None

    a1 = analyze_timeframe(hourly.reset_index(drop=True))
    a4 = analyze_timeframe(four_hour.reset_index(drop=True))

    if a1["score"] >= 2 and a4["score"] >= 2:
        direction = "BUY"
    elif a1["score"] <= -2 and a4["score"] <= -2:
        direction = "SELL"
    else:
        return None

    score = abs(a1["score"] + a4["score"])
    if score < 5:
        return None

    return create_signal(
        "XAUUSD", direction, min(score, 10),
        a1, {}, global_data, headlines,
        gold=True, gold_source=source
    )


# ---------------- MAIN ----------------

def main():
    logging.info("SUPREME BOT started at %s", now_sl())

    global_data = global_market()
    headlines = get_news()

    signals_sent = 0

    # Gold research
    try:
        gold_signal = analyze_gold(global_data, headlines)
        if gold_signal:
            if send_telegram(gold_signal):
                signals_sent += 1
    except Exception:
        logging.exception("Gold analysis failed")

    # Crypto research
    symbols = get_top_symbols()
    logging.info("Scanning %s futures symbols", len(symbols))

    for symbol in symbols:
        try:
            signal = analyze_crypto(
                symbol, global_data, headlines
            )
            if signal:
                if send_telegram(signal):
                    signals_sent += 1
                time.sleep(1)
        except Exception:
            logging.exception("Analysis failed for %s", symbol)

    logging.info(
        "Scan completed. Signals sent: %s", signals_sent
    )


if __name__ == "__main__":
    main()
