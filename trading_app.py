"""
trading_app.py — Trading engine for the dev_trading branch.

Usage:  python trading_app.py [demo|live]

Architecture
────────────
This branch focuses ONLY on trading logic:
  - Reads Fyers token from C:/Ballom_FYR/fyers_token.json (written by dev_scanner)
  - Reads option pairs from C:/Ballom_FYR/option_pairs.json (written by dev_scanner)
  - Reads commodity pairs from C:/Ballom_FYR/commodity_pairs.json (written by dev_scanner)
  - Computes SHA indicators, evaluates strategy, places trades
  - Dumps all state to C:/Ballom_FYR/state/<mode>/ for dashboard (dev branch)

Dependency chain:  dev_scanner → dev_update → dev_trading → dev (dashboard)

Outer Loop (runs forever):
  Step 1 — Day-change detection  → reload token + fetch holidays
  Step 2 — Time-window routing   → indices 9:15-15:30, commodities 15:30-23:55
  Step 3 — Position conflict     → mutual exclusion between index/commodity
  Step 3b— Signal update         → compute SHA for ALL pairs every cycle (dashboard freshness)
  Step 4 — Inner loop            → fetch history → SHA → strategy → trade → wait

Inner Loop (per symbol pair):
  Step A — Fetch history for CE, PE, underlying
  Step B — Compute SHA + power/list/crossover + trend + GAP + relationship + RSI
  Step C — Strategy.evaluate() → get OrderActions
  Step D — Strategy.execute_orders() → place trades
  Step E — Monitor positions; if all closed → break for fresh pair
  Step F — Day-change inside inner loop → reload token

All runtime state is written to JSON files under C:/Ballom_FYR/state/<mode>/
for the dashboard (dev branch) to consume.  No print/log statements.
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
from indicator import SmoothedHeikenAshi, RSI
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
    RSI_PERIOD,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    write_signal_state,
    write_position_state,
    write_account_state,
    log_strategy_event,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  TOKEN PATH  (written by dev_scanner, read here — NO re-auth)
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN_FILE = Path("C:/Ballom_FYR/fyers_token.json")


def load_fyers_session(fyers_obj: Fyers) -> bool:
    """
    Load the Fyers token from C: drive (written by dev_scanner).
    No TOTP re-auth — if token is missing or invalid, return False.
    Retries every 30s up to 10 times on first startup.
    """
    for attempt in range(10):
        try:
            if not TOKEN_FILE.exists():
                log_strategy_event("SYSTEM", "AUTH", "TOKEN_MISSING",
                                   details=f"Attempt {attempt+1}/10 — {TOKEN_FILE} not found, waiting 30s")
                sleep(30)
                continue

            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)

            token = data.get("access_token", "")
            if not token:
                log_strategy_event("SYSTEM", "AUTH", "TOKEN_EMPTY",
                                   details=f"Attempt {attempt+1}/10 — token field empty")
                sleep(30)
                continue

            # Build FyersModel with the loaded token
            fyers_obj._model = fyers_obj._build_model(token)
            fyers_obj._access_token = token
            fyers_obj._last_auth_date = date.today()

            # Verify the token is valid
            if fyers_obj._verify_token(token):
                log_strategy_event("SYSTEM", "AUTH", "TOKEN_LOADED",
                                   details=f"Token loaded from {TOKEN_FILE} "
                                           f"(written: {data.get('date', 'unknown')})")
                return True
            else:
                log_strategy_event("SYSTEM", "AUTH", "TOKEN_INVALID",
                                   details=f"Attempt {attempt+1}/10 — token verification failed")
                sleep(30)
                continue

        except Exception as e:
            log_strategy_event("SYSTEM", "AUTH", "TOKEN_LOAD_ERROR",
                               details=f"Attempt {attempt+1}/10 — {str(e)}")
            sleep(30)
            continue

    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  HOLIDAY AWARENESS
# ═══════════════════════════════════════════════════════════════════════════════

def is_trading_day(holidays: set, special_sessions: list) -> bool:
    """Return True if today is a valid trading day."""
    today = date.today()
    day_str = today.strftime("%Y-%m-%d")

    # Check special sessions first (e.g. Diwali Muhurat — market opens on a holiday)
    for ss in special_sessions:
        if ss.get("date") == day_str:
            return True

    # Weekend check
    if today.weekday() >= 5:
        return False

    # Holiday check
    if day_str in holidays:
        return False

    return True


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
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def load_symbols_config() -> dict:
    """Load symbols.json (indices + commodities config)."""
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE-DUMP HELPERS  (writes data for dashboard to consume)
# ═══════════════════════════════════════════════════════════════════════════════

def _dump_positions_and_account(fyers: Fyers) -> None:
    """Snapshot current positions + account state to JSON."""
    pos_df, overall = fyers.position()

    # Round all float columns in position DataFrame to 2 decimal places
    if not pos_df.empty:
        float_cols = pos_df.select_dtypes(include=["float", "float64"]).columns
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
#  SIGNAL HELPERS  (SHA computation — ported from app.py)
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
        sha_debug        — last 7 SHA OHLC dicts (most-recent-first)

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
    """Compute Trend SHA (longer-period) using same logic as get_symbol_details."""
    return get_symbol_details(raw_df, sha_length=sha_length, sha_type=sha_type)


def compute_sha_gap(signal_sha_debug: list, trend_sha_debug: list) -> list:
    """
    Compute GAP% between Signal SHA and Trend SHA for each candle.

    GAP% = ((signal_mid - trend_mid) / trend_mid) x 100
    where mid = (High + Low) / 2

    Returns list of dicts: [{gap_pct, signal_mid, trend_mid}, ...] most-recent first.
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
    Analyze the relationship between Signal SHA and Trend SHA.

    Returns dict with: status, strength, avg_gap, delta.
    """
    if not gap_list or len(gap_list) < 2:
        return {"status": "UNKNOWN", "strength": 0.0, "avg_gap": 0.0, "delta": 0.0}

    abs_gaps = [abs(g["gap_pct"]) for g in gap_list]
    avg_gap = sum(abs_gaps) / len(abs_gaps)

    CLOSE_THRESHOLD = 1.0
    if avg_gap < CLOSE_THRESHOLD:
        strength = round(1.0 - avg_gap / CLOSE_THRESHOLD, 4)
        return {"status": "CLOSE", "strength": strength,
                "avg_gap": round(avg_gap, 4), "delta": 0.0}

    mid = len(abs_gaps) // 2
    recent = abs_gaps[:max(mid, 1)]
    older  = abs_gaps[max(mid, 1):]

    avg_recent = sum(recent) / len(recent)
    avg_older  = sum(older) / len(older) if older else avg_recent

    delta = avg_recent - avg_older

    PARALLEL_THRESHOLD = 0.5
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
#  SIGNAL UPDATE  (runs every outer-loop cycle — keeps dashboard fresh)
# ═══════════════════════════════════════════════════════════════════════════════

def update_signals_for_all_pairs(
    fyers: Fyers,
    holidays: set,
    special_sessions: list,
    timeframe: str = DEFAULT_TIMEFRAME,
    candles: int = DEFAULT_CANDLES,
) -> None:
    """
    Compute SHA signals for ALL pairs (INDEX + COMMODITY) and write to
    signal_state.json every outer-loop cycle — regardless of trading window.

    This ensures the dashboard always has fresh signal data even outside
    market hours.
    """
    for pairs_json, market_type in [
        (OPTION_PAIRS_JSON, "INDEX"),
        (COMMODITY_PAIRS_JSON, "COMMODITY"),
    ]:
        pairs = _load_json(pairs_json)
        if not pairs:
            continue

        for symbol_key, info in pairs.items():
            ce_symbol  = info.get("CE", "")
            pe_symbol  = info.get("PE", "")
            underlying = info.get("indices", info.get("commodity", ""))

            if not ce_symbol or not pe_symbol or not underlying:
                continue

            pair_type = "INDEX" if info.get("indices") else "COMMODITY"
            if pair_type != market_type:
                continue

            try:
                # ── Fetch historical data ─────────────────────────────
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

                # ── Signal SHA ────────────────────────────────────────
                ce_power, ce_list, ce_cross, ce_sha_dbg = get_symbol_details(ce_df)
                pe_power, pe_list, pe_cross, pe_sha_dbg = get_symbol_details(pe_df)
                idx_power, idx_list, idx_cross, idx_sha_dbg = get_symbol_details(idx_df)

                # ── Trend SHA (longer period) ─────────────────────────
                ce_t_power, ce_t_list, ce_t_cross, ce_t_sha_dbg = get_trend_details(ce_df)
                pe_t_power, pe_t_list, pe_t_cross, pe_t_sha_dbg = get_trend_details(pe_df)
                idx_t_power, idx_t_list, idx_t_cross, idx_t_sha_dbg = get_trend_details(idx_df)

                # ── GAP% ──────────────────────────────────────────────
                ce_gap = compute_sha_gap(ce_sha_dbg, ce_t_sha_dbg)
                pe_gap = compute_sha_gap(pe_sha_dbg, pe_t_sha_dbg)
                idx_gap = compute_sha_gap(idx_sha_dbg, idx_t_sha_dbg)

                # ── Relationship ──────────────────────────────────────
                ce_rel = compute_sha_relationship(ce_gap)
                pe_rel = compute_sha_relationship(pe_gap)
                idx_rel = compute_sha_relationship(idx_gap)

                # ── Write to signal_state.json ────────────────────────
                write_signal_state(
                    symbol_key=symbol_key,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    underlying=underlying,
                    ce_power=ce_power, ce_list=ce_list, ce_crossover=ce_cross,
                    pe_power=pe_power, pe_list=pe_list, pe_crossover=pe_cross,
                    idx_power=idx_power, idx_list=idx_list, idx_crossover=idx_cross,
                    ce_sha_debug=ce_sha_dbg, pe_sha_debug=pe_sha_dbg, idx_sha_debug=idx_sha_dbg,
                    ce_trend_power=ce_t_power, ce_trend_list=ce_t_list, ce_trend_crossover=ce_t_cross,
                    pe_trend_power=pe_t_power, pe_trend_list=pe_t_list, pe_trend_crossover=pe_t_cross,
                    idx_trend_power=idx_t_power, idx_trend_list=idx_t_list, idx_trend_crossover=idx_t_cross,
                    ce_trend_sha_debug=ce_t_sha_dbg, pe_trend_sha_debug=pe_t_sha_dbg, idx_trend_sha_debug=idx_t_sha_dbg,
                    ce_gap=ce_gap, pe_gap=pe_gap, idx_gap=idx_gap,
                    ce_relationship=ce_rel, pe_relationship=pe_rel, idx_relationship=idx_rel,
                    market_type=market_type,
                )

            except Exception as e:
                log_strategy_event(symbol_key, "SIGNAL", "UPDATE_FAIL",
                                   details=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION HELPERS
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


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP — the blocking trading loop
# ═══════════════════════════════════════════════════════════════════════════════

def inner_loop(
    fyers: Fyers,
    strategy: HeikenAshiMartingale,
    pairs_json: Path,
    market_type: str,
    holidays: set,
    special_sessions: list,
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
      6. If day changes → reload token inside the loop

    Returns the (possibly updated) current_day so the outer loop stays in sync.
    """
    pairs = _load_json(pairs_json)
    if not pairs:
        write_app_status(mode, str(inner_day_ref), status="idle",
                         message=f"No pairs in {pairs_json.name} — waiting for dev_scanner")
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
            continue

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
        default_hedge = STRATEGY_HEDGE_INDEX if market_type == "INDEX" else STRATEGY_HEDGE_COMMODITY
        pair_hedge = info.get("hedge", default_hedge)

        write_app_status(mode, str(current_day), status="trading",
                         message=f"Trading {symbol_key} | CE={ce_symbol} PE={pe_symbol}")

        snapshot_counter = 0
        had_positions_ever = _has_open_positions(fyers, ce_symbol, pe_symbol)

        # ── trading loop for this symbol pair ──────────────────────────────
        while True:
            # ── Step F: Day-change detection — reload token ────────────────
            today = date.today()
            if today != current_day:
                # Reload token from C: drive (dev_scanner writes fresh daily)
                token_ok = load_fyers_session(fyers)
                if not token_ok:
                    log_strategy_event("SYSTEM", "INNER", "TOKEN_RELOAD_FAIL",
                                       details="Day changed but could not reload token")
                holidays_new, ss_new = Fyers.load_holiday_set(today.year)
                holidays = holidays_new
                special_sessions = ss_new
                current_day = today
                write_app_status(mode, str(current_day), status="re-auth",
                                 message=f"Day changed → token reloaded for {current_day}")

            # ── Check trading window ──────────────────────────────────────
            if not _is_in_trading_window(market_type):
                if not _has_open_positions(fyers, ce_symbol, pe_symbol):
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

                # ── Step B4: SHA Relationship
                ce_rel = compute_sha_relationship(ce_gap)
                pe_rel = compute_sha_relationship(pe_gap)
                idx_rel = compute_sha_relationship(idx_gap)

                # ── Step B5: RSI on option prices (martingale trigger) ────
                ce_rsi_series = RSI.calculate(ce_df, length=RSI_PERIOD)
                pe_rsi_series = RSI.calculate(pe_df, length=RSI_PERIOD)
                ce_rsi_val = float(ce_rsi_series.iloc[-1]) if len(ce_rsi_series) > 0 else float('nan')
                pe_rsi_val = float(pe_rsi_series.iloc[-1]) if len(pe_rsi_series) > 0 else float('nan')

                power_list = [
                    (ce_power, ce_list, ce_cross),
                    (pe_power, pe_list, pe_cross),
                    (idx_power, idx_list, idx_cross),
                ]

                trend_power_list = [
                    (ce_t_power, ce_t_list, ce_t_cross),
                    (pe_t_power, pe_t_list, pe_t_cross),
                    (idx_t_power, idx_t_list, idx_t_cross),
                ]

                gap_data = {
                    "ce_gap": ce_gap,
                    "pe_gap": pe_gap,
                    "idx_gap": idx_gap,
                }

                # ── Dump signal state to JSON for dashboard ──────────────
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
                    ce_gap=ce_gap,
                    pe_gap=pe_gap,
                    idx_gap=idx_gap,
                    ce_relationship=ce_rel,
                    pe_relationship=pe_rel,
                    idx_relationship=idx_rel,
                    market_type=market_type,
                )

                # ── Step C: Strategy evaluation ───────────────────────────
                pos_df, _ = fyers.position()

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
                    rsi_data={"ce_rsi": ce_rsi_val, "pe_rsi": pe_rsi_val},
                )

                # ── Step D: Execute orders ────────────────────────────────
                strategy.execute_orders(fyers, ce_action, pe_action)

                # ── Dump positions + account after execution ──────────────
                _dump_positions_and_account(fyers)

                # ── Snapshot profit history for dashboard (~15s) ──────────
                snapshot_counter += 1
                if snapshot_counter % 15 == 0 and tracker:
                    for _act in (ce_action, pe_action):
                        if _act.position_qty != 0:
                            tracker.log_snapshot(
                                _act.symbol, _act.pl,
                                _act.api_total_pl, abs(_act.position_qty),
                                ltp=_act.ltp, avg_price=_act.avg_price)

                # ── Step E: Position lifecycle tracking ────────────────────
                has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)

                if has_pos_now:
                    had_positions_ever = True

                if ce_action.is_actionable or pe_action.is_actionable:
                    sleep(2)
                    has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)
                    if has_pos_now:
                        had_positions_ever = True

                # Confirm pending closes that have now filled
                if not has_pos_now:
                    pending_ce = strategy.is_pending_close(ce_symbol)
                    pending_pe = strategy.is_pending_close(pe_symbol)
                    if pending_ce or pending_pe:
                        pos_df_fresh, _ = fyers.position()
                        for _sym in (ce_symbol, pe_symbol):
                            if strategy.is_pending_close(_sym):
                                _, _, _, fresh_pl, _, _ = HeikenAshiMartingale._read_position(
                                    pos_df_fresh, _sym, "MARGIN")
                                strategy.confirm_close(_sym, current_api_total_pl=fresh_pl)

                if not has_pos_now and had_positions_ever:
                    if pair_manager:
                        pair_manager.clear_pair(symbol_key)
                        log_strategy_event(symbol_key, "PAIR_MGR", "PAIR_CLEARED_AFTER_CLOSE",
                                           details=f"CE={ce_symbol} PE={pe_symbol} — lock released")
                    log_strategy_event(symbol_key, "INNER", "ALL_CLOSED",
                                       details="Position cycle complete — returning to outer loop for fresh pair")
                    break

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
    configure_state_writer(mode)

    config = load_symbols_config()
    brake = config.get("brake", 0)

    # ── Instantiate Fyers (Demo or Live) ──────────────────────────────────
    fyers = DemoFyers() if mode == "demo" else Fyers()

    # ── Load token from C: drive (written by dev_scanner) ─────────────────
    token_ok = load_fyers_session(fyers)
    if not token_ok:
        log_strategy_event("SYSTEM", "INIT", "TOKEN_LOAD_FATAL",
                           details="Could not load Fyers token after 10 attempts — exiting")
        write_app_status(mode, str(date.today()), status="fatal",
                         message="Token load failed — ensure dev_scanner has written fyers_token.json")
        sys.exit(1)

    # ── Position Tracker (per-symbol booked profit, profit history) ───────
    tracker = PositionTracker(mode=mode)

    # One-time cleanup: reset corrupted booked_profit values
    if not tracker._data.get("_pl_fix_applied"):
        tracker.reset_booked_profits()
        tracker._data["_pl_fix_applied"] = True
        tracker._save_tracker()
        log_strategy_event("SYSTEM", "INIT", "PL_FIX_RESET",
                           details="Booked profits reset — pl-formula fix deployed")

    # ── Pair Manager (lock CE/PE pairs per symbol) ────────────────────────
    pair_manager = PairManager(mode=mode)

    # ── Strategy ──────────────────────────────────────────────────────────
    strategy = HeikenAshiMartingale(
        mode=mode,
        brake=bool(brake),
        max_balance_usage=config.get("max_balance_usage", 0),
        tracker=tracker,
    )

    # ── Dump initial account state so dashboard shows balance immediately ─
    try:
        _dump_positions_and_account(fyers)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "ACCOUNT_DUMP_FAIL", details=str(e))

    # ── Holidays ──────────────────────────────────────────────────────────
    current_day = date.today()
    holidays: set = set()
    special_sessions: list = []

    try:
        holidays, special_sessions = Fyers.load_holiday_set()
        log_strategy_event("SYSTEM", "INIT", "HOLIDAYS_LOADED",
                           details=f"Loaded {len(holidays)} holidays for {current_day.year}")
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_LOAD_FAIL", details=str(e))

    try:
        Fyers.fetch_trading_holidays(current_day.year)
        if current_day.month == 12:
            Fyers.fetch_trading_holidays(current_day.year + 1)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_FETCH_FAIL", details=str(e))

    write_app_status(mode, str(current_day), status="started",
                     message=f"Trading engine started | mode={mode} | brake={'ON' if brake else 'OFF'}")

    # ══════════════════════════════════════════════════════════════════════
    #  OUTER LOOP  (forever)
    # ══════════════════════════════════════════════════════════════════════
    while True:
        today = date.today()
        now   = datetime.now().time()

        # ── Step 1: Day-change → reload token, reset tracker ─────────────
        if today != current_day:
            current_day = today
            tracker.reset_for_new_day()

            # Reload token (dev_scanner writes fresh token daily)
            token_ok = load_fyers_session(fyers)
            if not token_ok:
                log_strategy_event("SYSTEM", "OUTER", "TOKEN_RELOAD_FAIL",
                                   details="Day changed but could not reload token — will retry")
                write_app_status(mode, str(current_day), status="token_failed",
                                 message="Token reload failed — ensure dev_scanner is running")
                sleep(60)
                continue

            # Refresh holidays
            try:
                holidays, special_sessions = Fyers.load_holiday_set(today.year)
                if today.year != current_day.year or today.month == 12:
                    Fyers.fetch_trading_holidays(today.year + 1)
            except Exception as e:
                log_strategy_event("SYSTEM", "OUTER", "HOLIDAY_REFRESH_FAIL", details=str(e))

            write_app_status(mode, str(current_day), status="new_day",
                             message=f"New day — token reloaded for {current_day}")

        # ── Holiday / weekend check ──────────────────────────────────────
        if not is_trading_day(holidays, special_sessions):
            write_app_status(mode, str(current_day), status="holiday",
                             message=f"Not a trading day ({current_day})")
            sleep(300)  # Check every 5 minutes on holidays
            continue

        # ── Step 2: Determine time window ────────────────────────────────
        in_indices_window   = INDICES_START <= now <= INDICES_END
        in_commodity_window = COMMODITY_START <= now <= COMMODITY_END

        # ── Step 3: Position conflict check ──────────────────────────────
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
            status="running",
        )

        # ── Always dump account state so dashboard has fresh data ────────
        try:
            _dump_positions_and_account(fyers)
        except Exception:
            pass

        # ── Always update signals for ALL pairs (keeps dashboard fresh) ──
        try:
            update_signals_for_all_pairs(fyers, holidays, special_sessions)
        except Exception as e:
            log_strategy_event("SYSTEM", "OUTER", "SIGNAL_UPDATE_FAIL",
                               details=str(e))

        # ── Step 4a: INDICES window (9:15 - 15:30) — FIRST PRIORITY ─────
        if in_indices_window:
            if has_comm_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Commodity positions open — skipping indices")
            else:
                log_strategy_event("SYSTEM", "OUTER", "INDEX_TRADING",
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
                except Exception as e:
                    log_strategy_event("SYSTEM", "INNER", "INDEX_LOOP_FAIL",
                                       details=str(e))

        # ── Step 4b: COMMODITY window (15:30 - 23:55) ────────────────────
        elif in_commodity_window and not in_indices_window:
            if has_idx_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Index positions open — skipping commodities")
            else:
                log_strategy_event("SYSTEM", "OUTER", "COMMODITY_TRADING",
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
