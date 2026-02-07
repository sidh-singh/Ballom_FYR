"""
demo_fyers.py — Drop-in paper-trading replacement for Fyers.

Extends the real Fyers class so that all market-data methods (historical
data, option-chain scanner, holiday fetch, CSV downloads) still hit the
live API, but BUY / SELL / POSITION are simulated locally.

Storage: C:/Ballom_FYR/demo/
  ├── demo_account.json      — balance, realized P&L, win/loss stats
  ├── demo_positions.json    — open positions keyed by symbol_productType
  ├── demo_trades.json       — recent trades (current session)
  ├── demo_trade_history.json — append-only historical trade log
  └── logs/
      └── trade_log_YYYY-MM-DD.txt — human-readable daily transaction log

All runtime information is written to JSON state files —
no print/log statements.
"""

from __future__ import annotations

import json
import uuid
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from fyers import Fyers
from constants import (
    POSITION_COL, TRADE_COLS, ORDER_COLS,
    OverallPosition, Transaction,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE PATHS  (all under C:/Ballom_FYR/demo/)
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_STORAGE     = Path("C:/Ballom_FYR/demo")
POSITIONS_FILE   = DEMO_STORAGE / "demo_positions.json"
TRADES_FILE      = DEMO_STORAGE / "demo_trades.json"
ACCOUNT_FILE     = DEMO_STORAGE / "demo_account.json"
HISTORY_FILE     = DEMO_STORAGE / "demo_trade_history.json"
DAILY_PNL_FILE   = DEMO_STORAGE / "demo_daily_pnl.json"
LOGS_DIR         = DEMO_STORAGE / "logs"


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DemoPosition:
    """A single simulated position."""
    symbol: str
    qty: int
    side: int               # 1 = long, -1 = short
    avg_price: float
    product_type: str
    entry_time: str
    unrealized_pl: float = 0.0
    realized_pl: float = 0.0
    ltp: float = 0.0
    position_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class DemoTrade:
    """Record of a single executed trade."""
    trade_id: str
    symbol: str
    side: int               # 1 = buy, -1 = sell
    qty: int
    price: float
    product_type: str
    timestamp: str
    order_type: str         # ENTRY | EXIT | PARTIAL_EXIT
    pnl: float = 0.0


@dataclass
class DemoAccount:
    """Paper-trading account state."""
    initial_balance: float = 500_000.0
    current_balance: float = 500_000.0
    utilized_margin: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    last_updated: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
#  DEMO FYERS CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class DemoFyers(Fyers):
    """
    Paper-trading drop-in for :class:`Fyers`.

    Inherits everything from the real Fyers class so that market data,
    option-chain scanning, historical candles, holidays, CSV downloads,
    and authentication all work identically via the live API.

    Only **order execution** and **position tracking** are overridden to
    run locally with JSON persistence on C:/Ballom_FYR/demo/.
    """

    def __init__(self, initial_balance: float = 500_000.0) -> None:
        super().__init__()
        self.initial_balance = initial_balance
        self._lock = threading.RLock()
        self._ensure_storage()
        self.account: DemoAccount = self._load_account()
        self.demo_positions: Dict[str, DemoPosition] = self._load_positions()
        self.trades: List[DemoTrade] = self._load_trades()

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  STORAGE HELPERS                                                         ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def _ensure_storage() -> None:
        DEMO_STORAGE.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _to_native(obj):
        """Convert numpy / pandas types → native Python for JSON."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            return {k: DemoFyers._to_native(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [DemoFyers._to_native(i) for i in obj]
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    # ── account ────────────────────────────────────────────────────────────────

    def _load_account(self) -> DemoAccount:
        if ACCOUNT_FILE.exists():
            try:
                with open(ACCOUNT_FILE, "r") as f:
                    return DemoAccount(**json.load(f))
            except Exception:
                pass
        acct = DemoAccount(
            initial_balance=self.initial_balance,
            current_balance=self.initial_balance,
            last_updated=datetime.now().isoformat(),
        )
        self._save_account(acct)
        return acct

    def _save_account(self, acct: DemoAccount | None = None) -> None:
        acct = acct or self.account
        acct.last_updated = datetime.now().isoformat()
        with open(ACCOUNT_FILE, "w") as f:
            json.dump(self._to_native(asdict(acct)), f, indent=2)

    # ── positions ──────────────────────────────────────────────────────────────

    def _load_positions(self) -> Dict[str, DemoPosition]:
        if POSITIONS_FILE.exists():
            try:
                with open(POSITIONS_FILE, "r") as f:
                    data = json.load(f)
                return {k: DemoPosition(**v) for k, v in data.items()}
            except Exception:
                pass
        return {}

    def _save_positions(self) -> None:
        with self._lock:
            data = {k: asdict(v) for k, v in self.demo_positions.items()}
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self._to_native(data), f, indent=2)

    # ── trades ─────────────────────────────────────────────────────────────────

    def _load_trades(self) -> List[DemoTrade]:
        if TRADES_FILE.exists():
            try:
                with open(TRADES_FILE, "r") as f:
                    return [DemoTrade(**t) for t in json.load(f)]
            except Exception:
                pass
        return []

    def _save_trades(self) -> None:
        with self._lock:
            data = [asdict(t) for t in self.trades]
            with open(TRADES_FILE, "w") as f:
                json.dump(self._to_native(data), f, indent=2)

    def _record_trade(self, trade: DemoTrade) -> None:
        self.trades.append(trade)
        self._save_trades()
        self._append_history(trade)

    def _append_history(self, trade: DemoTrade) -> None:
        history: list = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r") as f:
                    history = json.load(f)
            except Exception:
                pass
        history.append(self._to_native(asdict(trade)))
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)

    # ── daily log (file-based, kept for audit trail) ───────────────────────────

    @staticmethod
    def _daily_log_path() -> Path:
        return LOGS_DIR / f"trade_log_{datetime.now():%Y-%m-%d}.txt"

    def _log_txn(self, action: str, symbol: str, qty: int, price: float,
                 pnl: float = 0.0, details: str = "") -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"\n{'=' * 80}\n"
            f"[{ts}] {action}\n"
            f"{'=' * 80}\n"
            f"  Symbol      : {symbol}\n"
            f"  Quantity    : {qty}\n"
            f"  Price       : ₹{price:,.2f}\n"
            f"  Total Value : ₹{price * qty:,.2f}\n"
            f"  P&L         : ₹{pnl:,.2f}\n"
            f"  Details     : {details}\n"
            f"  Balance     : ₹{self.account.current_balance:,.2f}\n"
            f"  Realized    : ₹{self.account.realized_pnl:,.2f}\n"
            f"  Positions   : {len(self.demo_positions)}\n"
            f"{'=' * 80}\n"
        )
        with open(self._daily_log_path(), "a", encoding="utf-8") as f:
            f.write(entry)

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  PRICE FETCH (via real API)                                              ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _get_ltp(self, symbol: str) -> float:
        """Fetch last-traded-price from live Fyers API."""
        try:
            resp = self.api.quotes(data={"symbols": symbol})
            if resp.get("s") == "ok" and resp.get("d"):
                return float(resp["d"][0].get("v", {}).get("lp", 0))
        except Exception:
            pass
        return 0.0

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  ORDER EXECUTION — SIMULATED                                             ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def buy(self, symbol: str, qty: int, product_type: str = "MARGIN") -> dict:
        """
        Simulate a BUY order.

        • If no position exists → open new long
        • If short position exists → close/reduce short (with P&L)
        • If long position exists → add to long (average up)
        """
        ts = datetime.now()
        ltp = float(self._get_ltp(symbol))
        qty = int(qty)

        if ltp <= 0:
            return {"s": "error", "message": "Price not available"}

        trade_value = ltp * qty
        key = f"{symbol}_{product_type}"
        pnl = 0.0
        order_type = "ENTRY"

        with self._lock:
            if key in self.demo_positions:
                pos = self.demo_positions[key]

                if pos.side == -1:
                    # ── closing / reducing a short ─────────────────────────
                    close_qty = min(qty, pos.qty)
                    pnl = (pos.avg_price - ltp) * close_qty
                    remaining = pos.qty - close_qty
                    order_type = "EXIT" if remaining == 0 else "PARTIAL_EXIT"

                    if remaining <= 0:
                        del self.demo_positions[key]
                    else:
                        pos.qty = remaining

                    self.account.realized_pnl += pnl
                    self.account.current_balance += pnl
                    self.account.utilized_margin -= pos.avg_price * close_qty
                    if pnl > 0:
                        self.account.winning_trades += 1
                    else:
                        self.account.losing_trades += 1

                    leftover = qty - close_qty
                    if leftover > 0:
                        self.demo_positions[key] = DemoPosition(
                            symbol=symbol, qty=leftover, side=1,
                            avg_price=ltp, product_type=product_type,
                            entry_time=ts.isoformat(), ltp=ltp,
                        )
                        self.account.utilized_margin += ltp * leftover

                elif pos.side == 1:
                    # ── averaging up an existing long ──────────────────────
                    total_qty = pos.qty + qty
                    pos.avg_price = (pos.avg_price * pos.qty + ltp * qty) / total_qty
                    pos.qty = total_qty
                    pos.ltp = ltp
                    self.account.utilized_margin += trade_value
            else:
                # ── brand-new long position ────────────────────────────────
                self.demo_positions[key] = DemoPosition(
                    symbol=symbol, qty=qty, side=1,
                    avg_price=ltp, product_type=product_type,
                    entry_time=ts.isoformat(), ltp=ltp,
                )
                self.account.utilized_margin += trade_value

            self.account.total_trades += 1
            self._save_positions()
            self._save_account()

        trade = DemoTrade(
            trade_id=str(uuid.uuid4())[:8], symbol=symbol, side=1,
            qty=qty, price=ltp, product_type=product_type,
            timestamp=ts.isoformat(), order_type=order_type, pnl=pnl,
        )
        self._record_trade(trade)
        self._log_txn("BUY", symbol, qty, ltp, pnl,
                       f"Product={product_type} | ID={trade.trade_id}")

        return {"s": "ok", "message": "DEMO BUY executed", "id": trade.trade_id}

    def sell(self, symbol: str, qty: int, product_type: str = "MARGIN") -> dict:
        """
        Simulate a SELL order.

        • If long position exists → close/reduce long (with P&L)
        • If no position exists → open new short
        • If short position exists → add to short (average down)
        """
        ts = datetime.now()
        ltp = float(self._get_ltp(symbol))
        qty = int(qty)

        if ltp <= 0:
            return {"s": "error", "message": "Price not available"}

        key = f"{symbol}_{product_type}"
        pnl = 0.0
        order_type = "ENTRY"

        with self._lock:
            if key in self.demo_positions:
                pos = self.demo_positions[key]

                if pos.side == 1:
                    # ── closing / reducing a long ──────────────────────────
                    close_qty = min(qty, pos.qty)
                    pnl = (ltp - pos.avg_price) * close_qty
                    remaining = pos.qty - close_qty
                    order_type = "EXIT" if remaining == 0 else "PARTIAL_EXIT"

                    if remaining <= 0:
                        del self.demo_positions[key]
                    else:
                        pos.qty = remaining

                    self.account.realized_pnl += pnl
                    self.account.current_balance += pnl
                    self.account.utilized_margin -= pos.avg_price * close_qty
                    if pnl > 0:
                        self.account.winning_trades += 1
                    else:
                        self.account.losing_trades += 1

                    leftover = qty - close_qty
                    if leftover > 0:
                        self.demo_positions[key] = DemoPosition(
                            symbol=symbol, qty=leftover, side=-1,
                            avg_price=ltp, product_type=product_type,
                            entry_time=ts.isoformat(), ltp=ltp,
                        )
                        self.account.utilized_margin += ltp * leftover

                elif pos.side == -1:
                    # ── averaging into an existing short ───────────────────
                    total_qty = pos.qty + qty
                    pos.avg_price = (pos.avg_price * pos.qty + ltp * qty) / total_qty
                    pos.qty = total_qty
                    pos.ltp = ltp
                    self.account.utilized_margin += ltp * qty
            else:
                # ── brand-new short position ───────────────────────────────
                self.demo_positions[key] = DemoPosition(
                    symbol=symbol, qty=qty, side=-1,
                    avg_price=ltp, product_type=product_type,
                    entry_time=ts.isoformat(), ltp=ltp,
                )
                self.account.utilized_margin += ltp * qty

            self.account.total_trades += 1
            self._save_positions()
            self._save_account()

        trade = DemoTrade(
            trade_id=str(uuid.uuid4())[:8], symbol=symbol, side=-1,
            qty=qty, price=ltp, product_type=product_type,
            timestamp=ts.isoformat(), order_type=order_type, pnl=pnl,
        )
        self._record_trade(trade)

        action_label = "SELL (EXIT)" if pnl != 0 else "SELL (SHORT)"
        self._log_txn(action_label, symbol, qty, ltp, pnl,
                       f"Product={product_type} | ID={trade.trade_id}")

        return {"s": "ok", "message": "DEMO SELL executed",
                "id": trade.trade_id, "pnl": pnl}

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  POSITION / FUNDS / CLOSE — SIMULATED                                   ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def position(self) -> Tuple[pd.DataFrame, OverallPosition]:
        """
        Return (position_df, overall) matching the same signature as
        :meth:`Fyers.position` so all callers work unchanged.
        """
        total_unrealized = 0.0
        rows: list[dict] = []

        for pos in self.demo_positions.values():
            ltp = self._get_ltp(pos.symbol)
            pos.ltp = ltp

            if pos.side == 1:
                unrealized = (ltp - pos.avg_price) * pos.qty
            else:
                unrealized = (pos.avg_price - ltp) * pos.qty
            pos.unrealized_pl = unrealized
            total_unrealized += unrealized

            rows.append({
                "symbol": pos.symbol,
                "id": pos.position_id,
                "buyAvg": pos.avg_price if pos.side == 1 else 0,
                "buyQty": pos.qty if pos.side == 1 else 0,
                "sellAvg": pos.avg_price if pos.side == -1 else 0,
                "sellQty": pos.qty if pos.side == -1 else 0,
                "netAvg": pos.avg_price,
                "netQty": pos.qty * pos.side,
                "side": pos.side,
                "qty": pos.qty,
                "productType": pos.product_type,
                "realized_profit": pos.realized_pl,
                "pl": unrealized,
                "crossCurrency": "N",
                "rbiRefRate": 0,
                "qtyMulti_com": 1,
                "segment": 11,
                "exchange": "NSE",
                "unrealized_profit": unrealized,
                "slNo": 1,
                "ltp": ltp,
                "fytoken": "",
                "cfBuyQty": 0,
                "cfSellQty": 0,
                "dayBuyQty": pos.qty if pos.side == 1 else 0,
                "daySellQty": pos.qty if pos.side == -1 else 0,
            })

        df = pd.DataFrame(rows, columns=POSITION_COL) if rows else pd.DataFrame(columns=POSITION_COL)

        overall = OverallPosition(
            count_total=len(self.demo_positions),
            count_open=len(self.demo_positions),
            pl_total=self.account.realized_pnl + total_unrealized,
            pl_realized=self.account.realized_pnl,
            pl_unrealized=total_unrealized,
        )
        return df, overall

    def funds(self) -> dict:
        """Simulated funds response."""
        total_unrealized = 0.0
        for pos in self.demo_positions.values():
            ltp = self._get_ltp(pos.symbol)
            if pos.side == 1:
                total_unrealized += (ltp - pos.avg_price) * pos.qty
            else:
                total_unrealized += (pos.avg_price - ltp) * pos.qty
        self.account.unrealized_pnl = total_unrealized

        return {
            "s": "ok",
            "fund_limit": [
                {"id": 1, "title": "Total Balance",
                 "equityAmount": self.account.current_balance, "commodityAmount": 0},
                {"id": 2, "title": "Utilized Amount",
                 "equityAmount": self.account.utilized_margin, "commodityAmount": 0},
                {"id": 3, "title": "Available Balance",
                 "equityAmount": self.account.current_balance - self.account.utilized_margin,
                 "commodityAmount": 0},
                {"id": 4, "title": "Realized P&L",
                 "equityAmount": self.account.realized_pnl, "commodityAmount": 0},
                {"id": 5, "title": "Unrealized P&L",
                 "equityAmount": total_unrealized, "commodityAmount": 0},
                {"id": 6, "title": "Initial Balance",
                 "equityAmount": self.account.initial_balance, "commodityAmount": 0},
            ],
        }

    def tradebook(self) -> pd.DataFrame:
        """Return recent demo trades as a DataFrame."""
        rows = []
        for t in self.trades:
            rows.append({
                "symbol": t.symbol,
                "row": 1,
                "orderDateTime": t.timestamp,
                "orderNumber": t.trade_id,
                "tradeNumber": t.trade_id,
                "tradePrice": t.price,
                "tradeValue": t.price * t.qty,
                "tradedQty": t.qty,
                "side": t.side,
                "productType": t.product_type,
                "exchangeOrderNo": t.trade_id,
                "segment": 11,
                "exchange": "NSE",
                "fyToken": "",
                "orderTag": t.symbol.split(":")[1] if ":" in t.symbol else t.symbol,
            })
        return pd.DataFrame(rows, columns=TRADE_COLS) if rows else pd.DataFrame(columns=TRADE_COLS)

    def orderbook(self) -> pd.DataFrame:
        """Demo orderbook (empty — all orders fill instantly)."""
        return pd.DataFrame(columns=ORDER_COLS)

    # ── close helpers ──────────────────────────────────────────────────────────

    def close_by_id(self, symbol: str, product_type: str = "MARGIN") -> dict:
        key = f"{symbol}_{product_type}"
        if key in self.demo_positions:
            pos = self.demo_positions[key]
            if pos.side == 1:
                return self.sell(symbol, pos.qty, product_type)
            else:
                return self.buy(symbol, pos.qty, product_type)
        return {"s": "error", "message": "Position not found"}

    def close_all(self) -> list[dict]:
        results = []
        for pos in list(self.demo_positions.values()):
            if pos.side == 1:
                results.append(self.sell(pos.symbol, pos.qty, pos.product_type))
            else:
                results.append(self.buy(pos.symbol, pos.qty, pos.product_type))
        return results

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  REPORTING                                                               ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _log_daily_summary(self) -> None:
        """Write end-of-day summary to the daily log file."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in self.trades if t.timestamp.startswith(today_str)]
        today_pnl = sum(t.pnl for t in today_trades)

        summary = (
            f"\n{'#' * 80}\n"
            f"  DAILY SUMMARY — {today_str}\n"
            f"{'#' * 80}\n"
            f"  Trades Today : {len(today_trades)}\n"
            f"  Today P&L    : ₹{today_pnl:,.2f}\n"
            f"  Balance      : ₹{self.account.current_balance:,.2f}\n"
            f"  Realized     : ₹{self.account.realized_pnl:,.2f}\n"
            f"  Win/Loss     : {self.account.winning_trades}/{self.account.losing_trades}\n"
            f"  Open Pos     : {len(self.demo_positions)}\n"
            f"{'#' * 80}\n"
        )
        with open(self._daily_log_path(), "a", encoding="utf-8") as f:
            f.write(summary)

    def get_trade_history(self, days: int = 7) -> pd.DataFrame:
        """Load trade history for the last *days* days."""
        if not HISTORY_FILE.exists():
            return pd.DataFrame()
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
        df = pd.DataFrame(history)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        cutoff = datetime.now() - pd.Timedelta(days=days)
        return df[df["timestamp"] >= cutoff]

    def reset_account(self, initial_balance: float | None = None) -> None:
        """Reset the demo account to starting state."""
        bal = initial_balance or self.initial_balance
        self.account = DemoAccount(
            initial_balance=bal, current_balance=bal,
            last_updated=datetime.now().isoformat(),
        )
        self.demo_positions = {}
        self.trades = []
        self._save_account()
        self._save_positions()
        self._save_trades()
