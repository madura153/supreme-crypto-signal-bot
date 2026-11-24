# SUPREME CRYPTO + GOLD SIGNAL BOT

Signal-only research assistant. It does **not** place orders and does not need exchange API keys.

## Markets and providers
- Crypto perpetual USDT markets: Bybit public API first, OKX fallback, Binance Futures fallback.
- Gold: Twelve Data XAU/USD if `TWELVEDATA_API_KEY` is configured; otherwise Yahoo `GC=F` COMEX gold futures proxy. The fallback is **not spot XAU/USD** and is labelled accordingly.
- Gold uses real 1-hour candles aggregated into 4-hour candles. It does not invent 5m/15m candles.

## Strategy
- Crypto: 1h + 15m trend confirmation, 5m entry confirmation.
- Gold: 4h + 1h trend confirmation.
- EMA20/50/200, RSI, MACD, ATR, relative volume.
- Closed candles only; ATR/swing-based stop and 1.5R/2R/3R targets.
- Signals are alerts, not probability estimates or guaranteed trades.
- Cooldown state is stored in a GitHub Actions artifact; artifact restore is best-effort.

## Required GitHub Actions secrets
Repository → Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional:
- `TWELVEDATA_API_KEY` for an XAU/USD spot feed. Without it, Yahoo GC=F futures proxy is used.

Never paste tokens into source code or commit them.

## Telegram setup
1. Create a bot with `@BotFather` and copy the token.
2. Open your bot chat and press Start.
3. Obtain your numeric chat ID (for a private bot, use `@userinfobot` or Telegram `getUpdates` after sending the bot a message).
4. Add the two required secrets in GitHub.

## Run
GitHub → Actions → Supreme Crypto Gold Signal Bot → Run workflow.
It also runs on a 15-minute schedule. GitHub scheduled jobs are not guaranteed to execute exactly on the minute.

## Important limitations
- A 451/403 response may mean a provider is restricted from the runner's network. The bot tries other public providers, but if all fail it skips that market and logs the reason.
- Public endpoints can change, rate-limit, or be unavailable.
- This is not a validated profitable strategy. Backtest and paper-track signals before risking money.
- Suggested `RISK_PERCENT` is only a planning reference; the bot does not know account balance, calculate a quantity, or manage a position.
- Verify contract type, funding, spread, tick size, market hours and current quote yourself before trading.
