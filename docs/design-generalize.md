# Design: generalize beyond Jira and acme (projects × task sources)

Status: **implemented (2026-09-24)** · builds on `design-explore-work-triage.md` · profile reference: `projects.md`

## 1. Goal

Today the app works on **one repo (acme)** and **one kind of input (a Jira ticket)**. The goal:

- **Any repo.** The core code contains nothing acme/Acme-specific. Each repo is a
  **project** described by a profile; acme becomes one profile among many, kept **outside** this
  codebase.
- **Any textual problem.** A task can be a Jira ticket, a **GitHub issue**, a **Slack thread**, or
  **free text** you paste in. The lifecycle (explore → work → Ship → pr-open → done) is the same for all.

Non-goals: code hosts other than GitHub (`gh` stays the PR/issue backend), renaming the app, and
running tasks from several projects in one worktree.

## 2. Today's coupling (what must move)

| Area | Where | acme/Jira-specific content |
|---|---|---|
| Repo layout | `config.py` | `ACME_REPO` (~/acme), `WORKTREES_DIR` (~/acme-worktrees), `PLUGINS_DIR` (acme/ai_infra/claude/plugins), `SHARED_SCRIPTS` |
| Base branch | `pr.py`, `worktree.py` | `BASE_BRANCH = "develop"` |
| Checkout env | `agent.py` | `ACME=<cwd>` (for the Maven enforcer) |
| Default cwd | `run_tests.py`, `background.py`, `orchestrator._attach` | fall back to `ACME_REPO` |
| Build/test | `references/test-commands.md`, `stages/work.md` | acme servers, suites, Maven `-am` rules, `$ACME` |
| Preflight | `preflight.py` | ports manager/ai_utils/postgres/mongodb/elasticsearch, services venvs, `ACME_LIC_FILE` |
| CI | `jenkins.py`, `config.py` | `jenkins.example.internal`, `PIPELINE = "CI_acme"`, `PR-<n>` job path, `ACME_JENKINS_*` creds; CI is manual-trigger only |
| Prompts | `stages/*.md`, `agent.py` | `acme-analyzer`, `plan-executor`, `handle-pr`, `jenkins-ci-debugger` plugins; `SKILLS_BY_STAGE` enables `jenkins-ci-debugger:analyze` |
| Task identity | everywhere | the Jira key (`ABC-\d+`) names state/notes/transcript files, worktree dir, `$SM_TICKET`, API routes; `parse_issue_key` accepts only that |
| Intake | `fetch_sprint.py`, `sprints.py`, `orchestrator.add_issue` | Jira sprint search / board 132 / single Jira key |
| Context | `fetch_sprint.simplify_issue`, `ticket-context.md` | Jira fields; Jira issue type → branch prefix |
| Side effects | `orchestrator._jira_*` | In Progress / In Review + PR comment / Done |
| Docs tracker | `confluence.py`, `config.py` | `acme.atlassian.net`, project `ABC`, Confluence space `ABC` |
| UI | `index.html`, `app.js` | sprint picker, "Jira issue URL or key", "Jira status" column, Jira link |

## 3. Concepts

### 3.1 Project (a repo + how to work in it)

A **project profile** is a directory with a `project.toml`, plus optional Markdown files it
references. It is looked up in this order (first found wins):

1. `<repo>/.sprint-manager/project.toml` — committed with the repo, shared by its team.
2. `~/.config/sprint-manager/projects/<name>/project.toml` — personal (where the acme profile goes,
   so nothing changes in the acme repo).

Registered projects = every profile under `~/.config/sprint-manager/projects/`, plus repos added in
the UI (**Add project** → a repo path → writes a minimal personal profile that points at it, and
picks up the repo-local one if present).

```toml
name = "acme"                        # short id; used in task ids and worktree paths
repo = "~/acme"                      # main checkout (read-only stages run here)
base_branch = "develop"             # default: the GitHub repo's default branch (gh repo view)
worktrees = "~/acme-worktrees"       # default: "<repo parent>/<repo name>-worktrees"
checkout_env = "ACME"                # env var set to the agent's checkout; default: none
extra_dirs = ["~/acme/ai_infra/claude/plugins"]  # extra readable dirs for the agent (add_dirs)
skills = ["jenkins-ci-debugger:analyze"]        # default: none

[prompts]                           # all optional; paths relative to the profile dir
test_commands = "test-commands.md"  # how to build/run suites — today's references/test-commands.md
notes = "agent-notes.md"            # project rules for every stage (Maven -am, plugin pointers …)
explore = "explore-notes.md"        # per-stage additions (e.g. "offer acme-analyzer in G1")
work = "work-notes.md"
pr-open = "pr-open-notes.md"        # e.g. handle-pr taxonomy, jenkins-ci-debugger usage

[preflight]                         # today's hardcoded tables, as data
ports = { manager = [8080, "start via manager/src/scripts/debug.sh"], postgres = [5432, "…"] }
paths = { "ai_utils_service venv" = "services/ai_utils/.venv" }   # relative to checkout
env = { ACME_LIC_FILE = "set ACME_LIC_FILE or configure Zentitle" }

[ci]
provider = "jenkins"                # "github" (default) | "jenkins" | "none"
url = "http://jenkins.example.internal:8080"
job = "/job/CI_acme/job/PR-{pr}"     # job path template
user_env = "ACME_JENKINS_USER"       # NAMES of env vars holding creds — ~/.bashrc stays unchanged
token_env = "ACME_JENKINS_API_TOKEN"

[jira]                              # optional — enables the Jira task source for this project
url = "https://example.atlassian.net"
project = "ABC"
board = 132
confluence_space = "ABC"            # optional — enables plan publishing
email_env = "JIRA_EMAIL"
token_env = "JIRA_API_TOKEN"

[github]                            # optional — GitHub-issue source settings
assign_self = true                  # self-assign the issue when work starts
in_progress_label = ""              # e.g. "in progress"; "" = don't label
```

**Zero-config default:** a GitHub repo with *no* profile still works: base branch from GitHub,
default worktrees dir, CI = GitHub checks, no preflight, and no test-commands file. The work prompt
then tells the agent to find the project's build/test commands (README, CONTRIBUTING, CI config)
and **confirm them with you at G2** before running anything.

The profile is loaded by a new zero-LLM module `project.py` (stdlib `tomllib`) into a `Project`
dataclass. Everything in §2's "Repo layout / Base branch / Checkout env / Default cwd / Build/test
/ Preflight / CI / Prompts" rows reads from it instead of module constants.

### 3.2 Task (what to solve)

A task keeps today's `TicketStatus` record (internal name `ticket` stays in code, routes and state
— see §7) and gains:

| Field | Meaning |
|---|---|
| `project` | profile name; picks the repo, worktrees, CI, prompts |
| `tracker` | `jira` \| `github` \| `slack` \| `text` |
| `kind` | `bug` \| `feature` — branch prefix; from Jira type / `bug` label / your choice |
| `external_ref` | Jira key, GitHub issue number, or Slack `<channel>/<thread_ts>`; empty for text |
| `external_url` | browse link, if any |

(`source` already exists and means *intake* — "sprint" / "manual" — so it stays as is.)

**Task ids** must be safe as a file name, branch component and env var, and unique across projects:

| Tracker | id | example |
|---|---|---|
| jira | the key (already globally unique) | `ABC-81034` |
| github | `<project>-gh-<n>` | `acme-gh-412` |
| slack | `<project>-slack-<n>` (per-project counter) | `acme-slack-3` |
| text | `<project>-t-<n>` (per-project counter) | `acme-t-7` |

The problem text lives in `state/<id>.task.md` (durable, editable from the panel: "clarify the
problem" without a new task). For Jira/GitHub/Slack it is a cached rendering, refreshed from the tracker at every fresh session
(a Slack thread is a live discussion — later replies are part of the problem).

### 3.3 Task source (a tracker adapter)

`sources/` — one small module per tracker, same interface, stdlib only:

```python
class Source(Protocol):
    name: str                                         # "jira" | "github" | "slack" | "text"
    def load(self, task) -> TaskContent               # title, kind, body (Markdown), comments, url, status
    def reference(self, task) -> str                  # "ABC-81034" | "#412" | ""   (commit/PR tag)
    def feedback_count(self, task) -> int | None      # tracker-side comments, for the pr-open poll
    def pr_body_footer(self, task) -> str             # "" | "Fixes #412" | ""
    def on_work_start(self, task) -> str | None       # side effects → a one-line report (or None)
    def on_shipped(self, task, pr_url) -> str | None
    def on_done(self, task) -> str | None
```

| | jira | github | slack | text |
|---|---|---|---|---|
| load | Jira REST (today's `simplify_issue`) | `gh issue view N --json title,body,labels,comments,state,url` | `slack.fetch_thread(url)` (exists today) → messages as Markdown | `state/<id>.task.md` |
| title | summary | title | you enter it (prefilled from the first message) | you enter it |
| kind | issue type | `bug` label → bug | chosen in the form | chosen in the form |
| reference (commits/PR title) | `ABC-81034` | `#412` | none | none |
| PR body footer | — | `Fixes #412` | thread link | — |
| on_work_start | → In Progress | optional self-assign / label | optional 👀 reaction on the thread root | — |
| on_shipped | → In Review + PR-link comment (only if the URL changed) | nothing — `Fixes #412` links the PR in the issue timeline | optional thread reply with the PR link | — |
| on_done | → Done | check the issue is closed; if not, report it (auto-close only happens when the PR merges into the **default** branch) | optional ✅ reaction | — |
| feedback_count (pr-open poll) | — | issue comment count — **folded into the existing PR GraphQL query** (`issue(number:N){comments{totalCount}}`), so no extra call | thread reply count (`conversations.replies`, one call) | — |
| batch intake | sprint (board) | `gh issue list` by assignee / label / milestone | — | — |

**Slack specifics.** Reading needs the bot scopes `slack.py` already uses (`channels:history` /
`groups:history`, `users:read`); the optional reactions and reply need `reactions:write` /
`chat:write` — off by default (`[slack] react = false`, `reply_on_ship = false` in the profile), so a
read-only token keeps working. The bot must be a member of the channel. A thread can be **filed as
a GitHub issue** like a text task (§4), keeping the thread link in the issue body.

**Tracker comments as a pr-open signal.** The review channel's watermark becomes
`PR comments + reviews + inline + tracker feedback_count`, so a new comment on the linked GitHub
issue or a new reply in the source Slack thread fires the same "new review comment(s)" notification
(no agent turn unless you engage). The review indicator's re-arm/dismiss works unchanged. Cost: zero
LLM tokens; for GitHub zero extra API calls, for Slack one `conversations.replies` per poll cycle
(every 15 min) per pr-open Slack task.

The orchestrator's `_jira_transition` / `_jira_comment` / `_jira_in_progress` become calls to
`source.on_*`; `_ensure_meta` / `_fetch_meta` become `source.load`. All side effects stay
best-effort and never block a stage, exactly as today.

**GitHub issue format** (for reference): title + **Markdown body**, labels, assignees, milestone,
optional org-level issue type and sub-issues, comments. Repos may define templates in
`.github/ISSUE_TEMPLATE/` — Markdown with front matter, or YAML **issue forms** whose fields still
render into the body as `### <field>` sections. The body is passed to the agent as-is; no parsing.

### 3.4 CI provider

`ci/` — `jenkins.py` becomes one implementation; `github.py` is new; `none` is trivial:

```python
class CI(Protocol):
    auto_triggers: bool                    # does a push start a build by itself?
    def verdict(self, pr: int) -> dict     # {verdict: running|passed|failed|no-build, run: {id, url}}
    def trigger(self, pr: int) -> dict     # {triggered: bool, reason?}
    def logs(self, pr: int) -> dict        # failing stages/jobs + trimmed log tails
```

- **github** (default): `gh pr checks <n> --json name,state,bucket,link` → verdict;
  `gh run view <id> --log-failed` → logs; `trigger` = `gh run rerun <id> --failed`. `auto_triggers`
  = true.
- **jenkins**: today's code, parameterised by `url` / `job` / cred env names from the profile.
  `auto_triggers` = false.
- **none**: verdict always `no-build`; the poll watches reviews only.

Effect on the lifecycle: with `auto_triggers`, Ship lands the ticket at **`waiting_external`**
(polling starts at once) instead of "press Trigger CI", and the **Trigger CI** button becomes
**Re-run CI** (reruns failed jobs). The agent's CI-trigger block stays for every provider.

## 4. Prompts

The core prompts get **no** project-specific text. Project text enters through the profile's
Markdown files, appended as `## Project notes` sections in the system prompt. Because they are
fixed per project, the system prompt stays byte-identical for every session of the same
(project, stage) and still caches (§ cacheable prompt in the previous design).

| Placeholder / text today | Becomes |
|---|---|
| `<<PLUGINS>>/acme-analyzer`, `plan-executor`, `handle-pr`, `jenkins-ci-debugger` | moved to the acme profile's notes files |
| `<<TEST_COMMANDS_PATH>>` | the profile's `test_commands`, or the "discover and confirm at G2" rule |
| Maven `-am` / `$ACME` rules in `work.md` | acme `work-notes.md` |
| "Jira ticket", "ticket key in the commit message" | "task"; "include `$SM_REF` in commit messages and the PR title **if it is set**" |
| `<<JENKINS_CMD>> status/logs` | `<<CI_CMD>> status/logs` (dispatches to the project's provider) |
| "press Trigger CI" | provider-dependent sentence, rendered from `ci.auto_triggers` |

New env vars for agents: `SM_PROJECT`, `SM_REF` (the reference tag; empty for text tasks),
`SM_REPO` (main checkout). `SM_TICKET` / `SM_WORKTREE` stay. `checkout_env` (e.g. `ACME`) is set only
if the profile names one.

**Explore for underspecified input.** A text task (and a thin GitHub issue) usually lacks what a
groomed Jira ticket has. For `tracker != jira` (text, GitHub, Slack), G1 becomes: restate the problem, list assumptions
and open questions, and propose **acceptance criteria**, then stop. The approved criteria:

- go into the explore summary (so work sees them in the notes view),
- are the definition of done at work's G4 (the agent checks each one), and
- are appended to the PR body at Ship.

**File as issue** (text and Slack tasks, explore or later): `gh issue create` from the task text + acceptance
criteria → the task becomes `tracker = github` with `external_ref = <n>`. Its id does **not** change
(files, worktree and branch keep their names); from then on commits/PR carry `#<n>` and the PR body
`Fixes #<n>`.

## 5. UI

- **Header:** project selector (all / one project), **＋ New task**, **Add project**.
- **＋ New task** dialog: project; then one of
  - *Describe it*: title, kind (bug/feature), problem text (large textarea, Markdown; Slack links
    are fetched as today);
  - *GitHub issue*: URL or `#number`;
  - *Slack thread*: the thread link (title prefilled from the first message, editable);
  - *Jira issue*: key or URL (only if the project has `[jira]`).
- **Batch intake:** "Load sprint" only for projects with `[jira]`; "Load issues" for GitHub
  (assignee `@me` + optional label/milestone).
- **Table:** Task · Project · Kind · Tracker (`jira: In Review` / `github: open` / `text`) · Stage ·
  Activity.
- **Panel:** title link only when `external_url` exists; "Edit problem text" for text tasks;
  **File as issue** for text and Slack tasks; the CI button label follows the provider.
- **⚙ Settings → Credentials:** groups are generated from the profiles' `*_env` names instead of
  the fixed Jira/Jenkins list.

## 6. Guard against re-coupling

A unit test fails if `acme`, `acme`, `services` or `ABC` appears (case-insensitive) anywhere in
`scripts/sprint_manager/`, `references/` or `web/`. Example profiles in this repo (`examples/`) are
generic (a Python project, a Node project).

## 7. Decisions

| Decision | Choice | Why |
|---|---|---|
| Profile location | repo-local first, then `~/.config/sprint-manager/projects/` | a team can commit theirs; the acme one stays personal and outside this codebase |
| Profile format | TOML (`tomllib`, stdlib) | CLI layer must stay dependency-free; comments allowed |
| Rename `ticket` → `task` in code | **no** (UI and prompts only) | it's in ~15 files, every API route and the state files; pure churn + a migration for no behaviour change. Revisit later if wanted |
| Task ids | Jira key / `<project>-gh-<n>` / `<project>-t-<n>` | unique across projects, safe in paths/branches/env |
| GitHub "done" | rely on `Fixes #N`; verify on final Approve | GitHub already does it; only closes on default-branch merges, so verify |
| CI default | GitHub checks | zero config, no creds, works for any GitHub repo |
| Existing acme data | migrate to project `acme`, tracker `jira` | no user action needed |

## 8. Migration

- A one-time `python -m sprint_manager.project migrate-legacy` writes
  `~/.config/sprint-manager/projects/acme/` from today's constants: `project.toml` (repo, develop,
  worktrees, `ACME` checkout env, plugins dir, skills, preflight tables, Jenkins, Jira/Confluence)
  and the notes files, extracted from `test-commands.md` and the acme-specific parts of the stage
  prompts. After it runs, acme behaves exactly as today — the acceptance test for phase 1.
- Existing `state/*.json`: missing `project` → `acme`, missing `tracker` → `jira`, `kind` from
  `issue_type` (in `TicketStatus.from_dict`, like the stage aliases).
- Credential env var names are unchanged (the acme profile names `ACME_JENKINS_*`), so `~/.bashrc`
  keeps working.

## 9. Rollout

Each phase is independently testable; phase 1 is the largest and riskiest.

1. **Projects.** `project.py` + `Project`; every §2 layout/build/CI/prompt constant reads from it;
   `migrate-legacy` writes the acme profile; the re-coupling guard test. **Acceptance:** acme works
   exactly as before, driven by the external profile; the guard test passes.
2. **Source interface, Jira behind it.** `sources/jira.py`; orchestrator calls `source.*`; new
   `TicketStatus` fields + migration. No behaviour change.
3. **Text, GitHub and Slack sources.** `sources/text.py`, `sources/github.py`, `sources/slack.py`,
   New-task dialog, "Load issues", Tracker column, `SM_REF` / `Fixes #N` in PRs, tracker
   `feedback_count` in the pr-open review watermark.
4. **CI providers.** `ci/github.py`, `ci/none.py`, the `auto_triggers` lifecycle change, Re-run CI.
5. **Explore for text/GitHub tasks.** Acceptance-criteria G1 and G4 check, **File as issue**.
6. **Docs + generic credentials UI + example profiles.**

Tests per phase: the existing 28 keep passing; new tests for profile loading/defaults, each source
(with `gh`/Jira faked), each CI provider (faked), id generation, state migration, and prompt
rendering (no unresolved placeholders; no project text in the core prompt files).

## 10. Resolved questions

1. **Turn slots across projects** — `MAX_ACTIVE` (how many agents may be mid-turn at once) stays
   **one app-wide limit**, not per project: every agent draws on the same Claude subscription and
   its usage limits regardless of project, and one-at-a-time keeps the manager in the loop. Raise
   `SPRINT_MANAGER_MAX_ACTIVE` for parallelism.
2. **Tracker comments as a pr-open signal** — **yes** (§3.3): new comments on the linked GitHub
   issue / replies in the source Slack thread fire the review channel. Zero LLM cost; zero extra
   GitHub calls; one Slack call per poll per Slack task.
3. **Slack thread as a task source** — **yes, in phase 3** (§3.3).
4. **Other code hosts** (GitLab, Bitbucket) — out of scope; the CI/source interfaces leave room.

## 11. Implementation notes / deviations

- **Preflight format.** Ports are inline tables `{ port, hint, host? }` (not `[port, hint]` arrays as
  sketched in §3.1); `paths` may be relative to the agent's checkout **or absolute / `~`** (the acme
  profile's venvs live in the main checkout, not in worktrees); `env` maps a variable to a hint.
- **Where "not groomed" lives.** The marker is the `Source:` line of the first message
  (`references/ticket-context.md`, rendered by `agent.build_context_message`), not a conditional in
  the system prompt — so the system prompt stays byte-identical per (project, stage) and keeps
  caching across tasks. explore's G1 reads "if your first message marks the task as not groomed …".
- **GitHub CI verdict.** One call — `gh pr view <n> --json headRefOid,statusCheckRollup` — covers
  CheckRuns and legacy StatusContexts (skipped checks ignored). The run watermark is
  `<sha12>:<comma-joined Actions run ids>`, so a new push or a re-run fires the CI channel. Field
  names were verified against a real PR.
- **Tracker status field.** `TicketStatus.jira_status` is kept (no state migration) as the
  tracker-status snapshot and exposed to the UI as `tracker_status`.
- **The acme setup is an external profile.** Everything formerly hardcoded for acme lives in
  `~/.config/sprint-manager/projects/acme/`: `project.toml` (repo, `develop`, worktrees,
  `checkout_env = "ACME"`, plugins dir, skills, preflight tables, Jenkins, Jira/Confluence, ABC
  description fields), `test-commands.md`, `agent-notes.md` / `explore-notes.md` / `work-notes.md` /
  `pr-open-notes.md` (Maven `-am`, acme-analyzer, plan-executor, jenkins-ci-debugger, handle-pr), and
  the old `reuse-map.md`. `references/test-commands.md` and `references/reuse-map.md` were removed
  from the repo; `default_project = "acme"` is set in `~/.config/sprint-manager/config.toml`. The
  §8 `migrate-legacy` command was not built — the profile was written once by hand, since generating
  it would have put acme strings back into core code.
- **Credentials UI** is generated from the profiles' `*_env` names (`config.credential_fields`) plus
  `SLACK_BOT_TOKEN`, already in phase 1.
- **A bug found in testing.** `sources._load_builtin` originally returned early whenever the registry
  was non-empty, so importing one source module directly (as `create_task` does for
  `parse_issue_ref`) left jira/text unregistered. It is now keyed on a flag, with a regression test
  running in a fresh interpreter.
- **Tests.** 97 tests across 9 modules (`scripts/tests/`), including a **re-coupling guard**
  (`test_projects.NoRecouplingGuardTest`: no `acme|acme|services|ABC` in core code, prompts,
  UI, launcher, `.env.example` or `examples/`), real-git worktree tests with a non-default base
  branch, and API tests via FastAPI `TestClient`. `tests/support.py` isolates both the state and the
  config dir.
- **Rollout** landed all six phases at once in v2, each with its own tests.
