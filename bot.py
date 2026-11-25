
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
from html import escape

import requests
import pandas as pd
import numpy as np


# ================= CONFIG =================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

MIN_SCORE = 7
MAX_SYMBOLS = 25
STATE_FILE = "bot_state.json"

OKX_URL = "https://www.okx.com"
BYBIT_URL = "https://api.bybit.com"
BINANCE_URL = "https://fapi.binance.com"

TIMEFRAMES = ["1h", "15m", "5m"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("SUPREME-BOT")

session = requests.Session()
session.headers.update({
    "User-Agent": "SupremeCryptoGoldSignalBot/3.0"
})


# ================= HTTP =================

def get_json(url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=25
        )
        response.raise_for_status()
        return response.json()

    except Exception as exc:
        log.warning("Request failed: %s | %s", url, exc)
        return None


# ================= STATE =================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        return state if isinstance(state, dict) else {}

    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)

    except Exception as exc:
        log.warning("State save failed: %s", exc)


# ================= TELEGRAM =================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram secrets are missing")
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
            timeout=25
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            log.error("Telegram API error: %s", result)
            return False

        log.info("Telegram message sent")
        return True

    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False


# ================= SYMBOL CONVERSION =================

def exchange_symbol(symbol, provider):
    symbol = str(symbol).strip()

    if provider == "OKX":
        if symbol.endswith("-USDT-SWAP"):
            return symbol

        if symbol.endswith("USDT"):
            return symbol[:-4] + "-USDT-SWAP"

        return symbol

    if symbol.endswith("-USDT-SWAP"):
        return symbol[:-10] + "USDT"

    return symbol


# ================= SYMBOL LIST =================

def get_symbols():
    symbols = []

    # OKX FIRST
    data = get_json(
        OKX_URL + "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )

    if data and data.get("code") == "0":
        tickers = data.get("data", [])

        tickers = sorted(
            tickers,
            key=lambda item: float(
                item.get("volCcy24h", 0) or 0
            ),
            reverse=True
        )

        for item in tickers:
            symbol = item.get("instId", "")

            if symbol.endswith("-USDT-SWAP"):
                symbols.append(symbol)

    # BYBIT FALLBACK
    if not symbols:
        data = get_json(
            BYBIT_URL + "/v5/market/tickers",
            {"category": "linear"}
        )

        if data and data.get("retCode") == 0:
            tickers = data.get("result", {}).get("list", [])

            tickers = sorted(
                tickers,
                key=lambda item: float(
                    item.get("turnover24h", 0) or 0
                ),
                reverse=True
            )

            for item in tickers:
                symbol = item.get("symbol", "")

                if symbol.endswith("USDT"):
                    symbols.append(symbol)

    # BINANCE FALLBACK
    if not symbols:
        data = get_json(
            BINANCE_URL + "/fapi/v1/ticker/24hr"
        )

        if isinstance(data, list):
            tickers = sorted(
                data,
                key=lambda item: float(
                    item.get("quoteVolume", 0) or 0
                ),
                reverse=True
            )

            for item in tickers:
                symbol = item.get("symbol", "")

                if symbol.endswith("USDT"):
                    symbols.append(symbol)

    # REMOVE DUPLICATES
    symbols = list(dict.fromkeys(symbols))

    # ALWAYS INCLUDE XRP
    if symbols and symbols[0].endswith("-USDT-SWAP"):
        xrp = "XRP-USDT-SWAP"
    else:
        xrp = "XRPUSDT"

    if xrp not in symbols:
        symbols.append(xrp)

    symbols = symbols[:MAX_SYMBOLS]

    if xrp not in symbols:
        if symbols:
            symbols[-1] = xrp
        else:
            symbols = [xrp]

    log.info("Selected %s crypto symbols", len(symbols))

    return symbols


# ================= CANDLE NORMALIZATION =================

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

        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df["timestamp"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"], errors="coerce"),
            unit="ms",
            utc=True
        )

        df = df.dropna()
        df = df.sort_values("timestamp")
        df = df.drop_duplicates("timestamp")
        df = df.set_index("timestamp")

        if len(df) < 220:
            return None

        return df[
            ["open", "high", "low", "close", "volume"]
        ]

    except Exception as exc:
        log.warning("Candle normalization failed: %s", exc)
        return None


# ================= OKX CANDLES =================

def get_okx_klines(symbol, timeframe, limit=300):
    market = exchange_symbol(symbol, "OKX")

    bar_map = {
        "5m": "5m",
        "15m": "15m",
        "1h": "1H"
    }

    if timeframe not in bar_map:
        return None

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

    if not rows:
        return None

    return normalize_ohlcv([
        row[:6] for row in rows
    ][::-1])


# ================= BYBIT CANDLES =================

def get_bybit_klines(symbol, timeframe, limit=300):
    market = exchange_symbol(symbol, "BYBIT")

    interval_map = {
        "5m": "5",
        "15m": "15",
        "1h": "60"
    }

    if timeframe not in interval_map:
        return None

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

    if not rows:
        return None

    return normalize_ohlcv([
        row[:6] for row in rows
    ][::-1])


# ================= BINANCE CANDLES =================

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

    if not isinstance(data, list) or not data:
        return None

    return normalize_ohlcv([
        row[:6] for row in data
    ])


# ================= CRYPTO DATA FALLBACK =================

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
                log.info(
                    "%s %s data received from %s",
                    symbol,
                    timeframe,
                    provider.__name__
                )
                return df

        except Exception as exc:
            log.warning(
                "%s failed for %s %s: %s",
                provider.__name__,
                symbol,
                timeframe,
                exc
            )

    log.warning(
        "No candle data available for %s %s",
        symbol,
        timeframe
    )

    return None


# ================= TWELVE DATA GOLD =================

def gold_twelvedata():
    if not TWELVEDATA_API_KEY:
        log.warning("Twelve Data API key missing")
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
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        if "volume" not in df.columns:
            df["volume"] = 0
        else:
            df["volume"] = pd.to_numeric(
                df["volume"],
                errors="coerce"
            ).fillna(0)

        df["timestamp"] = pd.to_datetime(
            df["datetime"],
            utc=True,
            errors="coerce"
        )

        df = df.dropna(
            subset=[
                "timestamp",
                "open",
                "high",
                "low",
                "close"
            ]
        )

        df = df.sort_values("timestamp")
        df = df.drop_duplicates("timestamp")
        df = df.set_index("timestamp")

        df = df[
            ["open", "high", "low", "close", "volume"]
        ]

        if len(df) < 220:
            log.warning("Not enough Twelve Data gold candles")
            return None

        return df

    except Exception as exc:
        log.warning("Twelve Data processing failed: %s", exc)
        return None


# ================= YAHOO GOLD FALLBACK =================

def gold_yahoo():
    data = get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
        {
            "range": "730d",
            "interval": "1h"
        }
    )

    try:
        if not data:
            return None

        chart = data.get("chart", {})
        results = chart.get("result")

        if not results:
            return None

        result = results[0]

        timestamps = result.get("timestamp", [])
        quote_data = result.get(
            "indicators", {}
        ).get("quote", [])

        if not timestamps or not quote_data:
            return None

        quote = quote_data[0]

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(
                timestamps,
                unit="s",
                utc=True
            ),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume")
        })

        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df["volume"] = df["volume"].fillna(0)

        df = df.dropna(
            subset=[
                "timestamp",
                "open",
                "high",
                "low",
                "close"
            ]
        )

        df = df.sort_values("timestamp")
        df = df.drop_duplicates("timestamp")
        df = df.set_index("timestamp")

        df = df[
            ["open", "high", "low", "close", "volume"]
        ]

        if len(df) < 220:
            log.warning("Not enough Yahoo gold candles")
            return None

        return df

    except Exception as exc:
        log.warning("Yahoo gold processing failed: %s", exc)
        return None


# ================= GOLD DATA SELECTOR =================

def get_gold_data():
    gold = gold_twelvedata()

    if gold is not None and len(gold) >= 220:
        log.info("Gold data source: Twelve Data XAU/USD")
        return gold, "XAU/USD"

    gold = gold_yahoo()

    if gold is not None and len(gold) >= 220:
        log.info("Gold data source: Yahoo GC=F proxy")
        return gold, "GC=F COMEX FUTURES PROXY"

    log.error("All gold data sources failed")

    return None, None


# ================= INDICATORS =================

def add_indicators(df):
    df = df.copy()

    close = df["close"]

    # EMA
    df["ema20"] = close.ewm(
        span=20,
        adjust=False
    ).mean()

    df["ema50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    df["ema200"] = close.ewm(
        span=200,
        adjust=False
    ).mean()

    # RSI
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

    # MACD
    ema12 = close.ewm(
        span=12,
        adjust=False
    ).mean()

    ema26 = close.ewm(
        span=26,
        adjust=False
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    # ATR
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

    # Average volume
    df["volume_avg"] = df["volume"].rolling(20).mean()

    return df


# ================= TIMEFRAME SCORE =================

def timeframe_score(df):
    if df is None or len(df) < 220:
        return 0, "NONE"

    try:
        df = add_indicators(df)

        # Use last completed candle
        row = df.iloc[-2]

        required = [
            "close",
            "ema20",
            "ema50",
            "ema200",
            "rsi",
            "macd",
            "macd_signal"
        ]

        if row[required].isna().any():
            return 0, "NONE"

        price = float(row["close"])
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

        # Avoid overbought BUY signals
        if row["rsi"] > 70:
            score -= 1

        # Avoid oversold SELL signals
        if row["rsi"] < 30:
            score += 1

        score = max(0, min(6, score))

        if score >= 4:
            direction = "BUY"

        elif score <= 2:
            direction = "SELL"

        else:
            direction = "NEUTRAL"

        return score, direction

    except Exception as exc:
        log.warning("Timeframe scoring failed: %s", exc)
        return 0, "NONE"


# ================= SIGNAL BUILDER =================

def build_signal(
    market,
    symbol,
    direction,
    score,
    entry_df,
    source=""
):
    if entry_df is None or len(entry_df) < 220:
        return None

    try:
        df = add_indicators(entry_df)

        row = df.iloc[-2]

        entry = float(row["close"])
        atr = float(row["atr"])
        rsi = float(row["rsi"])

        if not np.isfinite(entry):
            return None

        if not np.isfinite(atr) or atr <= 0:
            return None

        if direction == "BUY":
            stop = entry - (1.5 * atr)
            risk = entry - stop

            target1 = entry + (1.5 * risk)
            target2 = entry + (2.0 * risk)
            target3 = entry + (3.0 * risk)

        elif direction == "SELL":
            stop = entry + (1.5 * atr)
            risk = stop - entry

            target1 = entry - (1.5 * risk)
            target2 = entry - (2.0 * risk)
            target3 = entry - (3.0 * risk)

        else:
            return None

        message = (
            "🚨 <b>SUPREME SIGNAL</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"<b>Market:</b> {escape(str(market))}\n"
            f"<b>Symbol:</b> {escape(str(symbol))}\n"
            f"<b>Direction:</b> {direction}\n"
            f"<b>Score:</b> {score}/10\n"
            f"<b>Entry:</b> {entry:.6g}\n"
            f"<b>Stop Loss:</b> {stop:.6g}\n"
            f"<b>Target 1:</b> {target1:.6g}\n"
            f"<b>Target 2:</b> {target2:.6g}\n"
            f"<b>Target 3:</b> {target3:.6g}\n"
            f"<b>RSI:</b> {rsi:.2f}\n"
            f"<b>ATR:</b> {atr:.6g}\n"
        )

        if source:
            message += (
                f"<b>Data Source:</b> "
                f"{escape(str(source))}\n"
            )

        message += (
            "━━━━━━━━━━━━━━━━\n"
            "<i>Signal only. Not financial advice. "
            "No guaranteed profit.</i>"
        )

        return message

    except Exception as exc:
        log.warning("Signal building failed: %s", exc)
        return None


# ================= CRYPTO ANALYSIS =================

def evaluate_crypto(symbol):
    dataframes = {}

    for timeframe in TIMEFRAMES:
        df = get_crypto_klines(symbol, timeframe)

        if df is None:
            log.info(
                "Skipping %s: %s data unavailable",
                symbol,
                timeframe
            )
            return None

        dataframes[timeframe] = df

    score1, dir1 = timeframe_score(dataframes["1h"])
    score15, dir15 = timeframe_score(dataframes["15m"])
    score5, dir5 = timeframe_score(dataframes["5m"])

    # All timeframes must agree
    if not (dir1 == dir15 == dir5):
        return None

    if dir1 not in ["BUY", "SELL"]:
        return None

    if dir1 == "BUY":
        aligned_total = score1 + score15 + score5

    else:
        aligned_total = (
            (6 - score1)
            + (6 - score15)
            + (6 - score5)
        )

    total_score = round(
        (aligned_total / 18) * 10
    )

    if total_score < MIN_SCORE:
        log.info(
            "%s skipped: score %s/10",
            symbol,
            total_score
        )
        return None

    return build_signal(
        "CRYPTO",
        symbol,
        dir1,
        total_score,
        dataframes["5m"]
    )


# ================= GOLD ANALYSIS =================

def evaluate_gold():
    df1h, source = get_gold_data()

    if df1h is None:
        log.warning("Gold analysis skipped: no data")
        return None

    if len(df1h) < 220:
        return None

    try:
        df4h = df1h.resample("4h").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()

        if len(df4h) < 220:
            log.warning(
                "Not enough 4h gold candles: %s",
                len(df4h)
            )
            return None

        score4h, dir4h = timeframe_score(df4h)
        score1h, dir1h = timeframe_score(df1h)

        if dir4h != dir1h:
            return None

        if dir4h not in ["BUY", "SELL"]:
            return None

        if dir4h == "BUY":
            aligned_total = score4h + score1h

        else:
            aligned_total = (
                (6 - score4h)
                + (6 - score1h)
            )

        total_score = round(
            (aligned_total / 12) * 10
        )

        if total_score < MIN_SCORE:
            log.info(
                "GOLD skipped: score %s/10",
                total_score
            )
            return None

        return build_signal(
            "GOLD",
            "XAUUSD",
            dir4h,
            total_score,
            df1h,
            source
        )

    except Exception as exc:
        log.exception("Gold analysis failed: %s", exc)
        return None


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
        log.exception("Gold scan error")

    # CRYPTO
    try:
        symbols = get_symbols()

        if not symbols:
            log.error("No crypto symbols received")

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
                log.exception(
                    "Crypto analysis error: %s",
                    symbol
                )

    except Exception:
        log.exception("Crypto scan error")

    save_state(state)

    log.info("Supreme scan completed")


# ================= MAIN =================

def main():
    log.info("SUPREME CRYPTO + GOLD BOT STARTED")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )
        return

    run_scan()


if __name__ == "__main__":
    main()
