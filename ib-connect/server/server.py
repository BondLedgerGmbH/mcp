"""IB Connect MCP Server - Main entry point with FastMCP tool definitions."""

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

from .config import load_config, get_all_account_names, get_account_config
from .http_client import IBHttpClient
from .gateway import GatewayManager
from .market_data import MarketDataManager, INVERSE_ETF_INFO
from .portfolio import PortfolioManager
from .orders import OrderManager
from .performance import PerformanceManager, VALID_PERIODS

# --- Logging setup ---

def setup_logging(log_dir: str):
    """Configure logging to file and stderr."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / "mcp-server.log"

    logger = logging.getLogger("ib-connect")
    logger.setLevel(logging.INFO)

    # File handler
    fh = logging.FileHandler(log_file, mode="a")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    # Stderr handler (required for stdio transport)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("[ib-connect] %(levelname)s: %(message)s"))
    logger.addHandler(sh)

    return logger


# --- Initialize ---

cfg = load_config()
logger = setup_logging(cfg.get("log_dir", str(Path("~/.ib-connect/logs").expanduser())))
logger.info("MCP server starting, config loaded")

http = IBHttpClient(api_call_delay_ms=cfg.get("api_call_delay_ms", 300))
gateway = GatewayManager(cfg, http)
market_data = MarketDataManager(cfg, http)
portfolio = PortfolioManager(cfg, http)
order_mgr = OrderManager(http=http, market_data=market_data)
perf_mgr = PerformanceManager(cfg, http)

# --- Startup readiness gate ---
# Data tools block until at least one account completes the full startup workflow:
# gateway running → authenticated → account ID discovered → portfolio cached
# Accounts that fail to authenticate are tracked but do not block startup.

_startup_ready = threading.Event()
_startup_phase = "initializing"
_startup_error = None
_connected_accounts: list[str] = []      # accounts that completed startup
_failed_accounts: dict[str, str] = {}    # account → reason for failure


def _refresh_cfg():
    """Reload config from disk into the module-level cfg dict.

    Call after auto_discover_account_id() writes new account IDs to disk,
    so that all tools see the updated values.
    """
    fresh = load_config()
    cfg.update(fresh)
    gateway.cfg = cfg
    portfolio.cfg = cfg


def _set_phase(phase: str):
    global _startup_phase
    _startup_phase = phase
    logger.info("Startup phase: %s", phase)


# Check for gateway updates on startup (non-blocking)
try:
    update_result = gateway.check_for_update()
    if update_result.get("update_available"):
        logger.info("Gateway update available")
except Exception as e:
    logger.warning("Gateway update check failed on startup: %s", e)


# Auto-start gateways on MCP connect (background thread so it doesn't block
# the MCP server from accepting connections). Full flow:
# 1. Start gateways that aren't running
# 2. Open login pages for unauthenticated accounts
# 3. Poll until at least one account authenticates (up to 5 minutes)
# 4. Auto-discover account IDs for authenticated accounts
# 5. Warm up portfolio API session for authenticated accounts
# 6. Pull and cache portfolio summary for connected accounts
# 7. Set _startup_ready (partial availability — does not require all accounts)
def _auto_start_gateways():
    global _startup_error
    try:
        all_accounts = [
            acct for acct in get_all_account_names(cfg)
            if cfg["accounts"][acct].get("auto_start", True)
        ]

        # Phase 1: Start gateways and open login pages
        _set_phase("starting_gateways")
        needs_auth = []
        already_auth = []
        start_failed = []
        for acct in all_accounts:
            status = gateway.get_status(acct)
            if not status["gateway_running"]:
                logger.info("Auto-starting gateway for %s", acct)
                result = gateway.start(acct)
                if result["status"] in ("started", "already_running"):
                    gateway.open_login_page(acct)
                    needs_auth.append(acct)
                    logger.info("Auto-started gateway and opened login for %s", acct)
                else:
                    reason = f"Gateway start failed: {result.get('error')}"
                    _failed_accounts[acct] = reason
                    start_failed.append(acct)
                    logger.warning("Gateway start failed for %s: %s", acct, result.get("error"))
            elif not status["authenticated"]:
                gateway.open_login_page(acct)
                needs_auth.append(acct)
                logger.info("Gateway running but not authenticated for %s, opened login page", acct)
            else:
                already_auth.append(acct)
                logger.info("Gateway already running and authenticated for %s", acct)

        # Phase 2: Poll for auth completion (up to 5 minutes)
        # Succeeds when at least one account authenticates (or some were already auth'd)
        authenticated = list(already_auth)
        if needs_auth:
            _set_phase("waiting_for_auth")
            max_wait = 300
            poll_interval = 3
            start_time = time.time()
            pending = set(needs_auth)

            while pending and (time.time() - start_time) < max_wait:
                time.sleep(poll_interval)
                for acct in list(pending):
                    status = gateway.get_status(acct)
                    if status["authenticated"]:
                        logger.info("Authentication completed for %s", acct)
                        pending.discard(acct)
                        authenticated.append(acct)

            # Track timed-out accounts as failed, but don't abort startup
            for acct in pending:
                _failed_accounts[acct] = f"Authentication timed out after {max_wait}s"
                logger.warning("Auth timed out for %s", acct)

        if not authenticated:
            _startup_error = "No accounts authenticated. " + ", ".join(
                f"{a}: {r}" for a, r in _failed_accounts.items()
            )
            _set_phase("error")
            return

        logger.info("Authenticated accounts: %s (failed: %s)",
                     ", ".join(authenticated),
                     ", ".join(_failed_accounts.keys()) if _failed_accounts else "none")

        # Phase 3: Auto-discover account IDs and warm up portfolio API
        # Only for authenticated accounts
        _set_phase("discovering_accounts")
        ready_accounts = []
        for acct in authenticated:
            acct_cfg = get_account_config(cfg, acct)
            if not acct_cfg.get("account_id"):
                discovered = gateway.auto_discover_account_id(acct)
                if discovered:
                    logger.info("Discovered account ID for %s: %s", acct, discovered)
                    ready_accounts.append(acct)
                else:
                    _failed_accounts[acct] = "Could not discover account ID"
                    logger.warning("Could not discover account ID for %s", acct)
            else:
                # Warm up the portfolio API session even if account_id is known
                port = acct_cfg["port"]
                try:
                    http.get(f"https://localhost:{port}/v1/api/portfolio/accounts")
                    logger.info("Portfolio API warmed up for %s", acct)
                except Exception:
                    pass
                ready_accounts.append(acct)

        if not ready_accounts:
            _startup_error = "No accounts ready after discovery. " + ", ".join(
                f"{a}: {r}" for a, r in _failed_accounts.items()
            )
            _set_phase("error")
            return

        # Phase 4: Pull and cache initial portfolio summary for ready accounts
        _set_phase("loading_portfolio")

        # Reload config in case account IDs were just discovered
        _refresh_cfg()

        result = portfolio.get_full_summary(cfg, ready_accounts, force_refresh=True)

        # Validate — at least one account should have non-zero NAV
        balances = result.get("balances", {})
        has_nonzero_nav = any(
            b.get("nav", 0) != 0
            for k, b in balances.items()
            if k != "combined"
        )
        if not has_nonzero_nav:
            _startup_error = "Portfolio loaded but all connected accounts show zero NAV"
            _set_phase("error")
            return

        total_positions = len(result.get("positions", []))
        total_nav = balances.get("combined", {}).get("total_nav", 0)
        logger.info("Portfolio loaded: %d positions, NAV $%.2f", total_positions, total_nav)

        # Done — partial availability is OK
        _connected_accounts.extend(ready_accounts)
        _set_phase("ready")
        _startup_ready.set()

        if _failed_accounts:
            logger.info("Startup complete (partial): %d/%d accounts connected. Failed: %s",
                         len(ready_accounts), len(all_accounts),
                         ", ".join(f"{a} ({r})" for a, r in _failed_accounts.items()))
        else:
            logger.info("Startup complete: all %d accounts connected, portfolio cached",
                         len(ready_accounts))

    except Exception as e:
        _startup_error = str(e)
        _set_phase("error")
        logger.warning("Gateway auto-start failed: %s", e)

threading.Thread(target=_auto_start_gateways, daemon=True).start()


def _check_startup_ready() -> dict | None:
    """Return error dict if startup hasn't completed, None if ready."""
    if _startup_ready.is_set():
        return None
    if _startup_phase == "error":
        return {
            "error": "startup_failed",
            "phase": _startup_phase,
            "detail": _startup_error,
            "message": f"Server startup failed: {_startup_error}. "
                       "Try /mcp to reconnect."
        }
    return {
        "error": "startup_in_progress",
        "phase": _startup_phase,
        "message": f"Server is starting up (phase: {_startup_phase}). "
                   "Please wait for startup to complete."
    }


# --- FastMCP Server ---

mcp = FastMCP(
    "ib-connect",
    instructions="Interactive Brokers portfolio data server. Manages gateway lifecycle, authentication, portfolio data retrieval, and order execution across multiple accounts. Supports both live and paper trading modes."
)


def _resolve_accounts(account: str) -> list:
    """Resolve account parameter to list of account names.

    'all' returns only live accounts. Paper accounts must be
    requested explicitly by name.
    """
    if account == "all" or not account:
        return [
            acct for acct in get_all_account_names(cfg)
            if cfg["accounts"][acct].get("mode", "live") == "live"
        ]
    return [account]


def _filter_ready_accounts(accounts: list) -> tuple[list, dict | None]:
    """Filter accounts to only those that are running and authenticated.

    Returns (ready_accounts, error_or_none).
    - If ALL requested accounts are unavailable: returns ([], error_dict)
    - If SOME are available: returns (available_list, None) — partial availability
    - If ALL are available: returns (all_list, None)

    When account="all", unavailable accounts are silently filtered out (partial
    availability). When a specific account is requested and it's not ready,
    an error is returned.
    """
    needs_auth = []
    not_running = []
    ready = []
    for acct in accounts:
        status = gateway.get_status(acct)
        if not status["gateway_running"]:
            not_running.append(acct)
        elif not status["authenticated"]:
            needs_auth.append(acct)
        else:
            ready.append(acct)

    # If we have at least some ready accounts, proceed with partial availability
    if ready:
        return ready, None

    # No accounts ready — return an error
    if not_running:
        labels = [get_account_config(cfg, a).get("label", a) for a in not_running]
        return [], {
            "error": "gateway_not_running",
            "accounts_not_running": not_running,
            "message": f"Gateway not running for: {', '.join(labels)}. Use ib_start_gateway to start."
        }
    if needs_auth:
        labels = [get_account_config(cfg, a).get("label", a) for a in needs_auth]
        return [], {
            "error": "auth_required",
            "accounts_needing_auth": needs_auth,
            "message": f"Authentication required for: {', '.join(labels)}. Use ib_reauthenticate to open login pages."
        }
    return [], {"error": "no_accounts", "message": "No accounts configured."}


# === Tools ===


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_status(account: str = "all") -> dict:
    """Check the status of gateway instances and authentication.

    Args:
        account: Account name (e.g., "bondledger", "personal") or "all". Default: "all".
    """
    logger.info("Tool call: ib_status(account=%s)", account)
    accounts = _resolve_accounts(account)

    account_statuses = {}
    for acct in accounts:
        account_statuses[acct] = gateway.get_status(acct)

    all_auth = all(s["authenticated"] for s in account_statuses.values())

    result = {
        "accounts": account_statuses,
        "all_authenticated": all_auth,
        "startup_ready": _startup_ready.is_set(),
        "startup_phase": _startup_phase,
        "connected_accounts": list(_connected_accounts),
        "failed_accounts": dict(_failed_accounts),
        "gateway_version_date": gateway.gateway_version_date(),
        "update_available": cfg.get("update_available", False),
        "last_update_check": cfg.get("last_update_check"),
        "rollback_active": cfg.get("rollback_active", False),
        "rollback_reason": cfg.get("rollback_reason"),
        "portfolio_cache": portfolio.get_cache_info(),
    }

    if _startup_phase == "error":
        result["startup_error"] = _startup_error

    # Build message
    messages = []

    if not _startup_ready.is_set():
        if _startup_phase == "error":
            messages.append(f"Startup failed: {_startup_error}")
        else:
            messages.append(f"Startup in progress (phase: {_startup_phase}). Portfolio data not yet available.")

    if cfg.get("rollback_active"):
        messages.append(
            f"Gateway update failed and was rolled back. Reason: {cfg.get('rollback_reason')}. "
            f"Running previous version from {gateway.gateway_version_date()}."
        )
    if cfg.get("update_available"):
        messages.append("A gateway update is available. It will be applied on next ib_start_gateway if confirmed.")

    not_auth = [a for a, s in account_statuses.items() if s["gateway_running"] and not s["authenticated"]]
    not_running = [a for a, s in account_statuses.items() if not s["gateway_running"]]

    if not_running:
        labels = [get_account_config(cfg, a).get("label", a) for a in not_running]
        messages.append(f"Gateway not running for: {', '.join(labels)}. Use ib_start_gateway to start.")
    if not_auth:
        labels = [get_account_config(cfg, a).get("label", a) for a in not_auth]
        messages.append(f"Authentication required for: {', '.join(labels)}. Use ib_reauthenticate to open login pages.")
    if _startup_ready.is_set() and all_auth:
        messages.append("All accounts connected, authenticated, and portfolio data ready.")
    elif _startup_ready.is_set() and _failed_accounts:
        labels = [get_account_config(cfg, a).get("label", a) for a in _failed_accounts]
        messages.append(f"Partial availability: {len(_connected_accounts)} account(s) connected. "
                        f"Failed: {', '.join(labels)}.")

    result["message"] = " ".join(messages) if messages else "No gateways running."

    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def ib_start_gateway(account: str = "all") -> dict:
    """Start gateway instance(s) and open login pages.

    Args:
        account: Account name or "all". Default: "all".
    """
    logger.info("Tool call: ib_start_gateway(account=%s)", account)
    accounts = _resolve_accounts(account)

    started = []
    login_required = []
    errors = []

    for acct in accounts:
        result = gateway.start(acct)
        if result["status"] in ("started", "already_running"):
            acct_cfg = get_account_config(cfg, acct)
            started.append(acct)
            # Open login page
            url = gateway.open_login_page(acct)
            login_required.append({
                "account": acct,
                "url": url,
                "label": acct_cfg.get("label", acct)
            })
        elif result["status"] == "error":
            errors.append({"account": acct, "error": result.get("error", "unknown")})

    response = {
        "started": started,
        "login_required": login_required,
    }

    if errors:
        response["errors"] = errors
        response["message"] = f"Started {len(started)} gateway(s). {len(errors)} error(s)."
    else:
        response["message"] = "Gateways started. Login pages opened in your default browser. Complete authentication for each account."

    return response


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def ib_stop_gateway(account: str = "all") -> dict:
    """Stop gateway instance(s).

    Args:
        account: Account name or "all". Default: "all".
    """
    logger.info("Tool call: ib_stop_gateway(account=%s)", account)
    accounts = _resolve_accounts(account)

    stopped = []
    for acct in accounts:
        result = gateway.stop(acct)
        stopped.append({"account": acct, "status": result["status"]})

    return {
        "stopped": stopped,
        "message": f"Stopped {len([s for s in stopped if s['status'] == 'stopped'])} gateway(s)."
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
def ib_reauthenticate(account: str = "all") -> dict:
    """Re-open login page for running gateway(s) when session has expired.

    Args:
        account: Account name or "all". Default: "all".
    """
    logger.info("Tool call: ib_reauthenticate(account=%s)", account)
    accounts = _resolve_accounts(account)

    opened = []
    errors = []

    for acct in accounts:
        if not gateway.is_running(acct):
            errors.append({
                "account": acct,
                "error": f"Gateway not running for {acct}. Use ib_start_gateway instead."
            })
            continue

        url = gateway.open_login_page(acct)
        acct_cfg = get_account_config(cfg, acct)
        opened.append({"account": acct, "url": url, "label": acct_cfg.get("label", acct)})

    response = {"accounts_opened": [o["account"] for o in opened]}

    if opened:
        labels = [o["label"] for o in opened]
        response["message"] = f"Login page(s) opened for: {', '.join(labels)}. Complete authentication in your browser."
    if errors:
        response["errors"] = errors

    return response


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_portfolio_positions(account: str = "all") -> dict:
    """Retrieve current positions for specified account(s).

    Args:
        account: Account name or "all". Default: "all".
    """
    logger.info("Tool call: ib_portfolio_positions(account=%s)", account)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    accounts = _resolve_accounts(account)

    # Tickle and filter to ready accounts (partial availability)
    gateway.tickle_all()
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    all_positions = []
    for acct in ready:
        positions = portfolio.get_positions(acct)
        all_positions.extend(positions)

    result = {
        "positions": all_positions,
        "total_positions": len(all_positions),
        "accounts_queried": ready,
    }
    skipped = [a for a in accounts if a not in ready]
    if skipped:
        result["accounts_unavailable"] = skipped
    return result


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_portfolio_balances(account: str = "all") -> dict:
    """Retrieve account balances and NAV.

    Args:
        account: Account name or "all". Default: "all".
    """
    logger.info("Tool call: ib_portfolio_balances(account=%s)", account)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    accounts = _resolve_accounts(account)

    # Tickle and filter to ready accounts (partial availability)
    gateway.tickle_all()
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    all_balances = {}
    for acct in ready:
        balances = portfolio.get_balances(acct)
        if balances:
            all_balances[acct] = balances

    combined = {
        "total_nav": sum(b.get("nav", 0) for b in all_balances.values()),
        "total_cash": sum(b.get("cash", 0) for b in all_balances.values()),
        "total_positions_value": sum(b.get("total_positions_value", 0) for b in all_balances.values()),
    }

    result = {
        "accounts": all_balances,
        "combined": combined,
    }
    skipped = [a for a in accounts if a not in ready]
    if skipped:
        result["accounts_unavailable"] = skipped
    return result


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_portfolio_summary(account: str = "all", force_refresh: bool = False) -> dict:
    """Combined view: positions + balances + allocation breakdown + concentration flags.

    This is the primary tool for portfolio analysis. Returns cached data if fresh,
    or pulls live data from IB.

    Args:
        account: Account name or "all". Default: "all".
        force_refresh: If true, bypass cache and pull live data. Default: false.
    """
    logger.info("Tool call: ib_portfolio_summary(account=%s, force_refresh=%s)", account, force_refresh)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    accounts = _resolve_accounts(account)

    # Tickle and filter to ready accounts (partial availability)
    gateway.tickle_all()
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    # Auto-discover account IDs if needed (only for ready accounts)
    discovered_any = False
    for acct in ready:
        acct_cfg = get_account_config(cfg, acct)
        if not acct_cfg.get("account_id"):
            discovered = gateway.auto_discover_account_id(acct)
            if not discovered:
                return {
                    "error": "account_id_missing",
                    "message": f"Could not auto-discover account ID for {acct}. "
                               f"Set it manually in ~/.ib-connect/config.json."
                }
            discovered_any = True
    if discovered_any:
        _refresh_cfg()

    result = portfolio.get_full_summary(cfg, ready, force_refresh)

    # Validate: at least one account with non-zero NAV
    balances = result.get("balances", {})
    has_nonzero_nav = any(
        b.get("nav", 0) != 0
        for k, b in balances.items()
        if k != "combined"
    )
    if not has_nonzero_nav and balances:
        return {
            "error": "zero_nav",
            "message": "Portfolio data returned but all connected accounts show zero NAV. "
                       "Verify accounts are funded and gateway is returning correct data."
        }

    skipped = [a for a in accounts if a not in ready]
    if skipped:
        result["accounts_unavailable"] = skipped
    return result


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_option_chain(
    underlying: str,
    expiry_month: str,
    right: str = "P",
    strike_range_pct: float = 20.0,
    account: str = "",
) -> dict:
    """Retrieve a filtered option chain with live pricing, IV, and greeks.

    Fetches available strikes for the underlying within the specified range
    of the current price, then pulls live market data (bid/ask/last/IV/
    delta/theta/gamma/vega/open interest) for each contract.

    Args:
        underlying: Underlying symbol (e.g. "SPY", "QQQ", "XLK").
        expiry_month: Expiry in YYYYMM format (e.g. "202606" for June 2026).
        right: "P" for puts, "C" for calls. Default: "P".
        strike_range_pct: Include strikes within this % of current price. Default: 20.
        account: Account whose gateway to use (market data is the same from any
                 gateway). Default: first available authenticated account.
    """
    logger.info("Tool call: ib_option_chain(underlying=%s, expiry=%s, right=%s, range=%.1f%%)",
                underlying, expiry_month, right, strike_range_pct)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    # Use first authenticated gateway for market data
    accounts = _resolve_accounts(account) if account else get_all_account_names(cfg)
    gateway.tickle_all()
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    port = get_account_config(cfg, ready[0])["port"]

    result = market_data.get_option_chain(
        symbol=underlying,
        expiry_month=expiry_month,
        right=right,
        strike_range_pct=strike_range_pct,
        port=port,
    )

    # Add summary stats
    options = result.get("options", [])
    if options:
        bids = [o["bid"] for o in options if o.get("bid") is not None]
        result["summary"] = {
            "total_contracts": len(options),
            "contracts_with_bids": len(bids),
            "strike_range": f"{options[0]['strike']}-{options[-1]['strike']}",
        }

    return result


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_market_snapshot(
    symbols: list[str],
    account: str = "",
) -> dict:
    """Get current market data snapshots for multiple symbols.

    Returns current price, bid, ask, volume, and implied volatility for
    each symbol. Also includes metadata for well-known inverse ETFs
    (expense ratio, leverage, tracked index).

    Useful for: VIX level, inverse ETF pricing, safe haven asset prices,
    underlying prices for hedge cost estimation.

    Args:
        symbols: List of symbols (e.g. ["VIX", "SPY", "SQQQ", "GLD", "TLT"]).
        account: Account whose gateway to use. Default: first available.
    """
    logger.info("Tool call: ib_market_snapshot(symbols=%s)", symbols)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    accounts = _resolve_accounts(account) if account else get_all_account_names(cfg)
    gateway.tickle_all()
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    port = get_account_config(cfg, ready[0])["port"]

    result = market_data.get_quotes(symbols, port)

    # Enrich inverse ETFs with metadata
    snapshots = result.get("snapshots", {})
    for sym, data in snapshots.items():
        if sym in INVERSE_ETF_INFO:
            data["inverse_etf_info"] = INVERSE_ETF_INFO[sym]

    return result


# === Order helpers ===


def _get_positions_for_account(account_name: str) -> list:
    """Get current positions for a specific account.

    Returns list of position dicts with: ticker, position, market_value, currency, conid
    """
    return portfolio.get_positions(account_name)


def _resolve_conid_for_order(
    ticker: str, account: str, port: int, override_conid: int = 0
) -> dict:
    """Resolve a ticker to a conid for order placement.

    Resolution priority:
    1. Explicit conid override (from prior disambiguation)
    2. Portfolio positions (ticker already held → use that conid)
    3. IB secdef/search with disambiguation logic:
       - Single STK result → use it
       - Multiple results, same company → pick primary listing (most
         derivative sections = home exchange). If tied → ambiguous.
       - Multiple results, different companies → ambiguous.

    Returns:
        {"conid": int, "name": str}  on success
        {"conid": int, "name": str, "position": float}  if found in portfolio
        {"error": "ambiguous_symbol", "candidates": [...]}  if disambiguation needed
        {"error": "symbol_not_found", "message": str}  if no results
    """
    # 1. Explicit override
    if override_conid:
        logger.info("Using explicit conid %d for %s", override_conid, ticker)
        return {"conid": override_conid, "name": ticker}

    # 2. Check portfolio positions
    try:
        positions = _get_positions_for_account(account)
        pos = next((p for p in positions if p["ticker"] == ticker), None)
        if pos and pos.get("conid"):
            logger.info(
                "Resolved %s -> conid %s from portfolio positions",
                ticker, pos["conid"],
            )
            return {
                "conid": pos["conid"],
                "name": ticker,
                "position": pos.get("position", 0),
            }
    except Exception as e:
        logger.info("Portfolio position lookup failed for %s/%s: %s", ticker, account, e)
        pass  # fall through to search

    # 3. IB search with disambiguation
    candidates = market_data.search_symbol_candidates(ticker, port)
    if not candidates:
        return {"error": "symbol_not_found", "message": f"Symbol not found: {ticker}"}

    if len(candidates) == 1:
        c = candidates[0]
        logger.info(
            "Resolved %s -> conid %s (%s, %s) — single result",
            ticker, c["conid"], c["name"], c["exchange"],
        )
        return {"conid": c["conid"], "name": c["name"]}

    # Multiple candidates — pick primary listing by section count.
    # Primary listings (home exchange) have more derivative sections
    # (OPT, FUT, WAR) than secondary listings (STK only).
    top = candidates[0]  # already sorted by num_sections desc
    runner_up = candidates[1]
    if top["num_sections"] > runner_up["num_sections"]:
        logger.info(
            "Resolved %s -> conid %s (%s, %s) — primary listing "
            "(%d sections vs %d)",
            ticker, top["conid"], top["name"], top["exchange"],
            top["num_sections"], runner_up["num_sections"],
        )
        return {"conid": top["conid"], "name": top["name"]}

    # Same section count or genuinely ambiguous — return candidates
    company_names = set(c["name"].upper() for c in candidates)
    logger.info(
        "Ambiguous symbol %s: %d candidates across %d company name(s), "
        "top sections=%d",
        ticker, len(candidates), len(company_names),
        top["num_sections"],
    )
    return {
        "error": "ambiguous_symbol",
        "message": (
            f"Multiple matches for {ticker}. Specify which one by "
            f"passing the conid directly."
        ),
        "candidates": [
            {"conid": c["conid"], "name": c["name"], "exchange": c["exchange"]}
            for c in candidates
        ],
    }


# === Order tools ===


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def ib_place_order(
    account: str,
    ticker: str,
    side: str,
    quantity: float = 0,
    cash_amount: float = 0,
    order_type: str = "MKT",
    limit_price: float = 0,
    stop_price: float = 0,
    tif: str = "DAY",
    outside_rth: bool = False,
    conid: int = 0,
) -> dict:
    """Place an order on Interactive Brokers.

    Resolves the ticker to a contract ID, calculates quantity if cash_amount
    is provided, builds the order, and submits it (handling IB's confirmation
    loop automatically).

    IMPORTANT: The calling skill must obtain user confirmation BEFORE calling
    this tool. This tool submits the order immediately.

    Args:
        account: Account name as configured in config.json (e.g., "main",
                 "main-paper").
        ticker: Stock/ETF symbol (e.g., "AVAV", "CSH2", "SPY").
        side: "BUY" or "SELL".
        quantity: Number of shares. Required unless cash_amount is provided.
                  For SELL ALL: set to -1 (the tool fetches current position size).
        cash_amount: Dollar amount to invest (e.g., 9200.0). The tool calculates
                     shares from current price. Mutually exclusive with quantity.
        order_type: "MKT" (market), "LMT" (limit), "STP" (stop),
                    "STP_LIMIT" (stop-limit). Default: "MKT".
        limit_price: Required for LMT and STP_LIMIT orders.
        stop_price: Required for STP and STP_LIMIT orders.
        tif: Time-in-force: "DAY", "GTC", "IOC", "OPG". Default: "DAY".
        outside_rth: Allow fills outside regular trading hours. Default: false.
        conid: Explicit contract ID (skips ticker resolution). Use after
               disambiguation when multiple matches exist for a ticker.

    Returns:
        On success: {"success": true, "order_id": "...", "order_status": "...",
                     "ticker": "...", "side": "...", "quantity": N,
                     "order_type": "...", "account": "...", "mode": "live|paper"}
        On error: {"error": "error_code", "message": "..."}
        On ambiguous ticker: {"error": "ambiguous_symbol", "candidates": [...]}
    """
    logger.info(
        "Tool call: ib_place_order(account=%s, ticker=%s, side=%s, qty=%s, cash=%s, type=%s)",
        account, ticker, side, quantity, cash_amount, order_type,
    )

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    gateway.tickle_all()

    try:
        acct_cfg = get_account_config(cfg, account)
    except ValueError:
        return {"error": "invalid_account", "message": f"Unknown account: {account}"}

    port = acct_cfg["port"]
    account_id = acct_cfg.get("account_id")
    mode = acct_cfg.get("mode", "live")

    if not account_id:
        account_id = gateway.auto_discover_account_id(account)
        if account_id:
            _refresh_cfg()
            acct_cfg = get_account_config(cfg, account)
        else:
            return {
                "error": "account_not_discovered",
                "message": f"Account ID for {account} not yet discovered. Start gateway and authenticate first.",
            }

    ready, auth_error = _filter_ready_accounts([account])
    if auth_error:
        return auth_error

    # Validate inputs
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": "invalid_side", "message": "side must be BUY or SELL"}

    if quantity == 0 and cash_amount == 0:
        return {"error": "no_quantity", "message": "Provide either quantity or cash_amount"}

    if quantity != 0 and cash_amount != 0:
        return {"error": "ambiguous_quantity", "message": "Provide quantity OR cash_amount, not both"}

    order_type = order_type.upper()

    # Resolve symbol to conid (portfolio → search → disambiguate)
    resolution = _resolve_conid_for_order(ticker, account, port, conid)
    if "error" in resolution:
        return resolution
    resolved_conid = resolution["conid"]

    # Handle SELL ALL (quantity == -1)
    if quantity == -1:
        pos_qty = resolution.get("position")
        if pos_qty and pos_qty > 0:
            quantity = pos_qty
        else:
            # Position not found via resolution, try explicit lookup
            try:
                positions = _get_positions_for_account(account)
                pos = next((p for p in positions if p["ticker"] == ticker), None)
                if not pos or pos["position"] <= 0:
                    return {"error": "no_position", "message": f"No {ticker} position found in {account}"}
                quantity = pos["position"]
            except Exception as e:
                return {"error": "position_lookup_failed", "message": str(e)}

    # Handle cash-based ordering
    if cash_amount > 0:
        try:
            price = order_mgr.get_last_price(resolved_conid, port)
            quantity = order_mgr.calculate_shares_from_cash(cash_amount, price)
            logger.info(
                "Cash order: $%.2f / $%.2f = %d shares of %s",
                cash_amount, price, quantity, ticker,
            )
        except ValueError as e:
            return {"error": "cash_calculation_failed", "message": str(e)}

    # Build and submit order
    try:
        payload = order_mgr.build_order_payload(
            conid=resolved_conid,
            side=side,
            quantity=abs(quantity),
            order_type=order_type,
            limit_price=limit_price if limit_price > 0 else None,
            stop_price=stop_price if stop_price > 0 else None,
            tif=tif,
            outside_rth=outside_rth,
            ticker=ticker,
        )
        result = order_mgr.submit_order(port, account_id, payload)
    except ValueError as e:
        return {"error": "order_failed", "message": str(e)}

    return {
        "success": True,
        "order_id": result["order_id"],
        "order_status": result["order_status"],
        "warning_messages": result.get("warning_messages", []),
        "ticker": ticker,
        "side": side,
        "quantity": abs(quantity),
        "order_type": order_type,
        "limit_price": limit_price if limit_price > 0 else None,
        "account": account,
        "account_label": acct_cfg["label"],
        "mode": mode,
    }


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_order_preview(
    account: str,
    ticker: str,
    side: str,
    quantity: float = 0,
    cash_amount: float = 0,
    order_type: str = "MKT",
    limit_price: float = 0,
    stop_price: float = 0,
    conid: int = 0,
) -> dict:
    """Preview an order without submitting (what-if analysis).

    Returns estimated commission, margin impact, and any warnings.
    Use this before ib_place_order to show the user what will happen.

    Args:
        account: Account name.
        ticker: Stock/ETF symbol.
        side: "BUY" or "SELL".
        quantity: Number of shares (or -1 for SELL ALL).
        cash_amount: Dollar amount (alternative to quantity).
        order_type: "MKT", "LMT", "STP", "STP_LIMIT".
        limit_price: For limit orders.
        stop_price: For stop orders.
        conid: Explicit contract ID (skips ticker resolution).

    Returns:
        Preview data including: ticker, side, quantity, estimated_value,
        commission, margin_impact, warnings, last_price (for cash orders).
        On ambiguous ticker: {"error": "ambiguous_symbol", "candidates": [...]}
    """
    logger.info(
        "Tool call: ib_order_preview(account=%s, ticker=%s, side=%s)",
        account, ticker, side,
    )

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    gateway.tickle_all()

    try:
        acct_cfg = get_account_config(cfg, account)
    except ValueError:
        return {"error": "invalid_account", "message": f"Unknown account: {account}"}

    port = acct_cfg["port"]
    account_id = acct_cfg.get("account_id")
    if not account_id:
        account_id = gateway.auto_discover_account_id(account)
        if account_id:
            _refresh_cfg()
            acct_cfg = get_account_config(cfg, account)
        else:
            return {
                "error": "account_not_discovered",
                "message": f"Account ID for {account} not yet discovered.",
            }

    ready, auth_error = _filter_ready_accounts([account])
    if auth_error:
        return auth_error

    side = side.upper()
    order_type = order_type.upper()

    # Resolve symbol to conid (portfolio → search → disambiguate)
    resolution = _resolve_conid_for_order(ticker, account, port, conid)
    if "error" in resolution:
        return resolution
    resolved_conid = resolution["conid"]

    # Handle SELL ALL
    resolved_quantity = quantity
    if quantity == -1:
        pos_qty = resolution.get("position")
        if pos_qty and pos_qty > 0:
            resolved_quantity = pos_qty
        else:
            try:
                positions = _get_positions_for_account(account)
                pos = next((p for p in positions if p["ticker"] == ticker), None)
                if not pos or pos["position"] <= 0:
                    return {"error": "no_position", "message": f"No {ticker} position in {account}"}
                resolved_quantity = pos["position"]
            except Exception as e:
                return {"error": "position_lookup_failed", "message": str(e)}

    # Handle cash-based
    last_price = None
    if cash_amount > 0:
        try:
            last_price = order_mgr.get_last_price(resolved_conid, port)
            resolved_quantity = order_mgr.calculate_shares_from_cash(cash_amount, last_price)
        except ValueError as e:
            return {"error": "cash_calculation_failed", "message": str(e)}

    # Build payload and preview
    try:
        payload = order_mgr.build_order_payload(
            conid=resolved_conid,
            side=side,
            quantity=abs(resolved_quantity),
            order_type=order_type,
            limit_price=limit_price if limit_price > 0 else None,
            stop_price=stop_price if stop_price > 0 else None,
            ticker=ticker,
        )
        preview = order_mgr.preview_order(port, account_id, payload)
    except ValueError as e:
        return {"error": "preview_failed", "message": str(e)}

    preview["ticker"] = ticker
    preview["side"] = side
    preview["quantity"] = abs(resolved_quantity)
    preview["order_type"] = order_type
    preview["account"] = account
    preview["account_label"] = acct_cfg["label"]
    preview["mode"] = acct_cfg.get("mode", "live")
    if last_price:
        preview["last_price"] = last_price
        preview["cash_amount_requested"] = cash_amount
        preview["estimated_value"] = abs(resolved_quantity) * last_price
        preview["cash_remainder"] = cash_amount - (abs(resolved_quantity) * last_price)
    return preview


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_order_status(account: str = "all") -> dict:
    """List live and recent orders.

    Args:
        account: Account name or "all". Default: "all".

    Returns:
        {"accounts": {"account_name": {"orders": [...]}}}
    """
    logger.info("Tool call: ib_order_status(account=%s)", account)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    gateway.tickle_all()

    accounts = _resolve_accounts(account)
    # For order status, also allow explicit paper accounts
    if account not in ("all", "") and account not in accounts:
        accounts = [account]

    result = {}
    for acct in accounts:
        try:
            acct_cfg = get_account_config(cfg, acct)
        except ValueError:
            result[acct] = {"error": f"Unknown account: {acct}"}
            continue
        port = acct_cfg["port"]
        status = gateway.get_status(acct)
        if not status["authenticated"]:
            result[acct] = {"error": "Not authenticated"}
            continue
        orders = order_mgr.get_live_orders(port)
        result[acct] = orders

    return {"accounts": result}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def ib_cancel_order(account: str, order_id: str) -> dict:
    """Cancel a live order.

    Args:
        account: Account name (must be specific, not "all").
        order_id: Order ID from ib_place_order or ib_order_status.

    Returns:
        Cancellation confirmation or error.
    """
    logger.info("Tool call: ib_cancel_order(account=%s, order_id=%s)", account, order_id)

    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    gateway.tickle_all()

    if account == "all":
        return {
            "error": "account_required",
            "message": "Must specify a single account for cancellation.",
        }

    try:
        acct_cfg = get_account_config(cfg, account)
    except ValueError:
        return {"error": "invalid_account", "message": f"Unknown account: {account}"}

    port = acct_cfg["port"]
    account_id = acct_cfg.get("account_id")
    if not account_id:
        account_id = gateway.auto_discover_account_id(account)
        if account_id:
            _refresh_cfg()
            acct_cfg = get_account_config(cfg, account)
        else:
            return {
                "error": "account_not_discovered",
                "message": f"Account ID for {account} not yet discovered.",
            }

    ready, auth_error = _filter_ready_accounts([account])
    if auth_error:
        return auth_error

    result = order_mgr.cancel_order(port, account_id, order_id)
    result["account"] = account
    result["account_label"] = acct_cfg["label"]
    return result


# === Performance tools ===


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def ib_performance(account: str = "all", periods: str = "") -> dict:
    """Get time-weighted return (TWR) performance for accounts.

    Returns cumulative returns, monthly returns, and NAV series from the
    IB Portfolio Analyst API. Data is computed by IB from actual account
    history including dividends, fees, and FX effects.

    Args:
        account: Account name (e.g., "account1", "account2") or "all". Default: "all".
        periods: Comma-separated periods to fetch. Valid: 1D, 7D, MTD, 1M, YTD, 1Y.
                 Default (empty): fetches all periods.
    """
    logger.info("Tool call: ib_performance(account=%s, periods=%s)", account, periods)
    startup_error = _check_startup_ready()
    if startup_error:
        return startup_error

    accounts = _resolve_accounts(account)
    ready, auth_error = _filter_ready_accounts(accounts)
    if auth_error:
        return auth_error

    period_list = [p.strip() for p in periods.split(",") if p.strip()] if periods else None

    all_results = {}
    for acct in ready:
        acct_cfg = get_account_config(cfg, acct)
        perf = perf_mgr.get_performance(acct, period_list)
        all_results[acct] = {
            "account_id": acct_cfg.get("account_id", ""),
            "label": acct_cfg["label"],
            "periods": perf,
        }

    # Build a summary table for quick reading
    summary = {}
    for acct, data in all_results.items():
        summary[acct] = {
            p: data["periods"][p].get("cumulative_return_pct", "N/A")
            for p in data["periods"]
            if "error" not in data["periods"][p]
        }

    return {
        "accounts": all_results,
        "summary": summary,
        "valid_periods": list(VALID_PERIODS),
        "performance_method": "TWR",
    }


# === Entry point ===

def main():
    """Run the MCP server."""
    logger.info("Starting ib-connect MCP server")

    # Log first-run message
    if not Path("~/.ib-connect/config.json").expanduser().exists():
        logger.info("First run: default config created at ~/.ib-connect/config.json")

    mcp.run(transport="stdio")
