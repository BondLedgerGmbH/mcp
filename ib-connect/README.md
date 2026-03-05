# ib-connect

MCP server for Interactive Brokers Client Portal Gateway. Manages gateway lifecycle, authentication, and portfolio/market data retrieval across multiple accounts.

## Features

- Multi-account gateway management (start, stop, re-authenticate)
- Auto-discovery of IB account IDs on first connection
- Portfolio data: positions, balances, allocations, concentration flags
- Market data: multi-symbol snapshots with inverse ETF metadata
- Option chains: filtered by strike range with live pricing, IV, and greeks
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
      "tax_treatment": "no_capital_gains_tax"
    }
  }
}
```

The `account_id` field is auto-discovered on first successful authentication. Leave it empty.

For multiple accounts, assign different ports (e.g., 5100, 5101). Avoid ports 5000/5001 on macOS (AirPlay Receiver conflict).

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

## Known Data Limitations

The IB Client Portal Gateway snapshot API has these limitations that consumers should be aware of:

- **`implied_vol`**: Returned as a negative number by the CP Gateway. Take `abs(implied_vol)` to get the actual IV (e.g., -0.221 means 22.1% IV).
- **`theta` and `vega`**: Always null. The snapshot endpoint does not return these greeks.
- **`open_interest`**: Always null. Not provided by the snapshot endpoint.

## Startup Behaviour

On MCP server connect, the server automatically:

1. Starts gateway instances for all configured accounts
2. Opens login pages in the default browser
3. Polls for authentication completion (up to 5 minutes)
4. Auto-discovers account IDs if not configured
5. Warms up the portfolio API session
6. Pulls and caches initial portfolio data

Data tools block until this startup sequence completes.

## License

MIT
