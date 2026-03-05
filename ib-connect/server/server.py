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

# --- Startup readiness gate ---
# Data tools block until the full startup workflow completes:
# gateways running → authenticated → account IDs discovered → portfolio cached

_startup_ready = threading.Event()
_startup_phase = "initializing"
_startup_error = None


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
# 3. Poll until all accounts are authenticated (up to 5 minutes)
# 4. Auto-discover account IDs
# 5. Warm up portfolio API session
# 6. Pull and cache initial portfolio summary
# 7. Set _startup_ready
def _auto_start_gateways():
    global _startup_error
    try:
        all_accounts = get_all_account_names(cfg)

        # Phase 1: Start gateways and open login pages
        _set_phase("starting_gateways")
        needs_auth = []
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
                    _startup_error = f"Gateway start failed for {acct}: {result.get('error')}"
                    _set_phase("error")
                    return
            elif not status["authenticated"]:
                gateway.open_login_page(acct)
                needs_auth.append(acct)
                logger.info("Gateway running but not authenticated for %s, opened login page", acct)
            else:
                logger.info("Gateway already running and authenticated for %s", acct)

        # Phase 2: Poll for auth completion (up to 5 minutes)
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

            if pending:
                _startup_error = f"Auth timed out after {max_wait}s for: {', '.join(pending)}"
                _set_phase("error")
                return

        logger.info("All accounts authenticated")

        # Phase 3: Auto-discover account IDs and warm up portfolio API
        _set_phase("discovering_accounts")
        for acct in all_accounts:
            acct_cfg = get_account_config(cfg, acct)
            if not acct_cfg.get("account_id"):
                discovered = gateway.auto_discover_account_id(acct)
                if discovered:
                    logger.info("Discovered account ID for %s: %s", acct, discovered)
                else:
                    _startup_error = f"Could not discover account ID for {acct}"
                    _set_phase("error")
                    return
            else:
                # Warm up the portfolio API session even if account_id is known
                port = acct_cfg["port"]
                try:
                    http.get(f"https://localhost:{port}/v1/api/portfolio/accounts")
                    logger.info("Portfolio API warmed up for %s", acct)
                except Exception:
                    pass

        # Phase 4: Pull and cache initial portfolio summary
        _set_phase("loading_portfolio")

        # Reload config in case account IDs were just discovered
        fresh_cfg = load_config()
        cfg.update(fresh_cfg)
        gateway.cfg = cfg
        portfolio.cfg = cfg

        result = portfolio.get_full_summary(cfg, all_accounts, force_refresh=True)

        # Validate
        balances = result.get("balances", {})
        has_nonzero_nav = any(
            b.get("nav", 0) != 0
            for k, b in balances.items()
            if k != "combined"
        )
        if not has_nonzero_nav:
            _startup_error = "Portfolio loaded but all accounts show zero NAV"
            _set_phase("error")
            return

        total_positions = len(result.get("positions", []))
        total_nav = balances.get("combined", {}).get("total_nav", 0)
        logger.info("Portfolio loaded: %d positions, NAV $%.2f", total_positions, total_nav)

        # Done
        _set_phase("ready")
        _startup_ready.set()
        logger.info("Startup complete: all gateways running, authenticated, portfolio cached")

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
    instructions="Interactive Brokers portfolio data server. Manages gateway lifecycle, authentication, and portfolio data retrieval across multiple accounts."
)


def _resolve_accounts(account: str) -> list:
    """Resolve account parameter to list of account names."""
    if account == "all" or not account:
        return get_all_account_names(cfg)
    return [account]


def _check_auth_error(accounts: list) -> dict | None:
    """Check if any accounts need authentication. Return error dict or None."""
    needs_auth = []
    not_running = []
    for acct in accounts:
        status = gateway.get_status(acct)
        if not status["gateway_running"]:
            not_running.append(acct)
        elif not status["authenticated"]:
            needs_auth.append(acct)

    if not_running:
        labels = [get_account_config(cfg, a).get("label", a) for a in not_running]
        return {
            "error": "gateway_not_running",
            "accounts_not_running": not_running,
            "message": f"Gateway not running for: {', '.join(labels)}. Use ib_start_gateway to start."
        }
    if needs_auth:
        labels = [get_account_config(cfg, a).get("label", a) for a in needs_auth]
        urls = [f"https://localhost:{get_account_config(cfg, a)['port']}" for a in needs_auth]
        return {
            "error": "auth_required",
            "accounts_needing_auth": needs_auth,
            "message": f"Authentication required for: {', '.join(labels)}. Use ib_reauthenticate to open login pages."
        }
    return None


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

    # Tickle and check auth
    gateway.tickle_all()
    auth_error = _check_auth_error(accounts)
    if auth_error:
        return auth_error

    all_positions = []
    for acct in accounts:
        positions = portfolio.get_positions(acct)
        all_positions.extend(positions)

    return {
        "positions": all_positions,
        "total_positions": len(all_positions),
        "accounts_queried": accounts
    }


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

    # Tickle and check auth
    gateway.tickle_all()
    auth_error = _check_auth_error(accounts)
    if auth_error:
        return auth_error

    all_balances = {}
    for acct in accounts:
        balances = portfolio.get_balances(acct)
        if balances:
            all_balances[acct] = balances

    combined = {
        "total_nav": sum(b.get("nav", 0) for b in all_balances.values()),
        "total_cash": sum(b.get("cash", 0) for b in all_balances.values()),
        "total_positions_value": sum(b.get("total_positions_value", 0) for b in all_balances.values()),
    }

    return {
        "accounts": all_balances,
        "combined": combined
    }


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

    # Tickle and check auth
    gateway.tickle_all()
    auth_error = _check_auth_error(accounts)
    if auth_error:
        return auth_error

    # Auto-discover account IDs if needed
    for acct in accounts:
        acct_cfg = get_account_config(cfg, acct)
        if not acct_cfg.get("account_id"):
            discovered = gateway.auto_discover_account_id(acct)
            if not discovered:
                return {
                    "error": "account_id_missing",
                    "message": f"Could not auto-discover account ID for {acct}. "
                               f"Set it manually in ~/.ib-connect/config.json."
                }

    result = portfolio.get_full_summary(cfg, accounts, force_refresh)

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
            "message": "Portfolio data returned but all accounts show zero NAV. "
                       "Verify accounts are funded and gateway is returning correct data."
        }

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
    auth_error = _check_auth_error(accounts[:1])
    if auth_error:
        return auth_error

    port = get_account_config(cfg, accounts[0])["port"]

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
    auth_error = _check_auth_error(accounts[:1])
    if auth_error:
        return auth_error

    port = get_account_config(cfg, accounts[0])["port"]

    result = market_data.get_quotes(symbols, port)

    # Enrich inverse ETFs with metadata
    snapshots = result.get("snapshots", {})
    for sym, data in snapshots.items():
        if sym in INVERSE_ETF_INFO:
            data["inverse_etf_info"] = INVERSE_ETF_INFO[sym]

    return result


# === Entry point ===

def main():
    """Run the MCP server."""
    logger.info("Starting ib-connect MCP server")

    # Log first-run message
    if not Path("~/.ib-connect/config.json").expanduser().exists():
        logger.info("First run: default config created at ~/.ib-connect/config.json")

    mcp.run(transport="stdio")
