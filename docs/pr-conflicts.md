# Pull request conflict recovery

Incident Harness listens for GitHub `pull_request` webhooks. A recovery starts
only when the payload contains `pull_request.mergeable_state: "dirty"` and a
matching tracked pull request. GitHub can report `unknown` while it is still
calculating mergeability; that state is ignored so the worker does not create
false conflict work. A later webhook retries the check. When webhooks are
unavailable, the durable worker polls tracked open PRs every five minutes; this
interval is independent of the normal task recovery loop.

The webhook's `pull_request.base.ref` is the source of truth. The harness fetches
that branch from `origin`, merges the fetched commit into the incident branch,
and wakes the same durable task session. The session receives explicit conflict
instructions, must preserve both sets of changes, rerun local checks, and use
the normal publication lifecycle. It never force-pushes. Before publishing, a
recovered branch must contain the fetched base commit.

Recovery is scoped to the exact repository and pull request. Application tasks
record the retry and conflict state on the affected repository member while
retaining the shared task session. A webhook whose head SHA is older than the
durably recorded PR head is ignored. The per-task model iteration limit bounds
conflict retries; exhaustion moves the task to `blocked` for operator review.

The GitHub webhook must be configured with the harness webhook secret and a
delivery URL for `/hooks/github`. The GitHub token used by the CLI adapter needs
repository read access, pull-request read/write access, and permission to push
the incident branch. The webhook endpoint itself remains subject to the normal
signature verification and delivery de-duplication checks.
