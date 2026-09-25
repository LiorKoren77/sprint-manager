"""GitHub PR operations for a ticket, performed inside its worktree via the ``gh`` CLI.

Covers what the lifecycle needs: push the branch, open the PR, and read review state/comments.
Merging is deliberately NOT offered — the manager merges manually on GitHub, outside this system.
All commands run with the ticket's worktree as the working directory so ``gh`` auto-detects the
repo and branch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import project as project_mod  # noqa: E402
from sprint_manager.worktree import worktree_path  # noqa: E402


def _run(args: list[str], cwd: Path) -> str:
    """Run a command in ``cwd`` and return stripped stdout, raising on failure."""
    result = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _current_branch(cwd: Path) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)


def _repo_slug(cwd: Path) -> str:
    """Return ``owner/repo`` for the worktree's GitHub remote."""
    return _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd)


def push(ticket: str) -> str:
    """Push the ticket's branch to origin, setting upstream. Returns the branch name."""
    cwd = worktree_path(ticket)
    branch = _current_branch(cwd)
    _run(["git", "push", "-u", "origin", branch], cwd)
    return branch


def _open_pr_field(cwd: Path, field: str) -> str:
    """Return ``field`` of the branch's OPEN PR, or "" if none.

    Uses ``gh pr list --state open`` rather than ``gh pr view``: the latter resolves the branch's
    most recent PR even when it is CLOSED or MERGED, which is exactly wrong for idempotence and
    polling — a dead PR must not shadow the need for a fresh one.
    """
    branch = _current_branch(cwd)
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", field, "-q", f".[0].{field}"],
        cwd=str(cwd), text=True, capture_output=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def open_pr(ticket: str, title: str, body: str) -> str:
    """Open a PR for the ticket's branch against the project's base branch. Returns the PR URL.

    Idempotent: if the branch already has an OPEN PR (the ticket looped back through earlier
    stages and shipped again), return that PR's URL instead of failing on a duplicate create —
    the fresh push has already updated it. A closed/merged PR does NOT count: a new one is created.
    """
    cwd = worktree_path(ticket)
    existing = _open_pr_field(cwd, "url")
    if existing:
        return existing
    return _run(
        ["gh", "pr", "create", "--base", project_mod.resolve(ticket=ticket).base_branch,
         "--title", title, "--body", body],
        cwd,
    )


def pr_number(ticket: str) -> int | None:
    """Return the OPEN PR number for the ticket's branch, or ``None`` (closed/merged don't count)."""
    out = _open_pr_field(worktree_path(ticket), "number")
    return int(out) if out.isdigit() else None


def open_pr_info(ticket: str) -> dict | None:
    """Return ``{"number": int, "url": str}`` for the branch's OPEN PR, or ``None`` if there isn't one.

    One ``gh`` call for both fields — used by the poll loop, which needs the URL (to detect and heal
    a stale ``pr_url`` after the open PR is closed and a fresh one created for the same branch) as
    well as the number (to query CI/review), every cycle.
    """
    cwd = worktree_path(ticket)
    branch = _current_branch(cwd)
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "number,url", "-q", ".[0]"],
        cwd=str(cwd), text=True, capture_output=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    data = json.loads(result.stdout)
    return {"number": data["number"], "url": data["url"]}


def _norm_review(decision: str | None, count: int) -> str:
    """Normalize GitHub's reviewDecision (+ comment count) to our tab/state vocabulary."""
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "changes"
    return "commented" if count > 0 else "none"


def review_signal(ticket: str, number: int | None = None,
                  linked_issue: tuple[str, str, int] | None = None) -> dict | None:
    """Return ``{count, decision}`` for the open PR in ONE GraphQL call, or ``None`` if no open PR.

    ``count`` = issue comments + reviews + inline-thread comments (the "any comment?" watermark).
    ``decision`` = normalized review status ("none"|"commented"|"changes"|"approved"), which drives
    both the "review changed" signal and the tab indicator. This is the pr-open poll's cheap probe;
    the full comment bodies are only fetched (via ``comments()``) when the agent needs to read them.
    Pass ``number`` to reuse an already-resolved PR number and skip a ``gh`` round-trip.
    ``linked_issue`` = ``(owner, repo, number)`` of the task's GitHub issue: its comments are
    counted too, in the SAME GraphQL call (a new issue comment fires the review channel).
    """
    cwd = worktree_path(ticket)
    if number is None:
        number = pr_number(ticket)
    if number is None:
        return None
    owner, name = _repo_slug(cwd).split("/", 1)
    vars_ = ["-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
    issue_part, issue_vars = "", ""
    if linked_issue:
        issue_vars = ",$iowner:String!,$iname:String!,$inum:Int!"
        issue_part = "linked:repository(owner:$iowner,name:$iname){issue(number:$inum){comments{totalCount}}}"
        vars_ += ["-F", f"iowner={linked_issue[0]}", "-F", f"iname={linked_issue[1]}",
                  "-F", f"inum={linked_issue[2]}"]
    query = (
        f"query($owner:String!,$name:String!,$number:Int!{issue_vars}){{"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "reviewDecision comments{totalCount} reviews{totalCount} "
        f"reviewThreads(first:100){{nodes{{comments{{totalCount}}}}}}}}}} {issue_part}}}"
    )
    data = json.loads(_run(["gh", "api", "graphql", "-f", f"query={query}", *vars_], cwd))
    pull = data["data"]["repository"]["pullRequest"]
    inline = sum(n["comments"]["totalCount"] for n in pull["reviewThreads"]["nodes"])
    count = pull["comments"]["totalCount"] + pull["reviews"]["totalCount"] + inline
    if linked_issue:
        issue = (data["data"].get("linked") or {}).get("issue") or {}
        count += (issue.get("comments") or {}).get("totalCount", 0)
    return {"count": count, "decision": _norm_review(pull.get("reviewDecision"), count)}


def feedback_total(ticket: str) -> int | None:
    """Total review-feedback items on the open PR, or ``None`` when there is none. Thin wrapper
    over :func:`review_signal` kept for back-compat."""
    sig = review_signal(ticket)
    return sig["count"] if sig is not None else None


def comments(ticket: str) -> dict:
    """Return PR review state plus general, inline, and review-body comments.

    Combines the three GitHub comment surfaces (matching handle-pr's get-pr-comments) so the agent
    sees everything a human reviewer left.
    """
    cwd = worktree_path(ticket)
    number = pr_number(ticket)
    if number is None:
        return {"pr": None, "comments": "no open PR for this branch"}
    repo = _repo_slug(cwd)
    overview = json.loads(
        _run(["gh", "pr", "view", str(number), "--json", "url,state,reviewDecision,title"], cwd)
    )
    general = json.loads(_run(["gh", "api", f"repos/{repo}/issues/{number}/comments"], cwd))
    inline = json.loads(_run(["gh", "api", f"repos/{repo}/pulls/{number}/comments"], cwd))
    reviews = json.loads(_run(["gh", "api", f"repos/{repo}/pulls/{number}/reviews"], cwd))
    return {
        "pr": number,
        "overview": overview,
        "general_comments": general,
        "inline_comments": inline,
        "reviews": reviews,
    }


def ready(ticket: str) -> dict:
    """One JSON verdict on whether the PR is mergeable — collapses the several probes the agent
    would otherwise improvise when you ask "can I merge this?".

    Combines GitHub's review decision + mergeability with the CI verdict from the project's CI
    provider (``ci.py``). ``ready`` is the AND of: CI passed (or the project has no CI), review
    approved, no merge conflicts. (Merging itself stays manual on GitHub.)
    """
    cwd = worktree_path(ticket)
    number = pr_number(ticket)
    if number is None:
        return {"pr": None, "ready": False, "reason": "no open PR for this branch"}
    gh = json.loads(_run(
        ["gh", "pr", "view", str(number), "--json",
         "url,state,reviewDecision,mergeable,mergeStateStatus"], cwd))
    from sprint_manager import ci as ci_module  # local import: ci imports this module
    proj = project_mod.resolve(ticket=ticket)
    ci = ci_module.verdict(proj, number)["verdict"]
    review_ok = gh.get("reviewDecision") == "APPROVED"
    no_conflicts = gh.get("mergeable") != "CONFLICTING"
    ci_ok = ci == ci_module.PASSED or ci_module.provider(proj) == "none"
    return {
        "pr": number,
        "url": gh.get("url"),
        "ci": ci,
        "review_decision": gh.get("reviewDecision") or "NONE",
        "mergeable": gh.get("mergeable"),
        "merge_state": gh.get("mergeStateStatus"),
        "ready": bool(ci_ok and review_ok and no_conflicts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub PR operations for a ticket.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("push", "comments", "ready"):
        p = sub.add_parser(name)
        p.add_argument("--ticket", required=True)

    open_cmd = sub.add_parser("open", help="Open a PR")
    open_cmd.add_argument("--ticket", required=True)
    open_cmd.add_argument("--title", required=True)
    open_cmd.add_argument("--body", default="")

    args = parser.parse_args(argv)
    if args.command == "push":
        print(push(args.ticket))
    elif args.command == "open":
        print(open_pr(args.ticket, args.title, args.body))
    elif args.command == "comments":
        print(json.dumps(comments(args.ticket), indent=2))
    elif args.command == "ready":
        print(json.dumps(ready(args.ticket), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
