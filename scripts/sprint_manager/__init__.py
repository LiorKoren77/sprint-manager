"""sprint-manager: drive one Claude agent per sprint ticket through a defined lifecycle.

The package is split into two layers:

* The **zero-LLM CLI layer** (``config``, ``models``, ``state``, ``report_stage``,
  ``jira_client``, ``fetch_sprint``, ``branch``, ``worktree``, ``pr``, ``jenkins``) does the
  deterministic "fast actions" — fetching the sprint, making branches/worktrees, opening PRs,
  reading CI status, recording each ticket's status. It depends only on the standard library so
  it is runnable and testable without installing anything.

* The **orchestration layer** (``agent``, ``orchestrator``, ``server`` — added in later phases)
  drives the per-ticket Claude Agent SDK sessions and serves the browser UI.
"""

__version__ = "0.1.0"
