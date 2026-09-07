---
name: github
description: "Use when creating, updating, or monitoring a GitHub pull request."
triggers:
  - "create incident pull request"
  - "update incident pull request"
  - "monitor incident pull request"
---

# GitHub

- Work on a dedicated branch with a clear, task-scoped name.
- Keep commits focused and describe the incident fix accurately.
- Push the branch before creating or updating the pull request.
- Use the `show-me` skill when drafting the PR body. Include a concise Mermaid flow or code-shape
  sketch when it clarifies the incident fix, grounded in observed files and behavior.
- Include the root cause, changed behavior, tests, and deployment verification in the pull request.
- Keep the body brief and scannable; omit unsupported visual claims rather than inventing them.
- Do not claim a pull request is ready until the required checks have completed.
- Monitor review feedback and integrate authorized requested changes.
