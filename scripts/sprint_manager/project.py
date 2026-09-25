"""Projects: a repo plus how to work in it, loaded from a profile (``project.toml``).

The core code knows nothing about any particular repo. Everything repo-specific — where the main
checkout lives, the base branch, the worktrees directory, build/test instructions, preflight checks,
CI, the Jira/Confluence site — comes from a **project profile**:

* ``~/.config/sprint-manager/projects/<name>/project.toml`` — personal; registers the project (and
  may hold everything, e.g. for a repo you can't commit to), and
* ``<repo>/.sprint-manager/project.toml`` — optional, committed with the repo. Because anyone who
  can push to the repo controls it, it may only set what is safe to take from the repo: its
  prompt files, preflight checks (ports / paths — env checks only report set/unset) and the base
  branch. Everything that decides where credentials go or what the agent may read (Jira / CI
  URLs and credential env names, worktrees, checkout_env, extra_dirs, skills) is personal-only;
  such keys in the repo profile are ignored and listed in ``Project.warnings``.

Relative prompt paths resolve against the directory of the profile that names them. A profile that
only says ``repo = "..."`` is valid: every other setting has a working default (see ``Project``).

CLI modules resolve "which project?" via ``resolve``: an explicit ``--project``, else the agent's
``SM_PROJECT`` env var, else the ticket's stored project, else the default project.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("SPRINT_MANAGER_CONFIG_DIR", "~/.config/sprint-manager")).expanduser()
PROJECTS_DIR = CONFIG_DIR / "projects"
APP_CONFIG_FILE = CONFIG_DIR / "config.toml"   # app-wide settings: default_project
REPO_PROFILE = Path(".sprint-manager") / "project.toml"
# The only top-level keys a repo-local profile may set (see the module docstring).
REPO_LOCAL_KEYS = {"prompts", "preflight", "base_branch"}
# Env vars a profile's checkout_env must never overwrite.
_RESERVED_ENV = {"PATH", "HOME", "USER", "SHELL", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD",
                 "LD_LIBRARY_PATH", "ANTHROPIC_API_KEY", "BASH_MAX_OUTPUT_LENGTH", "GH_TOKEN",
                 "GITHUB_TOKEN", "GIT_DIR", "GIT_WORK_TREE", "GIT_SSH_COMMAND"}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_DETECTED_BRANCH: dict[str, str] = {}   # repo path -> detected default branch (process-wide cache)

# Profile prompt files the prompts know how to use (see agent.build_system_prompt).
PROMPT_KEYS = ("test_commands", "notes", "explore", "work", "pr-open")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ProjectError(RuntimeError):
    """Raised when a project can't be found or its profile is invalid (with a fix hint)."""


@dataclass
class Project:
    """One repo and how to work in it. Built by ``load``; every field has a usable default."""

    name: str
    repo: Path                                   # main checkout (read-only stages run here)
    worktrees: Path                              # one worktree per task, a sibling of the repo
    base_branch_setting: str = ""                # "" = detect (origin/HEAD, then gh, then "main")
    checkout_env: str = ""                       # env var set to the agent's checkout, if any
    extra_dirs: list[Path] = field(default_factory=list)   # extra dirs the agent may read
    skills: list[str] = field(default_factory=list)        # skills enabled in every stage
    prompts: dict[str, Path] = field(default_factory=dict) # PROMPT_KEYS -> absolute file path
    preflight: dict = field(default_factory=dict)          # {ports, paths, env}
    ci: dict = field(default_factory=lambda: {"provider": "github"})
    jira: dict = field(default_factory=dict)     # {} = no Jira for this project
    github: dict = field(default_factory=dict)
    slack: dict = field(default_factory=dict)
    profile_files: list[Path] = field(default_factory=list)  # where this came from (diagnostics)
    warnings: list[str] = field(default_factory=list)       # e.g. ignored repo-local keys

    @property
    def base_branch(self) -> str:
        """The branch PRs target and worktrees start from: the profile's ``base_branch``, else the
        remote's default branch (``origin/HEAD``, then ``gh``), else ``main``. Cached."""
        if self.base_branch_setting:
            return self.base_branch_setting
        key = str(self.repo)
        if key not in _DETECTED_BRANCH:  # cached per repo for the process: detection can take seconds
            _DETECTED_BRANCH[key] = _detect_default_branch(self.repo) or "main"
        return _DETECTED_BRANCH[key]

    def prompt_text(self, key: str) -> str:
        """The contents of the profile's prompt file ``key`` (see PROMPT_KEYS), or ""."""
        path = self.prompts.get(key)
        return path.read_text() if path and path.exists() else ""

    @property
    def has_jira(self) -> bool:
        return bool(self.jira.get("url"))

    @property
    def has_confluence(self) -> bool:
        return self.has_jira and bool(self.jira.get("confluence_space"))

    @property
    def has_preflight(self) -> bool:
        return any(self.preflight.get(k) for k in ("ports", "paths", "env"))

    def credential_envs(self) -> list[dict]:
        """The env-var NAMES this project reads credentials from, for the Settings UI."""
        rows: list[dict] = []
        if self.has_jira:
            rows += [
                {"key": self.jira.get("email_env", "JIRA_EMAIL"), "label": "Jira email",
                 "group": "Jira", "secret": False},
                {"key": self.jira.get("token_env", "JIRA_API_TOKEN"), "label": "Jira API token",
                 "group": "Jira", "secret": True},
            ]
        if self.ci.get("provider") == "jenkins":
            rows += [
                {"key": self.ci.get("user_env", "JENKINS_USER"), "label": "Jenkins username",
                 "group": "Jenkins", "secret": False},
                {"key": self.ci.get("token_env", "JENKINS_API_TOKEN"), "label": "Jenkins API token",
                 "group": "Jenkins", "secret": True},
            ]
        return rows


def _detect_default_branch(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short",
                              "refs/remotes/origin/HEAD"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().removeprefix("origin/")
        out = subprocess.run(["gh", "repo", "view", "--json", "defaultBranchRef",
                              "-q", ".defaultBranchRef.name"], cwd=str(repo),
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"Invalid TOML in {path}: {exc}") from exc


def _resolve_prompts(data: dict, base: Path) -> dict:
    """Make the ``[prompts]`` paths of one profile absolute, relative to that profile's dir."""
    prompts = data.get("prompts") or {}
    unknown = set(prompts) - set(PROMPT_KEYS)
    if unknown:
        raise ProjectError(f"Unknown [prompts] keys {sorted(unknown)}; allowed: {list(PROMPT_KEYS)}")
    return {k: (base / Path(v).expanduser()).resolve() for k, v in prompts.items()}


def _merge(base: dict, override: dict) -> dict:
    """Shallow-merge two profiles, merging one level into tables (repo-local wins key by key)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def list_projects() -> list[str]:
    """Names of every registered project (a personal profile dir with a project.toml)."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if (p / "project.toml").exists())


def load(name: str) -> Project:
    """Build the ``Project`` for a registered name (personal profile, then the repo-local one)."""
    personal_dir = PROJECTS_DIR / name
    personal = personal_dir / "project.toml"
    if not personal.exists():
        raise ProjectError(
            f"No project named {name!r}. Register it with "
            f"`python -m sprint_manager.project add --repo <path>` (profiles live in {PROJECTS_DIR}).")
    data = _read_toml(personal)
    prompts = _resolve_prompts(data, personal_dir)
    files = [personal]
    if not data.get("repo"):
        raise ProjectError(f"{personal}: `repo = \"<path to the main checkout>\"` is required.")
    repo = Path(data["repo"]).expanduser()
    repo_profile = repo / REPO_PROFILE
    warnings: list[str] = []
    if repo_profile.exists():
        local = _read_toml(repo_profile)
        ignored = sorted(set(local) - REPO_LOCAL_KEYS)
        if ignored:
            warnings.append(f"{repo_profile}: ignored {ignored} — only {sorted(REPO_LOCAL_KEYS)} may be "
                            f"set in a repo-local profile; put the rest in {personal}.")
        local = {k: v for k, v in local.items() if k in REPO_LOCAL_KEYS}
        prompts.update(_resolve_prompts(local, repo_profile.parent))
        data = _merge(data, local)
        files.append(repo_profile)
    checkout_env = data.get("checkout_env", "")
    if checkout_env and (not _ENV_NAME.match(checkout_env) or checkout_env in _RESERVED_ENV
                         or checkout_env.startswith("SM_")):
        raise ProjectError(f"{name}: checkout_env {checkout_env!r} is not allowed (must be an "
                           f"UPPER_CASE name, not a system/credential/SM_ variable).")

    worktrees = data.get("worktrees")
    ci = {"provider": "github", **(data.get("ci") or {})}
    if ci["provider"] not in ("github", "jenkins", "none"):
        raise ProjectError(f"{name}: [ci] provider must be github, jenkins or none — got {ci['provider']!r}")
    return Project(
        name=name,
        repo=repo,
        worktrees=Path(worktrees).expanduser() if worktrees else repo.parent / f"{repo.name}-worktrees",
        base_branch_setting=data.get("base_branch", ""),
        checkout_env=checkout_env,
        extra_dirs=[Path(d).expanduser() for d in data.get("extra_dirs", [])],
        skills=list(data.get("skills", [])),
        prompts=prompts,
        preflight=data.get("preflight") or {},
        ci=ci,
        jira=data.get("jira") or {},
        github=data.get("github") or {},
        slack=data.get("slack") or {},
        profile_files=files,
        warnings=warnings,
    )


def default_project_name() -> str | None:
    """``SPRINT_MANAGER_DEFAULT_PROJECT``, else ``default_project`` in config.toml, else the only
    registered project, else None."""
    name = os.environ.get("SPRINT_MANAGER_DEFAULT_PROJECT", "")
    if not name and APP_CONFIG_FILE.exists():
        name = _read_toml(APP_CONFIG_FILE).get("default_project", "")
    if not name:
        names = list_projects()
        name = names[0] if len(names) == 1 else ""
    return name or None


def resolve(name: str | None = None, ticket: str | None = None) -> Project:
    """The project to act on: explicit ``name``, else ``$SM_PROJECT`` (set in every agent's env),
    else the ticket's stored project, else the default project."""
    name = name or os.environ.get("SM_PROJECT", "")
    if not name and ticket:
        from sprint_manager import state  # local: state is only needed on this path
        stored = state.read(ticket)
        name = stored.project if stored else ""
    name = name or default_project_name() or ""
    if not name:
        raise ProjectError(
            "No project selected and no default project. Register one with "
            "`python -m sprint_manager.project add --repo <path>`, and if you have several, set "
            f"`default_project = \"<name>\"` in {APP_CONFIG_FILE}.")
    return load(name)


def for_ticket(ticket: str) -> Project:
    """The ticket's stored project (or the default one). Unlike ``resolve`` this ignores
    ``$SM_PROJECT`` — for the server process, which acts on many projects' tickets."""
    from sprint_manager import state  # local: state is only needed on this path
    stored = state.read(ticket)
    name = (stored.project if stored else "") or default_project_name()
    if not name:
        raise ProjectError(f"{ticket} has no project and there is no default project.")
    return load(name)


def register(repo: str, name: str | None = None) -> Project:
    """Register a repo as a project with a minimal personal profile (``repo = ...``). Idempotent
    for the same repo; refuses to overwrite a different repo's profile under the same name."""
    repo_path = Path(repo).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise ProjectError(f"{repo_path} is not a git checkout.")
    name = (name or repo_path.name).lower()
    if not _NAME_RE.match(name):
        raise ProjectError(f"Invalid project name {name!r}: use lowercase letters, digits, - or _.")
    profile = PROJECTS_DIR / name / "project.toml"
    if profile.exists():
        existing = Path(_read_toml(profile).get("repo", "")).expanduser().resolve()
        if existing != repo_path:
            raise ProjectError(f"Project {name!r} already points at {existing}; pick another name.")
        return load(name)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(f'# Registered by Sprint Manager. See docs/projects.md for every setting.\n'
                       f'repo = "{repo_path}"\n')
    return load(name)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="List, show or register projects.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Registered projects")
    show = sub.add_parser("show", help="The effective settings of a project")
    show.add_argument("--project", default=None)
    add = sub.add_parser("add", help="Register a repo as a project")
    add.add_argument("--repo", required=True)
    add.add_argument("--name", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps({"projects": list_projects(), "default": default_project_name()}, indent=2))
        else:
            p = register(args.repo, args.name) if args.command == "add" else resolve(args.project)
            print(json.dumps({
                "name": p.name, "repo": str(p.repo), "worktrees": str(p.worktrees),
                "base_branch": p.base_branch, "checkout_env": p.checkout_env,
                "extra_dirs": [str(d) for d in p.extra_dirs], "skills": p.skills,
                "prompts": {k: str(v) for k, v in p.prompts.items()}, "preflight": p.preflight,
                "ci": p.ci, "jira": p.jira, "github": p.github, "slack": p.slack,
                "profile_files": [str(f) for f in p.profile_files],
            }, indent=2))
    except ProjectError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
