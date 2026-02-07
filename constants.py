"""
constants.py — Shared enums, dataclasses, and column definitions.

Only contains values that are actively used across the codebase.
"""

from enum import Enum
from dataclasses import dataclass


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
