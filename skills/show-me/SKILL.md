---
name: show-me
description: Explain incidents and draft concise, evidence-based summaries and pull-request details.
triggers:
  - "explain incident"
  - "draft incident summary"
  - "draft incident pull request body"
  - "create incident pull request"
---

# Show me

Use this skill for an incident summary, an investigation explanation, or the body passed to
`open_pr`. It can be applied while investigation and implementation artifacts are being assembled,
and when a later session needs to explain their evidence. The runtime loads it for
`investigate`, `implement_fix`, and `run_session` operations so those artifacts can be summarized
consistently.

Prefer the model-generated investigation and fix artifacts as the source for an explanation. State
the root cause, affected behavior, repair, and verification evidence in terms supported by those
artifacts. When no generated artifact exists yet, use an explicit extractive fallback: quote or
closely summarize only the available incident, repository, and test evidence, and label the result
as an extractive fallback. Never imply that a model-generated explanation exists when it does not.

Include one concise visual section when it clarifies the incident fix. Choose the smallest form that
fits the evidence:

- A call tree for runtime control flow.
- Mermaid for component interaction, control flow, or data flow.
- A code-shape sketch for an algorithm, decision, or ownership boundary.

Prefer a Mermaid flow or code-shape sketch naming the affected boundary and verification path.
Never invent relationships, files, states, or evidence; omit the visual when the evidence cannot
support it. Keep the body easy to scan alongside root cause, changed behavior, tests, and deployment
verification. Do not overwhelm the reader with multiple visuals when one is sufficient.

This is an adaptation of HumanLayer's upstream `show-me` skill:
<https://github.com/humanlayer/skills/blob/main/plugins/show-me/skills/show-me/SKILL.md>.
