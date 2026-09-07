---
name: ponytail
description: "Choose the smallest correct incident repair using existing code, standard libraries, and platform features."
license: MIT
---

# Ponytail for incident repair

Adapted for this harness from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail).
The upstream MIT notice is in [LICENSE](LICENSE). This adaptation scopes the guidance
to incident work and preserves the harness's verification and output contracts.

Understand the expected behavior, failure path, and affected callers before trying
to shorten the solution. A small patch in the wrong place is still a wrong fix.

Choose the first sufficient option:

1. No code change, when evidence identifies an operational issue or expected behavior.
   Report the evidence and required action within the configured permissions.
2. Reuse or correct an existing implementation in this repository.
3. Use the standard library or a native platform feature.
4. Use an already installed dependency.
5. Write only the additional code needed to restore the established contract.

Before handing off the change, compare the diff against the regression scenario.
Remove unnecessary abstractions, wrappers, configuration knobs, and unrelated edits.
Prefer clear code over compressed one-liners. Do not remove validation, error handling,
security, accessibility, or tests to reduce the line count. Add a dependency only when
the existing tools cannot reasonably solve the demonstrated problem and policy allows it.

Repeat this check when verification reveals a problem: revisit the cause, simplify the
repair, and verify again within the existing task budget. Stop when the intended behavior
is restored and required checks pass, or record an evidence-backed blocker. Keep this
reasoning in the skill; use the harness's existing lifecycle and verification tools.
