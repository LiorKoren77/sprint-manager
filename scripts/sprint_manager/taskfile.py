"""The task's problem statement on disk: ``state/<TICKET>.task.md``.

For a **text** task this file IS the problem statement (what you typed; editable later from the
panel — "clarify the problem" without making a new task). For tracker-backed tasks (GitHub issue,
Slack thread) it is a cached rendering of the tracker content, refreshed whenever a fresh session
loads the task, so an offline restart still has something to seed the agent with.
"""

from __future__ import annotations

from pathlib import Path

from sprint_manager import config


def path(ticket: str) -> Path:
    return config.STATE_DIR / f"{ticket}.task.md"


def read(ticket: str) -> str:
    p = path(ticket)
    return p.read_text() if p.exists() else ""


def write(ticket: str, text: str) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path(ticket).write_text(text.rstrip() + "\n")


def delete(ticket: str) -> None:
    path(ticket).unlink(missing_ok=True)
