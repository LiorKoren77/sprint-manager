# Sprint Manager — Architecture (high level)

## Purpose

Drive **one supervised Claude agent per task** — free text, a GitHub issue, a Slack thread, or a
Jira issue, in any registered **project** (a GitHub-hosted repo described by a profile) — through a
fixed lifecycle
(explore → work → *ship* → pr-open), with a human (the "manager") supervising via a browser
dashboard. The agent **stops at every gate for the manager's decision**; the manager watches concise
summaries and approves progression. explore (recap → questions → plan, read-only) and work
(implement → review → test plan → run → ready) are each one session; ship is a zero-LLM action
(**Ship ▶**); in pr-open, CI verdicts and code reviews arrive in any order and each one the manager
engages starts a short triage episode. Agents never start CI (GitHub checks run on push; Jenkins
builds start from **Trigger CI**); the manager merges the PR on GitHub manually and can jump a task
back (*go to stage*) when feedback demands a re-plan or rework.

## System shape

```
┌─────────────────────────── Browser (web/) ───────────────────────────┐
│  project filter · New task · status table · chat tabs · gates         │
└───────────────▲───────────────────────────────────▲──────────────────┘
        REST (status/control)                WebSocket (live chat/summaries)
                │                                     │
┌───────────────┴─────────────────────────────────────┴──────────────────┐
│  FastAPI service  (scripts/sprint_manager/server.py)                    │
│                                                                          │
│  Orchestrator (orchestrator.py) — the "manager loop"                     │
│   • dict[task → TicketAgent]        • MAX_ACTIVE turn semaphore          │
│   • task intake (sources/)          • project profiles (project.py)      │
│   • status store (state/*.json)     • CI/PR poll notifies (+CI-fail triage)│
│   • event pub/sub → WebSocket       • event filter (summaries only)      │
│                                                                          │
│  TicketAgent (agent.py) = a Claude Agent SDK session, cwd = worktree     │
│   • prompt per (project, stage)    • merge/force-push/CI-start blocked   │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  Anthropic API (your logged-in Claude subscription)
                ▼
        Claude (per-stage model tier, see models.py) + the session store (~/.claude/projects/.../*.jsonl)
```

## Two layers

1. **Zero-LLM CLI layer** (stdlib only): `project`, `sources/*`, `taskfile`, `jira_client`,
   `confluence`, `fetch_sprint`, `sprints`, `branch`, `worktree`, `pr`, `ci`, `jenkins`, `slack`,
   `run_tests`, `preflight`, `background`, `report_stage`, `state`, `notes`, `models`.
   Deterministic "fast actions" — load projects, load tasks from their sources, make/sync branches
   & worktrees, open PRs, publish plans to Confluence, run test suites, read CI status/logs, check
   merge readiness, record status. Every mechanical (non-judgment) task lives here so it costs no LLM turn or
   context. Runnable and testable without installing anything. **Detail: `backend.md`.**
2. **Orchestration layer** (needs the venv): `agent`, `orchestrator`, `server`. Drives the
   per-task Claude sessions and serves the UI. **Detail: `backend.md` + `frontend.md`.**

## Projects, task sources, CI providers

The app is standalone and names no particular repo. Three pluggable axes (full reference:
`projects.md`; rationale: `design-generalize.md`):

- **Project** = a repo + how to work in it, from a profile (`~/.config/sprint-manager/projects/<name>/
  project.toml`, optionally overridden by `<repo>/.sprint-manager/project.toml`): main checkout,
  base branch, worktrees dir (default `<repo>-worktrees/`), checkout env var, extra readable dirs and
  skills, Markdown notes injected into the prompts (all stages / per stage / test commands),
  preflight checks, CI, and an optional Jira/Confluence site.
- **Task source** (`sources/`): `text`, `github`, `slack`, `jira` — each supplies the task content,
  the commit/PR reference, a PR-body footer, best-effort lifecycle side effects, and optional
  tracker-side feedback for the pr-open poll.
- **CI provider** (`ci.py`): `github` (checks, run on push), `jenkins` (manual trigger), `none`.

Repo-specific knowledge (build quirks, which plugins to use, test commands) lives in the project's
notes files and reaches the agent through the system prompt, which stays fixed per (project, stage)
and so caches. **Detail: `agent-lifecycle.md`.**

## Key design decisions

| Decision | Why |
|---|---|
| One agent per task, **serial** by default (`MAX_ACTIVE=1`, app-wide across projects) | keeps the manager in the loop; every agent draws on the same subscription |
| **Project specifics in profiles, not code** | core code/prompts/UI are repo-agnostic (a guard test fails on a project name in core); a profile + its notes files carry build/test/CI/tracker knowledge |
| **Pluggable task sources** | text, GitHub issue, Slack thread and Jira share one lifecycle; tasks that aren't groomed tickets get an assumptions + acceptance-criteria step in explore |
| **Fully user-gated** (gates inside a stage via chat; Approve / Ship ▶ between stages) | nothing runs autonomously; you review every gate, especially the plan, before it proceeds |
| **Sessions where context changes, not at every gate** — explore and work are one session each; pr-open is one short episode per signal | no lossy handoffs between orient/plan or implementation/testing; pr-open wakes long after the prompt cache expired, so it never resumes a huge session; fresh sessions are seeded from a per-stage view of `notes.md` |
| **Ship is a zero-LLM action** | commit check, final recap + PR text (+ source footer, e.g. `Fixes #N`), push, open PR, tracker update — no agent for mechanical work |
| **Summaries only** in the UI (drop thinking/tool events) | the manager follows progress without the firehose |
| **git worktree** isolation, created when work starts | parallel-safe edits, main checkout untouched, shares the repo's `.git` |
| **Agents never start CI** | GitHub checks run on push (dashboard: **Re-run CI**); Jenkins builds start only from **Trigger CI**; every CI start/re-run command is permission-blocked for agents |
| **pr-open polls two channels every 15 min** — CI verdict AND code review (PR + tracker comments) | keeps polling until both fire, then stops (re-arms when CI is re-triggered or on the next `waiting_external`); a fire notifies + lights the tab with no agent turn, except a CI failure auto-triages |
| **Merge is manual** — the manager merges on GitHub | `gh pr merge` is permission-blocked for agents; `pr.py` has no merge command |
| **session_id + notes persisted in state** | resume a task across restarts; the notes file is the durable cross-stage memory |
| **"Go to stage" control** | manager can jump any task to explore / work / pr-open without restarting the service; disposes current session, resets state |
| **Model overrides in `state/config.json`** | model+effort per stage is configurable via the settings UI; persists across restarts; takes effect on next stage start without a server restart |
| **MCP-free; skills per project** | `strict_mcp_config=True` on EVERY stage and no skills by default to cut cache cost; Jira, Confluence and Slack go over REST. A profile's `skills` list re-enables specific skills for that project |
| **Mechanical work → Python, not LLM** | every judgment-free task (task intake, Confluence publish, test run, infra check, CI-log fetch, merge-readiness, branch sync, tracker updates) is a zero-LLM script or orchestrator side effect; only judgment (planning, diagnosis, classification) spends turns |

## Configuration (environment)

`SPRINT_MANAGER_CONFIG_DIR` (~/.config/sprint-manager) · `SPRINT_MANAGER_DEFAULT_PROJECT` ·
`SPRINT_MANAGER_PORT` (8766) · `SPRINT_MANAGER_MAX_ACTIVE` (1) · credentials: your Claude login,
`gh auth login`, and the env-var names each project's profile declares (Jira, Jenkins) plus
`SLACK_BOT_TOKEN`. Everything repo-specific is in the project profile (`projects.md`).

Runtime model/effort overrides are stored in `state/config.json` and managed via the ⚙ settings
drawer in the dashboard (no restart needed).

## Where to change what

| You want to… | Go to |
|---|---|
| change the lifecycle / approval behaviour | `references/agent-system-prompt.md` (+ `agent-lifecycle.md`) |
| change what the dashboard shows / its controls | `web/` (+ `frontend.md`) |
| change orchestration (concurrency, polling, event filtering) | `orchestrator.py` (+ `backend.md`) |
| change model/effort per stage at runtime | settings drawer in the UI (⚙), or edit `state/config.json` |
| change model/effort per stage in code | `MODEL_BY_STAGE` in `models.py` |
| teach the agents about one repo (build, tests, plugins) | that project's profile + notes files (`projects.md`) |
| add skills for a project / MCP access for a stage | profile `skills` / `MCP_OPEN_STAGES` in `agent.py` |
| add a task source | `sources/<name>.py` implementing `sources.Source` (+ `ID_TAGS`) |
| add a CI provider | `ci.py` (`verdict` / `logs` / `trigger` / `auto_triggers`) + `_CI_TEXT` in `agent.py` |
| add a deterministic action (a new script) | `scripts/sprint_manager/*.py` (+ `backend.md`) |
| change Jira/branch behaviour | `sources/jira.py` + `jira_client.py` / `branch.py` |
