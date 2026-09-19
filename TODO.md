# TODO

Ideas and all that can make this better.

* Embed the event logger within the same repository 
Event from grafana -> deduplicate and group -> Root Cause Prediction: Machine learning model predicts incident root causes using logistic regression.
* Make sure we have a way to install this in the system, that the tui is configurable as a cli and that the executable code is installed. As of now we are copying the repository but I think it is better to provide this as a curl installable, configurable via the cli and so on and so forth. Then use `incident-agent` + command to do things. E.g. `incident-agent update` will update everything or `incident-agent run` installs and makes sure this whole system runs - if all is configured. `incident-agent config` will launch the TUI.
* Improve the TUI interface.
* Intelligent Summarization (using HumanLayer’s `show-me` skill for investigation and fix
  explanations, with an explicit extractive fallback before generated artifacts exist)
* Similar Incident Search: FAISS vector search with sentence transformers finds related incidents
* Stronger tool calling with RL into the main loop, find-research-install tools as needed and retries.
* Evals.
* Logs and observability as primitives.
* Workspaces, guardrails and other safety protocols. Instead relying on the using the .agent folder as the worflow.
* Security.
* Versioning and construction. For the system prompt, skills and connections.
* Extensibility other than by the use of skills.

## Acceptance status (2026-09-14)

| Requested work | Implementation and verification |
| --- | --- |
| Embedded event logger, grouping and root prediction | Durable Grafana intake, transactional deduplication, logistic regression from labeled history, automatic context enrichment; intake and holdout tests |
| System installation and CLI lifecycle | Curl installer, installed executable, config TUI, readiness checks, managed tool install/update; installed CLI and lifecycle tests |
| Improve TUI | Readiness overview, runtime/container controls, existing model/repository/connector and API-auth editors; Textual tests |
| Intelligent summarization | Summary API and incident explanations reuse model-generated investigation/fix artifacts; before artifacts exist they use an explicitly labelled extractive fallback; show-me guidance and artifact reads are covered by runtime tests |
| FAISS and sentence-transformer search | Optional vector index with cached model support, automatic history refresh, explicit lexical fallback; search tests and real optional-dependency smoke |
| RL tool calling, research/install/retries | Persistent contextual UCB bandit, compatible tool selection, approved catalog discovery and digest-pinned wheel installation, bounded read-only retries; SDK/extension tests |
| Evals | Contract suite, chronological holdout, retrieval relevance, and real WorkflowEngine repair fixture with independent gold tests; production model benchmarking requires representative data |
| Logs and observability | Rotating structured operation logs, persistent counters/failures/durations, authenticated metrics; restart and authentication tests |
| Workspaces and guardrails | SQLite task/event authority, durable worker leases and workspace registration, legacy migration; artifact-folder recovery and concurrency tests |
| Security | API/HMAC authentication, bounded providers, secret filtering and optional restricted containers for shell/test execution; real container denial/timeout checks |
| Versioning and construction | Immutable config/prompt/skills/connections bundles, validation, activation/rollback and runtime loading; integrity and application tests |
| Extensibility beyond skills | MCP plus trusted callable wheel extensions, catalog discovery and restart restoration; real wheel install/invoke test |

Scope limits: the tool learner is an online contextual bandit, not a pretrained general repair
policy. Discovery/installation is confined to the operator-approved catalog. Container mode covers
repository shell and lifecycle test execution, not native subscription CLI, MCP, or trusted plugin
processes. Synthetic evaluation results do not establish production root-cause or model repair
quality. Summaries depend on the evidence available in generated artifacts and persisted incident
records; an extractive fallback is used until generated artifacts exist.




(not done)
## KEY changes
* FIX: Scope limits: the tool learner is an online contextual bandit, not a pretrained general repair policy. Discovery/installation is confined to the operator-approved catalog. Container mode covers repository shell and lifecycle test execution, not native subscription CLI, MCP, or trusted plugin processes. Synthetic evaluation results do not establish production root-cause or model repair quality. Summaries depend on the evidence available in generated artifacts and persisted incident records; an extractive fallback is used until generated artifacts exist.
* Add security: https://github.com/cloudflare/security-audit-skill and run it before marking the PR as ready.
* PR annotations.
* Add Matt Pockock's skills for what they are useful.
* Replace looping for graphing  
See how the graph improves as opposed to a simpler looping mode.
* Real-time Analytics: Interactive dashboard with charts and incident statistics - visualize resolutions.
* replace chatpgpt with jev (new parallel model)

## Extras

* Plugins to AzureDevOps (and other ticketting SW) as tickets 
(move them on the right column) 
Incident -> TODO -> executing -> DOING -> completed and PR -> READY FOR REVIEW (and replace the incident prefaced with FIX:)
* Add idea to match to the code style used in the repository.
