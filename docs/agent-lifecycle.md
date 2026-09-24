# Sprint Manager — Agent lifecycle (user-gated stages, sessions where context changes)

The agent is **fully supervised**: it **stops at every gate for your decision**, shows **summaries
only**, and **delegates every question to you**. Session boundaries sit where context changes
character — not at every approval gate. **explore** and **work** are each ONE session with internal
gates (`waiting_user` + chat); **ship** is a zero-LLM orchestrator action (**Ship ▶**), not a
session; **pr-open** runs one short **triage episode** session per CI/review signal. Every fresh
session is seeded from a per-stage view of the durable per-task **notes file** + the code on disk.
(Design rationale: `docs/design-explore-work-triage.md`.)

The lifecycle is the same for every task source (text, GitHub issue, Slack thread, Jira issue) and
every project. What differs comes from the **project profile** (`docs/projects.md`) — its notes
files are injected into the prompts, its test commands, preflight checks and CI provider are used —
and from the task's **source** (`sources/`), which supplies the content, the commit/PR reference
(`$SM_REF`), a PR-body footer, and the tracker side effects at stage boundaries.

## Stages

| Stage | Session | cwd | What the agent does | Leave with |
|---|---|---|---|---|
| to-do | — | — | idle | **Start** |
| **explore** | one session | the project's main checkout (read-only) | **G1** recap + ask **where to look** (+ any G1 question the project's explore notes add; for **not-groomed** tasks also assumptions, open questions and a numbered `### Acceptance criteria` list) → **G2** draft the plan, iterate with you; Confluence publish on request (Jira projects with a space); **no edits** | **Approve plan** |
| **work** | one session | worktree | **G1** implement (branch/worktree + the tracker's `on_work_start` on entry), refinement rounds → **G2** test plan (from the project's test commands, or discovered and confirmed with you) → **G3** run suites on your yes (preflight if the profile defines checks, `run_tests.py`, infra auto-fix **≤10 turns**) → **G4** everything committed, each acceptance criterion ✅/❌ with evidence, ready to ship. Commits (starting with `$SM_REF` when set), but never pushes / opens a PR / triggers CI | **Ship ▶** |
| *(ship)* | **no agent** | — | orchestrator action: commit check, final recap + PR text, push, open PR, tracker `on_shipped` | — |
| **pr-open** | one short session **per signal** | worktree | 15-min dual-channel poll (CI + code review incl. tracker comments); each signal you engage (or a CI failure, auto) starts a fresh **triage episode**; small fixes here; CI re-runs on push (GitHub) or via **Trigger CI** (Jenkins) | **Approve** (after you merge on GitHub) |
| done | — | — | terminal | — |

The agent reports its gate in its note (`--note "G2: test plan"`), which the panel shows. Inside a
stage, a "yes"/"go on" in chat moves the agent to the next gate.

`models.py` defines `STAGE_FLOW`, `next_stage`, `READ_ONLY_STAGES`, `uses_worktree`,
`MAX_TURNS_BY_STAGE`, the per-stage model tier (`MODEL_BY_STAGE`), and `STAGE_ALIASES` (old stage
names → new ones, see *Migration* below).

## Control surface

- **Start** (`POST /api/start/{t}`) → `Orchestrator.start` → enter explore (or resume the current
  stage; with no stored session, a fresh session — in pr-open, a fresh triage episode).
- **Approve** (`POST /api/approve/{t}`) → `Orchestrator.approve` → `_advance`: get a full recap of
  the finished stage (see below), append it to the notes, dispose the agent, enter the next stage.
  Only **explore → work** ("Approve plan → work ▶") and **pr-open → done** ("Approve (merged) →
  done ✓", which also runs the source's `on_done` — Jira → Done, a GitHub issue checked closed,
  optional Slack ✅). In **work** it is refused (HTTP 400) and hidden — work ends
  with Ship ▶. Shown when activity is `waiting_user` **or** `waiting_external`.
- **Ship ▶** (`POST /api/ship/{t}`, work only, with a confirm dialog) → `Orchestrator.ship` →
  `_ship` (see *Ship* below).
- **Trigger CI / Re-run CI** (`POST /api/trigger-ci/{t}`, pr-open only) → `Orchestrator.trigger_ci`
  → `ci.py` — starts a Jenkins build, or re-runs failed GitHub Actions runs (see *PR-open* below).
- **chat** (WebSocket) → `Orchestrator.chat` → iterate within the current session (answer the
  explore questions, revise the plan, refine code, move between work gates, guide triage). In
  pr-open with no session, a chat message **starts** a triage episode.
- **Compact** (`POST /api/compact/{t}`, or `/compact` in chat) → gets a full recap (below), saves
  it to notes, disposes the session, and re-enters the same stage with a fresh session told to
  continue from the notes rather than restart the stage. Trims exploration noise without losing
  the actual work; the natural moment in work is G1→G2 — when the agent stops at its test-plan gate
  (note starting `G2`) with its context above `SPRINT_MANAGER_CONTEXT_WARN_TOKENS` (150k), the
  orchestrator posts a one-time
  "💡 Good moment to ⟳ Compact before testing" system message.
- **Go to stage** (`POST /api/goto-stage/{t} {stage}`) → `Orchestrator.goto_stage` → gets a full
  recap of the departing session (the target stage needs to know WHY it was sent back), saves it
  to the notes, disposes the session, and resets the ticket to the chosen stage (explore / work /
  pr-open) at `idle`. The panel then shows **Start ▶**; Start begins the fresh session, running the
  worktree/tracker prep if the target needs it. Typical: pr-open → explore (re-plan) or → work
  (substantial rework); Ship ▶ afterwards pushes to the existing PR.

## Compaction and the notes — how it works

- Durable memory = `state/<T>.notes.md` (`notes.py`), append-only. (A text task's problem
  statement is `state/<T>.task.md`; GitHub-issue and Slack tasks are re-read from their tracker at
  every fresh session and cached there.) Every transition that disposes a
  session (Approve, Ship, Compact, a "go to stage" jump) appends a summary of it via
  `_recap_then_dispose` (Ship uses its own recap, below).
- **The recap is a full recap, not just the last turn's summary.** If the departing session had
  more than one turn, it's asked ONE more time to write a complete, self-contained restatement of
  the stage's current state (`_RECAP_PROMPT`) before disposal — a single-turn session's own summary
  already IS the complete picture, so that case skips the extra turn.
- **Per-stage notes view.** A fresh session is seeded with `notes.view(ticket, stage)`, not the
  whole file: (1) for every non-pr-open stage name, the **latest section wins** — each is a full
  recap, so a later `plan — summary` after a loop back already contains the earlier one; (2) pr-open
  triage episodes are events, not recaps, so they never supersede each other — but a **pr-open**
  session sees only the **latest 3**; explore/work (a jump back) see them all. An omission marker
  points at the full file, which the agent can still `Read`.
- Entering a stage, the **system prompt** = base (`references/agent-system-prompt.md`) + the stage
  file (`references/stages/<stage>.md`) + the project's notes and CI wording — fixed per (project,
  stage), so it caches across sessions and tasks. The **first message** =
  `references/ticket-context.md` (task id, kind, reference, `Source:` line — marked **not groomed**
  for text/GitHub/Slack tasks — link, description, comments) + the notes view + the kickoff. The
  prior conversation is discarded.
- Compact's fresh session gets a *different* kickoff than a genuinely new stage entry
  (`_COMPACT_KICKOFF`, not the generic `_KICKOFF`) — it's told this is a **continuation** (the notes
  above are its own complete prior work) and must not restart the stage or re-ask settled questions.
- Within a stage, chat reuses the same session (continuity across gates and refinement rounds).

## Ship — a zero-LLM action

Ship ▶ runs `Orchestrator._ship`; every step is idempotent, so on any failure the ticket stays in
work **with its session intact** and you just press Ship ▶ again:

1. `worktree.branch_state` — refuse if the worktree is **dirty** (lists the files; ask the agent to
   commit) or the branch has **no commits beyond `origin/<base branch>`**.
2. Ask the live work session (resumed from disk if needed, e.g. after a restart) for the final
   recap **plus a `### PR` block** (`title:` / `body:`, with the acceptance-criteria checklist when
   the notes have one) — `_SHIP_RECAP_PROMPT`. `_parse_pr_block` extracts it, forces the source's
   reference (`ABC-12`, `#412`) into the title when there is one, and falls back to
   `<ref>: <summary>` and the recap's first paragraph. The source's footer is appended to the body
   (`Fixes owner/repo#412` for a GitHub issue — GitHub then closes it on merge into the default
   branch; the thread link for a Slack task).
3. `pr.push` + `pr.open_pr` against the project's base branch (returns the existing PR's URL if one
   is open).
4. Save the recap as `work — summary`, dispose the work session.
5. The source's `on_shipped(url, new_pr)` — Jira → **In Review** + the PR-link comment **only if the
   PR URL changed**; optional Slack reply.
6. Land in **pr-open** with **no session**: at `waiting_external` (polling armed) if the project's CI
   runs on push (`github`), else at `waiting_user` — "PR open — press Trigger CI when ready"
   (`jenkins`).

## Behaviour rules (enforced by the base prompt)

- Stop at every gate; end with `### Summary`, `report_stage --activity waiting_user --note "<gate>: …"`, stop.
- Summaries only — the orchestrator drops `thinking`/`tool` events (`VISIBLE_EVENT_KINDS`).
- Delegate questions → `waiting_user` (never an interactive ask-tool).
- No autonomous reviews; **agents never start CI**; merge / force-push / every CI start or re-run
  are permission-blocked (`_BLOCKED_FRAGMENTS`, `_CI_TRIGGER_FRAGMENTS` in `agent.py`). The CI
  paragraphs of the prompt are rendered per provider (`_CI_TEXT`: manual / auto / none).
- Never put the task id or reference in source code or comments; commit messages start with
  `$SM_REF` when it is set.
- The read-only stage (explore) also has `Edit`/`Write` disabled in the SDK as a backstop.

## PR-open: two feedback channels, triage episodes, manual merge

**CI follows the project's provider, and agents never start it** (`ci trigger`, `jenkins
trigger`, `gh run rerun`, `gh workflow run` are denied):

- **github** (default) — checks start on every push, so Ship lands at `waiting_external` and a
  pushed fix re-runs CI by itself. **Re-run CI** re-runs failed Actions runs.
- **jenkins** — you press **Trigger CI**. On `"triggered": true` the task goes to
  `waiting_external` with both fired flags reset (**re-armed**, so the fresh build is detected) and
  any live triage episode ends. On `"triggered": false` (the PR's job isn't indexed yet, or
  credentials aren't configured) nothing changes and the reason is posted.
- **none** — no CI button; only review is polled.

The orchestrator polls **every 15 min** (`config.PR_POLL_SECONDS`) on two independent channels —
the CI verdict (`ci.verdict`) and the code review (comment count + decision; the count includes
the task's tracker comments — a linked GitHub issue's comments, in the same GraphQL call, or a
Slack thread's replies) — and **keeps polling until both have fired at least once**, then stops. Each channel fires on any change vs its watermark. A fire
flips the ticket to `waiting_user`, records the notice in `last_signal`, posts a notification, and
lights the tab's CI/review indicators — **without spending an agent turn**. The one exception: a
**CI failure** auto-triages (one Sonnet turn that fetches the failing logs and analyzes them, with
whatever CI-debugging tools the project notes name).

**Triage episodes.** pr-open has no standing session. An episode starts when a CI failure
auto-triages or when you chat after a signal: a fresh session whose first message
(`_episode_kickoff`) carries the latest signal (`last_signal`), the PR URL, the branch's commit log
and diffstat vs `origin/<base branch>` (`worktree.branch_state`), and your message — enough to
orient without replaying the implementation history. It triages (CI logs via `ci.py logs`; review
comments classified Question / Valid / Invalid / Ambiguous / Scope-creep), makes small fixes on your
go-ahead (commit, `pr.py push`), and tells you CI re-runs on its own (GitHub) or asks you to press
Trigger CI (Jenkins). An episode **ends** when the task next rests at `waiting_external` — the agent
reporting it, you (re-)triggering CI, or re-arming the review channel — and its last summary is saved as `pr-open — triage episode (<timestamp>)` **without an
extra recap turn** (episodes are short; the last summary is the whole story). The next signal
starts a fresh one.

The **review channel can also be re-armed by hand**: the review indicator (the tab dot and the
panel badge) is clickable. When a review event is noise — e.g. CodeRabbit posting that it ran out of
tokens and couldn't review — click it to dismiss it. That resets `review_fired` (the watermarks stay,
so the same comment can't immediately re-fire), greys the indicator back to "watching", resumes
review polling until the next comment/approval/rejection, drops the ticket back to
`waiting_external`, and ends any live episode.

**Merging is manual**: you merge the PR on GitHub yourself — the agents cannot (`gh pr merge` is
permission-blocked and `pr.py` has no merge command). After merging, press **Approve** → done.

## Models & turn caps

Per-stage tier (defaults in `MODEL_BY_STAGE`; one model per session, since switching mid-session
would invalidate the prompt cache):

| Stage | Model | Effort | `MAX_TURNS_BY_STAGE` |
|---|---|---|---|
| explore | Opus 4.8 | high | 45 |
| work | Opus 4.8 | high | 130 |
| pr-open | Sonnet 4.6 | high | 50 |

These defaults can be overridden at runtime via the ⚙ settings drawer (persisted to
`state/config.json`; takes effect on the next stage start). The work stage's infra-fix is
additionally capped at 10 turns by the prompt. The per-session CACHE ALERT fires above 1M
cache-read tokens, except work (3M — one session spans implementation and testing).

## Migration from the six-stage model

Persisted state and config using old stage names resolve via `STAGE_ALIASES`: orient/plan →
explore; implementation/testing/ship → work; ci-build → pr-open. An aliased **working** stage has
its `session_id` dropped and is parked at `idle` (Start ▶ begins a fresh session from notes — an old
session would carry the old stage's instructions); an aliased pr-open keeps its activity (the poll
gates on it) but also drops its session. A ticket persisted at `ship` lands in work; Ship ▶ is
idempotent. Model overrides saved under old stage names are **ignored** (several old names map to
one new stage) — defaults apply until you re-save in ⚙ Settings.
