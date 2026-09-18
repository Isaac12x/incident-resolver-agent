# Application-scoped incident repair

Approved in conversation on 2026-09-18. An application groups explicitly configured services
and repositories into one incident investigation and durable agent session. Repository-only
incidents and persisted tasks remain compatible. Membership is an authorization boundary:
ambiguous or conflicting routing fails closed, and tasks snapshot their repository membership.

Each repository retains its own isolated worktree, instructions, graphs, permissions, branch,
PR, reviews, and exact deployment SHA. One lead agent can inspect and change every member,
using repository-aware tools. Only changed repositories receive PRs. Partial publication and
review processing survive restarts without duplicate PRs. Completion requires every changed
repository's configured verification and application integration verification when configured.
An application integration result is tied to the complete repository revision set.

An application does not combine unrelated alerts merely because they share an application.
Incident source, external ID, environment, and application scope determine deduplication.
Repository membership can overlap across applications; implicit routing must then reject
ambiguity. Existing single-repository configuration and task recovery remain supported.

Validation covers two local Git repositories (Python backend and React frontend), scoped
routing, tool boundaries, cross-repository checks, partial publication/resume, both PR review
routes, stale deployment rejection, and existing single-repository regressions. No new
dependencies are required. Live model/deployment verification is reported separately.
