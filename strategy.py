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
    STRATEGY_HEDGE,
    STRATEGY_FACTOR,
    STRATEGY_TIMES,
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
    HEDGE           = STRATEGY_HEDGE
    FACTOR          = STRATEGY_FACTOR
    TIMES           = STRATEGY_TIMES
    PRODUCT_TYPE    = STRATEGY_PRODUCT_TYPE

    def __init__(self, mode: str = "demo", brake: bool = False, max_balance_usage: float = 0, tracker: PositionTracker | None = None) -> None:
        self.mode = mode
        self.brake = brake
        self.max_balance_usage = max_balance_usage
        self.tracker = tracker

    # ── fibonacci helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _recur_fibo(n: int) -> int:
        if n <= 1:
            return n
        return HeikenAshiMartingale._recur_fibo(n - 1) + HeikenAshiMartingale._recur_fibo(n - 2)

    @classmethod
    def _fibo_threshold(cls, position_count: int) -> float:
        """Loss threshold = fib(position_count) ^ factor × times."""
        fib = [cls._recur_fibo(i) for i in range(FIBO_SEQUENCE_LENGTH)][2:]
        try:
            return (fib[position_count] * cls.TIMES) ** cls.FACTOR
        except (ValueError, IndexError):
            return cls.TIMES ** cls.FACTOR

    @classmethod
    def _fibo_next_qty(cls, current_qty: int, lot_size: int) -> int:
        """
        Fibonacci-based next entry qty for martingale.
        E.g. current_qty=65, lot_size=65 → current_lots=1 → fib next=2 → 130
        """
        if current_qty <= 0 or lot_size <= 0:
            return lot_size
        current_lots = abs(current_qty) // lot_size
        fib = [cls._recur_fibo(i) for i in range(FIBO_SEQUENCE_LENGTH)][2:]
        try:
            idx = fib.index(current_lots) + 1
            next_lots = fib[idx] if idx < len(fib) else fib[-1]
        except (ValueError, IndexError):
            next_lots = current_lots + 1
        return int(next_lots * lot_size)

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

        # Effective P&L: adjusted for previously booked profit (HEDGE cycles)
        if self.tracker:
            ce_pl = self.tracker.get_effective_pl(ce_symbol, ce_total_pl)
            pe_pl = self.tracker.get_effective_pl(pe_symbol, pe_total_pl)
        else:
            ce_pl = ce_unrealized
            pe_pl = pe_unrealized

        ce_count = 1 if abs(ce_qty) > 0 else 0
        pe_count = 1 if abs(pe_qty) > 0 else 0

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
                    f"CE_pwr={ce_power}/7 PE_pwr={pe_power}/7",
        )

        # ─────────────────────────────────────────────────────────────────────
        #  CE LOGIC — only when indices are BULLISH
        # ─────────────────────────────────────────────────────────────────────
        if idx_list[0] == 1:
            if ce_qty == 0:
                # ── entry ────────────────────────────────────────────────────
                if ce_list[0] == 1 and ce_cross[0] == 3:
                    ce_action.status = Transaction.BUY
                    log_strategy_event(ce_symbol, "CE", "ENTRY_BUY",
                                       qty=base_qty,
                                       details="Strong bullish crossover (3)")
                elif ce_list[0] == 0 and ce_cross[0] == -3:
                    ce_action.status = Transaction.SELL
                    log_strategy_event(ce_symbol, "CE", "ENTRY_SELL",
                                       qty=base_qty,
                                       details="Strong bearish crossover (-3)")

            elif ce_qty > 0:
                # ── exit / martingale (long) ─────────────────────────────────
                if ce_pl > self.HEDGE:
                    ce_action.status = Transaction.CLOSE_BUY
                    ce_action.qty = ce_qty
                    log_strategy_event(ce_symbol, "CE", "EXIT_PROFIT",
                                       qty=ce_qty, pl=ce_pl,
                                       details=f"P&L {ce_pl:.2f} > target {self.HEDGE}")
                elif ce_cross[0] in (1, -1, -2, -3):
                    ce_action.status = Transaction.CLOSE_BUY
                    ce_action.qty = ce_qty
                    log_strategy_event(ce_symbol, "CE", "EXIT_ADVERSE",
                                       qty=ce_qty, pl=ce_pl,
                                       details=f"Adverse crossover ({ce_cross[0]})")
                elif ce_pl < -self._fibo_threshold(ce_count):
                    thr = self._fibo_threshold(ce_count)
                    mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                    ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                    ce_action.qty = ce_qty
                    ce_action.martingale_qty = mg_qty
                    log_strategy_event(ce_symbol, "CE", "MARTINGALE_BUY",
                                       qty=mg_qty, pl=ce_pl,
                                       details=f"P&L {ce_pl:.2f} < -{thr:.2f}")

            elif ce_qty < 0:
                # ── exit / martingale (short) ────────────────────────────────
                if ce_pl > self.HEDGE:
                    ce_action.status = Transaction.CLOSE_SELL
                    ce_action.qty = abs(ce_qty)
                    log_strategy_event(ce_symbol, "CE", "EXIT_SHORT_PROFIT",
                                       qty=abs(ce_qty), pl=ce_pl,
                                       details=f"Short P&L {ce_pl:.2f} > target {self.HEDGE}")
                elif ce_cross[0] in (3, 2, 1):
                    ce_action.status = Transaction.CLOSE_SELL
                    ce_action.qty = abs(ce_qty)
                    log_strategy_event(ce_symbol, "CE", "EXIT_SHORT_ADVERSE",
                                       qty=abs(ce_qty), pl=ce_pl,
                                       details=f"Bullish crossover ({ce_cross[0]}) against short")
                elif ce_pl < -self._fibo_threshold(ce_count):
                    thr = self._fibo_threshold(ce_count)
                    mg_qty = self._fibo_next_qty(abs(ce_qty), base_qty)
                    ce_action.status = Transaction.SELL_WITH_SPECIFIC_VOLUME
                    ce_action.qty = abs(ce_qty)
                    ce_action.martingale_qty = mg_qty
                    log_strategy_event(ce_symbol, "CE", "MARTINGALE_SELL",
                                       qty=mg_qty, pl=ce_pl,
                                       details=f"P&L {ce_pl:.2f} < -{thr:.2f}")

        # ─────────────────────────────────────────────────────────────────────
        #  PE LOGIC — only when indices are BEARISH
        # ─────────────────────────────────────────────────────────────────────
        elif idx_list[0] == 0:
            if pe_qty == 0:
                # ── entry ────────────────────────────────────────────────────
                if pe_list[0] == 1 and pe_cross[0] == 3:
                    pe_action.status = Transaction.BUY
                    log_strategy_event(pe_symbol, "PE", "ENTRY_BUY",
                                       qty=base_qty,
                                       details="Strong bullish crossover (3)")
                elif pe_list[0] == 0 and pe_cross[0] == -3:
                    pe_action.status = Transaction.SELL
                    log_strategy_event(pe_symbol, "PE", "ENTRY_SELL",
                                       qty=base_qty,
                                       details="Strong bearish crossover (-3)")

            elif pe_qty > 0:
                # ── exit / martingale (long) ─────────────────────────────────
                if pe_pl > self.HEDGE:
                    pe_action.status = Transaction.CLOSE_BUY
                    pe_action.qty = pe_qty
                    log_strategy_event(pe_symbol, "PE", "EXIT_PROFIT",
                                       qty=pe_qty, pl=pe_pl,
                                       details=f"P&L {pe_pl:.2f} > target {self.HEDGE}")
                elif pe_cross[0] in (1, -1, -2, -3):
                    pe_action.status = Transaction.CLOSE_BUY
                    pe_action.qty = pe_qty
                    log_strategy_event(pe_symbol, "PE", "EXIT_ADVERSE",
                                       qty=pe_qty, pl=pe_pl,
                                       details=f"Adverse crossover ({pe_cross[0]})")
                elif pe_pl < -self._fibo_threshold(pe_count):
                    thr = self._fibo_threshold(pe_count)
                    mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                    pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                    pe_action.qty = pe_qty
                    pe_action.martingale_qty = mg_qty
                    log_strategy_event(pe_symbol, "PE", "MARTINGALE_BUY",
                                       qty=mg_qty, pl=pe_pl,
                                       details=f"P&L {pe_pl:.2f} < -{thr:.2f}")

            elif pe_qty < 0:
                # ── exit / martingale (short) ────────────────────────────────
                if pe_pl > self.HEDGE:
                    pe_action.status = Transaction.CLOSE_SELL
                    pe_action.qty = abs(pe_qty)
                    log_strategy_event(pe_symbol, "PE", "EXIT_SHORT_PROFIT",
                                       qty=abs(pe_qty), pl=pe_pl,
                                       details=f"Short P&L {pe_pl:.2f} > target {self.HEDGE}")
                elif pe_cross[0] in (3, 2, 1):
                    pe_action.status = Transaction.CLOSE_SELL
                    pe_action.qty = abs(pe_qty)
                    log_strategy_event(pe_symbol, "PE", "EXIT_SHORT_ADVERSE",
                                       qty=abs(pe_qty), pl=pe_pl,
                                       details=f"Bullish crossover ({pe_cross[0]}) against short")
                elif pe_pl < -self._fibo_threshold(pe_count):
                    thr = self._fibo_threshold(pe_count)
                    mg_qty = self._fibo_next_qty(abs(pe_qty), base_qty)
                    pe_action.status = Transaction.SELL_WITH_SPECIFIC_VOLUME
                    pe_action.qty = abs(pe_qty)
                    pe_action.martingale_qty = mg_qty
                    log_strategy_event(pe_symbol, "PE", "MARTINGALE_SELL",
                                       qty=mg_qty, pl=pe_pl,
                                       details=f"P&L {pe_pl:.2f} < -{thr:.2f}")

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

        In demo mode, DemoFyers handles paper trading internally —
        orders are sent to fyers.buy()/sell() regardless of mode.

        Balance-limit checks block new entries when utilized margin
        exceeds max_balance_usage.  Fibonacci qty escalation is
        already computed in evaluate() and stored in martingale_qty.
        """
        self._execute_single(fyers, ce_action, "CE")
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
            if self.tracker:
                self.tracker.record_close(
                    sym, action.api_total_pl, action.qty, action.pl)
            log_strategy_event(sym, label, "CLOSE_BUY_EXECUTED",
                               qty=action.qty, pl=action.pl, details=str(resp))

        # ── CLOSE_SELL (exit short → buy back existing qty) ────────────────
        elif s == Transaction.CLOSE_SELL:
            resp = fyers.buy(sym, action.qty)
            if self.tracker:
                self.tracker.record_close(
                    sym, action.api_total_pl, action.qty, action.pl)
            log_strategy_event(sym, label, "CLOSE_SELL_EXECUTED",
                               qty=action.qty, pl=action.pl, details=str(resp))

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
