# Repair context behavior

Before an agent edits code, the harness loads documentation separately for the application scope (when an application worktree exists) and for every repository scope. `VISION.md` is matched case insensitively. Architecture context includes `ARCHITECTURE.md` and Markdown, MDX, or RST files under `architecture/` or `adr/`, also case insensitively.

The prompt states when a document is absent. Absence is evidence that no documented constraint was found; it is not a reason to invent one. The harness currently guides this behavior through the agent context and does not claim to enforce architectural judgment mechanically.

| Architecture docs | Vision | Repair behavior |
| --- | --- | --- |
| Present | Present | Apply the vision and architecture documents as scoped evidence. Preserve the architecture for a normal fix. If the defect requires an architecture change, explain the cause, update the relevant document, and verify the result. |
| Present | Absent | Apply the architecture documents. Do not infer product vision. A required architecture change still needs a documented rationale and an updated architecture document. |
| Absent | Present | Apply the vision as project intent. Do not invent architecture constraints. If a durable architecture decision is required, add a small ADR and explain why. |
| Absent | Absent | Use source, tests, incident evidence, and repository instructions. Do not invent vision or architecture constraints. Add an ADR only when the repair creates a durable architecture decision. |

For a normal fix, the agent keeps the existing design and records the verification. For an architecture change, the agent records the causal reason, scope, trade-offs, and verification in the investigation and pull request summary. Existing repository rules remain binding; an ambiguous conflict with those rules or with the vision must be reported rather than guessed.

The bundled `resolving-merge-conflicts` skill directs conflict repair to inspect both sides' primary sources, preserve both intents where possible, run the project's checks, and finish the merge or rebase. The harness selects it automatically for conflict-related work, but the conflict workflow and pull request integration remain responsible for applying and verifying the resolution.

Incident investigation, implementation, review, and durable resolution sessions also load the bundled `code-review` skill. Its Standards axis uses repository rules and labelled smell heuristics; its Spec axis uses the originating incident and evidence, then the current pull request merge-base and working diff. It does not invent issue requirements or require an external issue tracker.
