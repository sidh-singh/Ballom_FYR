"""
Fyers - Single entry point for authentication, trading, and market-data downloads.

Token lifecycle:
  1. On startup, try loading token from C:/fyers_token.json
  2. If the file exists *and* was written today, verify it and reuse.
  3. Otherwise do a full TOTP-based login and persist the new token.
  4. The outer loop in app.py calls `ensure_session()` once per day-change.
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, date, timedelta, time as dt_time
from time import sleep
from typing import Tuple
from dataclasses import asdict, replace
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import os, json, pyotp, requests, warnings, time as _time
import numpy as np
import pandas as pd

from constants import (
    SYMBOLS_COLS, PlaceOrder, Transaction, CloseBySymbol, CloseBySection,
    POSITION_COL, TRADE_COLS, ORDER_COLS, OverallPosition,
)

warnings.filterwarnings("ignore")

# ── Paths — all under C:/Ballom_FYR/ ──────────────────────────────────────────
BASE_DIR    = Path("C:/Ballom_FYR")
TOKEN_FILE  = BASE_DIR / "fyers_token.json"
CACHE_DIR   = BASE_DIR / "cache"

# Public Fyers symbol CSVs
NSE_FO_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"
MCX_COM_URL = "https://public.fyers.in/sym_details/MCX_COM.csv"


class Fyers:
    """Thin wrapper around FyersModel for auth, orders, positions, and data."""

    # ── credentials (same as FyersAdapter) ─────────────────────────────────────
    FY_ID       = "YS07018"
    SECRET_KEY  = "9FBBBL2MAY"
    APP_ID      = "OUDS3XQTRU"
    APP_TYPE    = "100"
    CLIENT_ID   = f"{APP_ID}-{APP_TYPE}"
    GRANT_TYPE  = "authorization_code"
    RESPONSE_TYPE = "code"
    STATE       = "sample"
    PIN         = "0000"
    TOTP_KEY    = "NTXL3YEXLUC2QRZYAGC2ZTUMC3FJLBLZ"
    REDIRECT_URI = "https://jenkin.thealgotrading.in/"

    BASE_URL    = "https://api-t2.fyers.in/vagator/v2"
    BASE_URL_2  = "https://api-t1.fyers.in/api/v3"

    def __init__(self) -> None:
        self._api: fyersModel.FyersModel | None = None
        self._token_date: date | None = None  # date the current token was issued

        self._buy_tpl = PlaceOrder(
            symbol="", qty=0, type=2, side=Transaction.BUY.value,
            productType="MARGIN", limitPrice=0, stopPrice=0, validity="DAY",
            disclosedQty=0, stopLoss=0, takeProfit=0, offlineOrder=False, orderTag="",
        )
        self._sell_tpl = PlaceOrder(
            symbol="", qty=0, type=2, side=Transaction.SELL.value,
            productType="MARGIN", limitPrice=0, stopPrice=0, validity="DAY",
            disclosedQty=0, stopLoss=0, takeProfit=0, offlineOrder=False, orderTag="",
        )
        self._close_by_symbol = CloseBySymbol(id=[])
        self._close_by_section = CloseBySection(segment=[], side=[], productType=[])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  AUTH                                                                    ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def ensure_session(self, force: bool = False, read_only: bool = False) -> fyersModel.FyersModel:
        """
        Return a ready-to-use FyersModel.
        - Reuses today's token from disk unless *force* is True.
        - On day-change the caller should pass force=True.
        - If *read_only* is True, never attempt TOTP login — only load
          from the shared token file.  Use this from updater branches
          that rely on dev_scanner for authentication.
        """
        today = date.today()

        # Fast path: already authenticated today
        if not force and self._api and self._token_date == today:
            return self._api

        # Always try loading token from file first — even when force=True.
        # This prevents race conditions when multiple processes (scanner +
        # updaters) detect a day-change simultaneously: only the first
        # process to authenticate writes a fresh token; the rest pick it
        # up from the shared file instead of each generating (and
        # mutually invalidating) their own tokens via TOTP.
        token, token_dt = self._load_token()
        if token and token_dt == today and self._verify_token(token):
            self._api = self._build_model(token)
            self._token_date = today
            return self._api

        # read_only mode: updaters must wait for scanner to write a fresh token
        if read_only:
            raise RuntimeError(
                f"No valid token for {today} in {TOKEN_FILE} "
                f"(file date: {token_dt}) — waiting for scanner to refresh"
            )

        # Full login (only when file has no valid token for today)
        self._api = self._authenticate()
        self._token_date = today
        return self._api

    @property
    def api(self) -> fyersModel.FyersModel:
        """Shortcut – ensures session before returning the model."""
        return self.ensure_session()

    # ── token persistence ──────────────────────────────────────────────────────

    @staticmethod
    def _save_token(token: str) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": token,
            "date": date.today().isoformat(),
        }
        TOKEN_FILE.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _load_token() -> Tuple[str | None, date | None]:
        """Return (token, date) or (None, None)."""
        if not TOKEN_FILE.exists():
            return None, None
        try:
            blob = json.loads(TOKEN_FILE.read_text())
            return blob.get("access_token"), date.fromisoformat(blob.get("date", ""))
        except Exception:
            return None, None

    def _verify_token(self, token: str) -> bool:
        try:
            m = self._build_model(token)
            return m.get_profile().get("s") == "ok"
        except Exception:
            return False

    def _build_model(self, token: str) -> fyersModel.FyersModel:
        return fyersModel.FyersModel(
            client_id=self.CLIENT_ID,
            is_async=False,
            token=token,
            log_path=os.getcwd(),
        )

    # ── full TOTP login ────────────────────────────────────────────────────────

    def _authenticate(self) -> fyersModel.FyersModel:
        url_otp    = f"{self.BASE_URL}/send_login_otp"
        url_verify = f"{self.BASE_URL}/verify_otp"
        url_pin    = f"{self.BASE_URL}/verify_pin"
        url_token  = f"{self.BASE_URL_2}/token"

        # 1. send OTP
        r1 = requests.post(url_otp, json={"fy_id": self.FY_ID, "app_id": self.APP_ID}).json()
        rk = r1["request_key"]

        # 2. TOTP
        if datetime.now().second % 30 > 27:
            sleep(5)
        totp = pyotp.TOTP(self.TOTP_KEY).now()

        # 3. verify OTP
        r2 = requests.post(url_verify, json={"request_key": rk, "otp": totp}).json()
        rk = r2["request_key"]

        # 4. verify PIN
        ses = requests.Session()
        r3 = ses.post(url_pin, json={
            "request_key": rk, "identity_type": "pin", "identifier": self.PIN
        }).json()
        ses.headers.update({"authorization": f"Bearer {r3['data']['access_token']}"})

        # 5. authorization code
        payload = {
            "fyers_id": self.FY_ID, "app_id": self.APP_ID,
            "redirect_uri": self.REDIRECT_URI, "appType": self.APP_TYPE,
            "code_challenge": "", "state": self.STATE, "scope": "",
            "nonce": "", "response_type": self.RESPONSE_TYPE, "create_cookie": True,
        }
        r4 = ses.post(url_token, json=payload).json()
        if "Url" in r4:
            auth_code = parse_qs(urlparse(r4["Url"]).query)["auth_code"][0]
        else:
            auth_code = r4["data"]["auth"]

        # 6. final token
        session = fyersModel.SessionModel(
            client_id=self.CLIENT_ID, secret_key=self.SECRET_KEY,
            redirect_uri=self.REDIRECT_URI, response_type=self.RESPONSE_TYPE,
            grant_type=self.GRANT_TYPE,
        )
        session.set_token(auth_code)
        resp = session.generate_token()
        if resp.get("s") == "ERROR":
            raise RuntimeError(f"Token generation failed: {resp}")

        token = resp["access_token"]
        self._save_token(token)
        return self._build_model(token)

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING                                                                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def buy(self, symbol: str, qty: int, product_type: str = "MARGIN"):
        payload = asdict(replace(
            self._buy_tpl, symbol=symbol, qty=qty,
            productType=product_type, orderTag=symbol.split(":")[1],
        ))
        return self.api.place_order(data=payload)

    def sell(self, symbol: str, qty: int, product_type: str = "MARGIN"):
        payload = asdict(replace(
            self._sell_tpl, symbol=symbol, qty=int(qty),
            productType=product_type, orderTag=symbol.split(":")[1],
        ))
        return self.api.place_order(data=payload)

    def position(self) -> Tuple[pd.DataFrame, OverallPosition]:
        resp = self.api.positions()
        try:
            rows = resp["netPositions"]
            overall = OverallPosition(**resp["overall"])
        except KeyError:
            rows, overall = [], OverallPosition(0, 0, 0.0, 0.0, 0.0)
        return pd.DataFrame(rows, columns=POSITION_COL), overall

    def tradebook(self) -> pd.DataFrame:
        resp = self.api.tradebook()
        return pd.DataFrame(resp.get("tradeBook", []), columns=TRADE_COLS)

    def orderbook(self) -> pd.DataFrame:
        resp = self.api.orderbook()
        return pd.DataFrame(resp.get("orderBook", []), columns=ORDER_COLS)

    def funds(self) -> dict:
        return self.api.funds()

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  MARKET-DATA DOWNLOADS  (cached daily on C: drive)                       ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def download_option_data() -> pd.DataFrame:
        """Download NSE F&O symbol CSV once per day; cache to C:/AlgoTrading_Cache."""
        today = datetime.now().strftime("%Y%m%d")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"fyers_options_{today}.csv"

        if cache_file.exists():
            df = pd.read_csv(cache_file)
        else:
            df = pd.read_csv(NSE_FO_URL, header=None)
            df.columns = SYMBOLS_COLS
            df = df.drop_duplicates()
            df.to_csv(cache_file, index=False)
            # cleanup stale files
            for f in CACHE_DIR.glob("fyers_options_*.csv"):
                if today not in f.name:
                    f.unlink(missing_ok=True)
        return df

    @staticmethod
    def download_mcx_data() -> pd.DataFrame:
        """Download MCX commodity symbol CSV once per day; cache to C:/AlgoTrading_Cache."""
        today = datetime.now().strftime("%Y%m%d")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"fyers_mcx_{today}.csv"

        if cache_file.exists():
            df = pd.read_csv(cache_file)
        else:
            df = pd.read_csv(MCX_COM_URL, header=None)
            df.columns = SYMBOLS_COLS
            df = df.drop_duplicates()
            df.to_csv(cache_file, index=False)
            for f in CACHE_DIR.glob("fyers_mcx_*.csv"):
                if today not in f.name:
                    f.unlink(missing_ok=True)
        return df

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  OPTION-PAIR SCANNER  (ported from app_fyers_strategy.fetch_option_data) ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def fetch_option_pair(
        self,
        symbol: str,
        asset_type: str = "INDEX",
        expiry_mode: str = "AUTO",
        max_retries: int = 3,
        retry_delay: int = 5,
        min_trend_score: float = 0.65,
        max_expiry_days: int = 365,
        min_days_to_expiry: int = 14,
        min_oi_threshold: int = 10000,
        max_premium_per_lot: int = 55000,
        prefer_itm_otm: str = "SLIGHT_OTM",
    ) -> dict:
        """
        Scan the option chain for *symbol* and return the best CE/PE pair.

        Returns a dict with keys:
            Recommended, CE_Symbol, PE_Symbol, CE_Strike, PE_Strike,
            Expiry, Trend_Score, qty (set later), ...
        """
        if asset_type == "COMMODITY":
            min_trend_score = 0.35
            # Commodity options have much lower OI and volume than index
            # options — relax filters to avoid filtering out all rows.
            min_oi_threshold = min(min_oi_threshold, 500)
            min_days_to_expiry = min(min_days_to_expiry, 3)

        # ── helpers ────────────────────────────────────────────────────────────
        def _numeric(df, cols):
            for c in cols:
                df[c] = pd.to_numeric(df.get(c, np.nan), errors="coerce")
            return df

        def _normalize(series):
            s = pd.to_numeric(series.fillna(0), errors="coerce")
            mx = s.max()
            if not mx or np.isnan(mx):
                return pd.Series(0, index=s.index)
            return (s / mx).clip(0, 1)

        def _safe_div(num, denom, default=0):
            with np.errstate(divide="ignore", invalid="ignore"):
                result = np.where(denom != 0, num / denom, default)
            return np.nan_to_num(result, nan=default)

        def _bid_ask_eff(bid, ask):
            spread = ask - bid
            mid = (ask + bid) / 2
            return 1 - np.clip(_safe_div(spread, mid, 1.0), 0, 1)

        def _affordability(ltp, budget):
            if ltp <= 0 or budget <= 0:
                return 0.5
            r = ltp / budget
            if r > 1.0:   return 0.0
            if r < 0.1:   return 0.3
            if r < 0.3:   return 1.0
            if r < 0.6:   return 0.8
            return 0.5

        def _theta_score(days, _iv):
            if days < 7:   return 0.1
            if days < 14:  return 0.3
            if days < 21:  return 0.6
            if days <= 45: return 1.0
            if days <= 60: return 0.85
            return 0.7

        def _moneyness(strike, price, opt_type, pref):
            """Score how well-placed the strike is for BUYING the option.

            For a BUY-only strategy, slight ITM or ATM options have the
            best delta (premium responds strongly to underlying moves)
            while still being affordable.  Deep OTM is cheap but delta is
            too low; deep ITM is expensive with diminishing gamma.
            """
            m = (strike - price) / price if opt_type == "CE" else (price - strike) / price
            if pref == "ATM":
                return float(np.exp(-abs(m) * 50))
            if pref == "SLIGHT_OTM":
                # Slightly OTM → best balance of delta + affordability for BUY
                if 0.005 <= m <= 0.02:  return 1.0   # sweet spot
                if 0.0   <= m <= 0.04:  return 0.85   # near ATM / moderate OTM
                if -0.01  <= m < 0:     return 0.75   # slight ITM (good delta)
                if -0.03  <= m < -0.01: return 0.55   # deeper ITM
                if 0.04  <  m <= 0.07:  return 0.4    # far OTM (low delta)
                return 0.2
            # OTM
            if 0.02 <= m <= 0.05: return 1.0
            if 0.01 <= m <= 0.07: return 0.7
            return 0.4

        # ── retry loop ─────────────────────────────────────────────────────────
        _log = []  # collect debug breadcrumbs

        for attempt in range(1, max_retries + 1):
            try:
                # ─── 1. Get underlying price ──────────────────────────────
                quote = self.api.quotes(data={"symbols": symbol})
                if not isinstance(quote, dict) or quote.get("s") != "ok" or "d" not in quote:
                    err_msg = quote.get("message", quote.get("s", "unknown")) if isinstance(quote, dict) else str(quote)[:100]
                    raise ValueError(f"Quotes API error for {symbol}: {err_msg}")
                current_price = quote["d"][0]["v"].get("lp")
                if not current_price:
                    raise ValueError(f"Underlying price unavailable for {symbol}")
                _log.append(f"price={current_price}")

                # ─── 2. Base option chain (expiry list + VIX) ─────────────
                base_chain = self.api.optionchain(data={"symbol": symbol, "strikecount": 20})
                if not isinstance(base_chain, dict) or base_chain.get("s") != "ok":
                    err_msg = base_chain.get("message", base_chain.get("s", "unknown")) if isinstance(base_chain, dict) else str(base_chain)[:100]
                    raise ValueError(f"OptionChain API error for {symbol}: {err_msg}")
                data = base_chain.get("data", {})
                vix = data.get("indiavixData", {}).get("ltp", 20)
                _log.append(f"VIX={vix}")

                expiry_map = {e["date"]: e["expiry"] for e in data.get("expiryData", [])}
                parsed = sorted(
                    [(datetime.strptime(k, "%d-%m-%Y"), v) for k, v in expiry_map.items()],
                    key=lambda x: x[0],
                )
                cutoff = datetime.now() + timedelta(days=max_expiry_days)
                parsed = [(d, e) for d, e in parsed if d <= cutoff]
                _log.append(f"expiries_total={len(parsed)}")

                if expiry_mode == "NEAR_MONTH":
                    expiry_list = parsed[:1]
                elif expiry_mode == "NEXT_MONTH":
                    expiry_list = parsed[1:2]
                else:
                    expiry_list = [
                        (d, e) for d, e in parsed
                        if min_days_to_expiry <= (d - datetime.now()).days <= 60
                    ] or parsed
                _log.append(f"expiries_filtered={len(expiry_list)}")

                best_ce_score, best_pe_score = -1.0, -1.0
                best_ce, best_pe = None, None
                best_exp_info = None

                # ─── 3. Loop through each expiry ─────────────────────────
                for exp_date, exp_epoch in expiry_list:
                    dte = max((exp_date - datetime.now()).days, 0)
                    if dte < min_days_to_expiry:
                        _log.append(f"skip_exp={exp_date.strftime('%d%b')} dte={dte}<{min_days_to_expiry}")
                        continue

                    oc = self.api.optionchain(data={
                        "symbol": symbol, "strikecount": 20,
                        "timestamp": str(exp_epoch),
                    })
                    chain = oc.get("data", {}).get("optionsChain", [])
                    df = pd.DataFrame(chain)
                    if df.empty:
                        _log.append(f"exp={exp_date.strftime('%d%b')} chain=EMPTY")
                        continue

                    df = df[df["option_type"].isin(["CE", "PE"])]
                    if df.empty:
                        _log.append(f"exp={exp_date.strftime('%d%b')} CE+PE=0")
                        continue

                    df = _numeric(df, [
                        "strike_price", "oi", "prev_oi", "volume",
                        "ask", "bid", "ltp", "iv", "chng", "chng_oi",
                    ])
                    rows_before = len(df)
                    _ltp_floor = 1 if asset_type == "COMMODITY" else 5
                    _vol_floor = 10 if asset_type == "COMMODITY" else 100
                    df = df[(df["ltp"] <= max_premium_per_lot) & (df["ltp"] > _ltp_floor)]
                    df = df[(df["oi"] >= min_oi_threshold) | (df["volume"] > _vol_floor)]
                    if df.empty:
                        _log.append(f"exp={exp_date.strftime('%d%b')} rows={rows_before}->0(filtered)")
                        continue

                    _log.append(f"exp={exp_date.strftime('%d%b')} dte={dte} rows={len(df)}")

                    # ─── 4. Score CE and PE separately ────────────────────
                    # Scoring is optimised for a BUY-only strategy:
                    #   • Moneyness: slight OTM / ATM for best delta
                    #   • OI buildup: fresh positions = demand for this strike
                    #   • Price momentum: options already gaining value
                    #   • Liquidity: tight spread, decent volume
                    for otype in ["CE", "PE"]:
                        sub = df[df["option_type"] == otype].copy()
                        if sub.empty:
                            _log.append(f"  {otype}=0rows")
                            continue

                        # OI buildup (positive change = fresh longs being built)
                        oi_change = (sub["oi"] - sub["prev_oi"]).fillna(0)
                        # Also use chng_oi if available (more reliable)
                        if "chng_oi" in sub.columns:
                            chng_oi = sub["chng_oi"].fillna(0)
                            oi_change = np.maximum(oi_change, chng_oi)

                        # Price momentum: positive chng means option premium is
                        # rising — good for buying (confirms underlying trend)
                        price_momentum = pd.Series(0.5, index=sub.index)
                        if "chng" in sub.columns:
                            chng = sub["chng"].fillna(0)
                            # Positive price change → higher score (BUY likes rising premiums)
                            price_momentum = np.where(chng > 0, np.clip(0.5 + chng / (sub["ltp"] * 0.1 + 1e-9), 0.5, 1.0),
                                                      np.clip(0.5 + chng / (sub["ltp"] * 0.1 + 1e-9), 0.1, 0.5))
                            price_momentum = pd.Series(price_momentum, index=sub.index)

                        sub["score"] = (
                            sub["strike_price"].apply(lambda x: _moneyness(x, current_price, otype, prefer_itm_otm)) * 0.25
                            + _bid_ask_eff(sub["bid"].fillna(0), sub["ask"].fillna(0)) * 0.15
                            + sub["ltp"].apply(lambda x: _affordability(x, max_premium_per_lot)) * 0.15
                            + sub["iv"].apply(lambda iv: _theta_score(dte, iv)) * 0.10
                            + _normalize(oi_change.clip(lower=0)) * 0.15
                            + price_momentum * 0.10
                            + _normalize(np.log1p(sub["volume"])) * 0.10
                        ).clip(0, 1)
                        idx = sub["score"].idxmax()
                        sc = sub.loc[idx, "score"]
                        _log.append(f"  {otype}: best={sc:.3f} sym={sub.loc[idx, 'symbol']} strike={sub.loc[idx, 'strike_price']}")
                        if otype == "CE" and sc > best_ce_score:
                            best_ce_score = sc
                            best_ce = sub.loc[idx]
                            best_exp_info = (exp_date, dte, vix)
                        elif otype == "PE" and sc > best_pe_score:
                            best_pe_score = sc
                            best_pe = sub.loc[idx]
                            if best_exp_info is None:
                                best_exp_info = (exp_date, dte, vix)

                # ─── 5. Final decision ────────────────────────────────────
                combined = (
                    (best_ce_score + best_pe_score) / 2
                    if best_ce is not None and best_pe is not None
                    else max(best_ce_score, best_pe_score)
                )
                _log.append(f"combined={combined:.3f} threshold={min_trend_score}")

                if combined < min_trend_score or (best_ce is None and best_pe is None):
                    msg = f"No suitable options (CE={best_ce_score:.3f}, PE={best_pe_score:.3f})"
                    return {"Recommended": False, "Symbol": symbol,
                            "Message": msg, "Debug": " | ".join(_log)}

                # BOTH CE and PE must be found for a valid pair
                if best_ce is None or best_pe is None:
                    missing = "CE" if best_ce is None else "PE"
                    msg = f"Only one side found ({missing} missing, CE={best_ce_score:.3f}, PE={best_pe_score:.3f})"
                    return {"Recommended": False, "Symbol": symbol,
                            "Message": msg, "Debug": " | ".join(_log)}

                exp_date, dte, vix = best_exp_info
                _log.append(f"SELECTED CE={best_ce['symbol']} PE={best_pe['symbol']}")
                return {
                    "Recommended": True,
                    "CE_Symbol": best_ce["symbol"],
                    "PE_Symbol": best_pe["symbol"],
                    "CE_Strike": float(best_ce["strike_price"]),
                    "PE_Strike": float(best_pe["strike_price"]),
                    "CE_Premium": float(best_ce["ltp"]),
                    "PE_Premium": float(best_pe["ltp"]),
                    "Expiry": exp_date.strftime("%Y-%m-%d"),
                    "Days_To_Expiry": dte,
                    "Trend_Score": float(combined),
                    "VIX": float(vix),
                    "Debug": " | ".join(_log),
                }

            except Exception as e:
                _log.append(f"attempt{attempt}_err: {e}")
                if attempt == max_retries:
                    return {"Recommended": False, "Symbol": symbol,
                            "Message": f"Failed after {max_retries} attempts: {e}",
                            "Debug": " | ".join(_log)}
                _time.sleep(retry_delay)

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HELPERS — lot size, commodity resolver, position checks                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def get_lot_size(ce_symbol: str, symbol_df: pd.DataFrame) -> int:
        """Look up 'Minimum lot size' for *ce_symbol* in the downloaded CSV DataFrame."""
        row = symbol_df[symbol_df["Symbol ticker"] == ce_symbol]
        if row.empty:
            raise ValueError(f"Lot size not found for {ce_symbol}")
        return int(row["Minimum lot size"].iloc[0])

    @staticmethod
    def resolve_commodity_symbol(generic_name: str, mcx_df: pd.DataFrame) -> str | None:
        """
        Resolve a generic commodity name (e.g. 'SILVERM') to the nearest
        active (non-expired) MCX futures symbol (e.g. 'MCX:SILVERM26APRFUT').
        """
        # Use 'Underlying symbol' column for exact match to avoid SILVER
        # matching SILVERM, SILVERMIC, etc.
        name_upper = generic_name.upper()
        futures = mcx_df[
            (mcx_df["Underlying symbol"].str.upper() == name_upper)
            & mcx_df["Symbol ticker"].str.contains("FUT", case=False, na=False)
        ].copy()

        print(f"[DEBUG resolve_commodity] generic_name={generic_name!r}, "
              f"matched_futures={len(futures)} rows")
        if not futures.empty:
            print(f"[DEBUG resolve_commodity] tickers: "
                  f"{futures['Symbol ticker'].tolist()}")
            print(f"[DEBUG resolve_commodity] expiry_dates: "
                  f"{futures['Expiry date'].tolist()}")

        if futures.empty:
            print(f"[DEBUG resolve_commodity] No futures found for {name_upper}. "
                  f"Available underlying symbols: "
                  f"{mcx_df['Underlying symbol'].dropna().unique()[:20].tolist()}")
            return None

        # Pick the nearest non-expired contract by expiry date
        today_str = datetime.now().strftime("%Y-%m-%d")
        futures = futures[futures["Expiry date"] >= today_str]
        if futures.empty:
            print(f"[DEBUG resolve_commodity] All {name_upper} futures expired")
            return None

        futures = futures.sort_values("Expiry date")
        chosen = futures.iloc[0]["Symbol ticker"]
        print(f"[DEBUG resolve_commodity] Chosen: {chosen}")
        return chosen

    @staticmethod
    def has_index_positions(position_df: pd.DataFrame) -> bool:
        """True if any open NSE/NFO position with non-zero qty exists."""
        if position_df.empty:
            return False
        for _, row in position_df.iterrows():
            sym = str(row.get("symbol", "")).upper()
            if row.get("qty", 0) != 0 and ("NSE" in sym or "NFO" in sym) and "MCX" not in sym:
                return True
        return False

    @staticmethod
    def has_commodity_positions(position_df: pd.DataFrame) -> bool:
        """True if any open MCX position with non-zero qty exists."""
        if position_df.empty:
            return False
        for _, row in position_df.iterrows():
            if row.get("qty", 0) != 0 and "MCX" in str(row.get("symbol", "")).upper():
                return True
        return False

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING HOLIDAYS — fetched annually, cached on C: drive                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    # Market hours by asset type
    MARKET_HOURS = {
        "INDEX":     (dt_time(9, 15), dt_time(15, 30)),
        "COMMODITY": (dt_time(9, 0),  dt_time(23, 30)),
    }

    @staticmethod
    def fetch_trading_holidays(year: int | None = None) -> dict:
        """
        Fetch Indian trading holidays for *year* from NSE and cache to JSON.

        Returns dict::

            {
                "year": 2026,
                "fetched_on": "2026-01-15",
                "holidays": ["2026-01-26", ...],
                "special_sessions": [
                    {"date": "2026-02-01", "name": "Budget Day",
                     "open": "09:15", "close": "15:30"},
                    {"date": "2026-10-20", "name": "Diwali Muhurat",
                     "open": "18:15", "close": "19:30"},
                ]
            }
        """
        if year is None:
            year = date.today().year

        cache_file = CACHE_DIR / f"trading_holidays_{year}.json"

        # ── return from cache if already fetched this year ─────────────────
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                if cached.get("year") == year:
                    return cached
            except Exception:
                pass  # re-fetch if cache corrupt

        holidays_data: dict = {
            "year": year,
            "fetched_on": date.today().isoformat(),
            "holidays": [],
            "special_sessions": [],
        }

        # ── attempt NSE holiday-master API ─────────────────────────────────
        try:
            ses = requests.Session()
            ses.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
            })
            # warm-up: NSE requires cookies from homepage first
            ses.get("https://www.nseindia.com", timeout=10)

            resp = ses.get(
                "https://www.nseindia.com/api/holiday-master?type=trading",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                # NSE returns {"CM": [...], "FO": [...], "CD": [...]}
                # Use FO (F&O) holidays — superset that covers equity too
                fo_holidays = data.get("FO", data.get("CM", []))
                holiday_dates: set[str] = set()
                for h in fo_holidays:
                    try:
                        dt_val = datetime.strptime(h["tradingDate"], "%d-%b-%Y")
                        if dt_val.year == year:
                            holiday_dates.add(dt_val.strftime("%Y-%m-%d"))
                    except (KeyError, ValueError):
                        continue
                holidays_data["holidays"] = sorted(holiday_dates)
        except Exception:
            pass  # fall back to known holidays

        # ── fallback: well-known fixed + approximate lunar holidays ────────
        if not holidays_data["holidays"]:
            holidays_data["holidays"] = Fyers._known_fixed_holidays(year)

        # ── special / muhurat sessions ─────────────────────────────────────
        holidays_data["special_sessions"] = Fyers._known_special_sessions(year)

        # ── persist to cache ───────────────────────────────────────────────
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(holidays_data, f, indent=2)
        return holidays_data

    @staticmethod
    def _known_fixed_holidays(year: int) -> list[str]:
        """
        Return a best-effort list of Indian market holidays.
        Fixed-date holidays are exact; lunar holidays are approximate.
        The NSE API should be preferred — this is the fallback.
        """
        # Fixed-date holidays (always the same calendar date)
        fixed = [
            f"{year}-01-26",  # Republic Day
            f"{year}-05-01",  # Maharashtra Day / May Day
            f"{year}-08-15",  # Independence Day
            f"{year}-10-02",  # Mahatma Gandhi Jayanti
            f"{year}-12-25",  # Christmas
        ]
        # Approximate lunar / gazetted holidays (may shift ±1-2 days)
        approx = [
            f"{year}-03-14",  # Holi (approx)
            f"{year}-03-31",  # Id-Ul-Fitr / Ramadan (approx)
            f"{year}-04-06",  # Ram Navami (approx)
            f"{year}-04-10",  # Mahavir Jayanti (approx)
            f"{year}-04-14",  # Dr. Ambedkar Jayanti
            f"{year}-04-18",  # Good Friday (approx)
            f"{year}-05-12",  # Buddha Purnima (approx)
            f"{year}-06-07",  # Bakri Id / Eid Al-Adha (approx)
            f"{year}-07-06",  # Muharram (approx)
            f"{year}-08-16",  # Parsi New Year (approx)
            f"{year}-09-05",  # Milad-Un-Nabi (approx)
            f"{year}-10-02",  # Gandhi Jayanti
            f"{year}-10-21",  # Diwali / Laxmi Puja (approx)
            f"{year}-10-22",  # Diwali Balipratipada (approx)
            f"{year}-11-05",  # Guru Nanak Jayanti (approx)
        ]
        # De-duplicate and filter to valid weekdays (markets already closed on weekends)
        combined = sorted(set(fixed + approx))
        valid = []
        for d_str in combined:
            try:
                dt_val = datetime.strptime(d_str, "%Y-%m-%d")
                if dt_val.weekday() < 5:  # skip Sat/Sun
                    valid.append(d_str)
            except ValueError:
                continue
        return valid

    @staticmethod
    def _known_special_sessions(year: int) -> list[dict]:
        """
        Return known special trading sessions (Budget day, Diwali Muhurat, etc.).
        Dates are approximate — callers should verify against NSE circulars.
        """
        sessions = []
        # Budget Day — first weekday of February
        feb1 = date(year, 2, 1)
        # If Feb 1 falls on Saturday → no special session (regular Monday)
        # If Feb 1 falls on Sunday  → Monday Feb 2 is normal session
        # If Feb 1 is a weekday, market is open normally anyway;
        # but if it falls on Saturday, NSE sometimes opens a special session.
        if feb1.weekday() == 5:  # Saturday
            sessions.append({
                "date": feb1.isoformat(),
                "name": "Budget Day (Special Saturday)",
                "open": "09:15",
                "close": "15:30",
            })

        # Diwali Muhurat Trading — approx late October / early November
        # Exact date changes yearly; this is an approximation.
        muhurat_date = date(year, 10, 21)
        # Adjust to nearest weekday if needed (muhurat can be any day)
        sessions.append({
            "date": muhurat_date.isoformat(),
            "name": "Diwali Muhurat Trading",
            "open": "18:15",
            "close": "19:30",
        })

        return sessions

    @staticmethod
    def load_holiday_set(year: int | None = None) -> tuple[set[str], list[dict]]:
        """
        Convenience: return (holiday_dates_set, special_sessions_list)
        from the cached JSON (fetches if not cached).
        """
        data = Fyers.fetch_trading_holidays(year)
        return set(data.get("holidays", [])), data.get("special_sessions", [])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HISTORICAL DATA FETCH — with market-hours & holiday awareness           ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    # Timeframe → (resolution, timedelta, category)
    _TF_MAP = {
        "5S":  ("5S",  timedelta(seconds=5),  "intraday"),
        "10S": ("10S", timedelta(seconds=10), "intraday"),
        "15S": ("15S", timedelta(seconds=15), "intraday"),
        "30S": ("30S", timedelta(seconds=30), "intraday"),
        "45S": ("45S", timedelta(seconds=45), "intraday"),
        "1":   ("1",   timedelta(minutes=1),  "intraday"),
        "2":   ("2",   timedelta(minutes=2),  "intraday"),
        "3":   ("3",   timedelta(minutes=3),  "intraday"),
        "5":   ("5",   timedelta(minutes=5),  "intraday"),
        "10":  ("10",  timedelta(minutes=10), "intraday"),
        "15":  ("15",  timedelta(minutes=15), "intraday"),
        "20":  ("20",  timedelta(minutes=20), "intraday"),
        "30":  ("30",  timedelta(minutes=30), "intraday"),
        "60":  ("60",  timedelta(hours=1),    "intraday"),
        "120": ("120", timedelta(hours=2),     "intraday"),
        "240": ("240", timedelta(hours=4),     "intraday"),
        "D":   ("D",   timedelta(days=1),      "daily"),
    }

    def fetch_historical_data(
        self,
        symbol: str,
        timeframe: str,
        candles: int = 100,
        market_type: str = "INDEX",
        holidays: set[str] | None = None,
        special_sessions: list[dict] | None = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles from Fyers for *symbol*.

        Timing logic
        ────────────
        • INDEX   : 9:15 AM → 3:30 PM  Mon–Fri, skip NSE holidays
        • COMMODITY: 9:00 AM → 11:30 PM Mon–Fri, skip NSE holidays

        When walking backwards to fill *candles*, weekends AND holidays
        are skipped.  Special sessions (Budget Saturday, Diwali Muhurat)
        are treated as valid trading days.

        Returns
        ───────
        DataFrame with columns: Timestamp, Open, High, Low, Close, Volume
        """
        if timeframe not in self._TF_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        resolution, delta, _ = self._TF_MAP[timeframe]

        if holidays is None:
            holidays = set()
        if special_sessions is None:
            special_sessions = []

        # Build a fast-lookup: date-str → (open, close) for special sessions
        special_map: dict[str, tuple[dt_time, dt_time]] = {}
        for ss in special_sessions:
            try:
                ss_date = ss["date"]  # "YYYY-MM-DD"
                ss_open = dt_time(*map(int, ss["open"].split(":")))
                ss_close = dt_time(*map(int, ss["close"].split(":")))
                special_map[ss_date] = (ss_open, ss_close)
            except (KeyError, ValueError):
                continue

        # ── resolve market open / close for this asset type ────────────────
        mkt_open, mkt_close = self.MARKET_HOURS.get(
            market_type.upper(), self.MARKET_HOURS["INDEX"]
        )

        def _is_trading_day(d: date) -> bool:
            """True if *d* is a valid trading day (not weekend, not holiday, OR special session)."""
            d_str = d.isoformat()
            if d_str in special_map:
                return True  # special session overrides holiday
            if d.weekday() >= 5:  # Sat / Sun
                return False
            if d_str in holidays:
                return False
            return True

        def _market_times(d: date) -> tuple[dt_time, dt_time]:
            """Return (open, close) for a specific date, honouring special sessions."""
            d_str = d.isoformat()
            if d_str in special_map:
                return special_map[d_str]
            return mkt_open, mkt_close

        def _prev_trading_day(d: date) -> date:
            """Walk backwards to find the most recent trading day before *d*."""
            d = d - timedelta(days=1)
            while not _is_trading_day(d):
                d = d - timedelta(days=1)
            return d

        # ── determine end_dt ───────────────────────────────────────────────
        now = datetime.now()
        today_d = now.date()
        current_time = now.time()
        today_open, today_close = _market_times(today_d)

        if not _is_trading_day(today_d) or current_time < today_open:
            # Before market or non-trading day → use previous trading day's close
            ltd = _prev_trading_day(today_d)
            _, ltd_close = _market_times(ltd)
            end_dt = datetime.combine(ltd, ltd_close)
        elif current_time > today_close:
            # After today's close
            end_dt = datetime.combine(today_d, today_close)
        else:
            # During market hours
            end_dt = now

        # ── walk backwards to compute start_dt ─────────────────────────────
        start_dt = end_dt
        candles_remaining = candles

        while candles_remaining > 0:
            start_dt = start_dt - delta

            # If we've gone before this day's market open, jump to prev day
            day_open, _ = _market_times(start_dt.date())
            if start_dt.time() < day_open:
                prev_d = _prev_trading_day(start_dt.date())
                _, prev_close = _market_times(prev_d)
                start_dt = datetime.combine(prev_d, prev_close)

            candles_remaining -= 1

        # ── call Fyers history API ─────────────────────────────────────────
        # Options (CE/PE) must use cont_flag=0 (specific contract data).
        # cont_flag=1 (continuous/rolled) returns the underlying futures
        # series instead of the actual option prices.
        sym_name = symbol.split(":")[-1] if ":" in symbol else symbol
        is_option = sym_name.upper().endswith("CE") or sym_name.upper().endswith("PE")

        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "0",  # Unix timestamps
            "range_from": str(int(start_dt.timestamp())),
            "range_to": str(int(end_dt.timestamp())),
            "cont_flag": "0" if is_option else "1",
        }

        resp = self.api.history(data=payload)
        response_status = resp.get("s") if resp else None

        if not resp or response_status not in ("ok", "no_data"):
            msg = resp.get("message", "Unknown error") if resp else "No response"
            raise ValueError(f"Fyers API error for {symbol}: {msg}")

        if response_status == "no_data":
            raise ValueError(f"No historical data for {symbol}")

        candles_data = resp.get("candles", [])
        if not candles_data:
            raise ValueError(f"No candle data returned for {symbol}")

        df = pd.DataFrame(
            candles_data,
            columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"],
        )
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")

        # ── Data quality: sort ascending + deduplicate ────────────────
        # The Fyers API normally returns candles in ascending order, but
        # edge cases (rate limits, server glitches) can produce out-of-order
        # or duplicate entries.  Both SHA (Heiken-Ashi recursive) and RSI
        # (diff-based) are extremely sensitive to ordering — reversed data
        # causes SHA candle directions to invert and RSI to be off by 60+
        # points.  Defensive sort + dedup prevents this.
        was_unsorted = not df["Timestamp"].is_monotonic_increasing
        n_before = len(df)
        df = df.sort_values("Timestamp").drop_duplicates(
            subset="Timestamp", keep="last"
        ).reset_index(drop=True)
        n_dupes = n_before - len(df)

        # Attach diagnostics so callers can log data quality issues
        df.attrs["_data_quality"] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "was_unsorted": was_unsorted,
            "duplicates_removed": n_dupes,
            "candles_returned": len(df),
            "candles_requested": candles,
        }

        return df
