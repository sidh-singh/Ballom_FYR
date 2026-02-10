"""
strategy.py — Heiken-Ashi Martingale strategy.

Ported from FyersHeikenAshiMartingale.calculate_signal + place_order logic.

Key rules
─────────
• Indices direction determines which leg is active:
    - Indices BULLISH (lt_list[0] == 1) → only CE trades
    - Indices BEARISH (lt_list[0] == 0) → only PE trades
• Entry: strong crossover (±3) aligned with SHA momentum
• Exit:  profit target OR weak / adverse crossover
• Martingale: fibonacci-based position doubling on deep loss

All strategy decisions are logged to JSON state files —
no print/log statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from constants import (
    Transaction,
    STRATEGY_HEDGE_INDEX,
    STRATEGY_HEDGE_COMMODITY,
    STRATEGY_PRODUCT_TYPE,
    FIBO_SEQUENCE_LENGTH,
)
from state_writer import log_strategy_event
from position_tracker import PositionTracker


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderAction:
    """What the strategy wants to do with a single leg (CE or PE)."""
    symbol: str
    status: Transaction     # BUY, SELL, CLOSE_BUY, BUY_WITH_SPECIFIC_VOLUME, DO_NOTHING …
    qty: int                # base lot qty (for new entries) or current qty (for exits)
    pl: float               # unrealised P&L (0 if no position)
    martingale_qty: int     # fibonacci-calculated qty (only for BUY_WITH_SPECIFIC_VOLUME)
    api_total_pl: float = 0.0   # raw `pl` from Fyers API (realized + unrealized)
    position_qty: int = 0       # actual current position qty from API

    @property
    def is_actionable(self) -> bool:
        return self.status not in (Transaction.DO_NOTHING, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class HeikenAshiMartingale:
    """
    Stateless strategy evaluator.

    Call `evaluate()` with the latest power_list + position data → get
    (ce_action, pe_action) back.  Then call `execute_orders()` to place
    them via the Fyers object.
    """

    # ── tunable parameters (from constants.py) ────────────────────────────────
    PRODUCT_TYPE    = STRATEGY_PRODUCT_TYPE

    # Pre-compute fibonacci sequence once (shared across all instances)
    _FIBO = [0, 1]
    for _i in range(2, FIBO_SEQUENCE_LENGTH):
        _FIBO.append(_FIBO[-1] + _FIBO[-2])
    _FIBO = _FIBO[2:]  # [1, 2, 3, 5, 8, 13, 21, 34, 55, …]

    def __init__(self, mode: str = "demo", brake: bool = False, max_balance_usage: float = 0, tracker: PositionTracker | None = None) -> None:
        self.mode = mode
        self.brake = brake
        self.max_balance_usage = max_balance_usage
        self.tracker = tracker
        # Track symbols with pending close orders (order sent but not yet
        # confirmed filled).  On live Fyers, place_order() returns before
        # the order fills.  We must NOT update the tracker's booked_profit
        # until the position actually reaches netQty=0.
        self._pending_close: dict[str, dict] = {}  # symbol → {qty, pl, api_total_pl}

    # ── pending-close helpers ──────────────────────────────────────────────

    def mark_pending_close(self, symbol: str, qty: int, pl: float, api_total_pl: float) -> None:
        """Mark a symbol as having a pending close order."""
        self._pending_close[symbol] = {
            "qty": qty, "pl": pl, "api_total_pl": api_total_pl,
        }

    def is_pending_close(self, symbol: str) -> bool:
        """True if a close order was sent but position hasn't zeroed out yet."""
        return symbol in self._pending_close

    def confirm_close(self, symbol: str, current_api_total_pl: float | None = None) -> None:
        """
        Called when position for *symbol* has confirmed netQty=0.
        NOW update the tracker's booked_profit.

        Parameters
        ──────────
        current_api_total_pl : If provided, use this FRESH pl value (from
            the latest position read) instead of the stale value captured
            when the close order was sent.  This ensures booked_profit
            reflects the actual post-close pl, not a pre-fill estimate.
        """
        info = self._pending_close.pop(symbol, None)
        if info and self.tracker:
            # Prefer fresh pl from the current position read
            api_pl = current_api_total_pl if current_api_total_pl is not None else info["api_total_pl"]
            # Recompute effective_pl using the tracker's current booked_profit
            # and the (possibly refreshed) api_pl.  This is the TRUE profit
            # of the cycle that just closed.
            effective_pl = self.tracker.get_effective_pl(symbol, api_pl)
            self.tracker.record_close(symbol, api_pl, info["qty"], effective_pl)
            log_strategy_event(symbol, "CLOSE", "CLOSE_CONFIRMED",
                               qty=info["qty"], pl=effective_pl,
                               details=f"Position confirmed closed — tracker updated"
                                       f" | api_pl={api_pl:.2f}")

    def cancel_pending_close(self, symbol: str) -> None:
        """Cancel a pending close (order rejected or timed out)."""
        self._pending_close.pop(symbol, None)

    # ── fibonacci helpers ──────────────────────────────────────────────────────

    @classmethod
    def _fibo_threshold(cls, martingale_count: int, hedge: float) -> float:
        """
        Loss threshold before the *next* martingale fires.

            threshold = fibonacci[martingale_count] × hedge

        With HEDGE=500 this produces barriers at:
            level 0 → -500   (1×500)
            level 1 → -1000  (2×500)
            level 2 → -1500  (3×500)
            level 3 → -2500  (5×500)
            level 4 → -4000  (8×500)
            level 5 → -6500  (13×500)
            …

        The increasing gaps prevent rapid-fire martingale adds.
        """
        try:
            multiplier = cls._FIBO[martingale_count]
        except IndexError:
            multiplier = cls._FIBO[-1]  # cap at max fibonacci
        return multiplier * hedge

    @classmethod
    def _fibo_next_qty(cls, current_qty: int, lot_size: int) -> int:
        """
        Fibonacci-based next entry qty for martingale.
        E.g. current_qty=65, lot_size=65 → current_lots=1 → fib next=2 → 130
        """
        if current_qty <= 0 or lot_size <= 0:
            return lot_size
        current_lots = abs(current_qty) // lot_size
        try:
            idx = cls._FIBO.index(current_lots) + 1
            next_lots = cls._FIBO[idx] if idx < len(cls._FIBO) else cls._FIBO[-1]
        except (ValueError, IndexError):
            next_lots = current_lots + 1
        return int(next_lots * lot_size)

    def _get_martingale_count(self, symbol: str) -> int:
        """Read martingale level from tracker (0 = no martingale yet)."""
        if self.tracker:
            return self.tracker.get_martingale_count(symbol)
        return 0

    # ── position introspection ─────────────────────────────────────────────────

    @staticmethod
    def _read_position(position_df, symbol: str, product_type: str = "MARGIN"):
        """
        Return (qty, unrealized_pl, realized_pl, total_pl) for *symbol*.
        total_pl = realized_profit + unrealized_profit from Fyers API.
        """
        if position_df is None or position_df.empty:
            return 0, 0.0, 0.0, 0.0
        row = position_df[
            (position_df["symbol"] == symbol)
            & (position_df["productType"] == product_type)
        ]
        if row.empty:
            return 0, 0.0, 0.0, 0.0
        return (
            int(row["netQty"].iloc[0]),
            float(row["unrealized_profit"].iloc[0]),
            float(row["realized_profit"].iloc[0]),
            float(row["pl"].iloc[0]),
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  EVALUATE — pure signal logic, no side effects
    # ══════════════════════════════════════════════════════════════════════════

    def evaluate(
        self,
        ce_symbol: str,
        pe_symbol: str,
        base_qty: int,
        power_list: list[tuple],
        position_df,
        hedge: float = STRATEGY_HEDGE_INDEX,
    ) -> tuple[OrderAction, OrderAction]:
        """
        Determine the trading action for CE and PE legs.

        Parameters
        ──────────
        ce_symbol   : e.g. "NFO:NIFTY26FEB26000CE"
        pe_symbol   : e.g. "NFO:NIFTY26FEB25800PE"
        base_qty    : lot size × qty_times (from JSON)
        power_list  : [(ce_power, ce_list, ce_cross),
                       (pe_power, pe_list, pe_cross),
                       (idx_power, idx_list, idx_cross)]
        position_df : DataFrame from fyers.position()
        hedge       : ₹ profit target for this pair (default: index HEDGE)

        Returns
        ───────
        (ce_action, pe_action) — OrderAction dataclasses
        """
        ce_power, ce_list, ce_cross = power_list[0]
        pe_power, pe_list, pe_cross = power_list[1]
        idx_power, idx_list, idx_cross = power_list[2]

        ce_qty, ce_unrealized, ce_realized, ce_total_pl = self._read_position(
            position_df, ce_symbol, self.PRODUCT_TYPE)
        pe_qty, pe_unrealized, pe_realized, pe_total_pl = self._read_position(
            position_df, pe_symbol, self.PRODUCT_TYPE)

        # Effective P&L for the CURRENT open/close cycle:
        #     effective_pl = pl − booked_profit
        # where pl = realized + unrealized (Fyers' total day P&L for the symbol)
        # and booked_profit = pl captured at the most recent close.
        #
        # IMPORTANT: We must use `pl` (total), NOT `unrealized_profit` alone.
        # Fyers recalculates buyAvg/sellAvg across ALL intraday trades, so
        # unrealized_profit is NOT the clean floating P&L of the current
        # position cycle — it's contaminated by blended averages.  Using
        # `pl` (total) cancels perfectly with the previous booked_profit.
        if self.tracker:
            ce_pl = self.tracker.get_effective_pl(ce_symbol, ce_total_pl)
            pe_pl = self.tracker.get_effective_pl(pe_symbol, pe_total_pl)
        else:
            ce_pl = ce_total_pl
            pe_pl = pe_total_pl

        # Martingale level from tracker (0 = no martingale yet, 1 = one add, etc.)
        # This drives the fibonacci-scaled loss barrier: fib[level] × hedge
        ce_mg_level = self._get_martingale_count(ce_symbol)
        pe_mg_level = self._get_martingale_count(pe_symbol)

        # Defaults — do nothing
        ce_action = OrderAction(
            symbol=ce_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=ce_pl, martingale_qty=0,
            api_total_pl=ce_total_pl, position_qty=ce_qty,
        )
        pe_action = OrderAction(
            symbol=pe_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=pe_pl, martingale_qty=0,
            api_total_pl=pe_total_pl, position_qty=pe_qty,
        )

        idx_trend = "BULLISH" if idx_list[0] == 1 else "BEARISH"
        log_strategy_event(
            ce_symbol.split(":")[1] if ":" in ce_symbol else ce_symbol,
            "EVAL", "ANALYSIS",
            details=f"Idx={idx_trend} CE_cross={ce_cross[0]} PE_cross={pe_cross[0]} "
                    f"CE_pwr={ce_power}/7 PE_pwr={pe_power}/7"
                    f" | pending_close: CE={self.is_pending_close(ce_symbol)} PE={self.is_pending_close(pe_symbol)}",
        )

        # ─────────────────────────────────────────────────────────────────────
        #  PENDING-CLOSE HANDLING (live market: order sent, awaiting fill)
        #
        #  If a close was sent last cycle but position still has qty > 0,
        #  re-issue the CLOSE.  If qty has reached 0, confirm the close
        #  so the tracker's booked_profit gets updated NOW (not before).
        # ─────────────────────────────────────────────────────────────────────

        if self.is_pending_close(ce_symbol):
            if ce_qty == 0:
                # Close order filled — confirm with FRESH pl from position read
                self.confirm_close(ce_symbol, current_api_total_pl=ce_total_pl)
            else:
                # Still open — re-issue close
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "RETRY_CLOSE",
                                    qty=ce_qty, pl=ce_pl,
                                    details=f"Pending close not yet filled — retrying")
                # Also handle PE pending in parallel
                if self.is_pending_close(pe_symbol):
                    if pe_qty == 0:
                        self.confirm_close(pe_symbol, current_api_total_pl=pe_total_pl)
                    else:
                        pe_action.status = Transaction.CLOSE_BUY
                        pe_action.qty = pe_qty
                return ce_action, pe_action

        if self.is_pending_close(pe_symbol):
            if pe_qty == 0:
                self.confirm_close(pe_symbol, current_api_total_pl=pe_total_pl)
            else:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "RETRY_CLOSE",
                                    qty=pe_qty, pl=pe_pl,
                                    details=f"Pending close not yet filled — retrying")
                return ce_action, pe_action

        # ─────────────────────────────────────────────────────────────────────
        #  CE LOGIC — only when indices are BULLISH
        # ─────────────────────────────────────────────────────────────────────

        if ce_qty == 0 and pe_qty == 0:
            # ── entry ────────────────────────────────────────────────────
            
            if (ce_list[0] == 1) and (idx_list[0] == 1) and (ce_cross[0] == 3):
                ce_action.status = Transaction.BUY
                log_strategy_event(ce_symbol, "CE", "ENTRY_BUY",
                                    qty=base_qty,
                                    details="Strong bullish crossover (3)")
            elif (pe_list[0] == 1) and (idx_list[0] == 0) and (pe_cross[0] == 3):
                pe_action.status = Transaction.BUY
                log_strategy_event(pe_symbol, "PE", "ENTRY_BUY",
                                    qty=base_qty,
                                    details="Strong bullish crossover (3)")

        elif ce_qty > 0:
            # ── exit / martingale (long CE) ──────────────────────────────
            # if idx_list[0] == 0:
            #     # Index trend flipped BEARISH → close CE immediately
            #     ce_action.status = Transaction.CLOSE_BUY
            #     ce_action.qty = ce_qty
            #     log_strategy_event(ce_symbol, "CE", "EXIT_TREND_FLIP",
            #                         qty=ce_qty, pl=ce_pl,
            #                         details=f"Index now BEARISH — closing CE")
            if ce_pl > hedge:
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "EXIT_PROFIT",
                                    qty=ce_qty, pl=ce_pl,
                                    details=f"P&L {ce_pl:.2f} > target {hedge}")
            elif ce_list[0] == 0:
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "EXIT_ADVERSE",
                                    qty=ce_qty, pl=ce_pl,
                                    details=f"Adverse crossover ({ce_cross[0]})")
            elif ce_pl < -self._fibo_threshold(ce_mg_level, hedge):
                thr = self._fibo_threshold(ce_mg_level, hedge)
                mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                ce_action.qty = ce_qty
                ce_action.martingale_qty = mg_qty
                log_strategy_event(ce_symbol, "CE", "MARTINGALE_BUY",
                                    qty=mg_qty, pl=ce_pl,
                                    details=f"P&L {ce_pl:.2f} < -{thr:.2f} (level={ce_mg_level})")

        # ─────────────────────────────────────────────────────────────────────
        #  PE LOGIC — only when indices are BEARISH
        # ─────────────────────────────────────────────────────────────────────

        elif pe_qty > 0:
            # ── exit / martingale (long PE) ──────────────────────────────
            # if idx_list[0] == 1:
            #     # Index trend flipped BULLISH → close PE immediately
            #     pe_action.status = Transaction.CLOSE_BUY
            #     pe_action.qty = pe_qty
            #     log_strategy_event(pe_symbol, "PE", "EXIT_TREND_FLIP",
            #                         qty=pe_qty, pl=pe_pl,
            #                         details=f"Index now BULLISH — closing PE")
            if pe_pl > hedge:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "EXIT_PROFIT",
                                    qty=pe_qty, pl=pe_pl,
                                    details=f"P&L {pe_pl:.2f} > target {hedge}")
            elif pe_list[0] == 0:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "EXIT_ADVERSE",
                                    qty=pe_qty, pl=pe_pl,
                                    details=f"Adverse crossover ({pe_cross[0]})")
            elif pe_pl < -self._fibo_threshold(pe_mg_level, hedge):
                thr = self._fibo_threshold(pe_mg_level, hedge)
                mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                pe_action.qty = pe_qty
                pe_action.martingale_qty = mg_qty
                log_strategy_event(pe_symbol, "PE", "MARTINGALE_BUY",
                                    qty=mg_qty, pl=pe_pl,
                                    details=f"P&L {pe_pl:.2f} < -{thr:.2f} (level={pe_mg_level})")

        return ce_action, pe_action

    # ══════════════════════════════════════════════════════════════════════════
    #  BALANCE LIMIT HELPERS  (ported from app_fyers_strategy)
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _get_utilized_balance(fyers) -> tuple:
        """
        Fetch current utilized balance from Fyers API.
        Returns: (utilized_equity, utilized_commodity, total_utilized)
        """
        try:
            funds_response = fyers.funds()
            fund_limit = funds_response.get("fund_limit", [])

            utilized_equity = 0
            utilized_commodity = 0

            for item in fund_limit:
                title = item.get("title", "").lower()
                if "utilized" in title or "used" in title or item.get("id") == 2:
                    utilized_equity += abs(item.get("equityAmount", 0))
                    utilized_commodity += abs(item.get("commodityAmount", 0))

            return utilized_equity, utilized_commodity, utilized_equity + utilized_commodity
        except Exception:
            return 0, 0, 0

    def _check_balance_limit(self, fyers, order_type: Transaction) -> tuple:
        """
        Check if balance usage limit will be exceeded.
        Returns: (can_trade: bool, utilized_amount: float)

        Only blocks new entry/martingale orders (BUY, SELL, BUY_WITH_SPECIFIC_VOLUME,
        SELL_WITH_SPECIFIC_VOLUME).  Exit orders (CLOSE_BUY, CLOSE_SELL) always pass.
        """
        # No limit configured → allow everything
        if self.max_balance_usage <= 0:
            return True, 0

        # Exit orders are never blocked
        exit_types = (
            Transaction.CLOSE, Transaction.CLOSE_BUY, Transaction.CLOSE_SELL,
            Transaction.DO_NOTHING, Transaction.RESET,
        )
        if order_type in exit_types:
            return True, 0

        _, _, total_utilized = self._get_utilized_balance(fyers)

        if total_utilized >= self.max_balance_usage:
            log_strategy_event(
                "BALANCE", "CHECK", "LIMIT_EXHAUSTED",
                details=f"Utilized ₹{total_utilized:,.2f} >= Limit ₹{self.max_balance_usage:,.2f}",
            )
            return False, total_utilized

        return True, total_utilized

    # ══════════════════════════════════════════════════════════════════════════
    #  EXECUTE ORDERS — side-effect: calls fyers.buy() / fyers.sell()
    #
    #  Balance-limit and fibonacci qty escalation (from app_fyers_strategy)
    #  are enforced here so that every MARTINGALE_BUY/SELL increases
    #  quantity along the fibonacci series.
    # ══════════════════════════════════════════════════════════════════════════

    def execute_orders(self, fyers, ce_action: OrderAction, pe_action: OrderAction) -> None:
        """
        Place orders for CE and PE legs based on the evaluated actions.

        EXIT orders (CLOSE_BUY / CLOSE_SELL) are always executed FIRST
        so that opposite-side positions are closed before new entries
        are opened.  This prevents holding both CE and PE simultaneously
        after a trend flip.
        """
        exit_types = (Transaction.CLOSE_BUY, Transaction.CLOSE_SELL)

        # ── Phase 1: execute all exits first ──────────────────────────────
        if ce_action.status in exit_types:
            self._execute_single(fyers, ce_action, "CE")
        if pe_action.status in exit_types:
            self._execute_single(fyers, pe_action, "PE")

        # ── Phase 2: execute entries / martingale / other ─────────────────
        if ce_action.status not in exit_types:
            self._execute_single(fyers, ce_action, "CE")
        if pe_action.status not in exit_types:
            self._execute_single(fyers, pe_action, "PE")

    def _execute_single(self, fyers, action: OrderAction, label: str) -> None:
        """Execute a single leg's order action with balance-limit enforcement."""
        s = action.status
        sym = action.symbol

        if s == Transaction.DO_NOTHING:
            return

        # ── brake guard ────────────────────────────────────────────────────
        if self.brake and s in (Transaction.BUY, Transaction.SELL):
            log_strategy_event(sym, label, "BRAKE_BLOCKED",
                               details=f"{s.name} blocked — brake is ON")
            return

        # ── balance-limit guard for entry / martingale orders ──────────────
        entry_types = (
            Transaction.BUY, Transaction.SELL,
            Transaction.BUY_WITH_SPECIFIC_VOLUME,
            Transaction.SELL_WITH_SPECIFIC_VOLUME,
        )
        if s in entry_types:
            can_trade, utilized = self._check_balance_limit(fyers, s)
            if not can_trade:
                log_strategy_event(
                    sym, label, "BALANCE_BLOCKED",
                    details=f"{s.name} blocked — utilized ₹{utilized:,.2f} >= limit ₹{self.max_balance_usage:,.2f}",
                )
                return

        # ── BUY ────────────────────────────────────────────────────────────
        if s == Transaction.BUY:
            resp = fyers.buy(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, 1)
            log_strategy_event(sym, label, "BUY_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── SELL (short entry) ─────────────────────────────────────────────
        elif s == Transaction.SELL:
            resp = fyers.sell(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, -1)
            log_strategy_event(sym, label, "SELL_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── CLOSE_BUY (exit long → sell existing qty) ─────────────────────
        elif s == Transaction.CLOSE_BUY:
            resp = fyers.sell(sym, action.qty)
            order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
            if order_ok:
                # Order accepted — mark as pending close.
                # Tracker update is DEFERRED until position confirms netQty=0
                # (handled in evaluate() next cycle via confirm_close).
                self.mark_pending_close(
                    sym, action.qty, action.pl, action.api_total_pl)
                log_strategy_event(sym, label, "CLOSE_BUY_SENT",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order accepted — pending fill | {str(resp)}")
            else:
                # Order rejected — do NOT update tracker, will retry next cycle
                log_strategy_event(sym, label, "CLOSE_BUY_REJECTED",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order REJECTED — will retry | {str(resp)}")

        # ── CLOSE_SELL (exit short → buy back existing qty) ────────────────
        elif s == Transaction.CLOSE_SELL:
            resp = fyers.buy(sym, action.qty)
            order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
            if order_ok:
                self.mark_pending_close(
                    sym, action.qty, action.pl, action.api_total_pl)
                log_strategy_event(sym, label, "CLOSE_SELL_SENT",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order accepted — pending fill | {str(resp)}")
            else:
                log_strategy_event(sym, label, "CLOSE_SELL_REJECTED",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order REJECTED — will retry | {str(resp)}")

        # ── BUY_WITH_SPECIFIC_VOLUME (martingale add long) ────────────────
        #    qty increases along the fibonacci series (computed by evaluate)
        elif s == Transaction.BUY_WITH_SPECIFIC_VOLUME:
            fibo_qty = action.martingale_qty
            resp = fyers.buy(sym, fibo_qty)
            if self.tracker:
                self.tracker.record_martingale(
                    sym, fibo_qty, abs(action.position_qty),
                    1, action.pl, action.api_total_pl)
            log_strategy_event(sym, label, "MARTINGALE_BUY_EXECUTED",
                               qty=fibo_qty,
                               details=f"fibo_qty={fibo_qty} | {str(resp)}")

        # ── SELL_WITH_SPECIFIC_VOLUME (martingale add short) ──────────────
        #    qty increases along the fibonacci series (computed by evaluate)
        elif s == Transaction.SELL_WITH_SPECIFIC_VOLUME:
            fibo_qty = action.martingale_qty
            resp = fyers.sell(sym, fibo_qty)
            if self.tracker:
                self.tracker.record_martingale(
                    sym, fibo_qty, abs(action.position_qty),
                    -1, action.pl, action.api_total_pl)
            log_strategy_event(sym, label, "MARTINGALE_SELL_EXECUTED",
                               qty=fibo_qty,
                               details=f"fibo_qty={fibo_qty} | {str(resp)}")

        else:
            log_strategy_event(sym, label, "UNHANDLED",
                               details=f"Unhandled status: {s.name}")
