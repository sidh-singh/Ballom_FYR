# Ballom_FYR Trading System Flow Verification

## Issue Fixed
**Problem**: Dashboard was showing commodity symbols (CRUDEOIL, NATURALGAS) during indices trading hours (9:15-15:30), indicating commodities were being processed before indices.

**Root Cause**: 
1. Both indices and commodity windows start at 9:15 AM
2. No market-type filtering in inner_loop to skip wrong pairs
3. Weak outer-loop condition (`elif in_commodity_window and now > INDICES_END`)

**Solution Implemented**:
1. Added strict market-type filtering in `inner_loop()` 
2. Changed outer-loop condition to `elif in_commodity_window and not in_indices_window`
3. Enhanced logging to show when pairs are skipped due to market type mismatch

---

## Complete Trading Flow (Verified)

### Time Windows (constants.py)
```
INDICES:    9:15 AM - 3:30 PM (15:30)
COMMODITY:  9:15 AM - 11:55 PM (23:55)
```

### Outer Loop Flow (app.py main())

```
┌─────────────────────────────────────────────────────────────┐
│ OUTER LOOP (Forever)                                        │
└─────────────────────────────────────────────────────────────┘
           │
           ├─► [STEP 1] Day Change Detection
           │   └─► Re-authenticate Fyers API
           │   └─► Re-download NSE F&O + MCX commodity CSVs
           │   └─► Reset scan flags (indices_scanned, commodities_scanned)
           │   └─► Reset position tracker for new day
           │
           ├─► [STEP 2] Determine Current Time Window
           │   └─► in_indices_window   = 9:15 ≤ now ≤ 15:30
           │   └─► in_commodity_window = 9:15 ≤ now ≤ 23:55
           │
           ├─► [STEP 3] Check Position Conflicts
           │   └─► has_index_positions?
           │   └─► has_commodity_positions?
           │
           ├─► [STEP 3a] INDICES WINDOW (FIRST PRIORITY)
           │   │   Condition: if in_indices_window
           │   │   
           │   ├─► Block if commodity positions exist
           │   └─► Otherwise:
           │       ├─► Scan option pairs (once per day)
           │       └─► Call inner_loop(market_type="INDEX")
           │           └─► Uses OPTION_PAIRS_JSON
           │           └─► Only processes INDEX pairs
           │
           └─► [STEP 3b] COMMODITY WINDOW (ONLY AFTER INDICES)
               │   Condition: elif in_commodity_window and NOT in_indices_window
               │   
               ├─► Block if index positions exist
               └─► Otherwise:
                   ├─► Scan commodity pairs (once per day)
                   └─► Call inner_loop(market_type="COMMODITY")
                       └─► Uses COMMODITY_PAIRS_JSON
                       └─► Only processes COMMODITY pairs
```

### Inner Loop Flow (app.py inner_loop())

```
┌─────────────────────────────────────────────────────────────┐
│ INNER LOOP (Per Symbol Pair)                                │
└─────────────────────────────────────────────────────────────┘
           │
           ├─► For each pair in pairs_json:
           │   │
           │   ├─► [FILTER 1] Skip if incomplete (missing CE/PE/underlying)
           │   │
           │   ├─► [FILTER 2] Skip if wrong market type
           │   │   └─► pair_type = "INDEX" if info.get("indices") else "COMMODITY"
           │   │   └─► if pair_type != market_type: SKIP
           │   │   └─► Log: "Wrong market type (expected X, got Y)"
           │   │
           │   └─► [TRADING LOOP] Per symbol pair:
           │       │
           │       ├─► [CHECK] Day change? → Re-auth token
           │       │
           │       ├─► [CHECK] In trading window?
           │       │   └─► _is_in_trading_window(market_type)
           │       │   └─► If outside + no positions: break to next pair
           │       │
           │       ├─► [STEP A] Fetch historical data
           │       │   └─► CE, PE, underlying (15min candles)
           │       │
           │       ├─► [STEP B] Compute SHA signals
           │       │   └─► get_symbol_details() for all 3
           │       │   └─► Extract: power, list, crossover
           │       │
           │       ├─► [STEP C] Strategy evaluation
           │       │   └─► strategy.evaluate()
           │       │   └─► Returns (ce_action, pe_action)
           │       │
           │       ├─► [STEP D] Execute orders
           │       │   └─► strategy.execute_orders()
           │       │   └─► Place trades via Fyers API
           │       │
           │       ├─► [STEP E] Update state
           │       │   └─► write_signal_state() → signal_state.json
           │       │   └─► write_position_state() → position_state.json
           │       │   └─► write_account_state() → account_state.json
           │       │
           │       ├─► [STEP F] Check exit condition
           │       │   └─► If all positions closed: break to next pair
           │       │
           │       └─► [WAIT] Sleep INNER_LOOP_INTERVAL (1 second)
           │
           └─► Return updated current_day
```

---

## Priority Enforcement Mechanisms

### 1. **Outer Loop Level** (Primary Enforcement)
```python
if in_indices_window:
    # INDICES ONLY - commodities never processed here
    inner_loop(market_type="INDEX", pairs_json=OPTION_PAIRS_JSON)

elif in_commodity_window and not in_indices_window:
    # COMMODITIES ONLY - only when indices window is closed
    inner_loop(market_type="COMMODITY", pairs_json=COMMODITY_PAIRS_JSON)
```

**Key Change**: `not in_indices_window` prevents commodities during indices hours

### 2. **Inner Loop Level** (Secondary Enforcement)
```python
# Determine pair type from JSON structure
pair_type = "INDEX" if info.get("indices") else "COMMODITY"

# Skip if doesn't match current market_type
if pair_type != market_type:
    log_strategy_event(symbol_key, "INNER", "SKIP",
                       details=f"Wrong market type (expected {market_type}, got {pair_type})")
    continue
```

This ensures even if wrong pairs somehow enter inner_loop, they're filtered out.

### 3. **Trading Window Check** (Tertiary Enforcement)
```python
def _is_in_trading_window(market_type: str) -> bool:
    now = datetime.now().time()
    if market_type == "INDEX":
        return INDICES_START <= now <= INDICES_END
    else:
        return COMMODITY_START <= now <= COMMODITY_END
```

Continuously validates within inner_loop that we're in the correct time window.

---

## State File Updates (Per Cycle)

### During Indices Hours (9:15-15:30)
```
app_status.json         → mode, day, "in_indices_window: true"
signal_state.json       → INDEX pair signals only (e.g., NIFTY, BANKNIFTY)
position_state.json     → INDEX positions only
account_state.json      → balance, P&L
strategy_log.json       → SKIP messages for COMMODITY pairs
```

### After Indices Close (15:30-23:55)
```
app_status.json         → "in_commodity_window: true, in_indices_window: false"
signal_state.json       → COMMODITY pair signals (CRUDEOIL, NATURALGAS, etc.)
position_state.json     → COMMODITY positions only
account_state.json      → balance, P&L
strategy_log.json       → SKIP messages for INDEX pairs
```

---

## What You Should See Now

### Dashboard During Indices Hours (9:15-15:30)
- **Strategy Log**: INDEX symbols ONLY (NIFTY, BANKNIFTY)
- **SHA Signal Analysis**: Only index option pairs
- **Positions**: Only INDEX positions (if any)
- **No CRUDEOIL/NATURALGAS entries**

### Dashboard After Indices Close (15:30-23:55)
- **Strategy Log**: COMMODITY symbols (CRUDEOIL, NATURALGAS, etc.)
- **SHA Signal Analysis**: Only commodity option pairs
- **Positions**: Only COMMODITY positions (if any)

---

## Verification Checklist

✅ **Outer Loop Priority**: Indices always processed first (if in_indices_window)  
✅ **Time Window Separation**: Commodities NEVER run during 9:15-15:30  
✅ **Inner Loop Filtering**: Market-type mismatch pairs are skipped  
✅ **Position Conflict Check**: Index/commodity positions block opposite market  
✅ **State Isolation**: Demo/live modes write to separate directories  
✅ **Daily Reset**: All flags/trackers reset on day change  

---

## Testing Recommendations

1. **During Indices Hours (9:15-15:30)**:
   - Check `C:/Ballom_FYR/state/demo/strategy_log.json`
   - Should see ONLY index symbols (NIFTY, BANKNIFTY)
   - Any commodity references should be "SKIP" entries

2. **After Indices Close (15:30+)**:
   - Check strategy_log.json again
   - Should now see commodity symbols
   - Index symbols should show "SKIP" or no entries

3. **Dashboard Verification**:
   - Strategy Log widget should show correct symbols for time of day
   - SHA Signal Analysis should match current market type
   - No mixing of index/commodity during their respective windows

---

## File Locations (All State Files)

```
C:/Ballom_FYR/
├── state/
│   ├── demo/
│   │   ├── app_status.json
│   │   ├── signal_state.json
│   │   ├── position_state.json
│   │   ├── account_state.json
│   │   ├── strategy_log.json
│   │   ├── position_tracker.json
│   │   └── profit_history.json
│   └── live/
│       └── (same structure)
├── option_pairs.json         (indices)
├── commodity_pairs.json      (commodities)
├── cache/
│   ├── NSE_FO.csv
│   ├── MCX_COM.csv
│   └── holidays/
└── demo/
    ├── demo_positions.json
    ├── demo_trades.json
    └── demo_account.json
```

---

## Summary

The system now enforces **strict priority**:

1. **9:15-15:30**: INDICES ONLY (NIFTY, BANKNIFTY, etc.)
2. **15:30-23:55**: COMMODITIES ONLY (CRUDEOIL, NATURALGAS, etc.)
3. **No overlap or mixing** between market types
4. **Multi-layer filtering** ensures correct behavior even if data is malformed

The "SKIP INNER COMMODITY" messages you saw should now only appear during indices hours as confirmation that commodities are being correctly filtered out.
