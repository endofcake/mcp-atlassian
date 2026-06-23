# MCP Atlassian Helm Chart

This Helm chart deploys the [MCP Atlassian](https://github.com/sooperset/mcp-atlassian) server to Kubernetes, providing a Model Context Protocol (MCP) server for Jira and Confluence integration.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Atlassian Cloud or Server/Data Center instance
- API tokens or OAuth credentials

## Installation

### Quick Start (Cloud with API Tokens)

```bash
# Create values file
cat > my-values.yaml <<YAML
confluence:
  url: "https://your-company.atlassian.net/wiki"
  username: "your.email@company.com"
  apiToken: "your_confluence_api_token"

jira:
  url: "https://your-company.atlassian.net"
  username: "your.email@company.com"
  apiToken: "your_jira_api_token"
YAML

# Install the chart
helm install mcp-atlassian ./mcp-atlassian -f my-values.yaml
```

### Validate the chart

```bash
helm lint mcp-atlassian/
```

### Test installation

```bash
helm install mcp-atlassian ./mcp-atlassian \
  --set confluence.url="https://your-company.atlassian.net/wiki" \
  --set confluence.username="user@example.com" \
  --set confluence.apiToken="token" \
  --set jira.url="https://your-company.atlassian.net" \
  --set jira.username="user@example.com" \
  --set jira.apiToken="token" \
  --dry-run --debug
```

## Configuration

See the `values.yaml` file for all configuration options.

### Key Configuration Options

- **authMode**: `api-token`, `personal-token`, `oauth`, or `byot`
- **transport**: `stdio`, `sse`, or `streamable-http`
- **confluence/jira.enabled**: Enable/disable Confluence or Jira integration
- **config.readOnlyMode**: Disable all write operations
- **persistence.enabled**: Enable OAuth token persistence
- **oauthProxy.enabled**: Expose MCP OAuth discovery + DCR routes (opt-in)
- **oauthClientStorage.mode**: `default` (FastMCP storage) or `factory` (custom)
- **tls.enabled**: Serve the HTTP transports over HTTPS from an existing cert Secret

### Server TLS (HTTPS listener)

Serve the `sse`/`streamable-http` transports over HTTPS by terminating TLS at the
MCP server itself (for end-to-end encryption to the pod). The chart does **not**
create or manage certificates — supply an existing Secret (for example one issued
by cert-manager) holding the cert and key:

```yaml
tls:
  enabled: true
  secretName: mcp-atlassian-tls   # existing Secret with tls.crt / tls.key
  # certFileKey: tls.crt          # key within the Secret holding the cert
  # keyFileKey: tls.key           # key within the Secret holding the private key
  # mountPath: /etc/mcp-atlassian/tls
  # defaultMode: 288              # 0440 octal in decimal — owner+group read only
```

The Secret is mounted read-only and passed to the server via `--ssl-certfile` /
`--ssl-keyfile`. When enabled, the readiness probe automatically switches to
`scheme: HTTPS`; set `readinessProbe.httpGet.scheme` explicitly to override. The
render fails fast if `secretName` is empty or the transport is `stdio`.

### OAuth Proxy + DCR (opt-in)

Enable OAuth proxy/DCR endpoints:

```yaml
oauthProxy:
  enabled: true
  requireConsent: true
  allowedClientRedirectUris: "https://chatgpt.com/connector_platform_oauth_redirect,http://localhost:*"
  allowedGrantTypes: "authorization_code,refresh_token"
```

This sets:

- `ATLASSIAN_OAUTH_PROXY_ENABLE`
- `ATLASSIAN_OAUTH_REQUIRE_CONSENT`
- `ATLASSIAN_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS`
- `ATLASSIAN_OAUTH_ALLOWED_GRANT_TYPES`

### Custom OAuth Client Storage (factory mode)

For advanced deployments, you can provide a custom storage backend factory
without changing core chart API:

```yaml
oauthClientStorage:
  mode: factory
  factory:
    importPath: "my_pkg.storage:create_store"
    configJsonSecret:
      name: mcp-atlassian-storage-config
      key: config.json
```

This sets:

- `ATLASSIAN_OAUTH_CLIENT_STORAGE_MODE=factory`
- `ATLASSIAN_OAUTH_CLIENT_STORAGE_FACTORY`
- `ATLASSIAN_OAUTH_CLIENT_STORAGE_CONFIG_JSON` (optional)

The factory callable should return an async key/value compatible storage object
used by FastMCP OAuth proxy client registration storage.

### Health Checks and Readiness Probe

The MCP server exposes a `/healthz` endpoint that returns `{"status": "ok"}` for Kubernetes health checks. This endpoint is automatically used for the readiness probe when using HTTP transport modes (`sse` or `streamable-http`).

> **Note:** The readiness probe is only enabled for HTTP transports. When using `stdio` transport, no HTTP server is exposed, so the probe is disabled.

Default readiness probe configuration in `values.yaml`:

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: http
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 3
```

You can customize these values in your `values.yaml` file:

```yaml
readinessProbe:
  httpGet:
    path: /healthz
    port: http
  initialDelaySeconds: 15
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 5
```

## Upgrading

```bash
helm upgrade mcp-atlassian ./mcp-atlassian -f my-values.yaml
```

## Uninstalling

```bash
helm uninstall mcp-atlassian
```

## Support

For issues with the MCP Atlassian server, see https://github.com/sooperset/mcp-atlassian

## License

MIT License
