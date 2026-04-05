# MCP Servers

Collection of MCP (Model Context Protocol) servers used across BondLedger tooling.

## Servers

| Server | Description | Type |
|--------|-------------|------|
| [ib-connect](ib-connect/) | Interactive Brokers Client Portal Gateway — portfolio data, market data, order execution | Custom (Python/FastMCP) |
| [linear](linear/) | Linear issue tracking — issues, projects, milestones, documents | Hosted (official Linear MCP via `mcp-remote`) |

## Quick Start

See each server's README for installation and configuration instructions.

### ib-connect

Custom Python server managing IB Client Portal Gateway lifecycle, authentication, and data retrieval across multiple accounts.

```bash
git clone https://github.com/BondLedgerGmbH/mcp.git
# Follow ib-connect/README.md for setup
```

### linear

Uses the official Linear MCP server — no custom code required. Add to your MCP config:

```json
{
  "mcpServers": {
    "linear": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.linear.app/sse"]
    }
  }
}
```

## License

MIT
