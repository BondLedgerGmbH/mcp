# linear

MCP server for Linear issue tracking. Uses the official [Linear MCP server](https://mcp.linear.app) via `mcp-remote` proxy — no custom code required.

## Features

- Create, update, and search issues
- Manage projects, milestones, and cycles
- List teams, users, and labels
- Document management
- Attachment handling

## Requirements

- Node.js (for `npx`)
- A Linear account (OAuth consent on first use)

## Claude Desktop / Claude Code Setup

Add to your MCP configuration (`.mcp.json` or settings):

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

On first use, a browser window opens for Linear OAuth consent. After authorizing, the token is cached locally by `mcp-remote`.

## Tools

| Tool | Description |
|------|-------------|
| `get_issue` | Get issue details by ID |
| `save_issue` | Create or update an issue |
| `list_issues` | List issues with filters |
| `get_issue_status` | Get issue workflow status |
| `list_issue_statuses` | List available statuses |
| `create_issue_label` | Create a new label |
| `list_issue_labels` | List issue labels |
| `list_comments` | List comments on an issue |
| `save_comment` | Add or update a comment |
| `delete_comment` | Delete a comment |
| `get_project` | Get project details |
| `list_projects` | List projects |
| `save_project` | Create or update a project |
| `list_project_labels` | List project labels |
| `get_milestone` | Get milestone details |
| `list_milestones` | List milestones |
| `save_milestone` | Create or update a milestone |
| `list_cycles` | List cycles |
| `get_team` | Get team details |
| `list_teams` | List teams |
| `get_user` | Get user details |
| `list_users` | List users |
| `get_document` | Get document details |
| `list_documents` | List documents |
| `create_document` | Create a document |
| `update_document` | Update a document |
| `search_documentation` | Search documentation |
| `get_attachment` | Get attachment details |
| `create_attachment` | Create an attachment |
| `delete_attachment` | Delete an attachment |
| `extract_images` | Extract images from content |

## License

MIT
