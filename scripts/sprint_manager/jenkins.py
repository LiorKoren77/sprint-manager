"""Cheap, zero-LLM Jenkins CI status for a PR (the orchestrator's "done yet?" heartbeat).

Everything site-specific comes from the project's ``[ci]`` table (``provider = "jenkins"``)::

    [ci]
    provider = "jenkins"
    url = "http://jenkins.example.com:8080"
    job = "/job/<pipeline>/job/PR-{pr}"     # the multibranch job path for PR number {pr}
    user_env = "JENKINS_USER"               # NAMES of the env vars holding the credentials
    token_env = "JENKINS_API_TOKEN"

Uses Jenkins' CLASSIC REST API (``/job/...``), not Blue Ocean (which many sites have removed —
every ``/blue/rest/...`` path 404s there, and an earlier Blue-Ocean-based version of this module
was silently wrong, always reporting "no build").

Deliberately minimal: it answers only "is the PR's build running, passed, or failed?". The failure
*diagnosis* is NOT done here — the agent does it, with whatever CI-debugging tools the project's
notes name. Keeping the poll LLM-free means the orchestrator can check every cycle without spending
tokens.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402
from sprint_manager import pr as pr_module  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402

# Normalized verdicts the rest of the system reasons about.
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
NONE = "no-build"


def _auth_header(proj) -> str:
    _, user, token = config.jenkins_credentials(proj)
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def _classic_get(proj, path: str, as_json: bool = True):
    """GET a classic Jenkins REST path (relative to the site root); return parsed JSON or text."""
    base, _, _ = config.jenkins_credentials(proj)
    request = urllib.request.Request(f"{base}{path}")
    request.add_header("Authorization", _auth_header(proj))
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode(errors="replace")
    return json.loads(raw) if as_json else raw


def _strip_html(text: str) -> str:
    """wfapi/log's ``text`` field is HTML-wrapped (``<span class="timestamp">`` per line)."""
    return re.sub(r"<[^>]+>", "", text)


def _branch_job_path(proj, pr_number: int) -> str:
    """The job path for the PR, from the profile's ``[ci] job`` template (``{pr}`` = PR number)."""
    template = proj.ci.get("job", "")
    if "{pr}" not in template:
        raise config.ConfigError(f"Project {proj.name!r}: [ci] job must be a path template "
                                 f"containing {{pr}}, e.g. \"/job/<pipeline>/job/PR-{{pr}}\".")
    return template.replace("{pr}", urllib.parse.quote(str(pr_number), safe=""))


def _latest_run(proj, pr_number: int) -> dict | None:
    """Return the most recent build ``{number, result, building, timestamp}`` for ``PR-<n>``, or
    ``None`` if the branch job isn't registered in Jenkins yet (never indexed) or has no builds."""
    query = urllib.parse.urlencode({"tree": "builds[number,result,building,timestamp]{0,1}"})
    try:
        data = _classic_get(proj, f"{_branch_job_path(proj, pr_number)}/api/json?{query}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # branch job not registered yet
        raise
    builds = data.get("builds") or []
    return builds[0] if builds else None


def _crumb_header(proj) -> dict[str, str]:
    """Best-effort CSRF crumb for the POST below — an empty dict if crumb protection is disabled."""
    base, _, _ = config.jenkins_credentials(proj)
    request = urllib.request.Request(f"{base}/crumbIssuer/api/json")
    request.add_header("Authorization", _auth_header(proj))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode())
        return {data["crumbRequestField"]: data["crumb"]}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}  # crumb issuer disabled on this Jenkins instance
        raise


def trigger(proj, pr_number: int) -> dict:
    """Start a fresh Jenkins build for ``PR-<n>`` via the classic REST API's build-trigger endpoint.

    Only meaningful once Jenkins has already indexed the branch as a job (normally discovered via
    the GitHub webhook right after the PR opens) — a 404 here means that hasn't happened yet; the
    manager's "Trigger Jenkins" PR check remains the fallback either way, since it forces a re-scan.
    """
    base, _, _ = config.jenkins_credentials(proj)
    path = f"{_branch_job_path(proj, pr_number)}/build?delay=0sec"
    request = urllib.request.Request(f"{base}{path}", method="POST", data=b"")
    request.add_header("Authorization", _auth_header(proj))
    for key, value in _crumb_header(proj).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            location = response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"triggered": False,
                    "reason": f"Jenkins hasn't indexed PR #{pr_number} as a job yet — this "
                              "usually resolves within a minute or two of the PR opening. If it "
                              "persists, trigger it from Jenkins or the PR's checks instead."}
        raise
    return {"triggered": True, "queue_location": location}


def trigger_for_ticket(ticket: str) -> dict:
    """Resolve the ticket's open PR and start a fresh Jenkins build for it."""
    number = pr_module.pr_number(ticket)
    if number is None:
        return {"triggered": False, "reason": "no open PR for this ticket"}
    return {"pr": number, **trigger(project_mod.resolve(ticket=ticket), number)}


def verdict(proj, pr_number: int) -> dict:
    """Return ``{verdict, state, result, run}`` where verdict is running/passed/failed/no-build.

    ``run``, when present, is ``{"id": <build number>, "result", "building", "timestamp"}`` — the
    key is named ``"id"`` (not Jenkins' own ``"number"``) so existing callers that use it as the
    "is this a new build" watermark keep working unchanged.
    """
    run = _latest_run(proj, pr_number)
    if run is None:
        return {"verdict": NONE, "state": None, "result": None, "run": None}
    building = bool(run.get("building"))
    result = run.get("result")
    if building:
        normalized, state = RUNNING, "RUNNING"
    elif result == "SUCCESS":
        normalized, state = PASSED, "FINISHED"
    else:
        normalized, state = FAILED, "FINISHED"
    return {
        "verdict": normalized, "state": state, "result": result,
        "run": {"id": run.get("number"), "result": result, "building": building,
                "timestamp": run.get("timestamp")},
    }


def status_for_ticket(ticket: str) -> dict:
    """Resolve the ticket's PR number and return its CI verdict (or no-PR)."""
    number = pr_module.pr_number(ticket)
    if number is None:
        return {"verdict": NONE, "reason": "no open PR for this ticket"}
    return {"pr": number, **verdict(project_mod.resolve(ticket=ticket), number)}


def failure_logs(proj, pr_number: int, tail: int = 60) -> dict:
    """Return only the FAILING stages of the latest build, each with a trimmed log tail.

    This is the deterministic fetch+filter half of CI debugging — it turns a multi-megabyte build
    log into the handful of failing stages and their last ``tail`` lines, so the agent reasons over
    signal, not the whole console. Diagnosis stays LLM.

    Three API levels deep (stages -> leaf steps -> log), unlike Blue Ocean's two: a *stage* node's
    own log endpoint always returns empty (``length: 0``) on the classic API — only leaf *step*
    nodes carry log text. Only failing stages are drilled into.
    """
    run = _latest_run(proj, pr_number)
    if run is None:
        return {"pr": pr_number, "verdict": NONE, "reason": "no build registered yet"}
    build_number = run.get("number")
    build_path = f"{_branch_job_path(proj, pr_number)}/{build_number}"

    try:
        described = _classic_get(proj, f"{build_path}/wfapi/describe")
    except urllib.error.HTTPError as exc:
        return {"pr": pr_number, "run": build_number, "error": f"stage fetch HTTP {exc.code}"}

    stages = described.get("stages") or []
    failing = [s for s in stages if s.get("status") in ("FAILED", "UNSTABLE", "ABORTED")]

    result_stages: list[dict] = []
    if failing:
        for stage in failing:
            stage_id = stage.get("id")
            log_lines: list[str] = []
            try:
                steps = _classic_get(proj, f"{build_path}/execution/node/{stage_id}/wfapi/describe")
            except urllib.error.HTTPError:
                steps = {}
            for node in steps.get("stageFlowNodes") or []:
                node_id = node.get("id")
                try:
                    log = _classic_get(proj, f"{build_path}/execution/node/{node_id}/wfapi/log")
                except urllib.error.HTTPError:
                    continue
                text = log.get("text") or ""
                if text:
                    log_lines.extend(_strip_html(text).splitlines())
            result_stages.append({
                "stage": stage.get("name", stage_id),
                "log_tail": "\n".join(log_lines[-tail:]),
            })
    else:
        # No per-stage FAILURE recorded (e.g. a top-level failure) — fall back to the whole-build
        # console log so the agent is never left empty-handed.
        try:
            console = _classic_get(proj, f"{build_path}/consoleText", as_json=False)
        except urllib.error.HTTPError:
            console = ""
        result_stages.append({"stage": "(console log)", "log_tail": "\n".join(console.splitlines()[-tail:])})

    return {"pr": pr_number, "run": build_number, "verdict": verdict(proj, pr_number)["verdict"],
            "failing_stages": result_stages}


def logs_for_ticket(ticket: str, tail: int = 60) -> dict:
    number = pr_module.pr_number(ticket)
    if number is None:
        return {"verdict": NONE, "reason": "no open PR for this ticket"}
    return failure_logs(project_mod.resolve(ticket=ticket), number, tail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Jenkins CI status / failure logs for a ticket's PR.")
    sub = parser.add_subparsers(dest="command")

    status_cmd = sub.add_parser("status", help="Cheap running/passed/failed verdict")
    status_cmd.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")

    logs_cmd = sub.add_parser("logs", help="Failing stages + trimmed log tails")
    logs_cmd.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")
    logs_cmd.add_argument("--tail", type=int, default=60, help="Log lines per failing stage (default 60)")

    trigger_cmd = sub.add_parser("trigger", help="Start a fresh CI build for the ticket's open PR")
    trigger_cmd.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")

    # Back-compat: bare "--ticket" (no subcommand) still returns the status verdict.
    parser.add_argument("--ticket", dest="legacy_ticket", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "logs":
            print(json.dumps(logs_for_ticket(args.ticket, args.tail), indent=2))
        elif args.command == "trigger":
            print(json.dumps(trigger_for_ticket(args.ticket), indent=2))
        else:
            ticket = getattr(args, "ticket", None) or args.legacy_ticket
            if not ticket:
                parser.error("--ticket is required")
            print(json.dumps(status_for_ticket(ticket), indent=2))
    except (config.ConfigError, project_mod.ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
