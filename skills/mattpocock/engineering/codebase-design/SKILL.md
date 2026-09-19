---
name: codebase-design
description: "Use when an incident fix may require an architectural change or a new seam."
triggers:
  - "architecture change"
  - "architectural decision"
  - "design a module"
---

Use this vocabulary when assessing a proposed architectural change:

- A **module** has an interface and an implementation.
- **Depth** is the leverage at the interface; prefer a small interface with substantial behaviour behind it.
- A **seam** is where behaviour can change without editing the caller.
- An **adapter** satisfies an interface at a seam.
- **Locality** keeps a change and its verification concentrated in one place.

Apply the deletion test: if deleting a proposed module merely moves complexity to its callers, it is shallow. Treat the interface as the test surface and introduce a seam only when the repair demonstrates meaningful variation. Preserve existing architecture where it can fix the incident; if the defect requires a change, record the reason, alternatives, trade-offs, and verification in the repository's architecture document or a small ADR.
