"""The per-ticket durable memory: ``state/<TICKET>.notes.md``.

This is the compaction handoff. Each session starts fresh, so the notes file (plus the code on
disk) is what carries context forward — the recap, your guidance, the approved plan, each finished
session's summary, and each pr-open triage episode. The file itself is append-only (durable,
debuggable); what a new session is seeded with is a per-stage ``view`` of it, so a long-lived
ticket's history doesn't ride in every prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

from sprint_manager import config
from sprint_manager.models import STAGE_ALIASES, Stage

# How many pr-open triage episodes a pr-open session sees (older ones are superseded by later fixes).
PR_OPEN_EPISODES_IN_VIEW = 3

# A section heading the orchestrator writes: "## <stage> — <kind>". Only headings whose first word
# is a stage name (current or pre-merge) count, so a "## ..." inside an agent's own summary body is
# never mistaken for a section boundary.
_STAGE_NAMES = {s.value for s in Stage} | set(STAGE_ALIASES)
_HEADING = re.compile(r"^## (\S+) — (.*)$")


def notes_path(ticket: str) -> Path:
    return config.STATE_DIR / f"{ticket}.notes.md"


def read(ticket: str) -> str:
    """Return the ticket's accumulated notes (empty string if none yet)."""
    path = notes_path(ticket)
    return path.read_text() if path.exists() else ""


def delete(ticket: str) -> None:
    """Remove a ticket's notes file. No-op if absent."""
    notes_path(ticket).unlink(missing_ok=True)


def append_section(ticket: str, title: str, body: str) -> None:
    """Append a ``## <title>`` section with ``body`` to the ticket's notes (creating the file)."""
    if not body.strip():
        return
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = notes_path(ticket)
    if not path.exists():
        path.write_text(f"# {ticket} — working notes\n\n")
    with path.open("a") as f:
        f.write(f"## {title}\n\n{body.strip()}\n\n")


def _split(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split notes into (preamble, [(raw_stage, kind, full_section_text), ...]) in file order."""
    preamble: list[str] = []
    sections: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines(keepends=True):
        m = _HEADING.match(line.rstrip("\n"))
        if m and m.group(1) in _STAGE_NAMES:
            sections.append((m.group(1), m.group(2), [line]))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)
    return "".join(preamble), [(st, kind, "".join(body)) for st, kind, body in sections]


def filter_view(text: str, stage: Stage, full_path: str = "") -> str:
    """Pure core of ``view``: the subset of ``text`` a fresh ``stage`` session is seeded with.

    Two rules, both dropping only material a later section already supersedes:

    * **Latest recap per stage name wins.** Every non-pr-open section is written from a recap that
      is required to restate the FULL state of that stage (``_RECAP_PROMPT``: summary, compact,
      pre-jump summary), so a later section with the same stage name supersedes earlier ones — e.g.
      the second ``plan — summary`` after a loop back already contains the first.
    * **pr-open episodes are events, not recaps**, so none supersedes another — but a pr-open
      session sees only the latest ``PR_OPEN_EPISODES_IN_VIEW`` of them (older ones describe CI
      runs/reviews that later fixes already addressed). explore / work (a jump back) see all of
      them: a re-plan needs the full review history.
    """
    preamble, sections = _split(text)
    keep = [True] * len(sections)
    latest: dict[str, int] = {}
    for i, (raw, _, _) in enumerate(sections):
        if Stage(raw) == Stage.PR_OPEN:
            continue
        if raw in latest:
            keep[latest[raw]] = False
        latest[raw] = i
    if stage == Stage.PR_OPEN:
        pr_idx = [i for i, (raw, _, _) in enumerate(sections) if Stage(raw) == Stage.PR_OPEN]
        for i in pr_idx[:-PR_OPEN_EPISODES_IN_VIEW]:
            keep[i] = False
    omitted = keep.count(False)
    body = "".join(sec for (_, _, sec), k in zip(sections, keep) if k)
    if omitted:
        where = f" — full history in {full_path}" if full_path else ""
        body = f"_({omitted} superseded section(s) omitted{where})_\n\n" + body
    return preamble + body


def view(ticket: str, stage: Stage) -> str:
    """The notes a fresh ``stage`` session for ``ticket`` is seeded with (see ``filter_view``)."""
    return filter_view(read(ticket), stage, str(notes_path(ticket)))


def latest_heading_block(ticket: str, heading: str) -> str:
    """The body under the LAST ``### <heading>`` in the ticket's notes (up to the next ``#``-heading),
    or "". Used to carry the approved acceptance criteria out of the explore summary."""
    text = read(ticket)
    matches = list(re.finditer(rf"^#{{2,4}} {re.escape(heading)}\s*$", text, re.MULTILINE | re.IGNORECASE))
    if not matches:
        return ""
    rest = text[matches[-1].end():]
    end = re.search(r"^#{1,4} ", rest, re.MULTILINE)
    return (rest[: end.start()] if end else rest).strip()
