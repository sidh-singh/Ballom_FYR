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
    DEFAULT_TIMEFRAME,
    DEFAULT_CANDLES,
    INNER_LOOP_INTERVAL,
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
    rows = pos_df.to_dict(orient="records") if not pos_df.empty else []
    write_position_state(rows, asdict(overall))

    funds = fyers.funds()
    fund_map = {}
    for item in funds.get("fund_limit", []):
        fund_map[item.get("title", "")] = item.get("equityAmount", 0)

    write_account_state(
        balance=fund_map.get("Total Balance", 0),
        utilized=fund_map.get("Utilized Amount", 0),
        realized_pnl=fund_map.get("Realized P&L", overall.pl_realized),
        unrealized_pnl=overall.pl_unrealized,
        total_trades=getattr(fyers, "account", None) and fyers.account.total_trades or 0,
        winning_trades=getattr(fyers, "account", None) and fyers.account.winning_trades or 0,
        losing_trades=getattr(fyers, "account", None) and fyers.account.losing_trades or 0,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  OPTION-PAIR SCANNING  → JSON
# ═══════════════════════════════════════════════════════════════════════════════

def scan_and_dump_index_pairs(fyers: Fyers, indices: list, option_df):
    """
    For each index in config, call the option-chain scanner,
    look up lot size from the downloaded NSE F&O CSV, and dump to JSON.
    """
    result = _load_json(OPTION_PAIRS_JSON)

    for entry in indices:
        symbol_key = entry["symbol"]
        underlying = entry["indices"]
        qty_times  = entry.get("qty_times", 1)

        pair = fyers.fetch_option_pair(underlying, asset_type="INDEX")
        if not pair.get("Recommended"):
            log_strategy_event(symbol_key, "SCAN", "SKIP_INDEX",
                               details=pair.get("Message", "skipped"))
            continue

        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], option_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue

        qty = int(lot * qty_times)

        result[symbol_key] = {
            "CE": pair["CE_Symbol"],
            "PE": pair["PE_Symbol"],
            "CE_Strike": pair["CE_Strike"],
            "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"],
            "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"],
            "indices": underlying,
            "qty": qty,
        }
        log_strategy_event(
            symbol_key, "SCAN", "INDEX_PAIR_FOUND", qty=qty,
            details=f"CE={pair['CE_Symbol']} PE={pair['PE_Symbol']}",
        )

    _write_json_atomic(OPTION_PAIRS_JSON, result)


def scan_and_dump_commodity_pairs(fyers: Fyers, commodities: list, mcx_df):
    """
    For each enabled commodity, resolve to active futures symbol,
    scan option chain, look up lot size from MCX CSV, dump to JSON.
    """
    result = _load_json(COMMODITY_PAIRS_JSON)

    for entry in commodities:
        if not entry.get("enabled", True):
            continue

        symbol_key = entry["symbol"]
        generic    = entry["commodity"]
        qty_times  = entry.get("qty_times", 1)

        actual = Fyers.resolve_commodity_symbol(generic, mcx_df)
        if not actual:
            log_strategy_event(symbol_key, "SCAN", "RESOLVE_FAIL",
                               details=f"Could not resolve {generic}")
            continue

        pair = fyers.fetch_option_pair(
            actual,
            asset_type="COMMODITY",
            max_premium_per_lot=55000,
            min_trend_score=0.35,
        )
        if not pair.get("Recommended"):
            log_strategy_event(symbol_key, "SCAN", "SKIP_COMMODITY",
                               details=pair.get("Message", "skipped"))
            continue

        try:
            lot = Fyers.get_lot_size(pair["CE_Symbol"], mcx_df)
        except ValueError as e:
            log_strategy_event(symbol_key, "SCAN", "LOT_ERROR", details=str(e))
            continue

        qty = int(lot * qty_times)

        result[symbol_key] = {
            "CE": pair["CE_Symbol"],
            "PE": pair["PE_Symbol"],
            "CE_Strike": pair["CE_Strike"],
            "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"],
            "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"],
            "commodity": actual,
            "qty": qty,
        }
        log_strategy_event(
            symbol_key, "SCAN", "COMMODITY_PAIR_FOUND", qty=qty,
            details=f"CE={pair['CE_Symbol']} PE={pair['PE_Symbol']}",
        )

    _write_json_atomic(COMMODITY_PAIRS_JSON, result)


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

    Returns (lt_symbol_power, lt_symbol_list, crossover).
    """
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

    for i in range(-1, -8, -1):
        ha_range = abs(lt_sha["High"].iloc[i] - lt_sha["Low"].iloc[i])
        if ha_range == 0:
            ha_range = 1e-9

        lt_diff = (lt_sha["Close"].iloc[i] - lt_sha["Open"].iloc[i]) / ha_range

        lt_sha_diff = 1 if lt_diff >= threshold else 0
        lt_symbol_list.append(lt_sha_diff)
        lt_symbol_power += lt_sha_diff

        ct_p_high = raw_df["High"].iloc[i]
        ct_p_low = raw_df["Low"].iloc[i]
        lt_sha_high = lt_sha["High"].iloc[i]
        lt_sha_low = lt_sha["Low"].iloc[i]

        if lt_sha_diff == 1:
            if ct_p_low >= lt_sha_high:
                crossover.append(3)
            elif ct_p_high <= lt_sha_low:
                crossover.append(1)
            else:
                crossover.append(2)
        else:
            if ct_p_high <= lt_sha_low:
                crossover.append(-3)
            elif ct_p_low >= lt_sha_high:
                crossover.append(-1)
            else:
                crossover.append(-2)

    return lt_symbol_power, lt_symbol_list, crossover


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
) -> date:
    """
    Blocking inner loop: for every symbol in *pairs_json* —

      1. Fetch historical OHLCV for CE, PE, and underlying
      2. Compute SHA + get_symbol_details for all three
      3. strategy.evaluate() → (ce_action, pe_action)
      4. strategy.execute_orders()
      5. Wait / monitor — if all positions close → break
      6. If day changes → re-auth token inside the loop

    Returns the (possibly updated) current_day so the outer loop
    can stay in sync.
    """
    pairs = _load_json(pairs_json)
    if not pairs:
        write_app_status(mode, str(inner_day_ref), status="idle",
                         message="No pairs in JSON — nothing to trade")
        return inner_day_ref

    current_day = inner_day_ref

    for symbol_key, info in pairs.items():
        ce_symbol  = info.get("CE", "")
        pe_symbol  = info.get("PE", "")
        underlying = info.get("indices", info.get("commodity", ""))
        base_qty   = info.get("qty", 0)

        if not ce_symbol or not pe_symbol or not underlying:
            log_strategy_event(symbol_key, "INNER", "SKIP",
                               details="Incomplete pair — skipping")
            continue

        write_app_status(mode, str(current_day), status="trading",
                         message=f"Trading {symbol_key} | CE={ce_symbol} PE={pe_symbol}")

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
                write_app_status(mode, str(current_day), status="re-auth",
                                 message=f"Day changed → re-auth for {current_day}")

            # ── Check trading window ──────────────────────────────────────
            if not _is_in_trading_window(market_type):
                if not _has_open_positions(fyers, ce_symbol, pe_symbol):
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
                ce_power, ce_list, ce_cross = get_symbol_details(ce_df)
                pe_power, pe_list, pe_cross = get_symbol_details(pe_df)
                idx_power, idx_list, idx_cross = get_symbol_details(idx_df)

                power_list = [
                    (ce_power, ce_list, ce_cross),
                    (pe_power, pe_list, pe_cross),
                    (idx_power, idx_list, idx_cross),
                ]

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
                )

                # ── Step C: Strategy evaluation ───────────────────────────
                pos_df, _ = fyers.position()

                ce_action, pe_action = strategy.evaluate(
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    base_qty=base_qty,
                    power_list=power_list,
                    position_df=pos_df,
                )

                # ── Step D: Execute orders ────────────────────────────────
                strategy.execute_orders(fyers, ce_action, pe_action)

                # ── Dump positions + account after execution ──────────────
                _dump_positions_and_account(fyers)

                # ── Step E: Check if all positions are closed ─────────────
                if ce_action.is_actionable or pe_action.is_actionable:
                    sleep(2)

                if not _has_open_positions(fyers, ce_symbol, pe_symbol):
                    if not ce_action.is_actionable and not pe_action.is_actionable:
                        pass  # no signal yet — keep waiting
                    else:
                        log_strategy_event(symbol_key, "INNER", "ALL_CLOSED",
                                           details="All positions closed — returning to outer loop")
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
    # (C:/Ballom_FYR/state/demo/ or C:/Ballom_FYR/state/live/)
    configure_state_writer(mode)

    config = load_symbols_config()
    brake = config.get("brake", 0)

    fyers = DemoFyers() if mode == "demo" else Fyers()
    strategy = HeikenAshiMartingale(
        mode=mode,
        brake=bool(brake),
        max_balance_usage=config.get("max_balance_usage", 0),
    )

    indices     = config.get("indices", [])
    commodities = config.get("commodities", [])

    # ── first-time daily setup ─────────────────────────────────────────────
    current_day = date.today()
    option_df, mcx_df, holidays, special_sessions = daily_setup(fyers, force_auth=False)
    indices_scanned_today    = False
    commodities_scanned_today = False

    # ── fetch holidays for current year (and next year in December) ─────────
    current_year = current_day.year
    Fyers.fetch_trading_holidays(current_year)
    if current_day.month == 12:
        Fyers.fetch_trading_holidays(current_year + 1)

    write_app_status(mode, str(current_day), status="started",
                     message=f"Daily setup complete | brake={'ON' if brake else 'OFF'}")

    # ── outer loop (forever) ───────────────────────────────────────────────
    while True:
        today = date.today()
        now   = datetime.now().time()

        # ── Step 1: Day-change → re-auth, re-download, reset flags ─────────
        if today != current_day:
            current_day = today
            option_df, mcx_df, holidays, special_sessions = daily_setup(fyers, force_auth=True)
            indices_scanned_today     = False
            commodities_scanned_today = False

            if today.year != current_year:
                current_year = today.year
                Fyers.fetch_trading_holidays(current_year)
            if today.month == 12:
                Fyers.fetch_trading_holidays(current_year + 1)

            write_app_status(mode, str(current_day), status="new_day",
                             message=f"Daily setup complete for {current_day}")

        # ── Step 2: Determine time window ──────────────────────────────────
        in_indices_window   = INDICES_START <= now <= INDICES_END
        in_commodity_window = COMMODITY_START <= now <= COMMODITY_END

        # ── Step 3: Position conflict check ────────────────────────────────
        pos_df, _ = fyers.position()
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

        # ── Step 3a: INDICES window (first preference) ─────────────────────
        if in_indices_window:
            if has_comm_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Commodity positions open — skipping indices")
            else:
                if not indices_scanned_today:
                    write_app_status(mode, str(current_day), status="scanning",
                                     message="Scanning INDEX option pairs …")
                    scan_and_dump_index_pairs(fyers, indices, option_df)
                    indices_scanned_today = True

                current_day = inner_loop(
                    fyers, strategy,
                    pairs_json=OPTION_PAIRS_JSON,
                    market_type="INDEX",
                    holidays=holidays,
                    special_sessions=special_sessions,
                    inner_day_ref=current_day,
                    mode=mode,
                )

        # ── Step 3b: COMMODITY window (only outside indices hours) ─────────
        elif in_commodity_window and now > INDICES_END:
            if has_idx_pos:
                write_app_status(mode, str(current_day), status="blocked",
                                 message="Index positions open — skipping commodities")
            else:
                if not commodities_scanned_today:
                    write_app_status(mode, str(current_day), status="scanning",
                                     message="Scanning COMMODITY option pairs …")
                    scan_and_dump_commodity_pairs(fyers, commodities, mcx_df)
                    commodities_scanned_today = True

                current_day = inner_loop(
                    fyers, strategy,
                    pairs_json=COMMODITY_PAIRS_JSON,
                    market_type="COMMODITY",
                    holidays=holidays,
                    special_sessions=special_sessions,
                    inner_day_ref=current_day,
                    mode=mode,
                )

        sleep(1)


if __name__ == "__main__":
    main()
