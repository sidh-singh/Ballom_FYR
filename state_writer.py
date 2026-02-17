"""
state_writer.py — Atomic JSON state-file management for the dashboard.

All state files live under  C:/Ballom_FYR/state/<mode>/  and are written
atomically (temp + rename) so the Dash dashboard never reads a
half-written file.

Call ``configure(mode)`` once at startup (in app.py) before any writes.
Demo and live modes write to separate directories so switching modes
never corrupts or mixes state.

State files
───────────
  app_status.json     — mode, day, trading window, scan flags
  signal_state.json   — per-symbol SHA analysis (power, list, crossover)
  position_state.json — open positions & P/L snapshot
  account_state.json  — balance, realized / unrealised P&L
  strategy_log.json   — rolling log of strategy decisions (last 200)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from constants import STATE_DIR_DEMO, get_state_dir

# ── module-level state (set via configure) ─────────────────────────────────────
_mode: str = "demo"
STATE_DIR: Path = STATE_DIR_DEMO

APP_STATUS_FILE: Path     = STATE_DIR / "app_status.json"
SIGNAL_STATE_FILE: Path   = STATE_DIR / "signal_state.json"
POSITION_STATE_FILE: Path = STATE_DIR / "position_state.json"
ACCOUNT_STATE_FILE: Path  = STATE_DIR / "account_state.json"
STRATEGY_LOG_FILE: Path   = STATE_DIR / "strategy_log.json"      # legacy single file
STRATEGY_LOG_DIR: Path    = STATE_DIR / "strategy_log"            # date-partitioned dir

# Maximum strategy-log entries kept per date (FIFO)
_MAX_LOG_ENTRIES = 500

# How many days of strategy logs to retain
_MAX_LOG_DAYS = 14


def configure(mode: str) -> None:
    """
    Set the active mode ('demo' or 'live') — must be called once at startup.
    All subsequent writes go to the mode-specific state directory.
    """
    global _mode, STATE_DIR
    global APP_STATUS_FILE, SIGNAL_STATE_FILE, POSITION_STATE_FILE
    global ACCOUNT_STATE_FILE, STRATEGY_LOG_FILE, STRATEGY_LOG_DIR

    _mode = mode.lower()
    STATE_DIR = get_state_dir(_mode)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    APP_STATUS_FILE     = STATE_DIR / "app_status.json"
    SIGNAL_STATE_FILE   = STATE_DIR / "signal_state.json"
    POSITION_STATE_FILE = STATE_DIR / "position_state.json"
    ACCOUNT_STATE_FILE  = STATE_DIR / "account_state.json"
    STRATEGY_LOG_FILE   = STATE_DIR / "strategy_log.json"
    STRATEGY_LOG_DIR    = STATE_DIR / "strategy_log"
    STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_current_mode() -> str:
    """Return the currently configured mode."""
    return _mode


# ═══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _write_json_atomic(path: Path, data: Any) -> None:
    """Write *data* to *path* atomically via temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        shutil.move(tmp, str(path))
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
#  APP STATUS  (outer-loop lifecycle)
# ═══════════════════════════════════════════════════════════════════════════════

def write_app_status(
    mode: str,
    current_day: str,
    in_indices_window: bool = False,
    in_commodity_window: bool = False,
    indices_scanned: bool = False,
    commodities_scanned: bool = False,
    status: str = "running",
    message: str = "",
) -> None:
    _write_json_atomic(APP_STATUS_FILE, {
        "timestamp": _ts(),
        "mode": mode,
        "current_day": current_day,
        "in_indices_window": in_indices_window,
        "in_commodity_window": in_commodity_window,
        "indices_scanned": indices_scanned,
        "commodities_scanned": commodities_scanned,
        "status": status,
        "message": message,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL STATE  (per-symbol SHA analysis)
# ═══════════════════════════════════════════════════════════════════════════════

def write_signal_state(
    symbol_key: str,
    ce_symbol: str,
    pe_symbol: str,
    underlying: str,
    ce_power: int,
    ce_list: list,
    ce_crossover: list,
    pe_power: int,
    pe_list: list,
    pe_crossover: list,
    idx_power: int,
    idx_list: list,
    idx_crossover: list,
    ce_sha_debug: list | None = None,
    pe_sha_debug: list | None = None,
    idx_sha_debug: list | None = None,
    # ── Trend SHA (longer period) ─────────────────────────────────────
    ce_trend_power: int = 0,
    ce_trend_list: list | None = None,
    ce_trend_crossover: list | None = None,
    pe_trend_power: int = 0,
    pe_trend_list: list | None = None,
    pe_trend_crossover: list | None = None,
    idx_trend_power: int = 0,
    idx_trend_list: list | None = None,
    idx_trend_crossover: list | None = None,
    ce_trend_sha_debug: list | None = None,
    pe_trend_sha_debug: list | None = None,
    idx_trend_sha_debug: list | None = None,
    # ── GAP% between Signal SHA and Trend SHA ─────────────────────────
    ce_gap: list | None = None,
    pe_gap: list | None = None,
    idx_gap: list | None = None,
    market_type: str = "INDEX",
) -> None:
    """Upsert one symbol's signal data (signal SHA + trend SHA + GAP%)."""
    data = _read_json(SIGNAL_STATE_FILE)
    data[symbol_key] = {
        "timestamp": _ts(),
        "ce_symbol": ce_symbol,
        "pe_symbol": pe_symbol,
        "underlying": underlying,
        "market_type": market_type,
        # Signal SHA
        "ce": {"power": ce_power, "list": ce_list, "crossover": ce_crossover,
               "sha": ce_sha_debug or []},
        "pe": {"power": pe_power, "list": pe_list, "crossover": pe_crossover,
               "sha": pe_sha_debug or []},
        "idx": {"power": idx_power, "list": idx_list, "crossover": idx_crossover,
                "sha": idx_sha_debug or []},
        "idx_trend": "BULLISH" if idx_list and idx_list[0] == 1 else "BEARISH",
        # Trend SHA
        "ce_trend": {"power": ce_trend_power, "list": ce_trend_list or [],
                     "crossover": ce_trend_crossover or [],
                     "sha": ce_trend_sha_debug or []},
        "pe_trend": {"power": pe_trend_power, "list": pe_trend_list or [],
                     "crossover": pe_trend_crossover or [],
                     "sha": pe_trend_sha_debug or []},
        "idx_trend_sha": {"power": idx_trend_power, "list": idx_trend_list or [],
                          "crossover": idx_trend_crossover or [],
                          "sha": idx_trend_sha_debug or []},
        # GAP% between Signal and Trend SHA
        "ce_gap": ce_gap or [],
        "pe_gap": pe_gap or [],
        "idx_gap": idx_gap or [],
    }
    _write_json_atomic(SIGNAL_STATE_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION STATE  (snapshot of open positions + P&L)
# ═══════════════════════════════════════════════════════════════════════════════

def write_position_state(position_rows: list[dict], overall: dict) -> None:
    """
    *position_rows*: list of dicts (one per open position row).
    *overall*: dict with count_total, count_open, pl_total, pl_realized, pl_unrealized.
    """
    _write_json_atomic(POSITION_STATE_FILE, {
        "timestamp": _ts(),
        "positions": position_rows,
        "overall": overall,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT STATE  (balance, P&L)
# ═══════════════════════════════════════════════════════════════════════════════

def write_account_state(
    balance: float,
    utilized: float,
    realized_pnl: float,
    unrealized_pnl: float,
    total_trades: int = 0,
    winning_trades: int = 0,
    losing_trades: int = 0,
) -> None:
    _write_json_atomic(ACCOUNT_STATE_FILE, {
        "timestamp": _ts(),
        "balance": round(balance, 2),
        "utilized": round(utilized, 2),
        "available": round(balance - utilized, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round((winning_trades / max(total_trades, 1)) * 100, 2),
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY LOG  (rolling decision log)
# ═══════════════════════════════════════════════════════════════════════════════

def log_strategy_event(
    symbol_key: str,
    leg: str,
    action: str,
    qty: int = 0,
    pl: float = 0.0,
    details: str = "",
) -> None:
    """Append a strategy decision to the date-partitioned log.

    Logs are stored as  strategy_log/YYYY-MM-DD.json  so each trading
    day's events are preserved independently.  Old date files beyond
    _MAX_LOG_DAYS are pruned automatically.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = STRATEGY_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{today}.json"

    entries = _read_json(log_file)
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "timestamp": _ts(),
        "date": today,
        "symbol": symbol_key,
        "leg": leg,
        "action": action,
        "qty": qty,
        "pl": pl,
        "details": details,
    })
    # Keep only the last _MAX_LOG_ENTRIES per day
    if len(entries) > _MAX_LOG_ENTRIES:
        entries = entries[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(log_file, entries)

    # Also write to legacy single file for backward compatibility
    legacy = _read_json(STRATEGY_LOG_FILE)
    if not isinstance(legacy, list):
        legacy = []
    legacy.append(entries[-1])
    if len(legacy) > _MAX_LOG_ENTRIES:
        legacy = legacy[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(STRATEGY_LOG_FILE, legacy)

    # Prune old date-partitioned log files
    _cleanup_old_strategy_logs()


def _cleanup_old_strategy_logs() -> None:
    """Remove date-partitioned strategy log files older than _MAX_LOG_DAYS."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=_MAX_LOG_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    log_dir = STRATEGY_LOG_DIR
    if not log_dir.exists():
        return
    for f in log_dir.iterdir():
        if f.suffix == ".json" and f.stem < cutoff_str:
            try:
                f.unlink()
            except Exception:
                pass
