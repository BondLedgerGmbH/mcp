"""Portfolio data retrieval, allocation computation, and caching."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .http_client import IBHttpClient
from .config import get_account_config, get_all_account_names

logger = logging.getLogger("ib-connect")

# Exchange to geography mapping
EXCHANGE_GEOGRAPHY = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "ARCA": "US", "BATS": "US",
    "CBOE": "US", "IEX": "US", "PHLX": "US", "BYX": "US", "EDGX": "US",
    "LSE": "Europe", "LSEETF": "Europe", "SBF": "Europe", "IBIS": "Europe",
    "AEB": "Europe", "BM": "Europe", "VSE": "Europe", "SWX": "Europe",
    "BVME": "Europe", "ENEXT.BE": "Europe", "FWB": "Europe", "XETRA": "Europe",
    "EBS": "Europe", "OMXS": "Europe", "OMXH": "Europe", "OMXC": "Europe",
    "OSE": "Europe", "WSE": "Europe", "BIST": "Europe",
    "TSE": "Asia", "SEHK": "Asia", "SGX": "Asia", "KSE": "Asia",
    "TWSE": "Asia", "NSE": "Asia", "BSE": "Asia", "ASX": "Asia-Pacific",
    "NZX": "Asia-Pacific", "TASE": "Middle East",
    "JSE": "Africa", "BMV": "Latin America", "BOVESPA": "Latin America",
    "BVCA": "Latin America",
}


class PortfolioManager:
    """Retrieves and processes portfolio data from IB Client Portal Gateway."""

    def __init__(self, cfg: dict, http: IBHttpClient):
        self.cfg = cfg
        self.http = http
        self._cache_dir = Path(cfg.get("cache_dir", "~/.ib-connect/cache")).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_positions(self, account: str) -> list:
        """Fetch positions for a single account with pagination."""
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        account_id = acct_cfg.get("account_id", "")

        if not account_id:
            return []

        # Must call /portfolio/accounts first to initialize the session
        try:
            self.http.get(f"https://localhost:{port}/v1/api/portfolio/accounts")
        except Exception:
            pass

        all_positions = []
        page = 0

        while True:
            try:
                resp = self.http.get(
                    f"https://localhost:{port}/v1/api/portfolio/{account_id}/positions/{page}"
                )
                if resp.status_code != 200:
                    logger.error("Positions request failed for %s page %d: HTTP %d", account, page, resp.status_code)
                    break

                data = resp.json()
                if not data:
                    break

                for idx, pos in enumerate(data):
                    # Log raw keys for first non-USD position to identify IB base value fields
                    if idx == 0 and pos.get("currency") and pos.get("currency") != "USD":
                        base_keys = {k: v for k, v in pos.items() if "base" in k.lower() or "Base" in k}
                        logger.info("IB raw base fields for %s (%s): %s",
                                    pos.get("contractDesc", "?"), pos.get("currency"), base_keys)

                    position = {
                        "account": account,
                        "account_label": acct_cfg.get("label", account),
                        "tax_treatment": acct_cfg.get("tax_treatment", ""),
                        "conid": pos.get("conid", 0),
                        "ticker": pos.get("contractDesc", pos.get("ticker", "")),
                        "description": pos.get("name", pos.get("description", "")),
                        "asset_class": pos.get("assetClass", ""),
                        "currency": pos.get("currency", ""),
                        "position": pos.get("position", 0),
                        "market_price": pos.get("mktPrice", 0),
                        "market_value": pos.get("mktValue", 0),
                        "average_cost": pos.get("avgCost", 0),
                        "unrealized_pnl": pos.get("unrealizedPnl", 0),
                        "realized_pnl": pos.get("realizedPnl", 0),
                        "sector": pos.get("sector", ""),
                        "listing_exchange": pos.get("listingExchange", ""),
                    }
                    # IB provides base-currency (account base) values when available
                    # These may be None outside market hours
                    ib_base_mv = pos.get("baseMarketValue") or pos.get("baseMktValue")
                    ib_base_pnl = pos.get("baseUnrealizedPnl")
                    ib_base_avg = pos.get("baseAvgCost")
                    if ib_base_mv is not None:
                        position["base_market_value"] = ib_base_mv
                    if ib_base_pnl is not None:
                        position["base_unrealized_pnl"] = ib_base_pnl
                    if ib_base_avg is not None:
                        position["base_average_cost"] = ib_base_avg
                    all_positions.append(position)

                page += 1

            except Exception as e:
                logger.error("Failed to fetch positions for %s page %d: %s", account, page, e)
                break

        return all_positions

    def get_balances(self, account: str) -> dict:
        """Fetch account summary/balances for a single account."""
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        account_id = acct_cfg.get("account_id", "")

        if not account_id:
            return {}

        # Must call /portfolio/accounts first to initialize the session
        try:
            self.http.get(f"https://localhost:{port}/v1/api/portfolio/accounts")
        except Exception:
            pass

        try:
            resp = self.http.get(
                f"https://localhost:{port}/v1/api/portfolio/{account_id}/summary"
            )
            if resp.status_code != 200:
                logger.error("Balance request failed for %s: HTTP %d", account, resp.status_code)
                return {}

            data = resp.json()

            def extract_val(field_name):
                """Extract value from IB summary format {field: {amount: X, ...}}."""
                field = data.get(field_name, {})
                if isinstance(field, dict):
                    return field.get("amount", 0)
                return field if isinstance(field, (int, float)) else 0

            return {
                "account_id": account_id,
                "label": acct_cfg.get("label", account),
                "tax_treatment": acct_cfg.get("tax_treatment", ""),
                "nav": extract_val("netliquidation"),
                "cash": extract_val("totalcashvalue"),
                "total_positions_value": extract_val("stockmarketvalue") + extract_val("bondmarketvalue") + extract_val("optionmarketvalue"),
                "unrealized_pnl": extract_val("unrealizedpnl"),
                "realized_pnl": extract_val("realizedpnl"),
                "base_currency": data.get("baseCurrency", {}).get("currency", "USD") if isinstance(data.get("baseCurrency"), dict) else "USD",
                "maintenance_margin_req": extract_val("maintmarginreq"),
                "margin_borrowed": max(0, -extract_val("totalcashvalue")),
                "buying_power": extract_val("buyingpower"),
            }

        except Exception as e:
            logger.error("Failed to fetch balances for %s: %s", account, e)
            return {}

    def _fetch_fx_rates(self, port: int, currencies: set) -> dict:
        """Fetch FX rates from IB for converting to USD. Returns {currency: rate_to_usd}."""
        rates = {"USD": 1.0}
        for ccy in currencies:
            if ccy == "USD":
                continue
            try:
                resp = self.http.get(
                    f"https://localhost:{port}/v1/api/iserver/exchangerate?source={ccy}&target=USD",
                    rate_limit=False
                )
                if resp.status_code == 200:
                    data = resp.json()
                    rate = data.get("rate")
                    if rate and rate > 0:
                        rates[ccy] = rate
                        logger.info("FX rate %s/USD = %.6f", ccy, rate)
                    else:
                        logger.warning("Invalid FX rate for %s: %s", ccy, data)
                else:
                    logger.warning("FX rate request failed for %s: HTTP %d", ccy, resp.status_code)
            except Exception as e:
                logger.error("Failed to fetch FX rate for %s: %s", ccy, e)
        return rates

    def get_full_summary(self, cfg: dict, accounts: list | None = None, force_refresh: bool = False) -> dict:
        """
        Full combined portfolio: positions + balances + allocations + concentration flags.
        Uses cache unless force_refresh or cache expired.
        """
        cache_ttl = cfg.get("cache_ttl_minutes", 60)

        if not force_refresh:
            cached = self._load_cache()
            if cached:
                age_seconds = (datetime.now() - datetime.fromisoformat(cached["timestamp"])).total_seconds()
                if age_seconds < cache_ttl * 60:
                    cached["cache_status"] = "fresh"
                    cached["cache_age_seconds"] = int(age_seconds)
                    return cached

        if accounts is None:
            accounts = get_all_account_names(cfg)

        # Fetch live data
        all_positions = []
        all_balances = {}

        for account in accounts:
            positions = self.get_positions(account)
            all_positions.extend(positions)
            balances = self.get_balances(account)
            if balances:
                all_balances[account] = balances

        # Normalize all positions to base currency (USD).
        # Prefer IB-provided base values (set in get_positions from baseMarketValue etc.)
        # Only fetch FX rates and compute manually for positions missing IB base values.
        needs_fx = [p for p in all_positions if "base_market_value" not in p]
        needs_fx_currencies = {p.get("currency", "USD") for p in needs_fx} - {"USD"}

        fx_rates = {"USD": 1.0}
        if needs_fx_currencies:
            first_acct = accounts[0]
            port = get_account_config(cfg, first_acct)["port"]
            fx_rates = self._fetch_fx_rates(port, needs_fx_currencies)

        for pos in all_positions:
            if "base_market_value" in pos:
                # IB provided base value — just record the implied rate
                ccy = pos.get("currency", "USD")
                mv = pos.get("market_value", 0)
                if ccy == "USD":
                    pos["fx_rate"] = 1.0
                elif mv and mv != 0:
                    pos["fx_rate"] = round(pos["base_market_value"] / mv, 6)
                else:
                    pos["fx_rate"] = fx_rates.get(ccy)
                # Fill base_unrealized_pnl if IB didn't provide it
                if "base_unrealized_pnl" not in pos:
                    rate = pos.get("fx_rate") or 1.0
                    pos["base_unrealized_pnl"] = round(pos.get("unrealized_pnl", 0) * rate, 2)
            else:
                # No IB base value — compute from FX rate
                ccy = pos.get("currency", "USD")
                rate = fx_rates.get(ccy)
                mv = pos.get("market_value", 0)
                pnl = pos.get("unrealized_pnl", 0)
                if rate:
                    pos["base_market_value"] = round(mv * rate, 2)
                    pos["base_unrealized_pnl"] = round(pnl * rate, 2)
                    pos["fx_rate"] = rate
                else:
                    pos["base_market_value"] = mv
                    pos["base_unrealized_pnl"] = pnl
                    pos["fx_rate"] = None
                    logger.warning("No FX rate for %s, using unconverted value for %s", ccy, pos.get("ticker"))

        # Combined balances (NAV is already in USD from IB account summary)
        combined = {
            "total_nav": sum(b.get("nav", 0) for b in all_balances.values()),
            "total_cash": sum(b.get("cash", 0) for b in all_balances.values()),
            "total_positions_value": sum(b.get("total_positions_value", 0) for b in all_balances.values()),
            "total_unrealized_pnl": sum(b.get("unrealized_pnl", 0) for b in all_balances.values()),
            "total_realized_pnl": sum(b.get("realized_pnl", 0) for b in all_balances.values()),
        }
        all_balances["combined"] = combined

        # Compute allocations using base (USD) values
        allocations = self._compute_allocations(all_positions, combined["total_nav"])

        # Compute concentration flags using base (USD) values
        thresholds = cfg.get("concentration_thresholds", {})
        concentration_flags = self._compute_concentration_flags(
            all_positions, combined["total_nav"], thresholds
        )

        timestamp = datetime.now().isoformat()
        result = {
            "timestamp": timestamp,
            "cache_status": "live",
            "cache_age_seconds": 0,
            "fx_rates": fx_rates,
            "balances": all_balances,
            "positions": all_positions,
            "allocations": allocations,
            "concentration_flags": concentration_flags,
        }

        # Save to cache
        self._save_cache(result, timestamp)

        return result

    def _compute_allocations(self, positions: list, total_nav: float) -> dict:
        """Compute allocation breakdowns from positions using USD-normalized values."""
        if total_nav == 0:
            return {}

        by_asset_class = {}
        by_sector = {}
        by_currency = {}
        by_geography = {}
        by_account = {}

        for pos in positions:
            mv = abs(pos.get("base_market_value", 0))

            # By asset class
            ac = pos.get("asset_class", "OTHER")
            by_asset_class[ac] = by_asset_class.get(ac, 0) + mv

            # By sector
            sector = pos.get("sector", "") or "Unclassified"
            by_sector[sector] = by_sector.get(sector, 0) + mv

            # By currency (group by original currency, but use USD value)
            ccy = pos.get("currency", "USD")
            by_currency[ccy] = by_currency.get(ccy, 0) + mv

            # By geography
            exchange = pos.get("listing_exchange", "")
            geo = EXCHANGE_GEOGRAPHY.get(exchange, "Other")
            by_geography[geo] = by_geography.get(geo, 0) + mv

            # By account
            acct = pos.get("account", "unknown")
            by_account[acct] = by_account.get(acct, 0) + mv

        def to_pct(breakdown):
            return {
                k: {"value": round(v, 2), "pct": round(v / total_nav * 100, 2)}
                for k, v in sorted(breakdown.items(), key=lambda x: -x[1])
            }

        return {
            "by_asset_class": to_pct(by_asset_class),
            "by_sector": to_pct(by_sector),
            "by_currency": to_pct(by_currency),
            "by_geography": to_pct(by_geography),
            "by_account": to_pct(by_account),
        }

    def _compute_concentration_flags(self, positions: list, total_nav: float, thresholds: dict) -> list:
        """Flag positions or sectors exceeding concentration thresholds using USD-normalized values."""
        if total_nav == 0:
            return []

        single_pct = thresholds.get("single_position_pct", 10.0)
        sector_pct = thresholds.get("sector_pct", 30.0)
        flags = []

        # Single position concentration
        ticker_values = {}
        ticker_accounts = {}
        for pos in positions:
            ticker = pos.get("ticker", "")
            mv = abs(pos.get("base_market_value", 0))
            ticker_values[ticker] = ticker_values.get(ticker, 0) + mv
            if ticker not in ticker_accounts:
                ticker_accounts[ticker] = []
            acct = pos.get("account", "")
            if acct not in ticker_accounts[ticker]:
                ticker_accounts[ticker].append(acct)

        for ticker, value in ticker_values.items():
            pct = value / total_nav * 100
            if pct > single_pct:
                flags.append({
                    "type": "single_position",
                    "ticker": ticker,
                    "combined_pct": round(pct, 2),
                    "threshold": single_pct,
                    "accounts": ticker_accounts[ticker],
                })

        # Sector concentration
        sector_values = {}
        for pos in positions:
            sector = pos.get("sector", "") or "Unclassified"
            mv = abs(pos.get("base_market_value", 0))
            sector_values[sector] = sector_values.get(sector, 0) + mv

        for sector, value in sector_values.items():
            pct = value / total_nav * 100
            if pct > sector_pct:
                flags.append({
                    "type": "sector",
                    "sector": sector,
                    "combined_pct": round(pct, 2),
                    "threshold": sector_pct,
                })

        return flags

    # --- Cache ---

    def _cache_latest_path(self) -> Path:
        return self._cache_dir / "portfolio_latest.json"

    def _load_cache(self) -> dict | None:
        path = self._cache_latest_path()
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self, data: dict, timestamp: str):
        """Save cache: latest + timestamped history. Clean old history files."""
        # Latest
        latest = self._cache_latest_path()
        with open(latest, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(latest, 0o600)

        # Timestamped history
        ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
        history = self._cache_dir / f"portfolio_{ts_str}.json"
        with open(history, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(history, 0o600)

        # Cleanup: delete history files older than 10 days
        cutoff = datetime.now().timestamp() - (10 * 24 * 3600)
        for f in self._cache_dir.glob("portfolio_*.json"):
            if f.name == "portfolio_latest.json":
                continue
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info("Cleaned up old cache file: %s", f.name)

    def get_cache_info(self) -> dict:
        """Return cache status info."""
        path = self._cache_latest_path()
        if not path.exists():
            return {"exists": False, "cache_age_seconds": None, "last_refresh": None}

        try:
            with open(path, "r") as f:
                data = json.load(f)
            ts = data.get("timestamp")
            if ts:
                age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                return {"exists": True, "cache_age_seconds": int(age), "last_refresh": ts}
        except Exception:
            pass

        return {"exists": True, "cache_age_seconds": None, "last_refresh": None}
