"""
indicator.py — Clean indicator implementations.
"""

import numpy as np
import pandas as pd


class SmoothedHeikenAshi:
    """Smoothed Heiken Ashi (SHA) v3 indicator with flexible MA types."""

    @staticmethod
    def ma(series: pd.Series, length: int, ma_type: str = 'EMA', volume: pd.Series = None) -> pd.Series:
        """
        Moving average with multiple types.
        
        Supported types:
            SMA, EMA, WMA, RMA, VWMA, DEMA, TEMA, ZLEMA, HMA, ALMA, SMMA, SWMA, LSMA, DONCHIAN
        """
        if length <= 0:
            return series

        ma_type = ma_type.upper()

        if ma_type == 'SMA':
            return series.rolling(length).mean()
        elif ma_type == 'EMA':
            return series.ewm(span=length, adjust=False).mean()
        elif ma_type == 'WMA':
            weights = np.arange(1, length + 1)
            return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)
        elif ma_type == 'RMA':
            # ── Match TradingView ta.rma() exactly ──────────────────────
            # TV behaviour:
            #   bars 0 .. length-2  → NaN
            #   bar  length-1       → SMA(source, length)  (seed)
            #   bar  length ..      → alpha*src + (1-alpha)*prev
            # When input already contains leading NaNs (e.g. post-smooth
            # pass on HA values), we find the first window of `length`
            # consecutive non-NaN values to place the SMA seed.
            alpha = 1 / length
            values = series.values.astype(float)
            n = len(values)
            out = np.full(n, np.nan)

            # Find first window of `length` consecutive non-NaN values
            consec = 0
            seed_idx = -1
            for i in range(n):
                if np.isnan(values[i]):
                    consec = 0
                else:
                    consec += 1
                    if consec == length:
                        seed_idx = i
                        break

            if seed_idx < 0:
                return pd.Series(out, index=series.index)

            # SMA seed
            out[seed_idx] = np.mean(values[seed_idx - length + 1 : seed_idx + 1])

            # Recursive from seed_idx + 1
            for i in range(seed_idx + 1, n):
                if np.isnan(values[i]):
                    out[i] = np.nan
                else:
                    out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]

            return pd.Series(out, index=series.index)
        elif ma_type == 'VWMA':
            if volume is None:
                raise ValueError("VWMA requires 'volume' series.")
            return (series * volume).rolling(length).sum() / volume.rolling(length).sum()
        elif ma_type == 'DEMA':
            ema1 = series.ewm(span=length, adjust=False).mean()
            ema2 = ema1.ewm(span=length, adjust=False).mean()
            return 2 * ema1 - ema2
        elif ma_type == 'TEMA':
            ema1 = series.ewm(span=length, adjust=False).mean()
            ema2 = ema1.ewm(span=length, adjust=False).mean()
            ema3 = ema2.ewm(span=length, adjust=False).mean()
            return 3 * (ema1 - ema2) + ema3
        elif ma_type == 'ZLEMA':
            lag = (length - 1) / 2
            return series + (series - series.shift(int(lag))).ewm(span=length, adjust=False).mean()
        elif ma_type == 'HMA':
            wma_half = SmoothedHeikenAshi.ma(series, length // 2, 'WMA')
            wma_full = SmoothedHeikenAshi.ma(series, length, 'WMA')
            return SmoothedHeikenAshi.ma(2 * wma_half - wma_full, int(np.sqrt(length)), 'WMA')
        elif ma_type == 'ALMA':
            offset = 0.85
            sigma = 6
            m = offset * (length - 1)
            s = length / sigma
            weights = [np.exp(-((i - m) ** 2) / (2 * s ** 2)) for i in range(length)]
            weights = np.array(weights) / np.sum(weights)
            return series.rolling(length).apply(lambda x: np.dot(x, weights), raw=True)
        elif ma_type in ('SMMA', 'SWMA'):
            # SMMA is equivalent to RMA — delegate
            return SmoothedHeikenAshi.ma(series, length, 'RMA', volume)
        elif ma_type == 'LSMA':
            return series.rolling(length).apply(
                lambda x: np.polyfit(range(length), x, 1)[0] * (length - 1) + np.polyfit(range(length), x, 1)[1],
                raw=True
            )
        elif ma_type == 'DONCHIAN':
            return (series.rolling(length).max() + series.rolling(length).min()) / 2
        else:
            raise ValueError(f"Unsupported MA type: {ma_type}")

    @staticmethod
    def calculate(
        df: pd.DataFrame,
        smooth_length: int = 10,
        smooth_ma_type: str = 'EMA',
        after_smooth_length: int = 10,
        after_smooth_ma_type: str = 'EMA'
    ) -> pd.DataFrame:
        """
        Compute Smoothed Heiken Ashi v3.
        
        Parameters:
            df: OHLCV DataFrame with columns ['Open', 'High', 'Low', 'Close', 'Volume']
            smooth_length: period for pre-smoothing (default=10)
            smooth_ma_type: MA type for pre-smoothing (default='EMA')
            after_smooth_length: period for post-HA smoothing (default=10)
            after_smooth_ma_type: MA type for post-HA smoothing (default='EMA')
        
        Returns:
            DataFrame with smoothed HA columns: ['Open', 'High', 'Low', 'Close']
        """
        df = df.copy()

        # Step 1: Pre-smooth the OHLC
        o = SmoothedHeikenAshi.ma(df['Open'], smooth_length, smooth_ma_type, df['Volume'])
        h = SmoothedHeikenAshi.ma(df['High'], smooth_length, smooth_ma_type, df['Volume'])
        l = SmoothedHeikenAshi.ma(df['Low'], smooth_length, smooth_ma_type, df['Volume'])
        c = SmoothedHeikenAshi.ma(df['Close'], smooth_length, smooth_ma_type, df['Volume'])

        # Step 2: Heiken Ashi Calculation (recursive)
        # ha_close is NaN wherever any of o/h/l/c is NaN
        ha_close = (o + h + l + c) / 4
        ha_open = pd.Series(np.nan, index=df.index)

        # TradingView: `var float haopen = na`
        # haopen := na(haopen[1]) ? (o + c) / 2 : (haopen[1] + haclose[1]) / 2
        # → ha_open is NaN until the first bar where ha_close is valid,
        #   then seeded with (o + c) / 2 and recursive from there.
        ha_close_vals = ha_close.values
        first_valid_pos = -1
        for i in range(len(ha_close_vals)):
            if not np.isnan(ha_close_vals[i]):
                first_valid_pos = i
                break

        if first_valid_pos >= 0:
            ha_open.iloc[first_valid_pos] = (
                o.iloc[first_valid_pos] + c.iloc[first_valid_pos]
            ) / 2
            for i in range(first_valid_pos + 1, len(df)):
                ha_open.iloc[i] = (
                    ha_open.iloc[i - 1] + ha_close.iloc[i - 1]
                ) / 2

        ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
        ha_low = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)

        # Step 3: Smooth again after HA
        sha_open = SmoothedHeikenAshi.ma(ha_open, after_smooth_length, after_smooth_ma_type, df['Volume'])
        sha_high = SmoothedHeikenAshi.ma(ha_high, after_smooth_length, after_smooth_ma_type, df['Volume'])
        sha_low = SmoothedHeikenAshi.ma(ha_low, after_smooth_length, after_smooth_ma_type, df['Volume'])
        sha_close = SmoothedHeikenAshi.ma(ha_close, after_smooth_length, after_smooth_ma_type, df['Volume'])

        return pd.DataFrame({
            'Open': sha_open,
            'High': sha_high,
            'Low': sha_low,
            'Close': sha_close
        }, index=df.index)
