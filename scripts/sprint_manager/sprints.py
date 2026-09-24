"""List the board's current + upcoming sprints, so the UI can offer them as options.

The active sprint is labelled "current"; the earliest dated future sprint is labelled "next".
Dateless future sprints (e.g. a "Next Sprint Candidates" backlog) come last and are unlabelled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402
from sprint_manager.jira_client import JiraClient, JiraError  # noqa: E402


def list_sprints(project) -> list[dict]:
    """Return ``[{id, name, state, label}]`` ordered: active first, then future by start date."""
    raw = JiraClient(project).get_sprints(config.board_id(project))
    active = [s for s in raw if s.get("state") == "active"]
    future = [s for s in raw if s.get("state") == "future"]
    # Sort future sprints by start date; those without a start date sort to the end.
    future.sort(key=lambda s: (not s.get("startDate"), s.get("startDate") or ""))

    options = [{"id": s["id"], "name": s["name"], "state": "active", "label": "current"} for s in active]
    for index, s in enumerate(future):
        label = "next" if index == 0 and s.get("startDate") else ""
        options.append({"id": s["id"], "name": s["name"], "state": "future", "label": label})
    return options


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(list_sprints(project_mod.resolve(argv[0] if argv else None)), indent=2))
    except (config.ConfigError, JiraError, project_mod.ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
