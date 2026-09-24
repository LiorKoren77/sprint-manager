"""Jira source: a Jira issue in the project's ``[jira]`` site (see project.py / jira_client.py).

Side effects mirror the lifecycle: → "In Progress" when work starts, → "In Review" + a PR-link
comment when the PR first opens (a re-ship to the same PR doesn't re-comment), → "Done" on the final
approve. All best-effort: a missing transition is reported, never raised.
"""

from __future__ import annotations

from sprint_manager import config
from sprint_manager.fetch_sprint import simplify_issue
from sprint_manager.jira_client import JiraClient, JiraError
from sprint_manager.sources import Source, SourceError, register


class JiraSource(Source):
    name = "jira"

    def load(self, status, proj) -> dict:
        try:
            return simplify_issue(JiraClient(proj).get_issue(status.ticket), proj)
        except (config.ConfigError, JiraError) as exc:
            raise SourceError(str(exc)) from exc

    def reference(self, status) -> str:
        return status.ticket

    def _transition(self, status, proj, target: str) -> str:
        try:
            ok = JiraClient(proj).transition_issue(status.ticket, target)
        except (config.ConfigError, JiraError) as exc:
            raise SourceError(str(exc)) from exc
        return f"Jira → {target}" if ok else f"Jira: no '{target}' transition available"

    def on_work_start(self, status, proj) -> str | None:
        return self._transition(status, proj, "In Progress")

    def on_shipped(self, status, proj, pr_url: str, new_pr: bool) -> str | None:
        report = self._transition(status, proj, "In Review")
        if new_pr:
            try:
                JiraClient(proj).add_comment(status.ticket, f"PR opened: {pr_url}")
            except (config.ConfigError, JiraError) as exc:
                raise SourceError(str(exc)) from exc
        return report

    def on_done(self, status, proj) -> str | None:
        return self._transition(status, proj, "Done")


register(JiraSource())
