# Trusted extension contract

Set `agent.tool_registry` to an absolute path containing an operator-maintained JSON catalog:

```json
{
  "tools": [
    {
      "name": "incident_lookup",
      "version": "1.0.0",
      "kind": "python",
      "source": "/opt/incident-tools/incident_lookup-1.0.0-py3-none-any.whl",
      "sha256": "REPLACE_WITH_THE_64_CHARACTER_SHA256_OF_THE_WHEEL",
      "entrypoint": "incident_lookup:query",
      "capabilities": ["incident-history"]
    }
  ]
}
```

The source may be a local wheel or a pinned HTTP(S) URL. Names and versions must be safe identifiers.
The harness verifies the artifact SHA-256 before installing it in a dedicated virtual environment,
using `pip --no-deps`. Package dependencies must therefore already be supplied by the artifact.
A digest is an integrity check, not a publisher signature. Operators approve code by adding its
manifest; the agent cannot install arbitrary package names found on the Internet.

The entrypoint is a synchronous Python callable accepting one dictionary and returning JSON-compatible
data. `tool_catalog` exposes names and capabilities for discovery; `tool_install` requires
`permissions.allow_dependency_installation`. An installed tool can be invoked through `adaptive_tool`
and is restored for later runs while its version and digest remain in the trusted catalog.

Use explicit tool selection for different argument schemas. Supply compatible candidate names only
when they accept the same input and perform an equivalent operation. Contextual UCB selection records
success/failure rewards in persistent storage. Read-only retry permissions are explicit; arbitrary
shell commands and extension mutations must not be retried automatically.

Extensions run as operator-trusted host code in an isolated Python environment, not an OS sandbox.
Credentials are filtered from their environment, and execution/output limits apply. MCP connectors
are another supported extension interface and retain their configured authentication/trust boundary.
