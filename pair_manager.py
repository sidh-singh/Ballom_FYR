"""
pair_manager.py — Active trading pair manager with persistence.

Ensures only 1 CE + 1 PE pair per symbol_key (e.g., NIFTY, SILVERM)
is traded at any time.

Rules
─────
  1. Each symbol_key gets exactly 1 CE + 1 PE pair.
  2. Once locked, pair stays until ALL positions for it are fully closed
     (netQty == 0 for both CE and PE).
  3. Overnight open positions → keep the locked pair, skip re-scan.
  4. After pair is cleared → fresh scan picks new strikes.

State is persisted to  C:/Ballom_FYR/state/<mode>/active_pairs.json
so it survives app restarts and overnight carries.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from constants import get_state_dir


class PairManager:
    """
    Manages active CE/PE pairs per symbol_key with disk persistence.

    Lifecycle:
        1. App starts → PairManager loads persisted pairs from disk
        2. Before scanning → check if locked pair has open positions
        3. If open → skip scan, continue on locked pair
        4. If fully closed → clear lock, allow fresh scan
        5. After scan → lock the new pair
        6. Inner loop trades exclusively on the locked pair
    """

    def __init__(self, mode: str = "demo") -> None:
        self.mode = mode
        self._dir = get_state_dir(mode)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "active_pairs.json"
        self._data: dict = self._read()

    # ── atomic JSON I/O ────────────────────────────────────────────────────

    def _read(self) -> dict:
        if not self._file.exists():
            return {}
        try:
            with open(self._file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(self._file.parent))
        try:
            with open(fd, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
            shutil.move(tmp, str(self._file))
        except Exception:
            if Path(tmp).exists():
                Path(tmp).unlink()
            raise

    # ═══════════════════════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════

    def get_active_pair(self, symbol_key: str) -> dict | None:
        """Return the locked pair dict for *symbol_key*, or None."""
        return self._data.get(symbol_key)

    def has_active_pair(self, symbol_key: str) -> bool:
        """True if *symbol_key* has a locked pair."""
        return symbol_key in self._data

    def lock_pair(
        self,
        symbol_key: str,
        ce_symbol: str,
        pe_symbol: str,
        **extra,
    ) -> None:
        """
        Lock a CE/PE pair for *symbol_key*.
        Extra fields (CE_Strike, PE_Strike, Expiry, qty, indices/commodity, etc.)
        are stored alongside so inner_loop can use them directly.
        """
        self._data[symbol_key] = {
            "CE": ce_symbol,
            "PE": pe_symbol,
            "locked_at": datetime.now().isoformat(),
            **extra,
        }
        self._save()

    def clear_pair(self, symbol_key: str) -> None:
        """Clear the lock for *symbol_key* (all positions closed)."""
        if symbol_key in self._data:
            del self._data[symbol_key]
            self._save()

    def clear_all(self) -> None:
        """Clear all locked pairs."""
        self._data = {}
        self._save()

    def get_all_active(self) -> dict:
        """Return a copy of all locked pairs."""
        return dict(self._data)

    # ═══════════════════════════════════════════════════════════════════════
    #  POSITION CHECKING
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def is_pair_fully_closed(fyers, ce_symbol: str, pe_symbol: str) -> bool:
        """
        Check Fyers positions: return True if BOTH CE and PE have
        netQty == 0 (or no row at all).
        """
        pos_df, _ = fyers.position()
        if pos_df.empty:
            return True

        for sym in (ce_symbol, pe_symbol):
            if not sym:
                continue
            row = pos_df[
                (pos_df["symbol"] == sym)
                & (pos_df["productType"] == "MARGIN")
            ]
            if not row.empty and int(row["netQty"].iloc[0]) != 0:
                return False

        return True

    @staticmethod
    def detect_open_positions(fyers, symbol_key: str) -> dict | None:
        """
        Scan Fyers positions for any OPEN (netQty != 0) positions
        whose symbol name contains *symbol_key* (e.g. "NIFTY").

        Returns {"CE": <symbol>, "PE": <symbol>} if found, else None.
        Used to detect overnight positions that should be continued.
        """
        pos_df, _ = fyers.position()
        if pos_df.empty:
            return None

        open_ce = None
        open_pe = None

        for _, row in pos_df.iterrows():
            sym = str(row["symbol"])
            qty = int(row["netQty"])
            if qty == 0:
                continue
            if str(row["productType"]) != "MARGIN":
                continue

            # Match by symbol_key (e.g., "NIFTY" inside "NFO:NIFTY26MAR26500CE")
            sym_clean = sym.split(":")[-1] if ":" in sym else sym
            if symbol_key.upper() not in sym_clean.upper():
                continue

            if sym_clean.upper().endswith("CE"):
                open_ce = sym
            elif sym_clean.upper().endswith("PE"):
                open_pe = sym

        if open_ce or open_pe:
            return {"CE": open_ce or "", "PE": open_pe or ""}
        return None

    # ═══════════════════════════════════════════════════════════════════════
    #  HIGH-LEVEL PAIR RESOLUTION
    # ═══════════════════════════════════════════════════════════════════════

    def resolve_pair(
        self,
        fyers,
        symbol_key: str,
        scan_fn,
    ) -> dict | None:
        """
        Resolve the active CE/PE pair for *symbol_key*.

        Priority order:
          1. Locked pair with open positions → use it (skip scan)
          2. Locked pair fully closed         → clear lock, fall through
          3. Detect open positions (overnight) → lock them, return
          4. No open positions                 → run scan_fn(), lock result

        *scan_fn* is a callable() -> dict|None  that returns a pair dict
        with keys  {"CE", "PE", "qty", ...}  or None on failure.

        Returns the pair dict (with CE, PE, qty, etc.) or None.
        """
        from state_writer import log_strategy_event

        # ── 1. Check existing locked pair ─────────────────────────────────
        locked = self.get_active_pair(symbol_key)
        if locked:
            ce = locked.get("CE", "")
            pe = locked.get("PE", "")

            if ce or pe:
                closed = self.is_pair_fully_closed(fyers, ce, pe)
                if not closed:
                    # Pair still has open positions → use it
                    log_strategy_event(
                        symbol_key, "PAIR_MGR", "USING_LOCKED",
                        details=f"Open positions on locked pair CE={ce} PE={pe}",
                    )
                    return locked
                else:
                    # All positions closed → clear lock, allow fresh scan
                    log_strategy_event(
                        symbol_key, "PAIR_MGR", "LOCK_CLEARED",
                        details=f"All positions closed for CE={ce} PE={pe} — clearing lock",
                    )
                    self.clear_pair(symbol_key)

        # ── 2. Check for overnight open positions (no lock) ───────────────
        open_pos = self.detect_open_positions(fyers, symbol_key)
        if open_pos:
            ce = open_pos.get("CE", "")
            pe = open_pos.get("PE", "")
            log_strategy_event(
                symbol_key, "PAIR_MGR", "OVERNIGHT_DETECTED",
                details=f"Open overnight positions CE={ce} PE={pe} — locking",
            )
            # We don't have full pair metadata (strikes, expiry, qty)
            # but we have the symbols.  Return a minimal dict; the caller
            # should fill in qty from config.
            self.lock_pair(symbol_key, ce, pe, source="overnight_detect")
            return self.get_active_pair(symbol_key)

        # ── 3. Fresh scan ─────────────────────────────────────────────────
        pair_info = scan_fn()
        if pair_info:
            ce = pair_info.get("CE", "")
            pe = pair_info.get("PE", "")
            self.lock_pair(symbol_key, ce, pe, **{
                k: v for k, v in pair_info.items()
                if k not in ("CE", "PE")
            })
            log_strategy_event(
                symbol_key, "PAIR_MGR", "NEW_PAIR_LOCKED",
                details=f"Fresh scan → CE={ce} PE={pe}",
            )
            return self.get_active_pair(symbol_key)

        # ── 4. Scan failed — no valid pair ────────────────────────────────
        log_strategy_event(
            symbol_key, "PAIR_MGR", "NO_PAIR",
            details="Scan returned no valid pair",
        )
        return None
