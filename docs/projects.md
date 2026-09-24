# Projects and task sources

Sprint Manager works on any git repository hosted on GitHub. Each repo you use it on is a
**project**, described by a **profile**. Each piece of work is a **task**, which comes from a
**source**: text you type, a GitHub issue, a Slack thread, or a Jira issue.

## Registering a project

```bash
cd scripts
python3 -m sprint_manager.project add --repo ~/src/widgets          # name defaults to the dir name
python3 -m sprint_manager.project list
python3 -m sprint_manager.project show --project widgets            # the effective settings
```

Or use **＋ Project** in the dashboard. Registering writes a minimal personal profile,
`~/.config/sprint-manager/projects/<name>/project.toml`, containing `repo = "..."`. That profile is
enough to start. Every other setting has a default.

With several projects, set the one that new tasks and sprints go to by default in
`~/.config/sprint-manager/config.toml`:

```toml
default_project = "widgets"
```

## Where a profile lives

1. **Personal:** `~/.config/sprint-manager/projects/<name>/project.toml`. This registers the
   project and may hold everything, e.g. for a repo you can't commit to.
2. **Repo-local** (optional, committed with the repo so the team shares it):
   `<repo>/.sprint-manager/project.toml`. Its keys override the personal profile's, table by table.

Prompt file paths are relative to the directory of the profile that names them.

## Every setting

```toml
repo = "~/src/widgets"            # REQUIRED — the main checkout (the read-only explore stage runs here)
base_branch = "main"              # default: origin/HEAD, then GitHub's default branch, then "main"
worktrees = "~/src/widgets-worktrees"   # default: "<repo parent>/<repo name>-worktrees"
checkout_env = ""                 # env var set to the agent's checkout (e.g. for a build tool); default none
extra_dirs = []                   # extra dirs every agent may read (shared tooling, plugins)
skills = []                       # Claude skills enabled in every stage (each costs context per turn)

[prompts]                         # all optional Markdown files
test_commands = "test-commands.md"  # how to build + run the suites (without it, the agent finds out
                                    # and confirms the commands with you at work G2)
notes = "agent-notes.md"          # project rules for every stage
explore = "explore-notes.md"      # added to the explore stage only (e.g. an extra G1 question)
work = "work-notes.md"            # added to the work stage only (build quirks, preflight check names)
pr-open = "pr-open-notes.md"      # added to pr-open triage (CI-debugging tools, review conventions)

[preflight]                       # what `sprint_manager.preflight` checks before a suite runs
ports = { api = { port = 8080, hint = "start with ./run-api.sh" }, db = { port = 5432 } }
paths = { "api venv" = "api/.venv" }        # relative to the agent's checkout, or absolute / ~
env = { LICENSE_FILE = "set LICENSE_FILE to your license path" }

[ci]
provider = "github"               # github (default) | jenkins | none
# jenkins only:
# url = "http://jenkins.example.com:8080"
# job = "/job/<pipeline>/job/PR-{pr}"   # {pr} = the PR number
# user_env = "JENKINS_USER"             # NAMES of the env vars holding the credentials
# token_env = "JENKINS_API_TOKEN"

[github]                          # GitHub-issue tasks
assign_self = false               # self-assign the issue when work starts
in_progress_label = ""            # label to add when work starts ("" = none)

[slack]                           # Slack-thread tasks (reading needs only the read scopes)
react = false                     # 👀 when work starts, ✅ when done  (bot scope reactions:write)
reply_on_ship = false             # reply in the thread with the PR link (bot scope chat:write)

[jira]                            # optional — enables Jira tasks + sprint loading for this project
url = "https://example.atlassian.net"
project = "ABC"
board = 12                        # the Agile board whose sprints the dashboard lists
confluence_space = "ENG"          # optional — enables plan publishing in explore
email_env = "JIRA_EMAIL"          # NAMES of the env vars holding the credentials
token_env = "JIRA_API_TOKEN"
description_fields = { Bug = "customfield_10001" }   # per-type custom description fields, if any
```

Credentials are always named, never stored, in a profile. The values come from your shell, `.env`,
or **⚙ Settings → Credentials**, which lists exactly the variables your projects' profiles name.

## CI providers

| Provider | Verdict from | Starts on push? | Dashboard button |
|---|---|---|---|
| `github` (default) | `gh pr view --json statusCheckRollup` (GitHub checks) | yes: Ship goes straight to polling | **Re-run CI** (re-runs failed Actions runs) |
| `jenkins` | Jenkins classic REST API, `[ci] job` path | no: waits for you | **Trigger CI** |
| `none` | nothing | — | none; only code review is polled |

Agents can never start CI. Every trigger and re-run command is permission-blocked.

## Task sources

| Source | Add it with | Task id | Commits / PR title carry | PR body gets | Tracker updates |
|---|---|---|---|---|---|
| Text | ＋ New task → *Describe it* | `<project>-t-<n>` | nothing | — | none |
| GitHub issue | ＋ New task → *GitHub issue* (URL or `#N`), or *Load issues* | `<project>-gh-<n>` | `#<n>` | `Fixes owner/repo#<n>` | optional assign/label; GitHub closes the issue on merge into the default branch |
| Slack thread | ＋ New task → *Slack thread* (link) | `<project>-slack-<n>` | nothing | the thread link | optional 👀 / reply / ✅ |
| Jira issue | ＋ New task → *Jira issue*, or *Load sprint* | the Jira key | the key | — | In Progress → In Review + PR comment → Done |

- **Tasks that aren't groomed get an acceptance-criteria step.** For text, GitHub-issue and Slack
  tasks, explore's first gate also asks for assumptions, open questions and a numbered
  `### Acceptance criteria` list. The approved list is the definition of done at work's G4 and goes
  into the PR body as a checklist.
- **File as issue.** A text or Slack task can be promoted to a GitHub issue from the panel. The
  task keeps its id, and from then on it references and closes that issue.
- **Tracker comments count as review signals.** New comments on the linked GitHub issue, or new
  replies in the source Slack thread, fire the pr-open review notification just like PR comments.
  GitHub issue comments are read in the PR's own GraphQL call; a Slack thread costs one API call
  per poll.
- **Tracker content is re-read every session.** GitHub-issue and Slack tasks are fetched again at
  every fresh session, and the latest copy is cached in `state/<id>.task.md` for offline restarts.
  A text task's problem statement is that file; edit it with **✎ Problem**.
