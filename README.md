# 0DTE — Long-Only Auto-Trader

A long-only, auto-traded **0DTE options** strategy for **QQQ** and **SPY**, with a
sleek real-time trader dashboard. It composes two existing services:

| Service | Role here | Backend |
|---|---|---|
| **[MM](https://github.com/Biggjuann/MM)** | Trend **stance** (long / cautious-long / …), puts-vs-calls **flow**, and the **shared Schwab token** (auth for market data + order routing) | `web-production-fff5c.up.railway.app` |
| **[Gammagamma](https://github.com/Biggjuann/Gammagamma)** | Structural **levels** (call/put walls, gamma flip, GVWAP) | `gammagamma-production.up.railway.app` |

---

## The three levels (per ticker, weekly)

Derived from Gammagamma's `/api/levels/{symbol}?expiry=weekly`:

| Level | Definition | Source field |
|---|---|---|
| **Lower** | dominant put wall by **GEX** on the weekly | `put_wall` (= `major_put_walls[0]`) |
| **Mid** | gamma-weighted magnet | `gvwap` (fallback `gamma_flip`) |
| **Top** | dominant call wall by **GEX** on the weekly | `call_wall` (= `major_call_walls[0]`) |

> Levels are the **highest-|GEX|** walls, not the extreme strikes. Example (QQQ
> weekly): lower **735** (−$47.6M put GEX), mid **738.18** (GVWAP), top **742**
> (+$297.7M call GEX) — matches the Gammagamma dashboard.

## Strategy logic

A "buy the dip at structural support, ride to resistance" long-call strategy.

**Entry** (`FLAT/ARMED → OPEN`) — the only conditions for a long:
1. MM **stance ∈ {long, cautious-long}** → entry approval.
2. **Live price at/just above the put wall**: `lower ≤ price ≤ lower + PROXIMITY`.
   (Channel must be ordered `lower < mid < top`; one entry per put-wall touch.)
   → **BUY_TO_OPEN** a 0DTE call struck at the **closest listed strike to mid + `STRIKE_OFFSET`** (default +$1).

**Exit** (`OPEN → flat`, in priority order):
- **Protective** — stance loses long approval entirely (neutral/short) → flatten.
- **Stop** — price **below the put wall (lower)** → flatten.
- **Mid take-profit** — stance **downgrades long → cautious-long** during the trade *and* price reaches the **Mid** zone (≥ mid − PROXIMITY). Entering on cautious-long does **not** immediately exit.
- **Top take-profit** — price reaches the **Top** zone (≥ top − PROXIMITY) while still approved (long or cautious-long).

Take-profits use a reached-or-beyond band so a fast 0DTE move that overshoots a level between quote polls still closes the trade.

The `$1` band, contract count, tickers and poll cadences are all configurable.

## Strategy 2 — Person's Pivots (second tab)

A VIX-gated mean-reversion strategy on **Person's pivots** (computed from the
prior session's OHLC: `PP=(H+L+C)/3`, `R1=2PP−L`, `S1=2PP−H`, …).

- **Regime** by VIX vs its own daily pivot: VIX **above** its PP → **bearish**
  (shorts); VIX **below** → **bullish** (longs).
- **Short** (bearish): when QQQ/SPY reaches **R1** → **buy 0DTE puts** struck
  closest to **PP**. **Long** (bullish): when price reaches **S1** → **buy 0DTE
  calls** struck closest to PP.
- **Manage**: scale **50%** out at the **pivot (PP)**, move the stop to
  **breakeven**, run the rest to the opposite level (**S1** for shorts, **R1**
  for longs). Initial stop = **`PIVOT_STOP_PCT`** of entry premium (default 50%).
- Pivot data is **Schwab-backed** (daily OHLC + VIX) regardless of
  `MARKET_DATA_PROVIDER`. Its own trade log (`PIVOT_TRADE_LOG_PATH`),
  CSV export (`/api/pivot/trades.csv`), and controls (`/api/pivot/control/...`).
- **Bad-data guards** (a single spiked/stale print must not trade): decisions run
  on the validated 1-minute close; a price >`PIVOT_MAX_DEV_PCT` from the prior
  close or >`PIVOT_MAX_JUMP_PCT` between ticks is rejected; entries require a
  two-sided option market; exits are priced off an **intrinsic-floored** mark so
  a stale book can't fabricate a loss; and a `PIVOT_MIN_HOLD_SECONDS` window plus
  one-structural-action-per-tick prevent enter-and-flatten on the same spike.

## Dashboard

`GET /` serves a self-contained dark "trader" dashboard with **two tabs**
(Gamma Levels and Person's Pivots), no external CDNs:

- Per-ticker **level ladder** with live price marker and $1 proximity pulse.
- MM **status banner** + puts/calls **flow bars** with bull-control indicator.
- Live **position** panel (entry → now, P&L) and per-ticker **event feed**.
- Account **summary** (today/realized P&L, win rate) and a full **trade log**.
- Header controls: **Start / Stop**, **Auto-trade** toggle, **Kill switch**.

## Architecture

```
app/
  config.py              env-driven settings (safe defaults)
  models.py              Levels, MMSignal, Quote, Position, MMStatus, …
  clients/
    base.py              LevelsProvider / MMProvider / MarketData / Broker protocols
    mock.py              self-contained synthetic feed (runs with zero deps)
    gammagamma.py        LIVE levels  (verified against the real API)
    mm.py                LIVE stance/flow + shared Schwab token
    schwab.py            LIVE quotes, 0DTE chain, order placement (long-only)
    factory.py           DATA_MODE -> wires mock or live
  strategy/engine.py     per-ticker state machine + background loop
  store.py               trade log + P&L (JSON)
  server.py              FastAPI: dashboard + /api/state + controls
  static/dashboard.html  the UI
tests/test_strategy.py   scripted state-machine tests (no network)
```

The engine only ever talks to the four protocols in `clients/base.py`, so
**mock ↔ live is a config switch, never a code change**.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Mock mode (default) — full lifecycle with synthetic data, no creds needed:
python run.py
# open http://localhost:8080

# Tests:
python tests/test_strategy.py
```

### Live mode

```bash
cp .env.example .env
# set in .env:
DATA_MODE=live
DRY_RUN=true                          # keep true until verified end-to-end
SCHWAB_AUTH_MODE=shared               # share MM's single Schwab token
SCHWAB_TOKEN_URL=.../auth/token       # defaults to MM_BASE_URL + /auth/token
SCHWAB_TOKEN_SHARE_KEY=<share key>    # bearer for the token endpoint
SCHWAB_ACCOUNT_HASH=<hash>            # required only when DRY_RUN=false
MARKET_DATA_PROVIDER=schwab           # or "gammagamma"
OPTIONS_PROVIDER=schwab               # or "gammagamma"
```

**Shared token:** with `SCHWAB_AUTH_MODE=shared`, the app fetches the access
token from `SCHWAB_TOKEN_URL` using `SCHWAB_TOKEN_SHARE_KEY` as the bearer — the
same shared Schwab authorization MM uses — for both market data and order
routing. `MARKET_DATA_PROVIDER` / `OPTIONS_PROVIDER` select whether quotes and
option chains come from Schwab or Gammagamma (orders always route through
Schwab; Gammagamma contracts are converted to Schwab OSI symbols). GAMMA/MM
base URLs already default to the Railway services. With `DRY_RUN=true`, orders
are simulated against live quotes and **never transmitted**.

## Deploy to Railway

Already configured (`railway.json` + `Procfile`, binds `0.0.0.0:$PORT`):

1. New Railway project → deploy this repo.
2. **Variables**: `DATA_MODE=live`, `DRY_RUN=true`, `MM_API_KEY=…`,
   `SCHWAB_ACCOUNT_HASH=…` (for live orders), optional `TICKERS`, `CONTRACTS`,
   `PROXIMITY`.
3. Healthcheck is `/api/config`. The dashboard is the public domain root `/`.
4. **Persist the trade log**: attach a Railway **Volume** (e.g. mount at `/data`)
   and set `TRADE_LOG_PATH=/data/trades.json` so it survives redeploys.

## Trade log & review

Every entry/exit is logged with full context for offline review: entry levels
(lower/mid/top), MM stance at entry & exit, bull-control flag, hold time,
**MAE/MFE** (worst/best excursion), exit type (TOP / MID / STOP / PROTECTIVE /
KILL) and P&L.

- Dashboard trade log shows exit type, `[mfe/mae, hold]`, and an exit-type
  breakdown + avg win/loss/hold.
- **Export**: `GET /api/trades.csv` (download button on the dashboard) and
  `GET /api/trades.json`.

The strategy is **rule-based, not self-learning** — review the exported log
(share the CSV) and we tune the rules deliberately rather than auto-fitting a
live-money account.

## Safety

- **Long-only** is enforced at the broker layer (only `BUY_TO_OPEN` /
  `SELL_TO_CLOSE` on calls are emittable).
- **`DRY_RUN=true` by default** — no real orders until you opt in.
- **Kill switch** flattens all positions and halts auto-trading.
- Educational tooling; not investment advice.
