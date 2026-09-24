"""Run a test command and distil its output to compact JSON — so the testing-stage agent reasons
over ~20 lines of verdict, not ~2000 lines of raw suite output (a major token sink).

The exact suite commands live in the project's test-commands file (its profile's
``[prompts] test_commands``) or are discovered and confirmed with the manager; this is a generic
wrapper. The agent picks the documented command and runs::

    python -m sprint_manager.run_tests --cmd "<the documented command>" [--cwd DIR] [--tail 80]

It streams nothing, captures combined stdout+stderr to a log file, parses pass/fail counts for common
frameworks (pytest, Maven Surefire/JUnit, Cypress/Mocha), and prints::

    {command, cwd, exit_code, passed, failed, errors, skipped, framework,
     failure_lines: [...capped...], log_path}

The full log stays on disk (``log_path``) for the agent to open ONLY if the JSON is insufficient.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402  (STATE_DIR for logs)

# Count extractors per framework: (name, regex, group-map). First that matches "wins" the counts,
# but we still merge any counts other extractors find (a run may print more than one summary).
_PYTEST = re.compile(
    r"(?:(?P<failed>\d+) failed[,\s]*)?(?P<passed>\d+) passed"
    r"(?:[,\s]*(?P<skipped>\d+) skipped)?(?:[,\s]*(?P<errors>\d+) error)?",
)
_MAVEN = re.compile(
    r"Tests run:\s*(?P<total>\d+),\s*Failures:\s*(?P<failed>\d+),\s*Errors:\s*(?P<errors>\d+)"
    r"(?:,\s*Skipped:\s*(?P<skipped>\d+))?",
)
# Mocha/Cypress print the count BEFORE the word: "42 passing (3s)", "2 failing".
_CYPRESS_PASS = re.compile(r"(?P<passed>\d+)\s+passing", re.IGNORECASE)
_CYPRESS_FAIL = re.compile(r"(?P<failed>\d+)\s+failing", re.IGNORECASE)

# Lines worth surfacing to the agent as probable failure signal.
_FAILURE_LINE = re.compile(
    r"\b(FAIL(ED)?|ERROR|Exception|AssertionError|Traceback|✖|BUILD FAILURE|"
    r"Expected|but was|not ok)\b",
)


def _parse(text: str) -> dict:
    """Extract counts + a framework label from combined test output."""
    counts = {"passed": None, "failed": None, "errors": None, "skipped": None}
    framework = None

    m = _MAVEN.search(text)
    if m:
        framework = "maven"
        total = int(m.group("total"))
        failed = int(m.group("failed"))
        errors = int(m.group("errors"))
        skipped = int(m.group("skipped") or 0)
        counts.update(passed=total - failed - errors - skipped, failed=failed,
                      errors=errors, skipped=skipped)

    m = _PYTEST.search(text)
    if m and framework is None:
        framework = "pytest"
        counts.update(
            passed=int(m.group("passed")) if m.group("passed") else 0,
            failed=int(m.group("failed") or 0),
            skipped=int(m.group("skipped") or 0),
            errors=int(m.group("errors") or 0),
        )

    if framework is None:
        mp, mf = _CYPRESS_PASS.search(text), _CYPRESS_FAIL.search(text)
        if mp or mf:
            framework = "cypress"
            counts.update(passed=int(mp.group("passed")) if mp else 0,
                          failed=int(mf.group("failed")) if mf else 0)

    counts["framework"] = framework
    return counts


def _failure_lines(text: str, tail: int) -> list[str]:
    """Return up to ``tail`` lines that look like failure signal, de-noised and capped."""
    hits = [ln.rstrip() for ln in text.splitlines() if _FAILURE_LINE.search(ln)]
    # Keep the LAST `tail` (the final failures/summary are the most actionable).
    return hits[-tail:]


def run(cmd: str, cwd: str | None, tail: int) -> dict:
    """Run ``cmd`` in a shell, capture output to a log file, and return the parsed verdict."""
    workdir = cwd or os.getcwd()  # the agent's cwd is its checkout
    proc = subprocess.run(cmd, shell=True, cwd=workdir, text=True,
                          capture_output=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")

    log_dir = config.STATE_DIR / "test-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile("w", dir=str(log_dir), prefix="testrun-",
                                     suffix=".log", delete=False)
    with fd:
        fd.write(f"$ {cmd}\n(cwd={workdir}, exit={proc.returncode})\n\n{output}")

    parsed = _parse(output)
    return {
        "command": cmd,
        "cwd": workdir,
        "exit_code": proc.returncode,
        "framework": parsed.pop("framework"),
        **parsed,
        "failure_lines": _failure_lines(output, tail) if proc.returncode != 0 else [],
        "log_path": fd.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a test command and emit a compact JSON verdict.")
    parser.add_argument("--cmd", required=True, help="The exact test command")
    parser.add_argument("--cwd", default=None, help="Working directory (default: the current directory)")
    parser.add_argument("--tail", type=int, default=80, help="Max failure lines to include (default 80)")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.cmd, args.cwd, args.tail), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
