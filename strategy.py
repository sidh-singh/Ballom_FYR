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
from constants import Transaction
from state_writer import log_strategy_event


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

    # ── tunable parameters ─────────────────────────────────────────────────────
    HEDGE           = 100     # Profit target (₹) for closing positions
    FACTOR          = 1.4     # Exponent for fibonacci loss threshold
    TIMES           = 1       # Base multiplier for fibonacci sizing
    PRODUCT_TYPE    = "MARGIN"

    def __init__(self, mode: str = "demo", brake: bool = False) -> None:
        self.mode = mode
        self.brake = brake

    # ── fibonacci helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _recur_fibo(n: int) -> int:
        if n <= 1:
            return n
        return HeikenAshiMartingale._recur_fibo(n - 1) + HeikenAshiMartingale._recur_fibo(n - 2)

    @classmethod
    def _fibo_threshold(cls, position_count: int) -> float:
        """Loss threshold = fib(position_count) ^ factor × times."""
        fib = [cls._recur_fibo(i) for i in range(25)][2:]
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
        fib = [cls._recur_fibo(i) for i in range(25)][2:]
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
        Return (qty, unrealised_pl) for *symbol* from position DataFrame.
        """
        if position_df is None or position_df.empty:
            return 0, 0.0
        row = position_df[
            (position_df["symbol"] == symbol)
            & (position_df["productType"] == product_type)
        ]
        if row.empty:
            return 0, 0.0
        return int(row["netQty"].iloc[0]), float(row["unrealized_profit"].iloc[0])

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

        ce_qty, ce_pl = self._read_position(position_df, ce_symbol, self.PRODUCT_TYPE)
        pe_qty, pe_pl = self._read_position(position_df, pe_symbol, self.PRODUCT_TYPE)

        ce_count = 1 if abs(ce_qty) > 0 else 0
        pe_count = 1 if abs(pe_qty) > 0 else 0

        # Defaults — do nothing
        ce_action = OrderAction(
            symbol=ce_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=ce_pl, martingale_qty=0,
        )
        pe_action = OrderAction(
            symbol=pe_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=pe_pl, martingale_qty=0,
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
    #  EXECUTE ORDERS — side-effect: calls fyers.buy() / fyers.sell()
    # ══════════════════════════════════════════════════════════════════════════

    def execute_orders(self, fyers, ce_action: OrderAction, pe_action: OrderAction) -> None:
        """
        Place orders for CE and PE legs based on the evaluated actions.

        In demo mode, DemoFyers handles paper trading internally —
        orders are sent to fyers.buy()/sell() regardless of mode.
        """
        self._execute_single(fyers, ce_action, "CE")
        self._execute_single(fyers, pe_action, "PE")

    def _execute_single(self, fyers, action: OrderAction, label: str) -> None:
        """Execute a single leg's order action."""
        s = action.status
        sym = action.symbol

        if s == Transaction.DO_NOTHING:
            return

        # ── brake guard ────────────────────────────────────────────────────
        if self.brake and s in (Transaction.BUY, Transaction.SELL):
            log_strategy_event(sym, label, "BRAKE_BLOCKED",
                               details=f"{s.name} blocked — brake is ON")
            return

        # ── BUY ────────────────────────────────────────────────────────────
        if s == Transaction.BUY:
            resp = fyers.buy(sym, action.qty)
            log_strategy_event(sym, label, "BUY_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── SELL (short entry) ─────────────────────────────────────────────
        elif s == Transaction.SELL:
            resp = fyers.sell(sym, action.qty)
            log_strategy_event(sym, label, "SELL_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── CLOSE_BUY (exit long → sell existing qty) ─────────────────────
        elif s == Transaction.CLOSE_BUY:
            resp = fyers.sell(sym, action.qty)
            log_strategy_event(sym, label, "CLOSE_BUY_EXECUTED",
                               qty=action.qty, pl=action.pl, details=str(resp))

        # ── CLOSE_SELL (exit short → buy back existing qty) ────────────────
        elif s == Transaction.CLOSE_SELL:
            resp = fyers.buy(sym, action.qty)
            log_strategy_event(sym, label, "CLOSE_SELL_EXECUTED",
                               qty=action.qty, pl=action.pl, details=str(resp))

        # ── BUY_WITH_SPECIFIC_VOLUME (martingale add long) ────────────────
        elif s == Transaction.BUY_WITH_SPECIFIC_VOLUME:
            resp = fyers.buy(sym, action.martingale_qty)
            log_strategy_event(sym, label, "MARTINGALE_BUY_EXECUTED",
                               qty=action.martingale_qty, details=str(resp))

        # ── SELL_WITH_SPECIFIC_VOLUME (martingale add short) ──────────────
        elif s == Transaction.SELL_WITH_SPECIFIC_VOLUME:
            resp = fyers.sell(sym, action.martingale_qty)
            log_strategy_event(sym, label, "MARTINGALE_SELL_EXECUTED",
                               qty=action.martingale_qty, details=str(resp))

        else:
            log_strategy_event(sym, label, "UNHANDLED",
                               details=f"Unhandled status: {s.name}")
