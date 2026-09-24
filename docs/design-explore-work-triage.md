# Design: explore / work / triage (collapsing six stage sessions into three)

Status: **implemented (2026-09-24)** — see §10 for deviations from this proposal

## 1. Problem

Today every stage (orient, plan, implementation, testing, ship, pr-open) is its own fresh Claude
session, joined only by `notes.md`. That split was introduced to stop single sessions from
ballooning, but it has costs of its own:

- **Lossy handoffs where context matters most.** Plan re-derives what orient just learned; testing
  re-derives why implementation did what it did. Each handoff costs a recap turn
  (`_RECAP_PROMPT`) plus a cold-start re-read of the code, and still loses nuance (ABC-78318 lost a
  7-point plan at one boundary before the recap existed).
- **An agent for mechanical work.** Ship is a Haiku session that runs `pr.py push`, `pr.py open`,
  and asks a yes/no question. None of that needs judgment.
- **Notes bloat.** Six sessions each append a summary + recap; notes reach 60–94 KB (≈15–23k
  tokens) and are injected into every new session in full.

The opposite extreme (one session for the whole ticket) is also wrong: per-turn cost grows with
context length, and pr-open wakes hours or days later, long after the prompt cache (5 min / 1 h)
has expired — every wake would re-write the entire accumulated context to cache.

## 2. Proposal

Draw session boundaries where **context changes character**, not at every approval gate:

| New stage | Replaces | Session lifetime | cwd | Tools | Default model |
|---|---|---|---|---|---|
| **explore** | orient + plan | one session, until plan approved | `$ACME_REPO` | read-only | Opus / high |
| **work** | implementation + testing | one session, until Ship | worktree | full | Opus / high |
| *ship (action)* | ship | **no agent** — orchestrator action | — | — | — |
| **pr-open** (triage) | pr-open | **one short session per signal episode** | worktree | full | Sonnet / high |

Key principle: **stage ≠ gate.** Approval gates stay exactly as strict (nothing runs unapproved);
they just no longer force a session boundary. Inside explore and work, gates are `waiting_user` +
chat, as refinement rounds already are today.

```
to-do → explore ──Approve (plan)──► work ──Ship ▶──► [ship action] ──► pr-open ──Approve (merged)──► done
          ▲  recap→ask→plan          │  implement→review→test plan→run→results        │ episode per CI/review signal
          └──────── go to stage ◄────┴──────────────── go to stage ◄──────────────────┘
```

## 3. Stage details

### 3.1 explore (orient + plan)

One read-only session in `$ACME_REPO`. The prompt (`references/stages/explore.md`) merges today's
`orient.md` and `plan.md` with **two internal gates**:

1. **G1 — recap + two questions** (where to look? use `acme-analyzer`?). Stop at `waiting_user`.
2. **G2 — plan.** After the manager answers in chat, read only where pointed and present the plan.
   Iterate in the same session. Confluence publish / inline-comment reading / `waiting_external`
   for tech-lead sign-off are unchanged from `plan.md`.

**Approve** = plan approved → recap turn → notes → enter work. Re-entry after a backward jump keeps
today's "inspect the branch read-only via the worktree" rule.

Why Opus for the whole session: G1 is cheap (a recap + two questions, ~2 turns), so running it on
Opus costs little, and it means the model that builds the understanding is the one that plans.

### 3.2 work (implementation + testing)

One session in the worktree. The worktree/branch/Jira "In Progress" prep in `_ensure_worktree` is
unchanged. Prompt (`references/stages/work.md`) = today's `implementation.md` + `testing.md`, with
explicit gates the agent must stop at:

1. **G1 — code complete.** Summarise changes (files + rationale), ask for review. Refinement rounds
   loop here.
2. **G2 — test plan.** On the manager's go-ahead in chat, propose suites (from `test-commands.md`).
3. **G3 — run?** Ask before running anything. Preflight / `run_tests.py` / `background.py` /
   infra-fix ≤10 turns rules carry over verbatim.
4. **G4 — ready to ship.** Local results summarised; everything committed; tell the manager to
   press **Ship ▶**.

The agent **commits** (atomic, ticket-keyed) but never pushes, opens a PR, or triggers CI in work —
those move to the ship action. Test failures that need code changes are fixed in place (the big
win of the merge: the session remembers why the code looks the way it does).

**Context control inside work.** This is the session most at risk of ballooning (today's
implementation sessions alone reach 5–11M cumulative cache-read tokens). Mitigations:

- The existing **⟳ Compact** button stays, and the natural moment for it is G1→G2 (exploration
  noise from implementation is dead weight for testing). The UI surfaces a one-click
  "Compact before testing?" hint when the agent reports reaching G2 *and* the session's context is
  large (see §6 on measuring context size).
- `run_tests.py`/`background.py` already keep suite output out of context — that stays mandatory.

### 3.3 ship — a zero-LLM orchestrator action

Pressing **Ship ▶** in work (replaces Approve there, with a confirm dialog) runs
`Orchestrator._ship(ticket)`:

1. `git status --porcelain` in the worktree — if dirty, refuse and tell the manager ("uncommitted
   changes — ask the agent to commit"). Ticket stays in work.
2. Recap turn on the live work session (as today), **extended** to also emit a fenced block:

   ```
   ### PR
   title: <TICKET>: <short title>
   body:
   <one-paragraph summary>
   ```

   Parsed by the orchestrator; fallback title is `<TICKET>: <Jira summary>`, fallback body is the
   recap's first paragraph.
3. Save recap to notes, dispose the work session.
4. `pr.push(ticket)` then `pr.open_pr(ticket, title, body)` (already idempotent: an existing PR's
   URL is returned). Record `pr_url`.
5. Jira → *In Review* + PR-link comment (moved here from `_advance`'s ship branch).
6. Enter pr-open with **no session**, activity `waiting_user`, and a system message:
   "PR opened: … — press **Trigger CI** when ready."

Any failure in 4–5 leaves the ticket in work at `waiting_user` with the error; retry = press Ship ▶
again (every step is idempotent).

**CI trigger becomes a UI button**, not an agent question. `POST /api/trigger-ci/{ticket}` →
`jenkins.trigger_for_ticket` → on `triggered: true` set `waiting_external` (which re-arms the
poll, same as today); on `false` post the `reason` and the "use the PR's Trigger Jenkins check"
fallback. Shown in pr-open whenever the ticket is not `working`. This deletes the most fragile
prompt text in the system (the "only on an explicit yes / never claim you'll retry" rules in
`ship.md`, `pr-open.md` and the base prompt) because the agent can no longer trigger CI at all —
`jenkins.py trigger` is added to `_BLOCKED_FRAGMENTS`.

### 3.4 pr-open — short triage episodes

The dual-channel poll (`_poll_loop`, `_check_external`, watermarks, fired flags, re-arm, review
dismissal) is **unchanged**. What changes is the session model:

- **No standing session.** Between signals, pr-open has `session_id = ""`.
- **An episode starts** when (a) a CI failure auto-triages (today's behaviour), or (b) the manager
  chats after any signal. `_chat` in pr-open with no session → `_enter_stage` fresh, with a
  kickoff built from `TicketStatus.last_signal` (new field: the notice text `_check_external`
  already builds, e.g. "CI build finished: failed", "New code review comment(s)").
- The episode's system prompt is seeded with a **trimmed notes view** (§4) plus
  `git log --oneline develop..HEAD` and `git diff --stat develop...HEAD` — enough to orient without
  replaying implementation history.
- **An episode ends** when the ticket next rests at `waiting_external` (fix pushed + CI
  re-triggered via the button) or on Approve/goto. End = `_save_summary_and_dispose` (no extra
  recap turn — episodes are short and their last summary is the whole story) + `session_id = ""`.
  Hooked into `_run_turn`'s `finally`, next to the existing re-arm logic.

Hard fixes still jump back via **go to stage** (now to `explore` or `work`).

## 4. Notes: sectioned, filtered per stage

Notes stay one append-only file (durable memory, debuggable), but sections get stable title
prefixes and `build_system_prompt` injects a **per-stage view** instead of the whole file:

| Section prefix | Written by | Injected into |
|---|---|---|
| `explore — summary` (the approved plan) | explore recap | work, pr-open, explore re-entry |
| `work — summary` | work recap at Ship | pr-open, explore/work re-entry |
| `* — mid-stage compact` | Compact | same stage (latest only) |
| `pr-open — episode N` | episode end | pr-open (**latest 3 only**), explore/work re-entry (all) |
| `* — summary (before jump to …)` | goto | the target stage |

`notes.view(ticket, stage) -> str` in `notes.py` implements this (pure function, testable). The
full file is still on disk and readable by the agent via `Read` if it genuinely needs history.

## 5. Code changes

| File | Change |
|---|---|
| `models.py` | `Stage` → `TODO, EXPLORE, WORK, PR_OPEN, DONE`. `STAGE_ALIASES` gains `orient/plan→explore`, `implementation/testing/ship→work` (plus existing `ci-build→pr-open`). `READ_ONLY_STAGES={EXPLORE}`. `MAX_TURNS_BY_STAGE`: explore 45, work 130, pr-open 50 (sums of the old caps). `MODEL_BY_STAGE`: explore Opus/high, work Opus/high, pr-open Sonnet/high. `TicketStatus.last_signal: str = ""`. `from_dict`: if the raw stored stage was an alias, clear `session_id` (see §7). |
| `agent.py` | `_STAGE_FILES` → explore.md / work.md / pr-open.md. Add `jenkins trigger` to `_BLOCKED_FRAGMENTS`. `build_system_prompt` takes the notes **view** (§4). |
| `orchestrator.py` | New `ship()` / `_ship()` (§3.3) and `trigger_ci()`. `_advance`: work is no longer advanced by Approve (server returns 400 → UI shows Ship ▶); remove the ship branch (Jira In Review moves to `_ship`). pr-open episode start in `_chat` (no session → fresh attach with signal kickoff) and episode end in `_run_turn`'s `finally`. `_check_external` stores `last_signal`. `_check_cache_anomaly` threshold per stage (work gets a higher alert line so it doesn't cry wolf). Update the CI-failure auto-triage prompt (no CI re-trigger instruction). |
| `notes.py` | `view(ticket, stage)` (§4). |
| `server.py` | `POST /api/ship/{t}`, `POST /api/trigger-ci/{t}`. `/api/approve` rejects work with a pointer to Ship. |
| `report_stage.py` | No code change (choices come from `Stage` + aliases). |
| `web/app.js`, `index.html` | Primary button label per stage: explore "Approve plan", work "Ship ▶" (confirm dialog), pr-open "Approve (merged)". **Trigger CI** button in pr-open. "Compact before testing?" hint. goto-stage options already come from `/api/config`. |
| `references/stages/` | Add `explore.md`, `work.md`; rewrite `pr-open.md` (episode framing, no CI trigger, "push then tell the manager to press Trigger CI"). Delete `orient.md`, `plan.md`, `implementation.md`, `testing.md`, `ship.md`. |
| `references/agent-system-prompt.md` | "One stage only" → "**stop at every gate** your stage lists". Stage-purity rules rewritten for 3 stages. Remove CI-trigger rules (agent can't). `_RECAP_PROMPT` variant with the `### PR` block for work. |
| Docs | `CLAUDE.md`, `docs/architecture.md`, `docs/agent-lifecycle.md`, `docs/backend.md`, `docs/frontend.md`, `references/state-machine.md`, `references/reuse-map.md`. |

## 6. Risks and open questions

1. **Gate discipline inside one session.** The biggest behavioural risk: an agent in work sliding
   from G1 straight into running tests. Mitigation: gates are numbered in the prompt, the agent
   reports `--note "G2: test plan"` at each stop (visible in the UI), and running suites still
   requires `run_tests.py` which the prompt ties to an explicit yes. Verify on 2–3 real tickets
   before deleting the old prompts.
2. **"Approve" meaning.** Today Approve always means "next stage"; inside work, chat "yes" now
   means "next gate" and Ship ▶ means "leave". Distinct button labels are what keep this
   unambiguous — don't reuse "Approve" for work.
3. **`max_turns` semantics.** The SDK passes `--max-turns` to the CLI once per client; confirm
   whether it caps each `query()` or the whole streaming session. If per-session, 130 for work may
   be too low for long refinement loops — measure before choosing.
4. **Measuring context size.** `cache_read_tokens` is cumulative across turns, not the current
   context length, so it can't drive the "compact before testing?" hint. Need the last API call's
   `input + cache_read + cache_write` — check whether per-message usage is exposed in the SDK
   (0.2.94); otherwise approximate from the `ResultMessage` delta ÷ `num_turns`.
5. **Model tiering lost for testing.** Testing moves from Sonnet/medium to Opus/high. `set_model()`
   exists on `ClaudeSDKClient`, but switching mid-session invalidates the cache (per model), which
   likely costs more than it saves. Accept for now; revisit with data.
6. **Cost claims are unverified.** Nothing here has been measured. Success criteria below.

## 7. Migration

- Persisted `state/*.json` with old stage names resolve via `STAGE_ALIASES`. When the alias path is
  taken, `from_dict` clears `session_id` and the orchestrator shows **Start ▶** (idle): an old
  plan/testing session resumed under the new prompt would carry the old stage's instructions in its
  history. Start begins a fresh explore/work session from notes — exactly today's goto behaviour.
- A ticket persisted at `ship` maps to work; Ship ▶ is idempotent (push + existing PR URL), so it
  completes cleanly.
- `state/config.json` model overrides keyed by old stages: two old keys map to one new stage, so
  they're **ignored** (with a log line) rather than silently picking one; defaults apply until the
  manager re-saves in ⚙ Settings.
- In-flight agents at deploy time: restart the server (live agents are in-memory only).

## 8. Rollout (each phase independently shippable)

1. **Ship action + Trigger CI button**, with the current six stages (testing's Approve → `_ship` →
   pr-open; ship stage retired). Smallest change, removes a whole agent and the CI-trigger prompt
   rules.
2. **pr-open episodes + `last_signal`.** Independent of the stage merge.
3. **explore** (merge orient + plan).
4. **work** (merge implementation + testing) + compact hint.
5. **Notes views** (§4).

## 9. Success criteria

Compare 3+ tickets on the new flow against the existing state files (same stats the dashboard
already records):

- Cost per ticket (`cost_usd`) and turns per ticket (`total_turns`) — expect lower, not higher.
- Peak per-session cache-read in work vs. today's implementation (5–11M) — must not blow past it
  materially; if it does, Compact-at-G2 becomes automatic rather than a hint.
- Notes size at pr-open — target < 25 KB injected (the view, not the file).
- Qualitative: fewer "re-reading code I already read" turns at the start of plan/testing
  (observable in transcripts).

## 10. Implementation notes / deviations

What shipped in `~/sprint-manager` differs from the proposal above in these places (the code is
authoritative; `docs/agent-lifecycle.md` and `docs/backend.md` describe it):

- **Notes view rule** (§4): simplified to "**latest section per stage name wins**" for every
  non-pr-open stage name (each such section is a full recap, so a later one supersedes earlier ones
  — this also collapses the repeated `plan — summary` / `implementation — summary` sections that
  loop-backs left in legacy notes); **pr-open episodes are excluded** from that rule (events, not
  recaps), and the **pr-open view keeps the last 3** of them; explore/work see all. Implemented as
  the pure `notes.filter_view`. On the copied legacy notes this trims the injected size by ~30–45%.
- **Episode context** (§3.4): the branch context (commit log + diffstat vs `origin/develop`, via
  `worktree.branch_state`) and the latest signal are carried in the episode's **first user
  message** (`_episode_kickoff`), not in the system prompt.
- **Context size** (§6.4, resolved): the SDK exposes per-call `usage` on every `AssistantMessage`,
  so `TicketAgent` tracks `context_tokens` (the latest call's input + cache read + cache write) and
  `peak_context_tokens`, persisted in `TicketStatus` and reset whenever a session ends. The panel's
  indicator shows it (`context: 180k`, amber past `SPRINT_MANAGER_CONTEXT_WARN_TOKENS`, default
  150k). Baseline from v1's agent sessions: median call 115k, 37% of calls > 150k, peak 619k.
- **Compact hint** (§3.2): a one-time **system message from the orchestrator**
  (`_maybe_hint_compact`) when a work turn ends with the agent's note starting with `G2` and
  `context_tokens` above the warn threshold.
- **Context discipline** (added after the design): each agent Bash result is capped at
  `SPRINT_MANAGER_BASH_MAX_OUTPUT` chars (default 15000, passed to the CLI as
  `BASH_MAX_OUTPUT_LENGTH`), and the base prompt requires locate-then-read-a-range (`grep -n` +
  `Read` offset/limit) and redirecting long command output to a file.
- **Cacheable system prompt**: the system prompt is now ticket-agnostic (base rules + stage
  instructions; per-ticket values via `$SM_TICKET`/`$SM_WORKTREE` env vars), and the ticket + notes
  view travel in the first message (`references/ticket-context.md`). Caching matches only at
  content-block boundaries, so reordering inside one prompt string would have saved nothing. v1 data:
  a fresh session's first call read ~13k cached tokens (the tool schemas) and wrote 25–43k. The
  shareable system prompt is ~3k tokens per stage, so the saving is modest (~3k tokens moved from
  cache-write to cache-read per fresh session started within the hour). v1-format sessions
  (`session_format` < 2) are never resumed.
- **Prompt cache TTL**: nothing to change — on a subscription the CLI already uses the 1-hour TTL
  automatically (v1's agent sessions: 79.2M tokens written at 1h vs 0.75M at 5m). Reads, not writes,
  dominate (≈15:1), which is why context size is the lever.
- **Port**: defaults to **8766** (`SPRINT_MANAGER_PORT`).
- **Tests**: `scripts/tests/` — `test_stages.py` (aliases/migration, notes view, PR-block parsing)
  and `test_orchestrator_flows.py` (Ship, Trigger CI, triage episodes with the agent, git/gh,
  Jenkins and Jira faked; needs the venv). Run with
  `cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests`.
- **Rollout** (§8): all phases landed at once in the v2 copy rather than incrementally.
- **Open questions still open**: §6.3 (`--max-turns` scope per query vs per session) and §6.4
  (true context size) were not resolved; §9's measurements have not been taken yet.

