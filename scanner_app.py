"""
scanner_app.py — Standalone hourly option-pair scanner service.

Usage:  python scanner_app.py [demo|live]

Architecture
────────────
Forever Loop (runs 24×7):
  Step 1 — Day-change detection  → re-auth token at 12:00 AM + re-download CSVs
  Step 2 — Holiday check         → skip scan on NSE holidays (allow special sessions)
  Step 3 — Hourly scan trigger   → on the hour, scan index + commodity pairs
  Step 4 — Write results         → option_pairs.json + commodity_pairs.json

Output files are fully compatible with the dev branch dashboard and
trading bot — same JSON format, same paths on C:/Ballom_FYR/.

State files (for dashboard visibility):
  C:/Ballom_FYR/state/<mode>/app_status.json   — scanner lifecycle
  C:/Ballom_FYR/state/<mode>/strategy_log/      — scan events (date-partitioned)
"""

import sys
import json
import shutil
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path
from time import sleep

from fyers import Fyers
from demo_fyers import DemoFyers
from constants import (
    SYMBOLS_JSON,
    OPTION_PAIRS_JSON,
    COMMODITY_PAIRS_JSON,
    INDICES_START,
    INDICES_END,
    COMMODITY_START,
    COMMODITY_END,
    STRATEGY_HEDGE_INDEX,
    STRATEGY_HEDGE_COMMODITY,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    log_strategy_event,
)

# ── Scanner config ─────────────────────────────────────────────────────────────
POLL_INTERVAL = 30   # seconds between each loop iteration (checks for hour change)


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def load_symbols_config() -> dict:
    """Load symbols.json (indices + commodities config)."""
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


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


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY SETUP  (auth + CSV downloads + holidays)
# ═══════════════════════════════════════════════════════════════════════════════

def daily_setup(fyers: Fyers, force_auth: bool = False):
    """
    Run once per new trading day (or at startup):
      1. Authenticate and persist token to C:/Ballom_FYR/fyers_token.json
         (generic — reusable by any branch that reads this file)
      2. Download NSE options + MCX commodity symbol CSVs
      3. Fetch / cache trading holidays for the year
    Returns (option_df, mcx_df, holidays, special_sessions).
    """
    fyers.ensure_session(force=force_auth)
    option_df = Fyers.download_option_data()
    mcx_df = Fyers.download_mcx_data()
    holidays, special_sessions = Fyers.load_holiday_set()
    return option_df, mcx_df, holidays, special_sessions


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING-DAY AWARENESS
# ═══════════════════════════════════════════════════════════════════════════════

def is_trading_day(
    today: date,
    holidays: set[str],
    special_sessions: list[dict],
) -> bool:
    """
    True if *today* is a valid trading day.
    A special session (Budget Saturday, Diwali Muhurat) overrides holidays
    and weekend rules.
    """
    today_str = today.isoformat()
    if any(ss.get("date") == today_str for ss in special_sessions):
        return True
    if today.weekday() >= 5:      # Saturday / Sunday
        return False
    if today_str in holidays:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  PAIR SCANNING
# ═══════════════════════════════════════════════════════════════════════════════

def scan_index_pairs(fyers: Fyers, indices: list, option_df) -> dict:
    """
    Scan all index symbols from symbols.json and return best CE/PE pairs.

    Returns dict keyed by symbol_key (e.g. "NIFTY") with structure:
        { "CE": ..., "PE": ..., "CE_Strike": ..., "PE_Strike": ...,
          "Expiry": ..., "Trend_Score": ..., "VIX": ...,
          "indices": ..., "qty": ..., "hedge": ... }
    """
    result: dict = {}

    for entry in indices:
        symbol_key = entry["symbol"]
        underlying = entry["indices"]
        qty_times  = entry.get("qty_times", 1)
        hedge      = entry.get("hedge", STRATEGY_HEDGE_INDEX)

        try:
            pair = fyers.fetch_option_pair(underlying, asset_type="INDEX")
        except Exception as e:
            log_strategy_event(symbol_key, "SCAN", "INDEX_ERROR", details=str(e))
            continue

        debug_trail = pair.get("Debug", "")
        if not pair.get("Recommended"):
            log_strategy_event(
                symbol_key, "SCAN", "SKIP_INDEX",
                details=f"{pair.get('Message', 'skipped')} || {debug_trail}",
            )
            continue

        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], option_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue

        qty    = int(lot * qty_times)
        ce_sym = pair.get("CE_Symbol", "")
        pe_sym = pair.get("PE_Symbol", "")

        if not ce_sym or not pe_sym:
            log_strategy_event(
                symbol_key, "SCAN", "INVALID_PAIR",
                details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}",
            )
            continue

        result[symbol_key] = {
            "CE": ce_sym,
            "PE": pe_sym,
            "CE_Strike": pair["CE_Strike"],
            "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"],
            "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"],
            "indices": underlying,
            "qty": qty,
            "hedge": hedge,
        }
        log_strategy_event(
            symbol_key, "SCAN", "INDEX_PAIR_FOUND", qty=qty,
            details=f"CE={ce_sym} PE={pe_sym} Exp={pair['Expiry']} || {debug_trail}",
        )

    return result


def scan_commodity_pairs(fyers: Fyers, commodities: list, mcx_df) -> dict:
    """
    Scan all enabled commodity symbols from symbols.json and return best CE/PE pairs.

    Returns dict keyed by symbol_key (e.g. "SILVERM") with structure:
        { "CE": ..., "PE": ..., "CE_Strike": ..., "PE_Strike": ...,
          "Expiry": ..., "Trend_Score": ..., "VIX": ...,
          "commodity": ..., "qty": ..., "hedge": ... }
    """
    result: dict = {}

    for entry in commodities:
        if not entry.get("enabled", True):
            continue

        symbol_key = entry["symbol"]
        generic    = entry["commodity"]
        qty_times  = entry.get("qty_times", 1)
        hedge      = entry.get("hedge", STRATEGY_HEDGE_COMMODITY)

        # Resolve the generic commodity name (e.g. "SILVER") to the actual
        # Fyers tradable symbol (e.g. "MCX:SILVERM25JUNFUT")
        actual = Fyers.resolve_commodity_symbol(generic, mcx_df)
        if not actual:
            log_strategy_event(
                symbol_key, "SCAN", "RESOLVE_FAIL",
                details=f"Could not resolve {generic}",
            )
            continue

        try:
            pair = fyers.fetch_option_pair(
                actual,
                asset_type="COMMODITY",
                max_premium_per_lot=55000,
                min_trend_score=0.35,
            )
        except Exception as e:
            log_strategy_event(symbol_key, "SCAN", "COMMODITY_ERROR", details=str(e))
            continue

        debug_trail = pair.get("Debug", "")
        if not pair.get("Recommended"):
            log_strategy_event(
                symbol_key, "SCAN", "SKIP_COMMODITY",
                details=f"{pair.get('Message', 'skipped')} || {debug_trail}",
            )
            continue

        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], mcx_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue

        qty    = int(lot * qty_times)
        ce_sym = pair.get("CE_Symbol", "")
        pe_sym = pair.get("PE_Symbol", "")

        if not ce_sym or not pe_sym:
            log_strategy_event(
                symbol_key, "SCAN", "INVALID_PAIR",
                details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}",
            )
            continue

        result[symbol_key] = {
            "CE": ce_sym,
            "PE": pe_sym,
            "CE_Strike": pair["CE_Strike"],
            "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"],
            "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"],
            "commodity": actual,
            "qty": qty,
            "hedge": hedge,
        }
        log_strategy_event(
            symbol_key, "SCAN", "COMMODITY_PAIR_FOUND", qty=qty,
            details=f"CE={ce_sym} PE={pe_sym} UND={actual} || {debug_trail}",
        )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "demo").lower()

    # State writer → C:/Ballom_FYR/state/demo/ or .../live/
    configure_state_writer(mode)

    config      = load_symbols_config()
    indices     = config.get("indices", [])
    commodities = config.get("commodities", [])

    # ── Fyers client (DemoFyers inherits all market-data methods) ──────────
    fyers: Fyers = DemoFyers() if mode == "demo" else Fyers()

    # ── First-time daily setup ─────────────────────────────────────────────
    current_day = date.today()
    option_df   = None
    mcx_df      = None
    holidays: set[str]      = set()
    special_sessions: list  = []
    setup_ok    = False

    try:
        option_df, mcx_df, holidays, special_sessions = daily_setup(
            fyers, force_auth=False,
        )
        setup_ok = True
        log_strategy_event(
            "SYSTEM", "INIT", "DAILY_SETUP_OK",
            details=f"Scanner startup — auth + CSV done for {current_day}",
        )
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "DAILY_SETUP_FAIL", details=str(e))

    # Pre-fetch holidays for this year (and next if December)
    try:
        Fyers.fetch_trading_holidays(current_day.year)
        if current_day.month == 12:
            Fyers.fetch_trading_holidays(current_day.year + 1)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_FETCH_FAIL", details=str(e))

    write_app_status(
        mode, str(current_day), status="started",
        message=f"Scanner started — setup {'OK' if setup_ok else 'FAILED'}",
    )

    last_scan_hour = -1   # -1 forces a scan on the very first eligible hour

    # ══════════════════════════════════════════════════════════════════════════
    #  FOREVER LOOP
    # ══════════════════════════════════════════════════════════════════════════
    while True:
        today = date.today()
        now   = datetime.now()
        current_time = now.time()

        # ── Step 1: Day-change → re-auth at midnight, re-download CSVs ────
        if today != current_day:
            current_day = today
            setup_ok = False
            last_scan_hour = -1     # reset so first hour of new day triggers scan

            try:
                option_df, mcx_df, holidays, special_sessions = daily_setup(
                    fyers, force_auth=True,
                )
                setup_ok = True
                log_strategy_event(
                    "SYSTEM", "SCANNER", "NEW_DAY_SETUP_OK",
                    details=f"Day change — re-auth + CSV done for {current_day}",
                )
            except Exception as e:
                log_strategy_event(
                    "SYSTEM", "SCANNER", "NEW_DAY_SETUP_FAIL", details=str(e),
                )

            # Refresh holiday calendar if new year
            try:
                Fyers.fetch_trading_holidays(today.year)
                if today.month == 12:
                    Fyers.fetch_trading_holidays(today.year + 1)
            except Exception:
                pass

            write_app_status(
                mode, str(current_day), status="new_day",
                message=f"Scanner new day — setup {'OK' if setup_ok else 'FAILED'}",
            )

        # ── Step 1b: Retry daily_setup if it hasn't succeeded yet ──────────
        if not setup_ok:
            try:
                option_df, mcx_df, holidays, special_sessions = daily_setup(
                    fyers, force_auth=True,
                )
                setup_ok = True
                log_strategy_event(
                    "SYSTEM", "SCANNER", "SETUP_RETRY_OK",
                    details="daily_setup retry succeeded",
                )
            except Exception as e:
                write_app_status(
                    mode, str(current_day), status="setup_failed",
                    message=f"daily_setup failing: {str(e)[:80]}",
                )
                sleep(30)
                continue

        # ── Step 2: Holiday check → skip scanning on holidays ──────────────
        if not is_trading_day(today, holidays, special_sessions):
            write_app_status(
                mode, str(current_day), status="holiday",
                message="Market holiday — scanner idle",
            )
            sleep(60)
            continue

        # ── Step 3: Hourly scan trigger ────────────────────────────────────
        current_hour = now.hour

        if current_hour != last_scan_hour:
            in_idx_window = INDICES_START <= current_time <= INDICES_END
            in_com_window = COMMODITY_START <= current_time <= COMMODITY_END

            if in_idx_window or in_com_window:
                write_app_status(
                    mode, str(current_day), status="scanning",
                    in_indices_window=in_idx_window,
                    in_commodity_window=in_com_window,
                    message=f"Hourly scan at {now.strftime('%H:%M')}",
                )

                idx_count = 0
                com_count = 0

                # ── Scan indices (only during index window) ────────────────
                if in_idx_window and option_df is not None:
                    try:
                        idx_result = scan_index_pairs(fyers, indices, option_df)
                        _write_json_atomic(OPTION_PAIRS_JSON, idx_result)
                        idx_count = len(idx_result)
                        log_strategy_event(
                            "SYSTEM", "SCAN", "INDEX_SCAN_DONE",
                            details=f"{idx_count} valid index pair(s) written",
                        )
                    except Exception as e:
                        log_strategy_event(
                            "SYSTEM", "SCAN", "INDEX_SCAN_FAIL", details=str(e),
                        )

                # ── Scan commodities (only during commodity window) ────────
                if in_com_window and mcx_df is not None:
                    try:
                        com_result = scan_commodity_pairs(
                            fyers, commodities, mcx_df,
                        )
                        _write_json_atomic(COMMODITY_PAIRS_JSON, com_result)
                        com_count = len(com_result)
                        log_strategy_event(
                            "SYSTEM", "SCAN", "COMMODITY_SCAN_DONE",
                            details=f"{com_count} valid commodity pair(s) written",
                        )
                    except Exception as e:
                        log_strategy_event(
                            "SYSTEM", "SCAN", "COMMODITY_SCAN_FAIL",
                            details=str(e),
                        )

                last_scan_hour = current_hour
                next_hour = (current_hour + 1) % 24
                write_app_status(
                    mode, str(current_day), status="idle",
                    indices_scanned=idx_count > 0,
                    commodities_scanned=com_count > 0,
                    in_indices_window=in_idx_window,
                    in_commodity_window=in_com_window,
                    message=(
                        f"Scan done — IDX={idx_count} COM={com_count} | "
                        f"next at {next_hour:02d}:00"
                    ),
                )
            else:
                # Outside all market windows — nothing to scan
                last_scan_hour = current_hour
                write_app_status(
                    mode, str(current_day), status="idle",
                    message=f"Outside market hours ({now.strftime('%H:%M')})",
                )

        sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
