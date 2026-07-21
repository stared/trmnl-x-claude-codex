# TRMNL X — Claude Code + Codex usage screen

Pushes rate-limit status of Claude Code and Codex to a TRMNL private plugin
every 10 minutes from this Mac.

- **Claude Code**: undocumented `https://api.anthropic.com/api/oauth/usage`
  endpoint; OAuth token read from the macOS Keychain (`Claude Code-credentials`).
  Unofficial — may change without notice; rate-limits aggressively, so don't
  poll more often than every ~10 min.
- **Codex**: `codex app-server` JSON-RPC (`account/rateLimits/read`); the CLI
  handles its own auth.
- **$ per day + top projects**: [ccusage](https://ccusage.com) via `pnpm dlx`
  (API-equivalent cost across all agents it detects), with session→project
  mapping from `~/.claude/projects/**/*.jsonl` (`cwd` field).

## Setup

1. On [trmnl.com](https://trmnl.com) create a **Private Plugin**, strategy
   **Webhook**. Copy the webhook URL (`https://trmnl.com/api/custom_plugins/<uuid>`).
2. `cp config.json.example config.json` and paste the URL. Keep `config.json`
   out of git.
3. Paste `template.liquid` into the plugin's Markup editor and save.
4. Test once by hand:

   ```sh
   uv run --no-project push_usage.py            # pushes to TRMNL
   uv run --no-project push_usage.py --dry-run  # prints payload only
   ```

5. Install the launchd agent (runs every 10 min + on login/wake):

   ```sh
   cp com.pmigdal.trmnl-usage.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pmigdal.trmnl-usage.plist
   ```

   Logs: `/tmp/trmnl-usage.log`. Uninstall with
   `launchctl bootout gui/$(id -u)/com.pmigdal.trmnl-usage`.

## Merge variables sent

`claude_plan`, `claude_session_pct` / `_reset`, `claude_weekly_pct` / `_reset`,
`claude_scoped` (list of per-model weekly limits: `name` / `pct` / `reset`),
`codex_plan`, `codex_primary_pct` / `_label` / `_reset`, `codex_secondary_*`
(when the plan has a second window), `cost_days` (7 entries: `d` day label,
`c` $ rounded, `h` bar height 0–100), `cost_week`, `top_projects` (`name` /
`c`), `claude_ok` / `codex_ok` / `costs_ok` / `projects_ok`, `updated_at`.

`config.json` (webhook UUID) is gitignored — anyone with the UUID can push
screens to your device.

The screen goes stale while the Mac sleeps — the `updated at` stamp in the
title bar shows the last successful push.
