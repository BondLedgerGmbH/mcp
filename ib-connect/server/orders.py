"""Order placement, status, and management for IB Client Portal Gateway."""

import logging
import time
from typing import Optional

logger = logging.getLogger("ib-connect")

# Maximum confirmation loop iterations (safety valve)
MAX_CONFIRMATION_LOOPS = 5


class OrderManager:
    """Manages order placement and lifecycle via IB CP Gateway REST API."""

    def __init__(self, http, market_data):
        """
        Args:
            http: IBHttpClient instance
            market_data: MarketDataManager instance (for conid resolution and snapshots)
        """
        self.http = http
        self.market_data = market_data

    def resolve_symbol(self, ticker: str, port: int) -> dict:
        """Resolve ticker to conid using existing market_data search.

        Returns: {"conid": int, "name": str, "option_months": list[str]}
        Raises: ValueError if symbol not found
        """
        result = self.market_data.search_conid(ticker, port)
        if not result:
            raise ValueError(f"Symbol not found: {ticker}")
        return result

    def get_last_price(self, conid: int, port: int) -> float:
        """Fetch current last price for a conid.

        Uses MarketDataManager.get_snapshot which handles subscription
        warm-up, price prefix stripping, and retries.

        Returns: last price as float
        Raises: ValueError if no valid price available
        """
        snap = self.market_data.get_snapshot([conid], port)
        if not snap or conid not in snap:
            raise ValueError(f"Could not fetch price for conid {conid}")
        data = snap[conid]
        price = data.get("last") or data.get("bid") or data.get("ask")
        if not price or price <= 0:
            raise ValueError(f"No valid price for conid {conid}")

        # Clean up subscription
        self.market_data.unsubscribe_all(port)
        return float(price)

    def calculate_shares_from_cash(self, cash_amount: float, price: float) -> int:
        """Convert cash amount to share quantity.

        IB CP Gateway does NOT reliably support fractional shares for
        cash-based orders. Always rounds DOWN to whole shares.

        Returns: number of whole shares
        Raises: ValueError if result is 0 or price is non-positive
        """
        if price <= 0:
            raise ValueError(f"Invalid price: {price}")
        shares = int(cash_amount / price)  # floor division
        if shares <= 0:
            raise ValueError(
                f"Cash amount ${cash_amount:.2f} is less than one share at ${price:.2f}"
            )
        return shares

    def build_order_payload(
        self,
        conid: int,
        side: str,
        quantity: float,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        tif: str = "DAY",
        outside_rth: bool = False,
        ticker: str = "",
        is_ccy_conv: bool = False,
    ) -> dict:
        """Build the IB order JSON payload.

        Args:
            conid: Contract ID (from resolve_symbol)
            side: "BUY" or "SELL"
            quantity: Number of shares (float — IB accepts fractional for stocks)
            order_type: "MKT", "LMT", "STP", "STP_LIMIT"
            limit_price: Required for LMT and STP_LIMIT
            stop_price: Required for STP and STP_LIMIT (maps to auxPrice)
            tif: Time-in-force: "DAY", "GTC", "IOC", "OPG"
            outside_rth: Allow execution outside regular trading hours
            ticker: Informational ticker symbol
            is_ccy_conv: True for currency conversion orders

        Returns: dict ready for POST to /iserver/account/{id}/orders
        """
        order = {
            "conid": conid,
            "orderType": order_type,
            "side": side,
            "quantity": round(quantity, 4),
            "tif": tif,
            "outsideRTH": outside_rth,
            "ticker": ticker,
        }

        if order_type in ("LMT", "STP_LIMIT", "LOC"):
            if limit_price is None:
                raise ValueError(f"limit_price required for {order_type} orders")
            order["price"] = limit_price

        if order_type in ("STP", "STP_LIMIT"):
            if stop_price is None:
                raise ValueError(f"stop_price required for {order_type} orders")
            order["auxPrice"] = stop_price

        if is_ccy_conv:
            order["isCcyConv"] = True

        return {"orders": [order]}

    def submit_order(self, port: int, account_id: str, payload: dict) -> dict:
        """Submit order and handle IB's confirmation loop.

        IB almost always returns a confirmation/warning message before
        actually submitting the order. This method automatically confirms
        all warnings (the skill layer handles user approval BEFORE calling
        this method).

        Returns: {"order_id": str, "order_status": str, "warning_messages": [...]}
        Raises: ValueError on rejection
        """
        url = f"https://localhost:{port}/v1/api/iserver/account/{account_id}/orders"

        resp = self.http.post(url, json=payload)
        if resp.status_code not in (200, 201):
            raise ValueError(
                f"Order submission failed: HTTP {resp.status_code} — {resp.text}"
            )

        result = resp.json()
        collected_warnings = []

        # Confirmation loop: IB returns messages that must be confirmed
        loop_count = 0
        while loop_count < MAX_CONFIRMATION_LOOPS:
            # Check if this is a confirmation request (has "id" and "message")
            if isinstance(result, list) and len(result) > 0:
                item = result[0]

                # Check if order was accepted (has order_id)
                if "order_id" in item:
                    return {
                        "order_id": str(item["order_id"]),
                        "order_status": item.get("order_status", "Unknown"),
                        "local_order_id": item.get("local_order_id"),
                        "warning_messages": collected_warnings,
                    }

                # Check if this is a confirmation prompt
                if "id" in item and "message" in item:
                    reply_id = item["id"]
                    messages = item.get("message", [])
                    if isinstance(messages, list):
                        collected_warnings.extend(messages)
                    else:
                        collected_warnings.append(str(messages))

                    logger.info(
                        "Order confirmation required (loop %d): %s",
                        loop_count + 1,
                        messages,
                    )

                    # Auto-confirm
                    reply_url = f"https://localhost:{port}/v1/api/iserver/reply/{reply_id}"
                    reply_resp = self.http.post(reply_url, json={"confirmed": True})

                    if reply_resp.status_code not in (200, 201):
                        raise ValueError(
                            f"Confirmation failed: HTTP {reply_resp.status_code}"
                        )

                    result = reply_resp.json()
                    loop_count += 1
                    continue

            # Unexpected response format
            raise ValueError(f"Unexpected order response: {result}")

        raise ValueError(
            f"Order confirmation loop exceeded {MAX_CONFIRMATION_LOOPS} iterations"
        )

    def preview_order(self, port: int, account_id: str, payload: dict) -> dict:
        """Preview order without submitting (what-if analysis).

        Tries both payload formats (wrapped and unwrapped) since IB docs
        are inconsistent about which one the whatif endpoint expects.

        Returns: margin impact, commission estimate, and warnings.
        """
        url = f"https://localhost:{port}/v1/api/iserver/account/{account_id}/order/whatif"

        # Try with the orders wrapper first (same format as placement)
        resp = self.http.post(url, json=payload)
        if resp.status_code == 200:
            return self._parse_whatif_response(resp.json())

        # If that fails, try with unwrapped single order
        if "orders" in payload and len(payload["orders"]) == 1:
            logger.info("whatif: wrapped format failed (HTTP %d), trying unwrapped", resp.status_code)
            resp = self.http.post(url, json=payload["orders"][0])
            if resp.status_code == 200:
                return self._parse_whatif_response(resp.json())

        return {"error": f"Preview failed: HTTP {resp.status_code}", "detail": resp.text}

    def _parse_whatif_response(self, data) -> dict:
        """Parse the whatif endpoint response into a clean format.

        IB returns null for margin/commission/equity fields on some accounts
        (e.g. non-margin accounts). The useful data is in 'amount' and 'data'.
        """
        if not data or not isinstance(data, dict):
            logger.warning("whatif response was not a dict: %s", type(data))
            return {"error": f"Unexpected whatif response type: {type(data).__name__}"}

        result = {
            "initial_margin_change": (data.get("initial") or {}).get("change"),
            "maintenance_margin_change": (data.get("maintenance") or {}).get("change"),
            "commission": (data.get("commission") or {}).get("amount"),
            "commission_currency": (data.get("commission") or {}).get("currency"),
            "equity_with_loan": (data.get("equity") or {}).get("current"),
            "warnings": data.get("warns") or data.get("warn") or [],
            "error": data.get("error"),
        }

        # Extract amount summary (always present even when margin fields are null)
        amount_info = data.get("amount")
        if amount_info and isinstance(amount_info, dict):
            result["amount_summary"] = amount_info.get("amount")
            result["commission_summary"] = amount_info.get("commission")
            result["total_summary"] = amount_info.get("total")

        # Extract position/funds data from the 'data' array
        for item in data.get("data") or []:
            name = item.get("N", "")
            vals = item.get("V", [])
            val = vals[0] if vals else None
            if name == "CURRENT_POS":
                result["current_position"] = val
            elif name == "AFTER_POS":
                result["post_trade_position"] = val
            elif name == "CURRENT_FUNDS":
                result["available_funds"] = val
            elif name == "AFTER_FUNDS":
                result["post_trade_funds"] = val

        return result

    def get_live_orders(self, port: int) -> dict:
        """Fetch all live/recent orders for the session.

        Returns: {"orders": [...], "notifications": [...]}
        """
        url = f"https://localhost:{port}/v1/api/iserver/account/orders"
        resp = self.http.get(url)
        if resp.status_code != 200:
            return {"error": f"Failed to fetch orders: HTTP {resp.status_code}"}
        return resp.json()

    def cancel_order(self, port: int, account_id: str, order_id: str) -> dict:
        """Cancel a live order.

        Returns: {"order_id": str, "msg": str, ...}
        """
        url = f"https://localhost:{port}/v1/api/iserver/account/{account_id}/order/{order_id}"
        resp = self.http.delete(url)
        if resp.status_code != 200:
            return {
                "error": f"Cancel failed: HTTP {resp.status_code}",
                "detail": resp.text,
            }
        return resp.json()

    def resolve_fx_conid(self, from_currency: str, to_currency: str, port: int) -> int:
        """Resolve the conid for a currency pair.

        Example: resolve_fx_conid("USD", "EUR", 5100) -> conid for EUR.USD

        Uses /iserver/secdef/search with secType CASH.
        Reserved for future REBALANCE implementation.
        """
        pair = f"{to_currency}.{from_currency}"
        url = f"https://localhost:{port}/v1/api/iserver/secdef/search"
        resp = self.http.post(url, json={"symbol": pair, "secType": "CASH"})
        if resp.status_code != 200 or not resp.json():
            raise ValueError(f"FX pair not found: {pair}")
        return int(resp.json()[0]["conid"])
