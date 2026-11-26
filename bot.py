
"""
SUPREME CRYPTO + GOLD SIGNAL BOT
Version: 5.1
Signal-only Telegram alerts.
NO automatic trading or order placement.
Python 3.11+
"""

import os
import time
import json
import logging
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np


# =====================================================
# CONFIGURATION
# =====================================================

BOT_VERSION = "5.1"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

SL_TZ = timezone(timedelta(hours=5, minutes=30))

STATE_FILE = "bot_state.json"

OKX_URL = "https://www.okx.com"
BYBIT_URL = "https://api.bybit.com"
BINANCE_URL = "https://fapi.binance.com"

CRYPTO_SYMBOLS = [
    "XRP-USDT-SWAP",
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP"
]

CRYPTO_TIMEFRAMES = ["1d", "1h", "15m", "5m"]

MIN_SCORE = 7
MAX_ENTRY_DRIFT_ATR = 0.50
MAX_ENTRY_DRIFT_PCT = 0.003

RISK_ATR_MULTIPLIER = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("SUPREME-BOT")

session = requests.Session()
session.headers.update({
    "User-Agent": "SupremeSignalBot/5.1"
})


# =====================================================
# TIME
# =====================================================

def now_slt():
    return datetime.now(timezone.utc).astimezone(SL_TZ)


def format_slt(value):
    try:
        if isinstance(value, (int, float)):
            value = datetime.fromtimestamp(
                float(value) / 1000,
                tz=timezone.utc
            )
        elif isinstance(value, str):
            value = pd.to_datetime(value, utc=True).to_pydatetime()

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(SL_TZ).strftime(
            "%Y-%m-%d %H:%M:%S SLT"
        )
    except Exception:
        return str(value)


# =====================================================
# HTTP
# =====================================================

def get_json(url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=25
        )

        if not response.ok:
            log.warning(
                "GET HTTP %s | %s | response=%s",
                response.status_code,
                url,
                response.text[:500]
            )
            return None

        try:
            return response.json()
        except ValueError:
            log.warning(
                "GET returned non-JSON response | %s | response=%s",
                url,
                response.text[:500]
            )
            return None

    except requests.RequestException as exc:
        log.warning("GET request failed: %s | %s", url, exc)
        return None


def post_json(url, data):
    try:
        response = session.post(
            url,
            json=data,
            timeout=25
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {
                "ok": False,
                "http_status": response.status_code,
                "description": response.text[:500]
            }

        if not response.ok:
            log.error(
                "POST HTTP %s | response=%s",
                response.status_code,
                str(payload)[:700]
            )

        return payload

    except requests.RequestException as exc:
        log.error("POST request failed: %s", exc)
        return None


# =====================================================
# STATE / DUPLICATE PROTECTION
# =====================================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        temp_file = STATE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        os.replace(temp_file, STATE_FILE)

    except Exception as exc:
        log.error("State save failed: %s", exc)


# =====================================================
# TELEGRAM
# =====================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Telegram credentials missing: "
            "check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID GitHub Secrets"
        )
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    result = post_json(url, {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })

    if isinstance(result, dict) and result.get("ok") is True:
        log.info("Telegram message sent successfully")
        return True

    if isinstance(result, dict):
        log.error(
            "Telegram delivery rejected: error_code=%s | description=%s",
            result.get("error_code", result.get("http_status", "unknown")),
            result.get("description", result)
        )
    else:
        log.error(
            "Telegram delivery failed: no valid response received. "
            "Check network access, bot token, and Chat ID."
        )

    return False


# =====================================================
# CANDLE NORMALIZATION
# =====================================================

def normalize_ohlcv(rows):
    if not rows:
        return None

    try:
        df = pd.DataFrame(
            rows,
            columns=[
                "timestamp", "open", "high",
                "low", "close", "volume"
            ]
        )

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["timestamp"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"], errors="coerce"),
            unit="ms",
            utc=True
        )

        df = df.dropna(
            subset=["timestamp", "open", "high", "low", "close"]
        )

        df["volume"] = df["volume"].fillna(0)

        df = (
            df.sort_values("timestamp")
            .drop_duplicates("timestamp")
            .set_index("timestamp")
        )

        return df[["open", "high", "low", "close", "volume"]]

    except Exception as exc:
        log.warning("Candle normalization error: %s", exc)
        return None


# =====================================================
# EXCHANGE SYMBOL
# =====================================================

def exchange_symbol(symbol, provider):
    if provider == "OKX":
        if symbol.endswith("-USDT-SWAP"):
            return symbol
        return symbol.replace("USDT", "-USDT-SWAP")

    return symbol.replace("-USDT-SWAP", "USDT")


# =====================================================
# OKX DATA
# =====================================================

def get_okx_klines(symbol, timeframe):
    bars = {
        "5m": "5m",
        "15m": "15m",
        "1h": "1H",
        "1d": "1D"
    }

    if timeframe not in bars:
        return None

    data = get_json(
        OKX_URL + "/api/v5/market/history-candles",
        {
            "instId": exchange_symbol(symbol, "OKX"),
            "bar": bars[timeframe],
            "limit": "300"
        }
    )

    if not data or data.get("code") != "0":
        return None

    rows = data.get("data", [])
    return normalize_ohlcv([r[:6] for r in rows][::-1])


# =====================================================
# BYBIT DATA
# =====================================================

def get_bybit_klines(symbol, timeframe):
    intervals = {
        "5m": "5",
        "15m": "15",
        "1h": "60",
        "1d": "D"
    }

    if timeframe not in intervals:
        return None

    data = get_json(
        BYBIT_URL + "/v5/market/kline",
        {
            "category": "linear",
            "symbol": exchange_symbol(symbol, "BYBIT"),
            "interval": intervals[timeframe],
            "limit": "300"
        }
    )

    if not data or data.get("retCode") != 0:
        return None

    rows = data.get("result", {}).get("list", [])
    return normalize_ohlcv([r[:6] for r in rows][::-1])


# =====================================================
# BINANCE DATA
# =====================================================

def get_binance_klines(symbol, timeframe):
    data = get_json(
        BINANCE_URL + "/fapi/v1/klines",
        {
            "symbol": exchange_symbol(symbol, "BINANCE"),
            "interval": timeframe,
            "limit": 300
        }
    )

    if not isinstance(data, list) or not data:
        return None

    return normalize_ohlcv([r[:6] for r in data])


# =====================================================
# CRYPTO DATA - SAME EXCHANGE FOR ALL TIMEFRAMES
# =====================================================

PROVIDERS = [
    ("OKX", get_okx_klines),
    ("BYBIT", get_bybit_klines),
    ("BINANCE", get_binance_klines)
]


def get_crypto_frames(symbol):
    for provider_name, function in PROVIDERS:
        frames = {}
        success = True

        for timeframe in CRYPTO_TIMEFRAMES:
            df = function(symbol, timeframe)

            if df is None or len(df) < 220:
                success = False
                break

            frames[timeframe] = df

        if success:
            log.info("%s data source: %s", symbol, provider_name)
            return frames, provider_name

    log.warning("No complete exchange data for %s", symbol)
    return None, None


# =====================================================
# LIVE PRICE
# =====================================================

def get_live_price(symbol, provider):
    market = exchange_symbol(symbol, provider)

    if provider == "OKX":
        data = get_json(
            OKX_URL + "/api/v5/market/ticker",
            {"instId": market}
        )

        if data and data.get("code") == "0" and data.get("data"):
            item = data["data"][0]
            return float(item["last"]), item.get("ts", "")

    elif provider == "BYBIT":
        data = get_json(
            BYBIT_URL + "/v5/market/tickers",
            {"category": "linear", "symbol": market}
        )

        if data and data.get("retCode") == 0:
            items = data.get("result", {}).get("list", [])
            if items:
                return float(items[0]["lastPrice"]), data.get("time", "")

    elif provider == "BINANCE":
        data = get_json(
            BINANCE_URL + "/fapi/v1/ticker/price",
            {"symbol": market}
        )

        if data and data.get("price"):
            return float(data["price"]), data.get("time", "")

    return None, ""


# =====================================================
# EXCHANGE PRICE TICK
# =====================================================

def get_tick_size(symbol, provider):
    market = exchange_symbol(symbol, provider)

    if provider == "OKX":
        data = get_json(
            OKX_URL + "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": market}
        )

        if data and data.get("code") == "0" and data.get("data"):
            return data["data"][0].get("tickSz")

    elif provider == "BYBIT":
        data = get_json(
            BYBIT_URL + "/v5/market/instruments-info",
            {"category": "linear", "symbol": market}
        )

        if data and data.get("retCode") == 0:
            items = data.get("result", {}).get("list", [])
            if items:
                return items[0].get("priceFilter", {}).get("tickSize")

    elif provider == "BINANCE":
        data = get_json(BINANCE_URL + "/fapi/v1/exchangeInfo")

        if data and isinstance(data.get("symbols"), list):
            for item in data["symbols"]:
                if item.get("symbol") == market:
                    for filt in item.get("filters", []):
                        if filt.get("filterType") == "PRICE_FILTER":
                            return filt.get("tickSize")

    return None


def tick_round(value, tick, rounding):
    value = Decimal(str(value))
    tick = Decimal(str(tick))

    if tick <= 0:
        raise ValueError("Invalid tick size")

    return (
        (value / tick).to_integral_value(rounding=rounding)
        * tick
    )


def price_text(value, tick):
    tick_decimal = Decimal(str(tick))
    rounded = tick_round(value, tick_decimal, ROUND_HALF_UP)
    decimals = max(0, -tick_decimal.normalize().as_tuple().exponent)
    return f"{rounded:.{decimals}f}"


# =====================================================
# GOLD DATA - TWELVE DATA
# =====================================================

GOLD_INTERVALS = {
    "1d": "1day",
    "1h": "1h",
    "15m": "15min",
    "5m": "5min"
}


def get_gold_frame(timeframe):
    if not TWELVEDATA_API_KEY:
        log.warning("Gold requires TWELVEDATA_API_KEY")
        return None

    interval = GOLD_INTERVALS.get(timeframe)

    if not interval:
        return None

    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": "XAU/USD",
            "interval": interval,
            "outputsize": 500,
            "apikey": TWELVEDATA_API_KEY
        }
    )

    if not data or "values" not in data:
        log.warning("Gold %s data unavailable", timeframe)
        return None

    try:
        df = pd.DataFrame(data["values"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if "volume" not in df.columns:
            df["volume"] = 0
        else:
            df["volume"] = pd.to_numeric(
                df["volume"], errors="coerce"
            ).fillna(0)

        df["timestamp"] = pd.to_datetime(
            df["datetime"], utc=True, errors="coerce"
        )

        df = df.dropna(
            subset=["timestamp", "open", "high", "low", "close"]
        )

        df = (
            df.sort_values("timestamp")
            .drop_duplicates("timestamp")
            .set_index("timestamp")
        )

        return df[["open", "high", "low", "close", "volume"]]

    except Exception as exc:
        log.warning("Gold processing failed: %s", exc)
        return None


def get_gold_frames():
    frames = {}

    for timeframe in ["1d", "1h", "15m", "5m"]:
        df = get_gold_frame(timeframe)

        if df is None or len(df) < 220:
            log.warning("Gold skipped: insufficient %s candles", timeframe)
            return None

        frames[timeframe] = df

    return frames


# =====================================================
# INDICATORS
# =====================================================

def add_indicators(df):
    df = df.copy()

    close = df["close"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        min_periods=14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        min_periods=14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi"] = 100 - (100 / (1 + rs))

    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    df.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()

    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(
        span=9, adjust=False
    ).mean()

    previous_close = close.shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(20).mean()

    return df


# =====================================================
# BULLISH / BEARISH SCORING
# =====================================================

def timeframe_score(df):
    """
    Separate bullish and bearish scoring.
    Never convert a weak bullish score into a SELL automatically.
    """

    if df is None or len(df) < 220:
        return None

    try:
        df = add_indicators(df)

        # Last completed candle
        row = df.iloc[-2]

        required = [
            "close", "ema20", "ema50", "ema200",
            "rsi", "macd", "macd_signal"
        ]

        if row[required].isna().any():
            return None

        price = float(row["close"])
        bullish = 0
        bearish = 0

        # Trend
        if price > row["ema20"]:
            bullish += 1
        elif price < row["ema20"]:
            bearish += 1

        if price > row["ema50"]:
            bullish += 1
        elif price < row["ema50"]:
            bearish += 1

        if price > row["ema200"]:
            bullish += 1
        elif price < row["ema200"]:
            bearish += 1

        if row["ema20"] > row["ema50"]:
            bullish += 1
        elif row["ema20"] < row["ema50"]:
            bearish += 1

        # MACD
        if row["macd"] > row["macd_signal"]:
            bullish += 1
        elif row["macd"] < row["macd_signal"]:
            bearish += 1

        # RSI
        rsi = float(row["rsi"])

        if 50 < rsi < 70:
            bullish += 1
        elif 30 < rsi < 50:
            bearish += 1

        # Extreme RSI blocks chasing an entry.
        if rsi >= 75:
            bullish = min(bullish, 3)

        if rsi <= 25:
            bearish = min(bearish, 3)

        if bullish >= 4 and bullish > bearish:
            direction = "BUY"
        elif bearish >= 4 and bearish > bullish:
            direction = "SELL"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "bullish": bullish,
            "bearish": bearish,
            "rsi": rsi,
            "atr": float(row["atr"]),
            "candle": row.name,
            "close": price
        }

    except Exception as exc:
        log.warning("Scoring error: %s", exc)
        return None


# =====================================================
# SIGNAL VALIDATION
# =====================================================

def analyze_frames(frames):
    results = {}

    for timeframe, df in frames.items():
        result = timeframe_score(df)

        if result is None:
            return None

        results[timeframe] = result

    directions = [
        results[tf]["direction"]
        for tf in ["1d", "1h", "15m", "5m"]
    ]

    if not all(d == directions[0] for d in directions):
        log.info(
            "Signal rejected: timeframe directions are not aligned: %s",
            directions
        )
        return None

    direction = directions[0]

    if direction not in ["BUY", "SELL"]:
        log.info(
            "Signal rejected: no confirmed BUY/SELL setup; directions=%s",
            directions
        )
        return None

    # FIXED: Correct score key for BUY and SELL.
    score_key = "bullish" if direction == "BUY" else "bearish"

    # Stronger minimum requirement on higher timeframes.
    if results["1d"][score_key] < 4:
        log.info(
            "Signal rejected: 1D %s score too low (%s)",
            direction, results["1d"][score_key]
        )
        return None

    if results["1h"][score_key] < 4:
        log.info(
            "Signal rejected: 1H %s score too low (%s)",
            direction, results["1h"][score_key]
        )
        return None

    total = sum(
        results[tf][score_key]
        for tf in ["1d", "1h", "15m", "5m"]
    )

    confidence = round((total / 24) * 100)

    # Confidence is a technical score, not a probability of profit.
    if confidence < 70:
        log.info(
            "Signal rejected: technical score %s/100 is below minimum 70/100",
            confidence
        )
        return None

    log.info(
        "Signal setup confirmed: %s | technical score=%s/100",
        direction, confidence
    )
    return direction, confidence, results


# =====================================================
# TRADE LEVELS
# =====================================================

def build_levels(direction, entry, atr, market, tick=None):
    if not np.isfinite(entry) or not np.isfinite(atr):
        return None

    if entry <= 0 or atr <= 0:
        return None

    risk = atr * RISK_ATR_MULTIPLIER

    if direction == "BUY":
        stop = entry - risk
        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.0
        tp3 = entry + risk * 3.0

        if tick:
            stop = float(tick_round(stop, tick, ROUND_FLOOR))
            tp1 = float(tick_round(tp1, tick, ROUND_CEILING))
            tp2 = float(tick_round(tp2, tick, ROUND_CEILING))
            tp3 = float(tick_round(tp3, tick, ROUND_CEILING))
            entry = float(tick_round(entry, tick, ROUND_HALF_UP))

        if not stop < entry < tp1 < tp2 < tp3:
            return None

    elif direction == "SELL":
        stop = entry + risk
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.0
        tp3 = entry - risk * 3.0

        if tick:
            stop = float(tick_round(stop, tick, ROUND_CEILING))
            tp1 = float(tick_round(tp1, tick, ROUND_FLOOR))
            tp2 = float(tick_round(tp2, tick, ROUND_FLOOR))
            tp3 = float(tick_round(tp3, tick, ROUND_FLOOR))
            entry = float(tick_round(entry, tick, ROUND_HALF_UP))

        if not stop > entry > tp1 > tp2 > tp3:
            return None

    else:
        return None

    return {
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk": risk
    }


# =====================================================
# SIGNAL MESSAGE
# =====================================================

def make_message(
    market,
    pair,
    direction,
    confidence,
    levels,
    results,
    source,
    tick=None
):
    entry = levels["entry"]
    stop = levels["stop"]
    tp1 = levels["tp1"]
    tp2 = levels["tp2"]
    tp3 = levels["tp3"]

    def fmt(value):
        if tick:
            return price_text(value, tick)
        return f"{value:.2f}"

    current_time = now_slt()

    # Fixed review estimate.
    # This is not a guaranteed time for a target or stop-loss.
    expected_minutes = 45

    expected_close = current_time + timedelta(
        minutes=expected_minutes
    )

    candle_time = format_slt(
        results["5m"]["candle"]
    )

    direction_label = (
        "🟢 BUY"
        if direction == "BUY"
        else "🔴 SELL"
    )

    message = (
        "🚨 <b>SUPREME SIGNAL</b> 🚨\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>MARKET:</b> {market}\n"
        f"💱 <b>TRADING PAIR:</b> {pair}\n"
        f"📌 <b>DIRECTION:</b> {direction_label}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ENTRY PRICE:</b> {fmt(entry)}\n"
        f"🛑 <b>STOP LOSS:</b> {fmt(stop)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>TAKE PROFIT TARGETS</b>\n"
        f"🥇 <b>TP1:</b> {fmt(tp1)}\n"
        f"🥈 <b>TP2:</b> {fmt(tp2)}\n"
        f"🥉 <b>TP3:</b> {fmt(tp3)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⭐ <b>TECHNICAL SCORE:</b> {confidence}/100\n"
        f"📈 <b>RSI (5M):</b> {results['5m']['rsi']:.2f}\n"
        f"📉 <b>ATR (5M):</b> {results['5m']['atr']:.6g}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕘 <b>SIGNAL TIME (SLT):</b> {current_time:%Y-%m-%d %H:%M:%S}\n"
        f"⏱ <b>ESTIMATED REVIEW INTERVAL:</b> {expected_minutes} minutes\n"
        f"🕒 <b>REVIEW TIME:</b> {expected_close:%Y-%m-%d %H:%M:%S} SLT\n"
        f"🕯 <b>LAST COMPLETED 5M CANDLE:</b> {candle_time}\n"
        f"📡 <b>DATA SOURCE:</b> {source}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>TIMEFRAME CONFIRMATION</b>\n"
        f"1D: {results['1d']['direction']}\n"
        f"1H: {results['1h']['direction']}\n"
        f"15M: {results['15m']['direction']}\n"
        f"5M: {results['5m']['direction']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Technical score is not a win probability.</i>\n"
        "⚠️ <i>Review time is not a guaranteed closing time.</i>\n"
        "❌ <i>Signal only. No automatic trading.</i>\n"
        "⚠️ <i>No guaranteed profit.</i>"
    )

    return message


# =====================================================
# CRYPTO ANALYSIS
# =====================================================

def evaluate_crypto(symbol):
    frames, provider = get_crypto_frames(symbol)

    if frames is None:
        return None

    analysis = analyze_frames(frames)

    if analysis is None:
        log.info("%s: no aligned setup", symbol)
        return None

    direction, confidence, results = analysis

    live_price, quote_time = get_live_price(symbol, provider)

    if live_price is None or live_price <= 0:
        return None

    tick = get_tick_size(symbol, provider)

    if not tick:
        log.warning("%s: tick size unavailable", symbol)
        return None

    # Prevent chasing a price far from the completed 5m candle.
    candle_close = results["5m"]["close"]
    atr = results["5m"]["atr"]

    drift = abs(live_price - candle_close)

    allowed = min(
        MAX_ENTRY_DRIFT_ATR * atr,
        MAX_ENTRY_DRIFT_PCT * candle_close
    )

    if drift > allowed:
        log.info("%s: entry drift too high", symbol)
        return None

    levels = build_levels(
        direction,
        live_price,
        atr,
        "CRYPTO",
        tick
    )

    if levels is None:
        return None

    pair = symbol.replace("-USDT-SWAP", "/USDT")

    return make_message(
        "CRYPTO",
        pair,
        direction,
        confidence,
        levels,
        results,
        f"{provider} live market data",
        tick
    )


# =====================================================
# GOLD ANALYSIS
# =====================================================

def evaluate_gold():
    frames = get_gold_frames()

    if frames is None:
        log.warning("Gold signal skipped: required data unavailable")
        return None

    analysis = analyze_frames(frames)

    if analysis is None:
        log.info("Gold: no aligned setup")
        return None

    direction, confidence, results = analysis

    # Gold uses latest completed 5-minute candle as entry reference.
    entry = results["5m"]["close"]
    atr = results["5m"]["atr"]

    levels = build_levels(
        direction,
        entry,
        atr,
        "GOLD"
    )

    if levels is None:
        return None

    return make_message(
        "GOLD",
        "XAU/USD",
        direction,
        confidence,
        levels,
        results,
        "Twelve Data XAU/USD"
    )


# =====================================================
# DUPLICATE CHECK
# =====================================================

def process_signal(state, key, message):
    if not message:
        return

    # Use the candle and direction portion to avoid repeats.
    lines = message.splitlines()

    selected = [
        line for line in lines
        if "DIRECTION:" in line
        or "LAST COMPLETED 5M CANDLE:" in line
    ]

    fingerprint = "|".join(selected)

    if state.get(key) == fingerprint:
        log.info("Duplicate signal skipped: %s", key)
        return

    if send_telegram(message):
        state[key] = fingerprint


# =====================================================
# MAIN SCAN
# =====================================================

def run_scan():
    log.info("SUPREME BOT %s SCAN STARTED", BOT_VERSION)

    state = load_state()

    # GOLD - separate Telegram message
    try:
        message = evaluate_gold()

        if message:
            process_signal(state, "GOLD_XAUUSD", message)

    except Exception:
        log.exception("Gold scan error")

    # Exactly three crypto pairs
    for symbol in CRYPTO_SYMBOLS:
        try:
            message = evaluate_crypto(symbol)

            if message:
                process_signal(
                    state,
                    "CRYPTO_" + symbol,
                    message
                )

        except Exception:
            log.exception("Crypto scan failed: %s", symbol)

        time.sleep(1)

    save_state(state)

    log.info(
        "SUPREME BOT SCAN COMPLETED. "
        "If no Telegram alert was sent, check the rejection/data messages above; "
        "a successful scan does not guarantee that a valid signal exists."
    )


# =====================================================
# START
# =====================================================

def main():
    log.info("SUPREME SIGNAL BOT %s STARTED", BOT_VERSION)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Telegram secrets are missing. Add TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID under GitHub Settings > Secrets and variables > Actions."
        )
        raise SystemExit(1)

    test_mode = os.getenv("TELEGRAM_TEST_MODE", "false").strip().lower()
    if test_mode in ("1", "true", "yes", "on"):
        test_message = (
            "✅ <b>SUPREME BOT TELEGRAM TEST</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Telegram connection is working.\n"
            "Bot token and Chat ID were accepted by Telegram.\n"
            f"🕘 Test time (SLT): {now_slt():%Y-%m-%d %H:%M:%S}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "This is a connection test, not a trading signal."
        )

        if not send_telegram(test_message):
            log.error("Telegram test failed. See the error details above.")
            raise SystemExit(1)

        log.info("Telegram test completed. Market scan was skipped.")
        return

    run_scan()


if __name__ == "__main__":
    main()
