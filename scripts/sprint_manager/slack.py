"""Fetch a Slack thread's messages for agent context (standard library only, no MCP).

Mirrors ``jira_client.py``/``jenkins.py``: a Slack app bot token used over the plain Web API,
NOT the (interactively OAuth-authenticated) Slack MCP, which is unavailable to a headless agent
subprocess. Deliberately minimal — this answers only "what was said in this thread?"; summarizing
or acting on it stays with the agent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402

# A Slack message/thread link: https://<workspace>.slack.com/archives/<CHANNEL>/p<digits>
# (optionally with a ?thread_ts=<parent-ts>&cid=<channel> query when it's a deep link to one
# reply inside a thread — thread_ts then names the actual root, not the linked message itself).
_URL_RE = re.compile(r"archives/([A-Z0-9]+)/p(\d+)")


class SlackError(RuntimeError):
    """Raised when a Slack Web API request fails, with an actionable hint where possible."""


def parse_thread_url(text: str) -> tuple[str, str] | None:
    """Extract ``(channel_id, thread_root_ts)`` from a Slack link. ``None`` if none is found."""
    match = _URL_RE.search(text or "")
    if not match:
        return None
    channel, raw_ts = match.group(1), match.group(2)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    thread_ts = (query.get("thread_ts") or [None])[0]
    ts = thread_ts or f"{raw_ts[:-6]}.{raw_ts[-6:]}"  # p1788288345828959 -> 1788288345.828959
    return channel, ts


def _hint(error: str | None) -> str:
    if error == "not_in_channel":
        return " — invite the bot to this channel first (`/invite @<bot-name>` in Slack)."
    if error == "missing_scope":
        return " — the bot token is missing a required OAuth scope (reading: channels:history / " \
               "groups:history / users:read / channels:read; optional write-backs: " \
               "reactions:write / chat:write)."
    if error == "channel_not_found":
        return " — the bot can't see this channel (wrong workspace token, or a private channel " \
               "it hasn't been invited to)."
    return ""


def _get(method: str, params: dict) -> dict:
    token = config.slack_credentials()
    url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise SlackError(f"Slack {method} failed: {exc.reason} (VPN/network?)") from exc
    if not data.get("ok"):
        error = data.get("error")
        raise SlackError(f"Slack {method} failed: {error}{_hint(error)}")
    return data


def _post(method: str, payload: dict) -> dict:
    token = config.slack_credentials()
    request = urllib.request.Request(f"https://slack.com/api/{method}",
                                     data=json.dumps(payload).encode(), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise SlackError(f"Slack {method} failed: {exc.reason} (VPN/network?)") from exc
    if not data.get("ok") and data.get("error") != "already_reacted":
        error = data.get("error")
        raise SlackError(f"Slack {method} failed: {error}{_hint(error)}")
    return data


def _thread_or_raise(url: str) -> tuple[str, str]:
    parsed = parse_thread_url(url)
    if parsed is None:
        raise SlackError(f"Could not parse a Slack channel/thread from: {url}")
    return parsed


def add_reaction(url: str, name: str) -> None:
    """React to the thread's root message (needs the ``reactions:write`` scope)."""
    channel, ts = _thread_or_raise(url)
    _post("reactions.add", {"channel": channel, "timestamp": ts, "name": name})


def post_reply(url: str, text: str) -> None:
    """Reply in the thread (needs the ``chat:write`` scope)."""
    channel, ts = _thread_or_raise(url)
    _post("chat.postMessage", {"channel": channel, "thread_ts": ts, "text": text})


def fetch_thread(url: str) -> dict:
    """Return ``{channel, channel_name, url, messages: [{user, text, ts}]}`` for a thread link.

    If the link points at a message with no replies, ``messages`` is just that one message —
    ``conversations.replies`` returns the root alone when there's no thread.
    """
    parsed = parse_thread_url(url)
    if parsed is None:
        raise SlackError(f"Could not parse a Slack channel/thread from: {url}")
    channel, ts = parsed

    replies = _get("conversations.replies", {"channel": channel, "ts": ts, "limit": "200"})
    info = _get("conversations.info", {"channel": channel})
    channel_name = info.get("channel", {}).get("name", channel)

    user_cache: dict[str, str] = {}

    def _display_name(uid: str) -> str:
        if not uid:
            return "?"
        if uid not in user_cache:
            try:
                profile = _get("users.info", {"user": uid}).get("user", {})
                user_cache[uid] = profile.get("real_name") or profile.get("name") or uid
            except SlackError:
                user_cache[uid] = uid
        return user_cache[uid]

    messages = [
        {"user": _display_name(m.get("user", "")), "text": m.get("text", ""), "ts": m.get("ts", "")}
        for m in replies.get("messages", [])
    ]
    return {"channel": channel, "channel_name": channel_name, "url": url, "messages": messages}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a Slack thread's messages for agent context.")
    sub = parser.add_subparsers(dest="command", required=True)

    thread_cmd = sub.add_parser("thread", help="Fetch a thread's messages from its Slack link")
    thread_cmd.add_argument("--url", required=True, help="A Slack message/thread URL (…/archives/<C>/p<ts>)")

    args = parser.parse_args(argv)
    try:
        if args.command == "thread":
            print(json.dumps(fetch_thread(args.url), indent=2))
    except (config.ConfigError, SlackError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
