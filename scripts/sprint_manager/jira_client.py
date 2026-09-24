"""A thin Jira Cloud REST client (standard library only).

HTTP Basic with a personal API token, NOT an Atlassian MCP server (interactively authenticated,
unavailable to a headless service). The site and credential env-var names come from the project's
``[jira]`` table. Many Jira sites keep the real description of some issue types in custom fields;
map them per issue type with ``description_fields``::

    [jira]
    url = "https://example.atlassian.net"
    description_fields = { Bug = "customfield_10001", Story = "customfield_10002" }
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from sprint_manager import config

# A Jira issue key like ``ABC-12345``: an uppercase project key, a dash, and digits.
_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def parse_issue_key(text: str) -> str | None:
    """Extract an issue key from a browse URL or a bare key. Returns ``None`` if none is found.

    Accepts e.g. ``https://example.atlassian.net/browse/ABC-123?foo=bar``, ``ABC-123``, or
    ``abc-123`` (case-insensitively). The first key-shaped token wins.
    """
    if not text:
        return None
    match = _ISSUE_KEY_RE.search(text.strip().upper())
    return match.group(0) if match else None

# Fields we fetch for every issue, plus the project's per-type description fields (see above).
BASE_FIELDS = ["summary", "issuetype", "status", "priority", "description", "comment"]


def issue_fields(project) -> list[str]:
    return BASE_FIELDS + sorted(set((project.jira.get("description_fields") or {}).values()))


class JiraError(RuntimeError):
    """Raised when a Jira request fails (network, auth, or HTTP status)."""


class JiraClient:
    """Minimal Jira REST wrapper: fetch one issue, or search by JQL."""

    def __init__(self, project) -> None:
        self.project = project
        self.base_url, email, token = config.jira_credentials(project)
        raw = f"{email}:{token}".encode()
        self._auth_header = "Basic " + base64.b64encode(raw).decode()

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Issue a JSON request and return the parsed response, raising ``JiraError`` on failure."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._auth_header)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise JiraError(f"Jira {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Jira {method} {path} failed: {exc.reason} (VPN/network?)") from exc

    def get_issue(self, key: str) -> dict:
        """Return the raw issue object for a ticket key (e.g. ``ABC-1234``)."""
        query = urllib.parse.urlencode({"fields": ",".join(issue_fields(self.project))})
        return self._request("GET", f"/rest/api/3/issue/{key}?{query}")

    def get_transitions(self, key: str) -> list[dict]:
        """Return the workflow transitions currently available for an issue."""
        return self._request("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])

    def transition_issue(self, key: str, target_name: str = "In Progress") -> bool:
        """Best-effort: move an issue to the named status. Returns True if a transition was applied.

        Matches by name (case-insensitive substring) against the transition name and its target
        status, so "In Progress" finds a "Start Progress"/"In Progress" transition. Never raises —
        the status change is nice-to-have, not load-bearing.
        """
        try:
            wanted = target_name.lower()
            for t in self.get_transitions(key):
                name = (t.get("name", "") + " " + (t.get("to", {}) or {}).get("name", "")).lower()
                if wanted in name:
                    self._request(
                        "POST", f"/rest/api/3/issue/{key}/transitions",
                        {"transition": {"id": t["id"]}},
                    )
                    return True
        except JiraError:
            pass
        return False

    def add_comment(self, key: str, body: str) -> bool:
        """Post a plain-text comment on an issue. Returns True on success, never raises.

        Uses the v2 comment endpoint, which accepts a plain string body (v3 requires ADF). Posting
        the PR/Confluence link on the ticket is a nice-to-have, so a failure is swallowed.
        """
        try:
            self._request("POST", f"/rest/api/2/issue/{key}/comment", {"body": body})
            return True
        except JiraError:
            return False

    def get_sprints(self, board_id: str, states: str = "active,future") -> list[dict]:
        """Return a board's sprints in the given states (Agile API), following pagination."""
        sprints: list[dict] = []
        start = 0
        while True:
            page = self._request(
                "GET",
                f"/rest/agile/1.0/board/{board_id}/sprint?state={states}&startAt={start}&maxResults=50",
            )
            values = page.get("values", [])
            sprints.extend(values)
            if page.get("isLast", True) or not values:
                break
            start += len(values)
        return sprints

    def search(self, jql: str, max_results: int = 100) -> list[dict]:
        """Return all issues matching ``jql``, following pagination to completion.

        Uses the current Jira Cloud search endpoint (``/search/jql``), which paginates with an
        opaque ``nextPageToken`` rather than numeric offsets.
        """
        issues: list[dict] = []
        next_token: str | None = None
        while True:
            body: dict = {"jql": jql, "fields": issue_fields(self.project), "maxResults": max_results}
            if next_token:
                body["nextPageToken"] = next_token
            page = self._request("POST", "/rest/api/3/search/jql", body)
            issues.extend(page.get("issues", []))
            next_token = page.get("nextPageToken")
            if page.get("isLast", True) or not next_token:
                break
        return issues
