"""IB Client Portal API Portfolio Analyst (PA) performance data."""

import logging

from .http_client import IBHttpClient
from .config import get_account_config

logger = logging.getLogger("ib-connect")

VALID_PERIODS = ("1D", "7D", "MTD", "1M", "YTD", "1Y")


class PerformanceManager:
    """Fetch account performance data via the IB CP Gateway PA endpoints."""

    def __init__(self, cfg: dict, http: IBHttpClient):
        self.cfg = cfg
        self.http = http

    def get_performance(self, account: str, periods: list[str] | None = None) -> dict:
        """Fetch TWR performance for an account across multiple periods.

        Args:
            account: Account name (e.g. "account1", "account2").
            periods: List of periods to fetch. Defaults to all valid periods.

        Returns:
            Dict with per-period cumulative return, monthly returns, and NAV series.
        """
        acct_cfg = get_account_config(self.cfg, account)
        port = acct_cfg["port"]
        account_id = acct_cfg.get("account_id", "")

        if not account_id:
            return {"error": f"No account_id configured for {account}"}

        if periods is None:
            periods = list(VALID_PERIODS)

        results = {}
        for period in periods:
            if period not in VALID_PERIODS:
                results[period] = {"error": f"Invalid period: {period}. Valid: {', '.join(VALID_PERIODS)}"}
                continue

            resp = self.http.post(
                f"https://localhost:{port}/v1/api/pa/performance",
                json={"acctIds": [account_id], "period": period},
                rate_limit=True,
            )

            if resp.status_code != 200:
                logger.error("PA performance failed for %s period=%s: HTTP %d", account, period, resp.status_code)
                results[period] = {"error": f"HTTP {resp.status_code}", "detail": resp.text[:200]}
                continue

            data = resp.json()
            results[period] = self._parse_performance(data, account_id, period)

        return results

    def _parse_performance(self, data: dict, account_id: str, period: str) -> dict:
        """Parse raw PA performance response into a clean structure."""
        result = {"period": period, "pm": data.get("pm", "TWR")}

        # CPS: cumulative returns (daily series, values are cumulative from start)
        cps = data.get("cps", {})
        cps_data = cps.get("data", [])
        if cps_data:
            entry = cps_data[0]
            returns = entry.get("returns", [])
            dates = cps.get("dates", [])
            result["cumulative_return"] = returns[-1] if returns else 0.0
            result["cumulative_return_pct"] = f"{returns[-1] * 100:+.2f}%" if returns else "0.00%"
            result["start_date"] = entry.get("start", "")
            result["end_date"] = entry.get("end", "")
            result["trading_days"] = len(dates)
            result["base_currency"] = entry.get("baseCurrency", "USD")
            # Include daily series for charting (date -> cumulative return)
            result["daily_cumulative"] = [
                {"date": dates[i], "return": returns[i]}
                for i in range(min(len(dates), len(returns)))
            ]

        # TPPS: monthly returns
        tpps = data.get("tpps", {})
        tpps_data = tpps.get("data", [])
        if tpps_data:
            entry = tpps_data[0]
            monthly_returns = entry.get("returns", [])
            monthly_dates = tpps.get("dates", [])
            result["monthly_returns"] = [
                {"month": monthly_dates[i], "return": monthly_returns[i],
                 "return_pct": f"{monthly_returns[i] * 100:+.2f}%"}
                for i in range(min(len(monthly_dates), len(monthly_returns)))
            ]

        # NAV: daily net asset values
        nav = data.get("nav", {})
        nav_data = nav.get("data", [])
        if nav_data:
            entry = nav_data[0]
            result["start_nav"] = entry.get("startNAV", {})
            navs = entry.get("navs", [])
            result["end_nav"] = navs[-1] if navs else None

        return result
