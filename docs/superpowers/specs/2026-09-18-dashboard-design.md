# Incident dashboard design

Status: implementation authorized on 2026-09-18 with multiple Luna agents orchestrated by
the parent. Token/browser-session authentication is the implementation default. Core metrics
are incidents versus resolved incidents, time to resolution, failures versus successes, and
PRs opened. Resolved means the durable task reached completed; timing uses its completion
event, never the mutable last-update timestamp. This measures harness resolution, not
independently confirmed production recovery.

## Outcome and approach

`incident-agent dashboard` serves a live browser dashboard for the selected runtime.
It supports foreground and detached operation, and explicit commands to publish or remove
a route through the host's reverse proxy. The first version is read-only.

Use a separate FastAPI process with packaged HTML, CSS, and JavaScript, a read-only SQLite
reader, and explicit proxy adapters. This reuses installed dependencies and keeps dashboard
startup independent of model credentials, connectors, migrations, and repair workers.

Alternatives considered:

- Mount on the existing HTTP server: simpler deployment, but requires that server to run
  and risks exposing control endpoints alongside the dashboard.
- Use a separate frontend service and database: allows independent scaling, but adds
  installation and synchronization work that a single-host dashboard does not need.

## Command contract

```text
incident-agent dashboard                         # foreground; loopback URL printed
incident-agent dashboard -d                      # detach after readiness succeeds
incident-agent dashboard --detached              # canonical long spelling
incident-agent dashboard --dettached             # accepted compatibility alias
incident-agent dashboard --listen-port 8766       # internal loopback listener
incident-agent dashboard status                  # process, snapshot check time, public route
incident-agent dashboard stop                    # stop dashboard only
incident-agent dashboard logs                    # bounded recent dashboard logs
incident-agent dashboard port 8443                # publish using saved host/TLS settings
incident-agent dashboard port 8443 --host incidents.example.com
incident-agent dashboard port 8443 --proxy nginx  # explicit adapter if ambiguous
incident-agent dashboard port close              # revoke access and remove owned route
```

Existing global `--config PATH` selects the runtime. Process identity and routing ownership
are keyed by the canonical runtime path, including when a privileged routing helper is used.
Internal port defaults to 8766. A busy internal port produces a conflict, not silent reassignment.
The public port is separate from the internal port and must be an integer from 1 through 65535.
One public route is supported per runtime initially; changing it requires closing the old route.

Foreground and detached modes serve the same live data. Ctrl-C stops the foreground process.
Detached mode survives terminal exit; automatic reboot startup is a separate future feature.
Repeated start returns the existing instance's URL after checking its identity and readiness.
PID, process start identity, readiness nonce, listener, and log location are recorded with
owner-only permissions. A startup lock prevents duplicate instances. Stop never signals a PID
based solely on a stale PID file. The dashboard runs as the runtime owner, not root.

Publishing requires a running detached instance; otherwise report the exact start command.
Stop revokes active public sessions and stops the dashboard; a remaining proxy route is
reported as inactive. Restart begins with public access disabled until publication is renewed.
`port close` is the command that also removes the persistent routing configuration.

## Dashboard contents

The top bar shows runtime, worker status where observable, last successful refresh, and
local/public access status. Worker health is separate from dashboard health; an unavailable
worker does not erase historical data or imply that incident processing has stopped forever.

Overview cards show active, waiting for deployment, waiting for review, blocked, failed,
and completed task counts. A task table shows summary, application, repository membership,
environment, lifecycle state, age, last update, and PR links. Filters cover application,
repository, environment, state, and time range. One application incident counts once even
when it involves several repositories.

A detail view shows a sanitized event timeline and per-repository PR, test, and deployment
status, including revision matching. Older repository-only tasks remain readable. No cancel,
retry, edit-config, or publish buttons are included in the initial dashboard.

Operational metrics show existing lifetime call counts, failures, and cumulative duration
for agent runs, shell tools, HTTP requests, and lifecycle operations. Label them as global
lifetime metrics: the current telemetry table cannot support application or time filtering.
Do not display invented cost/token totals or historical charts unsupported by persisted data.
Task timing is labelled elapsed lifecycle time, not production recovery time.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Incidents | Matching durable incident tasks, one per application incident regardless of repository count |
| Resolved / successes | Matching tasks whose current durable state is completed |
| Failures | Matching tasks whose current durable state is failed |
| Success rate | Completed / (completed + failed), unavailable when neither outcome exists |
| Cancelled / blocked | Separate state counts; neither is a success or failure |
| Resolution time | Creation to the first valid recorded completion event, one sample per completed task |
| Timing samples | Completed tasks with valid nonnegative timing; missing timings are excluded explicitly |
| PRs opened | Union of persisted PR references, deduplicated by repository and number across compatibility and per-repository fields |

Incident metrics follow the active filters. PR counts represent known persisted PRs, including
closed PRs still referenced by the task. Operation telemetry retains its separate lifetime scope.
The dashboard does not infer alert recovery timestamps, production uptime, or model token cost.

## Data flow and live updates

Browser -> dedicated dashboard API -> read-only `runtime.sqlite3` connection.
Do not construct `Application`, `Storage`, `TaskCatalog`, or `Telemetry`: their constructors
perform initialization or migration writes. Open with SQLite `mode=ro` and query-only mode;
use short snapshot transactions and bounded queries. Do not use immutable mode against live WAL.
Missing or incompatible schemas produce an explicit empty/unavailable view without migration.

Initial JSON returns paginated task data and summary counts from one snapshot. Server-sent
events notify browsers of changes, polling persisted event sequence every two seconds.
Refresh task and metric snapshots periodically too: not every mutation creates a catalog event.
Support reconnect, heartbeat, bounded replay, and snapshot reset when a cursor becomes invalid
or the database is replaced. One shared poller per process avoids one database poll per browser.
Do not hold a SQLite transaction open for the life of an event stream.

Show disconnected/stale status immediately on stream failure and last refresh time. Bound
clients, event buffers, pagination, and timeouts; slow clients reconnect from a fresh snapshot.
The freshness target is five seconds under normal local operation, in both process modes.

## Opening internet access

Initially support host-managed Caddy and nginx. Adapter detection checks the running service
and its actual configuration, not just installed executables. If multiple candidates, an
unsupported proxy, container-only routing, or externally generated configuration is found,
report actionable configuration requirements without guessing or overwriting that system.
Additional routing systems can implement the same adapter contract later.

`port PORT` uses a saved hostname and TLS setup. On first use, require `--host` and a supported
certificate setup. Caddy can manage certificates when its issuance prerequisites are met;
nginx uses explicitly configured certificate files or an existing managed TLS configuration.
A bare port does not establish a hostname, certificate, DNS, firewall, NAT, or cloud route.
The command configures the local proxy only and distinguishes local success from independently
verified internet reachability. Firewall/cloud automation is outside this first version.

Publication is a journaled operation:

1. Resolve runtime, permissions, proxy service, active config, hostname, and TLS prerequisites.
2. Lock dashboard routing changes; inspect IPv4/IPv6 listeners and existing proxy listeners
   and routes. A busy requested public port is a conflict unless it is this exact owned route.
   Initial support uses a dedicated public listener, not shared-port virtual hosting.
3. Create a least-privilege owned snippet and record paths, hashes, prior content, and operation
   state. If an include must be added, manage one marked include rather than rewriting the file.
   Includes, original backups, and protected target bindings live beside the proxy configuration;
   runtime-owned journals cannot redirect elevated recovery writes. Retire a binding before
   deleting reusable route files. A retired binding with an incomplete journal fails closed and
   requires manual cleanup rather than permitting replay against a later route.
4. Validate the effective complete proxy configuration. Recheck file hashes before install
   to detect concurrent administrator edits. Do not change unrelated routes.
5. Atomically install changes and gracefully reload the known service. Restart is only an
   adapter-declared fallback when required, not a response to failed validation.
6. Verify the dashboard route and HTTPS behavior through the local proxy using the intended
   host/SNI, and verify unrelated-route fixtures in integration tests. Then enable public
   sessions and report the URL. On failure revoke access, restore owned edits, and reload.

Preflight checks cannot eliminate bind races; apply failures also trigger rollback. If rollback
or reload fails, report the actual state and recovery instructions, not success. Reconcile
interrupted operations from the journal on the next routing command. A repeated exact publish
is idempotent only after confirming that the running route matches the owned configuration.

Conflicts return nonzero with a concrete message, for example:
`Conflict: TCP port 8443 is already used by nginx for another listener; no changes made.`
Permission, unsupported proxy, missing TLS configuration, and reload failure are distinct errors.
Only the routing helper needs elevated privileges; it uses validated arguments and fixed adapter
commands, never shell fragments from web input. The web server has no routing-management API.

## Closing internet access

First revoke the public access generation in dashboard-owned local state, reject new public
requests, and terminate existing public event streams. The proxy authenticates its upstream
requests with a per-route secret that clients cannot override; only a fixed configured proxy
path is trusted. Local and proxied sessions are separate, and public sessions require an
enabled generation on every request. Reject unexpected Host/Origin values and forwarded
headers on direct requests. This prevents a stale proxy connection from retaining access.

Then remove only the owned route, validate and reload the proxy, and probe that it no longer
serves the dashboard. Preserve unrelated routes and administrator changes. Already closed is
success. If removal/reload fails, keep dashboard-side public access revoked and report routing
cleanup incomplete; do not re-enable access as part of rollback. Local viewing remains available.
Manual routes outside the adapter's ownership cannot be removed by this command.

## Authentication and browser safety

Implementation default: a separate high-entropy dashboard token exchanged
for a short-lived HttpOnly, SameSite browser session. Public sessions require HTTPS and Secure
cookies. Loopback sessions have a separate scope. Never reuse the control API token, put
tokens in URLs/localStorage, or expose agent control APIs through this listener. Rate-limit
login attempts and invalidate sessions when their generation is revoked.

Use explicit response field allowlists, escaped text, a restrictive content security policy,
and safe link protocols. Raw prompts, conversation history, connector credentials, shell output,
and full incident payloads are not dashboard fields. Timeline summaries require sanitization;
arbitrary event `data` must never be serialized directly to the browser.

## Implementation boundaries and verification

Keep CLI dispatch in `src/__main__.py`, with a new dashboard package separating query models,
HTTP/auth/streaming, process lifecycle, assets, and proxy adapters. Store dashboard process,
session, and routing metadata separately from incident runtime tables. Package assets in wheels.

Acceptance tests cover foreground/detached parity, readiness failure, duplicate starts, stale
PID handling, live changes/reconnects, unavailable/older runtime schemas, worker-down viewing,
and application/repository-only records. Prove dashboard reads do not modify incident state
or start workers. Exercise authentication, redaction, and session revocation.

For both real proxy implementations in disposable environments, test occupied IPv4/IPv6 ports,
invalid full config, missing privilege, concurrent edits, reload failure, rollback failure,
interrupted publication, repeat open/close, TLS routing, existing streams during close, and
preservation of unrelated routes. Test source and installed-wheel execution. No actual host
proxy or public exposure should be modified during ordinary tests.

Delivery order: local read-only live dashboard; detached lifecycle; authenticated exposure;
Caddy/nginx adapters and failure recovery. Implementation was authorized on 2026-09-18.

## References

- Existing `src/__main__.py`, `src/server.py`, `src/task_catalog.py`, `src/telemetry.py`,
  `src/sqlite_store.py`, and application-scoping proposal were reviewed for this design.
- [Caddy validation and reload](https://caddyserver.com/docs/command-line)
- [nginx configuration testing](https://nginx.org/en/docs/switches.html)
- [nginx reload behavior](https://nginx.org/en/docs/control.html)
