# MCP Atlassian Helm Chart

This Helm chart deploys the [MCP Atlassian](https://github.com/endofcake/mcp-atlassian) server to Kubernetes, providing a Model Context Protocol (MCP) server for Jira, Confluence, and Bitbucket Data Center integration.

Build the image from the repository (`docker build -t <registry>/mcp-atlassian:<tag> .`), push it to a registry your cluster can reach, and set `image.repository` and `image.tag`. The default `image.repository` is the name the repository's `docker-publish` workflow pushes to when it runs there.

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

- **authMode**: `api-token`, `personal-token`, `oauth`, `byot`, or `external`
- **transport**: `stdio`, `sse`, or `streamable-http`
- **confluence/jira.enabled**: Enable/disable Confluence or Jira integration
- **bitbucket.enabled**: Enable Bitbucket Data Center integration (see below)
- **config.readOnlyMode**: Disable all write operations
- **persistence.enabled**: Persist OAuth tokens (top-level or Bitbucket `oauth` mode)
- **oauthProxy.enabled**: Expose MCP OAuth discovery + DCR routes (opt-in)
- **oauthClientStorage.mode**: `default` (FastMCP storage) or `factory` (custom)
- **image.repository / image.tag**: The image to run (see the note above)

### Timeouts and fetcher concurrency

Each service accepts an optional per-service HTTP timeout, and Jira and
Bitbucket accept a cap on concurrent fetcher calls offloaded to worker
threads. Both are omitted from the pod when left empty, so the server's own
defaults apply (75 seconds and 8 workers).

| Value | Environment variable | Default |
|-------|----------------------|---------|
| `confluence.timeout` | `CONFLUENCE_TIMEOUT` | server default, 75 s |
| `jira.timeout` | `JIRA_TIMEOUT` | server default, 75 s |
| `jira.fetcherMaxWorkers` | `JIRA_FETCHER_MAX_WORKERS` | server default, 8 |
| `bitbucket.timeout` | `BITBUCKET_TIMEOUT` | server default, 75 s |
| `bitbucket.fetcherMaxWorkers` | `BITBUCKET_FETCHER_MAX_WORKERS` | server default, 8 |

```yaml
jira:
  timeout: "120"
  fetcherMaxWorkers: "4"
bitbucket:
  timeout: "120"
  fetcherMaxWorkers: "4"
```

The values are rendered as strings, so a bare number and a quoted number are
equivalent. A value that is not a whole number falls back to the server
default.

### Bitbucket Data Center

Bitbucket is configured independently of Jira and Confluence. `bitbucket.authMode`
is separate from the top-level `authMode` and accepts `oauth` (client credentials
from an application link registered on the Bitbucket instance) or `byot` (an
access token you supply). The
[authentication guide](../docs/authentication.mdx#bitbucket-data-center)
describes the token sources.

Client credentials identify the server to Bitbucket. Pair them with the OAuth
proxy, per-request bearer tokens, or a persisted token cache, which supply the
token at call time:

```yaml
bitbucket:
  enabled: true
  url: "https://bitbucket.your-company.com"
  authMode: oauth
  oauthClientId: "client-id-from-application-link"
  oauthClientSecret: "client-secret"
  oauthRedirectUri: "https://mcp.your-company.com/callback"
  oauthScope: "REPO_READ"
```

With an access token:

```yaml
bitbucket:
  enabled: true
  url: "https://bitbucket.your-company.com"
  authMode: byot
  oauthAccessToken: "existing-access-token"
```

The chart fails the render, with a message naming the value, when Bitbucket is
enabled without a `url`, with an unrecognised `authMode`, without the OAuth
client id, secret, or scope, or without the `byot` access token. A value made
of whitespace counts as missing.

`persistence.enabled: true` mounts the OAuth token cache when the top-level
`authMode` or `bitbucket.authMode` is `oauth`.

With `oauthProxy.enabled: true`, the proxy fronts one provider family per
release: Bitbucket, or Jira and Confluence. A Bitbucket release behind the
proxy uses `bitbucket.authMode: oauth` with a nonblank `oauthRedirectUri`,
leaves the top-level `oauth.clientId` empty, and leaves the Jira and Confluence
`url` values empty. The chart fails the render otherwise. To serve both
families through the proxy, run two releases.

Set `bitbucket.passthroughHeaders` only behind a trusted gateway that
authenticates every MCP request and overwrites the configured headers (see
"External proxy authentication" below). On a directly reachable deployment,
clients could supply those headers themselves.

For a private CA, keep `sslVerify: "true"`, mount the CA bundle into the pod,
and point `SSL_CERT_FILE` at it through `extraEnv`. The server's OS trust-store
integration reads that variable.

The `ATLASSIAN_*` retry, rate-limit, concurrency, and circuit-breaker settings
(`ATLASSIAN_RETRY_*`, `ATLASSIAN_REQUESTS_PER_SECOND`,
`ATLASSIAN_MAX_CONCURRENT_REQUESTS`, `ATLASSIAN_CIRCUIT_BREAKER_*`) are
process-wide, so one budget covers every enabled service in the pod. Set them
through `extraEnv`:

```yaml
extraEnv:
  - name: ATLASSIAN_REQUESTS_PER_SECOND
    value: "5"
  - name: ATLASSIAN_MAX_CONCURRENT_REQUESTS
    value: "8"
  - name: ATLASSIAN_RETRY_TOTAL
    value: "3"
```

### External proxy authentication

Use `authMode: external` only behind a trusted gateway that authenticates every
MCP request and overwrites the configured passthrough headers. The chart enables
`ATLASSIAN_EXTERNAL_AUTH_ENABLE` and `IGNORE_HEADER_AUTH` for this mode.

```yaml
authMode: external
transport: streamable-http
jira:
  url: "https://your-company.atlassian.net"
  passthroughHeaders: "Cookie"
confluence:
  url: "https://your-company.atlassian.net/wiki"
  passthroughHeaders: "Cookie"
```

For dynamic service URLs, add `MCP_ALLOWED_URL_DOMAINS` through `extraEnv`. The
server rejects dynamic external-auth destinations without this allowlist.

### Proxy + PAC/WPAD

Proxy values can also enable optional PAC/WPAD auto-configuration.

```yaml
proxy:
  enabled: true
  https: "https://proxy.example.com:8443"
  noProxy: "localhost,127.0.0.1,.internal.example.com"
  wpad:
    enabled: true
    url: "http://wpad/wpad.dat"
  jira:
    wpad:
      enabled: false
  confluence:
    wpad:
      enabled: true
      url: "http://confluence-wpad.example.com/wpad.dat"
```

This configures the related proxy environment variables when set, including:

- `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `SOCKS_PROXY`
- `ATLASSIAN_PROXY_WPAD_ENABLE`, `ATLASSIAN_PROXY_WPAD_URL`
- `JIRA_PROXY_WPAD_ENABLE`, `JIRA_PROXY_WPAD_URL`
- `CONFLUENCE_PROXY_WPAD_ENABLE`, `CONFLUENCE_PROXY_WPAD_URL`
- `BITBUCKET_PROXY_WPAD_ENABLE`, `BITBUCKET_PROXY_WPAD_URL`

PAC/WPAD remains opt-in and is only used when no explicit proxy is configured.

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

For issues with the MCP Atlassian server, see https://github.com/endofcake/mcp-atlassian/issues

## License

MIT License
