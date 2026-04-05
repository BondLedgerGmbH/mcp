# ib-connect

MCP server for Interactive Brokers Client Portal Gateway. Manages gateway lifecycle, authentication, portfolio/market data retrieval, and order execution across multiple accounts. Supports both live and paper trading modes.

## Features

- Multi-account gateway management (start, stop, re-authenticate)
- Live and paper trading modes (paper accounts excluded from auto-start and "all" resolution)
- Auto-discovery of IB account IDs on first connection
- Portfolio data: positions, balances, allocations, concentration flags
- Market data: multi-symbol snapshots with inverse ETF metadata
- Option chains: filtered by strike range with live pricing, IV, and greeks
- Order management: place, preview (what-if), status, and cancel orders
- Smart ticker resolution: portfolio positions → IB search → disambiguation
- Cash-based ordering (specify dollar amount instead of shares)
- Portfolio caching with configurable TTL
- FX rate conversion for multi-currency portfolios
- Gateway auto-update with rollback support

## Requirements

- Python 3.12+
- Java (for IB Client Portal Gateway): `brew install openjdk`
- [FastMCP](https://github.com/jlowin/fastmcp) 3.0+
- An Interactive Brokers account

## Installation

```bash
# Clone to ~/.ib-connect
git clone https://github.com/BondLedgerGmbH/ib-connect.git ~/.ib-connect

# Create virtual environment and install dependencies
cd ~/.ib-connect
python3 -m venv venv
source venv/bin/activate
pip install fastmcp requests
```

## Configuration

Copy the example config and edit it:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Edit `config.json` to add your account(s):

```json
{
  "accounts": {
    "main": {
      "port": 5100,
      "account_id": "",
      "label": "My Account",
      "type": "individual",
      "tax_treatment": "no_capital_gains_tax",
      "mode": "live"
    },
    "main-paper": {
      "port": 5200,
      "account_id": "",
      "label": "My Account (Paper)",
      "type": "individual",
      "tax_treatment": "no_capital_gains_tax",
      "mode": "paper",
      "live_account": "main",
      "auto_start": false
    }
  }
}
```

- `account_id` is auto-discovered on first successful authentication. Leave it empty.
- `mode`: `"live"` (default) or `"paper"`. Paper accounts are excluded from `"all"` account resolution and auto-start.
- `auto_start`: Set to `false` for accounts that should not start automatically on MCP connect.
- For multiple accounts, assign different ports (e.g., 5100, 5101). Avoid ports 5000/5001 on macOS (AirPlay Receiver conflict).

## Claude Desktop / Claude Code Setup

Add to your MCP configuration:

```json
{
  "mcpServers": {
    "ib-connect": {
      "command": "/path/to/python",
      "args": ["~/.ib-connect/run_server.py"],
      "env": {}
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `ib_status` | Check gateway and authentication status |
| `ib_start_gateway` | Start gateway instance(s) and open login pages |
| `ib_stop_gateway` | Stop gateway instance(s) |
| `ib_reauthenticate` | Re-open login page for expired sessions |
| `ib_portfolio_positions` | Retrieve current positions |
| `ib_portfolio_balances` | Retrieve account balances and NAV |
| `ib_portfolio_summary` | Combined view: positions + balances + allocations + concentration flags |
| `ib_option_chain` | Filtered option chain with live pricing, IV, and greeks |
| `ib_market_snapshot` | Market data snapshots for multiple symbols |
| `ib_place_order` | Place an order (market, limit, stop, stop-limit) |
| `ib_order_preview` | Preview order impact without submitting (what-if analysis) |
| `ib_order_status` | List live and recent orders |
| `ib_cancel_order` | Cancel a live order |

## Known Data Limitations

The IB Client Portal Gateway snapshot API has these limitations that consumers should be aware of:

- **`implied_vol`**: Returned as a negative number by the CP Gateway. Take `abs(implied_vol)` to get the actual IV (e.g., -0.221 means 22.1% IV).
- **`theta` and `vega`**: Always null. The snapshot endpoint does not return these greeks.
- **`open_interest`**: Always null. Not provided by the snapshot endpoint.

### Architecture

```
Claude Code
    │
    │ MCP (stdio transport)
    ▼
ib-connect MCP Server (Python/FastMCP)
    │
    │ HTTPS (self-signed cert)
    ▼
IB Client Portal Gateway (Java)  ×N instances (one per account)
    │              │
    │ port 5100    │ port 5101    ...
    ▼              ▼
Account A      Account B      ...
(live)         (live/paper)
```

### Directory Structure

```
~/.ib-connect/
├── run_server.py              # Entry point
├── config.json                # Account config (generated from example)
├── config.example.json        # Template config
├── server/
│   ├── __init__.py
│   ├── __main__.py
│   ├── server.py              # FastMCP tool definitions (13 tools)
│   ├── config.py              # Config management
│   ├── gateway.py             # Gateway lifecycle (start/stop/auth/update)
│   ├── http_client.py         # Rate-limited HTTP client for IB API
│   ├── market_data.py         # Security search, option chains, snapshots
│   ├── orders.py              # Order placement, preview, status, cancellation
│   └── portfolio.py           # Positions, balances, allocations, caching
├── gateway/                   # IB CP Gateway base installation (Java)
├── gateway-<account>/         # Per-account gateway copies (created on first start)
├── cache/                     # Portfolio data cache (configurable TTL)
├── logs/                      # Server and gateway logs
├── pids/                      # Gateway PID files
└── venv/                      # Python virtual environment
```

## Startup Behaviour

On MCP server connect, the server automatically:

1. Starts gateway instances for all accounts with `auto_start: true` (default)
2. Opens login pages in the default browser
3. Polls for authentication completion (up to 5 minutes)
4. Auto-discovers account IDs if not configured
5. Warms up the portfolio API session
6. Pulls and caches initial portfolio data

Data tools block until this startup sequence completes.

### Market Data Notes

- The snapshot endpoint has a subscription warm-up: first call may return stale/empty data, second call after ~1.5s returns live data. The server handles this with automatic retry (up to 3 attempts).
- After data retrieval, subscriptions are cleaned up via `unsubscribeall` to free slots.
- Maximum ~100 concurrent market data subscriptions per gateway session.

Option chain retrieval pipeline:
1. Symbol search → conid resolution (cached)
2. Available strikes for expiry month
3. Filter strikes to specified % range of current price (capped at 30)
4. Resolve individual option contract conids
5. Batch market data snapshots (groups of 20)
6. Combine and return structured data with greeks

### Gateway Update Management

The server checks for gateway updates every 24 hours (HEAD request to IB download URL). Updates are applied via `ib_start_gateway` with automatic rollback if the new version fails to start.

### Logs

- Server log: `~/.ib-connect/logs/mcp-server.log`
- Gateway logs: `~/.ib-connect/logs/gateway-{account}.log`
- Log rotation at 10MB

### Troubleshooting

| Issue | Solution |
|-------|----------|
| "startup_in_progress" on tool calls | Wait for auth to complete, or check logs for errors |
| "gateway_not_running" | Run `ib_start_gateway` |
| "auth_required" | Run `ib_reauthenticate`, complete browser login |
| Option chain returns no data | Check market hours, verify underlying is optionable |
| Snapshot returns stale "C"-prefixed values | Market is closed; data is closing prices |
| Port conflict | Check for other processes; disable AirPlay Receiver if on 5000/5001 |
| Paper account not starting | Paper accounts have `auto_start: false` — start manually |

## License

MIT
