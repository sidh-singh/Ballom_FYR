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

            # Recursive: alpha * src + (1 - alpha) * nz(prev)
            # Pine's nz() replaces NaN with 0
            for i in range(seed_idx + 1, n):
                if np.isnan(values[i]):
                    out[i] = np.nan
                else:
                    prev = 0.0 if np.isnan(out[i - 1]) else out[i - 1]
                    out[i] = alpha * values[i] + (1 - alpha) * prev

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
        elif ma_type == 'SMMA':
            # Pine: smma := na(smma[1]) ? src : (smma[1] * (length - 1) + src) / length
            # Key difference from RMA: seed = first non-NaN src value, NOT SMA(length)
            values = series.values.astype(float)
            n = len(values)
            out = np.full(n, np.nan)
            for i in range(n):
                if np.isnan(values[i]):
                    continue
                if i == 0 or np.isnan(out[i - 1]):
                    out[i] = values[i]                    # na(smma[1]) ? src
                else:
                    out[i] = (out[i - 1] * (length - 1) + values[i]) / length
            return pd.Series(out, index=series.index)
        elif ma_type == 'SWMA':
            # Pine: ta.swma(src) — fixed length 4, symmetric weights [1/6, 2/6, 2/6, 1/6]
            w = np.array([1.0, 2.0, 2.0, 1.0]) / 6.0
            return series.rolling(4).apply(lambda x: np.dot(x, w), raw=True)
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

        # Step 2: Heiken Ashi — line-for-line match with Pine Script:
        #   haclose = (o + h + l + c) / 4.0
        #   haopen  := na(haopen[1]) ? (o + c) / 2 : (haopen[1] + haclose[1]) / 2
        #   hahigh  = math.max(h, math.max(haopen, haclose))
        #   halow   = math.min(l, math.min(haopen, haclose))
        ha_close = (o + h + l + c) / 4.0

        o_vals  = o.values.astype(float)
        c_vals  = c.values.astype(float)
        hc_vals = ha_close.values.astype(float)
        n = len(df)
        ho_vals = np.full(n, np.nan)

        for i in range(n):
            if i == 0 or np.isnan(ho_vals[i - 1]):
                # Pine: na(haopen[1]) → seed with (o + c) / 2
                ov, cv = o_vals[i], c_vals[i]
                if np.isnan(ov) or np.isnan(cv):
                    ho_vals[i] = np.nan
                else:
                    ho_vals[i] = (ov + cv) / 2.0
            else:
                # Pine: (haopen[1] + haclose[1]) / 2
                ho_vals[i] = (ho_vals[i - 1] + hc_vals[i - 1]) / 2.0

        ha_open = pd.Series(ho_vals, index=df.index)

        # Pine: math.max(na, x) = na  →  skipna=False
        ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1, skipna=False)
        ha_low  = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1, skipna=False)

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


class RSI:
    """
    Relative Strength Index (RSI) — Wilder's smoothing.

    Matches TradingView's ta.rsi() exactly:
        change  = close - close[1]
        gain    = ta.rma(max(change, 0), length)
        loss    = ta.rma(max(-change, 0), length)
        rs      = gain / loss
        rsi     = 100 - 100 / (1 + rs)
    """

    @staticmethod
    def calculate(
        df: pd.DataFrame,
        length: int = 14,
        source_col: str = "Close",
    ) -> pd.Series:
        """
        Compute RSI on a DataFrame.

        Parameters
        ──────────
        df          : OHLCV DataFrame (must contain *source_col*).
        length      : RSI look-back period (default 14).
        source_col  : Column to compute RSI on (default 'Close').

        Returns
        ───────
        pd.Series of RSI values (0–100), same index as *df*.
        """
        source = df[source_col].astype(float)
        change = source.diff()

        gain = change.clip(lower=0)
        loss = (-change).clip(lower=0)

        # Wilder's smoothing = RMA (same as ta.rma in Pine)
        avg_gain = SmoothedHeikenAshi.ma(gain, length, "RMA")
        avg_loss = SmoothedHeikenAshi.ma(loss, length, "RMA")

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

        return rsi
