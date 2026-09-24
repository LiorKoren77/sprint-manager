"""CLI the per-ticket agent calls after every state transition to report where it is.

This is the agent's only status channel. Example (run by the agent via Bash):

    python -m sprint_manager.report_stage --ticket ABC-1234 \
        --stage plan --activity working --note "drafting the approach"

Set ``--activity waiting_user`` (and then stop) when the agent needs a human decision; set
``--activity waiting_external`` after opening the PR / triggering CI.
"""

from __future__ import annotations

import argparse
import sys

# Allow ``python sprint_manager/report_stage.py`` (direct file) as well as ``-m`` execution.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from sprint_manager import state  # noqa: E402
from sprint_manager.models import STAGE_ALIASES, Activity, Stage  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report a ticket's current stage and activity.")
    parser.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")
    # Deprecated aliases (STAGE_ALIASES) are accepted so sessions started before a rename (whose
    # prompts bake in the old name) keep reporting; Stage._missing_ resolves them.
    parser.add_argument("--stage", choices=[s.value for s in Stage] + list(STAGE_ALIASES),
                        help="Pipeline stage")
    parser.add_argument("--activity", choices=[a.value for a in Activity], help="Agent liveness")
    parser.add_argument("--note", default=None, help="One-line human-readable note")
    parser.add_argument("--branch", default=None, help="Working branch (optional)")
    parser.add_argument("--pr-url", dest="pr_url", default=None, help="PR URL (optional)")
    parser.add_argument("--ci-status", dest="ci_status", default=None, help="CI status (optional)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    status = state.update(
        args.ticket,
        stage=Stage(args.stage) if args.stage else None,
        activity=Activity(args.activity) if args.activity else None,
        note=args.note,
        branch=args.branch,
        pr_url=args.pr_url,
        ci_status=args.ci_status,
    )
    print(f"{status.ticket}: stage={status.stage.value} activity={status.activity.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
