# Ballom_FYR — Scanner Service (`dev_scanner`)

Standalone hourly option-pair scanning service for NSE F&O and MCX commodities.

## What it does

1. **Authenticates** with Fyers via TOTP and persists the token at
   `C:/Ballom_FYR/fyers_token.json` — any other branch (`dev`, `dev_trader`)
   can reuse this token without re-authenticating.
2. **Refreshes** the token automatically at midnight (day-change detection).
3. **Downloads** NSE options and MCX commodity symbol CSVs once per day.
4. **Scans** every hour during market windows:
   - **Indices** — 9:15 AM to 3:30 PM (NSE F&O)
   - **Commodities** — 9:15 AM to 11:55 PM (MCX)
5. **Writes** the best CE/PE pair for each symbol to:
   - `C:/Ballom_FYR/option_pairs.json` (indices)
   - `C:/Ballom_FYR/commodity_pairs.json` (commodities)
6. **Skips** holidays (NSE calendar fetched & cached annually) while
   honouring special sessions (Budget Saturday, Diwali Muhurat).
7. **Writes** scanner status to `C:/Ballom_FYR/state/<mode>/` so the
   `dev` branch dashboard can display scanner activity.

## Files

| File | Purpose |
|---|---|
| `scanner_app.py` | Main scanner service (forever loop) |
| `start_scanner.bat` | Windows launcher with auto-restart on crash |
| `fyers.py` | Fyers auth, market data, option-chain scanning |
| `demo_fyers.py` | Paper-trading wrapper (inherits all market-data methods) |
| `constants.py` | Shared paths, time windows, column definitions |
| `state_writer.py` | Atomic JSON state files for dashboard compatibility |
| `symbols.json` | Symbol configuration (indices + commodities) |
| `requirements_fyers.txt` | Python dependencies |

## Quick start

```bat
REM Demo mode (default)
start_scanner.bat

REM Live mode
start_scanner.bat live
```

## Output format

### `option_pairs.json`
```json
{
    "NIFTY": {
        "CE": "NFO:NIFTY2570526500CE",
        "PE": "NFO:NIFTY2570526000PE",
        "CE_Strike": 26500,
        "PE_Strike": 26000,
        "Expiry": "2025-07-05",
        "Trend_Score": 0.72,
        "VIX": 12.5,
        "indices": "NSE:NIFTY50-INDEX",
        "qty": 75,
        "hedge": 500
    }
}
```

### `commodity_pairs.json`
```json
{
    "SILVERM": {
        "CE": "MCX:SILVERM25JUL95000CE",
        "PE": "MCX:SILVERM25JUL90000PE",
        "CE_Strike": 95000,
        "PE_Strike": 90000,
        "Expiry": "2025-07-30",
        "Trend_Score": 0.65,
        "VIX": 14.2,
        "commodity": "MCX:SILVERM25JULFUT",
        "qty": 5,
        "hedge": 200
    }
}
```

## Architecture

```
start_scanner.bat
  └── scanner_app.py  (forever loop, 30s poll)
        ├── fyers.py          auth + fetch_option_pair + CSVs + holidays
        ├── demo_fyers.py     demo mode (inherits from Fyers)
        ├── constants.py      paths, time windows
        ├── state_writer.py   app_status.json + strategy_log/
        └── symbols.json      what to scan
```

## Token sharing

The scanner writes the Fyers access token to:
```
C:/Ballom_FYR/fyers_token.json
```
Other branches load and verify this token via `Fyers._load_token()`.
The token is valid for the calendar day it was issued.
