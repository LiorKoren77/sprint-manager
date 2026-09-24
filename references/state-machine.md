# Sprint Manager — task lifecycle (authoritative)

A task (free text, a GitHub issue, a Slack thread, or a Jira issue — see `docs/projects.md`)
lives in one project. Each task-agent advances a STAGE and reports an ACTIVITY (and its current gate, in the note)
after every transition. explore and work are each ONE session with internal gates; ship is an
orchestrator action, not a stage; pr-open runs one short triage session per signal.

```
to-do
  →  explore         ONE read-only session
  │     G1 recap + ask where to look (+ any project G1 questions;
  │        not-groomed tasks: + assumptions and ### Acceptance criteria)
  │     G2 draft + iterate the plan (Confluence publish → waiting_external for sign-off)
  │  ── Approve (plan) ──
  →  work            ONE worktree session (worktree + tracker "work started" on entry)
  │     G1 implement; refinement rounds
  │     G2 test plan
  │     G3 run suites on your yes (preflight, run_tests; infra auto-fix ≤10 turns)
  │     G4 all committed, acceptance criteria checked → ready to ship
  │  ── Ship ▶ (zero-LLM: commit check, recap + PR text, push, open PR, tracker update) ──
  →  pr-open         no standing session; CI runs on push (github) or on Trigger CI (jenkins)
  │        ┬──► CI verdict (project's provider, polled every 15 min)        ─┐  either fires →
  │        └──► code reviews (PR + tracker comments/decision, 15 min)       ─┘  waiting_user + notify
  │       each signal you engage (CI failure: automatically) = a fresh triage episode:
  │       ├─ small fix → commit/push here → CI re-runs (github) / Trigger CI (jenkins)
  │       │                → waiting_external (episode ends)
  │       ├─ hard fix  → manager jumps back to explore / work ("go to stage")
  │       └─ CI green + review approved → manager merges ON GITHUB (manual) → Approve
  →  done            (tracker on_done: Jira→Done / GitHub issue closed? / Slack ✅)
```

**Gates.** Inside explore and work, the agent stops at every gate (`waiting_user`, note `G<n>: …`).
A chat reply ("yes", "go on to testing") moves it to the next gate in the same session; Approve /
Ship ▶ move the task to the next stage.

**pr-open** is "PR in flight": two independent feedback channels — the CI verdict and code
reviews — arrive **in any order**. The review channel counts PR comments/reviews **plus the task's
tracker comments** (a linked GitHub issue's comments, a Slack thread's replies). Ship lands the task
here with no session: at `waiting_external` (polling armed) when the project's CI runs on push
(`github`), or at `waiting_user` when builds start only from **Trigger CI** (`jenkins`), which then
moves it to `waiting_external`. A project with CI `none` polls review only. The orchestrator polls both channels **every 15 min** and keeps polling until
BOTH have fired at least once. Any fire flips the ticket to `waiting_user`, records the notice in
`last_signal`, and posts a notification (and lights the tab's CI/review indicators) — no agent turn,
except a CI **failure** auto-triages. The poll re-arms when CI is (re-)triggered and whenever the
task next enters `waiting_external`. A **triage episode** is a fresh session seeded with the latest signal,
the branch commit log + diffstat, and your message; it ends (summary → notes, no extra recap turn)
when the ticket next rests at `waiting_external`.

**Agents never start CI.** GitHub checks start on push by themselves (the dashboard's **Re-run CI**
re-runs failed Actions runs); Jenkins builds start only from **Trigger CI**. Every CI start/re-run
command (`ci trigger`, `jenkins trigger`, `gh run rerun`, `gh workflow run`) is permission-blocked
for agents. If Jenkins hasn't indexed the PR's job yet, retry shortly or start it from Jenkins.

**Merging is manual.** The manager merges the PR on GitHub, outside this system. The agents cannot
merge (`gh pr merge` is permission-blocked; `pr.py` has no merge command). After merging, press
**Approve** on the pr-open stage to mark the task done.

**Backward jumps** use the panel's **go to stage** control: the current session is disposed (its
last summary is saved to the notes so the target stage sees why it was sent back), the ticket is
reset to the chosen stage at `idle`, and **Start ▶** begins the fresh session there. Typical
triggers: a CI failure or review comment that invalidates the plan (→ explore) or needs substantial
code change (→ work; Ship ▶ afterwards pushes to the existing PR).

## STAGE (pipeline position)

`to-do, explore, work, pr-open, done`

These map to the `Stage` enum in `models.py` (ordered in `STAGE_FLOW`; `next_stage(s)` gives the
`Approve` target — explore → work, pr-open → done; work leaves only via Ship ▶). Old values resolve
via `STAGE_ALIASES` / `Stage._missing_`: `orient`/`plan` → explore, `implementation`/`testing`/`ship`
→ work, `ci-build` → pr-open. A ticket persisted under an old name has its session dropped (an old
session carries the old stage's instructions); a working stage is also parked at `idle` so Start ▶
begins a fresh session from the notes.

## ACTIVITY (liveness — orthogonal to stage)

| activity | meaning | who resolves it |
|---|---|---|
| `working` | the agent is actively doing work | the agent |
| `queued` | a message is queued; another task holds the turn slot | the semaphore |
| `waiting_user` | a judgment call / gate needs you (round-robin jumps here) | you, via chat, Approve, Ship ▶, or the CI button |
| `waiting_external` | PR in flight (CI/review), a background job, or a plan awaiting tech-leader sign-off | the orchestrator polls and notifies you |
| `idle` | not started, terminal (`done`), or just reset by "go to stage" / migration | you, via Start |

## Approve / Ship / Trigger CI

- **Approve** (`POST /api/approve/{ticket}`) — explore and pr-open only, shown whenever activity is
  `waiting_user` **or** `waiting_external` (the tech leader signed off the plan, or the PR is
  merged). Refused (400) in work.
- **Ship ▶** (`POST /api/ship/{ticket}`) — work only; refused if the worktree is dirty or the branch
  has no commits beyond `origin/<base branch>`.
- **Trigger CI / Re-run CI** (`POST /api/trigger-ci/{ticket}`) — pr-open only, not while the agent
  is working; labelled by the project's CI provider (hidden for `none`).

A ticket at `idle` in a working stage shows **Start ▶** instead (it has no session yet — fresh
entry, not an advance).
