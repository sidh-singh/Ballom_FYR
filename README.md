# Ballom_FYR — Trading Engine (`dev_trading`)

Automated options trading engine for NSE indices and MCX commodities
using Smoothed Heiken-Ashi (SHA) with fibonacci martingale strategy.

## Branch: `dev_trading`

This branch contains the **trading execution engine** — the service that
reads pairs from `dev_scanner`, computes indicators, evaluates strategy,
places orders, and writes all state for the dashboard (`dev` branch).

### Architecture

```
dev_scanner          dev_update           dev_trading          dev
(hourly scan)        (SHA updater)        (THIS BRANCH)        (dashboard)
    │                    │                     │                   │
    ├─ fyers_token.json  │                     │                   │
    ├─ option_pairs.json ──────────────────────►│                   │
    ├─ commodity_pairs.json ───────────────────►│                   │
    │                    │                     │                   │
    │                    │   ┌─────────────────┤                   │
    │                    │   │  Fetch OHLCV    │                   │
    │                    │   │  Compute SHA    │                   │
    │                    │   │  Evaluate       │                   │
    │                    │   │  Execute orders │                   │
    │                    │   └─────────────────┤                   │
    │                    │                     ├─ signal_state.json ──►│
    │                    │                     ├─ position_state.json ►│
    │                    │                     ├─ account_state.json ─►│
    │                    │                     ├─ strategy_log.json ──►│
    │                    │                     ├─ profit_history.json ►│
    │                    │                     └─ position_tracker.json►│
```

### Files

| File | Purpose |
|---|---|
| `trading_app.py` | Main trading engine (forever loop) |
| `start_trading.bat` | Windows launcher with auto-restart |
| `strategy.py` | Heiken-Ashi Martingale strategy (evaluate + execute) |
| `indicator.py` | SmoothedHeikenAshi + RSI indicators |
| `position_tracker.py` | Per-symbol booked profit tracking |
| `pair_manager.py` | CE/PE pair locking (1 pair per symbol) |
| `fyers.py` | Fyers API wrapper (auth, orders, data) |
| `demo_fyers.py` | Paper-trading drop-in for Fyers |
| `constants.py` | Shared params (SHA, RSI, hedge targets, etc.) |
| `state_writer.py` | Atomic JSON state writes for dashboard |
| `symbols.json` | Symbol config (indices + commodities) |
| `requirements_fyers.txt` | Python dependencies |

### Usage

```bash
# Demo mode (paper trading)
start_trading.bat demo

# Live mode (real trading)
start_trading.bat live

# Or directly:
python trading_app.py demo
python trading_app.py live
```

### Prerequisites

1. **dev_scanner** must be running (writes `fyers_token.json` + pair JSONs)
2. Python 3.11+ with dependencies: `pip install -r requirements_fyers.txt`

### State Files Written (for dashboard)

All state files are under `C:/Ballom_FYR/state/<mode>/`:

| File | Contents | Dashboard Use |
|---|---|---|
| `signal_state.json` | SHA power, list, crossover, GAP%, relationship | SHA Signal Analysis panel |
| `position_state.json` | Open positions, netQty, P&L | Positions table |
| `account_state.json` | Balance, realized/unrealized P&L, win rate | KPI cards |
| `strategy_log.json` | Rolling strategy decisions (last 500) | Strategy Log panel |
| `strategy_log/YYYY-MM-DD.json` | Date-partitioned logs | Historical log viewer |
| `position_tracker.json` | Per-symbol booked profit, martingale count | Internal tracking |
| `profit_history.json` | Time-series P&L snapshots | Profit History graph |
| `app_status.json` | Mode, day, trading window, status | Status bar |

### Trading Logic

- **9:15 AM - 3:30 PM**: Trade INDEX pairs (from `option_pairs.json`)
- **3:30 PM - 11:55 PM**: Trade COMMODITY pairs (from `commodity_pairs.json`)
- **Mutual exclusion**: If commodity positions are open, skip indices (and vice versa)
- **Strategy**: SHA-based entry with RSI-triggered fibonacci martingale
- **Profit target**: Configurable per-symbol via `symbols.json` "hedge" field
