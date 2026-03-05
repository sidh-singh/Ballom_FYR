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

import math
from dataclasses import dataclass
from constants import (
    Transaction,
    STRATEGY_HEDGE_INDEX,
    STRATEGY_HEDGE_COMMODITY,
    STRATEGY_PRODUCT_TYPE,
    FIBO_SEQUENCE_LENGTH,
    MAX_MARTINGALE_LEVEL,
    GAP_RANGE_LOW,
    GAP_RANGE_HIGH,
    ENTRY_RELATIONSHIP_STATUSES,
    RSI_OVERSOLD,
    estimate_trade_charges,
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
    ltp: float = 0.0            # last traded price
    avg_price: float = 0.0      # netAvg (blended avg price)

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

    def mark_pending_close(self, symbol: str, qty: int, pl: float, api_total_pl: float,
                           ltp: float = 0.0, avg_price: float = 0.0) -> None:
        """Mark a symbol as having a pending close order."""
        self._pending_close[symbol] = {
            "qty": qty, "pl": pl, "api_total_pl": api_total_pl,
            "ltp": ltp, "avg_price": avg_price,
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
            # Guard: DemoFyers deletes closed positions from its dict,
            # so _read_position returns pl=0.0 for the deleted row.
            # Using 0.0 as booked_profit corrupts the next cycle's
            # effective_pl (it would include ALL prior realized profit).
            # Fall back to the stale value captured when the close order
            # was sent — this equals the correct cumulative pl at close.
            if api_pl == 0.0 and info["api_total_pl"] != 0.0:
                api_pl = info["api_total_pl"]
            # Recompute effective_pl using the tracker's current booked_profit
            # and the (possibly refreshed) api_pl.  This is the TRUE profit
            # of the cycle that just closed.
            effective_pl = self.tracker.get_effective_pl(symbol, api_pl)
            self.tracker.record_close(
                symbol, api_pl, info["qty"], effective_pl,
                ltp=info.get("ltp", 0.0), avg_price=info.get("avg_price", 0.0),
            )
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

            threshold = fibonacci[martingale_count]² × hedge

        Squaring the fibonacci multiplier produces much wider gaps
        between successive martingale levels, making each add require
        a significantly deeper drawdown than the previous one.

        With HEDGE=500 this produces barriers at:
            level 0 → 1²×500  =    500
            level 1 → 2²×500  =  2,000
            level 2 → 3²×500  =  4,500
            level 3 → 5²×500  = 12,500
            level 4 → 8²×500  = 32,000
            level 5 → 13²×500 = 84,500
            …

        The squared fibonacci gaps aggressively throttle martingale
        adds, preventing capital blow-up on extended drawdowns.
        """
        try:
            multiplier = cls._FIBO[martingale_count]
        except IndexError:
            multiplier = cls._FIBO[-1]  # cap at max fibonacci
        return (multiplier ** 2) * hedge

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
        Return (qty, unrealized_pl, realized_pl, total_pl, ltp, avg_price) for *symbol*.

        total_pl  = realized_profit + unrealized_profit from Fyers API.
        ltp       = last traded price (used for charge estimation).
        avg_price = netAvg from Fyers (blended average across day).
        """
        if position_df is None or position_df.empty:
            return 0, 0.0, 0.0, 0.0, 0.0, 0.0
        row = position_df[
            (position_df["symbol"] == symbol)
            & (position_df["productType"] == product_type)
        ]
        if row.empty:
            return 0, 0.0, 0.0, 0.0, 0.0, 0.0
        return (
            int(row["netQty"].iloc[0]),
            float(row["unrealized_profit"].iloc[0]),
            float(row["realized_profit"].iloc[0]),
            float(row["pl"].iloc[0]),
            float(row["ltp"].iloc[0]) if "ltp" in row.columns else 0.0,
            float(row["netAvg"].iloc[0]) if "netAvg" in row.columns else 0.0,
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
        trend_power_list: list[tuple] | None = None,
        gap_data: dict | None = None,
        relationship_data: dict | None = None,
        rsi_data: dict | None = None,
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
        trend_power_list : same structure as power_list but from Trend SHA (length 11)
                           [(ce_t_power, ce_t_list, ce_t_cross), ...]
        gap_data    : dict with keys ce_gap, pe_gap, idx_gap — each a list of
                      {gap_pct, signal_mid, trend_mid} dicts (most-recent first).
                      Use gap_data["ce_gap"][0]["gap_pct"] for latest CE gap%.
                      GAP_RANGE_LOW / GAP_RANGE_HIGH from constants.py available
                      as class-level references for range checks.
        relationship_data : dict with keys ce_rel, pe_rel, idx_rel — each a dict
                      with {status, strength, avg_gap, delta}.
                      status: "DIVERGING" | "CONVERGING" | "PARALLEL" | "CLOSE"
                      Use relationship_data["ce_rel"]["status"] for CE relationship.
        rsi_data    : dict with keys ce_rsi, pe_rsi — latest RSI value (float)
                      for the CE and PE option prices.  Used to trigger martingale
                      when RSI is oversold (< RSI_OVERSOLD from constants.py).

        Returns
        ───────
        (ce_action, pe_action) — OrderAction dataclasses
        """
        ce_power, ce_list, ce_cross = power_list[0]
        pe_power, pe_list, pe_cross = power_list[1]
        idx_power, idx_list, idx_cross = power_list[2]

        # ── Trend SHA data (optional — backwards compatible) ──────────
        if trend_power_list:
            ce_t_power, ce_t_list, ce_t_cross = trend_power_list[0]
            pe_t_power, pe_t_list, pe_t_cross = trend_power_list[1]
            idx_t_power, idx_t_list, idx_t_cross = trend_power_list[2]
        else:
            ce_t_power = pe_t_power = idx_t_power = 0
            ce_t_list = pe_t_list = idx_t_list = []
            ce_t_cross = pe_t_cross = idx_t_cross = []

        # ── GAP% data (optional — backwards compatible) ───────────────
        # gap_data["ce_gap"] is a dict with "gap_pct" (mean-based).
        # Use GAP_RANGE_LOW / GAP_RANGE_HIGH for range checks.
        _gap = gap_data or {}
        ce_gap_info = _gap.get("ce_gap", {})
        pe_gap_info = _gap.get("pe_gap", {})
        idx_gap_info = _gap.get("idx_gap", {})
        ce_gap_pct = ce_gap_info.get("gap_pct", 0.0) if isinstance(ce_gap_info, dict) else 0.0
        pe_gap_pct = pe_gap_info.get("gap_pct", 0.0) if isinstance(pe_gap_info, dict) else 0.0
        idx_gap_pct = idx_gap_info.get("gap_pct", 0.0) if isinstance(idx_gap_info, dict) else 0.0

        # GAP% range check (Alcadeias-style): only enter when gap between
        # Signal SHA and Trend SHA is within [LOW, HIGH] — confirms
        # momentum aligns with trend without over-extension.
        ce_gap_in_range = GAP_RANGE_LOW <= abs(ce_gap_pct) <= GAP_RANGE_HIGH
        pe_gap_in_range = GAP_RANGE_LOW <= abs(pe_gap_pct) <= GAP_RANGE_HIGH

        # ── SHA Relationship data (optional — backwards compatible) ────
        _rel = relationship_data or {}
        ce_rel = _rel.get("ce_rel", {})
        pe_rel = _rel.get("pe_rel", {})
        idx_rel = _rel.get("idx_rel", {})
        ce_rel_status = ce_rel.get("status", "UNKNOWN")
        pe_rel_status = pe_rel.get("status", "UNKNOWN")
        idx_rel_status = idx_rel.get("status", "UNKNOWN")

        # SHA Relationship filter: only enter when signal-vs-trend
        # relationship matches allowed statuses (e.g. DIVERGING).
        # Disabled (always True) if ENTRY_RELATIONSHIP_STATUSES is empty/None.
        _rel_filter = ENTRY_RELATIONSHIP_STATUSES or set()
        ce_rel_ok = (ce_rel_status in _rel_filter) if _rel_filter else True
        pe_rel_ok = (pe_rel_status in _rel_filter) if _rel_filter else True

        # ── RSI data (optional — backwards compatible) ────────────────
        _rsi = rsi_data or {}
        ce_rsi = _rsi.get("ce_rsi", float('nan'))
        pe_rsi = _rsi.get("pe_rsi", float('nan'))

        ce_qty, ce_unrealized, ce_realized, ce_total_pl, ce_ltp, ce_avg = self._read_position(
            position_df, ce_symbol, self.PRODUCT_TYPE)
        pe_qty, pe_unrealized, pe_realized, pe_total_pl, pe_ltp, pe_avg = self._read_position(
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

        # ── Estimated charges for adjusted hedge target ───────────────
        # The profit target must cover brokerage + STT + exchange + GST +
        # stamp duty so that NET profit ≈ hedge.
        #   num_orders = 1 entry + martingale adds + 1 close
        ce_num_orders = 2 + ce_mg_level
        pe_num_orders = 2 + pe_mg_level
        ce_charges = estimate_trade_charges(abs(ce_qty), ce_ltp, ce_num_orders) if ce_qty != 0 else 0.0
        pe_charges = estimate_trade_charges(abs(pe_qty), pe_ltp, pe_num_orders) if pe_qty != 0 else 0.0
        ce_adj_hedge = hedge + ce_charges
        pe_adj_hedge = hedge + pe_charges

        # NOTE: Cumulative stepped target (prev_profit) was REMOVED.
        # The effective_pl = pl − booked_profit formula already normalises
        # each open/close cycle to start from ~0, so the Fyers blended-
        # average concern that motivated prev_profit is fully addressed.
        # Adding prev_profit inflated the exit target beyond reach on
        # re-entries (e.g. hedge=500 + prev_profit=3708 → target 4208).

        # Defaults — do nothing
        ce_action = OrderAction(
            symbol=ce_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=ce_pl, martingale_qty=0,
            api_total_pl=ce_total_pl, position_qty=ce_qty,
            ltp=ce_ltp, avg_price=ce_avg,
        )
        pe_action = OrderAction(
            symbol=pe_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=pe_pl, martingale_qty=0,
            api_total_pl=pe_total_pl, position_qty=pe_qty,
            ltp=pe_ltp, avg_price=pe_avg,
        )

        idx_trend = "BULLISH" if idx_list[0] == 1 else "BEARISH"
        idx_trend_sha = "BULLISH" if idx_t_list and idx_t_list[0] == 1 else "BEARISH"
        log_strategy_event(
            ce_symbol.split(":")[1] if ":" in ce_symbol else ce_symbol,
            "EVAL", "ANALYSIS",
            details=f"Idx={idx_trend} IdxTrend={idx_trend_sha} "
                    f"CE_cross={ce_cross[0]} PE_cross={pe_cross[0]} "
                    f"CE_pwr={ce_power}/7 PE_pwr={pe_power}/7 "
                    f"GAP: CE={ce_gap_pct:.2f}% PE={pe_gap_pct:.2f}% IDX={idx_gap_pct:.2f}%"
                    f" | REL: CE={ce_rel_status} PE={pe_rel_status} IDX={idx_rel_status}"
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
            # ── entry (Alcadeias-style) ───────────────────────────────
            # Both Signal SHA and Trend SHA must agree on direction,
            # IDX must confirm, and GAP% must be in range.
            # Crossover is still computed but NOT used in entry condition.
            if (ce_list[0] == 1) and (idx_list[0] == 1) and (ce_t_list and ce_t_list[0] == 1) and ce_gap_in_range and ce_rel_ok:
                ce_action.status = Transaction.BUY
                log_strategy_event(ce_symbol, "CE", "ENTRY_BUY",
                                    qty=base_qty,
                                    details=f"Signal+Trend bullish, IDX bullish, "
                                            f"GAP={ce_gap_pct:.2f}% in [{GAP_RANGE_LOW},{GAP_RANGE_HIGH}]"
                                            f" REL={ce_rel_status}")
            elif (pe_list[0] == 1) and (idx_list[0] == 0) and (pe_t_list and pe_t_list[0] == 1) and pe_gap_in_range and pe_rel_ok:
                pe_action.status = Transaction.BUY
                log_strategy_event(pe_symbol, "PE", "ENTRY_BUY",
                                    qty=base_qty,
                                    details=f"Signal+Trend bullish, IDX bearish, "
                                            f"GAP={pe_gap_pct:.2f}% in [{GAP_RANGE_LOW},{GAP_RANGE_HIGH}]"
                                            f" REL={pe_rel_status}")

        elif ce_qty > 0:
            # ── exit / martingale (long CE) ──────────────────────────────
            if ce_pl > ce_adj_hedge:
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "EXIT_PROFIT",
                                    qty=ce_qty, pl=ce_pl,
                                    details=f"P&L {ce_pl:.2f} > adj_target {ce_adj_hedge:.2f}"
                                            f" (hedge={hedge} + charges={ce_charges:.2f})")
            elif not math.isnan(ce_rsi) and ce_rsi < RSI_OVERSOLD and ce_mg_level < MAX_MARTINGALE_LEVEL:
                # RSI oversold → martingale add (average down)
                mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                ce_action.qty = ce_qty
                ce_action.martingale_qty = mg_qty
                log_strategy_event(ce_symbol, "CE", "MARTINGALE_BUY_RSI",
                                    qty=mg_qty, pl=ce_pl,
                                    details=f"RSI={ce_rsi:.2f} < {RSI_OVERSOLD} oversold "
                                            f"(level={ce_mg_level}, fibo_qty={mg_qty})")

        # ─────────────────────────────────────────────────────────────────────
        #  PE LOGIC — only when indices are BEARISH
        # ─────────────────────────────────────────────────────────────────────

        elif pe_qty > 0:
            # ── exit / martingale (long PE) ──────────────────────────────
            if pe_pl > pe_adj_hedge:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "EXIT_PROFIT",
                                    qty=pe_qty, pl=pe_pl,
                                    details=f"P&L {pe_pl:.2f} > adj_target {pe_adj_hedge:.2f}"
                                            f" (hedge={hedge} + charges={pe_charges:.2f})")
            elif not math.isnan(pe_rsi) and pe_rsi < RSI_OVERSOLD and pe_mg_level < MAX_MARTINGALE_LEVEL:
                # RSI oversold → martingale add (average down)
                mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                pe_action.qty = pe_qty
                pe_action.martingale_qty = mg_qty
                log_strategy_event(pe_symbol, "PE", "MARTINGALE_BUY_RSI",
                                    qty=mg_qty, pl=pe_pl,
                                    details=f"RSI={pe_rsi:.2f} < {RSI_OVERSOLD} oversold "
                                            f"(level={pe_mg_level}, fibo_qty={mg_qty})")

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
    #  FORCE-CLOSE — fallback when martingale is blocked
    # ══════════════════════════════════════════════════════════════════════════

    def _force_close(self, fyers, action: OrderAction, label: str, reason: str) -> None:
        """
        Close the existing position when martingale add is blocked
        (by balance limit or hard cap).  First priority: stop the bleeding.

        Works for both demo and live accounts.
        """
        qty = action.position_qty or action.qty
        if qty == 0:
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_SKIP",
                               details=f"No position to close | {reason}")
            return

        # Determine close direction: long position → sell, short → buy
        if qty > 0:
            resp = fyers.sell(action.symbol, abs(qty))
        else:
            resp = fyers.buy(action.symbol, abs(qty))

        order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
        if order_ok:
            self.mark_pending_close(
                action.symbol, abs(qty), action.pl, action.api_total_pl,
                ltp=action.ltp, avg_price=action.avg_price)
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_SENT",
                               qty=abs(qty), pl=action.pl,
                               details=f"Position closed — {reason} | {str(resp)}")
        else:
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_REJECTED",
                               qty=abs(qty), pl=action.pl,
                               details=f"Close REJECTED — will retry | {reason} | {str(resp)}")

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
        martingale_types = (
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
                if s in martingale_types:
                    # Can't add more — CLOSE the position to stop bleeding
                    self._force_close(fyers, action, label,
                                      reason=f"Martingale blocked by balance limit "
                                             f"(utilized ₹{utilized:,.2f} >= ₹{self.max_balance_usage:,.2f})")
                return

        # ── BUY ────────────────────────────────────────────────────────────
        if s == Transaction.BUY:
            resp = fyers.buy(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, 1,
                                          ltp=action.ltp, avg_price=action.ltp)
            log_strategy_event(sym, label, "BUY_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── SELL (short entry) ─────────────────────────────────────────────
        elif s == Transaction.SELL:
            resp = fyers.sell(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, -1,
                                          ltp=action.ltp, avg_price=action.ltp)
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
                    sym, action.qty, action.pl, action.api_total_pl,
                    ltp=action.ltp, avg_price=action.avg_price)
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
                    sym, action.qty, action.pl, action.api_total_pl,
                    ltp=action.ltp, avg_price=action.avg_price)
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
            # Safety guard: block martingale if already at hard cap → close instead
            current_mg = self.tracker.get_martingale_count(sym) if self.tracker else 0
            if current_mg >= MAX_MARTINGALE_LEVEL:
                log_strategy_event(sym, label, "MARTINGALE_BLOCKED",
                                   details=f"mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL} — closing position")
                self._force_close(fyers, action, label,
                                  reason=f"Hard cap mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL}")
                return
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
            # Safety guard: block martingale if already at hard cap → close instead
            current_mg = self.tracker.get_martingale_count(sym) if self.tracker else 0
            if current_mg >= MAX_MARTINGALE_LEVEL:
                log_strategy_event(sym, label, "MARTINGALE_BLOCKED",
                                   details=f"mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL} — closing position")
                self._force_close(fyers, action, label,
                                  reason=f"Hard cap mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL}")
                return
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
