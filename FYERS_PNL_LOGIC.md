# Fyers API — P&L Calculation Logic

> Reference document for understanding how Fyers computes `realized_profit`,
> `unrealized_profit`, `pl`, `buyAvg`, `sellAvg`, `netAvg` — and how Ballom FYR
> derives **true cycle profit** from these fields.

---

## 1. Fyers Position API Response Fields

| Field | Type | Description |
|---|---|---|
| `buyAvg` | float | **Weighted average** of ALL buy trades for the symbol **today** |
| `buyQty` | int | Total quantity bought today |
| `sellAvg` | float | **Weighted average** of ALL sell trades for the symbol **today** |
| `sellQty` | int | Total quantity sold today |
| `netAvg` | float | Blended average across all trades (used for unrealized calc) |
| `netQty` | int | `buyQty - sellQty` (positive = long, negative = short) |
| `realized_profit` | float | P&L from closed portions of the position |
| `unrealized_profit` | float | P&L on the remaining open quantity |
| `pl` | float | **Total = realized_profit + unrealized_profit** |
| `ltp` | float | Last traded price |
| `productType` | str | `"MARGIN"`, `"INTRADAY"`, `"CNC"` |

---

## 2. How Fyers Calculates Each Field

### 2.1 buyAvg / sellAvg (Blended Averages)

Fyers computes a **volume-weighted average** across ALL trades of the same
side (buy or sell) for the **entire trading day**, not per cycle.

```
buyAvg = Σ(buy_price × buy_qty) / Σ(buy_qty)
sellAvg = Σ(sell_price × sell_qty) / Σ(sell_qty)
```

**Example — 2 buy-sell cycles on the same symbol in one day:**

| Trade | Action | Qty | Price |
|---|---|---|---|
| 1 | BUY | 65 | 160 |
| 2 | SELL (close) | 65 | 180 |
| 3 | BUY (re-entry) | 65 | 172 |

After Trade 3:
```
buyAvg = (160×65 + 172×65) / (65+65) = 166.0   ← NOT 172!
sellAvg = 180.0
buyQty = 130, sellQty = 65, netQty = 65
```

> **Key insight:** `buyAvg` is contaminated by the first cycle's buy at 160.
> It does NOT represent the current position's entry price (172).

### 2.2 realized_profit

```
realized_profit = (sellAvg - buyAvg) × min(buyQty, sellQty)
```

Using the example above:
```
realized_profit = (180 - 166) × 65 = 910
```

But the **true** Cycle 1 profit was `(180 - 160) × 65 = 1300`.

> **Key insight:** `realized_profit` is **wrong per-cycle** because it uses
> the blended `buyAvg` (166) instead of the actual Cycle 1 entry (160).

### 2.3 unrealized_profit

```
unrealized_profit = (LTP - netAvg) × netQty    (for long)
unrealized_profit = (netAvg - LTP) × netQty    (for short)
```

Where `netAvg` ≈ `buyAvg` when long, `sellAvg` when short.

Using the example (LTP = 185):
```
unrealized_profit = (185 - 166) × 65 = 1235
```

But the **true** Cycle 2 floating P&L is `(185 - 172) × 65 = 845`.

> **Key insight:** `unrealized_profit` is also **wrong per-cycle** due to blended averages.

### 2.4 pl (Total P&L) — THE ONLY RELIABLE FIELD

```
pl = realized_profit + unrealized_profit
```

Using the example:
```
pl = 910 + 1235 = 2145
```

Cross-check with actual trades:
- Cycle 1 closed profit: (180 - 160) × 65 = 1300
- Cycle 2 floating: (185 - 172) × 65 = 845
- True total: 1300 + 845 = **2145** ✓

> **`pl` is ALWAYS mathematically correct** regardless of how many cycles
> you've traded. The blended-average errors in `realized_profit` and
> `unrealized_profit` cancel out perfectly in the sum.

---

## 3. True Cycle Profit — The `effective_pl` Formula

Since `pl` is the only reliable field, Ballom FYR uses it to derive per-cycle P&L:

```
effective_pl = api_total_pl - booked_profit
```

Where:
- `api_total_pl` = Fyers' `pl` field (realized + unrealized)
- `booked_profit` = the value of `pl` captured at the moment the **previous** cycle closed

### 3.1 How It Works Step-by-Step

**Cycle 1 — Entry:**
```
booked_profit = 0 (no previous close)
pl = 0 (just entered)
effective_pl = 0 - 0 = 0
```

**Cycle 1 — Running (LTP moved up):**
```
pl = 1300 (Fyers total P&L)
effective_pl = 1300 - 0 = 1300  ← true floating profit ✓
```

**Cycle 1 — Close triggered (effective_pl > hedge):**
```
Strategy closes position.
booked_profit ← pl = 1300  (snapshot at close)
```

**Cycle 2 — Re-entry on same symbol:**
```
pl = 1300 + small_unrealized (Fyers accumulates)
   = e.g. 1310
effective_pl = 1310 - 1300 = 10  ← starts near 0 ✓
```

**Cycle 2 — Running (LTP = 185):**
```
pl = 2145
effective_pl = 2145 - 1300 = 845  ← true Cycle 2 floating profit ✓
```

### 3.2 Why NOT Use `unrealized_profit` Directly?

| Approach | Cycle 2 Value | Correct? |
|---|---|---|
| `unrealized_profit` from API | 1235 | ❌ Contaminated by blended buyAvg |
| `effective_pl = pl - booked_profit` | 845 | ✅ True cycle P&L |

The blended average makes `unrealized_profit` include phantom gains from
previous cycles. Only `pl - booked_profit` gives the clean current-cycle P&L.

---

## 4. Exit Target Calculation

```
adj_hedge = hedge + estimated_charges
```

| Component | Source | Example |
|---|---|---|
| `hedge` | `symbols.json` → `"hedge": 500` | ₹500 |
| `charges` | `estimate_trade_charges(qty, ltp, num_orders)` | ~₹70 |
| **adj_hedge** | | **~₹570** |

Exit triggers when:
```python
if effective_pl > adj_hedge:
    CLOSE_BUY  # take profit
```

### 4.1 Charge Estimation Breakdown

```
brokerage     = ₹20 × num_orders (flat per order)
STT           = 0.0625% × sell-side turnover
exchange_txn  = 0.0495% × turnover × 2 (both sides)
GST           = 18% × (brokerage + exchange + SEBI)
stamp_duty    = 0.003% × buy-side turnover
SEBI          = ₹10 per crore turnover
```

For a typical NIFTY options trade (65 qty × ₹185 LTP, 2 orders):
```
brokerage  = 20 × 2          = ₹40.00
turnover   = 65 × 185        = ₹12,025
STT        = 0.000625 × 12025 = ₹7.52
exchange   = 0.000495 × 12025 × 2 = ₹11.90
SEBI       = (12025 × 2) / 10000000 × 10 = ₹0.02
GST        = 0.18 × (40 + 11.90 + 0.02) = ₹9.35
stamp      = 0.00003 × 12025  = ₹0.36
─────────────────────────────────────
TOTAL      ≈ ₹69.15
```

---

## 5. Booked Profit — When & How It Updates

The `booked_profit` for a symbol is stored in `position_tracker.json` and
updates **only when a close is confirmed** (netQty reaches 0):

```
1. Strategy detects effective_pl > adj_hedge
2. Strategy sends CLOSE_BUY order → marks symbol as "pending close"
3. Next evaluation cycle: checks if netQty == 0
4. If yes → confirm_close():
     booked_profit = current api_total_pl (fresh read)
     effective_pl for next cycle starts from ~0
5. If no → re-issue CLOSE_BUY (retry)
```

> **Important:** `booked_profit` is NOT updated when the close order is sent.
> It's updated only when the position actually reaches `netQty = 0`. This
> prevents corrupted state if the order is rejected or partially filled.

---

## 6. Same Pair Re-Entry vs New Pair

### 6.1 Same Pair (same CE/PE symbols, same day)

Fyers **accumulates** all trades into blended averages. The `pl` field keeps
growing. Our formula handles this correctly:

```
Cycle 1: effective_pl = pl - 0 = pl
Cycle 2: effective_pl = pl - booked_profit_after_cycle1
Cycle 3: effective_pl = pl - booked_profit_after_cycle2
```

Each cycle independently targets `hedge + charges`. No carryover inflation.

### 6.2 New Pair (different CE/PE strikes after pair rotation)

When PairManager selects new strikes (e.g., different expiry or strike price),
the **new symbols have no history** in `position_tracker.json`:

```
booked_profit = 0 (no entry for new symbol)
effective_pl = pl - 0 = pl (fresh start)
```

This works correctly — new symbols start clean.

### 6.3 Day Change

`position_tracker.json` resets automatically on day change:
```python
if self._data.get("_date") != date.today().isoformat():
    self._data = {"_date": current_date}  # wipe all symbols
```

All `booked_profit` values reset to 0. Fresh start every day.

---

## 7. Martingale & Effective P&L

When martingale adds quantity to an existing position:

```
Entry:     BUY 65 @ 172  → avg = 172
Martingale: BUY 130 @ 160 → avg = (172×65 + 160×130) / 195 = 164

Fyers buyAvg blends ALL buys (including previous cycles).
But effective_pl = pl - booked_profit still works correctly.
```

The martingale level determines:
- **Loss threshold:** `fibonacci[level]² × hedge` before next add
- **Next qty:** fibonacci-based escalation (65 → 130 → 195 → ...)
- **Exit target:** same `hedge + charges` (no inflation)

---

## 8. Summary — Rules to Remember

| Rule | Details |
|---|---|
| **Never trust `unrealized_profit`** | Contaminated by blended averages after re-entry |
| **Never trust `realized_profit`** | Same blended-average contamination |
| **Always use `pl` (total)** | Sum is always mathematically correct |
| **Per-cycle P&L = `pl - booked_profit`** | The `effective_pl` formula |
| **Exit target = `hedge + charges`** | No carryover from previous cycles |
| **Update `booked_profit` only on confirmed close** | When netQty reaches 0, not when order is sent |
| **Day change resets everything** | `position_tracker.json` wipes on new date |
| **New symbols start clean** | `booked_profit` defaults to 0 |

---

## 9. Code References

| Concept | File | Key Function/Section |
|---|---|---|
| effective_pl calculation | `strategy.py` | `evaluate()` — lines using `tracker.get_effective_pl()` |
| booked_profit update | `position_tracker.py` | `record_close()` |
| exit target (adj_hedge) | `strategy.py` | `ce_adj_hedge = hedge + ce_charges` |
| charge estimation | `constants.py` | `estimate_trade_charges()` |
| pending-close lifecycle | `strategy.py` | `mark_pending_close()` / `confirm_close()` |
| position API wrapper | `fyers.py` | `position()` → returns DataFrame + OverallPosition |
| demo simulation | `demo_fyers.py` | `buy()` / `sell()` / `position()` |
| tracker persistence | `position_tracker.py` | `_save_tracker()` / JSON on disk |
