"""GitHub-issue source: an issue in (usually) the project's own repo, read and updated with ``gh``.

* ``external_ref`` = the issue number; ``external_url`` = its URL. All ``gh`` calls use the URL
  when known (so an issue in another repo works too), run from the project's main checkout.
* The PR body gets ``Fixes <owner>/<repo>#<n>`` — GitHub then links the PR in the issue's timeline
  and closes the issue when the PR merges **into the default branch** (``on_done`` checks that).
* Profile options (``[github]``): ``assign_self`` (self-assign when work starts), and
  ``in_progress_label`` (a label to add then; "" = none).
* New issue comments fire the pr-open review channel — folded into the PR's own GraphQL poll
  (``linked_issue``), so it costs no extra call.
"""

from __future__ import annotations

import json
import re
import subprocess

from sprint_manager.sources import Source, SourceError, register

_ISSUE_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)")
_ISSUE_NUM = re.compile(r"^\s*#?(\d+)\s*$")


def parse_issue_ref(text: str) -> tuple[str, str, int] | None:
    """``(owner, repo, number)`` from an issue URL, ``("", "", number)`` from ``#N``/``N``, or None."""
    m = _ISSUE_URL.search(text or "")
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = _ISSUE_NUM.match(text or "")
    return ("", "", int(m.group(1))) if m else None


def _gh(proj, *args: str) -> str:
    try:
        out = subprocess.run(["gh", *args], cwd=str(proj.repo), capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError(f"gh failed: {exc}") from exc
    if out.returncode != 0:
        raise SourceError(f"gh {' '.join(args[:3])} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


def _target(status) -> str:
    return status.external_url or status.external_ref


class GitHubSource(Source):
    name = "github"

    def fetch(self, proj, ref: str) -> dict:
        """Raw issue JSON for a URL or number (used by load and by intake)."""
        return json.loads(_gh(proj, "issue", "view", ref, "--json",
                              "number,title,body,labels,comments,state,url"))

    @staticmethod
    def to_meta(ticket: str, issue: dict) -> dict:
        labels = [lab.get("name", "").lower() for lab in issue.get("labels") or []]
        return {
            "key": ticket,
            "type": "Bug" if "bug" in labels else "Feature",
            "status": (issue.get("state") or "").lower(),
            "summary": issue.get("title", ""),
            "url": issue.get("url", ""),
            "description": issue.get("body") or "(no description)",
            "comments": [
                {"author": (c.get("author") or {}).get("login", "?"),
                 "created": c.get("createdAt", ""), "body": c.get("body", "")}
                for c in (issue.get("comments") or [])[-5:]
            ],
        }

    def load(self, status, proj) -> dict:
        return self.to_meta(status.ticket, self.fetch(proj, _target(status)))

    def reference(self, status) -> str:
        return f"#{status.external_ref}" if status.external_ref else ""

    def pr_body_footer(self, status) -> str:
        parsed = parse_issue_ref(status.external_url or "")
        if parsed and parsed[0]:
            return f"Fixes {parsed[0]}/{parsed[1]}#{parsed[2]}"
        return f"Fixes #{status.external_ref}" if status.external_ref else ""

    def linked_issue(self, status) -> tuple[str, str, int] | None:
        """``(owner, repo, number)`` for the PR poll to also count this issue's comments."""
        parsed = parse_issue_ref(status.external_url or "")
        return parsed if parsed and parsed[0] else None

    def on_work_start(self, status, proj) -> str | None:
        args, done = [], []
        if proj.github.get("assign_self"):
            args += ["--add-assignee", "@me"]
            done.append("assigned to you")
        label = proj.github.get("in_progress_label", "")
        if label:
            args += ["--add-label", label]
            done.append(f"labelled {label!r}")
        if not args:
            return None
        _gh(proj, "issue", "edit", _target(status), *args)
        return f"GitHub issue #{status.external_ref}: " + ", ".join(done)

    def on_shipped(self, status, proj, pr_url: str, new_pr: bool) -> str | None:
        if not new_pr:
            return None
        return (f"PR linked to issue #{status.external_ref} ({self.pr_body_footer(status)} in the "
                f"body) — GitHub closes the issue when the PR merges into the default branch.")

    def on_done(self, status, proj) -> str | None:
        issue_state = self.fetch(proj, _target(status)).get("state", "")
        if issue_state.upper() == "CLOSED":
            return f"GitHub issue #{status.external_ref} is closed."
        return (f"GitHub issue #{status.external_ref} is still open — it auto-closes only when the "
                f"PR merges into the default branch. Close it on GitHub if the work is done.")


register(GitHubSource())
