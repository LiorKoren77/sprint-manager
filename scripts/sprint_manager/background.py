"""Launch a long-running shell command (a slow compile/build) in the background and let the
orchestrator poll it — for work that can outlast a single turn's patience.

This app is turn-based: an agent cannot "continue when a background job completes" on its own —
nothing re-invokes it. The correct pattern is exactly the one pr-open already uses for CI: start
the job, report ``waiting_external``, and STOP; a cheap local poll (no external API, so it can run
every ~20s — see ``config.BG_JOB_POLL_SECONDS``) flips the ticket to ``waiting_user`` and notifies
the manager the moment it finishes. No agent turn is spent waiting.

    python -m sprint_manager.background start --ticket ABC-1234 --cmd "make build" --cwd DIR
    python -m sprint_manager.background status --ticket ABC-1234

Only one active job is tracked per ticket — starting a new one while one is still running just
replaces the pointer (the old process, if still alive, is left to finish or die on its own; the
agent shouldn't have two builds in flight for the same ticket at once).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402

_JOBS_DIR = config.STATE_DIR / "bg-jobs"


def _job_file(ticket: str) -> Path:
    return _JOBS_DIR / f"{ticket}.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start(ticket: str, cmd: str, cwd: str | None) -> dict:
    """Launch ``cmd`` fully detached (survives this process exiting), capturing combined
    stdout+stderr to a log file and its exit code to a companion file once it finishes."""
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    workdir = cwd or os.getcwd()  # the agent's cwd is its checkout
    stamp = int(time.time())
    log_path = _JOBS_DIR / f"{ticket}-{stamp}.log"
    exit_path = log_path.with_suffix(".exit")
    # Wrap so the exit code survives even though we never wait() on this process ourselves.
    wrapper = f"({cmd}) > {log_path} 2>&1; echo $? > {exit_path}"
    proc = subprocess.Popen(
        ["sh", "-c", wrapper], cwd=workdir, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    record = {
        "ticket": ticket, "cmd": cmd, "cwd": workdir, "pid": proc.pid,
        "log_path": str(log_path), "exit_path": str(exit_path), "started_at": stamp,
    }
    _job_file(ticket).write_text(json.dumps(record, indent=2))
    return {"job_started": True, "pid": proc.pid, "log_path": str(log_path)}


def status(ticket: str, tail: int = 40) -> dict:
    """``{"active": False}`` if no job is tracked for this ticket; otherwise ``{"active": True,
    "done": bool, "exit_code": int|None, "log_path": str, "log_tail": [...]}`` (the last two only
    once ``done``). ``exit_code`` is ``None`` if the process died without writing its exit marker
    (killed, crashed, or the machine restarted) — still reported as done so polling doesn't hang
    forever waiting for a process that no longer exists.
    """
    path = _job_file(ticket)
    if not path.exists():
        return {"active": False}
    record = json.loads(path.read_text())
    exit_path = Path(record["exit_path"])
    log_path = Path(record["log_path"])
    if exit_path.exists():
        raw = exit_path.read_text().strip()
        exit_code = int(raw) if raw.lstrip("-").isdigit() else None
    elif not _pid_alive(record["pid"]):
        exit_code = None
    else:
        return {"active": True, "done": False}
    log_tail = log_path.read_text().splitlines()[-tail:] if log_path.exists() else []
    return {"active": True, "done": True, "exit_code": exit_code,
            "log_path": str(log_path), "log_tail": log_tail}


def clear(ticket: str) -> None:
    """Forget a finished job — called once the orchestrator has notified the manager, so the same
    completion doesn't get reported twice."""
    _job_file(ticket).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch/poll a background shell command for a ticket.")
    sub = parser.add_subparsers(dest="command", required=True)

    st = sub.add_parser("start", help="Launch a command in the background; report waiting_external next")
    st.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")
    st.add_argument("--cmd", required=True, help="The shell command to run")
    st.add_argument("--cwd", default=None, help="Working directory (default: the current directory)")

    sc = sub.add_parser("status", help="Check whether the ticket's background job has finished")
    sc.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")

    args = parser.parse_args(argv)
    if args.command == "start":
        print(json.dumps(start(args.ticket, args.cmd, args.cwd), indent=2))
    else:
        print(json.dumps(status(args.ticket), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
