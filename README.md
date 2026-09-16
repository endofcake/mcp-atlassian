# MCP Atlassian

[![Run Tests](https://github.com/endofcake/mcp-atlassian/actions/workflows/tests.yml/badge.svg)](https://github.com/endofcake/mcp-atlassian/actions/workflows/tests.yml)
![License](https://img.shields.io/github/license/endofcake/mcp-atlassian)

Model Context Protocol (MCP) server for Atlassian products: Jira, Confluence,
and Bitbucket Data Center. Jira and Confluence work on both Cloud and
Server/Data Center. Bitbucket support covers Data Center.

## About this fork

This is a fork of [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian)
that adds **Bitbucket Data Center** as a third service. The 25 Bitbucket tools
cover projects, repositories, branches and tags, commits, source browsing, and
pull-request review. Install from this repository with `uvx --from git+...`
or build the container image yourself (see [Installation](docs/installation.mdx)).

Bitbucket bugs, and bugs in this fork, belong in
[this repository's issues](https://github.com/endofcake/mcp-atlassian/issues).
Jira and Confluence bugs that also reproduce on upstream belong upstream.

<details>
<summary>Confluence Demo</summary>

https://github.com/user-attachments/assets/7fe9c488-ad0c-4876-9b54-120b666bb785

</details>

## Quick Start

### 1. Get credentials

- **Jira / Confluence Cloud:** create an API token at
  https://id.atlassian.com/manage-profile/security/api-tokens.
- **Jira / Confluence Server/Data Center:** create a Personal Access Token in
  your profile settings.
- **Bitbucket Data Center:** obtain an OAuth 2.0 access token from an
  application link registered on the instance. See
  [Bitbucket Data Center](docs/authentication.mdx#bitbucket-data-center) for
  the other token sources.

### 2. Configure your IDE

Add to your Claude Desktop or Cursor MCP configuration. Jira and Confluence
Cloud with API tokens:

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/endofcake/mcp-atlassian", "mcp-atlassian"],
      "env": {
        "JIRA_URL": "https://your-company.atlassian.net",
        "JIRA_USERNAME": "your.email@company.com",
        "JIRA_API_TOKEN": "your_api_token",
        "CONFLUENCE_URL": "https://your-company.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "your.email@company.com",
        "CONFLUENCE_API_TOKEN": "your_api_token"
      }
    }
  }
}
```

> **Server/Data Center users**: Use `JIRA_PERSONAL_TOKEN` instead of
> `JIRA_USERNAME` + `JIRA_API_TOKEN` (same for Confluence).

Bitbucket Data Center with an access token:

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/endofcake/mcp-atlassian", "mcp-atlassian"],
      "env": {
        "BITBUCKET_URL": "https://bitbucket.your-company.com",
        "BITBUCKET_OAUTH_ACCESS_TOKEN": "your_bitbucket_access_token"
      }
    }
  }
}
```

All three services can be configured in one server. Each is enabled by the
presence of its `*_URL`.

#### Autohand Code

Use the same `uvx` server with your Atlassian credentials:

```bash
autohand mcp add mcp-atlassian env \
  JIRA_URL=https://your-company.atlassian.net \
  JIRA_USERNAME=your.email@company.com \
  JIRA_API_TOKEN=your_api_token \
  CONFLUENCE_URL=https://your-company.atlassian.net/wiki \
  CONFLUENCE_USERNAME=your.email@company.com \
  CONFLUENCE_API_TOKEN=your_api_token \
  uvx --from git+https://github.com/endofcake/mcp-atlassian mcp-atlassian
```

Add `--scope project` after `add` to keep the configuration in the current
project. See [Autohand Code](https://github.com/autohandai/code-cli/) for current
installation and CLI details.

### 3. Start using

Ask your AI assistant to:
- **"Find issues assigned to me in PROJ project"**
- **"Search Confluence for onboarding docs"**
- **"Create a bug ticket for the login issue"**
- **"List open pull requests in PROJ/my-repo and summarise the diff of #42"**

## Documentation

Documentation lives in this repository under [`docs/`](docs/).

| Topic | Description |
|-------|-------------|
| [Installation](docs/installation.mdx) | uvx from git, Docker (self-built), from source |
| [Authentication](docs/authentication.mdx) | API tokens, PAT, OAuth 2.0, the OAuth proxy, Bitbucket Data Center |
| [Configuration](docs/configuration.mdx) | IDE setup, environment variables |
| [HTTP Transport](docs/http-transport.mdx) | SSE, streamable-http, multi-user |
| [Tools Reference](docs/tools-reference.mdx) | All Jira, Confluence & Bitbucket tools |
| [Troubleshooting](docs/troubleshooting.mdx) | Common issues & debugging |
| [Helm chart](helm/README.md) | Kubernetes deployment, including Bitbucket |

## Compatibility

| Product | Deployment | Support |
|---------|------------|---------|
| Confluence | Cloud | Fully supported |
| Confluence | Server/Data Center | Supported (v6.0+) |
| Jira | Cloud | Fully supported |
| Jira | Server/Data Center | Supported (v8.14+) |
| Bitbucket | Cloud | Not supported |
| Bitbucket | Data Center | Supported (v9.0+, OAuth 2.0) |

## Key Tools

| Jira | Confluence | Bitbucket (Data Center) |
|------|------------|-------------------------|
| `jira_search` - Search with JQL | `confluence_search` - Search with CQL | `bitbucket_list_pull_requests` - List PRs |
| `jira_get_issue` - Get issue details | `confluence_get_page` - Get page content | `bitbucket_get_pull_request_diff` - Get PR diff |
| `jira_create_issue` - Create issues | `confluence_create_page` - Create pages | `bitbucket_add_pull_request_comment` - Comment on PRs |
| `jira_update_issue` - Update issues | `confluence_update_page` - Update pages | `bitbucket_browse_path` - Browse source |
| `jira_transition_issue` - Change status | `confluence_add_comment` - Add comments | `bitbucket_list_commits` - List commits |

**123 tools total** — See the [Tools Reference](docs/tools-reference.mdx) for the complete list.

## Security

Never share API tokens. Keep `.env` files secure. See [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## License

MIT - See [LICENSE](LICENSE). Not an official Atlassian product.
