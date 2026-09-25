"""Filesystem paths and credential lookup, derived from this file's location.

Paths are computed from ``__file__`` (not from the current working directory) so they stay
correct even when a script runs from inside a ticket worktree. Credentials come from environment
variables; the getters raise a clear, actionable error when something is missing instead of
failing deep inside an HTTP call.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The app's own root, derived from this file's location:
#   <APP_ROOT>/scripts/sprint_manager/config.py
# parents[0]=sprint_manager [1]=scripts [2]=<APP_ROOT>.
# The app can live anywhere (e.g. ~/sprint-manager) — it does NOT need to be inside the repo.
_CONFIG_FILE = Path(__file__).resolve()
APP_ROOT = _CONFIG_FILE.parents[2]

# Local, gitignored secrets file — an alternative to exporting credentials in a shell profile.
# Written to by the Settings UI (config.set_credential); loaded into os.environ below, BEFORE
# anything else in this module reads an env var, so every credential getter benefits transparently.
ENV_FILE = APP_ROOT / ".env"


def _load_dotenv_file() -> None:
    """Populate ``os.environ`` from ``.env``, without overriding a real shell-exported variable.

    Hand-rolled (not the ``python-dotenv`` package) to keep this stdlib-only, matching the rest of
    the zero-LLM CLI layer. Supports plain ``KEY=VALUE`` lines, ``#`` comments, blank lines, and
    tolerates optional surrounding quotes on the value.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)  # a real shell export always wins over .env


_load_dotenv_file()

# Runtime status store: one JSON file per ticket (gitignored), kept with the app. Override with
# SPRINT_MANAGER_STATE_DIR to point at an isolated directory — e.g. a temp dir for tests, so a
# verification run can never touch (or leave junk in) the real ticket store.
STATE_DIR = Path(os.environ.get("SPRINT_MANAGER_STATE_DIR", str(APP_ROOT / "state"))).expanduser()

# The ONE non-ticket file that deliberately lives alongside the per-ticket JSON files in STATE_DIR
# (model-override settings from the settings UI). Named here, once, so state.py's ticket-file glob
# can skip it by name instead of trying to parse it as a TicketStatus.
RUNTIME_CONFIG_FILE = STATE_DIR / "config.json"

# The web service listens here (override the port with SPRINT_MANAGER_PORT).
WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.environ.get("SPRINT_MANAGER_PORT", "8766"))

# How often to poll a pr-open ticket's two feedback channels (CI verdict + code review). Every
# 15 minutes by default — the poll is token-free (CLI/HTTP only), so a short cadence is cheap.
PR_POLL_SECONDS = int(os.environ.get("SPRINT_MANAGER_PR_POLL_SECONDS", "900"))
# Back-compat alias (older env var / references); PR_POLL_SECONDS is the one the poll loop uses.
CI_POLL_SECONDS = int(os.environ.get("SPRINT_MANAGER_CI_POLL_SECONDS", str(PR_POLL_SECONDS)))

# Cap (characters) on each Bash tool result an agent sees — passed to the CLI as
# BASH_MAX_OUTPUT_LENGTH. A tool result stays in context for every later API call of the session,
# so one unfiltered mvn/suite dump is re-read hundreds of times; the prompt tells agents to redirect
# long output to a file and grep/tail it instead. ~15k chars ≈ 4k tokens.
AGENT_BASH_MAX_OUTPUT = int(os.environ.get("SPRINT_MANAGER_BASH_MAX_OUTPUT", "15000"))

# Context size (tokens re-read by the latest API call) above which the panel's context indicator
# turns amber and the work stage's G2 "compact before testing?" hint fires.
CONTEXT_WARN_TOKENS = int(os.environ.get("SPRINT_MANAGER_CONTEXT_WARN_TOKENS", "150000"))

# How often to poll a ticket's backgrounded shell job (e.g. a slow compile launched via
# background.py) for completion. Unlike PR_POLL_SECONDS this is a pure local file/PID check — no
# external API, no rate limit to respect — so a tight cadence is cheap and gives near-instant
# notification once the job actually finishes.
BG_JOB_POLL_SECONDS = int(os.environ.get("SPRINT_MANAGER_BG_POLL_SECONDS", "20"))


class ConfigError(RuntimeError):
    """Raised when a required credential is missing, with a setup hint for the user."""


def _require(project, what: str, envs: dict[str, str], hint: str = "") -> list[str]:
    """Read the named env vars; raise a ConfigError naming exactly which ones to export."""
    values = [os.environ.get(env, "") for env in envs]
    missing = [env for env, value in zip(envs, values) if not value]
    if missing:
        lines = "\n".join(f"  export {env}=...   # {envs[env]}" for env in missing)
        raise ConfigError(f"{what} credentials for project {project.name!r} are not configured. "
                          f"Set (shell profile, .env, or ⚙ Settings → Credentials):\n{lines}"
                          + (f"\n{hint}" if hint else ""))
    return values


def jira_credentials(project) -> tuple[str, str, str]:
    """Return ``(base_url, email, api_token)`` for the project's Jira site, or raise with a hint.

    The site comes from the profile's ``[jira] url``; the credential env-var NAMES from
    ``email_env`` / ``token_env`` (default ``JIRA_EMAIL`` / ``JIRA_API_TOKEN``). Jira is accessed
    over REST with a personal API token, not via an (interactively authenticated) MCP server.
    """
    jira = project.jira
    if not jira.get("url"):
        raise ConfigError(f"Project {project.name!r} has no Jira configured ([jira] url in its profile).")
    email, token = _require(project, "Jira", {
        jira.get("email_env", "JIRA_EMAIL"): "your Atlassian account email",
        jira.get("token_env", "JIRA_API_TOKEN"):
            "create at https://id.atlassian.com/manage-profile/security/api-tokens",
    })
    return jira["url"].rstrip("/"), email, token


def browse_url(project, key: str) -> str:
    """The Jira browse link for an issue in ``project``, or "" if the project has no Jira."""
    base = (project.jira.get("url") or "").rstrip("/") if project else ""
    return f"{base}/browse/{key}" if base else ""


def board_id(project) -> str:
    """The Jira Agile board whose sprints the dashboard lists (``[jira] board``)."""
    board = project.jira.get("board")
    if not board:
        raise ConfigError(f"Project {project.name!r} has no [jira] board in its profile.")
    return str(board)


def confluence_credentials(project) -> tuple[str, str, str]:
    """Return ``(wiki_base_url, email, api_token)`` for the Confluence REST API.

    Confluence Cloud lives on the SAME Atlassian site as Jira and accepts the SAME email + API
    token. The wiki base is ``<jira url>/wiki`` unless the profile sets ``[jira] confluence_url``.
    """
    base_url, email, token = jira_credentials(project)
    wiki = (project.jira.get("confluence_url") or f"{base_url}/wiki").rstrip("/")
    return wiki, email, token


def slack_credentials() -> str:
    """Return a Slack bot token for the Web API, or raise with a setup hint.

    Uses a Slack app's bot token (``xoxb-...``) over the plain Web API — not the Slack MCP, which
    is interactively OAuth-authenticated and unavailable to a headless agent subprocess (same
    reasoning as ``jira_credentials``).
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise ConfigError(
            "Slack credentials are not configured. Create a Slack app (or reuse an existing one) "
            "with a bot token scoped to channels:history, groups:history, users:read, and "
            "channels:read (add groups:read too for private channels), install it to the "
            "workspace, then set:\n"
            '  export SLACK_BOT_TOKEN="xoxb-..."\n'
            "If a fetch later fails with not_in_channel, invite the bot to that channel first."
        )
    return token


def jenkins_credentials(project) -> tuple[str, str, str]:
    """Return ``(base_url, user, api_token)`` for the project's Jenkins (``[ci]`` in its profile)."""
    ci = project.ci
    if not ci.get("url"):
        raise ConfigError(f"Project {project.name!r} uses Jenkins but has no [ci] url in its profile.")
    user, token = _require(project, "Jenkins", {
        ci.get("user_env", "JENKINS_USER"): "your Jenkins username",
        ci.get("token_env", "JENKINS_API_TOKEN"): "Jenkins → your user → Configure → API Token",
    })
    return ci["url"].rstrip("/"), user, token


# ----- Settings UI: credential status + .env editing --------------------------------------------

# The env-var-backed credentials the Settings UI can show and edit: app-wide ones, plus whatever
# env-var NAMES the registered projects' profiles declare (Jira / Jenkins). Anything typed in there
# is written to .env (see set_credential) and applied to THIS process's os.environ immediately — no
# restart needed, since every getter above reads os.environ fresh on every call.
_APP_CREDENTIALS: list[dict] = [
    {"key": "SLACK_BOT_TOKEN", "label": "Slack bot token", "group": "Slack", "secret": True},
]


def credential_fields() -> list[dict]:
    """App-wide credential fields plus every registered project's, de-duplicated by env name."""
    from sprint_manager import project as project_mod  # local: project is optional at import time

    fields = list(_APP_CREDENTIALS)
    seen = {f["key"] for f in fields}
    for name in project_mod.list_projects():
        try:
            declared = project_mod.load(name).credential_envs()
        except project_mod.ProjectError:
            continue
        for f in declared:
            if f["key"] not in seen:
                seen.add(f["key"])
                fields.append(f)
    return fields


def _mask(value: str) -> str:
    """Show only the last 4 characters of a secret — enough to recognize it, not to reuse it."""
    return "••••" + value[-4:] if len(value) > 4 else "••••"


def credentials_status() -> list[dict]:
    """Every credential field's configured state, for the Settings UI (secrets masked)."""
    rows = []
    for field in credential_fields():
        raw = os.environ.get(field["key"], "")
        rows.append({
            "key": field["key"], "label": field["label"], "group": field["group"],
            "secret": field["secret"], "set": bool(raw),
            "value": _mask(raw) if field["secret"] and raw else raw,
        })
    return rows


def _write_env_var(key: str, value: str) -> None:
    """Update (or append) one ``KEY=VALUE`` line in .env, preserving the rest of the file."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)  # secrets — owner read/write only


def set_credential(key: str, value: str) -> None:
    """Persist one credential to .env and apply it to this process immediately (no restart)."""
    if key not in {f["key"] for f in credential_fields()}:
        raise ValueError(f"Unknown credential: {key}")
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Credential values can't contain line breaks.")
    _write_env_var(key, value)
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)  # an explicit blank clears it (falls back to shell env/default)


def external_auth_status() -> list[dict]:
    """Status of the two logins that are NOT env vars — gh CLI and the Claude subscription login.

    Read-only in the Settings UI: each needs its own interactive login flow (device auth / OAuth),
    not a value to paste in, so this only reports whether it's done and how to fix it if not.
    """
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        gh_ok = result.returncode == 0
        gh_hint = "" if gh_ok else "Run `gh auth login` in a terminal."
    except (FileNotFoundError, subprocess.TimeoutExpired):
        gh_ok, gh_hint = False, "gh CLI is not installed or not responding."

    claude_ok = (Path.home() / ".claude" / ".credentials.json").exists()
    return [
        {"label": "GitHub CLI (gh)", "ok": gh_ok, "hint": gh_hint},
        {"label": "Claude account login", "ok": claude_ok,
         "hint": "" if claude_ok else "Run `claude` then `/login` in a terminal."},
    ]
