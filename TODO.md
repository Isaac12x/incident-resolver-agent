# TODO

Ideas and all that can make this better.

* Embed the event logger within the same repository 
Event from grafana -> deduplicate and group -> Root Cause Prediction: Machine learning model predicts incident root causes using logistic regression.
* Make sure we have a way to install this in the system, that the tui is configurable as a cli and that the executable code is installed. As of now we are copying the repository but I think it is better to provide this as a curl installable, configurable via the cli and so on and so forth. Then use `incident-agent` + command to do things. E.g. `incident-agent update` will update everything or `incident-agent run` installs and makes sure this whole system runs - if all is configured. `incident-agent config` will launch the TUI.
* Improve the TUI interface.
* Intelligent Summarization (done via explain-code)
* Similar Incident Search: FAISS vector search with sentence transformers finds related incidents
* Stronger tool calling with RL into the main loop, find-research-install tools as needed and retries.
* Evals.
* Logs and observability as primitives.
* Workspaces, guardrails and other safety protocols. Instead relying on the using the .agent folder as the worflow.
* Security.
* Versioning and construction. For the system prompt, skills and connections.
* Extensibility other than by the use of skills.

## Implementation status (2026-09-14)

Implemented in this change:

- Embedded durable Grafana event history, grouping/deduplication metadata, and incident labels.
- Curlable isolated installation; `config`, `update`, and argument-free `run`; persistent user paths.
- TUI configuration location, keyboard exit, and API authentication controls.
- Bounded logistic root-cause prediction trained explicitly from labeled history.
- Optional FAISS/sentence-transformer similarity search and offline extractive summaries.
- Packaged offline contract evaluations and JSON reports.
- Authenticated control APIs, workspace identity checks, and versioned execution manifests.
- Bounded configured MCP discovery retries; MCP remains the extension mechanism beyond skills.

Still open (not represented as completed by the features above):

- Connect the requested explain-code summarization provider; no provider contract or executable
  is configured in this repository. Current summaries are extractive.
- Train/evaluate an RL policy for tool selection and define a trusted tool registry for autonomous
  research/installation. Current discovery retries are deterministic and use configured MCP tools.
- Evaluate root-cause prediction on a held-out, labeled incident dataset and model-driven repair
  quality on real incident benchmarks. Offline contract evals do not measure either.
- Strong process/container isolation and a wider security audit. Directory identity and existing
  command/path guardrails do not confine arbitrary subprocess execution at the OS level.
