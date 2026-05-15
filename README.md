# Polymarket Trading Bot

A multi-strategy trading bot for **Polymarket** that combines **BTC 5-minute Up/Down**
markets with **bucketed weather temperature** markets across US, Chinese, and European
cities. Real-time React dashboard. Runs as **simulation** by default; flip one env var to
trade with real money.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![React](https://img.shields.io/badge/react-18+-61DAFB) ![TypeScript](https://img.shields.io/badge/typescript-5.0+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

![Dashboard](docs/dashboard.png)

## What it does

| Strategy | How it works |
|---|---|
| **BTC 5-min Up/Down** | Every 60 s, computes RSI / momentum / VWAP / SMA / market-skew on 1-min candles. Trades the Polymarket 5-min market when its weighted composite signal disagrees with the market by ≥ 2 %. |
| **Weather temperature buckets** | Every 5 min, fetches Polymarket's "Highest/Lowest temperature in CITY on DATE" markets (the bucketed °C and °F series). Pulls a 31-member GFS ensemble forecast from Open-Meteo and computes the probability of each bucket. Trades the bucket with the biggest expected-value mispricing when edge ≥ 8 %. |

Both strategies share the same Kelly-criterion sizing, daily-loss circuit breaker, and
Polymarket settlement path.

## Live or simulated

| Mode | When | Bankroll source |
|---|---|---|
| **Simulation** (default) | `SIMULATION_MODE=true` OR any Polymarket cred missing | DB value (`INITIAL_BANKROLL`) |
| **Live** | `SIMULATION_MODE=false` + all 5 Polymarket creds set | Your Polymarket USDC wallet balance (read on-chain) |

Live mode requires manual account setup — see **[SETUP_LIVE.md](SETUP_LIVE.md)** for the
step-by-step. Simulation mode runs without any keys.

## Quick start (simulation)

```bash
# 1. Backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # default config is sim-mode + sensible caps
uvicorn backend.api.main:app --reload --port 8000

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The header badge should say **SIM** in amber.

## Configuration (`.env`)

Every knob lives in `.env`. Defaults are tuned for a $1k sim bankroll.

### Mode + bankroll
| Var | Default | Notes |
|---|---|---|
| `SIMULATION_MODE` | `true` | `false` activates live trading (also needs Polymarket creds) |
| `INITIAL_BANKROLL` | `1000` | Sim only; in live mode the bot reads real USDC balance instead |
| `KELLY_FRACTION` | `0.15` | Fractional Kelly multiplier |
| `MAX_TRADE_BANKROLL_FRACTION` | `0.05` | Hard cap per trade = 5 % of bankroll |
| `MAX_TRADE_SIZE` | `5000` | Absolute dollar cap (binds at high bankroll) |
| `DAILY_LOSS_FRACTION` | `0.25` | Halt new trades when day's settled losses ≥ 25 % of start-of-day bankroll |

### Per-market trading switches
Toggle each strategy independently. **Scans + dashboard display continue** regardless
— only trade *placement* is gated.

| Var | Default | Notes |
|---|---|---|
| `BTC_TRADING_ENABLED` | `true` | Disable BTC orders, keep BTC panel populated |
| `WEATHER_TRADING_ENABLED` | `true` | Disable weather orders, keep weather panel populated |

### BTC strategy
| Var | Default | Notes |
|---|---|---|
| `MIN_EDGE_THRESHOLD` | `0.02` | 2 % edge required (BTC is near-coinflip) |
| `MAX_ENTRY_PRICE` | `0.55` | Don't pay > 55 c/share |
| `BTC_MAX_TRADES_PER_SCAN` | `10` | Max new BTC trades per 1-min scan |
| `BTC_MAX_TOTAL_ALLOCATION` | `1000` | Stop opening once open BTC notional hits this |

### Weather strategy
| Var | Default | Notes |
|---|---|---|
| `WEATHER_ENABLED` | `true` | Master enable (scanning + trading) |
| `WEATHER_MIN_EDGE_THRESHOLD` | `0.08` | 8 % edge required |
| `WEATHER_MAX_ENTRY_PRICE` | `0.70` | Don't pay > 70 c/share |
| `WEATHER_MIN_VOLUME` | `1000` | Skip illiquid buckets (< $1 k lifetime volume) |
| `WEATHER_MAX_TRADES_PER_SCAN` | `10` | Max new weather trades per 5-min scan |
| `WEATHER_MAX_TOTAL_ALLOCATION` | `10000` | Stop opening once open weather notional hits this |
| `WEATHER_CITIES` | 26-city list | US + China/HK + Europe, comma-separated |

### Polymarket live trading
See **[SETUP_LIVE.md](SETUP_LIVE.md)** for how to obtain these.

| Var | Notes |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Signer key (treat like cash) |
| `POLYMARKET_FUNDER_ADDRESS` | Path A only: your Polymarket proxy address (the one holding USDC) |
| `POLYMARKET_API_KEY` / `_SECRET` / `_PASSPHRASE` | CLOB API credentials |
| `LIVE_TRADE_MAX_USD` | Hard cap per live order (default `5`) |
| `LIVE_TRADE_DAILY_USD_LIMIT` | Halt live orders when today's notional hits this (default `25`) |

## Cities covered

**US (7):** NYC, Chicago, Miami, Los Angeles, Austin, Atlanta, Seattle
**China + HK (8):** Beijing, Shanghai, Chongqing, Guangzhou, Chengdu, Wuhan, Hong Kong, Shenzhen
**Europe (11):** London, Paris, Madrid, Milan, Munich, Amsterdam, Warsaw, Helsinki, Moscow, Istanbul, Ankara

Edit `WEATHER_CITIES` in `.env` to enable/disable specific cities. New cities also need a
lat/lon entry in `backend/data/weather.py:CITY_CONFIG` and a globe-marker entry in
`frontend/src/components/GlobeView.tsx:CITIES`.

## How it works

### BTC 5-min strategy
1. Pull 60 one-minute BTC candles (Coinbase → Kraken → Binance fallback chain).
2. Compute RSI(14), momentum (1m / 5m / 15m), VWAP deviation, SMA crossover, market skew.
3. Weighted composite → model UP probability (clipped to 0.35–0.65).
4. Compare to Polymarket's UP / DOWN price, pick the higher-edge side.
5. If `|edge| ≥ 2 %` and entry price ≤ 55 c, place trade sized via fractional Kelly.

### Weather bucket strategy
1. Pull all Polymarket "Highest/Lowest temperature in CITY on DATE" events
   (`/events?tag_slug=weather`, paginated).
2. Parse 4 bucket shapes per event:
   - `equality` — e.g. "be 28°C" (1°C window)
   - `floor` — "be 25°C or below"
   - `ceiling` — "be 35°C or higher"
   - `range` — "between 56-57°F" (2°F window)
3. For each bucket, count fraction of ensemble members whose daily max/min falls in
   that range → model probability.
4. Pick the single biggest-edge bucket per (city, date) event to avoid double-betting.
5. If edge ≥ 8 %, entry price ≤ 70 c, and bucket volume ≥ $1 k → place trade.

### Edge & sizing
```
edge      = model_probability − market_probability
shares    = trade_usd / price
kelly     = (p × b − q) / b   where b = (1 − price) / price
trade_usd = min(
    kelly × KELLY_FRACTION × bankroll,    # fractional Kelly
    MAX_TRADE_BANKROLL_FRACTION × bankroll, # per-trade fraction cap
    MAX_TRADE_SIZE                         # absolute dollar cap
)
```

## CSV export (tax-ready)

```
GET /api/trades/export.csv          # settled trades only
GET /api/trades/export.csv?include_pending=true
```

21 columns covering Form 8949 / Schedule D essentials: entry/exit timestamps in UTC,
cost basis, proceeds, realized P&L, holding period, model-vs-market at entry. Click the
**Export CSV** button on the Trades panel header to download.

## Useful API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/dashboard` | All dashboard data in one call |
| `GET /api/stats` | Bankroll, P&L, mode (sim/live), pending count |
| `GET /api/btc/windows` | Active BTC 5-min markets |
| `GET /api/weather/forecasts` | Ensemble forecasts for all configured cities |
| `GET /api/weather/markets` | Weather markets currently in scope |
| `GET /api/trades?limit=50` | Recent trades |
| `GET /api/calibration` | Model calibration (predicted vs realized win rate) |
| `POST /api/run-scan` | Trigger manual scan (BTC + weather) |
| `POST /api/bot/start` | Start automatic trading |
| `POST /api/bot/stop` | Pause |
| `POST /api/bot/reset` | Reset all trades |

## Data sources

| Source | Data | Auth |
|---|---|---|
| Coinbase / Kraken / Binance | BTC 1-min candles (fallback chain) | None |
| Open-Meteo Ensemble | 31-member GFS daily max/min | None |
| Polymarket Gamma API | Markets + resolution | None for reads |
| Polymarket CLOB | Order placement (live mode only) | API key + wallet |

100 % free for simulation. For live trading you need USDC on Polygon (a few cents in gas).

## Project structure

```
.
├── backend/
│   ├── api/main.py                     FastAPI routes + dashboard endpoint
│   ├── core/
│   │   ├── signals.py                  BTC signal generation
│   │   ├── weather_signals.py          Weather signal generation (bucketed)
│   │   ├── scheduler.py                BTC + weather background jobs, live/sim routing
│   │   └── settlement.py               Trade settlement (Polymarket resolution)
│   ├── data/
│   │   ├── btc_markets.py              Polymarket BTC market fetcher
│   │   ├── weather.py                  Open-Meteo ensemble + city config
│   │   ├── weather_markets.py          Polymarket weather bucket parser
│   │   ├── polymarket_trader.py        Live-trading client (py-clob-client)
│   │   └── crypto.py                   BTC price + microstructure indicators
│   ├── models/database.py              SQLAlchemy (Trade, Signal, BotState)
│   └── config.py                       All settings
├── frontend/
│   └── src/                            React + TanStack Query + Tailwind
├── .env.example                        Reference config
├── SETUP_LIVE.md                       Live trading setup walkthrough
├── ARCHITECTURE.md                     Deeper design doc
├── TRADING_STRATEGY.md                 Strategy notes
└── README.md
```

## Disclaimer

Simulation mode trades nothing real. Live mode trades real money — read
`SETUP_LIVE.md` and start with tiny caps. The bot's edges are *hypotheses backed by
capital*, not proven returns. Past performance ≠ future performance. Prediction markets
involve risk of total loss on each trade.

## License

MIT.
