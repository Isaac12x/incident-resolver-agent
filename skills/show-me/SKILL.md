---
name: show-me
description: Draft concise, evidence-based visual details for an incident pull-request body.
triggers:
  - "draft incident pull request body"
  - "create incident pull request"
---

# Show me

Apply this skill only while drafting the body passed to `open_pr` for a new incident pull request.
Do not apply it during investigation, implementation, local or deployment verification, review
feedback, pull-request updates, or general explanations.

Include one concise visual section when it clarifies the incident fix. Choose the smallest form that
fits the evidence:

- A call tree for runtime control flow.
- Mermaid for component interaction, control flow, or data flow.
- A code-shape sketch for an algorithm, decision, or ownership boundary.

Prefer a Mermaid flow or code-shape sketch naming the affected boundary and verification path.
Never invent relationships, files, states, or evidence; omit the visual when the evidence cannot
support it. Keep the body easy to scan alongside root cause, changed behavior, tests, and deployment
verification. Do not overwhelm the reader with multiple visuals when one is sufficient.
