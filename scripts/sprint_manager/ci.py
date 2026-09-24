"""CI status for a PR, dispatched to the project's CI provider (``[ci] provider`` in its profile).

* ``github`` (default) — GitHub checks, read with ``gh`` (no credentials beyond ``gh auth``). A push
  starts a run by itself (``auto_triggers``); "trigger" re-runs the failed Actions runs.
* ``jenkins`` — the project's Jenkins (``jenkins.py``); builds start only when triggered.
* ``none``    — no CI; the verdict is always ``no-build`` and only code review is polled.

Every provider answers the same three questions, zero-LLM:

    python -m sprint_manager.ci status  --ticket T     # {verdict: running|passed|failed|no-build, run}
    python -m sprint_manager.ci logs    --ticket T     # failing checks/stages + trimmed log tails
    python -m sprint_manager.ci trigger --ticket T     # manager-only (the agent is blocked from it)

``run.id`` is the watermark the pr-open poll uses to tell a new run from a re-seen one (Jenkins:
the build number; GitHub: the PR head commit + the set of runs).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402
from sprint_manager import pr as pr_module  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402

RUNNING, PASSED, FAILED, NONE = "running", "passed", "failed", "no-build"

_RUN_URL = re.compile(r"/actions/runs/(\d+)")


def auto_triggers(proj) -> bool:
    """Does a push start a CI run by itself? (Decides whether Ship waits for Trigger CI.)"""
    return proj.ci.get("provider", "github") == "github"


def provider(proj) -> str:
    return proj.ci.get("provider", "github")


# ----- GitHub checks -----------------------------------------------------------------------

def _gh_json(proj, *args: str, cwd: Path | None = None) -> dict:
    out = subprocess.run(["gh", *args], cwd=str(cwd or proj.repo), capture_output=True, text=True,
                         timeout=60)
    if out.returncode != 0:
        raise config.ConfigError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def _normalize_checks(rollup: list[dict]) -> list[dict]:
    """``statusCheckRollup`` entries (CheckRun + legacy StatusContext) → ``{name, state, url}``
    with state in running / passed / failed / skipped."""
    checks = []
    for c in rollup or []:
        if c.get("__typename") == "StatusContext":
            raw = (c.get("state") or "").upper()
            st = {"SUCCESS": PASSED, "PENDING": RUNNING, "EXPECTED": RUNNING}.get(raw, FAILED)
            checks.append({"name": c.get("context", "?"), "state": st, "url": c.get("targetUrl", "")})
            continue
        status, concl = (c.get("status") or "").upper(), (c.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            st = RUNNING
        elif concl in ("SUCCESS", "NEUTRAL"):
            st = PASSED
        elif concl == "SKIPPED":
            st = "skipped"
        else:
            st = FAILED
        name = c.get("name", "?")
        if c.get("workflowName"):
            name = f"{c['workflowName']} / {name}"
        checks.append({"name": name, "state": st, "url": c.get("detailsUrl", "")})
    return checks


def _github_rollup(proj, pr_number: int) -> tuple[str, list[dict]]:
    data = _gh_json(proj, "pr", "view", str(pr_number), "--json", "headRefOid,statusCheckRollup")
    return data.get("headRefOid", ""), _normalize_checks(data.get("statusCheckRollup") or [])


def _github_verdict(proj, pr_number: int) -> dict:
    sha, checks = _github_rollup(proj, pr_number)
    counted = [c for c in checks if c["state"] != "skipped"]
    if not counted:
        return {"verdict": NONE, "run": None, "checks": checks}
    if any(c["state"] == RUNNING for c in counted):
        verdict = RUNNING
    elif any(c["state"] == FAILED for c in counted):
        verdict = FAILED
    else:
        verdict = PASSED
    run_ids = sorted({m.group(1) for c in counted if (m := _RUN_URL.search(c["url"]))})
    run_id = f"{sha[:12]}:{','.join(run_ids)}" if run_ids else sha[:12]
    return {"verdict": verdict, "run": {"id": run_id}, "checks": checks}


def _github_logs(proj, pr_number: int, tail: int) -> dict:
    sha, checks = _github_rollup(proj, pr_number)
    failing = [c for c in checks if c["state"] == FAILED]
    result = []
    for run_id in sorted({m.group(1) for c in failing if (m := _RUN_URL.search(c["url"]))}):
        out = subprocess.run(["gh", "run", "view", run_id, "--log-failed"], cwd=str(proj.repo),
                             capture_output=True, text=True, timeout=120)
        lines = (out.stdout or out.stderr).splitlines()
        result.append({"stage": f"actions run {run_id}", "log_tail": "\n".join(lines[-tail:])})
    for c in failing:
        if not _RUN_URL.search(c["url"]):  # a non-Actions check: we can only point at it
            result.append({"stage": c["name"], "log_tail": f"(external check — see {c['url']})"})
    return {"pr": pr_number, "head": sha[:12], "verdict": FAILED if failing else _github_verdict(
        proj, pr_number)["verdict"], "failing_stages": result}


def _github_trigger(proj, pr_number: int) -> dict:
    _, checks = _github_rollup(proj, pr_number)
    runs = sorted({m.group(1) for c in checks if c["state"] == FAILED and (m := _RUN_URL.search(c["url"]))})
    if not runs:
        return {"triggered": False, "reason": "No failed GitHub Actions runs to re-run (CI runs "
                                              "automatically on every push)."}
    for run_id in runs:
        out = subprocess.run(["gh", "run", "rerun", run_id, "--failed"], cwd=str(proj.repo),
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return {"triggered": False, "reason": f"gh run rerun {run_id} failed: {out.stderr.strip()[:200]}"}
    return {"triggered": True, "reran": runs}


# ----- dispatch -----------------------------------------------------------------------------

def verdict(proj, pr_number: int) -> dict:
    p = provider(proj)
    if p == "jenkins":
        from sprint_manager import jenkins
        return jenkins.verdict(proj, pr_number)
    if p == "github":
        return _github_verdict(proj, pr_number)
    return {"verdict": NONE, "run": None}


def logs(proj, pr_number: int, tail: int = 60) -> dict:
    p = provider(proj)
    if p == "jenkins":
        from sprint_manager import jenkins
        return jenkins.failure_logs(proj, pr_number, tail)
    if p == "github":
        return _github_logs(proj, pr_number, tail)
    return {"pr": pr_number, "verdict": NONE, "reason": "this project has no CI"}


def trigger(proj, pr_number: int) -> dict:
    p = provider(proj)
    if p == "jenkins":
        from sprint_manager import jenkins
        return jenkins.trigger(proj, pr_number)
    if p == "github":
        return _github_trigger(proj, pr_number)
    return {"triggered": False, "reason": "this project has no CI configured"}


def for_ticket(action: str, ticket: str, tail: int = 60) -> dict:
    """Resolve the ticket's project + open PR and run ``action`` (status / logs / trigger)."""
    proj = project_mod.resolve(ticket=ticket)
    number = pr_module.pr_number(ticket)
    if number is None:
        key = "triggered" if action == "trigger" else "verdict"
        return {key: False if action == "trigger" else NONE, "reason": "no open PR for this ticket"}
    if action == "status":
        return {"pr": number, **verdict(proj, number)}
    if action == "logs":
        return logs(proj, number, tail)
    return {"pr": number, **trigger(proj, number)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI status / failure logs for a ticket's PR.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (("status", "running/passed/failed/no-build verdict"),
                        ("logs", "failing checks/stages + trimmed log tails"),
                        ("trigger", "start / re-run CI (manager-only)")):
        cmd = sub.add_parser(name, help=help_)
        cmd.add_argument("--ticket", required=True)
        if name == "logs":
            cmd.add_argument("--tail", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(for_ticket(args.command, args.ticket, getattr(args, "tail", 60)), indent=2))
    except (config.ConfigError, project_mod.ProjectError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
