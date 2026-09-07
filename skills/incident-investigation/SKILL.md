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
3. Establish expected behavior from repository instructions, documented contracts,
   tests, callers, and relevant history. Contrast it with the observed symptom.
   An alert or stack trace alone does not define the correct behavior.
4. Inspect recent relevant changes and trace the failing path and shared callers.
5. Attempt to reproduce the failure; distinguish code defects from invalid input,
   configuration, infrastructure, and upstream failures.
6. Record a specific root-cause hypothesis, supporting evidence, expected behavior,
   and a regression scenario that would distinguish a repair from hiding the symptom.
7. If the intended behavior is ambiguous or evidence contradicts the hypothesis,
   gather the missing evidence or report the precise blocker. Do not invent a product
   requirement or force every incident into a code change.
