"""Market data retrieval: security search, option chains, and price snapshots.

Uses the IB Client Portal Gateway REST API endpoints for market data
that are separate from the portfolio endpoints used by portfolio.py.
"""

import logging
import time
from datetime import datetime

from .http_client import IBHttpClient
from .config import get_account_config, get_all_account_names

logger = logging.getLogger("ib-connect")

# Market data field IDs for IB Client Portal Gateway API.
# Options greeks field IDs (7308-7317) may vary by gateway version.
# The code handles missing fields gracefully.
SNAPSHOT_FIELDS = {
    "last": "31",
    "bid": "84",
    "ask": "86",
    "ask_size": "85",
    "bid_size": "87",
    "volume": "88",
    "open_interest": "7295",
    "implied_vol": "7308",
    "delta": "7310",
    "gamma": "7311",
    "theta": "7312",
    "vega": "7313",
    "underlying_price": "7317",
}

ALL_FIELD_IDS = ",".join(SNAPSHOT_FIELDS.values())

# Well-known inverse ETFs with annual expense ratios (as of 2025).
# Used for cost estimation. Verify current ratios before trading.
INVERSE_ETF_INFO = {
    "SH":   {"description": "ProShares Short S&P500",          "leverage": -1, "expense_ratio": 0.88, "index": "S&P 500"},
    "SDS":  {"description": "ProShares UltraShort S&P500",     "leverage": -2, "expense_ratio": 0.90, "index": "S&P 500"},
    "SPXU": {"description": "ProShares UltraPro Short S&P500", "leverage": -3, "expense_ratio": 0.90, "index": "S&P 500"},
    "PSQ":  {"description": "ProShares Short QQQ",             "leverage": -1, "expense_ratio": 0.95, "index": "Nasdaq-100"},
    "QID":  {"description": "ProShares UltraShort QQQ",        "leverage": -2, "expense_ratio": 0.95, "index": "Nasdaq-100"},
    "SQQQ": {"description": "ProShares UltraPro Short QQQ",    "leverage": -3, "expense_ratio": 0.95, "index": "Nasdaq-100"},
    "SOXS": {"description": "Direxion Daily Semiconductor Bear 3X", "leverage": -3, "expense_ratio": 1.01, "index": "ICE Semiconductor"},
    "TBT":  {"description": "ProShares UltraShort 20+ Year Treasury", "leverage": -2, "expense_ratio": 0.90, "index": "20+ Year Treasury"},
    "TBF":  {"description": "ProShares Short 20+ Year Treasury", "leverage": -1, "expense_ratio": 0.90, "index": "20+ Year Treasury"},
    "ERY":  {"description": "Direxion Daily Energy Bear 2X",   "leverage": -2, "expense_ratio": 1.07, "index": "Energy Select Sector"},
    "EUM":  {"description": "ProShares Short MSCI Emerging Markets", "leverage": -1, "expense_ratio": 0.95, "index": "MSCI Emerging Markets"},
    "DOG":  {"description": "ProShares Short Dow30",           "leverage": -1, "expense_ratio": 0.95, "index": "Dow Jones 30"},
    "RWM":  {"description": "ProShares Short Russell2000",     "leverage": -1, "expense_ratio": 0.95, "index": "Russell 2000"},
}


class MarketDataManager:
    """Retrieves market data and option chains from IB Client Portal Gateway."""

    def __init__(self, cfg: dict, http: IBHttpClient):
        self.cfg = cfg
        self.http = http
        self._conid_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Security search
    # ------------------------------------------------------------------

    def search_conid(self, symbol: str, port: int) -> dict | None:
        """Search for security by symbol.

        Returns {"conid": int, "name": str, "option_months": list[str]} or None.
        Option months are in IB format (e.g. "JUN25", "JUL25").
        """
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]

        try:
            resp = self.http.post(
                f"https://localhost:{port}/v1/api/iserver/secdef/search",
                json={"symbol": symbol},
            )
            if resp.status_code != 200:
                logger.error("secdef search failed for %s: HTTP %d", symbol, resp.status_code)
                return None

            results = resp.json()
            if not results:
                logger.warning("No results for symbol search: %s", symbol)
                return None

            # Prefer STK (stock/ETF) results over futures/index/other types.
            # IB may return futures or foreign indices first for common symbols
            # (e.g. GLD returns Gold Futures HKFE before the SPDR ETF).
            match = next(
                (r for r in results if any(
                    s.get("secType") == "STK" for s in r.get("sections", [])
                )),
                results[0],
            )
            conid = int(match.get("conid"))

            option_months = []
            for section in match.get("sections", []):
                if section.get("secType") == "OPT":
                    months_str = section.get("months", "")
                    if months_str:
                        option_months = [m.strip() for m in months_str.split(";") if m.strip()]
                    break

            info = {
                "conid": conid,
                "name": match.get("companyName", symbol),
                "option_months": option_months,
            }
            self._conid_cache[symbol] = info
            logger.info(
                "Resolved %s -> conid %s (%s), %d option months",
                symbol, conid, info["name"], len(option_months),
            )
            return info

        except Exception as e:
            logger.error("secdef search failed for %s: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    # Option strikes
    # ------------------------------------------------------------------

    def get_strikes(self, underlying_conid: int, month: str, port: int) -> dict:
        """Get available option strikes for an expiry month.

        Args:
            month: YYYYMM format (e.g. "202606").

        Returns: {"call": [float, ...], "put": [float, ...]}
        """
        try:
            resp = self.http.get(
                f"https://localhost:{port}/v1/api/iserver/secdef/strikes"
                f"?conid={underlying_conid}&sectype=OPT&month={month}",
            )
            if resp.status_code != 200:
                logger.error("strikes request failed for conid %d month %s: HTTP %d",
                             underlying_conid, month, resp.status_code)
                return {"call": [], "put": []}

            data = resp.json()
            return {
                "call": data.get("call", []),
                "put": data.get("put", []),
            }
        except Exception as e:
            logger.error("Failed to get strikes for conid %d month %s: %s",
                         underlying_conid, month, e)
            return {"call": [], "put": []}

    # ------------------------------------------------------------------
    # Option contract conid resolution
    # ------------------------------------------------------------------

    def get_option_conids(
        self,
        underlying_conid: int,
        month: str,
        strikes: list[float],
        right: str,
        port: int,
    ) -> list[dict]:
        """Get option contract conids for specific strikes.

        Args:
            month: YYYYMM format.
            right: "P" for puts, "C" for calls.

        Returns: list of {"conid": int, "strike": float, "right": str, "expiry": str}
        """
        results = []
        for strike in strikes:
            try:
                resp = self.http.get(
                    f"https://localhost:{port}/v1/api/iserver/secdef/info"
                    f"?conid={underlying_conid}&sectype=OPT&month={month}"
                    f"&right={right}&strike={strike}",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        contracts = data if isinstance(data, list) else [data]
                        for c in contracts:
                            opt_conid = c.get("conid")
                            if opt_conid:
                                results.append({
                                    "conid": opt_conid,
                                    "strike": strike,
                                    "right": right,
                                    "expiry": c.get("maturityDate", month),
                                })
                                break  # take first match per strike
                else:
                    logger.debug("secdef/info returned %d for strike %s", resp.status_code, strike)
            except Exception as e:
                logger.warning("Failed to get option conid for %s %s strike %s: %s",
                               right, month, strike, e)

        logger.info("Resolved %d/%d option conids for conid %d %s %s",
                     len(results), len(strikes), underlying_conid, right, month)
        return results

    # ------------------------------------------------------------------
    # Market data snapshots
    # ------------------------------------------------------------------

    def get_snapshot(
        self,
        conids: list[int],
        port: int,
        fields: str = ALL_FIELD_IDS,
        max_retries: int = 3,
    ) -> dict:
        """Get market data snapshot for conids. Handles subscription warm-up.

        The IB CP Gateway snapshot endpoint requires a subscription warm-up:
        the first call initiates the subscription and may return stale or
        incomplete data. Subsequent calls return live data.

        Returns: {conid: {"last": float, "bid": float, ...}, ...}
        """
        if not conids:
            return {}

        conid_str = ",".join(str(c) for c in conids)
        url = (
            f"https://localhost:{port}/v1/api/iserver/marketdata/snapshot"
            f"?conids={conid_str}&fields={fields}"
        )

        for attempt in range(max_retries):
            try:
                resp = self.http.get(url)
                if resp.status_code != 200:
                    logger.warning("Snapshot request failed: HTTP %d (attempt %d/%d)",
                                   resp.status_code, attempt + 1, max_retries)
                    time.sleep(1)
                    continue

                data = resp.json()
                if not data:
                    time.sleep(1.5)
                    continue

                # Check if we got real data (not just subscription confirmations).
                # Stale values are prefixed with "C" (closing) or "H" (halted).
                has_live = any(
                    item.get("31") is not None and not str(item.get("31", "")).startswith("C")
                    for item in data
                )

                if has_live or attempt == max_retries - 1:
                    parsed = self._parse_snapshot(data)
                    if attempt > 0:
                        logger.info("Snapshot data received after %d warm-up attempt(s)", attempt)
                    return parsed

                # Data not ready, wait and retry
                time.sleep(1.5)

            except Exception as e:
                logger.error("Snapshot request failed (attempt %d/%d): %s",
                             attempt + 1, max_retries, e)
                time.sleep(1)

        return {}

    def _parse_snapshot(self, raw_data: list) -> dict:
        """Parse IB snapshot response into structured format."""
        field_reverse = {v: k for k, v in SNAPSHOT_FIELDS.items()}
        result = {}

        for item in raw_data:
            conid = item.get("conid")
            if not conid:
                continue

            parsed = {}
            for field_id, field_name in field_reverse.items():
                raw_val = item.get(field_id)
                if raw_val is not None:
                    parsed[field_name] = self._parse_field_value(raw_val)

            # Log raw keys on first encounter for field ID verification
            if not result:
                extra_keys = [k for k in item.keys() if k not in ("conid", "server_id", "_updated")]
                logger.debug("Snapshot raw field IDs for conid %s: %s", conid, extra_keys)

            result[conid] = parsed

        return result

    @staticmethod
    def _parse_field_value(val):
        """Parse an IB field value. Handles string-encoded numbers and prefixes."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return val
        s = str(val).strip()
        # Remove prefix characters: C=closing, H=halted
        if s and s[0] in ("C", "H") and len(s) > 1:
            s = s[1:]
        try:
            return float(s)
        except (ValueError, TypeError):
            return None  # non-numeric, skip

    def unsubscribe_all(self, port: int):
        """Unsubscribe from all market data to free subscription slots."""
        try:
            self.http.get(
                f"https://localhost:{port}/v1/api/iserver/marketdata/unsubscribeall",
                rate_limit=False,
            )
            logger.info("Unsubscribed from all market data")
        except Exception as e:
            logger.warning("Failed to unsubscribe market data: %s", e)

    # ------------------------------------------------------------------
    # High-level: full option chain
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        symbol: str,
        expiry_month: str,
        right: str,
        strike_range_pct: float,
        port: int,
    ) -> dict:
        """Retrieve a filtered option chain with live greeks and pricing.

        Args:
            symbol: Underlying symbol (e.g. "SPY").
            expiry_month: YYYYMM format (e.g. "202606").
            right: "P" for puts, "C" for calls.
            strike_range_pct: Include strikes within this % of current price.
            port: Gateway port.

        Returns dict with keys:
            underlying, underlying_conid, underlying_price, expiry_month,
            right, options (list), fetch_timestamp, error (if any).
        """
        # 1. Resolve underlying conid
        sec_info = self.search_conid(symbol, port)
        if not sec_info:
            return {"error": f"Could not resolve symbol: {symbol}", "options": []}

        underlying_conid = sec_info["conid"]

        # 2. Get underlying price
        underlying_snap = self.get_snapshot([underlying_conid], port)
        underlying_price = None
        if underlying_conid in underlying_snap:
            underlying_price = underlying_snap[underlying_conid].get("last")

        if not underlying_price:
            return {
                "error": f"Could not get price for {symbol}",
                "underlying_conid": underlying_conid,
                "options": [],
            }

        # 3. Get available strikes
        strikes_data = self.get_strikes(underlying_conid, expiry_month, port)
        strike_key = "put" if right == "P" else "call"
        all_strikes = strikes_data.get(strike_key, [])

        if not all_strikes:
            return {
                "underlying": symbol,
                "underlying_conid": underlying_conid,
                "underlying_price": underlying_price,
                "expiry_month": expiry_month,
                "right": right,
                "options": [],
                "error": f"No {right} strikes available for {symbol} {expiry_month}",
                "fetch_timestamp": datetime.now().isoformat(),
            }

        # 4. Filter strikes to range around current price
        min_strike = underlying_price * (1 - strike_range_pct / 100)
        max_strike = underlying_price * (1 + strike_range_pct / 100)
        filtered = [s for s in all_strikes if min_strike <= s <= max_strike]

        # Cap at 30 strikes to limit API calls and subscription slots
        if len(filtered) > 30:
            step = max(1, len(filtered) // 30)
            filtered = filtered[::step][:30]

        logger.info(
            "Option chain: %s %s %s, price=%.2f, %d strikes in %.0f-%.0f range",
            symbol, expiry_month, right, underlying_price,
            len(filtered), min_strike, max_strike,
        )

        if not filtered:
            return {
                "underlying": symbol,
                "underlying_conid": underlying_conid,
                "underlying_price": underlying_price,
                "expiry_month": expiry_month,
                "right": right,
                "options": [],
                "error": f"No strikes in {strike_range_pct}% range for {symbol} at {underlying_price}",
                "fetch_timestamp": datetime.now().isoformat(),
            }

        # 5. Get option contract conids
        option_contracts = self.get_option_conids(
            underlying_conid, expiry_month, filtered, right, port,
        )

        if not option_contracts:
            return {
                "underlying": symbol,
                "underlying_conid": underlying_conid,
                "underlying_price": underlying_price,
                "expiry_month": expiry_month,
                "right": right,
                "options": [],
                "error": f"Could not resolve option contracts for {symbol} {expiry_month} {right}",
                "fetch_timestamp": datetime.now().isoformat(),
            }

        # 6. Get market data for all options in batches of 20
        option_conids = [c["conid"] for c in option_contracts]
        all_snapshots = {}
        for i in range(0, len(option_conids), 20):
            batch = option_conids[i : i + 20]
            batch_data = self.get_snapshot(batch, port)
            all_snapshots.update(batch_data)

        # 7. Combine contract info with market data
        options = []
        for contract in option_contracts:
            conid = contract["conid"]
            snap = all_snapshots.get(conid, {})

            options.append({
                "strike": contract["strike"],
                "right": contract["right"],
                "expiry": contract.get("expiry", expiry_month),
                "conid": conid,
                "bid": snap.get("bid"),
                "ask": snap.get("ask"),
                "last": snap.get("last"),
                "mid": _midpoint(snap.get("bid"), snap.get("ask")),
                "volume": snap.get("volume"),
                "open_interest": snap.get("open_interest"),
                "implied_vol": snap.get("implied_vol"),
                "delta": snap.get("delta"),
                "gamma": snap.get("gamma"),
                "theta": snap.get("theta"),
                "vega": snap.get("vega"),
            })

        # 8. Clean up subscriptions
        self.unsubscribe_all(port)

        options.sort(key=lambda x: x["strike"])

        return {
            "underlying": symbol,
            "underlying_conid": underlying_conid,
            "underlying_price": underlying_price,
            "expiry_month": expiry_month,
            "right": right,
            "options": options,
            "contracts_resolved": len(option_contracts),
            "contracts_with_data": sum(1 for o in options if o["bid"] is not None),
            "fetch_timestamp": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # High-level: multi-symbol quotes
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: list[str], port: int) -> dict:
        """Get current quotes for multiple symbols.

        Returns: {symbol: {"price": float, "bid": float, "ask": float,
                           "volume": float, "implied_vol": float}, ...}
        """
        conid_map: dict[int, str] = {}
        failed = []
        for sym in symbols:
            info = self.search_conid(sym, port)
            if info and info.get("conid"):
                conid_map[info["conid"]] = sym
            else:
                failed.append(sym)

        if failed:
            logger.warning("Could not resolve conids for: %s", ", ".join(failed))

        if not conid_map:
            return {"error": "No symbols resolved", "snapshots": {}}

        snapshots = self.get_snapshot(list(conid_map.keys()), port)

        result = {}
        for conid, data in snapshots.items():
            symbol = conid_map.get(conid)
            if symbol:
                result[symbol] = {
                    "price": data.get("last"),
                    "bid": data.get("bid"),
                    "ask": data.get("ask"),
                    "volume": data.get("volume"),
                    "implied_vol": data.get("implied_vol"),
                }

        self.unsubscribe_all(port)

        return {
            "snapshots": result,
            "resolved": list(result.keys()),
            "failed": failed,
            "fetch_timestamp": datetime.now().isoformat(),
        }


def _midpoint(bid, ask):
    """Calculate midpoint price from bid/ask."""
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 4)
    return None
