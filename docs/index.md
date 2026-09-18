---
title: Incident Harness documentation
---

# Incident Harness documentation

Configure the agent that investigates production incidents, prepares fixes, and verifies them through pull requests and preview deployments.

- [Configuration reference](configuration.md): every TOML option, its default, accepted values, and runtime meaning.
- [Example configuration](configuration-example.toml): a starting point for one GitHub repository and Grafana webhook intake.
- [Tool extension contract](tool-registry.md): configure an operator-managed tool registry.
- [Verification record](https://github.com/Isaac12x/incident-resolver-agent/blob/master/docs/verification.md): historical implementation checks.

## Choose a configuration file

The CLI selects configuration in this order:

1. An explicit `incident-agent --config /absolute/path/config.toml ...` argument.
2. The `INCIDENT_AGENT_CONFIG` environment variable.
3. `.agent/config.toml` when the current directory contains that file or an `.agent` directory.
4. `$XDG_CONFIG_HOME/incident-harness/config.toml`, defaulting to `~/.config/incident-harness/config.toml`.

When running from a source checkout, `init` defaults to `.agent/config.toml` unless `--config` or `INCIDENT_AGENT_CONFIG` is supplied.

Run `incident-agent init` to initialize configuration, then `incident-agent config` to edit it in the terminal UI. `incident-agent tui` opens the same editor.

New per-user configurations use `$XDG_STATE_HOME/incident-harness` for state (default `~/.local/state/incident-harness`), enable `server.require_api_auth`, and add a Grafana webhook connector. For a missing or empty explicit config, the loader initially uses the config file's parent as `runtime_root`. If an authenticated supported Codex CLI is available, initialization selects `subscription-cli`; otherwise it selects `agents-sdk`.

The [reference](configuration.md) lists schema defaults, which differ from these initialization choices. In an existing TOML file, relative paths such as `runtime_root` are relative to the worker's current directory, not automatically to the configuration file. Prefer absolute paths in service deployments.

## Configure and check the worker

Download the [example TOML](configuration-example.toml), replace `example/service` and its clone URL with your repository, and set the correct base branch and preview test command. The example uses the API runtime explicitly.

```sh
incident-agent --config /absolute/path/config.toml config
incident-agent --config /absolute/path/config.toml doctor
incident-agent --config /absolute/path/config.toml run
```

Store credentials in the worker environment. Fields ending in `_env` contain **variable names**, never secret values. For the example, supply `OPENAI_API_KEY`, `INCIDENT_AGENT_API_TOKEN`, `AGENT_WEBHOOK_SECRET`, and `GITHUB_WEBHOOK_SECRET`, and authenticate the host's GitHub CLI for repository access. The application loads `.env` from the current directory and configuration directory without overriding variables already in the environment.

The example receives signed incident webhooks at `/hooks/incidents/grafana`. GitHub webhooks use `/hooks/github`. Protected control APIs use `Authorization: Bearer TOKEN`; they do not replace webhook HMAC signatures.

After changing configuration, restart the worker. If an immutable bundle is active, update the active bundle as well: its snapshot supplies the runtime configuration.

## Publish with GitHub Pages

These documents are a Jekyll site using a GitHub Pages-supported theme. No custom deployment workflow is required for branch publishing.

After merging, open the repository's **Settings → Pages**, choose **Deploy from a branch**, then select **master** and **/docs**, and save. Use the branch containing these documents if the default branch changes. GitHub builds `index.md` as the site entry point. Relative Markdown links are converted by `jekyll-relative-links`, so they also work under a project-site URL prefix.

See GitHub's [publishing-source instructions](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site) for permissions and repository availability. Adding these files prepares the site; it does not change the repository's Pages settings or publish a deployment.
