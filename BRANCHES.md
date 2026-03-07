# Ballom_FYR — Branch Architecture

> **Repo:** `sidh-singh/Ballom_FYR`
> **Platform:** Fyers API (Indian stock broker — NSE index options + MCX commodity options)
> **Runtime:** Windows (`C:/Ballom_FYR/` state paths), developed/pushed from macOS
> **Dashboard:** Dash / Plotly on `http://127.0.0.1:8050`

---

## Data Flow

```
dev_scanner
  │  scans every hour, writes:
  │    C:/Ballom_FYR/option_pairs.json      (index pairs)
  │    C:/Ballom_FYR/commodity_pairs.json    (commodity pairs)
  │    C:/Ballom_FYR/fyers_token.json        (auth token, refreshed daily)
  │
  ├──► dev_update_nifty   ─┐
  ├──► dev_update_gold     ├─ each updater reads pairs, computes SHA/RSI/GAP,
  └──► dev_update_silver   ─┘  writes to:
         │                        C:/Ballom_FYR/state/<mode>/signal_state.json
         │                        (upsert per symbol key — all updaters share ONE file)
         │
         ├──► dev_trading   reads signal_state + pairs → evaluates strategy → places trades
         │                  writes position/order/strategy state JSON files
         │
         └──► dev (dashboard)   reads ALL JSON state files → renders live dashboard
```

`<mode>` is either `demo` or `live`, determined by CLI arg.

---

## Branch Details

### 1. `dev` — Dashboard (frontend)

| | |
|---|---|
| **Main file** | `dashboard.py` |
| **Launch** | `python dashboard.py [demo\|live] [port]` |
| **Purpose** | Live Dash/Plotly dashboard — reads JSON state files and displays account balance, P&L, open positions, SHA signal analysis, RSI indicators, strategy log |
| **Port** | 8050 (default), configurable |
| **Refresh** | 3 seconds auto-refresh |

**Unique files:** `dashboard.py`, `app.py`, `start_dashboard.bat`, `start_job.bat`, `asset/`, `FLOW_VERIFICATION.md`, `FYERS_PNL_LOGIC.md`

**Dashboard features:**
- Account balance card (realized + unrealized P&L)
- Open positions table
- SHA Signal Analysis cards per symbol:
  - Signal SHA (length=3) and Trend SHA (length=6) values
  - Power, List indicators
  - GAP% with color-coded badges
  - SHA Relationship (DIVERGING / CONVERGING / PARALLEL / CLOSE)
  - RSI 1min, 5min, 15min for CE and PE (color-coded: green=normal, red=oversold, orange=overbought)
  - 💰 POSITION OPEN badge (cyan) — when CE or PE symbol has an open position
  - ⚠ RSI OVERSOLD — NO NEW ENTRY badge (amber) — when any RSI timeframe is below 30 and no position open
- Strategy decision log (rolling)

**All 18 files:**
```
dashboard.py, app.py, strategy.py, position_tracker.py, pair_manager.py,
constants.py, state_writer.py, indicator.py, fyers.py, demo_fyers.py,
symbols.json, start_dashboard.bat, start_job.bat,
FLOW_VERIFICATION.md, FYERS_PNL_LOGIC.md, README.md,
asset/, requirements_fyers.txt
```

---

### 2. `dev_scanner` — Pair Scanner

| | |
|---|---|
| **Main file** | `scanner_app.py` |
| **Launch** | `python scanner_app.py [demo\|live]` |
| **Purpose** | Hourly option-pair scanner — finds best CE/PE option pairs for NIFTY (index) and GOLDM/SILVERM (commodity) based on volume, OI, and expiry filters |
| **Runs** | 24×7 forever loop |

**Behavior:**
1. Day-change detection → re-auth Fyers token at midnight + re-download symbol CSVs
2. Holiday check → skip scan on NSE holidays
3. Hourly scan trigger → scan index + commodity pairs
4. Write results → `option_pairs.json` + `commodity_pairs.json`
5. Per-symbol position check → skip scanning symbols that already have open trades

**Output files:**
- `C:/Ballom_FYR/option_pairs.json` — index option pairs
- `C:/Ballom_FYR/commodity_pairs.json` — commodity option pairs
- `C:/Ballom_FYR/fyers_token.json` — auth token (refreshed daily)
- `C:/Ballom_FYR/state/<mode>/app_status.json` — scanner lifecycle
- `C:/Ballom_FYR/state/<mode>/strategy_log/` — scan events (date-partitioned)

**All 9 files:**
```
scanner_app.py, start_scanner.bat,
constants.py, fyers.py, state_writer.py, demo_fyers.py,
symbols.json, requirements_fyers.txt, README.md
```

---

### 3. `dev_trading` — Trading Engine

| | |
|---|---|
| **Main file** | `trading_app.py` |
| **Launch** | `python trading_app.py [demo\|live]` |
| **Purpose** | Core trading engine — computes SHA indicators, evaluates Heiken-Ashi Martingale strategy, places/monitors trades via Fyers API |
| **Runs** | 24×7 forever loop |

**Behavior:**
1. Day-change detection → reload token + fetch holidays
2. Time-window routing → indices 9:15–15:30 IST, commodities 9:15–23:55 IST
3. Position conflict → mutual exclusion between index/commodity
4. Signal update → compute SHA for ALL pairs every cycle (dashboard freshness)
5. Inner loop per symbol pair:
   - Fetch 1min history for CE, PE, underlying
   - Compute Signal SHA + Trend SHA + Power + List + GAP% + Relationship
   - Compute RSI 1min, 5min (200 candles), 15min (100 candles)
   - `Strategy.evaluate()` → returns `OrderAction` list
   - `Strategy.execute_orders()` → places trades via Fyers
   - Monitor positions; if all closed → break for fresh pair
   - Day-change inside inner loop → reload token

**Strategy logic (strategy.py):**
- **Entry conditions:**
  - Indices direction (lt_list[0]) determines active leg: BULLISH(1)→CE only, BEARISH(0)→PE only
  - SHA momentum aligned with trend + GAP% within range
  - **RSI oversold guard:** if ANY RSI (1min, 5min, or 15min) < 30 → block initial entry
- **Exit:** profit target OR adverse signal reversal
- **Martingale (fibonacci position doubling on deep loss):**
  - Level 0 (initial entry): standard entry
  - Level 1: RSI 1min < 30 triggers add (BUY_WITH_SPECIFIC_VOLUME)
  - Level 2: RSI 5min < 30 triggers add (MARTINGALE_BUY_RSI_5M)
  - Level 3: RSI 15min < 30 triggers add (MARTINGALE_BUY_RSI_15M)
  - MAX_MARTINGALE_LEVEL = 3 (entry + 3 adds; close on 4th trigger)
  - Threshold formula: `fibonacci[level]² × HEDGE` → barriers at -500, -2000, -4500, …

**Unique files:** `trading_app.py`, `strategy.py`, `position_tracker.py`, `pair_manager.py`, `start_trading.bat`

**All 13 files:**
```
trading_app.py, strategy.py, position_tracker.py, pair_manager.py,
start_trading.bat, indicator.py,
constants.py, fyers.py, state_writer.py, demo_fyers.py,
symbols.json, requirements_fyers.txt, README.md
```

---

### 4. `dev_update` — Updater Template

| | |
|---|---|
| **Main file** | `updater_app.py` |
| **Purpose** | Template/base branch for the per-symbol updater branches. Not run directly in production — the symbol-specific forks (`dev_update_nifty`, `dev_update_gold`, `dev_update_silver`) are deployed instead. |

**All 10 files:**
```
updater_app.py, start_updater.bat, indicator.py,
constants.py, fyers.py, state_writer.py, demo_fyers.py,
symbols.json, requirements_fyers.txt, README.md
```

---

### 5. `dev_update_nifty` — NIFTY Signal Updater

| | |
|---|---|
| **Main file** | `updater_app.py` |
| **Launch** | `python updater_app.py [demo\|live]` |
| **Symbol** | NIFTY (NSE index) |
| **Type** | Index |
| **Pairs source** | `option_pairs.json` |
| **Runs** | 24×7 forever loop |

**Behavior:**
1. Load Fyers token from `C:/Ballom_FYR/fyers_token.json` (written by dev_scanner)
2. Day-change detection → reload token + holidays
3. Read CE/PE pairs from `option_pairs.json`, filter to NIFTY only
4. Compute per pair:
   - Signal SHA (length=3, RMA) for CE, PE, IDX
   - Trend SHA (length=6, RMA) for CE, PE, IDX
   - Power, List
   - GAP% between Signal and Trend SHA
   - SHA Relationship
   - RSI 1min (14-period, 500 candles)
   - RSI 5min (14-period, 200 candles) — via ThreadPoolExecutor
   - RSI 15min (14-period, 100 candles) — via ThreadPoolExecutor
5. Upsert to `signal_state.json` (shared with other updaters)

**All 10 files:** same as `dev_update` template

---

### 6. `dev_update_gold` — GOLDM Signal Updater

| | |
|---|---|
| **Main file** | `updater_app.py` |
| **Launch** | `python updater_app.py [demo\|live]` |
| **Symbol** | GOLDM (MCX commodity) |
| **Type** | Commodity |
| **Pairs source** | `commodity_pairs.json` |
| **Runs** | 24×7 forever loop |

Same architecture as `dev_update_nifty` but filters to GOLDM pairs from `commodity_pairs.json`.

**All 10 files:** same as `dev_update` template

---

### 7. `dev_update_silver` — SILVERM Signal Updater

| | |
|---|---|
| **Main file** | `updater_app.py` |
| **Launch** | `python updater_app.py [demo\|live]` |
| **Symbol** | SILVERM (MCX commodity) |
| **Type** | Commodity |
| **Pairs source** | `commodity_pairs.json` |
| **Runs** | 24×7 forever loop |

Same architecture as `dev_update_nifty` but filters to SILVERM pairs from `commodity_pairs.json`.

**All 10 files:** same as `dev_update` template

---

## Shared Files Across Branches

These files exist on multiple branches and must stay in sync:

| File | Branches | Purpose |
|------|----------|---------|
| `constants.py` | ALL 7 | Shared enums, dataclasses, paths, strategy parameters |
| `state_writer.py` | ALL 7 | `write_signal_state()` — serializes SHA/RSI/GAP data to JSON |
| `fyers.py` | ALL 7 | Fyers API wrapper (auth, orders, positions, history) |
| `demo_fyers.py` | ALL 7 | Mock Fyers client for demo mode |
| `symbols.json` | ALL 7 | Symbol definitions (NIFTY, GOLDM, SILVERM with exchange/segment info) |
| `requirements_fyers.txt` | ALL 7 | Python dependencies |
| `README.md` | ALL 7 | Project readme |
| `indicator.py` | dev, dev_trading, dev_update, dev_update_* | SHA + RSI indicator computation |
| `strategy.py` | dev, dev_trading | Heiken-Ashi Martingale strategy logic |
| `position_tracker.py` | dev, dev_trading | Tracks booked_profit, martingale_count per symbol |

> **Important:** When modifying a shared file, the change must be pushed to ALL branches that contain it.

---

## Key Constants Reference

### File Paths
| Constant | Value | Description |
|----------|-------|-------------|
| `STATE_DIR_BASE` | `C:/Ballom_FYR/state` | Root state directory |
| `OPTION_PAIRS_JSON` | `C:/Ballom_FYR/option_pairs.json` | Index pairs (scanner output) |
| `COMMODITY_PAIRS_JSON` | `C:/Ballom_FYR/commodity_pairs.json` | Commodity pairs (scanner output) |

### Trading Windows
| Constant | Value |
|----------|-------|
| `INDICES_START` / `INDICES_END` | 9:15 – 15:30 IST |
| `COMMODITY_START` / `COMMODITY_END` | 9:15 – 23:55 IST |

### SHA Parameters
| Constant | Value | Description |
|----------|-------|-------------|
| `SHA_LENGTH` | 3 | Signal SHA period |
| `SHA_TREND_LENGTH` | 6 | Trend SHA period |
| `SHA_MA_TYPE` / `SHA_TREND_MA_TYPE` | RMA | Moving average type |
| `DEFAULT_TIMEFRAME` | "1" | 1-minute candles |
| `DEFAULT_CANDLES` | 500 | Candles fetched for 1min |

### RSI Parameters
| Constant | Value | Description |
|----------|-------|-------------|
| `RSI_PERIOD` | 14 | Wilder's look-back period |
| `RSI_OVERSOLD` | 30 | Below this → martingale trigger / entry block |
| `RSI_OVERBOUGHT` | 70 | Reserved for future use |
| `RSI_5MIN_TIMEFRAME` | "5" | 5-minute candle timeframe |
| `RSI_5MIN_CANDLES` | 200 | Candles fetched for 5min RSI |
| `RSI_15MIN_TIMEFRAME` | "15" | 15-minute candle timeframe |
| `RSI_15MIN_CANDLES` | 100 | Candles fetched for 15min RSI |

### Strategy Parameters
| Constant | Value | Description |
|----------|-------|-------------|
| `GAP_RANGE_LOW` | 0.5 | Min GAP% for entry |
| `GAP_RANGE_HIGH` | 2.5 | Max GAP% for entry |
| `STRATEGY_HEDGE_INDEX` | 500 | ₹ profit target for index pairs |
| `STRATEGY_HEDGE_COMMODITY` | 200 | ₹ profit target for commodity pairs |
| `FIBO_SEQUENCE_LENGTH` | 25 | Fibonacci sequence length for martingale |
| `MAX_MARTINGALE_LEVEL` | 3 | Max martingale adds (entry + 3 adds) |

### Martingale Threshold Formula
```
threshold[level] = fibonacci[level]² × HEDGE
e.g. HEDGE=500 → barriers at -500, -2000, -4500, -12500, -32000, …
```

---

## State Files (written to `C:/Ballom_FYR/state/<mode>/`)

| File | Writer | Reader | Content |
|------|--------|--------|---------|
| `signal_state.json` | dev_update_* (all 3) + dev_trading | dev (dashboard) | Per-symbol SHA analysis, RSI values, GAP%, relationship |
| `app_status.json` | dev_scanner, dev_update_*, dev_trading | dev (dashboard) | App lifecycle status (running, sleeping, error) |
| `strategy_log/` | dev_scanner, dev_trading | dev (dashboard) | Date-partitioned strategy decision logs |
| `positions.json` | dev_trading | dev (dashboard) | Open positions snapshot |
| `orders.json` | dev_trading | dev (dashboard) | Recent orders |

---

## signal_state.json Schema (per symbol key)

```json
{
  "NIFTY": {
    "ce_symbol": "NSE:NIFTY25JUL24500CE",
    "pe_symbol": "NSE:NIFTY25JUL24500PE",
    "idx_symbol": "NSE:NIFTY50-INDEX",
    "ce_sha_signal": { "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0 },
    "pe_sha_signal": { "...": "..." },
    "idx_sha_signal": { "...": "..." },
    "ce_sha_trend": { "...": "..." },
    "pe_sha_trend": { "...": "..." },
    "idx_sha_trend": { "...": "..." },
    "ce_power": 1,
    "pe_power": 0,
    "ce_list": 1,
    "pe_list": 0,
    "ce_gap_pct": 1.23,
    "pe_gap_pct": -0.45,
    "ce_relationship": "DIVERGING",
    "pe_relationship": "CONVERGING",
    "ce_rsi": 55.2,
    "pe_rsi": 42.8,
    "ce_rsi_5m": 48.1,
    "pe_rsi_5m": 38.5,
    "ce_rsi_15m": 52.3,
    "pe_rsi_15m": 35.7,
    "updated_at": "2025-07-15T10:30:00"
  }
}
```

---

## Deployment Architecture

All branches run as separate processes on the same Windows machine:

```
┌─────────────────────────────────────────────────────────┐
│                    Windows Machine                       │
│                                                         │
│  scanner_app.py (dev_scanner)     ← runs 24×7           │
│       ↓ writes token + pairs                            │
│                                                         │
│  updater_app.py (dev_update_nifty)  ← runs 24×7         │
│  updater_app.py (dev_update_gold)   ← runs 24×7         │
│  updater_app.py (dev_update_silver) ← runs 24×7         │
│       ↓ writes signal_state.json                        │
│                                                         │
│  trading_app.py (dev_trading)     ← runs 24×7           │
│       ↓ reads pairs + signal_state, places trades       │
│       ↓ writes position/order state                     │
│                                                         │
│  dashboard.py (dev)               ← runs 24×7           │
│       ↓ reads ALL state files                           │
│       ↓ serves http://127.0.0.1:8050                    │
└─────────────────────────────────────────────────────────┘
```

Each branch is checked out into its own directory on the Windows machine (or run via `git worktree`).

---

## Push Rules

When modifying code, push to the correct branch(es):

| Changed File | Push To |
|-------------|---------|
| `constants.py` | ALL branches that contain it |
| `state_writer.py` | ALL branches that contain it |
| `strategy.py` | `dev_trading` (+ `dev` if it has a copy) |
| `trading_app.py` | `dev_trading` only |
| `updater_app.py` | `dev_update_nifty`, `dev_update_gold`, `dev_update_silver` (all 3) |
| `dashboard.py` | `dev` only |
| `scanner_app.py` | `dev_scanner` only |
| `indicator.py` | `dev`, `dev_trading`, `dev_update*` (all that contain it) |
| `position_tracker.py` | `dev`, `dev_trading` |
| `pair_manager.py` | `dev_trading` |

---

*Last updated: July 2025*
