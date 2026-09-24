"""Publish a plan to Confluence over REST (standard library only) — no Atlassian MCP.

This is the ONLY thing the plan stage used the MCP for; doing it here means every stage can run
with ``strict_mcp_config=True`` (no 30+ tool schemas riding in each turn's context) and there is no
OAuth marketplace server to break. Auth reuses the Jira email + API token (same Atlassian site).

The agent writes its plan as Markdown to a file and runs::

    python -m sprint_manager.confluence publish --title "ABC-1234 plan" \
        --file plan.md [--parent-id 123456]

``publish`` is idempotent by (space, title): if a page with that title already exists it is UPDATED
(new version), so re-publishing a revised plan never creates duplicates. Markdown is converted to
Confluence *storage* format (the API's canonical XHTML) covering the plan-doc subset: headings,
paragraphs, bold/italic/inline-code, fenced code blocks, bullet/numbered lists, links, rules.

Once a plan is published, reviewers often leave INLINE comments directly on the page (highlighting
a paragraph and annotating it) rather than replying in Jira or chat::

    python -m sprint_manager.confluence comments --title "ABC-1234 plan"

Returns each comment's status (open/resolved/reopened/dangling), author, the exact text it's
anchored to, and its body — so the agent can read reviewer feedback left directly on the plan
without the manager having to relay it. Open (unresolved) comments only by default; pass
``--all`` for the full history including resolved ones.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import config  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402


class ConfluenceError(RuntimeError):
    """Raised when a Confluence request fails (network, auth, or HTTP status)."""


# ----- Markdown -> Confluence storage (XHTML) ------------------------------------------------

def _inline(text: str) -> str:
    """Convert inline Markdown in one already-HTML-escaped line to storage markup."""
    text = html.escape(text, quote=False)
    # code spans first so their contents aren't touched by bold/italic/link rules
    code_spans: list[str] = []

    def _stash_code(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash_code, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    # restore code spans (their bodies stay literal)
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{code_spans[int(m.group(1))]}</code>", text)
    return text


def _storage_to_text(value: str) -> str:
    """Convert a comment's Confluence storage-format body to plain, readable text.

    A lossy one-way strip (the reverse direction of ``md_to_storage``): drops all tags and
    unescapes entities. Good enough for reading reviewer feedback — a comment body is prose, not a
    document needing structure preserved. Self-closing tags like an ``<ac:link>`` @mention with no
    inner text just disappear; the comment's own author is already tracked separately.
    """
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _code_macro(language: str, body: str) -> str:
    lang = f'<ac:parameter ac:name="language">{html.escape(language)}</ac:parameter>' if language else ""
    return (
        f'<ac:structured-macro ac:name="code">{lang}'
        f"<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>"
    )


def md_to_storage(md: str) -> str:
    """Convert a Markdown plan to Confluence storage format (the plan-doc subset)."""
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    list_stack: list[str] = []  # open "ul"/"ol" tags, to close on dedent/blank

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    while i < len(lines):
        line = lines[i]

        # fenced code block
        fence = re.match(r"^```(\w*)\s*$", line)
        if fence:
            close_lists()
            language = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^```\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            out.append(_code_macro(language, "\n".join(body)))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_lists()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            close_lists()
            out.append("<hr/>")
            i += 1
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or number:
            want = "ul" if bullet else "ol"
            if not list_stack or list_stack[-1] != want:
                close_lists()
                out.append(f"<{want}>")
                list_stack.append(want)
            content = (bullet or number).group(1)
            out.append(f"<li>{_inline(content.strip())}</li>")
            i += 1
            continue

        if line.strip() == "":
            close_lists()
            i += 1
            continue

        # plain paragraph: gather consecutive non-blank, non-structural lines
        close_lists()
        para: list[str] = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+[.)]\s|\s*[-*_]{3,}\s*$)", lines[i]
        ):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{_inline(' '.join(s.strip() for s in para))}</p>")

    close_lists()
    return "".join(out)


# ----- Confluence REST client ----------------------------------------------------------------

class ConfluenceClient:
    """Minimal Confluence Cloud REST wrapper: resolve a space, find/create/update a page."""

    def __init__(self, project) -> None:
        self.base_url, email, token = config.confluence_credentials(project)
        raw = f"{email}:{token}".encode()
        self._auth_header = "Basic " + base64.b64encode(raw).decode()

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._auth_header)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ConfluenceError(f"Confluence {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ConfluenceError(f"Confluence {method} {path} failed: {exc.reason} (VPN/network?)") from exc

    def space_id(self, space_key: str) -> str:
        """Resolve a space KEY (e.g. "ENG") to its numeric id (required by the v2 API)."""
        query = urllib.parse.urlencode({"keys": space_key})
        results = self._request("GET", f"/api/v2/spaces?{query}").get("results", [])
        if not results:
            raise ConfluenceError(f"No Confluence space with key {space_key!r}.")
        return results[0]["id"]

    def find_page(self, space_id: str, title: str) -> dict | None:
        """Return an existing page (id + version) with this exact title in the space, or None."""
        query = urllib.parse.urlencode({"space-id": space_id, "title": title})
        results = self._request("GET", f"/api/v2/pages?{query}").get("results", [])
        return results[0] if results else None

    def _page_url(self, page: dict) -> str:
        links = page.get("_links", {})
        webui = links.get("webui", "")
        base = links.get("base") or self.base_url
        return f"{base}{webui}" if webui else f"{self.base_url}/pages/{page.get('id', '')}"

    def _display_name(self, account_id: str, cache: dict[str, str]) -> str:
        """Resolve an accountId to a display name (v1 REST — the only place it's exposed), cached
        per call since a page's comments are typically from a handful of reviewers."""
        if account_id not in cache:
            try:
                user = self._request("GET", f"/rest/api/user?accountId={urllib.parse.quote(account_id)}")
                cache[account_id] = user.get("displayName") or account_id
            except ConfluenceError:
                cache[account_id] = account_id  # best-effort — fall back to the raw id
        return cache[account_id]

    def inline_comments(self, page_id: str) -> list[dict]:
        """Every inline comment on a page (paginated), newest-anchor-first as the API returns them.

        Each: ``{id, status, author, created, anchored_text, text, url}`` — ``status`` is
        Confluence's own ``open``/``resolved``/``reopened``/``dangling`` (a "dangling" comment's
        anchor text no longer exists on the page, but the comment itself persists). Does not
        include replies to a comment; each inline comment is returned once, at its own anchor.
        """
        results: list[dict] = []
        author_cache: dict[str, str] = {}
        path = f"/api/v2/pages/{page_id}/inline-comments?body-format=storage&limit=50"
        while path:
            data = self._request("GET", path)
            for c in data.get("results", []):
                props = c.get("properties") or {}
                version = c.get("version") or {}
                body = ((c.get("body") or {}).get("storage") or {}).get("value", "")
                author_id = version.get("authorId", "")
                webui = (c.get("_links") or {}).get("webui", "")
                results.append({
                    "id": c.get("id"),
                    "status": c.get("resolutionStatus", "unknown"),
                    "author": self._display_name(author_id, author_cache) if author_id else "",
                    "created": version.get("createdAt", ""),
                    "anchored_text": props.get("inlineOriginalSelection", ""),
                    "text": _storage_to_text(body),
                    "url": f"{self.base_url}{webui}" if webui else "",
                })
            next_link = (data.get("_links") or {}).get("next")
            # `next` is site-root-relative (starts with /wiki); _request prepends base_url, which
            # ALREADY ends in /wiki — strip it so the two don't double up.
            path = next_link[len("/wiki"):] if next_link and next_link.startswith("/wiki") else next_link
        return results


def publish(project, space_key: str, title: str, body_markdown: str, parent_id: str | None = None) -> dict:
    """Create or update a Confluence page from Markdown. Returns ``{id, url, action}``.

    Idempotent by (space, title): an existing page is updated to a new version rather than
    duplicated. Returns the page id and its browser URL (posted to the Jira issue by the caller).
    """
    client = ConfluenceClient(project)
    sid = client.space_id(space_key)
    storage = md_to_storage(body_markdown)
    existing = client.find_page(sid, title)
    if existing:
        current_version = (existing.get("version") or {}).get("number", 1)
        page = client._request("PUT", f"/api/v2/pages/{existing['id']}", {
            "id": existing["id"],
            "status": "current",
            "title": title,
            "spaceId": sid,
            "body": {"representation": "storage", "value": storage},
            "version": {"number": current_version + 1, "message": "Updated by sprint-manager"},
        })
        return {"id": page["id"], "url": client._page_url(page), "action": "updated"}
    payload = {
        "spaceId": sid,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": storage},
    }
    if parent_id:
        payload["parentId"] = parent_id
    page = client._request("POST", "/api/v2/pages", payload)
    return {"id": page["id"], "url": client._page_url(page), "action": "created"}


def read_inline_comments(project, space_key: str, title: str, unresolved_only: bool = True) -> dict:
    """Return the inline comments left on a published page. Returns ``{page_id, url, count,
    comments}`` where each comment is ``{id, status, author, created, anchored_text, text, url}``.

    ``unresolved_only`` (default) drops ``resolved`` comments, keeping ``open``/``reopened``/
    ``dangling`` — the ones that still need a look. Pass ``False`` for the full history.
    """
    client = ConfluenceClient(project)
    sid = client.space_id(space_key)
    page = client.find_page(sid, title)
    if not page:
        raise ConfluenceError(f"No page titled {title!r} in space {space_key!r}.")
    comments = client.inline_comments(page["id"])
    if unresolved_only:
        comments = [c for c in comments if c["status"] != "resolved"]
    return {"page_id": page["id"], "url": client._page_url(page), "count": len(comments), "comments": comments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a Markdown plan to Confluence (REST).")
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser("publish", help="Create or update a page from a Markdown file")
    pub.add_argument("--space", default=None,
                     help="Space KEY (default: the project's [jira] confluence_space)")
    pub.add_argument("--title", required=True, help="Page title (idempotent key within the space)")
    pub.add_argument("--file", required=True, help="Path to the Markdown file to publish")
    pub.add_argument("--parent-id", dest="parent_id", default=None, help="Parent page id (optional)")
    pub.add_argument("--ticket", default=None,
                     help="If given, post the published page's link as a comment on this Jira issue")

    com = sub.add_parser("comments", help="Read inline comments left on a published page")
    com.add_argument("--space", default=None,
                     help="Space KEY (default: the project's [jira] confluence_space)")
    com.add_argument("--title", required=True, help="Page title (must already be published)")
    com.add_argument("--all", action="store_true",
                     help="Include resolved comments too (default: unresolved only)")

    for cmd in (pub, com):
        cmd.add_argument("--project", default=None, help="Project name (default: $SM_PROJECT)")
    args = parser.parse_args(argv)
    try:
        proj = project_mod.resolve(args.project, args.ticket if args.command == "publish" else None)
        space = args.space or proj.jira.get("confluence_space")
        if not space:
            raise config.ConfigError(f"No --space given and project {proj.name!r} has no "
                                     "[jira] confluence_space.")
        if args.command == "comments":
            result = read_inline_comments(proj, space, args.title, unresolved_only=not args.all)
        else:
            markdown = Path(args.file).read_text()
            result = publish(proj, space, args.title, markdown, args.parent_id)
            if args.ticket:
                from sprint_manager.jira_client import JiraClient  # same CLI layer
                posted = JiraClient(proj).add_comment(
                    args.ticket, f"Plan {result['action']} in Confluence: {result['url']}")
                result["jira_comment"] = posted
        print(json.dumps(result, indent=2))
    except (config.ConfigError, ConfluenceError, OSError, project_mod.ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
