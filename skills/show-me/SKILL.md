---
name: show-me
description: Help the user understand the current topic visually with concise diagrams, code-shape sketches, and focused HTML artifacts.
---

# Show me

Use the smallest visual that makes the current point clear, and keep the surrounding prose brief.
Place the visual beside the claim it explains. Preserve real file, component, state, and boundary
names when they matter.

Choose the form that fits the evidence:

- Pseudocode for an algorithm or decision.
- A call tree for runtime control flow.
- A component tree for UI structure and state boundaries.
- A shallow file tree for ownership or a broad refactor.
- Mermaid for component interaction, control flow, or data flow.
- A focused HTML artifact for a dense layout, comparison, or concept that Mermaid cannot show
  clearly. Use real labels and data, support desktop and mobile, and open it for the user.

Use a diff when the surrounding shape already exists and the change is the point. Show the complete
block when omitted context would hide ownership or order, or when the result must be copied directly.

For pull-request body copy, include one concise visual section when it clarifies the incident fix.
Prefer a Mermaid flow or code-shape sketch naming the affected boundary and verification path.
Never invent relationships, files, states, or evidence; omit the visual when the evidence cannot
support it. Keep the body easy to scan alongside root cause, changed behavior, tests, and deployment
verification. Do not overwhelm the reader with multiple visuals when one is sufficient.
