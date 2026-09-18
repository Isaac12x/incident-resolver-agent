# Source plugins and connections

`incident-agent plugins` (or `incident-agent plugins list`) lists the source adapters that are built into the
harness. Listing is local and has no network side effects:

```bash
incident-agent plugins
incident-agent plugins list
incident-agent plugins --json
```

The catalog contains `mcp`, `webhook`, `loki`, `grafana`, and `local-logs`.
Catalog entries describe available adapters; they are loaded when their source
is configured. `connect` only saves the source configuration; the adapter is
instantiated when the server or worker starts. A plugin is not connected merely
because it appears in the catalog.

Use `connect` to add a named source to the configured TOML file. The global
`--config` option must come before the subcommand:

```bash
incident-agent --config /etc/incident-harness/config.toml connect local-logs \
  --name api-logs --log-path /var/log/myapp/api.log
```

Names identify connections, so one plugin type can be configured more than
once. Names are normalized for duplicate checks, including `-` and `_`, so
choose names that remain distinct after normalization. For example, these are
two independent local log sources:

```bash
incident-agent connect local-logs --name api-logs --log-path /var/log/myapp/api.log
incident-agent connect local-logs --name worker-logs --log-path /var/log/myapp/worker.log
```

With no arguments, `connect` opens the interactive connection form. Use
`connect --list` to show configured names and their adapter types.

The common options are:

| Option | Use |
| --- | --- |
| `--name NAME` | Unique name of up to 48 letters, digits, underscores, or hyphens; start with a letter or underscore. |
| `--url URL` | Credential-free HTTP(S) endpoint for MCP, Loki, or Grafana. |
| `--log-path PATH` | Absolute log file path for `local-logs`. |
| `--transport` | MCP transport: `stdio`, `streamable-http`, or `sse`. |
| `--auth-token-env NAME` | Bearer-token environment variable for Loki, Grafana, or HTTP/SSE MCP. The token value is never stored in config. |
| `--tenant-id ID` | Loki tenant sent as `X-Scope-OrgID`. |
| `--datasource-uid UID` | Grafana Loki datasource UID. Required for `grafana`. |
| `--purpose PURPOSE` | `incident`, `output`, `observability`, or `other`. |
| `--capability VALUE` | Add a capability; repeat for multiple values. MCP requires at least one, such as `logs`. |
| `--command REMAINDER` | MCP stdio command and arguments. Put it last because it consumes the remainder of the command line. |

Credentials belong in the environment of the process running the harness. Do
not put token values in URLs or TOML. Loki and Grafana adapters are read-only
log/metric sources; `webhook` is the incident intake adapter; `local-logs`
reads a bounded recent tail from a regular file on the harness host; and MCP
can expose tools from either a stdio process or an HTTP server.

Examples:

```bash
# Receive Grafana Alerting webhooks.
incident-agent connect webhook --name grafana-alerts --purpose incident

# Query a Loki tenant. LOKI_TOKEN is read when the connector starts.
incident-agent connect loki --name production-loki \
  --url https://loki.example.com --auth-token-env LOKI_TOKEN \
  --tenant-id production --purpose observability --capability logs

# Query a Loki datasource through Grafana.
incident-agent connect grafana --name grafana-prod \
  --url https://grafana.example.com --datasource-uid loki-prod \
  --auth-token-env GRAFANA_TOKEN --purpose observability

# Connect to an MCP server over Streamable HTTP or SSE. The logs capability
# makes the adapter available to log-oriented operations.
incident-agent connect mcp --name incident-tools \
  --transport streamable-http --url https://tools.example.com/mcp \
  --auth-token-env MCP_TOKEN --capability logs
incident-agent connect mcp --name local-tools --transport stdio \
  --capability logs --command /usr/local/bin/incident-mcp --workspace /srv/app
```

After adding or changing a connection, restart the running server or worker so
its `ConnectorManager` loads the new configuration. If an active runtime
bundle is in use, build and activate a bundle after the configuration change,
then restart the service:

```bash
incident-agent bundle build
incident-agent bundle activate VERSION
sudo systemctl restart incident-harness.service
```

The active bundle is the immutable set of configuration and skills used by a
run. Rebuilding and activating makes the updated connection part of that set;
a process restart makes the running server or worker use it.
