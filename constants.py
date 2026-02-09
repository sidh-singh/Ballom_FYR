"""
constants.py — Shared enums, dataclasses, and column definitions.

Only contains values that are actively used across the codebase.
"""

from enum import Enum
from dataclasses import dataclass
from datetime import time as dt_time
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_JSON         = Path(__file__).resolve().parent / "symbols.json"
OPTION_PAIRS_JSON    = Path("C:/Ballom_FYR/option_pairs.json")
COMMODITY_PAIRS_JSON = Path("C:/Ballom_FYR/commodity_pairs.json")

# ── State file directories (mode-separated so demo & live never clash) ─────────
STATE_DIR_BASE       = Path("C:/Ballom_FYR/state")
STATE_DIR_DEMO       = STATE_DIR_BASE / "demo"
STATE_DIR_LIVE       = STATE_DIR_BASE / "live"


def get_state_dir(mode: str) -> Path:
    """Return the state directory for the given mode."""
    return STATE_DIR_LIVE if mode == "live" else STATE_DIR_DEMO


# ── Dashboard config ───────────────────────────────────────────────────────────
DASHBOARD_PORT           = 8050
DASHBOARD_REFRESH_MS     = 3_000   # auto-refresh interval (milliseconds)


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING TIME WINDOWS
# ═══════════════════════════════════════════════════════════════════════════════

INDICES_START   = dt_time(9, 15)
INDICES_END     = dt_time(15, 30)
COMMODITY_START = dt_time(9, 15)
COMMODITY_END   = dt_time(23, 55)


# ═══════════════════════════════════════════════════════════════════════════════
#  SHA INDICATOR PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

SHA_LENGTH          = 7
SHA_MA_TYPE         = "RMA"
DEFAULT_TIMEFRAME   = "1"
DEFAULT_CANDLES     = 500


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP TIMING  (near real-time: 1 second)
# ═══════════════════════════════════════════════════════════════════════════════

INNER_LOOP_INTERVAL = 1   # seconds between each strategy evaluation cycle


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY TUNING PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_HEDGE          = 100      # Profit target (₹) for closing positions
STRATEGY_FACTOR         = 1.6      # Exponent for fibonacci loss threshold
STRATEGY_TIMES          = 1        # Base multiplier for fibonacci sizing
STRATEGY_PRODUCT_TYPE   = "MARGIN"
FIBO_SEQUENCE_LENGTH    = 25       # Length of fibonacci sequence for martingale


# ═══════════════════════════════════════════════════════════════════════════════
#  TRANSACTION ENUM
# ═══════════════════════════════════════════════════════════════════════════════

class Transaction(Enum):
    BUY = 1
    SELL = -1
    BUY_WITH_SPECIFIC_VOLUME = 40
    SELL_WITH_SPECIFIC_VOLUME = 41
    CLOSE = 0
    CLOSE_BUY = 21
    CLOSE_SELL = 22
    DO_NOTHING = 2
    RESET = 8


# ═══════════════════════════════════════════════════════════════════════════════
#  COLUMN DEFINITIONS  (Fyers API response shapes)
# ═══════════════════════════════════════════════════════════════════════════════

ORDER_COLS = [
    'id', 'exchOrdId', 'symbol', 'qty', 'remainingQuantity', 'filledQty',
    'status', 'slNo', 'message', 'segment', 'limitPrice', 'stopPrice',
    'productType', 'type', 'side', 'disclosedQty', 'orderValidity',
    'orderDateTime', 'parentId', 'tradedPrice', 'source', 'fytoken',
    'offlineOrder', 'pan', 'clientId', 'exchange', 'instrument',
    'discloseQty', 'orderTag',
]

TRADE_COLS = [
    'symbol', 'row', 'orderDateTime', 'orderNumber', 'tradeNumber',
    'tradePrice', 'tradeValue', 'tradedQty', 'side', 'productType',
    'exchangeOrderNo', 'segment', 'exchange', 'fyToken', 'orderTag',
]

POSITION_COL = [
    'symbol', 'id', 'buyAvg', 'buyQty', 'sellAvg', 'sellQty', 'netAvg',
    'netQty', 'side', 'qty', 'productType', 'realized_profit', 'pl',
    'crossCurrency', 'rbiRefRate', 'qtyMulti_com', 'segment', 'exchange',
    'unrealized_profit', 'slNo', 'ltp', 'fytoken', 'cfBuyQty', 'cfSellQty',
    'dayBuyQty', 'daySellQty', 'exchange',
]

SYMBOLS_COLS = [
    'Fytoken', 'Symbol Details', 'Exchange Instrument type',
    'Minimum lot size', 'Tick size', 'ISIN', 'Trading Session',
    'Last update date', 'Expiry date', 'Symbol ticker', 'Exchange',
    'Segment', 'Scrip code', 'Underlying symbol', 'Underlying scrip code',
    'Strike price', 'Option type', 'Underlying FyToken',
    'Reserved column1', 'Reserved column2', 'Reserved column3',
]


# ═══════════════════════════════════════════════════════════════════════════════
#  DATACLASSES  (API payloads & response wrappers)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlaceOrder:
    symbol: str
    qty: int
    type: int
    side: int
    productType: str
    limitPrice: float
    stopPrice: float
    validity: str
    disclosedQty: int
    stopLoss: float
    takeProfit: float
    offlineOrder: bool
    orderTag: str


@dataclass
class CloseBySymbol:
    id: list


@dataclass
class CloseBySection:
    segment: list
    side: list
    productType: list


@dataclass
class OverallPosition:
    count_total: int
    count_open: int
    pl_total: float
    pl_realized: float
    pl_unrealized: float
