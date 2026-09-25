# Sprint Manager — Frontend design

Vanilla HTML/CSS/JS under `web/` — **no build step**. Served by FastAPI as static files at `/`.
Three files: `index.html` (structure), `styles.css` (styling + state colours), `app.js` (logic).

## Layout

- **Header** — **project selector** (`<select id="project">`: *All projects* or one; filters the
  table and picks where new tasks/sprints go; remembered in `localStorage`), **＋ Project**
  (prompts for a repo path → `POST /api/projects`), **＋ New task** (opens the New-task dialog), and
  — only when the selected project has a `[jira]` table — the sprint picker (`<select id="sprint">`)
  + "Load sprint"; ⚙ gear button (opens Settings drawer); 🌙 theme toggle.
- **New-task dialog** (`<dialog id="task-dialog">`) — a project select and four tabs:
  *Describe it* (title, kind bug/feature, problem text → `POST /api/tasks {tracker: "text"}`),
  *GitHub issue* (URL or `#N` → `{tracker: "github"}`; or **Load issues** by assignee/label →
  `POST /api/load-issues`), *Slack thread* (link, optional title, kind → `{tracker: "slack"}`),
  *Jira issue* (key/URL → `POST /api/add`; a hint shows when the project has no Jira). On success the
  dialog closes and the new task's tab opens.
- **Overview table** (`#overview-table`) — one `<tbody class="issue">` (two rows) per task:
  task · project · kind · **tracker** (`jira: In Review` / `github: open` / `text`, from
  `tracker_status`) · stage · activity · note · CI · cost · **✕ remove** button. Clicking any row
  opens its tab.
- **Detail section** — a tab strip (`#tabs`) with a round-robin **"Next ⚠ needs you"** button, and
  a panel (`#panel`) with:
  - Task title (a link to its tracker — Jira issue, GitHub issue, Slack thread — or plain text for
    a text task), stage/activity badges, context indicator
  - **✎ Problem** (text tasks: edit the problem statement — `GET`/`PUT /api/task-text/{t}`, in
    `<dialog id="text-dialog">`) and **⇪ File as issue** (text and Slack tasks: `POST
    /api/file-issue/{t}` after a confirm)
  - A–/A+ font size controls
  - **Start ▶** (to-do, or idle after a stage jump), **Approve** (explore: "Approve plan → work ▶";
    pr-open: "Approve (merged) → done ✓"; hidden in work), **Ship ▶** (work only), the CI button
    (pr-open only; **Trigger CI** for Jenkins projects, **Re-run CI** for GitHub checks, hidden for
    CI `none`) — the last three when waiting\_user or waiting\_external — **⏹ Stop** (working
    only), **⟳ Compact** (active, not working/idle), **↩ go to stage** select
  - Transcript area, queued-messages display, chat textarea

## Client state (`app.js`)

```js
state = {
  rows,          // latest /api/status
  openTickets,   // tickets with an open tab, in open order
  selected,      // currently shown ticket
  socket,        // WebSocket to the selected ticket
  fontPx,        // transcript font size (persisted to localStorage)
  queue,         // messages sent but awaiting agent acknowledgement
  drafts,        // per-ticket unsent input drafts: ticket → string
  projects,      // /api/projects rows: {name, repo, default, jira, ci}
  project,       // table filter / target project ("" = all; persisted to localStorage)
  taskTracker,   // the New-task dialog's active tab
}
```

## Server contract

| Call | Purpose |
|---|---|
| `GET /api/projects` | the project selector / dialog (`{projects: [{name, repo, default, jira, ci}], default}`) |
| `POST /api/projects {repo, name?}` | register a repo as a project |
| `GET /api/sprints?project=` | populate the sprint picker for a Jira project (`current`/`next` labels; 400 without Jira) |
| `GET /api/status` | the overview table (polled every 3 s); rows add `project`, `tracker_status`, `url` |
| `GET /api/ticket/{t}` | panel detail + replay the stored transcript on open |
| `POST /api/load {sprint, project}` | load a Jira sprint's issues |
| `POST /api/add {url, project}` | add one Jira issue |
| `POST /api/tasks {project, tracker, title?, kind?, body?, ref?}` | create a text / GitHub-issue / Slack-thread task → `{key}` (400 with `error` on bad input) |
| `POST /api/load-issues {project, assignee?, label?, milestone?}` | open GitHub issues → tasks |
| `GET` / `PUT /api/task-text/{t} {body}` | read / edit a text task's problem statement |
| `POST /api/file-issue/{t}` | promote a text / Slack task to a GitHub issue |
| `POST /api/start/{t}` | start/resume the ticket's agent (runs to its next gate) |
| `POST /api/approve/{t}` | explore → work, or pr-open → done (400 in work) |
| `POST /api/ship/{t}` | work only: commit check, final recap + PR text, push, open PR → pr-open (400 elsewhere) |
| `POST /api/trigger-ci/{t}` | pr-open only: start (Jenkins) or re-run failed (GitHub) CI (400 elsewhere / while working / provider none) |
| `POST /api/rearm-review/{t}` | dismiss a review event, re-arm the review poll channel |
| `POST /api/interrupt/{t}` | interrupt an in-flight turn |
| `POST /api/compact/{t}` | save last summary to notes, restart the stage with fresh context |
| `POST /api/goto-stage/{t} {stage}` | jump to explore / work / pr-open (disposes session, resets state) |
| `DELETE /api/ticket/{t}` | remove a task from the dashboard |
| `GET /api/config` | return effective model+effort config for all working stages |
| `POST /api/config/models [{…}]` | save model+effort overrides (takes effect on next stage start) |
| `WS /ws/{t}` | receive `{kind, text}` events; send a line to chat; `/compact` resets context |

Events arriving on the WebSocket have `kind ∈ {user, text, system, status}`:
- `thinking`/`tool`/`result` are dropped on the backend; the transcript stays summary-level.
- `status` is a special control event (not shown): carries a JSON-encoded `TicketStatus` dict that
  the frontend merges into `state.rows` and renders (panel header from the MERGED row — the raw
  payload lacks the poll rows' `url` enrichment — plus table and tabs) without waiting for the
  next `/api/status` poll. It is live-only: the backend never persists it, and transcript replay
  is filtered, so stale snapshots can't overwrite fresh data on tab open.

## UI behaviours

- **Per-issue draft text** — `state.drafts` stores each ticket's unsent input. On `openTicket`,
  the outgoing ticket's textarea content is saved; the incoming ticket's draft is restored.
- **Approve gate** — shown when `activity === "waiting_user" || "waiting_external"` in explore and
  pr-open, labelled per stage (`APPROVE_LABELS`). Posts `POST /api/approve/{t}`. Like Compact, the
  backend gets a full recap from the finishing session (not just its last turn) before handing off
  to the next stage — see `_recap_then_dispose` in `docs/backend.md`. Moving between gates *inside*
  a stage is a chat reply ("yes", "go on to testing"), not a button.
- **Ship ▶** — work only, same visibility rule. Confirms first, then `POST /api/ship/{t}`. The
  backend refuses a dirty worktree or a branch with nothing to ship (reason in the transcript);
  on success the task lands in pr-open with the PR link shown (already polling if the project's CI
  runs on push).
- **Trigger CI / Re-run CI** — pr-open only, same visibility rule; label and visibility follow the
  task's project's CI provider (from `/api/projects`). `POST /api/trigger-ci/{t}`; hidden
  optimistically and re-shown if nothing was triggered (the reason is posted to the transcript —
  e.g. Jenkins hasn't indexed the PR yet, or no failed GitHub runs to re-run).
- **Compact** — gets a full recap of the stage's current state from the session (not just its last
  turn), saves that to `notes.md`, and starts a fresh session at the same stage told to continue
  from it rather than restart the stage. Available whenever active and not working or idle. Also
  triggered by `/compact` in the chat box.
- **Go to stage** — a `<select>` in the panel header whose options are populated from
  `GET /api/config`'s `stages` list (nothing hardcoded in the HTML). On change, calls
  `POST /api/goto-stage/{t}` with the selected stage, then resets to the placeholder. The
  backend saves the departing session's summary to notes and parks the ticket at **`idle`** in the
  new stage — which is what makes the panel show **Start ▶** (fresh entry) instead of Approve
  (which would advance past the stage you just jumped to).
- **Start** — shown for a to-do ticket AND for an idle ticket in a working stage (i.e. after a
  stage jump or a migration from the old stage names). Both call `POST /api/start/{t}`; the backend
  enters explore, resumes the stored session, or begins the stage fresh (in pr-open: a triage
  episode), respectively.
- **Context indicator** — shows the current session's context size (`context_tokens`: tokens the
  latest API call processed, i.e. what every next call re-reads) as `context: 180k`. Amber above
  `context_warn_tokens` from `/api/config` (`SPRINT_MANAGER_CONTEXT_WARN_TOKENS`, default 150k); the
  tooltip adds the session peak and the cumulative cache read. Hidden until the session's first call.
- **Queue display** — messages sent while the agent is working are shown in a dashed box below the
  transcript so you can see what's pending, and cleared once the turn settles.
- **Round-robin** — **Next ⚠ needs you** cycles through tickets that need the human: activity
  `waiting_user`, plus tickets parked at `idle` in a working stage (a goto-stage jump awaiting
  Start), so a jumped ticket can't silently stall out of the rotation.
- **Error surfacing** — dialog actions report into the dialog's own message line (`postTo`);
  panel actions (start/approve/ship/trigger-ci/stop/compact/goto-stage/file-issue) go through a shared
  `postJSON` helper that reports network errors and 4xx/5xx responses (e.g. goto-stage's "Unknown
  stage") as a system line in the transcript instead of silently doing nothing.
- **No-change render skip** — `refreshStatus` compares the raw `/api/status` body with the
  previous poll and skips re-rendering when identical, so the 3-second poll doesn't destroy text
  selections in the table.
- **Start feedback** — clicking **Start** immediately hides the button (optimistic); the next status
  poll restores the correct state once the agent picks it up.
- **Close tab** — each tab has an ×; `closeTab` removes it from `openTickets` and falls back to
  another tab (or hides the panel).
- **Remove ticket** — the ✕ in the overview table deletes state/transcript/notes (confirms first);
  the git worktree/branch is left untouched.

## Settings drawer

The ⚙ gear button (top-right of the header, left of the theme toggle) opens a slide-in drawer:

- **Left nav** — extendable category list; currently: **Models**, **Credentials** (the fields come
  from the registered projects' profiles — exactly the env-var names they declare — plus
  `SLACK_BOT_TOKEN`).
- **Models section** — a table of all working stages × model dropdown × effort dropdown. Stage
  names, model ids/labels, and the no-effort model set all come from `GET /api/config` (sourced
  from `models.py`) — the client hardcodes none of them, so a backend rename or model bump can't
  leave the UI listing stale values.
  - Models in `no_effort_models` (Haiku) auto-disable the effort column (no default stage uses
    Haiku any more; it remains selectable).
  - **Save** POSTs to `/api/config/models`, applies immediately, persists to `state/config.json`.
    Changes take effect on the **next** stage start (running sessions are unaffected).
- Drawer closes on overlay click or ✕ button.

## pr-open CI + review indicators

For tickets in the `pr-open` stage, two small coloured dots appear in the tab (beside the activity
dot) and as badges in the panel header — so you can see each PR's CI and review status at a glance
from any page (tabs always render). They're driven by the `ci_status` and `review_decision` fields
in the `/api/status` payload (and the WS `status` push), so they update within the 15-min poll or
instantly when a channel fires:

- **CI:** grey = none/running · green = passed · red = failed (incl. unstable)
- **Review:** grey = none · blue = commented · amber = changes-requested · green = approved

Rendered by `prLights(r)` (tabs) and the `panel-ci`/`panel-review` badges (panel); non-pr-open
tickets show neither.

## Activity colours (`styles.css`)

`working` green · `queued` indigo · `waiting_user` red · `waiting_external` amber · `idle` grey.
Same palette used for the table badge, the tab dot, and the panel badges.

## Extending the UI

- **New column** → edit the `<thead>` in `index.html` and the row template in `renderTable`.
- **New control** → add the element in `index.html`, style in `styles.css`, wire at the bottom of
  `app.js` (the `$("id").onclick = …` block).
- **New event kind in transcript** → add it to `VISIBLE_EVENT_KINDS` in `orchestrator.py`
  (backend) and to the label map in `addEvent` (frontend).
- **New settings category** → add a `<button class="settings-nav-item" data-section="…">` to the
  nav in `index.html`, a matching `<section id="settings-…">` in the drawer body, and the
  load/render/save functions in `app.js`.


## Access token

The API and the chat WebSocket require the per-launch token the server prints at startup (see
`docs/backend.md` → *Local API access control*). The page reads it once from `?token=…`, keeps it
in `localStorage`, removes it from the address bar, and sends it on every call (a `fetch` wrapper
adds `X-SM-Token`; the WebSocket URL carries `?token=`). A 401 shows a red banner: the server was
restarted with a new token — open the newly printed URL. Everything the server returns is escaped
before it goes into HTML (`escapeHtml`, and `cls()` for class-name fragments); PR links must be
`https://`.
