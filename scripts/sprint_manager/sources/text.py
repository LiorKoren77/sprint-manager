"""Text source: a problem statement typed (or pasted) into the dashboard. No tracker, no side
effects; the text lives in ``state/<id>.task.md`` (taskfile) and can be edited from the panel."""

from __future__ import annotations

from sprint_manager import taskfile
from sprint_manager.sources import Source, register


class TextSource(Source):
    name = "text"

    def load(self, status, proj) -> dict:
        return {
            "key": status.ticket,
            "type": "Bug" if status.branch_kind == "bug" else "Feature",
            "status": "",
            "summary": status.summary,
            "url": "",
            "description": taskfile.read(status.ticket),
            "comments": [],
        }


register(TextSource())
