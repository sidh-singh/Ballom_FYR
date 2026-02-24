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

# Signal SHA — fast / short-term momentum indicator
SHA_LENGTH          = 35
SHA_MA_TYPE         = "RMA"

# Trend SHA — slower / longer-term trend indicator
SHA_TREND_LENGTH    = 70
SHA_TREND_MA_TYPE   = "RMA"

DEFAULT_TIMEFRAME   = "1"
DEFAULT_CANDLES     = 500


# ═══════════════════════════════════════════════════════════════════════════════
#  GAP% PARAMETERS  (gap between Signal SHA and Trend SHA)
# ═══════════════════════════════════════════════════════════════════════════════
# GAP% = ((signal_sha_mid - trend_sha_mid) / trend_sha_mid) × 100
# where mid = (High + Low) / 2  (mean of SHA candle)
#
# GAP_RANGE_LOW / GAP_RANGE_HIGH define comfortable bounds for strategy use.
# When |GAP%| is within [LOW, HIGH] range, trend and momentum agree.
# When |GAP%| exceeds HIGH, signal is over-extended from trend.
# When |GAP%| is below LOW, signal is converging with trend (range-bound).

GAP_RANGE_LOW       = 2.0     # % — below this, signal is too close to trend
GAP_RANGE_HIGH      = 16.0     # % — above this, signal is diverging from trend


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMISSION / TAX ESTIMATION
# ═══════════════════════════════════════════════════════════════════════════════
# Used to buffer the hedge profit target so that NET profit (after broker
# charges) still meets the target.
#
# Fyers options intraday charges:
#   Brokerage       ₹20 per executed order (flat)
#   STT             0.0625% of sell-side premium (options)
#   Exchange txn    ~0.0495% per side (NSE F&O)
#   GST             18% on (brokerage + exchange + SEBI charges)
#   Stamp duty      ~0.003% on buy-side turnover
#   SEBI            ₹10 per crore turnover (negligible)
#
# For each trade cycle, total charges ≈ ₹40-60 brokerage + proportional.
# The strategy adds estimated charges to the hedge target so that
# GROSS P&L at exit ≥ hedge + charges → NET P&L ≈ hedge.

BROKERAGE_PER_ORDER   = 20.0      # ₹ flat per executed order
STT_OPTIONS_RATE      = 0.000625  # 0.0625% on sell-side premium
EXCHANGE_TXN_RATE     = 0.000495  # ~0.0495% per side (NSE F&O)
GST_RATE              = 0.18      # 18% on (brokerage + exchange + SEBI)
STAMP_DUTY_RATE       = 0.00003   # ~0.003% on buy-side turnover
SEBI_PER_CRORE        = 10.0      # ₹10 per crore turnover


def estimate_trade_charges(
    qty: int,
    ltp: float,
    num_orders: int = 2,
) -> float:
    """
    Estimate total broker charges for a trade cycle (₹).

    Parameters
    ──────────
    qty         : Position quantity being closed.
    ltp         : Last traded price of the option.
    num_orders  : Total orders in the cycle:
                  1 (entry) + N (martingale adds) + 1 (close) = 2 + N.

    Returns
    ───────
    Estimated total ₹ charges (brokerage + STT + exchange + GST + stamp).
    """
    if qty <= 0 or ltp <= 0:
        return 0.0

    turnover = abs(qty) * ltp  # approximate per-side turnover

    brokerage = BROKERAGE_PER_ORDER * num_orders
    stt       = STT_OPTIONS_RATE * turnover                 # sell side
    exchange  = EXCHANGE_TXN_RATE * turnover * 2            # both sides
    sebi      = (turnover * 2 / 1_00_00_000) * SEBI_PER_CRORE
    gst       = GST_RATE * (brokerage + exchange + sebi)
    stamp     = STAMP_DUTY_RATE * turnover                  # buy side

    return round(brokerage + stt + exchange + gst + stamp + sebi, 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP TIMING  (near real-time: 1 second)
# ═══════════════════════════════════════════════════════════════════════════════

INNER_LOOP_INTERVAL = 1   # seconds between each strategy evaluation cycle


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY TUNING PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

# Profit targets (₹) — configurable per-symbol in symbols.json via "hedge" key.
# These are the defaults when symbols.json doesn't specify a value.
STRATEGY_HEDGE_INDEX      = 500    # ₹ profit target for index option pairs
STRATEGY_HEDGE_COMMODITY  = 200    # ₹ profit target for commodity option pairs
#                                    (bumped from 100 → 200 because flat ₹20/order
#                                     brokerage eats ~48% of a ₹100 target at qty=1)
STRATEGY_PRODUCT_TYPE     = "MARGIN"
FIBO_SEQUENCE_LENGTH      = 25     # Length of fibonacci sequence for martingale
MAX_MARTINGALE_LEVEL      = 2      # Hard cap: max martingale adds (entry + 2 adds, close on 3rd trigger)

# Martingale threshold formula (SQUARED FIBONACCI):
#   threshold[level] = fibonacci[level]² × HEDGE
#   e.g. HEDGE=500 → barriers at -500, -2000, -4500, -12500, -32000, …
# Squaring the fibonacci sequence [1, 2, 3, 5, 8, 13, 21, …] produces
# much wider gaps between martingale adds, aggressively throttling
# capital usage on extended drawdowns.


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
    'dayBuyQty', 'daySellQty',
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
