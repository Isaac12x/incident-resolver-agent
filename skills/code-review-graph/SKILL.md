---
name: code-review-graph
description: "Use for codebase navigation, architecture questions, symbol lookup, call graphs, and change-impact analysis when .code-review-graph/ exists."
triggers:
  - "code graph"
  - "callers"
  - "callees"
  - "impact radius"
  - "symbol search"
  - "how does this code work"
---

# Code Review Graph

Use the repository graph built at `.code-review-graph/graph.db` before broad text search.

## Harness tools

When the incident harness exposes graph tools, prefer them in this order:

1. `code_graph_search` — semantic symbol search for a natural-language question
2. `code_graph_query` — structured queries (`callers_of`, `callees_of`, `imports_of`, `tests_for`, `inherits_from`, `file_summary`)
3. `code_graph_impact` — blast radius for changed files before or after edits

Treat graph results as navigation evidence and confirm conclusions against source and tests.

## CLI

Rebuild or refresh the graph from the repository root:

```bash
code-review-graph build --repo .
code-review-graph update --repo .
code-review-graph status --repo .
```

Skip rebuilding when `.code-review-graph/graph.db` already exists for the current checkout unless files changed materially or the harness regenerated the worktree.
