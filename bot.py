
"""
SUPREME CRYPTO + GOLD SIGNAL BOT
Signal-only Telegram alerts.
No automatic trading or order placement.
Python 3.11+
"""

import os
import time
import json
import logging
import html
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import feedparser

# ---------------- CONFIGURATION ----------------

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


# ---------------- HTTP REQUEST ----------------

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
        log.warning("Request failed: %s | %s", url, exc)
        return None


# ---------------- STATE MANAGEMENT ----------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
    except Exception as exc:
        log.warning("State save failed: %s", exc)


# ---------------- TELEGRAM ----------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram secrets are missing.")
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
            log.error("Telegram rejected message: %s", result)
            return False

        return True

    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False
