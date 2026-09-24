"""Task sources: where a task's problem statement comes from, and what happens there as it moves.

A task's ``tracker`` (``TicketStatus.tracker``) names its source. Every source implements the same
small interface (``Source``); the orchestrator calls it at the same lifecycle points for every task:

* ``load``            — the task's content, as the ``meta`` dict the prompts and UI use
                        (key, type, summary, url, description, comments, status)
* ``reference``       — the tag commits and the PR title carry ("ABC-12" / "#412" / "")
* ``pr_body_footer``  — text appended to the PR body at Ship ("Fixes #412", a thread link, "")
* ``on_work_start`` / ``on_shipped`` / ``on_done`` — best-effort tracker side effects; each returns a
                        one-line report for the transcript, or None when there is nothing to say
* ``feedback_count``  — tracker-side comment count, folded into the pr-open review watermark
                        (None = this source has no such signal)

Side effects are never load-bearing: the orchestrator reports a failing hook and carries on.
Zero-LLM, stdlib only (the CLI layer's rule).
"""

from __future__ import annotations


class SourceError(RuntimeError):
    """A source could not load or update its task (network, auth, not found) — with a hint."""


class Source:
    """The interface every tracker implements. Defaults describe a tracker with no side effects."""

    name = ""

    def load(self, status, proj) -> dict:
        raise NotImplementedError

    def reference(self, status) -> str:
        return ""

    def pr_body_footer(self, status) -> str:
        return ""

    def on_work_start(self, status, proj) -> str | None:
        return None

    def on_shipped(self, status, proj, pr_url: str, new_pr: bool) -> str | None:
        return None

    def on_done(self, status, proj) -> str | None:
        return None

    def feedback_count(self, status, proj) -> int | None:
        return None


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.name] = source
    return source


def get(name: str) -> Source:
    """The source for a tracker name. Tickets persisted before trackers existed were all Jira."""
    _load_builtin()
    try:
        return _REGISTRY[name or "jira"]
    except KeyError:
        raise SourceError(f"Unknown tracker {name!r}; known: {sorted(_REGISTRY)}") from None


def names() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


_builtin_loaded = False


def _load_builtin() -> None:
    """Import (and so register) every built-in source once. Keyed on a flag, NOT on the registry
    being non-empty: importing one source module directly (``from sprint_manager.sources.github
    import …``) registers just that one, and must not stop the others from loading."""
    global _builtin_loaded
    if _builtin_loaded:
        return
    _builtin_loaded = True
    from sprint_manager.sources import github, jira, slack, text  # noqa: F401  (they register)


# Id tags per tracker for generated task ids: "<project>-<tag>-<n>". Jira tasks keep their key.
ID_TAGS = {"text": "t", "github": "gh", "slack": "slack"}


def new_task_id(project: str, tracker: str, number: int | None = None) -> str:
    """A fresh id for a non-Jira task: ``<project>-<tag>-<n>`` — safe as a file name, branch
    component and env var, unique across projects. GitHub issues use their issue number (so the
    id is stable if the issue is added twice); text/Slack tasks take the next free counter value."""
    from sprint_manager import state  # local: avoid an import cycle at package import

    prefix = f"{project}-{ID_TAGS[tracker]}-"
    if number is not None:
        return f"{prefix}{number}"
    used = [int(s.ticket[len(prefix):]) for s in state.all_statuses()
            if s.ticket.startswith(prefix) and s.ticket[len(prefix):].isdigit()]
    return f"{prefix}{max(used, default=0) + 1}"
