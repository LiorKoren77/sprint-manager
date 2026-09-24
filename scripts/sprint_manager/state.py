"""The status store: one JSON file per ticket under ``STATE_DIR``.

This is the single source of truth the UI renders and the agents update. Writes are atomic
(write to a temp file, then ``os.replace``) so a reader never sees a half-written file even if an
agent and the dashboard touch it at the same time.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sprint_manager import config
from sprint_manager.models import TicketStatus


def _path_for(ticket: str) -> Path:
    return config.STATE_DIR / f"{ticket}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(ticket: str) -> TicketStatus | None:
    """Return the stored status for a ticket, or ``None`` if it has none yet."""
    path = _path_for(ticket)
    if not path.exists():
        return None
    return TicketStatus.from_dict(json.loads(path.read_text()))


def write(status: TicketStatus) -> None:
    """Persist a status record atomically, stamping ``updated_at``."""
    if not status.ticket.strip():  # never create a state file for a blank ticket key
        return
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    status.updated_at = _now_iso()
    path = _path_for(status.ticket)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status.to_dict(), indent=2))
    os.replace(tmp, path)  # atomic on POSIX


def update(ticket: str, **fields) -> TicketStatus:
    """Merge ``fields`` into a ticket's status (creating it if absent) and persist.

    Unknown keys are ignored. This is what ``report_stage.py`` and the orchestrator call.
    """
    status = read(ticket) or TicketStatus(ticket=ticket)
    for key, value in fields.items():
        if value is not None and hasattr(status, key):
            setattr(status, key, value)
    write(status)
    return status


def delete(ticket: str) -> None:
    """Remove a ticket's status file (and any stray temp file). No-op if absent."""
    path = _path_for(ticket)
    path.unlink(missing_ok=True)
    path.with_suffix(".json.tmp").unlink(missing_ok=True)


def all_statuses() -> list[TicketStatus]:
    """Return every stored ticket status, sorted by ticket key.

    Skips ``RUNTIME_CONFIG_FILE`` by name — the one non-ticket ``*.json`` file that deliberately
    lives in this same directory (settings-UI model overrides) — and tolerates any other file that
    fails to parse as a ``TicketStatus`` (corrupt write, stray file) rather than letting one bad
    file take down every caller of this function, chiefly ``/api/status``.
    """
    if not config.STATE_DIR.exists():
        return []
    statuses = []
    for p in config.STATE_DIR.glob("*.json"):
        if p == config.RUNTIME_CONFIG_FILE:
            continue
        try:
            statuses.append(TicketStatus.from_dict(json.loads(p.read_text())))
        except Exception:  # noqa: BLE001 - one unreadable file must never break the whole listing
            continue
    statuses = [s for s in statuses if s.ticket.strip()]  # ignore any blank-ticket leftovers
    return sorted(statuses, key=lambda s: s.ticket)
