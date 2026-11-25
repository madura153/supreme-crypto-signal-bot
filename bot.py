
"""
SUPREME CRYPTO + GOLD SIGNAL BOT
Signal-only Telegram alerts.
NO automatic trading or order placement.
Python 3.11+
"""

import os
import time
import json
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# ================= CONFIG =================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

MIN_SCORE = 7
MAX_SYMBOLS = 25
SCAN_INTERVAL = 900

BYBIT_URL = "https://api.bybit.com"
OKX_URL = "https://www.okx.com"
BINANCE_URL = "https://fapi.binance.com"

STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("SUPREME-BOT")

session = requests.Session()
session.headers.update({
    "User-Agent": "SupremeCryptoGoldSignalBot/1.0"
})


# ================= HTTP =================

def get_json(url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=20
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        log.warning("Request failed: %s", exc)
        return None


# ================= STATE =================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.warning("State save failed: %s", exc)


# ================= TELEGRAM =================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram secrets missing")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    try:
        response = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            log.error("Telegram error: %s", result)
            return False

        return True

    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False


# ================= SYMBOLS =================

def exchange_symbol(symbol, provider):
    symbol = symbol.strip()

    if provider == "OKX":
        if symbol.endswith("-USDT-SWAP"):
            return symbol
        if symbol.endswith("USDT"):
            return symbol[:-4] + "-USDT-SWAP"
        return symbol

    if symbol.endswith("-USDT-SWAP"):
        return symbol[:-10] + "USDT"

    return symbol


def get_symbols():
    symbols = []

    # OKX first
    data = get_json(
        OKX_URL + "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )

    if data and data.get("code") == "0":
        for item in data.get("data", []):
            symbol = item.get("instId", "")
            if symbol.endswith("-USDT-SWAP"):
                symbols.append(symbol)

    # Bybit fallback
    if not symbols:
        data = get_json(
            BYBIT_URL + "/v5/market/tickers",
            {"category": "linear"}
        )

        if data and data.get("retCode") == 0:
            for item in data.get("result", {}).get("list", []):
                symbol = item.get("symbol", "")
                if symbol.endswith("USDT"):
                    symbols.append(symbol)

    # Binance fallback
    if not symbols:
        data = get_json(
            BINANCE_URL + "/fapi/v1/exchangeInfo"
        )

        if data and "symbols" in data:
            for item in data["symbols"]:
                if (
                    item.get("quoteAsset") == "USDT"
                    and item.get("contractType") == "PERPETUAL"
                    and item.get("status") == "TRADING"
                ):
                    symbols.append(item["symbol"])

    # Add XRP even if it is not in the returned list
    xrp = (
        "XRP-USDT-SWAP"
        if symbols and "-USDT-SWAP" in symbols[0]
        else "XRPUSDT"
    )

    if xrp not in symbols:
        symbols.append(xrp)

    # Keep XRP in the scan list
    symbols = symbols[:MAX_SYMBOLS]
    if xrp not in symbols:
        symbols[-1] = xrp

    return symbols


# ================= CANDLE DATA =================

def normalize_ohlcv(rows):
    if not rows:
        return None

    try:
        df = pd.DataFrame(
            rows,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["timestamp"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"]),
            unit="ms",
            utc=True
        )

        df = df.dropna()
        df = df.sort_values("timestamp")
        df = df.drop_duplicates("timestamp")
        df = df.set_index("timestamp")

        if len(df) < 220:
            return None

        return df

    except Exception as exc:
        log.warning("Candle conversion failed: %s", exc)
        return None


def get_okx_klines(symbol, timeframe, limit=300):
    market = exchange_symbol(symbol, "OKX")

    bar_map = {
        "5m": "5m",
        "15m": "15m",
        "1h": "1H"
    }

    data = get_json(
        OKX_URL + "/api/v5/market/candles",
        {
            "instId": market,
            "bar": bar_map[timeframe],
            "limit": str(limit)
        }
    )

    if not data or data.get("code") != "0":
        return None

    rows = data.get("data", [])
    return normalize_ohlcv([
        row[:6] for row in rows
    ][::-1])


def get_bybit_klines(symbol, timeframe, limit=300):
    market = exchange_symbol(symbol, "BYBIT")

    interval_map = {
        "5m": "5",
        "15m": "15",
        "1h": "60"
    }

    data = get_json(
        BYBIT_URL + "/v5/market/kline",
        {
            "category": "linear",
            "symbol": market,
            "interval": interval_map[timeframe],
            "limit": limit
        }
    )

    if not data or data.get("retCode") != 0:
        return None

    rows = data.get("result", {}).get("list", [])
    return normalize_ohlcv([
        row[:6] for row in rows
    ][::-1])


def get_binance_klines(symbol, timeframe, limit=300):
    market = exchange_symbol(symbol, "BINANCE")

    data = get_json(
        BINANCE_URL + "/fapi/v1/klines",
        {
            "symbol": market,
            "interval": timeframe,
            "limit": limit
        }
    )

    if not data or not isinstance(data, list):
        return None

    return normalize_ohlcv([
        row[:6] for row in data
    ])


def get_crypto_klines(symbol, timeframe):
    providers = [
        get_okx_klines,
        get_bybit_klines,
        get_binance_klines
    ]

    for provider in providers:
        try:
            df = provider(symbol, timeframe)
            if df is not None and len(df) >= 220:
                return df
        except Exception as exc:
            log.warning(
                "%s %s failed: %s",
                symbol,
                provider.__name__,
                exc
            )

    return None


# ================= GOLD DATA =================

def gold_twelvedata():
    if not TWELVEDATA_API_KEY:
        return None

    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": "XAU/USD",
            "interval": "1h",
            "outputsize": 5000,
            "apikey": TWELVEDATA_API_KEY
        }
    )

    if not data or "values" not in data:
        log.warning("Twelve Data gold unavailable")
        return None

    try:
        df = pd.DataFrame(data["values"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if "volume" not in df.columns:
            df["volume"] = 0
        else:
            df["volume"] = pd.to_numeric(
                df["volume"],
                errors="coerce"
            )

        df["timestamp"] = pd.to_datetime(
            df["datetime"],
            utc=True
        )

        df = df.dropna()
        df = df.sort_values("timestamp")
        df = df.set_index("timestamp")

        return df[["open", "high", "low", "close", "volume"]]

    except Exception as exc:
        log.warning("Twelve Data conversion failed: %s", exc)
        return None


def gold_yahoo():
    # GC=F is COMEX gold futures, NOT spot XAU/USD.
    data = get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
        {
            "range": "60d",
            "interval": "1h"
        }
    )

    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(
                timestamps,
                unit="s",
                utc=True
            ),
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": quote["close"],
            "volume": quote["volume"]
        })

        df = df.dropna()
        df = df.set_index("timestamp")

        if len(df) < 220:
            return None

        return df

    except Exception as exc:
        log.warning("Yahoo gold unavailable: %s", exc)
        return None


def get_gold_data():
    gold = gold_twelvedata()

    if gold is not None:
        return gold, "XAU/USD"

    gold = gold_yahoo()

    if gold is not None:
        return gold, "GC=F COMEX FUTURES PROXY"

    return None, None


# ================= INDICATORS =================

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

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()

    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    df["volume_avg"] = df["volume"].rolling(20).mean()

    return df


# ================= TREND SCORE =================

def timeframe_score(df):
    if df is None or len(df) < 220:
        return 0, "NONE"

    df = add_indicators(df)

    # Ignore the currently forming candle
    if len(df) < 3:
        return 0, "NONE"

    row = df.iloc[-2]

    if pd.isna(row["ema200"]) or pd.isna(row["rsi"]):
        return 0, "NONE"

    price = row["close"]
    score = 0

    if price > row["ema20"]:
        score += 1
    if price > row["ema50"]:
        score += 1
    if price > row["ema200"]:
        score += 1
    if row["ema20"] > row["ema50"]:
        score += 1
    if row["macd"] > row["macd_signal"]:
        score += 1

    if row["rsi"] > 50:
        score += 1

    if row["rsi"] > 70:
        score -= 1

    if row["rsi"] < 30:
        score += 0

    if score >= 4:
        direction = "BUY"
    elif score <= 2:
        direction = "SELL"
    else:
        direction = "NEUTRAL"

    return score, direction


# ================= SIGNAL BUILDER =================

def build_signal(
    market,
    symbol,
    direction,
    score,
    entry_df,
    source=""
):
    df = add_indicators(entry_df)

    row = df.iloc[-2]

    entry = float(row["close"])
    atr = float(row["atr"])

    if not np.isfinite(atr) or atr <= 0:
        return None

    if direction == "BUY":
        stop = entry - (1.5 * atr)
        risk = entry - stop
        target1 = entry + (1.5 * risk)
        target2 = entry + (2.0 * risk)
        target3 = entry + (3.0 * risk)

    else:
        stop = entry + (1.5 * atr)
        risk = stop - entry
        target1 = entry - (1.5 * risk)
        target2 = entry - (2.0 * risk)
            if direction == "BUY":
        aligned_total = score1 + score15 + score5
    else:
        aligned_total = (6 - score1) + (6 - score15) + (6 - score5)

    total_score = round((aligned_total / 18) * 10)

    if total_score < MIN_SCORE:
        log.info("%s %s skipped: score %s/10", symbol, direction, total_score)
        return None
        f"<b>Direction:</b> {direction}\n"
        f"<b>Score:</b> {score}/10\n"
        f"<b>Entry:</b> {entry:.6g}\n"
        f"<b>Stop Loss:</b> {stop:.6g}\n"
        f"<b>Target 1:</b> {target1:.6g}\n"
        f"<b>Target 2:</b> {target2:.6g}\n"
        f"<b>Target 3:</b> {target3:.6g}\n"
        f"<b>RSI:</b> {row['rsi']:.2f}\n"
        f"<b>ATR:</b> {atr:.6g}\n"
    )

    if source:
        message += f"<b>Data:</b> {source}\n"

    message += (
        "━━━━━━━━━━━━━━━━\n"
        "<i>Research alert only. Not a guaranteed trade.</i>"
    )

    return message


# ================= CRYPTO ANALYSIS =================

def evaluate_crypto(symbol):
    df1h = get_crypto_klines(symbol, "1h")
    df15m = get_crypto_klines(symbol, "15m")
    df5m = get_crypto_klines(symbol, "5m")

    if any(x is None for x in [df1h, df15m, df5m]):
        log.info("Skipping %s: candle data unavailable", symbol)
        return None

    score1, dir1 = timeframe_score(df1h)
        if direction == "BUY":
        aligned_total = score4h + score1h
    else:
        aligned_total = (6 - score4h) + (6 - score1h)

        if direction == "BUY":
        aligned_total = score4h + score1h
    else:
        aligned_total = (6 - score4h) + (6 - score1h)

    total_score = round((aligned_total / 12) * 10)

    if total_score < MIN_SCORE:
        log.info("GOLD %s skipped: score %s/10", direction, total_score)
        return None
        direction = "SELL"
    else:
        return None

    total_score = round(
        (score1 + score15 + score5) / 18 * 10
    )

    if total_score < MIN_SCORE:
        return None

    return build_signal(
        "CRYPTO",
        symbol,
        direction,
        total_score,
        df5m
    )


# ================= GOLD ANALYSIS =================

def evaluate_gold():
    df1h, source = get_gold_data()

    if df1h is None:
        log.warning("Gold data unavailable")
        return None

    df4h = df1h.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()

    if len(df1h) < 220 or len(df4h) < 220:
        log.warning("Not enough gold candle history")
        return None

    score4h, dir4h = timeframe_score(df4h)
    score1h, dir1h = timeframe_score(df1h)

    if dir4h == "BUY" and dir1h == "BUY":
        direction = "BUY"
    elif dir4h == "SELL" and dir1h == "SELL":
        direction = "SELL"
    else:
        return None

    total_score = round(
        (score4h + score1h) / 12 * 10
    )

    if total_score < MIN_SCORE:
        return None

    return build_signal(
        "GOLD",
        "XAUUSD",
        direction,
        total_score,
        df1h,
        source
    )


# ================= MAIN SCAN =================

def run_scan():
    log.info("Starting Supreme Crypto + Gold scan")

    state = load_state()

    # GOLD
    try:
        gold_message = evaluate_gold()

        if gold_message:
            key = "GOLD"
            if state.get(key) != gold_message:
                if send_telegram(gold_message):
                    state[key] = gold_message

    except Exception:
        log.exception("Gold analysis error")

    # CRYPTO
    try:
        symbols = get_symbols()
        log.info("Symbols received: %s", len(symbols))

        for symbol in symbols:
            try:
                message = evaluate_crypto(symbol)

                if not message:
                    continue

                key = "CRYPTO_" + symbol

                if state.get(key) != message:
                    if send_telegram(message):
                        state[key] = message

                time.sleep(1)

            except Exception:
                log.exception("Crypto error: %s", symbol)

    except Exception:
        log.exception("Crypto scan error")

    save_state(state)
    log.info("Scan completed")


# ================= MAIN =================

def main():
    log.info("SUPREME CRYPTO + GOLD BOT STARTED")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return

    run_scan()


if __name__ == "__main__":
    main()
