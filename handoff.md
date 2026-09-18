
## Configuration documentation — 2026-09-18

Branch: docs/configuration-reference, based on origin/master (1597261).
Added a GitHub Pages/Jekyll documentation home, complete 99-option configuration reference,
example TOML, and Pages configuration. Linked the docs from README and updated CHANGELOG.
Validated all documented option names against Config.model_json_schema(), parsed and validated
example TOML through Config, checked new Markdown links/anchors, and ran git diff --check.
YAML/front matter parsing and Markdown rendering passed for all 99 TOML option rows.
Also documented six environment-only configuration/installer controls.
Full Jekyll build could not run: RubyGems DNS lookup timed out during dependency setup.
No live Pages deployment or settings change performed.
Original checkout's uncommitted work is preserved; changes live in the isolated worktree
/tmp/incident-harness-configuration-docs. No VISION.md exists on the source or default branch.

PR: https://github.com/Isaac12x/incident-resolver-agent/pull/20
Initial review check at approximately 2026-09-18 08:10 UTC: mergeable, no general or inline
comments, reviews, or CI checks. Follow-up monitoring is in progress.
