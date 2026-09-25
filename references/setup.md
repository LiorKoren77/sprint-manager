# Sprint Manager — setup

## 1. Python environment (first run only)

The launcher does this automatically — `~/sprint-manager/sprint-manager` builds the venv on first
run. To do it by hand:

```bash
cd ~/sprint-manager/scripts
python3 -m venv .venv
./.venv/bin/pip install -e .          # installs claude-agent-sdk, fastapi, uvicorn
```

The zero-LLM CLI layer (projects, sources, sprint fetch, branch, worktree, PR, CI status) uses only the standard
library, so those scripts run with a plain `python3` once `PYTHONPATH` points at `scripts/`.

## 2. Credentials

| Variable | Used for | Required |
|---|---|---|
| (Claude login) | the per-ticket Claude agents | yes (agents) — see below |
| (`gh auth login`) | PRs, GitHub-issue tasks, GitHub CI checks | yes |
| `JIRA_EMAIL` / `JIRA_API_TOKEN` (or the names a profile's `[jira]` sets) | Jira + Confluence, for projects that use them | per project |
| `JENKINS_USER` / `JENKINS_API_TOKEN` (or the names a profile's `[ci]` sets) | Jenkins CI polling, for projects that use it | per project |
| `SLACK_BOT_TOKEN` | Slack-thread tasks, and fetching a linked Slack thread for context in any stage | optional |

Which site, board, CI server and credential variable names a project uses is set in its **project
profile** (`docs/projects.md`), not here.

`SLACK_BOT_TOKEN` is a bot token (`xoxb-...`) from a Slack app installed to the workspace, scoped
to `channels:history`, `groups:history`, `users:read`, `channels:read` (add `groups:read` too for
private channels). If a fetch fails with `not_in_channel`, invite the bot to that channel. The
optional Slack write-backs a profile can enable (`[slack] react`, `reply_on_ship`) additionally need
`reactions:write` / `chat:write`.

Three equivalent ways to set these (mix and match):

1. **⚙ Settings → Credentials** in the running dashboard — types straight into `.env` (see below)
   and applies immediately, no restart. Also shows a green/red status for every credential,
   including the two that aren't env vars (`gh` CLI login, Claude account login).
2. **`.env` file** at the app root — `cp .env.example .env` and fill it in. Loaded automatically at
   startup (`scripts/sprint_manager/config.py`); gitignored, so it's safe for real secrets.
3. **Shell profile** (`~/.bashrc` / `~/.zshrc`) — `export JIRA_API_TOKEN=...` etc.

The Settings list is built from your registered projects' profiles, so it shows exactly the
variable names they declare (e.g. a profile whose `[ci] user_env = "MY_CI_USER"`).

A real shell-exported variable always wins over `.env`, so switching to `.env`/Settings never
surprises anyone who already has these exported. The dashboard starts without any tracker
credentials; only the features that need them (loading a Jira sprint, polling Jenkins, Slack
threads) fail, with a message naming the missing variables. Sites on a private network (a
corporate Jira or Jenkins) need that network/VPN.

**Claude authentication:** the per-ticket agents run on your **logged-in Claude account**
(team/subscription). Log in once with `claude` (`/login`) — credentials live in
`~/.claude/.credentials.json`. You do **not** need `ANTHROPIC_API_KEY`; the agents deliberately
blank it for their CLI subprocess so billing goes to the subscription, not a pay-as-you-go key.
(A project's own test suites may need their own keys — that is unrelated to the agents.)

## 3. Projects

Register each repo you want to work on:

```bash
cd ~/sprint-manager/scripts
python3 -m sprint_manager.project add --repo ~/src/myrepo     # or ＋ Project in the dashboard
python3 -m sprint_manager.project show --project myrepo
```

With more than one project, set `default_project = "<name>"` in `~/.config/sprint-manager/config.toml`.
Every profile setting (build/test notes, preflight, CI provider, Jira) is documented in
`docs/projects.md`; `examples/` has starting points.

## 4. Run

```bash
~/sprint-manager/sprint-manager               # launcher: builds venv if needed, starts service
# or by hand:
cd ~/sprint-manager/scripts && ./.venv/bin/python -m sprint_manager.server
# then open the URL the server prints (http://127.0.0.1:8766/?token=…)
```

## Concurrency

Serial by default (one agent works at a time, across all projects). To allow more, export
`SPRINT_MANAGER_MAX_ACTIVE=<n>` before starting the service. 1 is the default because every agent
draws on the same Claude subscription and its limits, and one at a time keeps you in the loop.
