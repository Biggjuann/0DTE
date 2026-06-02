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

**Entry** (`FLAT/ARMED → OPEN`) — all three required:
1. MM **stance ∈ {long, cautious-long}** → entry approval.
2. **Bull control**: puts-below-spot **>** calls-≥-spot (MM flow confirmation).
3. 1-minute **close within $1 of the Lower level**.
   → **BUY_TO_OPEN** a 0DTE **call** struck **$1 above the Mid** level
   (`STRIKE_OFFSET`, default 1.0; QQQ 738.18 → strike 739).

**Exit** (`OPEN → flat`):
- **Mid take-profit** — stance downgrades to **cautious-long** *and* price within $1 of **Mid**.
- **Top take-profit** — stance **stays long** and price within $1 of **Top** (largest call wall).
- **Protective** — stance loses long approval entirely (neutral/short) → flatten.

The `$1` band, contract count, tickers and poll cadences are all configurable.

## Dashboard

`GET /` serves a self-contained dark "trader" dashboard (no external CDNs):

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
DRY_RUN=true                 # keep true until verified end-to-end
MM_API_KEY=<schwab share key>   # MM /auth/token bearer
SCHWAB_ACCOUNT_HASH=<hash>      # required only when DRY_RUN=false
```

GAMMA/MM base URLs already default to the Railway services. With `DRY_RUN=true`,
orders are simulated against live quotes and **never transmitted**.

## Deploy to Railway

Already configured (`railway.json` + `Procfile`, binds `0.0.0.0:$PORT`):

1. New Railway project → deploy this repo.
2. **Variables**: `DATA_MODE=live`, `DRY_RUN=true`, `MM_API_KEY=…`,
   `SCHWAB_ACCOUNT_HASH=…` (for live orders), optional `TICKERS`, `CONTRACTS`,
   `PROXIMITY`.
3. Healthcheck is `/api/config`. The dashboard is the public domain root `/`.
4. Trade log is on the container FS (ephemeral) — attach a Railway **Volume**
   at `/app/data` if you want persistence across redeploys.

## Safety

- **Long-only** is enforced at the broker layer (only `BUY_TO_OPEN` /
  `SELL_TO_CLOSE` on calls are emittable).
- **`DRY_RUN=true` by default** — no real orders until you opt in.
- **Kill switch** flattens all positions and halts auto-trading.
- Educational tooling; not investment advice.
