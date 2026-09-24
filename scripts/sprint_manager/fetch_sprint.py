"""Fetch the tickets assigned to you in a named sprint -> compact JSON.

This is the reusable sprint fetch. It is MCP-free (plain Jira REST via ``jira_client``), so it
works headless. Example:

    python -m sprint_manager.fetch_sprint --sprint "Sprint 42" --assignee me [--project NAME]
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402
from sprint_manager.jira_client import JiraClient, JiraError  # noqa: E402


def adf_to_text(node: object) -> str:
    """Flatten an Atlassian Document Format value (dict/list) into plain text.

    Jira Cloud v3 returns rich fields (description, bug/feature fields) as ADF documents. We only
    need their text for the agent's context, so we recursively collect ``text`` leaves and insert
    newlines around block nodes.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(child) for child in node)
    if isinstance(node, dict):
        text = node.get("text", "")
        inner = adf_to_text(node.get("content", []))
        block_types = {"paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock"}
        suffix = "\n" if node.get("type") in block_types else ""
        return f"{text}{inner}{suffix}"
    return ""


def _description_for(fields: dict, project) -> str:
    """Pick the description source by issue type: the profile's ``[jira] description_fields`` entry
    for that type when it's filled in, else the standard ``description`` field."""
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    custom = (project.jira.get("description_fields") or {}).get(issue_type)
    if custom and fields.get(custom):
        return adf_to_text(fields[custom])
    return adf_to_text(fields.get("description"))


def _recent_comments(fields: dict, limit: int = 5) -> list[dict]:
    """Return the most recent comments as ``{author, created, body}`` dicts."""
    comments = (fields.get("comment") or {}).get("comments", [])
    recent = comments[-limit:]
    return [
        {
            "author": (c.get("author") or {}).get("displayName", "?"),
            "created": c.get("created", ""),
            "body": adf_to_text(c.get("body")),
        }
        for c in recent
    ]


def simplify_issue(issue: dict, project) -> dict:
    """Reduce a raw Jira issue to the compact shape the UI and agents consume."""
    key = issue.get("key", "")
    fields = issue.get("fields", {})
    return {
        "key": key,
        "type": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "summary": fields.get("summary", ""),
        "url": config.browse_url(project, key),
        "description": _description_for(fields, project),
        "comments": _recent_comments(fields),
    }


def fetch_sprint(project, sprint: str, assignee: str = "me") -> list[dict]:
    """Return the simplified issues in ``sprint`` assigned to ``assignee`` ("me" = current user)."""
    who = "currentUser()" if assignee == "me" else f'"{assignee}"'
    jql = f'sprint = "{sprint}" AND assignee = {who} ORDER BY status, priority DESC'
    issues = JiraClient(project).search(jql)
    return [simplify_issue(issue, project) for issue in issues]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch your tickets in a sprint as JSON.")
    parser.add_argument("--sprint", required=True, help='Sprint name, e.g. "Sprint 42"')
    parser.add_argument("--project", default=None, help="Project name (default: the default project)")
    parser.add_argument("--assignee", default="me", help='"me" (default) or a Jira display name')
    args = parser.parse_args(argv)
    try:
        proj = project_mod.resolve(args.project)
        print(json.dumps(fetch_sprint(proj, args.sprint, args.assignee), indent=2))
    except (config.ConfigError, JiraError, project_mod.ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
