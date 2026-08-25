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
   → **BUY_TO_OPEN** **`CONTRACTS`** (default **10**) 0DTE calls struck **ATM** —
   the closest listed strike to **spot + `STRIKE_OFFSET`** (default +$0 = ATM).

**Exit** (`OPEN → flat`, in priority order):
- **Protective** — stance loses long approval entirely (neutral/short) → flatten.
- **Stop** — price **below the put wall (lower)** → flatten.
- **Take profit** — close the **whole position at +`TAKE_PROFIT_PCT`** (default
  **+50%** of premium). The RTH **EOD flatten** is the only other exit.

Anti-whipsaw (`STOP_COOLDOWN_SECONDS`, `REARM_DISTANCE`) and the RTH gate apply.
Contract count, band, take-profit %, tickers and poll cadences are all configurable.

## Strategy 2 — Person's Pivots (second tab)

A **zone-fade** mean-reversion strategy on **Person's pivots** (`PP=(H+L+C)/3`,
`R1=2PP−L`, `S1=2PP−H`, …), computed from the **prior completed week's** OHLC
(`PIVOT_TIMEFRAME=weekly`; set `daily` for the prior session). Levels are
**frozen on the first fetch of the trading day** so they never drift under an
open trade.

- **Zones, not lines**: each of the 7 pivot levels (PP, R1–R3, S1–S3) is a
  **zone** centered on the line, per ticker via `PIVOT_ZONES` (default **SPY
  $0.50**, **QQQ $0.75** wide). The ladder draws them as shaded bands.
- **Entry by direction of approach** — into any zone:
  - **From below** (price rising into the zone) → **short**: buy **10 ATM puts**.
  - **From above** (price falling into the zone) → **long**: buy **10 ATM calls**.
  - Size via `PIVOT_CONTRACTS` (default 10).
- **Take profit**: close the **whole position at +50%** (`PIVOT_SCALE_PROFIT`).
  No runner by default (`PIVOT_RUNNER_CONTRACTS=0`); the only other exit is the
  RTH **EOD flatten**.
- **No stop by default** (`PIVOT_STOP_PCT=0`). Set `PIVOT_RUNNER_CONTRACTS>0` to
  keep a runner (scales the rest at +50%, runs it to the next zone) or
  `PIVOT_STOP_PCT>0` to re-enable a premium stop.
- **Contracts** via `PIVOT_CONTRACTS` (default 4). VIX is still shown as context
  but no longer gates entries.
- Pivot data is **Schwab-backed** (daily OHLC + VIX) regardless of
  `MARKET_DATA_PROVIDER`. Its own trade log (`PIVOT_TRADE_LOG_PATH`),
  CSV export (`/api/pivot/trades.csv`), and controls (`/api/pivot/control/...`).
- **Bad-data guards** (a single spiked/stale print must not trade): decisions run
  on the validated live price; a price >`PIVOT_MAX_DEV_PCT` from the prior close
  or >`PIVOT_MAX_JUMP_PCT` between ticks is rejected; entries require a two-sided
  option market; exits are priced off an **intrinsic-floored** mark so a stale
  book can't fabricate a loss; and a `PIVOT_MIN_HOLD_SECONDS` window prevents
  enter-and-flatten on the same spike.

> Note: the gamma tab now takes full profit at **+50%**, so it exits before a
> position could reach the older **+100% breakeven-arm** threshold — that path is
> effectively dormant unless `TAKE_PROFIT_PCT` is raised above `BREAKEVEN_ARM_PROFIT`.

## Anti-whipsaw (gamma strategy)

A price hovering right at the put wall used to churn enter→stop→re-enter. Two
damping controls (in addition to the existing re-arm-on-retouch latch):

- **Stop cooldown** — after a `STOP`, the ticker sits out for
  `STOP_COOLDOWN_SECONDS` (default 180s) before any re-entry.
- **Re-arm distance** — `REARM_DISTANCE` widens the band price must clear above
  the wall before it can re-arm (0 = use `PROXIMITY`).

## Regular-trading-hours gate (both strategies)

These are **0DTE** options, so **all** activity is restricted to the US equity
regular session (`America/New_York`, holidays aside):

- **No entries** outside the session — pre-market, after-hours, or weekends.
- **No new entries** after `SESSION_NO_ENTRY` (default 15:45 ET).
- Any open position is **flattened** by `SESSION_FLATTEN` (default 15:55 ET) —
  a same-day-expiry option is never held overnight.
- Configurable via `RTH_ONLY` / `SESSION_OPEN` / `SESSION_CLOSE` /
  `SESSION_NO_ENTRY` / `SESSION_FLATTEN`. Set `RTH_ONLY=false` only for off-hours
  mock demos. The dashboard header shows the live session phase.

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
