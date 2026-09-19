---
name: code-review
description: "Review incident changes against repository standards and the originating incident specification."
triggers:
  - "review code changes"
  - "review pull request"
  - "review incident fix"
---

Review the diff from the current PR's actual merge-base. Use two separate axes and report them
side by side:

- **Standards**: does the change follow the repository's documented coding standards? If no
  standards are documented, use the smell heuristics below as labelled judgement calls.
- **Spec**: does the change satisfy the originating incident, investigation, or review request?
  The incident and its evidence are the spec when no separate issue text is available. Do not
  invent product requirements or ask the operator to configure an issue tracker.

Pin the fixed point with `git merge-base HEAD <base>` and inspect `git diff <fixed-point>...HEAD`
plus the commit list. Also inspect uncommitted tracked/index changes with
`git diff <fixed-point>` and newly added files with
`git ls-files --others --exclude-standard`. Review the actual working diff and current
merge-base, not an imagined target.
Find repository standards in files such as `AGENTS.md`, `CONTRIBUTING.md`, and
`CODING_STANDARDS.md`. Treat explicit repository rules as authoritative.

Run the Standards and Spec reviews as independent parallel investigations when sub-agents are
available, then aggregate without masking one axis with the other. Standards findings cite the
rule and file. Spec findings cite the incident evidence or request. Under each axis, distinguish
missing requirements, scope creep, and behavior that appears implemented incorrectly.

Use these smell heuristics only as labelled judgement calls: Mysterious Name, Duplicated Code,
Feature Envy, Data Clumps, Primitive Obsession, Repeated Switches, Shotgun Surgery, Divergent
Change, Speculative Generality, Message Chains, Middle Man, and Refused Bequest. Skip anything
already enforced by tooling, and let documented repository standards override a heuristic.

Keep the two reports separate. End with counts and the worst issue within each axis; do not choose
a single winner across Standards and Spec.

Adapted from Matt Pocock's `code-review` skill at commit
`c55ee46073ed923f86ce59a5eb3b6d895095d1b7`, under the included MIT license.
