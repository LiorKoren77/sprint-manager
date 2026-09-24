# Sprint Manager — Backend design

Python package `sprint_manager` under `scripts/`. Two layers: a **zero-LLM CLI layer** (stdlib
only) and an **orchestration layer** (needs the venv: claude-agent-sdk, fastapi, uvicorn).

## Module responsibilities

| Module | Layer | Responsibility |
|---|---|---|
| `config.py` | foundation | Paths (`APP_ROOT` from `__file__`, `STATE_DIR`) and **per-project** credential lookup (`jira_credentials(project)`, `confluence_credentials(project)`, `jenkins_credentials(project)`, `browse_url(project, key)`, `board_id(project)`) — the site comes from the profile, the credential env-var NAMES from its `*_env` keys. Raises `ConfigError` naming exactly which variables to export. Loads a local gitignored `.env` into `os.environ` at import (`_load_dotenv_file`, shell exports always win); `credential_fields()` (app-wide `SLACK_BOT_TOKEN` + every registered project's declared env names), `credentials_status`/`set_credential` back the Settings → Credentials UI (writes to `.env`, masks secrets, applies immediately — no restart); `external_auth_status` reports `gh`/Claude login state read-only. |
| `project.py` | foundation | Project profiles: `load(name)` (personal `~/.config/sprint-manager/projects/<name>/project.toml` merged with the repo-local `<repo>/.sprint-manager/project.toml`, table by table; prompt paths resolved per profile dir; defaults for everything but `repo`), `list_projects`, `default_project_name` (`SPRINT_MANAGER_DEFAULT_PROJECT` → `config.toml` `default_project` → the only project), `resolve(name, ticket)` (explicit → `$SM_PROJECT` → the ticket's stored project → default — for CLI modules), `for_ticket` (ignores `$SM_PROJECT` — for the server), `register(repo, name)`. `Project.base_branch` detects `origin/HEAD` → `gh` → `main` unless set. CLI: `list` / `show` / `add`. See `projects.md`. |
| `sources/` | CLI | Task sources behind one interface (`Source`: `load` → meta, `reference`, `pr_body_footer`, `on_work_start` / `on_shipped` / `on_done`, `feedback_count`): `jira.py`, `github.py` (`gh issue view/edit`; `Fixes owner/repo#N`; `linked_issue` folds issue comments into the PR's GraphQL poll), `slack.py` (thread → Markdown; reply count as feedback; optional reactions/reply), `text.py` (the taskfile). `get(tracker)` (legacy `""` = jira), `new_task_id(project, tracker, n)` → `<project>-<t|gh|slack>-<n>`. Built-ins register once via a flag in `_load_builtin` (so importing one source module first can't hide the others). |
| `taskfile.py` | CLI | `state/<T>.task.md`: a text task's problem statement (editable), or the cached rendering of a GitHub issue / Slack thread (offline fallback). |
| `models.py` | foundation | `Stage` (`to-do`/`explore`/`work`/`pr-open`/`done`) + `Activity` enums, with `STAGE_ALIASES` / `Stage._missing_` migration aliases (orient/plan → explore; implementation/testing/ship → work; ci-build → pr-open — `TicketStatus.from_dict` drops the session of an aliased stage and parks a working one at `idle`), the per-stage model map (`MODEL_BY_STAGE`) and turn caps (`MAX_TURNS_BY_STAGE`: 45/130/50), runtime model overrides (`_model_overrides`, `set_model_overrides` — ignores old stage names, `get_model_config`), and the `TicketStatus` dataclass (`to_dict`/`from_dict`; includes `project`, `tracker` (jira/github/slack/text — legacy rows migrate to jira), `kind` (bug/feature; `branch_kind` derives it from `issue_type` when empty), `external_ref`, `external_url`, `last_signal` (the latest poll notice that seeds the next pr-open triage episode), and `jira_status` — the tracker status snapshot, exposed to the UI as `tracker_status`). |
| `notes.py` | foundation | The per-ticket notes file (`state/<T>.notes.md`, append-only) and `view(ticket, stage)` / pure `filter_view`: what a fresh session is seeded with — the latest section per (non-pr-open) stage name wins (each is a full recap), and a pr-open session sees only the latest 3 pr-open sections (episodes); an omission marker points at the full file. `latest_heading_block(ticket, heading)` pulls the last `### Acceptance criteria` block (used by File as issue). |
| `state.py` | CLI | The status store: one `state/<task>.json` per task. Atomic writes (`tmp` + `os.replace`). `read` / `write` / `update` / `all_statuses`. |
| `report_stage.py` | CLI | The **agent's** status channel — a CLI the agent runs after each transition to record `stage`/`activity`/`note`. |
| `jira_client.py` | CLI | Thin Jira REST client (stdlib `urllib`, basic auth), constructed per project (`JiraClient(project)`). `get_issue`, `search` (JQL, paginated), `get_sprints`, `transition_issue`, `add_comment`; `issue_fields(project)` adds the profile's per-type `description_fields`. |
| `confluence.py` | CLI | Publish a Markdown plan to Confluence over REST (`publish`, idempotent by title; `--space` defaults to the project's `confluence_space`; markdown→storage converter; optional `--ticket` posts the link to Jira), and read reviewer feedback back (`comments`/`read_inline_comments` — every inline comment on a page: status, author, anchored text, body; unresolved-only by default, paginated, resolves accountIds to display names). Replaces the plan stage's Atlassian **MCP** — no MCP anywhere now. |
| `fetch_sprint.py` | CLI | `fetch_sprint(project, sprint, assignee)` → simplified issues; ADF→text flattening; `simplify_issue(issue, project)` picks the description field per issue type from the profile. |
| `sprints.py` | CLI | `list_sprints(project)` → the project's board's sprints labelled `current` (active) / `next` (earliest future). |
| `branch.py` | CLI | `slugify`, `branch_name` (`bugfix/`/`feature/` prefix from the task's kind, **≤80-char** trim). |
| `worktree.py` | CLI | Per project (repo, worktrees dir and base branch from the profile): `add` / `remove` / `list --project` / `sync` (merge `origin/<base>`, report conflicts) / `state` (`branch_state`: dirty files, commits ahead of `origin/<base>`, commit log, diffstat — Ship's pre-check and the triage-episode kickoff) via `git -C … worktree …`. Branch creation lives here (never touches the main checkout). |
| `pr.py` | CLI | `gh`-based: `push`, `open_pr` (idempotent — existing **open** PR only; closed/merged don't count), `pr_number`, `comments`, `review_signal(ticket, number, linked_issue)` (one GraphQL call → `{count, decision}` for the pr-open poll; a linked GitHub issue's comment count rides in the same call; `feedback_total` wraps it), `ready` (one JSON merge-readiness verdict: CI + review + conflicts). **No merge** — manual on GitHub. Runs in the task's worktree; `open_pr` targets the project's base branch. |
| `ci.py` | CLI | CI dispatcher on the project's `[ci] provider`: `verdict` / `logs` / `trigger` / `auto_triggers`, CLI `status|logs|trigger --ticket` (the prompts' `<<CI_CMD>>`; `trigger` is manager-only — agents are blocked). **github** (default): one `gh pr view --json headRefOid,statusCheckRollup` → verdict (running/passed/failed/no-build; CheckRuns + legacy StatusContexts; skipped ignored), run watermark `<sha12>:<run ids>`; `logs` via `gh run view --log-failed`; `trigger` = `gh run rerun --failed`. **jenkins** → `jenkins.py`. **none** → always `no-build`. |
| `jenkins.py` | CLI | Jenkins provider over the classic REST API (not Blue Ocean), all parameterised by the project's `[ci]` (`url`, `job` path template with `{pr}`, credential env names): `verdict`, `failure_logs` (failing stages + trimmed log tails), `trigger` (POST; a 404 means the PR job isn't indexed yet). Failure *diagnosis* stays with the agent (and whatever tools the project notes name). |
| `slack.py` | CLI | `fetch_thread(url)`, plus `add_reaction` / `post_reply` (optional Slack-task write-backs, need `reactions:write` / `chat:write`) — parses a Slack message/thread link (`.../archives/<CHANNEL>/p<ts>`, resolving a reply deep-link's `thread_ts` query param back to the thread root), fetches it via the plain Web API (`conversations.replies`) with a bot token, and resolves user IDs to display names. Available to every stage (not just explore) via `<<SLACK_CMD>>` in the base prompt — no Slack MCP, same reasoning as `jira_client.py`. |
| `run_tests.py` | CLI | Runs a documented suite command and distils output to compact JSON (counts + failure tails + `log_path`) so the work agent doesn't ingest thousands of log lines while testing. |
| `preflight.py` | CLI | Deterministic infra check driven by the profile's `[preflight]` (`ports` as `{ port, hint, host? }`, `paths` relative to the checkout or absolute/`~`, `env` vars) → JSON (`--need` limits port checks; `--project` / `$SM_PROJECT`), so the work stage's infra-fix work starts from ground truth, not turn-by-turn probing. |
| `background.py` | CLI | Launch a slow shell command (a compile, a slow suite) fully detached (`start`) and poll it (`status`) — this app is turn-based, so an agent cannot "continue when it finishes" on its own. The agent starts the job, reports `waiting_external`, and stops; the orchestrator's poll loop (`config.BG_JOB_POLL_SECONDS`, ~20s — pure local file/PID check, no external API) flips it to `waiting_user` and notifies with the exit code + log tail once done. |
| `agent.py` | orchestration | `TicketAgent`: wraps `ClaudeSDKClient`. `build_system_prompt(stage, project)` renders a **task-agnostic** system prompt — base rules + stage instructions + the project's notes (`## Project notes`), test-commands pointer (or a "discover and confirm at G2" rule), base branch, `<<IF:confluence>>`/`<<IF:preflight>>` sections and provider-specific CI wording (`_CI_TEXT`: manual/auto/none) — byte-identical across sessions of the same (project, stage), so it hits the prompt cache; `build_context_message(meta, notes, status)` renders `references/ticket-context.md` (task id, kind, reference, `Source:` line — non-Jira sources are marked **not groomed**, which switches on explore's acceptance-criteria step — link, description, comments, the stage's notes view), which `_enter_stage` prepends to the session's first message (the transcript shows only the kickoff). Per-task values reach commands via env vars (`_agent_env`): `SM_TICKET`, `SM_REF`, `SM_WORKTREE`, `SM_PROJECT`, `SM_REPO`, plus the profile's `checkout_env` set to the agent's checkout; `add_dirs` = the profile's `extra_dirs`; skills = the profile's `skills`. `TicketStatus.session_format` (`SESSION_FORMAT` = 2) marks sessions created under this layout; older ones are never resumed (started fresh from notes). sets model/effort/cwd/permission-guard, streams events, tracks cost + `session_id`. Its `total_cost_usd`/`total_turns` are **per-session** (a fresh agent per stage session / triage episode); the orchestrator adds them to a per-ticket baseline (`_stage_base`) so the persisted `cost_usd` and `total_turns` are **cumulative across sessions**. Token/cache counters stay per-session (the cache alert needs a per-session signal), and `context_tokens`/`peak_context_tokens` come from each `AssistantMessage.usage` (the latest API call's input + cache read + cache write — the real current-size signal). Passes `BASH_MAX_OUTPUT_LENGTH` (`config.AGENT_BASH_MAX_OUTPUT`) to cap each Bash result in context. The permission guard denies merge/force-push (`_BLOCKED_FRAGMENTS`) and every CI start/re-run (`_CI_TRIGGER_FRAGMENTS`). Exports `MCP_OPEN_STAGES` (stages that get full MCP access). |
| `orchestrator.py` | orchestration | The manager loop. Owns agents, serializes turns (`MAX_ACTIVE`), pub/sub of events, the CI/PR poll, event filtering, session persistence. Task intake: `create_task` (text / github / slack; GitHub and Slack idempotent), `load_github_issues`, `load_sprint` / `add_issue` (Jira), `update_task_text`, `file_as_issue`. Tracker side effects go through `_hook` (best-effort, reported). `approve` (explore → work, pr-open → done; refused in work), `ship` (zero-LLM ship action), `trigger_ci`, pr-open triage episodes, `compact`/`goto_stage` for stage control. Broadcasts a `"status"` WS event at the end of every turn. |
| `server.py` | orchestration | FastAPI: REST + WebSocket, serves `web/` (default port 8766). Project/task endpoints: `GET`/`POST /api/projects`, `POST /api/tasks`, `POST /api/load-issues`, `GET`/`PUT /api/task-text/{t}`, `POST /api/file-issue/{t}`, `GET /api/sprints?project=`, `POST /api/load` / `/api/add` (with `project`). `/api/status` rows add `tracker_status`, the tracker `url`, and the resolved `project`. `POST /api/approve` / `/api/ship` / `/api/trigger-ci` (and the task endpoints) return 400 with an `error` when not applicable. Loads `state/config.json` on startup to apply persisted model overrides. |

## Control flow of one stage

```
UI POST /api/start/{ticket}
  → Orchestrator.start(ticket)               # resume-aware: continue saved session, or begin at explore
  → run_turn(ticket, prompt)                 # under the MAX_ACTIVE semaphore
      → _attach(ticket, stage)               # project → cwd/prompt/env; worktree + tracker hook on first use
      → state.update(activity=working)
      → agent.send(prompt, on_event)         # streams; on_event persists session_id, filters events
      → (agent works to its next gate, writes "### Summary", report_stage --activity waiting_user
         --note "G<n>: …", stops)
      → state.update(cost, session_id)
      → _broadcast(ticket, "status", state.to_dict())  # immediate UI update, no poll needed
UI shows the summary; manager replies in chat (next gate, same session) or presses
Approve (explore → work) / Ship ▶ (work → pr-open) / Approve (pr-open → done)
```

## Stage control operations

- **`approve(ticket)`** — `_advance`: full recap → notes → dispose → enter the next stage. Only
  explore → work and pr-open → done (+ the source's `on_done`). Returns `{"error": …}` in work.
- **`ship(ticket)`** → **`_ship`** (work only) — the zero-LLM ship action. `worktree.branch_state`
  refuses a dirty worktree or a branch with no commits beyond `origin/<base>`; the live work
  session (resumed if only on disk) is asked for its final recap plus a `### PR` block
  (`_SHIP_RECAP_PROMPT`, which also asks for the acceptance-criteria checklist), parsed by
  `_parse_pr_block` (the source's `reference` forced into the title when non-empty; fallbacks:
  `<ref>: <summary>` and the recap's first paragraph); the source's `pr_body_footer` (e.g.
  `Fixes owner/repo#N`, a Slack thread link) is appended; then `pr.push` + `pr.open_pr`. On a
  push/open failure the task stays in work with its session intact (retry = Ship ▶ again). On
  success: recap saved as `work — summary`, session disposed, source `on_shipped(url, new_pr)`
  (new_pr = the URL changed), task lands in pr-open with **no session** — at `waiting_external`
  with both channels armed when `ci.auto_triggers` (GitHub checks run on push), else at
  `waiting_user` ("press Trigger CI").
- **`trigger_ci(ticket)`** (pr-open only, refused while a turn is live/queued/advancing) —
  `ci.for_ticket("trigger")`: Jenkins starts a build; GitHub re-runs failed Actions runs; none
  refuses. On `triggered: true`: ends any live triage episode, sets `waiting_external`, resets
  `ci_fired`/`review_fired` (re-arm). On `false`: posts the reason, changes nothing.
- **Fresh sessions re-read tracker-backed tasks** — `_enter_stage` drops the cached meta for
  GitHub/Slack tasks so each fresh session sees new comments/replies (cached to the taskfile).
- **Task intake** — `create_task(project, tracker, …)`: text (title + body → taskfile), github
  (`#N`/URL → id `<project>-gh-<N>`, meta from the issue), slack (thread link → id
  `<project>-slack-<n>`, same thread → same task); `load_github_issues` (batch, `gh issue list`
  filters); `file_as_issue` (text/slack → `gh issue create` with problem + acceptance criteria;
  the task keeps its id and becomes `tracker = github`).
- **pr-open triage episodes** — pr-open has no standing session. `_chat` with no session (or
  `start`/`_enter_stage` for pr-open) starts one: a fresh session whose kickoff
  (`_episode_kickoff`) carries `last_signal`, the PR URL, the branch commit log + diffstat
  (`worktree.branch_state`), and the manager's message. `_maybe_end_episode` (after every pr-open
  chat turn) and `trigger_ci` / `rearm_review` call `_end_episode` once the ticket rests at
  `waiting_external`: the last summary is saved as `pr-open — triage episode (<timestamp>)` —
  **no extra recap turn** — the session is disposed and `session_id` cleared.
- **`compact(ticket)`** — saves a full recap to `notes.md` (via `_recap_then_dispose`, below),
  disposes the session, and re-enters the **same** stage with a fresh Claude session told to
  CONTINUE from the notes, not restart the stage (`_COMPACT_KICKOFF`, distinct from the generic
  stage-entry `_KICKOFF`). Equivalent to the agent calling `/compact` in the chat box. Useful when
  cache tokens are piling up; trims exploration noise (thinking/tool calls/dead ends) without
  losing the substance.
- **`goto_stage(ticket, stage)`** — saves a full recap of the departing session to the notes (a
  backward jump usually happens BECAUSE of what that session found — the target stage needs it),
  disposes the session, resets `session_id = ""`, and sets the ticket to the chosen stage at
  **`idle`** — so the panel shows **Start ▶**, not Approve (Approve would advance past the target
  stage). `start()` detects the empty session and does a full `_enter_stage`. Worktree/Jira
  provisioning lives in `_attach` and runs for ANY worktree stage (`uses_worktree`), so no path —
  Start, chat after restart, chat after a jump — can attach a write-enabled agent to the project's
  main checkout. Validates that the target is not `to-do` or `done`. Broadcasts a
  `"status"` WS event immediately.
- **Compact hint** — `_maybe_hint_compact`: once per work session, when the agent's note starts
  with `G2` (its test-plan gate) and `context_tokens` exceeds `config.CONTEXT_WARN_TOKENS` (150k), a system message suggests
  ⟳ Compact before testing. Separately, `_check_cache_anomaly` raises a CACHE ALERT above a
  per-stage line (`_CACHE_ALERT_TOKENS`: work 3M, others 1M) and on 2× growth.
- **`_recap_then_dispose(ticket, title, reason)`** — the shared primitive behind `_advance`,
  `_compact`, and `_goto_stage`. If the departing session had more than one turn, asks it for ONE
  complete, self-contained recap of the full current state (`_RECAP_PROMPT`) before saving to
  notes — a single-turn session's own summary already IS the complete picture, so that case skips
  the extra turn. Without this, notes only got whatever the agent's most recent turn happened to
  summarize — often a terse delta, not the actual state (confirmed for real: a normal Approve once
  carried forward a one-line nitpick and dropped a 7-point plan that only the live conversation
  still had). Falls back silently to the last saved summary when there's no live session to ask
  (e.g. right after a server restart, before the ticket's had a turn).

## Concurrency

- `run_turn` holds an `asyncio.Semaphore(MAX_ACTIVE)` (default 1) — only that many agent turns run
  at once. Agents in `waiting_user`/`waiting_external` hold no slot.
- The **poll loop** (`_poll_loop`, every `PR_POLL_SECONDS` = 15 min) drives the pr-open
  dual-channel model. Gate per ticket: stage is `pr-open`, activity is resting
  (`waiting_external`/`waiting_user`, never while a turn is live), and NOT both channels already
  fired — that last clause implements **"poll until BOTH fired, then stop."** `_check_external`
  probes both channels **concurrently** (`asyncio.gather(asyncio.to_thread(...))`): the Jenkins
  verdict (`ci.verdict` — the project's provider) and the review signal (`_review_signal`:
  `pr.review_signal` — one GraphQL call returning comment count + normalized decision, including a
  linked GitHub issue's comments — plus the source's `feedback_count`, e.g. a Slack thread's replies). A channel **fires on any change vs its
  watermark**: CI on a new run id or changed verdict (`ci_run_id`/`ci_status`), review on a higher
  count or changed decision (`pr_comment_count`/`review_decision`). A fire flips the ticket to
  `waiting_user`, persists that channel's watermark + `*_fired` flag, posts a notification, and
  broadcasts status (lighting the tab indicators) — **no agent turn**, EXCEPT a CI **failure**,
  which additionally `_spawn`s a background triage turn (Sonnet — starting a triage episode). Each
  fire also records its notice in `last_signal`. The `*_fired` flags **re-arm** (reset to False)
  when CI is (re-)triggered from the dashboard, and in `_run_turn`'s finally whenever the ticket settles into
  `waiting_external` (the agent's "waiting on fresh signals" declaration) — watermarks stay,
  so a genuinely new build/comment re-fires but a stale one does not. The review channel can also be
  re-armed **manually** via `Orchestrator.rearm_review` (`POST /api/rearm-review/{ticket}`, the
  clickable review indicator): it resets `review_fired` only (watermarks kept, so the just-seen
  comment won't immediately re-fire) and drops the ticket back to `waiting_external` — for dismissing
  a useless review event (usually there's nothing to do, and you wait for a more useful one); it
  also ends any live triage episode. The per-ticket
  check is exception-guarded and skipped while a turn is live (`ticket in self._running`); all state
  writes are on the event loop (threads only read), avoiding races with `state.update`.
- A **second, separate poll loop** (`_bg_job_poll_loop`, every `BG_JOB_POLL_SECONDS` = 20s) handles
  backgrounded shell jobs (`background.py`) — stage-agnostic, unlike the pr-open loop: it acts on
  ANY ticket sitting at `waiting_external` that has an actual tracked job (`background.status`
  returns inactive otherwise, so it can never collide with pr-open's own use of the same activity
  value). Much tighter cadence than the PR poll since it's a pure local file/PID check, not an
  external API call with a rate limit to respect. On completion: flips to `waiting_user`, notifies
  with the exit code + log tail, clears the job pointer — no agent turn, same "notify, don't
  replace the manual gate" philosophy as the PR poll.

## Event model (pub/sub → UI)

- Agents emit blocks → `agent.send` calls `on_event(kind, text)` with kinds
  `thinking | text | tool | result` (+ `user`/`system` injected by the orchestrator).
- `run_turn.on_event` (a) **persists `session_id`** the moment it appears — guarded by an
  agent-identity check so a session disposed mid-turn (goto-stage/compact) can't write a stale id
  over the reset — and (b) **forwards only `VISIBLE_EVENT_KINDS = {user, text, system}`** to
  `_broadcast` (`result` duplicates the final text; thinking/tool are the firehose).
- At the end of every turn (and on goto-stage), the orchestrator broadcasts a synthetic `"status"`
  event carrying the full `TicketStatus` JSON — the UI merges this immediately so stage/activity
  changes are reflected without waiting for the next `/api/status` poll. **`status` is live-only**:
  `_broadcast` skips persistence for it, and `transcript()` filters to `VISIBLE_EVENT_KINDS`, so
  stale snapshots never accumulate on disk or get replayed over fresh state on tab open.
- `_broadcast` appends (non-status events) to a per-ticket ring buffer (`TRANSCRIPT_LIMIT`) plus
  the disk transcript, and pushes to each subscribed WebSocket queue. New tab subscribers get the
  stored transcript replayed via `/api/ticket`.

## Sessions & resume

- The Agent SDK persists every session to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
- We store `session_id` (+ `session_format`) in `state/<task>.json` (persisted as soon as it appears, so a mid-turn
  crash is still resumable).
- `_attach` passes `resume_session_id=stored.session_id` to `TicketAgent` when the session is
  resumable (current `session_format`). Stop/restart and then **Start** resumes the task where it
  left off.

## Model configuration

`MODEL_BY_STAGE` in `models.py` defines the default (model, effort) for each working stage.
A module-level `_model_overrides` dict takes precedence at `model_for_stage()` call time —
so a live update via the settings UI takes effect on the **next** `_attach()` without a restart.

`set_model_overrides()` takes the same `{stage: {model, effort}}` mapping that
`state/config.json` persists, so the load path (`_load_runtime_config()` on startup) and the save
path (`POST /api/config/models`) pass it through without shape conversions.

`GET /api/config` also serves the UI's option lists — `stages` (`WORKING_STAGES`),
`model_options` (`AVAILABLE_MODELS`), `no_effort_models` — so the frontend never hardcodes stage
names or model ids (the goto-stage select and the settings dropdowns are populated from it).

## Skills and MCP

- **Skills** come from the project profile's `skills` list (default none — each enabled skill's
  metadata costs cache-read tokens on every turn), applied to every stage of that project's tasks.
- `MCP_OPEN_STAGES: frozenset[Stage]` (`agent.py`) — stages that get `strict_mcp_config=False`.
  **Empty**: every stage runs MCP-free (Jira, Confluence and Slack all go over REST). The mechanism
  is kept for any future MCP need.

## Deterministic side effects (no agent turn)

Mechanical, judgment-free work is done by the CLI layer or the orchestrator rather than spending
LLM turns:
- **Tracker updates at stage boundaries** — `_hook` calls the task's source: `on_work_start` when
  the work worktree is created (Jira → In Progress; GitHub optional assign/label; Slack optional
  👀), `on_shipped` in `_ship` (Jira → In Review + PR comment if new; Slack optional reply),
  `on_done` on the final approve (Jira → Done; GitHub: check the issue closed; Slack optional ✅).
  All best-effort (a failure is reported, never raised).
- **Ship** — commit check, push, open PR (`_ship`); the only LLM part is the final recap + PR text.
- **CI trigger / re-run** — `trigger_ci`, from the dashboard's CI button.
- **Confluence publishing + review feedback** — `confluence.py` over REST; `--ticket` also comments
  the page link on publish; `comments` reads inline comments reviewers left on the page directly.
- **Test running / infra check** — `run_tests.py` / `preflight.py` return JSON, not raw logs.
- **CI failure logs** — `ci.py logs` fetches+trims only the failing checks/stages.
- **Merge readiness / branch sync** — `pr.py ready`, `worktree.py sync`.
The judgment parts (diagnosis, comment classification, conflict resolution, plan writing) stay with
the LLM.

## Permission gating

`TicketAgent` sets `permission_mode="acceptEdits"` and a `can_use_tool` guard that **denies** Bash
commands containing `gh pr merge` / force-push (`_BLOCKED_FRAGMENTS`) or any CI start/re-run —
`ci trigger`, `jenkins trigger`, `gh run rerun`, `gh workflow run` (`_CI_TRIGGER_FRAGMENTS`).
Merging is manual by design and CI is started/re-run only from the dashboard, so both denies are
unconditional (no approval flow unblocks them).

## Testing

```bash
cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
```

97 tests in 9 modules. `tests/support.py` (imported first by every module) points
`SPRINT_MANAGER_STATE_DIR` **and** `SPRINT_MANAGER_CONFIG_DIR` at a temp dir and registers a
minimal default project `demo`, so tests never touch real tasks or profiles. The agent, `gh`,
Slack, Jenkins and Jira are faked; worktree tests run real `git` against a temp origin with a
non-default base branch. Coverage by module: `test_stages` (stage model, notes view),
`test_orchestrator_flows` (ship / CI / episodes / context), `test_projects` (profiles, resolution,
prompt rendering, preflight, worktrees, Jenkins/Jira parameterisation, example profiles, and the
**re-coupling guard** — core code, prompts, UI and examples must not name a particular repo or
company), `test_sources` (Jira source, hooks), `test_task_sources` (text/GitHub/Slack, intake,
linked-issue query, registry regression), `test_ci` (providers, prompt wording, CI block),
`test_grooming` (not-groomed marker, acceptance criteria, File as issue), `test_api` (endpoints via
FastAPI `TestClient`). Modules that import the orchestrator or server skip without the venv.

The CLI layer by hand (no venv, no LLM) — isolate state first:

```bash
cd scripts && export PYTHONPATH=$PWD
export SPRINT_MANAGER_STATE_DIR=$(mktemp -d)   # isolate: can't touch real tasks
python3 -m sprint_manager.project list
python3 -m sprint_manager.project show --project <name>
python3 -m sprint_manager.worktree list --project <name>
python3 -m sprint_manager.worktree state --ticket <TASK>
python3 -m sprint_manager.ci status --ticket <TASK>
python3 -m sprint_manager.branch --ticket ABC-1 --type Bug --summary "…"
```

`config.STATE_DIR` is the single source for the store location, so every module (`state`, `notes`,
`taskfile`, `transcript_store`, `run_tests` logs, `server`'s `config.json`) follows the override as
one unit.
