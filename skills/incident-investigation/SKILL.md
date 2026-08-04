---
name: incident-investigation
description: "Use before modifying code to investigate an incoming production incident."
triggers:
  - "investigate production incident"
  - "find incident root cause"
  - "reproduce incident"
---

# Incident Investigation

Before modifying code:

1. Preserve the original incident evidence.
2. Identify the repository and affected environment.
3. Inspect recent relevant changes.
4. Attempt to reproduce the failure.
5. Form a specific root-cause hypothesis.
6. Record evidence supporting the hypothesis.
7. Do not edit solely because a stack trace names a file.
