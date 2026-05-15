# Trading Strategy

A detailed walk-through of how the bot actually trades. All claims map back to specific files and lines in the codebase as of this writing — if a value below differs from what's in `backend/config.py` today, the code is the source of truth.

---

## 1. Overview

Two independent strategies run side-by-side on the same scheduler:

| Strategy | Markets | Edge source | Cadence |
|---|---|---|---|
| **BTC 5-min** | Polymarket `btc-updown-5m-*` binary up/down windows | Technical microstructure on 1-min BTC candles | Scan every 60s |
| **Weather** | Polymarket bucketed °C and binary °F daily-high markets (plus Kalshi if enabled) | 31-member ensemble forecast vs market-implied probability | Scan every 300s |

Both share the same plumbing: signal generation → edge calc → Kelly sizing → entry filters → trade record → periodic settlement → P&L into bankroll. Risk caps and a daily-loss circuit breaker sit above both.

Default mode is paper trading (`SIMULATION_MODE = True`, `config.py:32`) on a $1,000 simulated bankroll (`config.py:33`).

---

## 2. Lifecycle (scheduler.py)

`APScheduler` wires four jobs onto an async loop:

| Job | Interval | What it does |
|---|---|---|
| `scan_and_trade_job` | 60s | BTC: pull markets, build signals, gate, place trades (`scheduler.py:55-183`) |
| `settlement_job` | 120s | Resolve any pending trade whose market has closed (`scheduler.py:315-362`) |
| `weather_scan_and_trade_job` | 300s | Same shape as BTC, weather-specific limits (`scheduler.py:186-312`) |
| Heartbeat | 60s | Logs pending count + bankroll for the dashboard (`scheduler.py:421-427`) |

Each scan does this in order:

1. **Discover markets** via the data layer (Polymarket gamma-api / Kalshi REST).
2. **Generate signals** per market — model probability, edge, direction, suggested size.
3. **Filter** to actionable (`|edge| >= MIN_EDGE_THRESHOLD`) and sort by edge descending.
4. **Gate** against risk controls: daily-loss breaker, pending-trade cap, per-market dedupe, per-trade size cap.
5. **Persist trade** with link back to the signal so settlement can record calibration.

There is no exit logic — positions are held to market resolution.

---

## 3. BTC 5-Min Strategy

### 3.1 What we trade

`backend/data/btc_markets.py` targets Polymarket's `btc-updown-5m-{unix_ts}` series where `unix_ts` is the window end. Each scan:

- Computes the 6 upcoming window slugs from current time (`btc_markets.py:69-87`), validates them with a strict regex (`btc_markets.py:16-22`).
- Falls back to Polymarket series search if direct fetch comes up empty.

The bot only ever buys **one side** of a window (up or down). No shorting, no exits.

### 3.2 The signal

Signal generation lives in `backend/core/signals.py:122-287` and runs five indicators on 60 one-minute BTCUSDT candles fetched from Coinbase (primary), with Kraken / Binance / ByBit fallbacks (`crypto.py:181-254`). The candle pull has a 30s cache to survive the 60s scan cadence cleanly (`crypto.py:22-23`).

| Indicator | Source | Mapping to signed signal | Weight |
|---|---|---|---|
| RSI(14, Wilder) | `crypto.py:156-178` | `<30` → +1 (oversold, mean-revert up), `>70` → −1 | 0.20 |
| Momentum | 1m/5m/15m % change blended 50/35/15 | ±0.1% normalized to ±1.0 | 0.35 |
| VWAP deviation | 30-candle rolling VWAP | (px − vwap)/vwap, ±0.05 → ±1.0 | 0.20 |
| SMA crossover | (SMA5 − SMA15)/px | ±0.03 → ±1.0 | 0.15 |
| Market skew | Polymarket UP price | Contrarian fade, 4× leverage on skew | 0.10 |

Weights are tunable in `config.py:52-57`.

**Convergence filter** (`signals.py:182-193`): at least 2 of the 4 technical indicators (everything except market skew) must agree on direction with `|signal| > 0.05`. Without convergence, the trade is held back even if the composite has the "right" sign.

**Composite → model probability** (`signals.py:205-208`):

```
model_prob = clip(0.50 + composite * 0.15, 0.35, 0.65)
```

The narrow ±15% band is deliberate — these are 5-minute coin flips and the bot shouldn't claim 80% conviction from 60 minutes of candles.

### 3.3 Edge and direction

`calculate_edge()` (`signals.py:48-71`):

```
up_edge   = model_prob_up   − market_up_price
down_edge = (1 − model_prob_up) − (1 − market_up_price)
direction = "up" if up_edge >= down_edge else "down"
edge      = max(up_edge, down_edge)
```

A signal "passes" if `edge >= MIN_EDGE_THRESHOLD` (2%, `config.py:40`).

### 3.4 Entry filters

Before a passing signal is allowed to trade (`signals.py:213-228`):

- **Time to window close** must be in `[60s, 1800s]` (`config.py:49-50`). Avoids last-second noise and far-future windows where conviction decays.
- **Entry price** must be `<= MAX_ENTRY_PRICE = 0.55` (`config.py:41`). The bot only buys the cheap side of a coin-flip market.
- **Convergence** must hold (see above).

If filters fail, the signal is still emitted to the DB with `edge=0` so the dashboard can show what was considered and rejected (`signals.py:231-232`).

### 3.5 Sizing — fractional Kelly with hard caps

`calculate_kelly_size()` (`signals.py:74-119`):

```
odds   = (1 − entry_price) / entry_price
kelly  = (win_prob * odds − lose_prob) / odds
size   = bankroll * min(kelly * KELLY_FRACTION, MAX_TRADE_BANKROLL_FRACTION)
size   = min(size, MAX_TRADE_SIZE)
```

| Knob | Value | File |
|---|---|---|
| `KELLY_FRACTION` | 0.15 | `config.py:34` |
| `MAX_TRADE_BANKROLL_FRACTION` | 5% | `config.py:48` |
| `MAX_TRADE_SIZE` | $5,000 | `config.py:47` |

The scheduler re-applies the same `MAX_TRADE_BANKROLL_FRACTION` cap at execution time (`scheduler.py:112, 134`), and `MIN_TRADE_SIZE = $10` (`scheduler.py:111`) filters dust.

### 3.6 Execution gate (per scan)

After actionable signals are sorted by `|edge|` (`signals.py:319`), the scheduler:

1. Drops the scan entirely if `daily_loss_breaker_tripped()` returns True (`scheduler.py:55-74`) — i.e. today's settled P&L `<= -DAILY_LOSS_FRACTION * start_of_day_bankroll`. Start-of-day bankroll is reconstructed as `state.bankroll - daily_pnl`. The same helper gates the weather scan.
2. Drops the scan if pending trade count `>= MAX_TOTAL_PENDING_TRADES = 20` (`config.py:43`).
3. Takes **at most 2 trades per cycle** (`scheduler.py:86`).
4. Skips any signal whose `event_slug` already has an unsettled trade — **no re-entry, no averaging in** (`scheduler.py:110-116`).
5. Creates the `Trade` row, links it to the originating `Signal` for calibration (`scheduler.py:147-153`), increments `BotState.total_trades`.

---

## 4. Weather Strategy

### 4.1 What we trade

`backend/data/weather_markets.py` covers three market shapes:

1. **Polymarket bucketed °C — equality** ("Will the high in NYC be 28°C on …?"). Wins if observed high is within 0.5°C of the bucket center.
2. **Polymarket bucketed °C — floor** ("… be 28°C or below"). Wins if observed high ≤ threshold.
3. **Polymarket binary °F** (legacy "Weather" tag — "exceed/drop below NN°F"). Handles high or low.
4. **Kalshi weather markets** if `KALSHI_ENABLED=True`. Fetching is implemented; **trade placement and settlement are not yet wired** (see §7).

Same-day and past-dated markets are filtered out (`weather_markets.py:282`) because the ensemble forecast can't see intraday observations once the day is in progress.

Default city set is 20 large US + Chinese metros (`config.py:69-73`), tunable via `WEATHER_CITIES` env var.

### 4.2 The signal

Forecast comes from the **Open-Meteo GFS ensemble** — 31 members, daily max/min in °F, 15-minute cache (`weather.py:246, 253-334`). The probability of each bucket is just the fraction of members satisfying its condition (`weather_signals.py:48-78`):

| Market shape | Model probability |
|---|---|
| Equality bucket (28°C) | Members with high ∈ [27.5, 28.5)°C / total |
| Floor bucket (≤28°C) | Members with high ≤ 28°C / total |
| Binary °F above N | Members with high > N°F / total |
| Binary °F below N | Members with high < N°F / total |

All clipped to `[0.05, 0.95]` (`weather_signals.py:78`) — the bot never trades unanimous ensembles, where divergence is more likely a model gap than real edge.

Edge calc reuses `calculate_edge()` from BTC by mapping YES/NO onto UP/DOWN (`weather_signals.py:83-84`).

### 4.3 Differences from BTC

| Aspect | BTC 5-min | Weather |
|---|---|---|
| Edge threshold | 2% | **8%** (`config.py:66`) — higher bar |
| Max entry price | 0.55 | **0.70** (`config.py:67`) — allows betting more confident sides |
| Scan interval | 60s | 300s |
| Settlement check | 120s | 1800s |
| Trades per scan | 2 | 3 (`scheduler.py:219`) |
| Allocation cap | None beyond pending count | **$500 total pending weather exposure** (`scheduler.py:221-231`) |
| Time-to-close filter | 60s – 1800s | None (resolution is end-of-day) |
| Convergence filter | Yes (2/4 indicators) | N/A — single ensemble probability |
| Bucket dedupe | N/A | Same (city, date): keep only highest-`\|edge\|` signal (`weather_signals.py:200-213`) |

The bucket dedupe matters: a single forecast naturally has edge on multiple correlated bucket markets for the same day. Without it the bot would double-bet the same underlying view.

### 4.4 Sizing

Identical Kelly logic to BTC. Per-trade dollar cap is `WEATHER_MAX_TRADE_SIZE = $5,000` (`config.py:68`).

The `$500` pending-exposure cap (`scheduler.py:221`) is the real binding constraint — it stops the bot from loading up on correlated weather positions during a single scan.

---

## 5. Risk Controls

| Control | Value | Scope | File |
|---|---|---|---|
| Daily loss circuit breaker | 25% of start-of-day bankroll (UTC day, settled P&L only) | Halts both BTC and weather scans via shared `daily_loss_breaker_tripped()` helper | `config.py:46`, `scheduler.py:55-74` |
| Max concurrent pending trades | 20 | All strategies | `config.py:43` |
| Weather pending exposure | $500 | Weather only | `scheduler.py:221` |
| Max trades per scan | 2 BTC / 3 weather | Per-cycle | `scheduler.py:86, 219` |
| Min trade size | $10 | Per-trade | `scheduler.py:87` |
| Max trade size (relative) | 5% bankroll | Per-trade (Kelly cap) | `config.py:48` |
| Max trade size (scheduler re-check) | 5% bankroll | Per-trade (mirrors `MAX_TRADE_BANKROLL_FRACTION`) | `scheduler.py:112, 134` |
| Max trade size (absolute) | $5,000 | Per-trade | `config.py:47` |
| Kelly fraction | 0.15 | Sizing dampener | `config.py:34` |
| Re-entry prevention | One trade per `event_slug` | Per-market | `scheduler.py:110-116` |

**No stop-loss, no take-profit, no scale-in/out.** Every position is held to resolution. The 5-minute window for BTC and the daily resolution for weather already cap downside duration.

---

## 6. Settlement & Calibration

`backend/core/settlement.py` runs out-of-band from signal generation.

**Resolution detection:**
- **Polymarket** (`settlement.py:14-130`): fetch market by `event_slug`, check `closed`, read `outcomePrices`. `outcomePrices[0] > 0.99` → UP won; `< 0.01` → DOWN won; anything in between → not yet resolved.
- **Kalshi** (`settlement.py:189-237`): check `status in ("finalized", "determined")` and read `result`.

**P&L** (`settlement.py:132-160`):

```
win:  pnl = +size * (1.0 − entry_price)
loss: pnl = -size * entry_price
```

E.g. $100 UP at 0.40: win = +$60, loss = −$40.

**Calibration loop** (`settlement.py:281-289`): each settled `Trade` writes `actual_outcome` and `outcome_correct` back onto its originating `Signal` row. That's what `/calibration` in the API uses to show signal accuracy by confidence bucket — the basis for future weight tuning.

**Bankroll** is updated only post-settlement (`settlement.py:308-330`): `BotState.bankroll += pnl`, `total_pnl += pnl`, `winning_trades += 1` on wins. Pending trades do **not** mark-to-market against bankroll, which is why the pending-trade cap matters.

---

## 7. AI Integration (Wired but Inactive)

Clients exist for Claude (`backend/ai/claude.py`, `claude-sonnet-4-20250514`) and Groq (`backend/ai/groq.py`, `llama-3.1-70b-versatile`). The `AILog` table tracks calls, tokens, latency, and cost for a $1/day soft budget (`config.py:29`).

**As of now, neither LLM gates trade execution.** Signal generation is purely deterministic (technicals + ensemble math + Kelly). The AI surface is reserved for future use cases — pre-trade validation, market classification, anomaly flagging — and isn't on the hot path.

---

## 8. Known Gaps

1. **Kalshi trade placement and settlement are not implemented** — only market fetching. `KALSHI_ENABLED` defaults to `False`.
2. **Weather settlement depends on Polymarket marking the market closed**, which can lag the actual high-temp resolution. NWS observation fetching exists (`weather.py:337-385`) but isn't wired into auto-settlement.
3. **AI integration is inactive** (see §7).
4. **No position management** — held to resolution, no stops, no partials, no re-entry.
5. **2% edge threshold on 50/50 BTC markets is tight.** The convergence filter offsets some noise but expect high variance; the daily-loss circuit breaker is the backstop.

---

## 9. Config Cheat Sheet (`backend/config.py`)

| Knob | Default | Purpose |
|---|---|---|
| `SIMULATION_MODE` | True | Paper vs live |
| `INITIAL_BANKROLL` | $1,000 | Starting capital |
| `KELLY_FRACTION` | 0.15 | Fractional Kelly multiplier |
| `MIN_EDGE_THRESHOLD` | 0.02 | BTC edge bar |
| `MAX_ENTRY_PRICE` | 0.55 | BTC max entry |
| `MIN_TIME_REMAINING` | 60s | Don't trade end-of-window |
| `MAX_TIME_REMAINING` | 1800s | Don't trade far-future windows |
| `MAX_TRADE_SIZE` | $5,000 | Absolute per-trade cap |
| `MAX_TRADE_BANKROLL_FRACTION` | 0.05 | Relative per-trade cap |
| `MAX_TOTAL_PENDING_TRADES` | 20 | Concurrent position cap |
| `DAILY_LOSS_FRACTION` | 0.25 | Circuit breaker (fraction of start-of-day bankroll) |
| `WEIGHT_RSI / MOMENTUM / VWAP / SMA / MARKET_SKEW` | 0.20 / 0.35 / 0.20 / 0.15 / 0.10 | Composite weights |
| `WEATHER_ENABLED` | True | Weather module toggle |
| `WEATHER_MIN_EDGE_THRESHOLD` | 0.08 | Weather edge bar |
| `WEATHER_MAX_ENTRY_PRICE` | 0.70 | Weather max entry |
| `WEATHER_MAX_TRADE_SIZE` | $5,000 | Weather per-trade cap |
| `WEATHER_CITIES` | 20 cities | Universe |
| `KALSHI_ENABLED` | False | Kalshi toggle (fetch only) |
| `AI_DAILY_BUDGET_USD` | $1.00 | AI spend soft cap |

---

## 10. Execution Flow (BTC scan, one tick)

```
scan_and_trade_job (every 60s)
  │
  ├─ scan_for_signals()
  │     ├─ fetch_active_btc_markets()          [Polymarket gamma-api]
  │     └─ for each market:
  │           ├─ compute_btc_microstructure()  [Coinbase 1m candles, 30s cache]
  │           │     └─ RSI / momentum / VWAP / SMA / volatility
  │           ├─ generate_btc_signal()
  │           │     ├─ weighted composite → model_prob (clipped 0.35–0.65)
  │           │     ├─ convergence check (2/4 indicators)
  │           │     ├─ time-to-close filter (60–1800s)
  │           │     ├─ entry-price filter (≤ 0.55)
  │           │     ├─ calculate_edge() → direction, edge
  │           │     └─ calculate_kelly_size() → suggested_size
  │           └─ persist signal row (for calibration)
  │
  ├─ filter |edge| ≥ 0.02, sort desc
  ├─ daily-loss breaker check
  ├─ pending-trades cap check
  └─ for each signal (max 2):
        ├─ skip if event_slug already has open trade
        ├─ size = min(suggested, bankroll*MAX_TRADE_BANKROLL_FRACTION, MAX_TRADE_SIZE)
        ├─ insert Trade, link signal_id, mark signal.executed
        └─ increment BotState.total_trades

settlement_job (every 120s)
  └─ for each pending Trade:
        ├─ check resolution via platform
        ├─ compute P&L, set settled=True
        ├─ write actual_outcome back onto Signal
        └─ BotState.bankroll += pnl
```

---

*Source-of-truth files: `backend/core/scheduler.py`, `backend/core/signals.py`, `backend/core/weather_signals.py`, `backend/core/settlement.py`, `backend/config.py`, `backend/data/btc_markets.py`, `backend/data/weather_markets.py`, `backend/data/crypto.py`, `backend/data/weather.py`, `backend/models/database.py`.*
