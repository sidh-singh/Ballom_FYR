"""
app.py — Main entry-point launched by start_job.bat.

Usage:  python app.py [demo|live]

Architecture
────────────
Outer Loop (runs forever):
  Step 1 — Day-change detection  → re-auth + persist token + fetch holidays
  Step 2 — Daily data download   → NSE options & MCX commodities (once/day)
  Step 3 — Time-window routing   → scan option pairs, dump CE/PE + qty to JSON
  Step 4 — Inner loop            → fetch history → SHA → strategy → trade → wait

Inner Loop (blocking, per symbol):
  Step A — Fetch history for CE, PE, underlying
  Step B — Compute SHA + power/list/crossover for all three
  Step C — Strategy.evaluate() → get OrderActions
  Step D — Strategy.execute_orders() → place trades
  Step E — Monitor positions; if all closed → break back to outer loop
  Step F — Day-change inside inner loop → re-auth to prevent token expiry

All runtime information is dumped to JSON state files under
C:/Ballom_FYR/state/ — no print/log statements in the loops.
"""

import sys
import json
import shutil
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path
from time import sleep
from dataclasses import asdict

from fyers import Fyers
from demo_fyers import DemoFyers
from indicator import SmoothedHeikenAshi
from strategy import HeikenAshiMartingale
from position_tracker import PositionTracker
from pair_manager import PairManager
from constants import (
    Transaction,
    SYMBOLS_JSON,
    OPTION_PAIRS_JSON,
    COMMODITY_PAIRS_JSON,
    INDICES_START,
    INDICES_END,
    COMMODITY_START,
    COMMODITY_END,
    SHA_LENGTH,
    SHA_MA_TYPE,
    SHA_TREND_LENGTH,
    SHA_TREND_MA_TYPE,
    GAP_RANGE_LOW,
    GAP_RANGE_HIGH,
    DEFAULT_TIMEFRAME,
    DEFAULT_CANDLES,
    INNER_LOOP_INTERVAL,
    STRATEGY_HEDGE_INDEX,
    STRATEGY_HEDGE_COMMODITY,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    write_signal_state,
    write_position_state,
    write_account_state,
    log_strategy_event,
)


def load_symbols_config() -> dict:
    """Load symbols.json (indices + commodities config)."""
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY SETUP
# ═══════════════════════════════════════════════════════════════════════════════

def daily_setup(fyers: Fyers, force_auth: bool = False):
    """
    Run once per new trading day:
      1. (Re-)authorize and cache token
      2. Download NSE options + MCX commodity CSVs to cache
      3. Fetch / cache trading holidays for the year
    Returns (option_df, mcx_df, holidays, special_sessions).
    """
    fyers.ensure_session(force=force_auth)
    option_df = Fyers.download_option_data()
    mcx_df = Fyers.download_mcx_data()
    holidays, special_sessions = Fyers.load_holiday_set()
    return option_df, mcx_df, holidays, special_sessions


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON PERSISTENCE  (atomic write)
# ═══════════════════════════════════════════════════════════════════════════════

def _write_json_atomic(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically via temp-file + move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=4)
        shutil.move(tmp, str(path))
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE-DUMP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _dump_positions_and_account(fyers: Fyers) -> None:
    """Snapshot current positions + account state to JSON."""
    pos_df, overall = fyers.position()

    # Round all float columns in position DataFrame to 2 decimal places
    if not pos_df.empty:
        float_cols = pos_df.select_dtypes(include=["float", "float64"]).columns
        # Deduplicate column names to avoid pandas "Columns must be same length as key" error
        float_cols = float_cols.drop_duplicates()
        pos_df[float_cols] = pos_df[float_cols].round(2)

    rows = pos_df.to_dict(orient="records") if not pos_df.empty else []
    overall_dict = asdict(overall)
    for k, v in overall_dict.items():
        if isinstance(v, float):
            overall_dict[k] = round(v, 2)
    write_position_state(rows, overall_dict)

    funds = fyers.funds()
    fund_map = {}
    for item in funds.get("fund_limit", []):
        fund_map[item.get("title", "")] = item.get("equityAmount", 0)

    write_account_state(
        balance=round(fund_map.get("Total Balance", 0), 2),
        utilized=round(fund_map.get("Utilized Amount", 0), 2),
        realized_pnl=round(fund_map.get("Realized P&L", overall.pl_realized), 2),
        unrealized_pnl=round(overall.pl_unrealized, 2),
        total_trades=getattr(fyers, "account", None) and fyers.account.total_trades or 0,
        winning_trades=getattr(fyers, "account", None) and fyers.account.winning_trades or 0,
        losing_trades=getattr(fyers, "account", None) and fyers.account.losing_trades or 0,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION-PAIR SCANNING  → JSON
# ═══════════════════════════════════════════════════════════════════════════════

def scan_and_dump_index_pairs(
    fyers: Fyers,
    indices: list,
    option_df,
    pair_manager: PairManager | None = None,
) -> int:
    """
    For each index in config, resolve the active CE/PE pair via PairManager:
      1. If a locked pair has open positions → keep it (no scan)
      2. If overnight positions detected     → lock them (no scan)
      3. Otherwise                           → fresh scan, lock result

    Exactly 1 CE + 1 PE per index symbol_key at any time.
    Returns the number of valid pairs written to option_pairs.json.
    """
    result = {}

    for entry in indices:
        symbol_key = entry["symbol"]
        underlying = entry["indices"]
        qty_times  = entry.get("qty_times", 1)
        hedge      = entry.get("hedge", STRATEGY_HEDGE_INDEX)

        # ── PairManager resolution (locked pair / overnight / fresh scan) ─
        if pair_manager:
            def _scan_index(sym_key=symbol_key, und=underlying, qt=qty_times, hdg=hedge):
                """Fresh-scan closure for this index."""
                pair = fyers.fetch_option_pair(und, asset_type="INDEX")
                debug_trail = pair.get("Debug", "")
                if not pair.get("Recommended"):
                    log_strategy_event(sym_key, "SCAN", "SKIP_INDEX",
                                       details=f"{pair.get('Message', 'skipped')} || {debug_trail}")
                    return None
                try:
                    lot = Fyers.get_lot_size(pair["CE_Symbol"], option_df)
                except ValueError as e:
                    log_strategy_event(sym_key, "SCAN", "LOT_ERROR", details=str(e))
                    return None
                qty = int(lot * qt)
                ce_sym = pair.get("CE_Symbol", "")
                pe_sym = pair.get("PE_Symbol", "")
                if not ce_sym or not pe_sym:
                    log_strategy_event(sym_key, "SCAN", "INVALID_PAIR",
                                       details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}")
                    return None
                log_strategy_event(
                    sym_key, "SCAN", "INDEX_PAIR_FOUND", qty=qty,
                    details=f"CE={ce_sym} PE={pe_sym} Exp={pair['Expiry']} || {debug_trail}",
                )
                return {
                    "CE": ce_sym, "PE": pe_sym,
                    "CE_Strike": pair["CE_Strike"], "PE_Strike": pair["PE_Strike"],
                    "Expiry": pair["Expiry"], "Trend_Score": pair["Trend_Score"],
                    "VIX": pair["VIX"], "indices": und, "qty": qty,
                    "hedge": hdg,
                }

            resolved = pair_manager.resolve_pair(fyers, symbol_key, _scan_index)
            if resolved:
                # Ensure qty is set (overnight detection may lack it)
                if not resolved.get("qty"):
                    try:
                        lot = Fyers.get_lot_size(resolved["CE"], option_df)
                        resolved["qty"] = int(lot * qty_times)
                        pair_manager.lock_pair(symbol_key, resolved["CE"], resolved["PE"],
                                               **{k: v for k, v in resolved.items() if k not in ("CE", "PE")})
                    except Exception:
                        resolved["qty"] = 0
                if not resolved.get("indices"):
                    resolved["indices"] = underlying
                result[symbol_key] = {
                    k: v for k, v in resolved.items()
                    if k not in ("locked_at", "source")
                }
            continue

        # ── Fallback: no PairManager (backward compat) ────────────────────
        pair = fyers.fetch_option_pair(underlying, asset_type="INDEX")
        debug_trail = pair.get("Debug", "")
        if not pair.get("Recommended"):
            log_strategy_event(symbol_key, "SCAN", "SKIP_INDEX",
                               details=f"{pair.get('Message', 'skipped')} || {debug_trail}")
            continue
        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], option_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue
        qty = int(lot * qty_times)
        ce_sym = pair.get("CE_Symbol", "")
        pe_sym = pair.get("PE_Symbol", "")
        if not ce_sym or not pe_sym:
            log_strategy_event(symbol_key, "SCAN", "INVALID_PAIR",
                               details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}")
            continue
        result[symbol_key] = {
            "CE": ce_sym, "PE": pe_sym,
            "CE_Strike": pair["CE_Strike"], "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"], "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"], "indices": underlying, "qty": qty,
            "hedge": hedge,
        }
        log_strategy_event(
            symbol_key, "SCAN", "INDEX_PAIR_FOUND", qty=qty,
            details=f"CE={ce_sym} PE={pe_sym} Exp={pair['Expiry']} || {pair.get('Debug', '')}",
        )

    log_strategy_event("SYSTEM", "SCAN", "INDEX_SCAN_DONE",
                       details=f"{len(result)} valid index pairs found")
    _write_json_atomic(OPTION_PAIRS_JSON, result)
    return len(result)


def scan_and_dump_commodity_pairs(
    fyers: Fyers,
    commodities: list,
    mcx_df,
    pair_manager: PairManager | None = None,
) -> int:
    """
    For each enabled commodity, resolve the active CE/PE pair via PairManager:
      1. If a locked pair has open positions → keep it (no scan)
      2. If overnight positions detected     → lock them (no scan)
      3. Otherwise                           → fresh scan, lock result

    Exactly 1 CE + 1 PE per commodity symbol_key at any time.
    Returns the number of valid pairs written to commodity_pairs.json.
    """
    result = {}

    for entry in commodities:
        if not entry.get("enabled", True):
            continue

        symbol_key = entry["symbol"]
        generic    = entry["commodity"]
        qty_times  = entry.get("qty_times", 1)
        hedge      = entry.get("hedge", STRATEGY_HEDGE_COMMODITY)

        # ── PairManager resolution ────────────────────────────────────────
        if pair_manager:
            def _scan_commodity(sym_key=symbol_key, gen=generic, qt=qty_times, hdg=hedge):
                """Fresh-scan closure for this commodity."""
                actual = Fyers.resolve_commodity_symbol(gen, mcx_df)
                if not actual:
                    log_strategy_event(sym_key, "SCAN", "RESOLVE_FAIL",
                                       details=f"Could not resolve {gen}")
                    return None
                pair = fyers.fetch_option_pair(
                    actual, asset_type="COMMODITY",
                    max_premium_per_lot=55000, min_trend_score=0.35,
                )
                debug_trail = pair.get("Debug", "")
                if not pair.get("Recommended"):
                    log_strategy_event(sym_key, "SCAN", "SKIP_COMMODITY",
                                       details=f"{pair.get('Message', 'skipped')} || {debug_trail}")
                    return None
                try:
                    lot = Fyers.get_lot_size(pair["CE_Symbol"], mcx_df)
                except ValueError as e:
                    log_strategy_event(sym_key, "SCAN", "LOT_ERROR", details=str(e))
                    return None
                qty = int(lot * qt)
                ce_sym = pair.get("CE_Symbol", "")
                pe_sym = pair.get("PE_Symbol", "")
                if not ce_sym or not pe_sym:
                    log_strategy_event(sym_key, "SCAN", "INVALID_PAIR",
                                       details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}")
                    return None
                log_strategy_event(
                    sym_key, "SCAN", "COMMODITY_PAIR_FOUND", qty=qty,
                    details=f"CE={ce_sym} PE={pe_sym} UND={actual} || {debug_trail}",
                )
                return {
                    "CE": ce_sym, "PE": pe_sym,
                    "CE_Strike": pair["CE_Strike"], "PE_Strike": pair["PE_Strike"],
                    "Expiry": pair["Expiry"], "Trend_Score": pair["Trend_Score"],
                    "VIX": pair["VIX"], "commodity": actual, "qty": qty,
                    "hedge": hdg,
                }

            resolved = pair_manager.resolve_pair(fyers, symbol_key, _scan_commodity)
            if resolved:
                if not resolved.get("qty"):
                    try:
                        lot = Fyers.get_lot_size(resolved["CE"], mcx_df)
                        resolved["qty"] = int(lot * qty_times)
                        pair_manager.lock_pair(symbol_key, resolved["CE"], resolved["PE"],
                                               **{k: v for k, v in resolved.items() if k not in ("CE", "PE")})
                    except Exception:
                        resolved["qty"] = 0
                if not resolved.get("commodity"):
                    actual = Fyers.resolve_commodity_symbol(generic, mcx_df)
                    resolved["commodity"] = actual or generic
                result[symbol_key] = {
                    k: v for k, v in resolved.items()
                    if k not in ("locked_at", "source")
                }
            continue

        # ── Fallback: no PairManager ──────────────────────────────────────
        actual = Fyers.resolve_commodity_symbol(generic, mcx_df)
        if not actual:
            log_strategy_event(symbol_key, "SCAN", "RESOLVE_FAIL",
                               details=f"Could not resolve {generic}")
            continue
        pair = fyers.fetch_option_pair(
            actual, asset_type="COMMODITY",
            max_premium_per_lot=55000, min_trend_score=0.35,
        )
        debug_trail = pair.get("Debug", "")
        if not pair.get("Recommended"):
            log_strategy_event(symbol_key, "SCAN", "SKIP_COMMODITY",
                               details=f"{pair.get('Message', 'skipped')} || {debug_trail}")
            continue
        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], mcx_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue
        qty = int(lot * qty_times)
        ce_sym = pair.get("CE_Symbol", "")
        pe_sym = pair.get("PE_Symbol", "")
        if not ce_sym or not pe_sym:
            log_strategy_event(symbol_key, "SCAN", "INVALID_PAIR",
                               details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}")
            continue
        result[symbol_key] = {
            "CE": ce_sym, "PE": pe_sym,
            "CE_Strike": pair["CE_Strike"], "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"], "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"], "commodity": actual, "qty": qty,
            "hedge": hedge,
        }
        log_strategy_event(
            symbol_key, "SCAN", "COMMODITY_PAIR_FOUND", qty=qty,
            details=f"CE={ce_sym} PE={pe_sym} UND={actual} || {pair.get('Debug', '')}",
        )

    log_strategy_event("SYSTEM", "SCAN", "COMMODITY_SCAN_DONE",
                       details=f"{len(result)} valid commodity pairs found")
    _write_json_atomic(COMMODITY_PAIRS_JSON, result)
    return len(result)


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL HELPERS  (ported from FyersHeikenAshiMartingale.get_symbol_details)
# ═══════════════════════════════════════════════════════════════════════════════

def get_symbol_details(
    raw_df,
    sha_length: int = SHA_LENGTH,
    sha_type: str = SHA_MA_TYPE,
):
    """
    Compute Smoothed Heiken-Ashi on *raw_df* (OHLCV) and derive:
        lt_symbol_power  — count of bullish candles in last 7
        lt_symbol_list   — [1|0, ...] most-recent-first
        crossover        — price vs SHA position [-3..-1, 1..3]
        sha_debug        — last 7 SHA OHLC dicts (most-recent-first) for diagnostics

    Returns (lt_symbol_power, lt_symbol_list, crossover, sha_debug).
    """
    import math

    lt_sha = SmoothedHeikenAshi.calculate(
        df=raw_df,
        smooth_length=sha_length,
        smooth_ma_type=sha_type,
        after_smooth_length=sha_length,
        after_smooth_ma_type=sha_type,
    )

    threshold = 0
    lt_symbol_power = 0
    lt_symbol_list = []
    crossover = []
    sha_debug = []

    for i in range(-1, -8, -1):
        sha_o = lt_sha["Open"].iloc[i]
        sha_h = lt_sha["High"].iloc[i]
        sha_l = lt_sha["Low"].iloc[i]
        sha_c = lt_sha["Close"].iloc[i]

        # Guard against NaN SHA values (insufficient candles)
        if math.isnan(sha_o) or math.isnan(sha_h) or math.isnan(sha_l) or math.isnan(sha_c):
            lt_symbol_list.append(0)
            crossover.append(-2)
            sha_debug.append({
                "ts": str(raw_df["Timestamp"].iloc[i]) if "Timestamp" in raw_df.columns else "",
                "O": 0, "H": 0, "L": 0, "C": 0, "dir": "NaN",
            })
            continue

        ha_range = abs(sha_h - sha_l)
        if ha_range == 0:
            ha_range = 1e-9

        lt_diff = (sha_c - sha_o) / ha_range

        lt_sha_diff = 1 if lt_diff >= threshold else 0
        lt_symbol_list.append(lt_sha_diff)
        lt_symbol_power += lt_sha_diff

        ct_p_high = raw_df["High"].iloc[i]
        ct_p_low = raw_df["Low"].iloc[i]

        if lt_sha_diff == 1:
            if ct_p_low >= sha_h:
                crossover.append(3)
            elif ct_p_high <= sha_l:
                crossover.append(1)
            else:
                crossover.append(2)
        else:
            if ct_p_high <= sha_l:
                crossover.append(-3)
            elif ct_p_low >= sha_h:
                crossover.append(-1)
            else:
                crossover.append(-2)

        # ── diagnostic: capture SHA OHLC + timestamp for dashboard ────
        ts = str(raw_df["Timestamp"].iloc[i]) if "Timestamp" in raw_df.columns else ""
        sha_debug.append({
            "ts": ts,
            "O": round(float(sha_o), 2),
            "H": round(float(sha_h), 2),
            "L": round(float(sha_l), 2),
            "C": round(float(sha_c), 2),
            "dir": "BULL" if lt_sha_diff == 1 else "BEAR",
        })

    return lt_symbol_power, lt_symbol_list, crossover, sha_debug


def get_trend_details(
    raw_df,
    sha_length: int = SHA_TREND_LENGTH,
    sha_type: str = SHA_TREND_MA_TYPE,
):
    """
    Compute Trend SHA (longer-period) on *raw_df* and derive the same
    outputs as get_symbol_details:
        power, list, crossover, sha_debug.

    Uses the same logic but with a longer SHA length (default 11)
    for trend identification.
    """
    return get_symbol_details(raw_df, sha_length=sha_length, sha_type=sha_type)


def compute_sha_gap(signal_sha_debug: list, trend_sha_debug: list) -> list:
    """
    Compute GAP% between Signal SHA and Trend SHA for each candle.

    GAP% = ((signal_mid - trend_mid) / trend_mid) × 100
    where mid = (High + Low) / 2

    Trend SHA is the base (denominator).

    Returns a list of dicts: [{gap_pct, signal_mid, trend_mid}, ...] most-recent first.
    Aligns by index (both lists are most-recent-first).
    """
    gap_list = []
    for i in range(min(len(signal_sha_debug), len(trend_sha_debug))):
        sig = signal_sha_debug[i]
        trd = trend_sha_debug[i]

        sig_h = sig.get("H", 0)
        sig_l = sig.get("L", 0)
        trd_h = trd.get("H", 0)
        trd_l = trd.get("L", 0)

        sig_mid = (sig_h + sig_l) / 2
        trd_mid = (trd_h + trd_l) / 2

        if trd_mid == 0:
            gap_pct = 0.0
        else:
            gap_pct = ((sig_mid - trd_mid) / trd_mid) * 100

        gap_list.append({
            "gap_pct": round(gap_pct, 4),
            "signal_mid": round(sig_mid, 2),
            "trend_mid": round(trd_mid, 2),
        })

    return gap_list


def compute_sha_relationship(gap_list: list) -> dict:
    """
    Analyze the relationship between Signal SHA and Trend SHA based on
    the GAP% time series (most-recent-first).

    Returns
    ───────
    dict with:
        status   : "DIVERGING" | "CONVERGING" | "PARALLEL" | "CLOSE"
        strength : 0.0 – 1.0  (how strong the pattern is)
        avg_gap  : average absolute GAP% across the window
        delta    : change rate between recent and older halves
    """
    if not gap_list or len(gap_list) < 2:
        return {"status": "UNKNOWN", "strength": 0.0, "avg_gap": 0.0, "delta": 0.0}

    abs_gaps = [abs(g["gap_pct"]) for g in gap_list]
    avg_gap = sum(abs_gaps) / len(abs_gaps)

    # ── CLOSE: SHAs nearly overlapping ────────────────────────────────
    CLOSE_THRESHOLD = 1.0  # < 1% average gap = close / overlapping
    if avg_gap < CLOSE_THRESHOLD:
        strength = round(1.0 - avg_gap / CLOSE_THRESHOLD, 4)
        return {"status": "CLOSE", "strength": strength,
                "avg_gap": round(avg_gap, 4), "delta": 0.0}

    # ── Trend analysis: compare recent half vs older half ─────────────
    mid = len(abs_gaps) // 2
    recent = abs_gaps[:max(mid, 1)]       # first half  (more recent)
    older  = abs_gaps[max(mid, 1):]       # second half (older)

    avg_recent = sum(recent) / len(recent)
    avg_older  = sum(older) / len(older) if older else avg_recent

    delta = avg_recent - avg_older  # positive = gap widening

    PARALLEL_THRESHOLD = 0.5  # < 0.5% change between halves = parallel
    if abs(delta) < PARALLEL_THRESHOLD:
        strength = round(1.0 - abs(delta) / PARALLEL_THRESHOLD, 4)
        return {"status": "PARALLEL", "strength": strength,
                "avg_gap": round(avg_gap, 4), "delta": round(delta, 4)}
    elif delta > 0:
        strength = round(min(1.0, delta / 5.0), 4)
        return {"status": "DIVERGING", "strength": strength,
                "avg_gap": round(avg_gap, 4), "delta": round(delta, 4)}
    else:
        strength = round(min(1.0, abs(delta) / 5.0), 4)
        return {"status": "CONVERGING", "strength": strength,
                "avg_gap": round(avg_gap, 4), "delta": round(delta, 4)}


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP — the blocking trading loop
# ═══════════════════════════════════════════════════════════════════════════════

def _has_open_positions(fyers: Fyers, ce_symbol: str, pe_symbol: str) -> bool:
    """Return True if either CE or PE has a non-zero qty in positions."""
    pos_df, _ = fyers.position()
    if pos_df.empty:
        return False
    for sym in (ce_symbol, pe_symbol):
        row = pos_df[
            (pos_df["symbol"] == sym)
            & (pos_df["productType"] == "MARGIN")
        ]
        if not row.empty and int(row["netQty"].iloc[0]) != 0:
            return True
    return False


def _is_in_trading_window(market_type: str) -> bool:
    """Check if we are currently inside the trading time window."""
    now = datetime.now().time()
    if market_type == "INDEX":
        return INDICES_START <= now <= INDICES_END
    else:
        return COMMODITY_START <= now <= COMMODITY_END


def inner_loop(
    fyers: Fyers,
    strategy: HeikenAshiMartingale,
    pairs_json: Path,
    market_type: str,
    holidays: set[str],
    special_sessions: list[dict],
    inner_day_ref: date,
    mode: str = "demo",
    timeframe: str = DEFAULT_TIMEFRAME,
    candles: int = DEFAULT_CANDLES,
    tracker: PositionTracker | None = None,
    pair_manager: PairManager | None = None,
) -> date:
    """
    Blocking inner loop: for every symbol in *pairs_json* —

      1. Fetch historical OHLCV for CE, PE, and underlying
      2. Compute SHA + get_symbol_details for all three
      3. strategy.evaluate() → (ce_action, pe_action)
      4. strategy.execute_orders()
      5. Wait / monitor — if all positions close → clear pair lock → break
      6. If day changes → re-auth token inside the loop

    Returns the (possibly updated) current_day so the outer loop
    can stay in sync.
    """
    pairs = _load_json(pairs_json)
    if not pairs:
        write_app_status(mode, str(inner_day_ref), status="idle",
                         message=f"No pairs in {pairs_json.name} — scan may have failed")
        log_strategy_event("SYSTEM", "INNER", "NO_PAIRS",
                           details=f"Empty file: {pairs_json.name} | market_type={market_type}")
        return inner_day_ref

    current_day = inner_day_ref

    # Filter to only valid, matching pairs
    valid_pairs = {}
    for symbol_key, info in pairs.items():
        ce_symbol  = info.get("CE", "")
        pe_symbol  = info.get("PE", "")
        underlying = info.get("indices", info.get("commodity", ""))
        base_qty   = info.get("qty", 0)

        if not ce_symbol or not pe_symbol or not underlying:
            log_strategy_event(symbol_key, "INNER", "SKIP_INCOMPLETE",
                               details=f"CE={ce_symbol!r} PE={pe_symbol!r} UND={underlying!r}")
            continue

        pair_type = "INDEX" if info.get("indices") else "COMMODITY"
        if pair_type != market_type:
            continue  # silently skip wrong type

        if base_qty <= 0:
            log_strategy_event(symbol_key, "INNER", "SKIP_QTY",
                               details=f"Invalid qty={base_qty}")
            continue

        valid_pairs[symbol_key] = info

    if not valid_pairs:
        log_strategy_event("SYSTEM", "INNER", "NO_VALID_PAIRS",
                           details=f"{len(pairs)} pairs in file, 0 valid for {market_type}")
        return inner_day_ref

    log_strategy_event("SYSTEM", "INNER", "TRADING",
                       details=f"{len(valid_pairs)} valid {market_type} pairs: {', '.join(valid_pairs.keys())}")

    for symbol_key, info in valid_pairs.items():
        ce_symbol  = info["CE"]
        pe_symbol  = info["PE"]
        underlying = info.get("indices", info.get("commodity", ""))
        base_qty   = info["qty"]
        # Per-pair profit target: from pairs JSON (set by scan), with
        # fallback to market-type default
        default_hedge = STRATEGY_HEDGE_INDEX if market_type == "INDEX" else STRATEGY_HEDGE_COMMODITY
        pair_hedge = info.get("hedge", default_hedge)

        write_app_status(mode, str(current_day), status="trading",
                         message=f"Trading {symbol_key} | CE={ce_symbol} PE={pe_symbol}")

        snapshot_counter = 0
        # Track if a position was ever opened on this pair.
        # Pre-check: if pair already has open positions (overnight carry),
        # mark as True so we break correctly when they close.
        had_positions_ever = _has_open_positions(fyers, ce_symbol, pe_symbol)

        # ── trading loop for this symbol pair ──────────────────────────────
        while True:
            # ── Step F: Day-change re-authorization ────────────────────────
            today = date.today()
            if today != current_day:
                fyers.ensure_session(force=True)
                holidays_new, ss_new = Fyers.load_holiday_set(today.year)
                holidays = holidays_new
                special_sessions = ss_new
                current_day = today
                write_app_status(mode,           str(current_day), status="re-auth",
                                 message=f"Day changed → re-auth for {current_day}")

            # ── Check trading window ──────────────────────────────────────
            if not _is_in_trading_window(market_type):
                if not _has_open_positions(fyers, ce_symbol, pe_symbol):
                    # Outside window, no positions → clear pair if it was traded
                    if had_positions_ever and pair_manager:
                        pair_manager.clear_pair(symbol_key)
                        log_strategy_event(symbol_key, "PAIR_MGR", "PAIR_CLEARED_WINDOW_END",
                                           details=f"CE={ce_symbol} PE={pe_symbol} — outside window, no positions")
                    write_app_status(mode, str(current_day), status="idle",
                                     message=f"{symbol_key}: outside window & no positions")
                    break
                sleep(INNER_LOOP_INTERVAL)
                continue

            try:
                # ── Step A: Fetch historical data ─────────────────────────
                ce_df = fyers.fetch_historical_data(
                    ce_symbol, timeframe, candles,
                    market_type=market_type,
                    holidays=holidays,
                    special_sessions=special_sessions,
                )
                pe_df = fyers.fetch_historical_data(
                    pe_symbol, timeframe, candles,
                    market_type=market_type,
                    holidays=holidays,
                    special_sessions=special_sessions,
                )
                idx_df = fyers.fetch_historical_data(
                    underlying, timeframe, candles,
                    market_type=market_type,
                    holidays=holidays,
                    special_sessions=special_sessions,
                )

                # ── Step B: SHA + signal details ──────────────────────────
                ce_power, ce_list, ce_cross, ce_sha_dbg = get_symbol_details(ce_df)
                pe_power, pe_list, pe_cross, pe_sha_dbg = get_symbol_details(pe_df)
                idx_power, idx_list, idx_cross, idx_sha_dbg = get_symbol_details(idx_df)

                # ── Step B2: Trend SHA (longer period) ────────────────────
                ce_t_power, ce_t_list, ce_t_cross, ce_t_sha_dbg = get_trend_details(ce_df)
                pe_t_power, pe_t_list, pe_t_cross, pe_t_sha_dbg = get_trend_details(pe_df)
                idx_t_power, idx_t_list, idx_t_cross, idx_t_sha_dbg = get_trend_details(idx_df)

                # ── Step B3: GAP% between Signal SHA and Trend SHA ────────
                ce_gap = compute_sha_gap(ce_sha_dbg, ce_t_sha_dbg)
                pe_gap = compute_sha_gap(pe_sha_dbg, pe_t_sha_dbg)
                idx_gap = compute_sha_gap(idx_sha_dbg, idx_t_sha_dbg)

                # ── Step B4: SHA Relationship (diverge/converge/parallel/close)
                ce_rel = compute_sha_relationship(ce_gap)
                pe_rel = compute_sha_relationship(pe_gap)
                idx_rel = compute_sha_relationship(idx_gap)

                power_list = [
                    (ce_power, ce_list, ce_cross),
                    (pe_power, pe_list, pe_cross),
                    (idx_power, idx_list, idx_cross),
                ]

                # Trend power list (same structure, just from trend SHA)
                trend_power_list = [
                    (ce_t_power, ce_t_list, ce_t_cross),
                    (pe_t_power, pe_t_list, pe_t_cross),
                    (idx_t_power, idx_t_list, idx_t_cross),
                ]

                # GAP data for strategy (most-recent gap% per leg)
                gap_data = {
                    "ce_gap": ce_gap,
                    "pe_gap": pe_gap,
                    "idx_gap": idx_gap,
                }

                # Dump signal state to JSON for dashboard
                write_signal_state(
                    symbol_key=symbol_key,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    underlying=underlying,
                    ce_power=ce_power,
                    ce_list=ce_list,
                    ce_crossover=ce_cross,
                    pe_power=pe_power,
                    pe_list=pe_list,
                    pe_crossover=pe_cross,
                    idx_power=idx_power,
                    idx_list=idx_list,
                    idx_crossover=idx_cross,
                    ce_sha_debug=ce_sha_dbg,
                    pe_sha_debug=pe_sha_dbg,
                    idx_sha_debug=idx_sha_dbg,
                    # Trend SHA data
                    ce_trend_power=ce_t_power,
                    ce_trend_list=ce_t_list,
                    ce_trend_crossover=ce_t_cross,
                    pe_trend_power=pe_t_power,
                    pe_trend_list=pe_t_list,
                    pe_trend_crossover=pe_t_cross,
                    idx_trend_power=idx_t_power,
                    idx_trend_list=idx_t_list,
                    idx_trend_crossover=idx_t_cross,
                    ce_trend_sha_debug=ce_t_sha_dbg,
                    pe_trend_sha_debug=pe_t_sha_dbg,
                    idx_trend_sha_debug=idx_t_sha_dbg,
                    # GAP% data
                    ce_gap=ce_gap,
                    pe_gap=pe_gap,
                    idx_gap=idx_gap,
                    # SHA Relationship data
                    ce_relationship=ce_rel,
                    pe_relationship=pe_rel,
                    idx_relationship=idx_rel,
                    market_type=market_type,
                )

                # ── Step C: Strategy evaluation ───────────────────────────
                pos_df, _ = fyers.position()

                # SHA Relationship data for strategy
                relationship_data = {
                    "ce_rel": ce_rel,
                    "pe_rel": pe_rel,
                    "idx_rel": idx_rel,
                }

                ce_action, pe_action = strategy.evaluate(
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    base_qty=base_qty,
                    power_list=power_list,
                    position_df=pos_df,
                    hedge=pair_hedge,
                    trend_power_list=trend_power_list,
                    gap_data=gap_data,
                    relationship_data=relationship_data,
                )

                # ── Step D: Execute orders ────────────────────────────────
                strategy.execute_orders(fyers, ce_action, pe_action)

                # ── Dump positions + account after execution ──────────────
                _dump_positions_and_account(fyers)

                # ── Snapshot profit history for dashboard (~15s) ────────
                snapshot_counter += 1
                if snapshot_counter % 15 == 0 and tracker:
                    for _act in (ce_action, pe_action):
                        if _act.position_qty != 0:
                            tracker.log_snapshot(
                                _act.symbol, _act.pl,
                                _act.api_total_pl, abs(_act.position_qty),
                                ltp=_act.ltp, avg_price=_act.avg_price)

                # ── Step E: Position lifecycle tracking ────────────────────
                #
                # Track whether a position was ever opened on this pair.
                # Once opened and then fully closed → break out to outer
                # loop so PairManager can pick fresh CE/PE strikes.
                #
                # Pending-close confirmation: strategy.evaluate() already
                # handles confirming closes (calls confirm_close when
                # netQty=0 for a pending symbol). But we also check here
                # for the break condition.
                # ──────────────────────────────────────────────────────────

                has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)

                if has_pos_now:
                    had_positions_ever = True

                if ce_action.is_actionable or pe_action.is_actionable:
                    sleep(2)
                    # Re-check after sleep (order may have settled)
                    has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)
                    if has_pos_now:
                        had_positions_ever = True

                # Confirm any pending closes that have now filled.
                # Read fresh position data so confirm_close gets the
                # actual post-close pl (not the stale pre-fill estimate).
                if not has_pos_now:
                    pending_ce = strategy.is_pending_close(ce_symbol)
                    pending_pe = strategy.is_pending_close(pe_symbol)
                    if pending_ce or pending_pe:
                        pos_df_fresh, _ = fyers.position()
                        for _sym in (ce_symbol, pe_symbol):
                            if strategy.is_pending_close(_sym):
                                _, _, _, fresh_pl, _ = HeikenAshiMartingale._read_position(
                                    pos_df_fresh, _sym, "MARGIN")
                                strategy.confirm_close(_sym, current_api_total_pl=fresh_pl)

                if not has_pos_now and had_positions_ever:
                    # Position was opened and is now fully closed → done
                    if pair_manager:
                        pair_manager.clear_pair(symbol_key)
                        log_strategy_event(symbol_key, "PAIR_MGR", "PAIR_CLEARED_AFTER_CLOSE",
                                           details=f"CE={ce_symbol} PE={pe_symbol} — lock released")
                    log_strategy_event(symbol_key, "INNER", "ALL_CLOSED",
                                       details="Position cycle complete — returning to outer loop for fresh pair")
                    break
                # else: no position ever opened yet — keep waiting for entry signal

            except Exception as e:
                log_strategy_event(symbol_key, "INNER", "ERROR", details=str(e))

            sleep(INNER_LOOP_INTERVAL)

    return current_day


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "demo").lower()

    # Configure state writer to use mode-specific directory
    # (C:/Ballom_FYR/state/demo/ or C:/Ballom_FYR/state/live/)
    configure_state_writer(mode)

    config = load_symbols_config()
    brake = config.get("brake", 0)

    fyers = DemoFyers() if mode == "demo" else Fyers()
    tracker = PositionTracker(mode=mode)

    # One-time cleanup: reset corrupted booked_profit values from the old
    # formula (unrealized-based).  The "_pl_fix_applied" sentinel prevents
    # this from running more than once per day.
    if not tracker._data.get("_pl_fix_applied"):
        tracker.reset_booked_profits()
        tracker._data["_pl_fix_applied"] = True
        tracker._save_tracker()
        log_strategy_event("SYSTEM", "INIT", "PL_FIX_RESET",
                           details="Booked profits reset — pl-formula fix deployed")

    pair_manager = PairManager(mode=mode)
    strategy = HeikenAshiMartingale(
        mode=mode,
        brake=bool(brake),
        max_balance_usage=config.get("max_balance_usage", 0),
        tracker=tracker,
    )

    indices     = config.get("indices", [])
    commodities = config.get("commodities", [])

    # ── dump initial account state so dashboard shows balance immediately ──
    try:
        _dump_positions_and_account(fyers)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "ACCOUNT_DUMP_FAIL", details=str(e))

    # ── first-time daily setup ─────────────────────────────────────────────
    current_day = date.today()
    option_df  = None
    mcx_df     = None
    holidays   = set()
    special_sessions = []
    setup_ok   = False                    # tracks whether daily_setup succeeded

    try:
        option_df, mcx_df, holidays, special_sessions = daily_setup(fyers, force_auth=False)
        setup_ok = True
        log_strategy_event("SYSTEM", "INIT", "DAILY_SETUP_OK",
                           details=f"Auth + CSV downloads done for {current_day}")
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "DAILY_SETUP_FAIL", details=str(e))

    indices_scanned_today     = False
    commodities_scanned_today = False

    # ── fetch holidays for current year (and next year in December) ─────────
    current_year = current_day.year
    try:
        Fyers.fetch_trading_holidays(current_year)
        if current_day.month == 12:
            Fyers.fetch_trading_holidays(current_year + 1)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_FETCH_FAIL", details=str(e))

    write_app_status(mode, str(current_day), status="started",
                     message=f"Daily setup {'OK' if setup_ok else 'FAILED'} | brake={'ON' if brake else 'OFF'}")

    # ── outer loop (forever) ───────────────────────────────────────────────
    while True:
        today = date.today()
        now   = datetime.now().time()

        # ── Step 1: Day-change → re-auth, re-download, reset flags ─────────
        if today != current_day:
            current_day = today
            tracker.reset_for_new_day()
            setup_ok = False
            try:
                option_df, mcx_df, holidays, special_sessions = daily_setup(fyers, force_auth=True)
                setup_ok = True
                log_strategy_event("SYSTEM", "OUTER", "DAILY_SETUP_OK",
                                   details=f"New day setup done for {current_day}")
            except Exception as e:
                log_strategy_event("SYSTEM", "OUTER", "DAILY_SETUP_FAIL", details=str(e))
            indices_scanned_today     = False
            commodities_scanned_today = False

            try:
                if today.year != current_year:
                    current_year = today.year
                    Fyers.fetch_trading_holidays(current_year)
                if today.month == 12:
                    Fyers.fetch_trading_holidays(current_year + 1)
            except Exception as e:
                log_strategy_event("SYSTEM", "OUTER", "HOLIDAY_FETCH_FAIL", details=str(e))

            write_app_status(mode, str(current_day), status="new_day",
                             message=f"Daily setup {'OK' if setup_ok else 'FAILED'} for {current_day}")

        # ── Step 1b: Retry daily_setup if it hasn't succeeded yet ──────────
        if not setup_ok:
            try:
                option_df, mcx_df, holidays, special_sessions = daily_setup(fyers, force_auth=True)
                setup_ok = True
                log_strategy_event("SYSTEM", "OUTER", "SETUP_RETRY_OK",
                                   details="daily_setup retry succeeded")
            except Exception as e:
                # Log only once every 30 seconds to avoid flooding
                write_app_status(mode, str(current_day), status="setup_failed",
                                 message=f"daily_setup failing: {str(e)[:80]}")
                sleep(30)
                continue

        # ── Step 2: Determine time window ──────────────────────────────────
        in_indices_window   = INDICES_START <= now <= INDICES_END
        in_commodity_window = COMMODITY_START <= now <= COMMODITY_END

        # ── Step 3: Position conflict check ────────────────────────────────
        try:
            pos_df, _ = fyers.position()
        except Exception:
            pos_df = __import__("pandas").DataFrame()
        has_idx_pos  = Fyers.has_index_positions(pos_df)
        has_comm_pos = Fyers.has_commodity_positions(pos_df)

        # Update app status every cycle
        write_app_status(
            mode, str(current_day),
            in_indices_window=in_indices_window,
            in_commodity_window=in_commodity_window,
            indices_scanned=indices_scanned_today,
            commodities_scanned=commodities_scanned_today,
            status="running",
        )

        # ── Always dump account state so dashboard has fresh balance ───────
        try:
            _dump_positions_and_account(fyers)
        except Exception:
            pass   # non-critical — don't crash the loop

        # ── Step 3a: INDICES window (FIRST PRIORITY — ALWAYS) ─────────────
        if in_indices_window:
            # During indices hours, ONLY process indices — never commodities
            if has_comm_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Commodity positions open — skipping indices")
            else:
                # PairManager controls whether we scan or reuse locked pairs.
                # indices_scanned_today gates whether inner_loop can run;
                # with PairManager, we always "scan" (which may just return
                # the locked pair) so inner_loop always has fresh data.
                if not indices_scanned_today:
                    # Guard: option_df must be valid
                    if option_df is None:
                        log_strategy_event("SYSTEM", "SCAN", "INDEX_SCAN_SKIP",
                                           details="option_df is None — daily_setup may have failed")
                        sleep(5)
                    else:
                        write_app_status(mode, str(current_day), status="scanning",
                                         message="Scanning INDEX option pairs …")
                        try:
                            n_pairs = scan_and_dump_index_pairs(
                                fyers, indices, option_df,
                                pair_manager=pair_manager,
                            )
                            if n_pairs > 0:
                                indices_scanned_today = True
                            else:
                                log_strategy_event("SYSTEM", "SCAN", "INDEX_SCAN_EMPTY",
                                                   details="Scan OK but 0 valid pairs — will retry in 60s")
                                sleep(60)   # avoid API spam; retry next iteration
                        except Exception as e:
                            log_strategy_event("SYSTEM", "SCAN", "INDEX_SCAN_FAIL",
                                               details=str(e))
                            sleep(10)   # back off before retry

                if indices_scanned_today:
                    log_strategy_event("SYSTEM", "DEBUG", "INDEX_SCAN_COMPLETE",
                                       details="Entering inner loop for INDEX pairs")
                    try:
                        current_day = inner_loop(
                            fyers, strategy,
                            pairs_json=OPTION_PAIRS_JSON,
                            market_type="INDEX",
                            holidays=holidays,
                            special_sessions=special_sessions,
                            inner_day_ref=current_day,
                            mode=mode,
                            tracker=tracker,
                            pair_manager=pair_manager,
                        )
                        # After inner_loop returns, re-scan is allowed on
                        # next iteration (PairManager decides whether to
                        # reuse locked pair or scan fresh).
                        indices_scanned_today = False
                    except Exception as e:
                        log_strategy_event("SYSTEM", "INNER", "INDEX_LOOP_FAIL",
                                           details=str(e))

        # ── Step 3b: COMMODITY window (ONLY after indices close) ───────────
        elif in_commodity_window and not in_indices_window:
            # Commodities can ONLY trade when indices window is completely closed
            if has_idx_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Index positions open — skipping commodities")
            else:
                if not commodities_scanned_today:
                    # Guard: mcx_df must be valid
                    if mcx_df is None:
                        log_strategy_event("SYSTEM", "SCAN", "COMMODITY_SCAN_SKIP",
                                           details="mcx_df is None — daily_setup may have failed")
                        sleep(5)
                    else:
                        write_app_status(mode, str(current_day), status="scanning",
                                         message="Scanning COMMODITY option pairs …")
                        try:
                            n_pairs = scan_and_dump_commodity_pairs(
                                fyers, commodities, mcx_df,
                                pair_manager=pair_manager,
                            )
                            if n_pairs > 0:
                                commodities_scanned_today = True
                            else:
                                log_strategy_event("SYSTEM", "SCAN", "COMMODITY_SCAN_EMPTY",
                                                   details="Scan OK but 0 valid pairs — will retry in 60s")
                                sleep(60)
                        except Exception as e:
                            log_strategy_event("SYSTEM", "SCAN", "COMMODITY_SCAN_FAIL",
                                               details=str(e))
                            sleep(10)

                if commodities_scanned_today:
                    log_strategy_event("SYSTEM", "DEBUG", "COMMODITY_SCAN_COMPLETE",
                                       details="Entering inner loop for COMMODITY pairs")
                    try:
                        current_day = inner_loop(
                            fyers, strategy,
                            pairs_json=COMMODITY_PAIRS_JSON,
                            market_type="COMMODITY",
                            holidays=holidays,
                            special_sessions=special_sessions,
                            inner_day_ref=current_day,
                            mode=mode,
                            tracker=tracker,
                            pair_manager=pair_manager,
                        )
                        # After inner_loop returns, re-scan is allowed
                        commodities_scanned_today = False
                    except Exception as e:
                        log_strategy_event("SYSTEM", "INNER", "COMMODITY_LOOP_FAIL",
                                           details=str(e))
        else:
            # Outside all trading windows
            write_app_status(mode, str(current_day), status="idle",
                             message=f"Outside trading hours ({now.strftime('%H:%M')})")

        sleep(1)


if __name__ == "__main__":
    main()
