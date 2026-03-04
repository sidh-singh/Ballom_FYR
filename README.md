# Ballom_FYR — SHA Signal Updater (`dev_update`)

Real-time SHA Signal Analysis service that feeds the `dev` branch dashboard.

## What it does

1. **Reads** the Fyers token from `C:/Ballom_FYR/fyers_token.json`
   (written by `dev_scanner`) — **no re-auth** performed here.
2. **Reads** CE/PE pair JSON from `dev_scanner`:
   - `C:/Ballom_FYR/option_pairs.json` (indices)
   - `C:/Ballom_FYR/commodity_pairs.json` (commodities)
3. **Computes** every ~3 seconds during market hours:
   - **Signal SHA** (length=3, RMA) for CE, PE, and IDX/underlying
   - **Trend SHA** (length=6, RMA) for CE, PE, and IDX/underlying
   - **Power** — count of bullish SHA candles in last 7
   - **Candle List** — [1|0] bull/bear sequence (most-recent-first)
   - **Crossover** — price vs SHA position (±1, ±2, ±3)
   - **GAP%** — percentage gap between Signal and Trend SHA midpoints
   - **SHA Relationship** — DIVERGING / CONVERGING / PARALLEL / CLOSE
4. **Writes** all signal data to `C:/Ballom_FYR/state/<mode>/signal_state.json`
   in the exact format the `dev` branch dashboard expects.
5. **Skips** holidays (NSE calendar) and honours special sessions.
6. Works for both **demo** and **live** modes.

## Files

| File | Purpose |
|---|---|
| `updater_app.py` | Main updater service (forever loop, ~3s cycle) |
| `start_updater.bat` | Windows launcher with auto-restart on crash |
| `fyers.py` | Fyers auth + market data (token read-only) |
| `demo_fyers.py` | Paper-trading wrapper (inherits market-data methods) |
| `indicator.py` | SmoothedHeikenAshi + RSI implementations |
| `constants.py` | Shared paths, SHA params, time windows |
| `state_writer.py` | Atomic JSON state files for dashboard |
| `symbols.json` | Symbol configuration |
| `requirements_fyers.txt` | Python dependencies |

## Quick start

```bat
REM Demo mode (default)
start_updater.bat

REM Live mode
start_updater.bat live
```

**Prerequisites:** `dev_scanner` must be running to provide:
- Fyers token at `C:/Ballom_FYR/fyers_token.json`
- Pair JSONs at `C:/Ballom_FYR/option_pairs.json` and `commodity_pairs.json`

## Dashboard compatibility

This branch writes `signal_state.json` with the **exact** same schema used by `app.py` on `dev`, so the dashboard's SHA Signal Analysis section works without changes:

- Signal SHA (3) — Power, Candles, Crossover
- Trend SHA (6) — Power, Candles, Crossover
- GAP% (Signal vs Trend SHA)
- SHA Relationship (Signal ↔ Trend)
- BULLISH / BEARISH trend indicator

## Architecture

```
start_updater.bat
  └── updater_app.py  (forever loop, 3s cycle)
        ├── fyers.py          token loading + fetch_historical_data
        ├── demo_fyers.py     demo mode
        ├── indicator.py      SmoothedHeikenAshi.calculate()
        ├── constants.py      SHA_LENGTH, SHA_TREND_LENGTH, time windows
        ├── state_writer.py   write_signal_state() + log_strategy_event()
        └── reads:
            ├── C:/Ballom_FYR/fyers_token.json       (from dev_scanner)
            ├── C:/Ballom_FYR/option_pairs.json       (from dev_scanner)
            └── C:/Ballom_FYR/commodity_pairs.json    (from dev_scanner)
```

## Branch dependency

```
dev_scanner  →  dev_update  →  dev (dashboard)
  (auth+scan)    (SHA signals)   (dashboard.py reads signal_state.json)
```
