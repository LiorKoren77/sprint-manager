"""Per-ticket UI transcript persistence: ``state/<TICKET>.transcript.jsonl``.

The orchestrator keeps a live in-memory buffer of UI events, but that buffer is lost when the
server restarts (and is capped at ``TRANSCRIPT_LIMIT``). This module mirrors every broadcast event
to an append-only JSONL file so the dashboard can re-render a ticket's full conversation whenever
its tab is (re)opened — including after a restart. It accumulates across stages on purpose: the
notes file is the compacted handoff for the *agent*, while this is the human-readable scrollback.
"""

from __future__ import annotations

import json
from pathlib import Path

from sprint_manager import config


def transcript_path(ticket: str) -> Path:
    return config.STATE_DIR / f"{ticket}.transcript.jsonl"


def append(ticket: str, event: dict) -> None:
    """Append one ``{"kind", "text"}`` event as a JSON line (creating the file/dir)."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with transcript_path(ticket).open("a") as f:
        f.write(json.dumps(event) + "\n")


def delete(ticket: str) -> None:
    """Remove a ticket's persisted transcript. No-op if absent."""
    transcript_path(ticket).unlink(missing_ok=True)


def read(ticket: str) -> list[dict]:
    """Return the ticket's full persisted event history (empty list if none yet)."""
    path = transcript_path(ticket)
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
