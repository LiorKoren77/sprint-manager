"""Manage one isolated git worktree per ticket under its project's worktrees directory.

A worktree gives each agent its own working directory and branch while sharing the main repo's
object store (the history is NOT duplicated). Branch creation happens here via ``git worktree add``
so it never touches — or switches — your main checkout. The repo, worktrees dir and base branch all
come from the ticket's project (``project.resolve``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import project as project_mod  # noqa: E402
from sprint_manager.branch import branch_name  # noqa: E402


def _project(ticket: str | None = None, name: str | None = None):
    return project_mod.resolve(name, ticket)


def _git(proj, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command against the project's main repo and return the completed process."""
    return subprocess.run(
        ["git", "-C", str(proj.repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _remote_branch_exists(proj, branch: str) -> bool:
    out = _git(proj, "ls-remote", "--heads", "origin", branch).stdout
    return branch in out


def _local_branch_exists(proj, branch: str) -> bool:
    return _git(proj, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


def worktree_path(ticket: str) -> Path:
    return _project(ticket).worktrees / ticket


def add(ticket: str, issue_type: str, summary: str) -> dict:
    """Create (or reuse) the worktree for a ticket and return ``{ticket, branch, path}``.

    Reuses an existing branch when one already exists for the ticket; otherwise branches fresh
    from ``origin/<base branch>``. Idempotent: if the worktree directory already exists, it is
    returned as-is.
    """
    proj = _project(ticket)
    branch = branch_name(ticket, issue_type, summary)
    path = proj.worktrees / ticket
    result = {"ticket": ticket, "branch": branch, "path": str(path)}

    if path.exists():
        return result

    proj.worktrees.mkdir(parents=True, exist_ok=True)
    _git(proj, "fetch", "origin", proj.base_branch)

    if _local_branch_exists(proj, branch):
        _git(proj, "worktree", "add", str(path), branch)
    elif _remote_branch_exists(proj, branch):
        _git(proj, "fetch", "origin", branch)
        _git(proj, "worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}")
    else:
        _git(proj, "worktree", "add", "-b", branch, str(path), f"origin/{proj.base_branch}")

    return result


def remove(ticket: str, force: bool = True) -> None:
    """Remove a ticket's worktree (used on ``done`` cleanup)."""
    proj = _project(ticket)
    args = ["worktree", "remove", str(proj.worktrees / ticket)]
    if force:
        args.append("--force")
    _git(proj, *args, check=False)
    _git(proj, "worktree", "prune", check=False)


def list_worktrees(project: str | None = None) -> str:
    """Return ``git worktree list`` output for the project's repo, for inspection."""
    return _git(_project(name=project), "worktree", "list").stdout


def _git_wt(ticket: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command INSIDE the ticket's worktree (not the main checkout)."""
    return subprocess.run(
        ["git", "-C", str(worktree_path(ticket)), *args],
        text=True, capture_output=True, check=check,
    )


def sync(ticket: str, abort: bool = False) -> dict:
    """Bring the ticket's branch up to date with ``origin/<base branch>`` by merging it in.

    Deterministic plumbing for the common "base branch moved under a long-lived PR" case. Returns
    ``{ok, action, conflicts}``. On conflicts it leaves the tree in the conflicted state (with the
    file list) for the agent to resolve and commit — resolution is the judgment part, not this. Use
    ``--abort`` to back out an in-progress conflicted merge.
    """
    proj = _project(ticket)
    path = proj.worktrees / ticket
    if not path.exists():
        return {"ok": False, "action": "none", "error": f"no worktree at {path}"}
    if abort:
        _git_wt(ticket, "merge", "--abort", check=False)
        return {"ok": True, "action": "aborted", "conflicts": []}

    _git(proj, "fetch", "origin", proj.base_branch)  # fetch into the shared object store
    merge = _git_wt(ticket, "merge", "--no-edit", f"origin/{proj.base_branch}", check=False)
    if merge.returncode == 0:
        updated = "Already up to date" not in merge.stdout
        return {"ok": True, "action": "merged" if updated else "up-to-date", "conflicts": []}
    conflicts = _git_wt(ticket, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    return {
        "ok": False,
        "action": "conflicts",
        "conflicts": conflicts,
        "message": "Resolve the conflicts in the worktree and commit, or run sync --abort to back out.",
    }


def branch_state(ticket: str) -> dict:
    """Deterministic "is this branch shippable?" facts for the ticket's worktree.

    Returns ``{ok, dirty: [paths], ahead: int, log: str, diffstat: str}`` — ``dirty`` is the
    porcelain status (uncommitted or untracked files), ``ahead`` the number of commits on the branch
    not in ``origin/<base branch>``, and ``log``/``diffstat`` a compact picture of the branch's work (used
    to orient a fresh pr-open triage session without replaying the implementation history).
    """
    proj = _project(ticket)
    path = proj.worktrees / ticket
    if not path.exists():
        return {"ok": False, "error": f"no worktree at {path}"}
    base = f"origin/{proj.base_branch}"
    dirty = [line[3:] for line in _git_wt(ticket, "status", "--porcelain").stdout.splitlines() if line]
    ahead = _git_wt(ticket, "rev-list", "--count", f"{base}..HEAD", check=False).stdout.strip()
    log = _git_wt(ticket, "log", "--oneline", f"{base}..HEAD", check=False).stdout.strip()
    diffstat = _git_wt(ticket, "diff", "--stat", f"{base}...HEAD", check=False).stdout.strip()
    return {"ok": True, "dirty": dirty, "ahead": int(ahead) if ahead.isdigit() else 0,
            "log": log, "diffstat": diffstat}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage per-ticket git worktrees.")
    sub = parser.add_subparsers(dest="command", required=True)

    add_cmd = sub.add_parser("add", help="Create/reuse a ticket worktree")
    add_cmd.add_argument("--ticket", required=True)
    add_cmd.add_argument("--type", required=True, dest="issue_type")
    add_cmd.add_argument("--summary", required=True)

    rm_cmd = sub.add_parser("remove", help="Remove a ticket worktree")
    rm_cmd.add_argument("--ticket", required=True)

    sync_cmd = sub.add_parser("sync", help="Merge origin/<base branch> into the ticket's branch")
    sync_cmd.add_argument("--ticket", required=True)
    sync_cmd.add_argument("--abort", action="store_true", help="Back out a conflicted merge")

    state_cmd = sub.add_parser("state", help="Dirty files / commits ahead / diffstat vs the base branch")
    state_cmd.add_argument("--ticket", required=True)

    list_cmd = sub.add_parser("list", help="List a project's worktrees")
    list_cmd.add_argument("--project", default=None)

    args = parser.parse_args(argv)
    if args.command == "add":
        print(json.dumps(add(args.ticket, args.issue_type, args.summary), indent=2))
    elif args.command == "remove":
        remove(args.ticket)
        print(f"removed worktree for {args.ticket}")
    elif args.command == "sync":
        print(json.dumps(sync(args.ticket, args.abort), indent=2))
    elif args.command == "state":
        print(json.dumps(branch_state(args.ticket), indent=2))
    else:
        print(list_worktrees(args.project), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
