"""
state_writer.py — Atomic JSON state-file management for the dashboard.

All state files live under  C:/Ballom_FYR/state/  and are written
atomically (temp + rename) so the Dash dashboard never reads a
half-written file.

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

# ── root directory ─────────────────────────────────────────────────────────────
STATE_DIR = Path("C:/Ballom_FYR/state")

# ── individual state files ─────────────────────────────────────────────────────
APP_STATUS_FILE     = STATE_DIR / "app_status.json"
SIGNAL_STATE_FILE   = STATE_DIR / "signal_state.json"
POSITION_STATE_FILE = STATE_DIR / "position_state.json"
ACCOUNT_STATE_FILE  = STATE_DIR / "account_state.json"
STRATEGY_LOG_FILE   = STATE_DIR / "strategy_log.json"

# Maximum strategy-log entries kept (FIFO)
_MAX_LOG_ENTRIES = 200


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
) -> None:
    """Upsert one symbol's signal data."""
    data = _read_json(SIGNAL_STATE_FILE)
    data[symbol_key] = {
        "timestamp": _ts(),
        "ce_symbol": ce_symbol,
        "pe_symbol": pe_symbol,
        "underlying": underlying,
        "ce": {"power": ce_power, "list": ce_list, "crossover": ce_crossover},
        "pe": {"power": pe_power, "list": pe_list, "crossover": pe_crossover},
        "idx": {"power": idx_power, "list": idx_list, "crossover": idx_crossover},
        "idx_trend": "BULLISH" if idx_list and idx_list[0] == 1 else "BEARISH",
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
        "balance": balance,
        "utilized": utilized,
        "available": balance - utilized,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": realized_pnl + unrealized_pnl,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": (winning_trades / max(total_trades, 1)) * 100,
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
    """Append a strategy decision to the rolling log (max 200 entries)."""
    entries = _read_json(STRATEGY_LOG_FILE)
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "timestamp": _ts(),
        "symbol": symbol_key,
        "leg": leg,
        "action": action,
        "qty": qty,
        "pl": pl,
        "details": details,
    })
    # Keep only the last _MAX_LOG_ENTRIES
    if len(entries) > _MAX_LOG_ENTRIES:
        entries = entries[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(STRATEGY_LOG_FILE, entries)
