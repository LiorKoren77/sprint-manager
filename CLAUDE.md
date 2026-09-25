# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Sprint Manager is a standalone Python/FastAPI application that orchestrates autonomous Claude agents to work through **tasks** in any GitHub-hosted git repo. Each agent handles one task through a fixed lifecycle (explore → work → *ship* → pr-open), supervised by a manager via a browser dashboard. explore and work are each ONE session with internal gates; ship is a zero-LLM action (**Ship ▶**); pr-open runs one short triage session per CI/review signal. Design rationale: `docs/design-explore-work-triage.md` (the stages) and `docs/design-generalize.md` (projects × task sources × CI providers).

Three independent axes:

- **Projects** — each repo is described by a profile (`project.toml`: repo, base branch, worktrees dir, test commands, preflight, CI, Jira…). Profiles live in `~/.config/sprint-manager/projects/<name>/` (personal) and optionally `<repo>/.sprint-manager/` (committed). **Full reference: `docs/projects.md`.** The core code names no particular repo or company — a guard test enforces it.
- **Task sources** (`sources/`) — a task is free **text**, a **GitHub issue**, a **Slack thread**, or a **Jira issue**. Same lifecycle for all; the source supplies content, the commit/PR reference, a PR-body footer, and best-effort tracker side effects.
- **CI providers** (`ci.py`) — `github` (default: GitHub checks, runs on push), `jenkins` (manual trigger), `none`.

Worktrees are created at `<project worktrees dir>/<TASK>` (default `<repo parent>/<repo name>-worktrees/`).

## Quick Start

```bash
# First run (creates venv automatically)
~/sprint-manager/sprint-manager

# Register a repo (or use ＋ Project in the dashboard)
cd ~/sprint-manager/scripts && python3 -m sprint_manager.project add --repo ~/src/myrepo

# Then open the URL the server prints (http://127.0.0.1:8766/?token=…)
```

Requires: a logged-in Claude account (`claude` → `/login`; the agents deliberately blank `ANTHROPIC_API_KEY` so billing goes to the subscription), `gh auth login`, and whatever credentials your projects' profiles name — Jira (`[jira] email_env`/`token_env`, default `JIRA_EMAIL`/`JIRA_API_TOKEN`), Jenkins (`[ci] user_env`/`token_env`, default `JENKINS_USER`/`JENKINS_API_TOKEN`) — plus optionally `SLACK_BOT_TOKEN` (Slack-thread tasks and fetching linked threads). Set these via shell profile, a local gitignored `.env` (`cp .env.example .env`), or **⚙ Settings → Credentials** (which lists exactly the variables your profiles name) — all three are equivalent and a real shell export always wins. See `references/setup.md`.

## Project Structure

```
scripts/sprint_manager/          # Python package
├── config.py                    # Paths, per-project credential lookup (Jira/Confluence/Jenkins), .env loading
├── project.py                   # Project profiles: load/merge/resolve/register (+ CLI: list/show/add)
├── models.py                    # Stage/Activity enums, per-stage model map, stage aliases, TicketStatus
├── state.py                     # Status store (state/*.json)
├── notes.py                     # Per-task notes file + per-stage notes view + acceptance-criteria extraction
├── taskfile.py                  # state/<TASK>.task.md — a text task's problem statement / cached tracker content
├── sources/                     # Task sources: jira, github, slack, text (+ registry, task ids)
├── report_stage.py              # Agent's status channel
├── jira_client.py               # Jira REST API (stdlib urllib): issues, transitions, comments
├── confluence.py                # Publish plan to Confluence + read inline comments, over REST (no MCP)
├── fetch_sprint.py              # Fetch a Jira sprint's issues
├── sprints.py                   # List a Jira board's sprints (current/next)
├── branch.py                    # Branch naming (≤80 char, bugfix/feature prefix)
├── worktree.py                  # git worktree operations (add/remove/list/sync/state), per project
├── pr.py                        # gh-based PR operations (push/open/comments/ready/review signal)
├── ci.py                        # CI dispatcher: github checks / jenkins / none (status, logs, trigger)
├── jenkins.py                   # Jenkins provider (classic REST API), configured by [ci]
├── slack.py                     # Slack Web API: fetch a thread, optional reaction/reply
├── run_tests.py                 # Run a suite → compact JSON verdict
├── preflight.py                 # Deterministic infra check from the profile's [preflight]
├── background.py                # Launch + poll a slow compile/suite in the background
├── agent.py                     # TicketAgent (wraps ClaudeSDKClient), prompt rendering, agent env
├── orchestrator.py              # Manager loop, task intake, serialization, polling
└── server.py                    # FastAPI + WebSocket

scripts/tests/                   # unittest (97 tests, 9 modules); support.py isolates state + config dirs
web/                             # Dashboard (index.html, app.js, styles.css)
references/                      # Agent system prompt (ticket-agnostic), ticket-context.md (first-message template), stage prompts
examples/                        # Example project profiles (generic)
docs/                            # Architecture, backend, frontend, agent lifecycle, projects, designs
```

## Architecture

**Two layers:**

1. **Zero-LLM CLI layer** (stdlib only): `project`, `sources/*`, `taskfile`, `jira_client`, `confluence`, `fetch_sprint`, `sprints`, `branch`, `worktree`, `pr`, `ci`, `jenkins`, `slack`, `run_tests`, `preflight`, `background`, `report_stage`, `state`, `notes`, `models`. Deterministic, fast, testable without LLM. Every mechanical (non-judgment) task lives here so it costs no agent turn or context. CLI modules resolve their project from `--project`, else the agent's `$SM_PROJECT`, else the task's stored project, else the default project.

2. **Orchestration layer** (needs venv): `agent`, `orchestrator`, `server`. Drives Claude sessions, serves UI.

**System shape:**
- Browser dashboard ↔ FastAPI service ↔ Claude Agent SDK ↔ Anthropic API
- Orchestrator manages per-task agents, serializes turns (`MAX_ACTIVE` semaphore, app-wide across projects), handles CI/PR polling, filters events for UI
- Each agent works one stage at a time and stops at every gate for the manager

## Key Design Decisions

| Decision | Rationale |
|---|---|
| **Serial by default** (`MAX_ACTIVE=1`, app-wide) | Manager stays in the loop; every agent draws on the same Claude subscription regardless of project |
| **User-gated stages** | Nothing runs autonomously; every gate and every advance requires you (especially the plan) |
| **Sessions where context changes, not per gate** | explore and work are one session each; pr-open is one short episode per signal; fresh sessions are seeded from a per-stage view of `notes.md` |
| **Project specifics in profiles** | Core code, prompts and UI are repo/company-agnostic (guard test); a repo's build/test/CI/tracker knowledge lives in its profile and its Markdown notes files |
| **Pluggable task sources** | Text, GitHub issue, Slack thread, Jira — one interface (`sources.Source`); side effects are best-effort, never load-bearing |
| **Ship is a zero-LLM action** | commit check, final recap + PR text, push, open PR, tracker update — no agent for mechanical work |
| **git worktree isolation** | Parallel-safe edits; main checkout untouched; shares `.git` |
| **Agents never start CI** | GitHub checks run on push by themselves; Jenkins builds start only from the dashboard's **Trigger CI**; every trigger/re-run command is permission-blocked for agents |
| **Merge is manual** | The manager merges the PR on GitHub, outside the app; the agent command guard blocks merge commands (best-effort) |
| **No MCP** | Every stage runs `strict_mcp_config=True`; Jira/Confluence/Slack go over REST |
| **Cacheable system prompt** | The system prompt is task-agnostic (fixed per project + stage); the task and notes view travel in the first message |
| **Summaries only in UI** | Manager sees progress, not the thinking/tool firehose |
| **Model overrides at runtime** | ⚙ settings drawer → `state/config.json`; takes effect on next stage start |

## Common Commands

### Development

```bash
# Run the service (first run builds venv)
./sprint-manager
# or:
cd scripts && ./.venv/bin/python -m sprint_manager.server

# Tests (unittest; tests/support.py points state + config at a temp dir)
cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests

# The zero-LLM CLI layer (no venv needed; PYTHONPATH=.)
cd scripts && export PYTHONPATH=$PWD
python3 -m sprint_manager.project list | show --project NAME | add --repo PATH
python3 -m sprint_manager.worktree list --project NAME
python3 -m sprint_manager.worktree state --ticket TASK
python3 -m sprint_manager.ci status --ticket TASK
python3 -m sprint_manager.preflight --project NAME
python3 -m sprint_manager.fetch_sprint --project NAME --sprint "name" --assignee me   # Jira projects
```

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `SPRINT_MANAGER_CONFIG_DIR` | `~/.config/sprint-manager` | where project profiles (`projects/<name>/`) and `config.toml` (`default_project`) live |
| `SPRINT_MANAGER_DEFAULT_PROJECT` | — | overrides `default_project` (else the only registered project) |
| `SPRINT_MANAGER_PORT` | `8766` | Web service port |
| `SPRINT_MANAGER_MAX_ACTIVE` | `1` | Max concurrent agent turns (app-wide) |
| `SPRINT_MANAGER_PR_POLL_SECONDS` | `900` | pr-open feedback poll interval (CI verdict + code review), 15 min |
| `SPRINT_MANAGER_BG_POLL_SECONDS` | `20` | backgrounded shell job (slow compile/suite) poll interval — pure local check, so it's tight |
| `SPRINT_MANAGER_STATE_DIR` | `<app>/state` | task state store location; point at a temp dir to isolate tests from real tasks |
| `SPRINT_MANAGER_TOKEN` | random per start | the API/WebSocket access token (the server prints the URL with it); set for a stable, bookmarkable one |
| `SPRINT_MANAGER_ALLOWED_HOSTS` | — | extra `Host` values the API accepts (comma-separated; tests use `testserver`) |
| `SPRINT_MANAGER_BASH_MAX_OUTPUT` | `15000` | cap (chars) on each agent Bash result kept in context (→ CLI `BASH_MAX_OUTPUT_LENGTH`) |
| `SPRINT_MANAGER_CONTEXT_WARN_TOKENS` | `150000` | context size at which the panel indicator turns amber and work's G2 compact hint fires |

Set by the app **in every agent's environment**: `SM_TICKET` (task id), `SM_REF` (reference tag: Jira key / `#<n>` / empty), `SM_WORKTREE`, `SM_PROJECT`, `SM_REPO` (main checkout), plus the profile's `checkout_env` (if any) = the agent's checkout.

## Tasks

| Source | Added via | Task id | Commits / PR title | PR body footer | Tracker side effects |
|---|---|---|---|---|---|
| text | ＋ New task → *Describe it* | `<project>-t-<n>` | — | — | none (✎ Problem edits the text; ⇪ File as issue promotes it) |
| github | *GitHub issue* (URL / `#N`), *Load issues* | `<project>-gh-<n>` | `#<n>` | `Fixes owner/repo#<n>` | optional self-assign / label; GitHub closes on merge to default branch (checked at Done) |
| slack | *Slack thread* (link) | `<project>-slack-<n>` | — | thread link | optional 👀 / PR-link reply / ✅ (`[slack]`) |
| jira | *Jira issue* (key/URL), *Load sprint* | the Jira key | the key | — | In Progress → In Review + PR comment (new PR only) → Done |

`TicketStatus` carries `project`, `tracker`, `kind` (bug/feature → branch prefix), `external_ref`, `external_url`; legacy rows migrate to `tracker = "jira"`. Tasks that aren't groomed tracker tickets (text, GitHub, Slack) are marked **not groomed** in the first message (`ticket-context.md` `Source:` line): explore G1 then also asks for assumptions + a numbered `### Acceptance criteria` list, which work G4 checks and Ship puts in the PR body.

## Stages (User-Gated Lifecycle)

Stage ≠ gate. Each fresh session is seeded from a per-stage **view** of the task's durable `notes.md` (`notes.view`: latest section per stage name wins; a pr-open session sees only the latest 3 triage episodes) + code on disk. Inside a stage the agent stops at numbered gates (`waiting_user`, note `G<n>: …`); a chat reply moves it to the next gate in the same session.

| Stage | Session | cwd | Purpose | Leave with |
|---|---|---|---|---|
| explore | one | project's main checkout (RO) | G1 recap + ask where to look (+ project G1 questions; + assumptions & acceptance criteria if not groomed) → G2 draft + iterate the plan; Confluence publish on request (Jira projects with a space); no edits | **Approve** (plan) |
| work | one | worktree | Worktree + tracker "work started" on entry. G1 implement + refine → G2 test plan → G3 run suites on your yes (infra auto-fix ≤10 turns) → G4 all committed, acceptance criteria checked, ready. Commits; never pushes / opens a PR / triggers CI | **Ship ▶** |
| *(ship)* | none | — | Orchestrator action: refuse dirty worktree / nothing ahead of the base branch; final recap + `### PR` title/body (+ source footer); push; open PR (idempotent); tracker `on_shipped`; lands pr-open — `waiting_external` if CI runs on push (github), else `waiting_user` ("press Trigger CI") | — |
| pr-open | one per signal | worktree | **15-min** poll of **both** CI + code review (PR comments + the task's tracker comments) until both fire; fire → `waiting_user` + `last_signal` + notify + tab lights (CI failure auto-triages). Each signal you engage starts a fresh **triage episode** (kickoff: signal + commit log + diffstat + your message); it ends at `waiting_external` (agent, CI button, or review re-arm). Merge is manual on GitHub | **Approve** (after you merge) |

Approve is refused (400) in work. Backward jumps use the panel's **go to stage** control: the session is disposed, its last summary is saved to notes, and the task goes `idle` at the target stage — press **Start ▶**.

Model defaults per stage (overridable in ⚙ settings): explore = Opus high, work = Opus high, pr-open = Sonnet high. `MAX_TURNS_BY_STAGE`: explore 45, work 130, pr-open 50. The per-session CACHE ALERT line is 1M (work: 3M); when work reaches G2 with context > `SPRINT_MANAGER_CONTEXT_WARN_TOKENS`, the orchestrator suggests ⟳ Compact once.

**Migration**: old stage names resolve via `STAGE_ALIASES` (orient/plan → explore, implementation/testing/ship → work, ci-build → pr-open). Model overrides saved under old stage names are ignored. Rows without `project` use the default project; rows without `tracker` are Jira.

## CI

`ci.py` dispatches on the project's `[ci] provider`:

- **github** (default) — verdict from one `gh pr view --json headRefOid,statusCheckRollup`; run watermark `<sha12>:<run ids>`; logs via `gh run view --log-failed`; the dashboard button is **Re-run CI** (`gh run rerun --failed`).
- **jenkins** — classic REST API, `[ci] url` / `job` template (`{pr}`) / credential env names; builds start only from **Trigger CI**.
- **none** — verdict always `no-build`; no CI button; only review is polled.

Prompts get provider-specific wording (`<<CI_POLICY>>`, `<<CI_GUARD>>` …) and a single command, `python -m sprint_manager.ci status|logs --ticket`.

## Concurrency & Sessions

- **Semaphore-gated turns**: `run_turn` holds `asyncio.Semaphore(MAX_ACTIVE)`. Agents in `waiting_user`/`waiting_external` hold no slot.
- **Session resume**: The Agent SDK persists every session to `~/.claude/projects/<cwd-hash>/<session-id>.jsonl`. We store `session_id` (+ `session_format`) in `state/<task>.json`. Stop/restart preserves progress.
- **PR polling** (`pr-open` dual-channel): Every `PR_POLL_SECONDS`, poll each pr-open task's CI verdict and review signal concurrently, and **keep polling until BOTH have fired**. A channel fires on any change vs its watermark (CI: new run id or changed verdict; review: higher comment count or changed decision). The review count includes the **task's tracker comments**: a linked GitHub issue's comments ride in the PR's own GraphQL call; a Slack thread's replies cost one API call per poll. A fire flips the task to `waiting_user` and notifies — **no agent turn** — EXCEPT a CI **failure**, which also auto-triages. The fired flags **re-arm** when CI is (re-)triggered and whenever the task next enters `waiting_external`. The review indicator is clickable to dismiss a useless review event (`POST /api/rearm-review/{ticket}`).
- **Background job polling** (any stage): an agent launches a slow compile/suite with `background.py start`, reports `waiting_external`, and stops; every `BG_JOB_POLL_SECONDS` the orchestrator checks for a finished job and flips the task to `waiting_user` with the exit code + log tail.
- **Tracker refresh**: GitHub-issue and Slack tasks are re-read at every fresh session (cached in `state/<task>.task.md`).

## Control Flow

```
UI: POST /api/tasks | /api/add | /api/load | /api/load-issues   # create tasks
UI: POST /api/start/{ticket}
  → Orchestrator.start(ticket)           # resume-aware
  → run_turn(ticket, prompt)             # under MAX_ACTIVE semaphore
      → _attach(ticket, stage)           # project → cwd/prompt/env; worktree + tracker hook on first use
      → agent.send(prompt, on_event)     # first message = task context + notes view + kickoff
      → (agent works to its next gate, writes "### Summary", report_stage, stops)
      → state.update(cost, session_id)

UI shows summary → reply in chat (next gate, same session)
                 → Approve (explore → work, pr-open → done) — POST /api/approve/{ticket}
                 → Ship ▶  (work → pr-open, zero-LLM)      — POST /api/ship/{ticket}
                 → Trigger / Re-run CI (pr-open)           — POST /api/trigger-ci/{ticket}
```

## Event Model (pub/sub → UI)

- Agent emits blocks → `on_event(kind, text)` with kinds: `thinking | text | tool | result`.
- `run_turn.on_event` **persists `session_id`** and **forwards only `VISIBLE_EVENT_KINDS = {user, text, system}`** to WebSocket.
- A synthetic **`status`** event (full `TicketStatus` JSON) is pushed at the end of every turn and on goto-stage. It is **live-only**: never persisted to the transcript file and filtered out of replay.

## Important Conventions

1. **Branch naming** (`branch.py`): `bugfix/<task>-<slug>` or `feature/<task>-<slug>` (from the task's `kind`), max 80 chars total.
2. **Tracker side effects**: driven by the orchestrator at stage boundaries through the task's source (`Orchestrator._hook` → `on_work_start` / `on_shipped` / `on_done`), best-effort and reported, never raised.
3. **Agent command guard** (`guard.py`, wired per stage by `agent.make_guard`): parses each Bash command (quote-aware segments, `shlex` tokens) and denies merging, force-pushing, any CI start/re-run, state-changing HTTP calls, pushing outside pr-open, and — in explore — anything off a read-only allowlist. **Defense in depth, not a sandbox.** Each stage's agent also only gets the credentials its tools need (`agent._agent_env`).
4. **Read-only stage** (explore): `Edit`/`Write` disabled in SDK as backstop.
5. **No project names in core** (`test_projects.NoRecouplingGuardTest`): project specifics belong in a profile. The words to forbid are yours, in the gitignored `scripts/tests/.forbidden-words` (the test skips without it).
6. **Local API access control** (`server.py` `LocalOnlyMiddleware`): every request/WebSocket must have Host `127.0.0.1`/`localhost:<port>` (anti DNS-rebinding), a matching `Origin` on WebSockets and state-changing requests (blocks other web pages), and — for `/api` and `/ws` — the per-launch token (`X-SM-Token` header; `?token=` on the WebSocket). The server prints the URL with the token at startup; set `SPRINT_MANAGER_TOKEN` for a stable one. Ticket ids in paths must match `state.valid_ticket`.
7. **Repo-local profiles are untrusted**: `<repo>/.sprint-manager/project.toml` may only set `prompts`, `preflight`, `base_branch`; credential URLs/env names, worktrees, `checkout_env`, `extra_dirs`, `skills` are personal-profile only.

## Relevant References

- `docs/projects.md` — project profiles (every setting), CI providers, task sources
- `docs/architecture.md` — full system architecture
- `docs/agent-lifecycle.md` — user-gated stages, compaction, behavior rules
- `docs/backend.md` — module responsibilities, control flow, testing
- `docs/frontend.md` — dashboard design
- `docs/design-explore-work-triage.md` — why three stages instead of six
- `docs/design-generalize.md` — why projects × task sources × CI providers
- `references/agent-system-prompt.md` — agent behavior rules (stop at every gate, summaries, delegation)
- `references/stages/<stage>.md` — per-stage prompts (explore, work, pr-open)
- `references/ticket-context.md` — the first-message template (task, source, reference, notes)
- `references/state-machine.md` — state transitions
- `examples/` — example profiles

## Common Debugging Scenarios

**Agent stuck**: Check `state/<task>.json` for `session_id`, activity, stage. Restart the server (`./sprint-manager`) to resume.

**Session lost**: The notes file (`state/<task>.notes.md`) is the durable memory. If the session jsonl is corrupted, the agent resumes from the notes on next start.

**Wrong project / "No project selected"**: `python3 -m sprint_manager.project list` / `show --project NAME`; set `default_project` in `~/.config/sprint-manager/config.toml`.

**CI not triggering**: GitHub-checks projects run CI on push (Re-run CI only re-runs failed Actions runs). Jenkins projects start only from **Trigger CI** (pr-open, agent not working); a `"triggered": false` reason (job not indexed yet, missing credentials) is posted to the transcript. `python3 -m sprint_manager.ci status --ticket <TASK>` shows the verdict.

**Ship refused**: Ship ▶ refuses a dirty worktree (lists the files — ask the agent to commit) or a branch with no commits beyond `origin/<base branch>`. A push/open failure leaves the task in work with its session intact; fix and press Ship ▶ again. `python3 -m sprint_manager.worktree state --ticket <TASK>` shows what Ship sees.

**Worktree issues**: Check `<worktrees dir>/<TASK>` (`project show` prints the dir). Branch is created when the work stage starts. `python3 -m sprint_manager.worktree list --project NAME` to audit.
