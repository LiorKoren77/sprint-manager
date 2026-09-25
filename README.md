# Sprint Manager (standalone)

One supervised Claude agent per task, a manager loop that reports status, and a local browser
dashboard to watch progress and chat with each agent in round-robin.

The app is standalone and works on **any GitHub-hosted git repo** you register as a **project**. A
task can be free **text** you type, a **GitHub issue**, a **Slack thread**, or a **Jira issue**. Each
project's specifics (base branch, worktrees dir, build/test commands, preflight checks, CI provider,
Jira site) live in its **profile** — see [`docs/projects.md`](docs/projects.md).

## Run

```bash
~/sprint-manager/sprint-manager       # first run builds the venv automatically
# → open the URL the server prints (http://127.0.0.1:8766/?token=…)
```

Register a repo with **＋ Project** in the dashboard, or
`cd scripts && python3 -m sprint_manager.project add --repo ~/src/myrepo`. A profile with just
`repo = "..."` works; everything else has a default.

The per-task agents run on your **logged-in Claude account** (`claude` → `/login` once; no
`ANTHROPIC_API_KEY` needed — the agents deliberately blank it so billing goes to the subscription).
You also need `gh auth login`, plus whatever credentials your projects' profiles name (Jira, Jenkins)
and optionally `SLACK_BOT_TOKEN` — see `references/setup.md`.

In the dashboard: pick a project, press **＋ New task** (*Describe it* / *GitHub issue* / *Slack
thread* / *Jira issue*, or *Load issues* / *Load sprint* for a batch) → open the task and press
**Start ▶**. Each task goes **explore** (recap, your guidance, acceptance criteria for tasks that
aren't groomed tickets, the plan — read-only) → **Approve plan** → **work** (implement, review,
test — one session; reply in chat to move between its gates) → **Ship ▶** (push + open the PR, no
agent) → **pr-open** (GitHub CI runs on push; Jenkins projects press **Trigger CI**; each CI/review
signal you engage starts a short triage session) → merge on GitHub → **Approve** → done.
**"Next ⚠ needs you"** cycles through agents waiting on you.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SPRINT_MANAGER_CONFIG_DIR` | `~/.config/sprint-manager` | project profiles (`projects/<name>/`) and `config.toml` (`default_project`) |
| `SPRINT_MANAGER_DEFAULT_PROJECT` | — | overrides `default_project` |
| `SPRINT_MANAGER_PORT` | `8766` | web service port |
| `SPRINT_MANAGER_MAX_ACTIVE` | `1` | how many agents may work at once, across all projects (serial by default) |
| `SPRINT_MANAGER_PR_POLL_SECONDS` | `900` | how often the pr-open feedback poll runs (CI + code review), 15 min |
| `SPRINT_MANAGER_STATE_DIR` | `~/sprint-manager/state` | where the task state store lives (set to a temp dir to isolate tests) |
| `SPRINT_MANAGER_BASH_MAX_OUTPUT` | `15000` | cap (chars) on each agent Bash result kept in context |
| `SPRINT_MANAGER_CONTEXT_WARN_TOKENS` | `150000` | context size that turns the panel indicator amber / triggers the compact hint |

## Layout

```
~/sprint-manager/
├── sprint-manager          # launcher (builds venv on first run, starts the service)
├── README.md
├── references/             # agent-system-prompt, ticket-context, state-machine, setup, stages/
├── examples/               # example project profiles
├── docs/                   # architecture, backend, frontend, lifecycle, projects, designs
├── web/                    # dashboard (index.html, app.js, styles.css)
├── state/                  # runtime status JSON + notes + task text per task (created on use)
└── scripts/
    ├── pyproject.toml      # python>=3.11; claude-agent-sdk, fastapi, uvicorn
    ├── sprint_manager/     # config, project, models, state, notes, taskfile, sources/, jira_client,
    │                       # confluence, fetch_sprint, sprints, branch, worktree, pr, ci, jenkins,
    │                       # slack, run_tests, preflight, background, report_stage, agent,
    │                       # orchestrator, server
    └── tests/              # unittest (97 tests); support.py isolates state + config
```

Run the tests with `cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests`.

Worktrees are created at `<project worktrees dir>/<TASK>` (default: a sibling of the repo named
`<repo>-worktrees`). Merging a PR is **manual** — you merge on GitHub yourself; the agents cannot
merge or start CI. See `references/state-machine.md` for the lifecycle.
