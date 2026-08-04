---
name: deployment-verification
description: "Use when verifying a pull request against a preview deployment."
triggers:
  - "verify preview deployment"
  - "test deployed incident fix"
  - "check pull request deployment"
---

# Deployment Verification

Only verify a deployment when all of these values match the task and current pull request:

- Pull request number
- Current pull request head SHA
- Deployment SHA
- Required environment
- Preview URL
- Playwright result

Reject stale deployments, deployments for another environment, and unsafe or non-HTTP preview
URLs. Report the deployment identity and every verification command and result.
