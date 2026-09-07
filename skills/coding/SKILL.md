---
name: coding
description: "Use when implementing or changing code to resolve an incident."
triggers:
  - "implement incident fix"
  - "change incident code"
  - "apply incident patch"
---

# Coding

- Preserve repository conventions.
- Confirm the proposed fix restores the expected behavior established during investigation.
- Apply the loaded Ponytail skill before choosing an implementation and when reviewing the diff.
- Fix the demonstrated cause at the appropriate shared boundary; check affected callers.
- Never silence errors, return fabricated success, weaken validation, or change test expectations
  merely to make an alert disappear. Preserve intentional rejection and failure behavior.
- Avoid unrelated refactoring.
- Inspect files before replacing them.
- Add a regression test whenever practical.
- Inspect the final diff before committing.
