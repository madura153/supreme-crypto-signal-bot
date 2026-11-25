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
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    ROUND_CEILING,
    ROUND_HALF_UP
)

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

MAX_ENTRY_DRIFT_ATR = 0.75
MAX_ENTRY_DRIFT_PCT = 0.005

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("SUPREME-BOT")

session = requests.Session()
session.headers.update({
    "User-Agent": "SupremeCryptoGoldSignalBot/4.0"
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


# ================= SIGNAL FINGERPRINT =================

def signal_fingerprint(message):
    """
    Prevent duplicate alerts for the same market,
    pair, direction and completed candle.
    """

    fields = {}

    labels = (
        "MARKET",
        "TRADING PAIR",
        "DIRECTION",
        "CANDLE"
    )

    for line in message.splitlines():
        for label in labels:
            marker = f"<b>{label}:</b>"

            if marker in line:
                fields[label] = line.split(
                    marker, 1
                )[1].strip()

    required = (
        "MARKET",
        "TRADING PAIR",
        "DIRECTION",
        "CANDLE"
    )

    if not all(fields.get(label) for label in required):
        log.warning(
            "Could not create signal fingerprint. "
            "Required message fields are missing."
        )
        return ""

    return "|".join(
        fields[label] for label in required
    )


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


# ================= TRADING PAIR DISPLAY =================

def get_trade_identity(market, symbol, source=""):
    """
    Separate the base coin and quote currency for Telegram display.
    """

    symbol = str(symbol).strip().upper()
    market = str(market).strip().upper()

    if market == "GOLD":
        return {
            "asset": "GOLD (XAU)",
            "quote": "USD",
            "pair": "XAU / USD",
            "contract": (
                "GOLD FUTURES PROXY"
                if "GC=F" in str(source).upper()
                else "GOLD SPOT DATA"
            )
        }

    if symbol.endswith("-USDT-SWAP"):
        base = symbol[:-10]
        quote = "USDT"

    elif symbol.endswith("USDT"):
        base = symbol[:-4]
        quote = "USDT"

    else:
        base = symbol
        quote = "UNKNOWN"

    return {
        "asset": base,
        "quote": quote,
        "pair": f"{base} / {quote}",
        "contract": (
            "USDT PERPETUAL SWAP"
            if quote == "USDT"
            else "EXCHANGE CONTRACT"
        )
    }


# ================= SYMBOL LIST =================

def get_symbols():
    symbols = []

    # OKX
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

    symbols = list(dict.fromkeys(symbols))

    # Always include XRP
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
    if rows is None or len(rows) == 0:
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
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df["timestamp"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"], errors="coerce"),
            unit="ms",
            utc=True
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

        df["volume"] = df["volume"].fillna(0)

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


# ================= LIVE PRICE + EXCHANGE TICK SIZE =================

def get_live_price(symbol, provider):
    """
    Return live last price and quote timestamp
    from the same exchange used for candle data.
    """

    market = exchange_symbol(symbol, provider)

    if provider == "OKX":
        data = get_json(
            OKX_URL + "/api/v5/market/ticker",
            {"instId": market}
        )

        if data and data.get("code") == "0" and data.get("data"):
            item = data["data"][0]

            try:
                return float(item["last"]), item.get("ts", "")
            except (KeyError, TypeError, ValueError):
                return None, ""

    elif provider == "BYBIT":
        data = get_json(
            BYBIT_URL + "/v5/market/tickers",
            {
                "category": "linear",
                "symbol": market
            }
        )

        if data and data.get("retCode") == 0:
            items = data.get("result", {}).get("list", [])

            if items:
                try:
                    return (
                        float(items[0]["lastPrice"]),
                        str(data.get("time", ""))
                    )
                except (KeyError, TypeError, ValueError):
                    return None, ""

    elif provider == "BINANCE":
        data = get_json(
            BINANCE_URL + "/fapi/v1/ticker/price",
            {"symbol": market}
        )

        if data and data.get("price") is not None:
            try:
                return (
                    float(data["price"]),
                    str(data.get("time", ""))
                )
            except (TypeError, ValueError):
                return None, ""

    return None, ""


def get_price_tick_size(symbol, provider):
    """
    Read the instrument's actual price tick from exchange metadata.
    """

    market = exchange_symbol(symbol, provider)

    if provider == "OKX":
        data = get_json(
            OKX_URL + "/api/v5/public/instruments",
            {
                "instType": "SWAP",
                "instId": market
            }
        )

        if data and data.get("code") == "0" and data.get("data"):
            return data["data"][0].get("tickSz")

    elif provider == "BYBIT":
        data = get_json(
            BYBIT_URL + "/v5/market/instruments-info",
            {
                "category": "linear",
                "symbol": market
            }
        )

        if data and data.get("retCode") == 0:
            items = data.get("result", {}).get("list", [])

            if items:
                return items[0].get(
                    "priceFilter", {}
                ).get("tickSize")

    elif provider == "BINANCE":
        data = get_json(
            BINANCE_URL + "/fapi/v1/exchangeInfo"
        )

        if data and isinstance(data.get("symbols"), list):
            for item in data["symbols"]:
                if item.get("symbol") == market:
                    for filt in item.get("filters", []):
                        if filt.get("filterType") == "PRICE_FILTER":
                            return filt.get("tickSize")

    return None


def round_to_tick(value, tick_size, rounding):
    value_dec = Decimal(str(value))
    tick_dec = Decimal(str(tick_size))

    if tick_dec <= 0:
        raise ValueError("Invalid exchange tick size")

    return (
        value_dec / tick_dec
    ).to_integral_value(rounding=rounding) * tick_dec


def format_tick_price(value, tick_size):
    tick_dec = Decimal(str(tick_size))

    rounded = round_to_tick(
        value,
        tick_dec,
        ROUND_HALF_UP
    )

    decimals = max(
        0,
        -tick_dec.normalize().as_tuple().exponent
    )

    return f"{rounded:.{decimals}f}"


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

        for col in ["open", "high", "low", "close", "volume"]:
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

    rsi = 100 - (100 / (1 + rs))

    # Handle zero-loss and flat-market cases
    rsi = rsi.mask(
        (avg_loss == 0) & (avg_gain > 0),
        100
    )

    rsi = rsi.mask(
        (avg_loss == 0) & (avg_gain == 0),
        50
    )

    df["rsi"] = rsi

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

        # Last completed candle
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
    source="",
    live_price=None,
    quote_time="",
    tick_size=None
):
    if entry_df is None or len(entry_df) < 220:
        return None

    try:
        df = add_indicators(entry_df)

        row = df.iloc[-2]

        candle_close = float(row["close"])

        entry = (
            float(live_price)
            if live_price is not None
            else candle_close
        )

        atr = float(row["atr"])
        rsi = float(row["rsi"])

        if not np.isfinite(entry):
            return None

        if not np.isfinite(atr) or atr <= 0:
            return None

        if not np.isfinite(rsi):
            return None

        if market == "CRYPTO":
            if not tick_size:
                log.warning(
                    "%s skipped: no verified exchange tick size",
                    symbol
                )
                return None

            drift = abs(entry - candle_close)

            allowed_drift = min(
                MAX_ENTRY_DRIFT_ATR * atr,
                MAX_ENTRY_DRIFT_PCT * candle_close
            )

            if drift > allowed_drift:
                log.info(
                    "%s skipped: live quote drift %.8f exceeds %.8f",
                    symbol,
                    drift,
                    allowed_drift
                )
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

        if market == "CRYPTO":
            try:
                if direction == "BUY":
                    stop = float(
                        round_to_tick(
                            stop,
                            tick_size,
                            ROUND_FLOOR
                        )
                    )

                    target1 = float(
                        round_to_tick(
                            target1,
                            tick_size,
                            ROUND_CEILING
                        )
                    )

                    target2 = float(
                        round_to_tick(
                            target2,
                            tick_size,
                            ROUND_CEILING
                        )
                    )

                    target3 = float(
                        round_to_tick(
                            target3,
                            tick_size,
                            ROUND_CEILING
                        )
                    )

                else:
                    stop = float(
                        round_to_tick(
                            stop,
                            tick_size,
                            ROUND_CEILING
                        )
                    )

                    target1 = float(
                        round_to_tick(
                            target1,
                            tick_size,
                            ROUND_FLOOR
                        )
                    )

                    target2 = float(
                        round_to_tick(
                            target2,
                            tick_size,
                            ROUND_FLOOR
                        )
                    )

                    target3 = float(
                        round_to_tick(
                            target3,
                            tick_size,
                            ROUND_FLOOR
                        )
                    )

                entry = float(
                    round_to_tick(
                        entry,
                        tick_size,
                        ROUND_HALF_UP
                    )
                )

            except (
                InvalidOperation,
                ValueError,
                TypeError
            ) as exc:
                log.warning(
                    "Price rounding failed for %s: %s",
                    symbol,
                    exc
                )
                return None

            if direction == "BUY" and not (
                stop < entry < target1 < target2 < target3
            ):
                log.warning(
                    "%s skipped: invalid rounded BUY levels",
                    symbol
                )
                return None

            if direction == "SELL" and not (
                stop > entry > target1 > target2 > target3
            ):
                log.warning(
                    "%s skipped: invalid rounded SELL levels",
                    symbol
                )
                return None

        def price_text(value):
            if market == "CRYPTO" and tick_size:
                return format_tick_price(value, tick_size)

            return (
                f"{value:.2f}"
                if market == "GOLD"
                else f"{value:.6g}"
            )

        candle_time = row.name.isoformat()

        identity = get_trade_identity(
            market,
            symbol,
            source
        )

        if market == "GOLD":
            identity_section = (
                f"🪙 <b>ASSET:</b> {escape(identity['asset'])}\n"
                f"💵 <b>QUOTE CURRENCY:</b> {escape(identity['quote'])}\n"
                f"💱 <b>TRADING PAIR:</b> {escape(identity['pair'])}\n"
                f"📌 <b>INSTRUMENT:</b> {escape(identity['contract'])}\n"
            )

        else:
            identity_section = (
                f"🪙 <b>COIN:</b> {escape(identity['asset'])}\n"
                f"💵 <b>QUOTE CURRENCY:</b> {escape(identity['quote'])}\n"
                f"💱 <b>TRADING PAIR:</b> {escape(identity['pair'])}\n"
                f"📌 <b>CONTRACT:</b> {escape(identity['contract'])}\n"
            )

        message = (
            "🚨 <b>SUPREME SIGNAL</b> 🚨\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{identity_section}"
            f"📊 <b>MARKET:</b> {escape(str(market))}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>DIRECTION:</b> {'🟢 BUY' if direction == 'BUY' else '🔴 SELL'}\n"
            f"⭐ <b>SIGNAL SCORE:</b> {score}/10\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>LIVE ENTRY PRICE:</b> {price_text(entry)}\n"
            f"🛑 <b>STOP LOSS:</b> {price_text(stop)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>TAKE PROFIT TARGETS</b>\n"
            f"🥇 <b>TARGET 1:</b> {price_text(target1)}\n"
            f"🥈 <b>TARGET 2:</b> {price_text(target2)}\n"
            f"🥉 <b>TARGET 3:</b> {price_text(target3)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>RSI:</b> {rsi:.2f}\n"
            f"📉 <b>ATR:</b> {atr:.8g}\n"
            + (
                f"📍 <b>5M CANDLE CLOSE:</b> {price_text(candle_close)}\n"
                if market == "CRYPTO"
                else ""
            )
            + (
                f"🕘 <b>LIVE QUOTE TIME (ms):</b> {escape(str(quote_time))}\n"
                if quote_time
                else ""
            )
            + f"🕒 <b>CANDLE:</b> {escape(candle_time)}\n"
        )

        if source:
            message += (
                f"📡 <b>DATA SOURCE:</b> "
                f"{escape(str(source))}\n"
            )

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>Signal only. Not financial advice.</i>\n"
            "❌ <i>No automatic trading or order placement.</i>\n"
            "⚠️ <i>No guaranteed profit.</i>"
        )

        return message

    except Exception as exc:
        log.warning("Signal building failed: %s", exc)
        return None


# ================= CRYPTO ANALYSIS =================

def evaluate_crypto(symbol):
    # Use one exchange for every timeframe to avoid mixing price feeds.
    providers = [
        ("OKX", get_okx_klines),
        ("BYBIT", get_bybit_klines),
        ("BINANCE", get_binance_klines)
    ]

    selected_frames = None
    selected_provider = None

    for provider_name, provider_func in providers:
        frames = {}

        for timeframe in TIMEFRAMES:
            try:
                df = provider_func(symbol, timeframe)

            except Exception as exc:
                log.warning(
                    "%s failed for %s %s: %s",
                    provider_name,
                    symbol,
                    timeframe,
                    exc
                )
                df = None

            if df is None or len(df) < 220:
                break

            frames[timeframe] = df

        if len(frames) == len(TIMEFRAMES):
            selected_frames = frames
            selected_provider = provider_name
            break

    if selected_frames is None:
        log.warning(
            "No single exchange supplied all timeframes for %s",
            symbol
        )
        return None

    score1, dir1 = timeframe_score(
        selected_frames["1h"]
    )

    score15, dir15 = timeframe_score(
        selected_frames["15m"]
    )

    score5, dir5 = timeframe_score(
        selected_frames["5m"]
    )

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

    # Get live quote and tick size from the same exchange
    live_price, quote_time = get_live_price(
        symbol,
        selected_provider
    )

    tick_size = get_price_tick_size(
        symbol,
        selected_provider
    )

    if live_price is None or live_price <= 0:
        log.warning(
            "%s skipped: live quote unavailable on %s",
            symbol,
            selected_provider
        )
        return None

    if not tick_size:
        log.warning(
            "%s skipped: exchange tick size unavailable on %s",
            symbol,
            selected_provider
        )
        return None

    return build_signal(
        "CRYPTO",
        symbol,
        dir1,
        total_score,
        selected_frames["5m"],
        f"{selected_provider} LIVE QUOTE",
        live_price=live_price,
        quote_time=quote_time,
        tick_size=tick_size
    )


# ================= GOLD ANALYSIS =================

def evaluate_gold():
    df1h, source = get_gold_data()

    if df1h is None or len(df1h) < 220:
        log.warning(
            "Gold analysis skipped: insufficient data"
        )
        return None

    try:
        df4h = df1h.resample("4h").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna(
            subset=["open", "high", "low", "close"]
        )

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

def process_signal(state, key, message):
    if not message:
        return

    fingerprint = signal_fingerprint(message)

    if not fingerprint:
        log.warning(
            "Signal not processed because fingerprint is invalid: %s",
            key
        )
        return

    if state.get(key) == fingerprint:
        log.info(
            "Duplicate candle signal skipped: %s",
            key
        )
        return

    if send_telegram(message):
        state[key] = fingerprint


def run_scan():
    log.info("Starting Supreme Crypto + Gold scan")

    state = load_state()

    # GOLD
    try:
        gold_message = evaluate_gold()

        if gold_message:
            process_signal(
                state,
                "GOLD",
                gold_message
            )

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

                if message:
                    process_signal(
                        state,
                        "CRYPTO_" + symbol,
                        message
                    )

            except Exception:
                log.exception(
                    "Crypto analysis error: %s",
                    symbol
                )

            time.sleep(1)

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
