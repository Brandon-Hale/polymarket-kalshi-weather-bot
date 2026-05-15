# Live Trading Setup — Polymarket

The bot defaults to **simulation** mode. To enable real trading on Polymarket
you must complete the on-chain account setup yourself (no code can do this for
you), then flip `SIMULATION_MODE=False` in `.env`.

The bot will only enter live mode when **all four** Polymarket credentials are
present AND `SIMULATION_MODE=False`. If any credential is missing it falls
back to simulation — no accidental real-money trades.

---

## Path A — You already have a Polymarket account with USDC (recommended)

Most users sign up via email — Polymarket creates a "Magic" smart wallet for
you and your USDC sits in a proxy contract. The bot can trade from this
existing balance directly. You need two values from Polymarket:

### A.1 — Export the signer private key

1. Log into [polymarket.com](https://polymarket.com).
2. Profile menu (top-right) → **Wallet** → **Show / Export private key**.
3. Save the `0x…` hex string somewhere safe (1Password). **Anyone with this
   key can drain the wallet** — treat it like cash.

### A.2 — Find your proxy (funder) address

1. Profile menu → **Wallet** → the address shown next to "Deposit USDC".
   This is the proxy contract that holds your USDC.
2. Copy that `0x…` address. You'll paste it as `POLYMARKET_FUNDER_ADDRESS` below.

### A.3 — Verify USDC balance

Paste the proxy address into [polygonscan.com](https://polygonscan.com/) →
Token Holdings — you should see your USDC balance. That's what the bot will
see and size against.

→ Skip to **section 4 (API credentials)**.

---

## Path B — Fresh wallet (only if you don't already have a Polymarket account)

### B.1 — Create a dedicated wallet

**Don't reuse a personal wallet.** Create a fresh one for the bot. Easiest:

1. Install MetaMask, create a brand-new account.
2. Export the private key (Account → Show Private Key). It starts with `0x…` and is 64 hex characters.
3. Save it somewhere secure (1Password / hardware wallet keystore). Anyone with this key can drain the wallet.

### B.2 — Fund the wallet with USDC on Polygon

Polymarket settles in USDC on the Polygon network (not Ethereum mainnet).
You need:
- A small amount of MATIC for gas (~$1 worth is enough — Polygon gas is fractions of a cent).
- USDC.e on Polygon — this is your trading bankroll.

Simplest path from cash:
1. Buy USDC on Coinbase / Kraken / Binance.
2. Withdraw to your wallet address **on the Polygon network** (every exchange has Polygon as a withdrawal option for USDC).
3. Wait ~30 seconds for confirmation.

You can verify the balance on [polygonscan.com](https://polygonscan.com/) by pasting your wallet address.

### B.3 — Create the Polymarket account

1. Go to [polymarket.com](https://polymarket.com).
2. Click "Log in" → "Sign with wallet". Connect the wallet from step 1.
3. Polymarket will prompt you to **approve** the trading contract — this is a
   one-time on-chain transaction that lets Polymarket's smart contract move
   USDC out of your wallet when you place orders. Approve it.

(Path B users: leave `POLYMARKET_FUNDER_ADDRESS` unset in the `.env` below.)

## 4 — Generate API credentials (both paths)

1. In Polymarket: Profile → Settings → "API Keys" → "Create API Key".
2. Save the three values:
   - `key` (API key)
   - `secret` (API secret)
   - `passphrase` (API passphrase)

These authenticate your bot to the CLOB. Together with the wallet private key,
they let the bot sign and place orders.

## 5 — Add credentials to `.env`

Open `.env` in this repo and add (or fill in):

```bash
# Wallet — treat like cash
POLYMARKET_PRIVATE_KEY=0xYOUR_SIGNER_PRIVATE_KEY_HERE

# Path A (you already had a Polymarket account / email login):
# SET this to your Polymarket proxy address (the one holding your USDC).
POLYMARKET_FUNDER_ADDRESS=0xYOUR_PROXY_ADDRESS

# Path B (fresh EOA wallet, MetaMask): LEAVE FUNDER UNSET.
# POLYMARKET_FUNDER_ADDRESS=

# CLOB API credentials
POLYMARKET_API_KEY=YOUR_API_KEY
POLYMARKET_API_SECRET=YOUR_API_SECRET
POLYMARKET_API_PASSPHRASE=YOUR_API_PASSPHRASE

# THE big switch
SIMULATION_MODE=False

# Live-mode safety caps — start TIGHT
LIVE_TRADE_MAX_USD=5.0          # Hard cap per individual order
LIVE_TRADE_DAILY_USD_LIMIT=25.0 # Halt new orders once today's notional hits this
```

**Never commit `.env`.** It's already in `.gitignore`, double-check.

## 6 — Install the live-trading SDK

The SDK is in `requirements.txt`. In your venv:

```bash
pip install -r requirements.txt
```

This installs `py-clob-client` and `web3`.

## 7 — Restart the backend

```bash
# stop the running bot
# then start fresh:
./venv/bin/uvicorn backend.api.main:app --reload
```

In the logs you should see:

```
Polymarket live trading client initialised.
```

In the dashboard, the `SIM` badge in the header turns into a pulsing red `LIVE` badge. The Bank stat now reflects your **actual Polymarket USDC balance**.

## 8 — Phased rollout (strongly recommended)

| Phase | Caps | What to verify |
|---|---|---|
| 1. Tiny | `LIVE_TRADE_MAX_USD=2`, `DAILY=10` | One order fills, settlement works, P&L matches wallet movement |
| 2. Small | `LIVE_TRADE_MAX_USD=10`, `DAILY=50` | Calibration accumulates, no surprises |
| 3. Real | Raise to your comfort | Only after ≥30 settled live trades show calibration ≥ 0.5 |

## 9 — Kill switch

To stop live trading immediately:

**Fastest** — edit `.env` and set `SIMULATION_MODE=True`, then restart the backend. New trades stop instantly; in-flight orders are unaffected (Polymarket FAK orders don't rest).

**Cancel any existing positions** — Polymarket UI → Positions → Sell. The bot does not auto-exit positions; settlement happens at market resolution.

## What stays the same in live mode

- All sim-mode logic, filters, sizing, calibration UI — unchanged.
- `KELLY_FRACTION`, `WEATHER_MIN_EDGE_THRESHOLD`, `MAX_TRADE_BANKROLL_FRACTION`, etc. all apply identically.
- Settlement / P&L tracking — works the same; the bot polls Polymarket for resolution either way.

## What's different in live mode

- `bankroll` in the dashboard reads the on-chain USDC balance, not the DB.
- Each Trade row has `live_mode=True`, `live_order_id` (the on-chain order ID),
  `live_filled_size` (actual shares), `live_status` (matched / partial / etc).
- A trade is **only persisted if the order actually filled**. Rejections or
  errors are logged but no Trade row is created.
- `LIVE_TRADE_MAX_USD` clamps each order independently of `MAX_TRADE_SIZE`.
- `LIVE_TRADE_DAILY_USD_LIMIT` halts further live orders once today's notional
  reaches the cap.

## Troubleshooting

**"Live mode enabled but market has no CLOB token IDs"** — the bot fetched a
weather or BTC market without `clobTokenIds`. This shouldn't happen on
Polymarket markets but is logged + skipped safely.

**"Polymarket client unavailable"** — usually means `py-clob-client` failed to
import or one of the creds is wrong. Check the backend log for the actual
import / auth error.

**Order rejected: "not enough balance"** — your wallet's USDC balance is below
the order notional. Either fund more USDC or lower `LIVE_TRADE_MAX_USD`.

**Order rejected: "allowance"** — you skipped step 3 (approving the contract).
Visit Polymarket UI, try placing any trade manually, approve the prompt.

**Geo-block** — Polymarket blocks several countries by IP. If running on a
VPS, choose a non-US, non-UK, non-France region. Hetzner Germany or Vultr
Sydney both work.
