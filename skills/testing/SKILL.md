---
name: testing
description: "Use when verifying an incident fix and reporting its results."
triggers:
  - "test incident fix"
  - "verify local incident fix"
  - "run incident regression tests"
---

# Testing

Run verification in this order:

1. The narrowest regression test.
2. The relevant test suite.
3. Type checking.
4. Linting.
5. Build.
6. Final diff inspection.

Never claim that a fix works without reporting executed verification.
