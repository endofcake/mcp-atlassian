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

- **authMode**: `api-token`, `personal-token`, `oauth`, `byot`, or `external`
- **transport**: `stdio`, `sse`, or `streamable-http`
- **confluence/jira.enabled**: Enable/disable Confluence or Jira integration
- **config.readOnlyMode**: Disable all write operations
- **persistence.enabled**: Enable OAuth token persistence
- **oauthProxy.enabled**: Expose MCP OAuth discovery + DCR routes (opt-in)
- **oauthClientStorage.mode**: `default` (FastMCP storage) or `factory` (custom)

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

### Server TLS (HTTPS listener)

For end-to-end encryption to the pod, the server can terminate TLS itself instead of
relying on the ingress. Provide an **existing** Secret holding the certificate and key
(for example one issued by cert-manager) — the chart never creates or manages
certificates:

```yaml
transport: streamable-http
tls:
  enabled: true
  secretName: mcp-atlassian-server-tls   # existing kubernetes.io/tls Secret
  # certFileKey: tls.crt                 # defaults match a kubernetes.io/tls Secret
  # keyFileKey: tls.key
```

The Secret is mounted read-only (file mode `0400`) and passed to the server via
`--ssl-certfile`/`--ssl-keyfile`. The chart sets
`MCP_ATLASSIAN_USE_SYSTEM_TRUSTSTORE=false` automatically — the HTTPS listener
requires it — and defaults the readiness probe scheme to `HTTPS` (an explicitly set
scheme wins). Enabling TLS with `stdio` transport, without a `secretName`, or with a
blank cert/key key name fails the render loudly.

With the OS trust store disabled, outbound SSL verification uses the bundled certifi
CAs. If your Atlassian instance uses a private CA, mount a CA bundle from an existing
Secret:

```yaml
caBundle:
  secretName: mcp-atlassian-corp-ca      # existing Secret holding the bundle (PEM)
  # key: ca.crt
```

This exposes the bundle via `REQUESTS_CA_BUNDLE` (Jira/Confluence REST clients) and
`SSL_CERT_FILE` (OAuth proxy paths). Include public roots in the bundle if the
deployment also talks to Atlassian Cloud.

Two operational notes:

- **Certificate rotation requires a restart.** The pair is loaded once at startup, and
  the chart cannot watch an external Secret. Pair the deployment with something like
  [Stakater Reloader](https://github.com/stakater/Reloader) (add
  `secret.reloader.stakater.com/reload: <secretName>` via `podAnnotations`) or schedule
  a `kubectl rollout restart` inside the certificate's renewal window.
- **Behind an ingress, the controller must speak HTTPS to the backend — and verify
  it.** `backend-protocol: "HTTPS"` alone only encrypts the ingress-to-pod hop; by
  default ingress-nginx does **not** verify the backend certificate, so the hop is not
  authenticated. For ingress-nginx, provide the CA (and enable verification) as well:

  ```yaml
  ingress:
    annotations:
      nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
      # Authenticate the backend, not just encrypt the hop:
      nginx.ingress.kubernetes.io/proxy-ssl-secret: "<namespace>/<ca-secret>"
      nginx.ingress.kubernetes.io/proxy-ssl-verify: "on"
      nginx.ingress.kubernetes.io/proxy-ssl-server-name: "on"
      nginx.ingress.kubernetes.io/proxy-ssl-name: "<service-dns-name>"
  ```

  Other controllers have equivalent backend-verification settings; without them an
  in-cluster man-in-the-middle could intercept the credentials the listener exists to
  protect.

### Ingress labels

Custom labels can be merged into the Ingress metadata (in addition to the chart's
standard labels), alongside the existing annotation support. On a key collision with a
standard label, the chart's label wins and the key is emitted once:

```yaml
ingress:
  enabled: true
  labels:
    team: platform
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
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
