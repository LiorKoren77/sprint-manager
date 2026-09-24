"""Slack-thread source: a thread whose discussion is the problem statement.

``external_ref`` = the thread URL. The whole thread is re-read at every fresh session (people keep
replying), and a reply count is the pr-open feedback signal (``feedback_count``: one
``conversations.replies`` call per poll). Optional write-backs, off by default because they need
extra bot scopes (``[slack]`` in the profile): ``react = true`` (👀 when work starts, ✅ when done;
``reactions:write``) and ``reply_on_ship = true`` (a thread reply with the PR link; ``chat:write``).
"""

from __future__ import annotations

from sprint_manager import config
from sprint_manager import slack as slack_api
from sprint_manager.sources import Source, SourceError, register


def render_thread(thread: dict) -> str:
    """A Slack thread as Markdown: one ``**who**: text`` paragraph per message."""
    lines = [f"Slack thread in #{thread.get('channel_name', '?')}: {thread.get('url', '')}", ""]
    for m in thread.get("messages", []):
        lines.append(f"**{m.get('user', '?')}**: {m.get('text', '').strip()}")
        lines.append("")
    return "\n".join(lines).strip()


def first_line(thread: dict, limit: int = 80) -> str:
    """A title suggestion: the root message's first line, trimmed."""
    msgs = thread.get("messages") or []
    text = (msgs[0].get("text", "") if msgs else "").strip().splitlines()
    head = text[0] if text else "Slack thread"
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


class SlackSource(Source):
    name = "slack"

    def fetch(self, url: str) -> dict:
        try:
            return slack_api.fetch_thread(url)
        except (config.ConfigError, slack_api.SlackError) as exc:
            raise SourceError(str(exc)) from exc

    def load(self, status, proj) -> dict:
        thread = self.fetch(status.external_ref)
        return {
            "key": status.ticket,
            "type": "Bug" if status.branch_kind == "bug" else "Feature",
            "status": "",
            "summary": status.summary or first_line(thread),
            "url": status.external_ref,
            "description": render_thread(thread),
            "comments": [],
        }

    def pr_body_footer(self, status) -> str:
        return f"Slack thread: {status.external_ref}"

    def _write(self, fn, *args) -> None:
        try:
            fn(*args)
        except (config.ConfigError, slack_api.SlackError) as exc:
            raise SourceError(str(exc)) from exc

    def on_work_start(self, status, proj) -> str | None:
        if not proj.slack.get("react"):
            return None
        self._write(slack_api.add_reaction, status.external_ref, "eyes")
        return "Slack: 👀 added to the thread."

    def on_shipped(self, status, proj, pr_url: str, new_pr: bool) -> str | None:
        if not (proj.slack.get("reply_on_ship") and new_pr):
            return None
        self._write(slack_api.post_reply, status.external_ref, f"PR opened: {pr_url}")
        return "Slack: posted the PR link in the thread."

    def on_done(self, status, proj) -> str | None:
        if not proj.slack.get("react"):
            return None
        self._write(slack_api.add_reaction, status.external_ref, "white_check_mark")
        return "Slack: ✅ added to the thread."

    def feedback_count(self, status, proj) -> int | None:
        return max(len(self.fetch(status.external_ref).get("messages", [])) - 1, 0)


register(SlackSource())
