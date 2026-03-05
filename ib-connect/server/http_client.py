"""Rate-limited, retry-capable HTTP client for IB Client Portal Gateway API."""

import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Suppress SSL warnings for self-signed gateway certs
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("ib-connect")


class IBHttpClient:
    """HTTP client for IB Client Portal Gateway with rate limiting and retry."""

    def __init__(self, api_call_delay_ms: int = 300):
        self.api_call_delay_ms = api_call_delay_ms
        self._last_call_time = 0.0
        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
        retry = Retry(
            total=3,
            backoff_factor=1,  # 1s, 2s, 4s
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _rate_limit(self):
        """Enforce minimum delay between API calls."""
        now = time.time()
        elapsed_ms = (now - self._last_call_time) * 1000
        if elapsed_ms < self.api_call_delay_ms:
            sleep_s = (self.api_call_delay_ms - elapsed_ms) / 1000
            time.sleep(sleep_s)
        self._last_call_time = time.time()

    def get(self, url: str, timeout: int = 10, rate_limit: bool = True, **kwargs) -> requests.Response:
        if rate_limit:
            self._rate_limit()
        logger.debug("GET %s", url)
        start = time.time()
        resp = self._session.get(url, timeout=timeout, **kwargs)
        duration = time.time() - start
        logger.info("GET %s -> %d (%.1fs)", url, resp.status_code, duration)
        return resp

    def post(self, url: str, timeout: int = 10, rate_limit: bool = True, **kwargs) -> requests.Response:
        if rate_limit:
            self._rate_limit()
        logger.debug("POST %s", url)
        start = time.time()
        resp = self._session.post(url, timeout=timeout, **kwargs)
        duration = time.time() - start
        logger.info("POST %s -> %d (%.1fs)", url, resp.status_code, duration)
        return resp

    def health_check(self, port: int, timeout: int = 5) -> bool:
        """Check if gateway is responding on given port. No rate limit."""
        try:
            resp = self.post(
                f"https://localhost:{port}/v1/api/iserver/auth/status",
                timeout=timeout,
                rate_limit=False
            )
            return resp.status_code == 200
        except Exception:
            return False

    def auth_status(self, port: int) -> dict:
        """Get auth status from gateway. Returns parsed JSON or error dict."""
        try:
            resp = self.post(
                f"https://localhost:{port}/v1/api/iserver/auth/status",
                timeout=10,
                rate_limit=False
            )
            if resp.status_code == 200:
                return resp.json()
            return {"authenticated": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"authenticated": False, "error": str(e)}

    def tickle(self, port: int) -> bool:
        """Send tickle to keep session alive. No rate limit."""
        try:
            resp = self.post(
                f"https://localhost:{port}/v1/api/tickle",
                timeout=5,
                rate_limit=False
            )
            return resp.status_code == 200
        except Exception:
            return False

    def init_brokerage_session(self, port: int) -> bool:
        """Initialize brokerage session after auth."""
        try:
            resp = self.post(
                f"https://localhost:{port}/v1/api/iserver/auth/ssodh/init",
                timeout=10,
                rate_limit=False
            )
            return resp.status_code == 200
        except Exception:
            return False
