"""The manager loop: drives user-gated stages for each ticket.

Session boundaries sit where context changes character, not at every gate. ``start`` enters
explore (one read-only session: recap → questions → plan); ``approve`` advances explore → work (one
worktree session: implement → review → test plan → run → results). ``ship`` is a zero-LLM action
that leaves work: commit check, final recap with the PR title/body, push, open the PR, Jira → In
Review. In pr-open there is no standing session: each CI/review signal the manager engages with
starts a short triage *episode* (fresh session seeded from a trimmed notes view + the branch's
commit log/diffstat), which ends when the ticket next rests at waiting_external. CI is triggered
only by the manager (``trigger_ci``). ``chat`` iterates within the current session. Only summaries
reach the UI (thinking/tool events are dropped).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess
import urllib.error
from collections import defaultdict, deque
from datetime import datetime

from sprint_manager import background, ci, config, notes, pr, state, taskfile, transcript_store, worktree
from sprint_manager import project as project_mod
from sprint_manager import sources
from sprint_manager.agent import (
    MCP_OPEN_STAGES,
    TicketAgent,
    build_context_message,
    build_system_prompt,
)
from sprint_manager.fetch_sprint import fetch_sprint, simplify_issue
from sprint_manager.jira_client import JiraClient, JiraError, parse_issue_key
from sprint_manager.models import (
    READ_ONLY_STAGES,
    SESSION_FORMAT,
    Activity,
    Stage,
    TicketStatus,
    max_turns_for,
    model_for_stage,
    next_stage,
    uses_worktree,
)

TRANSCRIPT_LIMIT = 400
# Drop thinking/tool (summaries only). "result" is also dropped: it just re-echoes the final
# assistant text already shown as "text", so it's a UI duplicate. (The compaction summary written
# to the notes file uses ResultMessage.result directly in _run_turn — unaffected by this.)
VISIBLE_EVENT_KINDS = {"user", "text", "system"}

# Transient activities: meaningful ONLY while a live in-flight turn backs them in THIS process.
# They have no durable meaning — a persisted working/queued without a live turn (process death, a
# turn that raised, or the agent's own stale report_stage write) is, by definition, not running.
# The in-memory _running/_queued sets are the sole authority; persisted transient values are
# reconciled against them on every read (see _live_activity). This is what makes a "stuck" state
# structurally impossible rather than something we detect after the fact.
TRANSIENT_ACTIVITIES = {Activity.WORKING, Activity.QUEUED}

# The orchestrator's opening message when it enters a stage; the stage file carries the specifics.
_KICKOFF = ("Begin the {stage} stage now, following your stage instructions. Keep it to one concise "
            "summary, delegate any question to me, then stop at waiting_user.")

# Compact is a CONTINUATION of the same stage's work, not a fresh start — it must never look like
# the generic _KICKOFF above, which tells a brand-new session to "begin the stage" from scratch and
# is exactly what made a mid-stage compact read as "the plan got cleared and re-asked from zero".
_COMPACT_KICKOFF = (
    "Your context was just compacted to trim unrelated exploration — this is a CONTINUATION, not a "
    "fresh start. The notes above already contain your complete prior work in the {stage} stage "
    "(see the '{stage} — mid-stage compact' section) — treat it as ground truth. Do not restart "
    "the stage, redo work already done, or re-ask questions already answered. If the stage's work "
    "is already complete, just reconfirm it concisely, delegate anything outstanding to me, and "
    "stop at waiting_user."
)

# Asked of the CURRENT (still-live) session right before ANY transition that disposes it — compact,
# Approve, or a "go to stage" jump. Without this, the notes only got whatever the agent's most
# recent turn happened to summarize — often a terse delta ("addressed your feedback on step 3"),
# not the actual plan/state. Confirmed for real on a live ticket: a normal Approve carried forward only
# a one-item nitpick, dropping the actual 7-point plan that only the live conversation still had.
_RECAP_PROMPT = (
    "Before this session ends, write ONE complete, self-contained `### Summary` covering the FULL "
    "current state of this stage: restate the whole plan / all decisions made and their rationale / "
    "exact status — everything the next session needs to continue seamlessly with zero other "
    "context. Do not just describe what changed in this last exchange — restate everything "
    "relevant to date. Then report your status and stop."
)

# Ship's variant of the recap: the same full restatement (it becomes the work section of the notes,
# which the pr-open triage episodes and any jump back are seeded from) PLUS the PR title/body, which
# the orchestrator parses (_parse_pr_block) instead of spending an agent on `gh pr create`.
_SHIP_RECAP_PROMPT = (
    _RECAP_PROMPT.replace("Then report your status and stop.", "")
    + "Then, at the very end, add the pull request text in exactly this form (the system opens the "
    "PR with it — do NOT push, open a PR, or trigger CI yourself):\n\n"
    "### PR\n"
    "title: {title_prefix}<short imperative title>\n"
    "body:\n"
    "<one-paragraph summary of the change and how it was verified>\n"
    "<if the notes hold approved acceptance criteria: a checklist of them, `- [x]` met / `- [ ]` not>\n\n"
    "Change nothing in the worktree. Then report your status and stop."
)

# The opening message of a pr-open triage episode (a fresh session, see _episode_kickoff).
_EPISODE_KICKOFF = (
    "This is a fresh pr-open triage episode — the implementation session is gone; the notes above "
    "and the branch state below are your context. Follow your pr-open stage instructions."
)

# Auto-triage on a CI failure (the only poll signal that spends an agent turn by itself).
_CI_FAILURE_PROMPT = (
    "The CI build failed. Fetch the failing checks/stages and their logs, analyze them (with any "
    "CI-debugging tools the project notes name), identify which failures are relevant to THIS task "
    "vs pre-existing noise on the base branch, summarise, and recommend how to proceed; then stop "
    "at waiting_user."
)

# The heading agents are told to end every turn with (base prompt: "write a short ### Summary").
_SUMMARY_MARKER = "### Summary"

# Per-session cache-read level above which a turn raises a CACHE ALERT. work is one session that
# spans implementation AND testing, so it legitimately grows past the 1M line the old per-stage
# sessions used; alerting there on every turn would just be noise. (Heuristic, not measured.)
_CACHE_ALERT_TOKENS = {Stage.WORK: 3_000_000}
_CACHE_ALERT_DEFAULT = 1_000_000

_PR_TITLE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)


def _parse_pr_block(recap: str, ref: str, summary: str) -> tuple[str, str]:
    """Extract (title, body) from the ship recap's ``### PR`` block, with deterministic fallbacks.

    ``ref`` is the task source's reference tag ("ABC-12", "#412", or "" for a task with none).
    Fallback title is ``<ref>: <summary>``; fallback body is the recap's first paragraph (the recap
    always exists when a session did). A non-empty ref is forced into the title so the PR is always
    traceable even if the agent drops it.
    """
    title, body = "", ""
    idx = recap.rfind("### PR")
    if idx >= 0:
        block = recap[idx + len("### PR"):]
        m = _PR_TITLE.search(block)
        if m:
            title = m.group(1).strip()
        body_idx = block.find("body:")
        if body_idx >= 0:
            body = block[body_idx + len("body:"):].strip()
    if not title:
        title = (f"{ref}: {summary}" if ref else summary).strip().rstrip(":") or "Change"
    if ref and ref not in title:
        title = f"{ref}: {title}"
    if not body:
        paragraphs = [p.strip() for p in (recap[:idx] if idx >= 0 else recap).split("\n\n")]
        body = next((p for p in paragraphs if p and not p.startswith("#")), "") or title
    return title, body


def _resumable(status: TicketStatus) -> bool:
    """A stored session can be resumed only if it exists AND was created under the current prompt
    layout (``SESSION_FORMAT``) — an older one lacks the ticket context in its own history."""
    return bool(status.session_id) and status.session_format == SESSION_FORMAT


def _friendly_poll_error(exc: BaseException, service: str) -> str:
    """Translate a raw urllib/socket exception into an actionable one-liner.

    Jenkins, Jira/Confluence, and (via ``gh``) GitHub are all reached over the corporate network
    from here — a DNS/connection failure polling any of them is overwhelmingly "not on the VPN",
    not a code problem, so say that directly instead of surfacing a cryptic urllib string.
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror) or "name resolution" in str(exc).lower():
        return f"can't reach {service} (DNS lookup failed) — check your VPN connection"
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError)):
        return f"can't reach {service} ({exc}) — check your VPN/network connection"
    return str(exc)


def _summary_from_blocks(text_blocks: list[str], fallback: str | None) -> str:
    """Derive the durable per-turn summary (for the notes handoff) from a turn's text blocks.

    Agents are told to end with a ``### Summary``; some then add a trailing line ("please
    approve"), which the SDK reports as a separate block — and ``ResultMessage.result`` keeps only
    that last block, dropping the summary. So: take everything from the LAST ``### Summary`` heading
    onward; if the agent emitted no such heading, keep the whole turn's text (better than a
    fragment); only if there were no text blocks at all fall back to ``result.result``.
    """
    if not text_blocks:
        return (fallback or "").strip()
    for i in range(len(text_blocks) - 1, -1, -1):
        if _SUMMARY_MARKER in text_blocks[i]:
            return "\n\n".join(text_blocks[i:]).strip()
    return "\n\n".join(text_blocks).strip()


class Orchestrator:
    """Owns each ticket's CURRENT stage agent and drives gated transitions with compaction."""

    def __init__(self, max_active: int = 1) -> None:
        self._agents: dict[str, TicketAgent] = {}              # ticket -> current stage's agent
        self._meta: dict[str, dict] = {}                       # ticket -> simplified Jira issue
        self._last_summary: dict[str, str] = {}                # ticket -> last turn's summary text
        self._transcripts: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRANSCRIPT_LIMIT))
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._turn_lock = asyncio.Semaphore(max_active)
        self._poll_task: asyncio.Task | None = None
        self._bg_poll_task: asyncio.Task | None = None
        self._cache_history: dict[str, dict] = {}              # ticket -> {stage: (turn, cache_read)}
        # Cumulative baselines: the ticket's totals at the CURRENT stage's start (cost + turns).
        # Each fresh stage agent counts only its own spend/turns (starts at 0), so persisted value =
        # baseline + agent total — making cost_usd and total_turns accumulate across stages instead
        # of resetting each stage. Keyed ticket -> {"cost": float, "turns": int}.
        self._stage_base: dict[str, dict] = {}
        # Ground truth for transient state: a ticket is "working"/"queued" iff it is in these sets.
        # Reset to empty on every process start, so a restart can never inherit a stuck turn.
        self._running: set[str] = set()                        # tickets with a live agent.send()
        self._queued: set[str] = set()                         # tickets awaiting the turn slot
        # Tickets mid-transition (approve/start) BEFORE their turn's _running kicks in — the slow
        # window of dispose + Jira + worktree checkout + agent connect. Counts as "working" for the
        # UI (instant feedback) and blocks a duplicate approve/start (the double-click bug).
        self._advancing: set[str] = set()
        # Last poll-failure message per (ticket, channel) — "ci" / "review" / "general". Lets the
        # poll loop notify once per outage instead of repeating the identical DNS/connection error
        # every PR_POLL_SECONDS for as long as the manager is off VPN. Cleared the moment that
        # channel's poll succeeds again, so a NEW failure (or recovery-then-fail) still notifies.
        self._poll_error_seen: dict[tuple[str, str], str] = {}
        # Work sessions (by session id) that already got the "compact before testing?" hint.
        self._compact_hinted: set[str] = set()
        # The event loop the service runs on (set when the first control task starts), so worker
        # threads can hand broadcasts back to it.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Serializes every transition/chat coroutine PER TICKET (see _locked). A stage transition
        # (_advance/_goto_stage/_compact) spans several ``await`` points between disposing the old
        # agent, attaching the new one, and persisting the new stage to disk. A concurrent chat
        # landing in that window could see ``ticket not in self._agents`` and reattach using the
        # stage still on disk (not yet updated) — silently replacing the just-created new-stage
        # agent with one resuming the OLD stage's session. Confirmed for real on a live ticket: a
        # "continue" sent right as plan->implementation advanced left the ticket permanently
        # talking to a resumed plan session, even though disk state (and the worktree/branch)
        # already showed implementation.
        self._agent_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ----- sprint loading -------------------------------------------------------------------

    def load_sprint(self, sprint: str, assignee: str = "me", project: str | None = None) -> list[dict]:
        """Fetch the sprint's tickets (the project's Jira board) and seed a to-do row for each new one."""
        proj = project_mod.resolve(project) if project else self._default_project()
        issues = fetch_sprint(proj, sprint, assignee)
        for issue in issues:
            self._seed_issue(issue, source="sprint", project=proj.name)
        return issues

    @staticmethod
    def _default_project():
        name = project_mod.default_project_name()
        if not name:
            raise project_mod.ProjectError("No default project — pick one, or register a project.")
        return project_mod.load(name)

    def _project(self, ticket: str):
        """The ticket's project (see project.for_ticket)."""
        return project_mod.for_ticket(ticket)

    def _seed_issue(self, issue: dict, source: str, project: str = "") -> None:
        """Cache an issue's metadata and ensure it has a persisted status row (idempotent).

        A brand-new ticket gets a fresh ``to-do`` row tagged with ``source``; an existing one only
        has its display fields refreshed — so re-seeding (re-load, re-add, or startup rehydrate)
        never resets an in-progress ticket's stage/activity or changes its original source.
        """
        key = issue["key"]
        self._meta[key] = issue
        fields = {
            "summary": issue.get("summary", ""),
            "issue_type": issue.get("type", ""),
            "jira_status": issue.get("status", ""),
        }
        if state.read(key) is None:
            state.update(key, source=source, project=project, tracker="jira", external_ref=key,
                         external_url=issue.get("url", ""), stage=Stage.TODO,
                         activity=Activity.IDLE, note="not started", **fields)
        else:
            state.update(key, **fields)

    def add_issue(self, url_or_key: str, project: str | None = None) -> dict:
        """Add a single issue (in the sprint or not) by browse URL or key. Idempotent.

        Tries to fetch full details from Jira; if that fails (VPN/creds), still seeds a minimal row
        from the key so you can add offline — the agent's lazy ``_ensure_meta`` (and the next
        startup rehydrate) fill the details in later. Returns ``{key, warning?}``.
        """
        key = parse_issue_key(url_or_key)
        if not key:
            raise ValueError(f"Couldn't find an issue key in {url_or_key!r} (expected e.g. ABC-12345).")
        proj = project_mod.resolve(project) if project else self._default_project()
        try:
            self._seed_issue(simplify_issue(JiraClient(proj).get_issue(key), proj),
                             source="manual", project=proj.name)
            return {"key": key}
        except (config.ConfigError, JiraError) as exc:
            # Offline-friendly: seed a minimal row from the key alone. description=None makes the
            # lazy _ensure_meta re-fetch the full issue the first time the agent needs it.
            self._seed_issue(
                {"key": key, "type": "", "status": "", "summary": "",
                 "url": config.browse_url(proj, key), "description": None, "comments": []},
                source="manual", project=proj.name,
            )
            return {"key": key, "warning": f"Added {key}, but couldn't reach Jira ({exc}); details load later."}

    # ----- task intake: text / GitHub issue / Slack thread (Jira: load_sprint / add_issue) ----

    def create_task(self, project: str | None, tracker: str, title: str = "", kind: str = "",
                    body: str = "", ref: str = "") -> dict:
        """Create a task from a non-Jira source (blocking; run in a thread). Returns ``{key}``.

        * ``text``   — ``title`` + ``body`` (the problem statement) + ``kind`` (bug/feature).
        * ``github`` — ``ref`` = an issue URL or ``#N``; title/kind come from the issue.
        * ``slack``  — ``ref`` = a thread link; ``title`` optional (defaults to the root message's
          first line), ``kind`` as chosen.

        Idempotent for GitHub (the id is the issue number) and Slack (same thread → same task).
        """
        proj = project_mod.resolve(project) if project else self._default_project()
        kind = kind if kind in ("bug", "feature") else ""
        if tracker == "text":
            if not title.strip() or not body.strip():
                raise ValueError("A text task needs a title and a problem description.")
            key = sources.new_task_id(proj.name, "text")
            taskfile.write(key, body)
            self._seed_task(key, proj, "text", title.strip(), kind or "feature", "", "")
        elif tracker == "github":
            from sprint_manager.sources.github import parse_issue_ref
            parsed = parse_issue_ref(ref)
            if not parsed:
                raise ValueError(f"Not a GitHub issue URL or #number: {ref!r}")
            issue = sources.get("github").fetch(proj, ref.strip())
            key = sources.new_task_id(proj.name, "github", issue["number"])
            meta = sources.get("github").to_meta(key, issue)
            self._seed_task(key, proj, "github", meta["summary"], kind or meta["type"].lower(),
                            str(issue["number"]), issue.get("url", ""), meta)
        elif tracker == "slack":
            from sprint_manager.sources.slack import first_line, render_thread
            existing = next((st for st in state.all_statuses()
                             if st.tracker == "slack" and st.external_ref == ref.strip()), None)
            if existing:
                return {"key": existing.ticket, "existing": True}
            thread = sources.get("slack").fetch(ref.strip())
            key = sources.new_task_id(proj.name, "slack")
            taskfile.write(key, render_thread(thread))
            self._seed_task(key, proj, "slack", title.strip() or first_line(thread),
                            kind or "feature", ref.strip(), ref.strip())
        else:
            raise ValueError(f"Unknown tracker {tracker!r} (Jira issues are added by key/URL).")
        return {"key": key}

    def _seed_task(self, key: str, proj, tracker: str, title: str, kind: str, external_ref: str,
                   external_url: str, meta: dict | None = None) -> None:
        if meta:
            self._meta[key] = meta
            taskfile.write(key, meta.get("description", ""))
        if state.read(key) is None:
            state.update(key, source="manual", project=proj.name, tracker=tracker, summary=title,
                         kind=kind, external_ref=external_ref, external_url=external_url,
                         issue_type=kind.capitalize(), stage=Stage.TODO, activity=Activity.IDLE,
                         note="not started")

    def load_github_issues(self, project: str | None, assignee: str = "@me", label: str = "",
                           milestone: str = "") -> list[str]:
        """Batch intake (the GitHub counterpart of load_sprint): open issues matching the filters
        become tasks. Returns the task keys (existing ones are left untouched)."""
        import json as _json
        import subprocess as _sp

        proj = project_mod.resolve(project) if project else self._default_project()
        args = ["gh", "issue", "list", "--state", "open", "--limit", "100",
                "--json", "number,title,body,labels,state,url,comments"]
        for flag, value in (("--assignee", assignee), ("--label", label), ("--milestone", milestone)):
            if value:
                args += [flag, value]
        out = _sp.run(args, cwd=str(proj.repo), capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise sources.SourceError(f"gh issue list failed: {out.stderr.strip()[:300]}")
        keys = []
        for issue in _json.loads(out.stdout):
            key = sources.new_task_id(proj.name, "github", issue["number"])
            meta = sources.get("github").to_meta(key, issue)
            self._seed_task(key, proj, "github", meta["summary"], meta["type"].lower(),
                            str(issue["number"]), issue.get("url", ""), meta)
            keys.append(key)
        return keys

    def file_as_issue(self, ticket: str) -> dict:
        """Promote a text / Slack task to a GitHub issue in its project's repo (blocking; run in a
        thread). The issue body = the problem statement (for Slack: the rendered thread, link
        included) + the approved acceptance criteria from the notes, if any. The task keeps its id —
        files, worktree and branch keep their names — but becomes ``tracker = github``: from then on
        commits/PR carry ``#<n>`` and the PR body ``Fixes …#<n>``."""
        import subprocess as _sp

        from sprint_manager.sources.github import parse_issue_ref

        stored = state.read(ticket)
        if not stored or stored.tracker not in ("text", "slack"):
            return {"error": "Only a text or Slack task can be filed as a GitHub issue."}
        body = taskfile.read(ticket).strip() or stored.summary
        criteria = notes.latest_heading_block(ticket, "Acceptance criteria")
        if criteria:
            body += f"\n\n### Acceptance criteria\n\n{criteria}"
        proj = self._project(ticket)
        out = _sp.run(["gh", "issue", "create", "--title", stored.summary or ticket, "--body", body],
                      cwd=str(proj.repo), capture_output=True, text=True, timeout=60)
        url = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
        parsed = parse_issue_ref(url)
        if not parsed:
            return {"error": f"gh issue create failed: {(out.stderr or out.stdout).strip()[:300]}"}
        state.update(ticket, tracker="github", external_ref=str(parsed[2]), external_url=url)
        self._meta.pop(ticket, None)
        self._broadcast(ticket, "system", f"Filed as GitHub issue #{parsed[2]}: {url} — commits and "
                                          f"the PR now reference it (the PR will close it on merge).")
        return {"ticket": ticket, "issue": parsed[2], "url": url}

    def update_task_text(self, ticket: str, body: str) -> dict:
        """Replace a text task's problem statement (the next fresh session sees the new text)."""
        stored = state.read(ticket)
        if not stored or stored.tracker != "text":
            return {"error": "Only a text task's problem statement can be edited here."}
        if not body.strip():
            return {"error": "The problem statement can't be empty."}
        taskfile.write(ticket, body)
        self._meta.pop(ticket, None)
        self._broadcast(ticket, "system", "Problem statement updated — a fresh session will see "
                                          "the new text (⟳ Compact to apply it to the current one).")
        return {"ticket": ticket, "updated": True}

    def rehydrate_meta(self) -> None:
        """Best-effort: refresh every persisted ticket's content from its source (blocking; run in
        a thread). Called at startup so the table's tracker-status column repopulates after a
        restart. Unreachable tickets keep their persisted snapshot and retry next time."""
        for status in state.all_statuses():
            try:
                meta = sources.get(status.tracker).load(status, self._project(status.ticket))
            except (sources.SourceError, project_mod.ProjectError, config.ConfigError):
                continue
            self._meta[status.ticket] = meta
            state.update(status.ticket, summary=meta.get("summary", ""),
                         issue_type=meta.get("type", ""), jira_status=meta.get("status", ""))

    # ----- streaming pub/sub ----------------------------------------------------------------

    def subscribe(self, ticket: str) -> asyncio.Queue:
        # Stream only future events. History is loaded by the dashboard via /api/ticket
        # (transcript()) before it opens the socket, so replaying the buffer here would
        # render every past event twice.
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[ticket].add(queue)
        return queue

    def unsubscribe(self, ticket: str, queue: asyncio.Queue) -> None:
        self._subscribers[ticket].discard(queue)

    def transcript(self, ticket: str) -> list[dict]:
        # Full history from disk so it survives restarts (the in-memory buffer does not).
        # Filter to the visible kinds: older files may hold "status"/"result" events, and replaying
        # a stale status snapshot would overwrite fresher poll data in the UI.
        return [e for e in transcript_store.read(ticket) if e.get("kind") in VISIBLE_EVENT_KINDS]

    def _broadcast(self, ticket: str, kind: str, text: str) -> None:
        # Called from worker threads too (asyncio.to_thread paths: meta fetch, file-as-issue …);
        # asyncio.Queue isn't thread-safe, so hop onto the event loop when not already on it.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._broadcast, ticket, kind, text)
                return
        event = {"kind": kind, "text": text}
        # "status" is a live-push signal, not scrollback: never persisted or buffered, so stale
        # snapshots can't accumulate on disk and get replayed over fresh state on tab open.
        if kind != "status":
            self._transcripts[ticket].append(event)
            transcript_store.append(ticket, event)  # persist so the tab can re-render after a restart
        for queue in list(self._subscribers[ticket]):
            queue.put_nowait(event)

    def _broadcast_status(self, status: TicketStatus) -> None:
        """Push a full-state snapshot so the UI updates without waiting for the next poll.

        Sends the LIVE (overlaid) activity — working while running/queued/advancing — not the raw
        persisted value, so the push matches what /api/status would show and the UI flips to working
        the instant a transition starts.
        """
        data = status.to_dict()
        data["activity"] = self._live_activity(status).value
        self._broadcast(status.ticket, "status", json.dumps(data))

    def _begin_transition(self, ticket: str, allow_running: bool = False) -> bool:
        """Mark a ticket as advancing and flip the UI to working immediately. Returns False (and
        ignores the request) if a transition is already in flight — the double-click guard.

        ``allow_running=True`` permits the transition even while a turn is live (go-to-stage is
        meant to interrupt a running turn and jump); it still blocks a duplicate *transition*.
        """
        if ticket in self._advancing or ticket in self._queued or (not allow_running and ticket in self._running):
            self._broadcast(ticket, "system", "Already working — ignoring the duplicate request.")
            return False
        self._advancing.add(ticket)
        stored = state.read(ticket)
        if stored:
            self._broadcast_status(stored)  # activity overlays to working (ticket is now advancing)
        return True

    # ----- public controls ------------------------------------------------------------------

    def start(self, ticket: str) -> None:
        """Press Start: enter explore, resume the current stage, or begin fresh after a stage jump.

        Three cases: (a) to-do/done → enter explore; (b) mid-stage with a stored session → resume
        it with a kickoff; (c) mid-stage with NO session (goto_stage reset it, or a pre-merge stage
        was migrated) → full _enter_stage, so the worktree/Jira prep runs if the target is work and
        the session starts fresh (in pr-open: a fresh triage episode).
        """
        if not self._begin_transition(ticket):
            return
        stored = state.read(ticket)
        if stored and stored.stage not in (Stage.TODO, Stage.DONE):
            if _resumable(stored):
                self._spawn(ticket, self._locked(
                    ticket, self._chat(ticket, _KICKOFF.format(stage=stored.stage.value), "system")))
            else:
                self._spawn(ticket, self._locked(ticket, self._enter_stage(ticket, stored.stage)))
        else:
            self._spawn(ticket, self._locked(ticket, self._enter_stage(ticket, Stage.EXPLORE)))

    def approve(self, ticket: str) -> dict:
        """Press Approve → explore → work (plan approved) or pr-open → done (PR merged).

        work is deliberately NOT advanced by Approve: inside work, "yes" in chat moves between the
        agent's gates, and leaving it means shipping — a distinct, explicit action (``ship``).
        """
        stored = state.read(ticket)
        if stored and stored.stage == Stage.WORK:
            return {"error": "The work stage ends with Ship ▶ (commit check, push, open the PR), "
                             "not Approve. To move between gates inside work, reply in chat."}
        if not self._begin_transition(ticket):
            return {"ignored": ticket}
        self._spawn(ticket, self._locked(ticket, self._advance(ticket)))
        return {"approved": ticket}

    def ship(self, ticket: str) -> dict:
        """Press Ship ▶ (work only): the zero-LLM ship action — see ``_ship``."""
        stored = state.read(ticket)
        if not stored or stored.stage != Stage.WORK:
            return {"error": "Ship is only available in the work stage."}
        if not self._begin_transition(ticket):
            return {"ignored": ticket}
        self._spawn(ticket, self._locked(ticket, self._ship(ticket)))
        return {"shipping": ticket}

    async def trigger_ci(self, ticket: str) -> dict:
        """Press Trigger CI (pr-open only): start a Jenkins build for the ticket's open PR.

        The ONLY way CI starts in this system — agents are permission-blocked from triggering it.
        On success the ticket rests at waiting_external with both poll channels re-armed (so the
        fresh build is detected), and any live triage episode ends (its summary → notes): waiting
        on a new build is exactly the end of an episode. On ``triggered: false`` nothing changes
        but the manager is told why and pointed at the PR's "Trigger Jenkins" check.
        """
        stored = state.read(ticket)
        if not stored or stored.stage != Stage.PR_OPEN:
            return {"error": "Trigger CI is only available in the pr-open stage."}
        if ticket in self._running or ticket in self._queued or ticket in self._advancing:
            return {"error": "The agent is working — wait for it to stop, then trigger CI."}
        try:
            result = await asyncio.to_thread(ci.for_ticket, "trigger", ticket)
        except (config.ConfigError, OSError, subprocess.CalledProcessError,
                project_mod.ProjectError) as exc:
            msg = _friendly_poll_error(exc, "CI")
            self._broadcast(ticket, "system", f"CI trigger failed: {msg}")
            return {"error": f"CI trigger failed: {msg}"}
        if not result.get("triggered"):
            reason = result.get("reason", "unknown reason")
            self._broadcast(ticket, "system",
                f"CI was NOT triggered: {reason} Retry shortly, or start it from the CI system / "
                f"the PR's checks.")
            return {"triggered": False, "reason": reason}
        async with self._agent_locks[ticket]:
            await self._end_episode(ticket)
            final = state.update(ticket, activity=Activity.WAITING_EXTERNAL, ci_fired=False,
                                 review_fired=False, note="CI triggered — waiting on CI + code review")
        self._broadcast(ticket, "system",
            f"CI triggered for PR #{result.get('pr')}. Polling CI + code review every "
            f"{config.PR_POLL_SECONDS // 60} min.")
        self._broadcast_status(final)
        return {"triggered": True, "pr": result.get("pr")}

    def chat(self, ticket: str, message: str) -> None:
        """A manager chat message → iterate within the current stage's session."""
        self._spawn(ticket, self._locked(ticket, self._chat(ticket, message, "user")))

    def rearm_review(self, ticket: str) -> dict:
        """Re-arm the code-review poll channel: forget that a review signal fired so polling
        resumes until the NEXT comment/approval/rejection.

        For dismissing a useless review event (e.g. CodeRabbit "ran out of tokens, can't review").
        Only ``review_fired`` is reset — the watermarks (``pr_comment_count``/``review_decision``)
        are kept, so the comment we just saw does not immediately re-fire; the next genuinely new
        one does. The ticket returns to ``waiting_external``: dismissing a review event usually means
        there's nothing to do in response and we're waiting for a more useful review. No agent turn.
        """
        stored = state.read(ticket)
        if not stored or stored.stage != Stage.PR_OPEN:
            return {"error": "Re-arm is only available while a ticket is in the pr-open stage."}
        if ticket in self._running or ticket in self._queued or ticket in self._advancing:
            return {"error": "The agent is working — wait for it to stop, then dismiss the review event."}
        # A fired CI result (e.g. a failure) still needs you: dismissing the REVIEW event must not
        # demote the ticket to "waiting" and hide it.
        fields = {"review_fired": False}
        if not stored.ci_fired:
            fields["activity"] = Activity.WAITING_EXTERNAL
        final = state.update(ticket, **fields)
        self._broadcast(ticket, "system",
            "Code-review channel re-armed — polling continues until the next comment/approval/rejection.")
        self._broadcast_status(final)
        # Back to waiting_external = the end of any live triage episode (nothing to act on).
        if final.activity == Activity.WAITING_EXTERNAL and (ticket in self._agents or final.session_id):
            self._spawn(ticket, self._locked(ticket, self._end_episode(ticket)))
        return {"ticket": ticket, "review_fired": False}

    def _spawn(self, ticket: str, coro) -> None:
        """Launch a control coroutine with a guaranteed cleanup backstop.

        Any unhandled exception in a fire-and-forget task would otherwise be swallowed by asyncio,
        leaving the ticket's transient bookkeeping dangling. This callback surfaces the error to the
        transcript and clears the in-memory live sets — so even an unforeseen crash path cannot
        leave a ticket appearing to work. (The read-time overlay would mask it anyway; this also
        heals the source.)
        """
        self._loop = self._loop or asyncio.get_running_loop()
        task = asyncio.create_task(coro)

        def _done(t: asyncio.Task) -> None:
            # Always clear the advancing flag on completion — a transition that never reached
            # _run_turn (e.g. approve on a done ticket, or an early return) must not stay "working".
            self._advancing.discard(ticket)
            if t.cancelled():
                self._running.discard(ticket)
                self._queued.discard(ticket)
                return
            exc = t.exception()
            if exc is not None:
                self._running.discard(ticket)
                self._queued.discard(ticket)
                self._broadcast(ticket, "system",
                    f"⚠️ Turn ended with an internal error: {exc!r}. Returned to you — re-send to retry.")

        task.add_done_callback(_done)

    async def _locked(self, ticket: str, coro) -> None:
        """Run one transition/chat coroutine for ``ticket`` under its per-ticket lock.

        Every public entry point that can touch ``self._agents[ticket]`` or decide whether to
        reattach (start/approve/chat/compact/goto_stage, plus the CI-failure auto-triage chat)
        goes through this, so a chat can never interleave with an in-flight stage transition — see
        ``_agent_locks`` above for why that matters.
        """
        async with self._agent_locks[ticket]:
            await coro

    def _live_activity(self, status: TicketStatus) -> Activity:
        """The ground-truth activity for a ticket, reconciling persisted state against live turns.

        A ticket can only be working/queued if THIS process is actually running/queuing it (the
        in-memory sets). Any persisted transient value with no backing live turn — from a crash, a
        restart, or the agent's own stale report_stage write — resolves deterministically to
        waiting_user. No timeouts, no heuristics: it is reconciliation against ground truth, which
        is why a stuck state cannot occur rather than merely being detected later.
        """
        if status.ticket in self._running or status.ticket in self._advancing:
            return Activity.WORKING
        if status.ticket in self._queued:
            return Activity.QUEUED
        if status.activity in TRANSIENT_ACTIVITIES:
            return Activity.WAITING_USER
        return status.activity

    def reconcile_activity(self, status: TicketStatus) -> Activity:
        """Return the live activity and lazily heal a STALE persisted transient state.

        Called from the status read path (every UI poll). We heal only the one direction that
        matters for correctness: a persisted working/queued that no live turn backs (→ waiting_user).
        We never write working/queued back from the overlay — transient liveness stays in-memory
        only — so a live turn produces no disk churn and a crash leaves nothing transient to inherit.
        """
        live = self._live_activity(status)
        if status.activity in TRANSIENT_ACTIVITIES and live not in TRANSIENT_ACTIVITIES:
            state.update(status.ticket, activity=live)
        return live

    def reconcile_all(self) -> list[str]:
        """Startup reconciliation: normalize every persisted transient state to its live value.

        At process start the live sets are empty, so any persisted working/queued is by definition
        dead and resolves to waiting_user. Returns the tickets it healed (for logging).
        """
        healed = []
        for s in state.all_statuses():
            if s.activity in TRANSIENT_ACTIVITIES and self._live_activity(s) != s.activity:
                state.update(s.ticket, activity=Activity.WAITING_USER)
                healed.append(s.ticket)
        return healed

    def compact(self, ticket: str) -> None:
        """Mid-stage compact: save the last summary to notes and restart the stage with a fresh session."""
        self._spawn(ticket, self._locked(ticket, self._compact(ticket)))

    async def _compact(self, ticket: str) -> None:
        stored = state.read(ticket)
        if not stored or stored.stage in (Stage.TODO, Stage.DONE):
            self._broadcast(ticket, "system", "Nothing to compact (ticket not in an active stage).")
            return
        stage = stored.stage
        self._broadcast(ticket, "system",
            f"Compacting {stage.value} — trimming exploration, keeping the actual work, then "
            f"continuing (not restarting) with a fresh session.")
        await self._recap_then_dispose(ticket, f"{stage.value} — mid-stage compact", "Compacting")
        await self._enter_stage(ticket, stage, kickoff=_COMPACT_KICKOFF.format(stage=stage.value))

    def goto_stage(self, ticket: str, stage: Stage) -> None:
        """Jump to any working stage: dispose the current session and reset for a fresh start."""
        # allow_running: a jump may interrupt a live turn (that's the point) — but a duplicate jump
        # while one is already in flight is ignored, and the UI flips to working instantly.
        if not self._begin_transition(ticket, allow_running=True):
            return
        self._spawn(ticket, self._locked(ticket, self._goto_stage(ticket, stage)))

    async def _goto_stage(self, ticket: str, stage: Stage) -> None:
        # Interrupt in-flight turn if one is running.
        agent = self._agents.get(ticket)
        if agent and ticket in self._running:
            try:
                await agent.interrupt()
            except Exception:  # noqa: BLE001
                pass
        # Carry the departing session's findings into the notes — jumping backward is usually
        # BECAUSE of what that session found (a CI failure analysis, a review verdict), and the
        # target stage needs it.
        stored = state.read(ticket)
        old = stored.stage.value if stored else "?"
        await self._recap_then_dispose(
            ticket, f"{old} — summary (before jump to {stage.value})", "Jumping stage")
        # Clear the advancing flag BEFORE the final broadcast so the ticket lands on idle, not the
        # working overlay — idle is what makes the panel show Start ▶ instead of Approve (pressing
        # Approve here would advance PAST the stage we just jumped to).
        self._advancing.discard(ticket)
        final = state.update(ticket, stage=stage, activity=Activity.IDLE, session_id="",
                             context_tokens=0, peak_context_tokens=0,
                             note=f"Stage reset to {stage.value} by manager.")
        self._broadcast(ticket, "system",
            f"Stage reset to {stage.value}. Click Start ▶ to begin the fresh session.")
        self._broadcast_status(final)

    def interrupt(self, ticket: str) -> None:
        """Interrupt the ticket's in-flight turn, if one is running (no-op otherwise)."""
        agent = self._agents.get(ticket)
        if agent is not None:
            self._spawn(ticket, self._interrupt(ticket, agent))

    async def _interrupt(self, ticket: str, agent: TicketAgent) -> None:
        try:
            await agent.interrupt()
            self._broadcast(ticket, "system", "Interrupted by you.")
        except Exception as exc:  # noqa: BLE001 - nothing running / transport race; just report
            self._broadcast(ticket, "system", f"Interrupt failed: {exc}")

    async def remove_ticket(self, ticket: str) -> dict:
        """Remove a ticket from the dashboard: dispose its agent and delete its persisted files.

        Deliberately does NOT touch the git worktree/branch — it may hold uncommitted work — so the
        worktree path (if any) is returned for the caller to surface and the user to clean manually.
        """
        await self._dispose(ticket)
        stored = state.read(ticket)
        worktree_path = stored.worktree if stored else ""
        state.delete(ticket)
        transcript_store.delete(ticket)
        notes.delete(ticket)
        taskfile.delete(ticket)
        self._meta.pop(ticket, None)
        self._last_summary.pop(ticket, None)
        self._transcripts.pop(ticket, None)
        self._subscribers.pop(ticket, None)
        self._stage_base.pop(ticket, None)
        for key in [k for k in self._poll_error_seen if k[0] == ticket]:
            self._poll_error_seen.pop(key, None)
        return {"removed": ticket, "worktree": worktree_path}

    # ----- stage entry / advance (compaction) ----------------------------------------------

    async def _enter_stage(self, ticket: str, stage: Stage, kickoff: str | None = None,
                           origin: str = "system") -> None:
        """Start a fresh session for ``stage`` (worktree/Jira prep happens inside _attach).

        ``kickoff`` overrides the default "begin the stage" prompt — used by compact, which needs
        the fresh session to CONTINUE the existing work from notes rather than start the stage over,
        by pr-open, whose every fresh session is a triage episode (``_episode_kickoff``), and by a
        chat to a ticket with no session (the manager's message is the kickoff).

        The first message = the ticket context + the stage's notes view, then the kickoff. That is
        the ONLY place the per-ticket context enters a session: the system prompt is ticket-agnostic
        so it caches across sessions (see agent.build_system_prompt). The transcript shows only the
        kickoff — the context block is large and the manager already has it.
        """
        # A tracker-backed task (GitHub issue / Slack thread) may have changed since it was loaded —
        # new comments, replies — so every fresh session re-reads it.
        stored = state.read(ticket)
        if stored and stored.tracker in ("github", "slack"):
            self._meta.pop(ticket, None)
        if kickoff is None and stage == Stage.PR_OPEN:
            kickoff = await self._episode_kickoff(ticket, None)
        await self._attach(ticket, stage, resume=False)
        # Persist only the durable fields (stage, fresh session). Liveness comes from _run_turn's
        # in-memory _running set — never written to disk — so an interruption here can't strand a
        # working state. waiting_user is the durable resting baseline if the turn never starts.
        state.update(ticket, stage=stage, activity=Activity.WAITING_USER, session_id="",
                     session_format=SESSION_FORMAT, context_tokens=0, peak_context_tokens=0)
        kickoff = kickoff or _KICKOFF.format(stage=stage.value)
        context = build_context_message(await self._ensure_meta(ticket), notes.view(ticket, stage),
                                        state.read(ticket))
        await self._run_turn(ticket, f"{context}\n\n---\n\n{kickoff}", origin=origin,
                             display=kickoff)

    async def _advance(self, ticket: str) -> None:
        """Compact the finished stage into the notes, then enter the next stage (or finish).

        explore → work, or pr-open → done (which flips the Jira issue to "Done"). work never comes
        through here — it leaves via ``_ship``, which owns the "In Review" side effects.
        """
        stored = state.read(ticket)
        if not stored or stored.stage == Stage.WORK:
            return
        await self._recap_then_dispose(ticket, f"{stored.stage.value} — summary", "Advancing")
        nxt = next_stage(stored.stage)
        if nxt == Stage.DONE:
            await self._hook(ticket, "on_done")
            self._advancing.discard(ticket)
            # session_id cleared: a done task has no session to resume (see _chat).
            final = state.update(ticket, stage=Stage.DONE, activity=Activity.IDLE, note="done",
                                 session_id="")
            self._broadcast(ticket, "system", "Marked done.")
            self._broadcast_status(final)
            return
        await self._enter_stage(ticket, nxt)

    async def _save_summary_and_dispose(self, ticket: str, title: str) -> None:
        """The shared half of every stage transition: persist the departing session's summary to
        the notes (skipped when empty — no blank sections) and dispose the session."""
        # pop, not get: a later transition whose session never ran a turn must not re-save this.
        summary = self._last_summary.pop(ticket, "")
        if summary:
            notes.append_section(ticket, title, summary)
        await self._dispose(ticket)

    async def _recap_then_dispose(self, ticket: str, title: str, reason: str) -> None:
        """Ask the CURRENT session for one comprehensive recap before saving it to notes and
        disposing it — unless it only ever had a single turn, whose own summary already IS the
        complete picture (no point spending a second turn asking it to restate itself).

        Shared by every transition that hands a session off to a fresh one: compact, Approve, and
        "go to stage" jumps. Without this, the notes only got whatever the agent's most recent turn
        happened to summarize — often a terse delta ("addressed your feedback on step 3"), not the
        actual state. Confirmed for real: a normal Approve on a live ticket once carried forward a
        one-line nitpick and dropped a 7-point plan that only the live conversation still had.
        """
        agent = self._agents.get(ticket)
        if agent and agent.total_turns > 1:
            self._broadcast(ticket, "system", f"{reason} — asking the current session for a full recap first.")
            await self._run_turn(ticket, _RECAP_PROMPT, origin="system")
            # _run_turn released the "advancing" marker when the recap turn began; the rest of the
            # transition (dispose, worktree, Jira, connect) is still in flight — keep the UI on
            # working and keep refusing duplicate Approve/Start/Compact until it's done.
            self._advancing.add(ticket)
        await self._save_summary_and_dispose(ticket, title)

    async def _chat(self, ticket: str, message: str, origin: str) -> None:
        """Send a message to the current stage's session, reattaching it if needed (post-restart).

        With no session to resume, a message starts a FRESH one through ``_enter_stage`` (so it
        gets the ticket context — a bare attach would leave the agent without it). In pr-open that
        fresh session is a triage episode whose kickoff carries the latest poll signal, the branch
        state, and this message.
        """
        stored = state.read(ticket)
        if stored and stored.stage == Stage.DONE:
            # A done task has no stage to work in; resuming its last session would put a
            # write-enabled agent in the main checkout.
            self._broadcast(ticket, "system", "This task is done. To do more work on it, use "
                                              "↩ go to stage (explore / work), then Start ▶.")
            return
        if ticket not in self._agents:
            stage = stored.stage if stored and stored.stage != Stage.TODO else Stage.EXPLORE
            if not (stored and _resumable(stored)):
                if stage == Stage.PR_OPEN:
                    self._broadcast(ticket, "system", "Starting a fresh pr-open triage episode.")
                    message = await self._episode_kickoff(ticket, message)
                await self._enter_stage(ticket, stage, kickoff=message, origin=origin)
                await self._maybe_end_episode(ticket)
                return
            await self._attach(ticket, stage, resume=True)
        await self._run_turn(ticket, message, origin=origin)
        await self._maybe_end_episode(ticket)

    # ----- ship (zero-LLM action) + pr-open triage episodes -----------------------------------

    async def _ship(self, ticket: str) -> None:
        """Leave work: commit check → final recap (+ PR title/body) → push → open PR → pr-open.

        Every step is idempotent (push is a plain push; ``pr.open_pr`` returns an already-open PR's
        URL), so any failure leaves the ticket in work with its session intact and the manager just
        presses Ship ▶ again. The work session is disposed only once the PR exists. CI is NOT
        started — the manager does that with Trigger CI.
        """
        try:
            stored = state.read(ticket)
            if not stored or stored.stage != Stage.WORK:
                return
            branch = await asyncio.to_thread(worktree.branch_state, ticket)
            if not branch.get("ok"):
                self._broadcast(ticket, "system", f"Can't ship: {branch.get('error')}")
                return
            if branch["dirty"]:
                listing = "\n".join(f"  {path}" for path in branch["dirty"][:20])
                self._broadcast(ticket, "system",
                    "Can't ship — the worktree has uncommitted changes. Ask the agent to commit "
                    f"(or discard) them, then press Ship ▶ again:\n{listing}")
                return
            if branch["ahead"] == 0:
                self._broadcast(ticket, "system",
                    f"Can't ship — the branch has no commits beyond "
                    f"origin/{self._project(ticket).base_branch}.")
                return
            # The recap needs a live session; after a restart it's only on disk, so resume it.
            if ticket not in self._agents and _resumable(stored):
                await self._attach(ticket, Stage.WORK, resume=True)
            if ticket in self._agents:
                self._broadcast(ticket, "system",
                    "Shipping — asking the work session for a final recap + PR title/body.")
                ref = sources.get(stored.tracker).reference(stored)
                await self._run_turn(ticket, _SHIP_RECAP_PROMPT.format(
                    title_prefix=f"{ref}: " if ref else ""), origin="system")
                self._advancing.add(ticket)  # keep the UI on working through push/open
            recap = self._last_summary.get(ticket, "")
            meta = await self._ensure_meta(ticket)
            src = sources.get(stored.tracker)
            title, body = _parse_pr_block(recap, src.reference(stored), meta.get("summary", ""))
            footer = src.pr_body_footer(stored)
            if footer and footer not in body:
                body = f"{body}\n\n{footer}"
            try:
                await asyncio.to_thread(pr.push, ticket)
                url = await asyncio.to_thread(pr.open_pr, ticket, title, body)
            except (subprocess.CalledProcessError, OSError) as exc:
                detail = (getattr(exc, "stderr", "") or str(exc)).strip()
                self._broadcast(ticket, "system",
                    f"Ship failed at push / open PR — still in work, session kept. Fix and press "
                    f"Ship ▶ again.\n{detail}")
                return
            await self._save_summary_and_dispose(ticket, "work — summary")
            # new_pr=False on a re-ship to the same PR after a jump back: don't re-announce it.
            await self._hook(ticket, "on_shipped", url, url != stored.pr_url)
            proj = self._project(ticket)
            auto, no_ci = ci.auto_triggers(proj), ci.provider(proj) == "none"
            # Every ship arms the review channel afresh. CI: a push already started it (auto) →
            # armed; no CI at all → marked done, so polling stops once review has reported;
            # manual CI → armed, waiting for Trigger CI.
            fields = {"ci_fired": no_ci, "review_fired": False}
            if auto or no_ci:
                fields.update(activity=Activity.WAITING_EXTERNAL,
                              note="PR open — CI running on push" if auto else "PR open — waiting on code review")
            else:
                fields.update(activity=Activity.WAITING_USER, note="PR open — press Trigger CI when ready")
            state.update(ticket, stage=Stage.PR_OPEN, pr_url=url, session_id="", last_signal="",
                         context_tokens=0, peak_context_tokens=0, **fields)
            self._broadcast(ticket, "system", f"PR open: {url}\n" + (
                "CI starts automatically on push — watching CI + code review." if auto else
                "This project has no CI — watching code review." if no_ci else
                "Press **Trigger CI** when you're ready for a build."))
        finally:
            self._advancing.discard(ticket)
            final = state.read(ticket)
            if final:
                self._broadcast_status(final)

    async def _episode_kickoff(self, ticket: str, message: str | None) -> str:
        """The opening message of a pr-open triage episode: the latest signal, the PR, and a compact
        picture of the branch (commit log + diffstat vs the base branch) — enough to orient a fresh session
        without replaying the implementation history — plus the manager's message, if any."""
        stored = state.read(ticket)
        try:
            branch = await asyncio.to_thread(worktree.branch_state, ticket)
        except (OSError, subprocess.SubprocessError) as exc:
            branch = {"ok": False, "error": str(exc)}
        parts = [
            _EPISODE_KICKOFF,
            f"**Latest signal:** {(stored.last_signal if stored else '') or '(none recorded yet)'}",
            f"**PR:** {(stored.pr_url if stored else '') or '(unknown)'}",
        ]
        if branch.get("ok"):
            diffstat = "\n".join(branch["diffstat"].splitlines()[-60:])  # keep the summary line
            base = self._project(ticket).base_branch
            parts.append(f"**Branch commits vs {base}:**\n```\n{branch['log'] or '(none)'}\n```")
            parts.append(f"**Diffstat vs {base}:**\n```\n{diffstat or '(empty)'}\n```")
        else:
            parts.append(f"(Branch state unavailable: {branch.get('error')})")
        parts.append(f"**Manager's message:**\n{message}" if message
                     else "Handle the latest signal per your instructions, then stop.")
        return "\n\n".join(parts)

    async def _maybe_end_episode(self, ticket: str) -> None:
        """End the triage episode if the turn left a pr-open ticket resting at waiting_external."""
        stored = state.read(ticket)
        if (stored and stored.stage == Stage.PR_OPEN
                and stored.activity == Activity.WAITING_EXTERNAL and ticket in self._agents):
            await self._end_episode(ticket)

    async def _end_episode(self, ticket: str) -> None:
        """Close the live pr-open triage session: its last summary → notes, dispose, clear the id.

        No extra recap turn (unlike stage transitions): an episode is short and single-purpose, so
        its last summary already is the whole story. Called under the ticket's lock.
        """
        if ticket in self._agents:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            await self._save_summary_and_dispose(ticket, f"pr-open — triage episode ({stamp})")
        state.update(ticket, session_id="", context_tokens=0, peak_context_tokens=0)

    async def _ensure_worktree(self, ticket: str) -> None:
        """Create the branch + worktree and move the Jira issue to In Progress (once)."""
        stored = state.read(ticket)
        if stored and stored.worktree:
            return
        meta = await self._ensure_meta(ticket)  # fetch first so the branch name gets a real summary
        stored = state.read(ticket)
        kind = stored.kind if stored and stored.kind else (
            "bug" if (meta.get("type") or "").lower() == "bug" else "feature")
        # git fetch + worktree add can take a while (and hang off-VPN): never on the event loop.
        tree = await asyncio.to_thread(worktree.add, ticket, kind, meta.get("summary", ""))
        state.update(ticket, branch=tree["branch"], worktree=tree["path"])
        self._broadcast(ticket, "system", f"Created worktree {tree['path']} on {tree['branch']}")
        await self._hook(ticket, "on_work_start")

    async def _hook(self, ticket: str, hook: str, *args) -> None:
        """Run a task-source lifecycle hook (on_work_start / on_shipped / on_done) best-effort, in a
        worker thread (tracker HTTP / gh calls block for seconds): report what it did, or why it was
        skipped — never raise (tracker updates aren't load-bearing)."""
        tracker = "tracker"
        try:
            stored = state.read(ticket)
            tracker = stored.tracker if stored else "tracker"
            fn = getattr(sources.get(tracker), hook)
            message = await asyncio.to_thread(fn, stored, self._project(ticket), *args)
        except (sources.SourceError, project_mod.ProjectError, config.ConfigError) as exc:
            self._broadcast(ticket, "system", f"{tracker} update skipped: {exc}")
            return
        if message:
            self._broadcast(ticket, "system", message)

    async def _attach(self, ticket: str, stage: Stage, resume: bool) -> None:
        """Build and connect a stage-scoped agent (fresh, or resuming the stored session).

        Worktree provisioning lives HERE, not in _enter_stage, so EVERY path that attaches a
        worktree-stage agent (Start, chat after a restart, chat after a goto-stage jump) is
        guaranteed an isolated cwd — a write-enabled agent must never fall back to the main checkout.
        """
        if uses_worktree(stage):
            await self._ensure_worktree(ticket)
        model, effort = model_for_stage(stage)
        stored = state.read(ticket)
        # Seed the cumulative baselines (cost + turns) to the ticket's totals so far. The new agent
        # starts at 0 and only tracks THIS stage, so persisted total = baseline + agent total,
        # accumulating across stages. (On resume after a restart, the stored totals already include
        # this stage's pre-restart turns and the resumed agent starts at 0, so no double-count.)
        self._stage_base[ticket] = {
            "cost": stored.cost_usd if stored else 0.0,
            "turns": stored.total_turns if stored else 0,
        }
        proj = self._project(ticket)
        cwd = stored.worktree if (stored and uses_worktree(stage) and stored.worktree) else str(proj.repo)
        agent = TicketAgent(
            ticket=ticket,
            system_prompt=build_system_prompt(stage, proj),
            cwd=cwd,
            model=model,
            effort=effort,
            max_turns=max_turns_for(stage),
            read_only=stage in READ_ONLY_STAGES,
            resume_session_id=(stored.session_id or None) if (resume and stored) else None,
            skills=proj.skills,
            strict_mcp=stage not in MCP_OPEN_STAGES,
            worktree=(stored.worktree if stored else "") or "",
            project=proj,
            ref=sources.get(stored.tracker).reference(stored) if stored else "",
            stage=stage.value,
        )
        await agent.connect()
        self._agents[ticket] = agent

    async def _dispose(self, ticket: str) -> None:
        agent = self._agents.pop(ticket, None)
        if agent is not None:
            try:
                await agent.disconnect()
            except Exception:  # noqa: BLE001 - disconnect is best-effort cleanup
                pass

    async def _ensure_meta(self, ticket: str) -> dict:
        """Return the ticket's Jira metadata for the prompt, fetching from Jira if not cached.

        The agent must ALWAYS know its ticket — so even if the sprint was never loaded (or the
        server restarted), we fetch the issue directly. Falls back to the stored summary if Jira is
        unreachable. The current branch/worktree are merged in from the status store.
        """
        meta = self._meta.get(ticket)
        if not meta or meta.get("description") is None:
            meta = await asyncio.to_thread(self._fetch_meta, ticket)
            self._meta[ticket] = meta
        meta = dict(meta)
        stored = state.read(ticket)
        if stored:
            meta["worktree"] = stored.worktree
            meta["branch"] = stored.branch
        return meta

    def _fetch_meta(self, ticket: str) -> dict:
        """Load the task from its source (caching tracker content in the taskfile); fall back to the
        stored summary + the cached taskfile text on any error."""
        stored = state.read(ticket)
        try:
            meta = sources.get(stored.tracker if stored else "").load(stored, self._project(ticket))
            if stored and stored.tracker in ("github", "slack"):
                taskfile.write(ticket, meta.get("description", ""))
            return meta
        except (sources.SourceError, project_mod.ProjectError, config.ConfigError) as exc:
            self._broadcast(ticket, "system", f"Couldn't load the task details ({exc}); using stored info.")
            return {
                "key": ticket,
                "type": stored.issue_type if stored else "",
                "summary": stored.summary if stored else "",
                "url": stored.external_url if stored else "",
                "description": taskfile.read(ticket),
                "comments": [],
            }

    async def _run_turn(self, ticket: str, prompt: str, origin: str = "user",
                        display: str | None = None) -> None:
        """Run one turn of the current stage's agent, streaming summaries and persisting cost/session.

        ``display`` is what the transcript shows for ``prompt`` when they differ (a session's first
        message carries the whole ticket context + notes; the manager sees only the kickoff).

        Transient liveness (working/queued) lives ONLY in the in-memory _running/_queued sets — it
        is never persisted, so a crash or restart cannot leave it behind. The status read path
        overlays these sets (reconcile_activity) to render working/queued. The whole body runs under
        try/finally so every exit path — normal, exception, interrupt, cancellation — clears the
        live sets and parks the ticket at a durable resting state.
        """
        # Echo your input immediately, BEFORE we (might) block on the single global turn slot — so
        # you see it land even while another ticket's agent is mid-turn. Marking queued is a pure
        # in-memory set add (FIFO serial mode); the read overlay turns it into the UI's "queued".
        self._broadcast(ticket, origin, prompt if display is None else display)
        self._queued.add(ticket)
        self._advancing.discard(ticket)  # the turn now owns liveness (queued/running supersede it)
        if self._turn_lock.locked():
            self._broadcast(ticket, "system", "Queued — another ticket's agent is working; this runs next.")
        try:
            async with self._turn_lock:  # enforces MAX_ACTIVE
                self._queued.discard(ticket)
                agent = self._agents.get(ticket)
                if agent is None:
                    return
                self._running.add(ticket)
                last_sid = {"value": None}
                text_blocks: list[str] = []

                def on_event(kind: str, text: str) -> None:
                    # Persist the session id only while this agent is still the ticket's CURRENT
                    # agent — a goto-stage/compact disposes it mid-turn, and a late write here
                    # would resurrect the old session over the reset's session_id="".
                    if (agent.session_id and agent.session_id != last_sid["value"]
                            and self._agents.get(ticket) is agent):
                        last_sid["value"] = agent.session_id
                        state.update(ticket, session_id=agent.session_id)
                    if kind == "text":
                        text_blocks.append(text)  # accumulate the full turn's prose for the notes handoff
                    if kind in VISIBLE_EVENT_KINDS:
                        self._broadcast(ticket, kind, text)

                result = await agent.send(prompt, on_event=on_event)
                if result is not None:
                    # The compaction handoff (notes) depends on this. Do NOT use result.result alone:
                    # it holds only the agent's LAST text block, so an agent that emits its
                    # "### Summary" and then a trailing line ("please approve") would carry only the
                    # trailing line to the next stage — the summary (and any manager answers in it)
                    # is lost. Capture from the last "### Summary" heading onward instead.
                    self._last_summary[ticket] = _summary_from_blocks(text_blocks, result.result)
                if result is not None and self._agents.get(ticket) is agent:  # same stale-write guard
                    base = self._stage_base.get(ticket, {"cost": 0.0, "turns": 0})
                    state.update(
                        ticket,
                        cost_usd=base["cost"] + agent.total_cost_usd,
                        session_id=agent.session_id or "",
                        total_turns=base["turns"] + agent.total_turns,
                        input_tokens=agent.input_tokens,
                        output_tokens=agent.output_tokens,
                        cache_read_tokens=agent.cache_read_tokens,
                        cache_write_tokens=agent.cache_write_tokens,
                        context_tokens=agent.context_tokens,
                        peak_context_tokens=agent.peak_context_tokens,
                    )
                    # Emit a per-turn diagnostic so it appears in the transcript scrollback.
                    total_input = agent.input_tokens + agent.cache_read_tokens + agent.cache_write_tokens
                    cache_pct = (
                        round(100 * agent.cache_read_tokens / total_input)
                        if total_input else 0
                    )
                    self._broadcast(ticket, "system", (
                        f"[diag] turns={agent.total_turns} in={agent.input_tokens} "
                        f"out={agent.output_tokens} cache_read={agent.cache_read_tokens} "
                        f"cache_write={agent.cache_write_tokens} cache_hit={cache_pct}% "
                        f"context={agent.context_tokens} peak={agent.peak_context_tokens}"
                    ))
                    # Detect cache anomalies: threshold-based (>1M) and ratio-based (2x+ growth).
                    current = state.read(ticket)
                    if current is not None:
                        self._check_cache_anomaly(
                            ticket, current.stage, agent.total_turns, agent.cache_read_tokens)
                        self._maybe_hint_compact(ticket, current, agent)
        finally:
            # Single cleanup point for EVERY exit path. Clear the live sets, then park the ticket at
            # a durable resting state: leave a durable activity the agent set itself (e.g.
            # waiting_external after opening a PR) untouched, but normalize any leftover transient
            # value to waiting_user so the manager regains control. Guard the ticket being removed.
            self._running.discard(ticket)
            self._queued.discard(ticket)
            settled = state.read(ticket)
            if settled and settled.activity in TRANSIENT_ACTIVITIES:
                settled = state.update(ticket, activity=Activity.WAITING_USER)
            # Re-arm the pr-open poll: declaring waiting_external means "I'm waiting on fresh
            # external signals again" (e.g. the manager just approved triggering a fresh CI build,
            # or a new review round is expected — CI never auto-triggers on a push by itself).
            # Reset the fired flags so polling resumes; the watermarks stay, so a genuinely new
            # build/comment fires but a stale one does not.
            if (settled and settled.stage == Stage.PR_OPEN
                    and settled.activity == Activity.WAITING_EXTERNAL
                    and (settled.ci_fired or settled.review_fired)):
                settled = state.update(ticket, ci_fired=False, review_fired=False)
            # Push the fully-settled state to the UI so stage/activity changes made by the agent
            # (via report_stage Bash calls) are reflected immediately — not waiting for the next poll.
            if settled:
                self._broadcast_status(settled)

    def _maybe_hint_compact(self, ticket: str, current: TicketStatus, agent: TicketAgent) -> None:
        """Once per work session: when the agent stops at its test-plan gate (note "G2 …") with a
        large context (config.CONTEXT_WARN_TOKENS), suggest ⟳ Compact — the natural boundary where
        implementation's exploration noise stops being useful. A hint only; the manager decides."""
        if (current.stage == Stage.WORK and current.note.startswith("G2")
                and agent.session_id and agent.session_id not in self._compact_hinted
                and agent.context_tokens > config.CONTEXT_WARN_TOKENS):
            self._compact_hinted.add(agent.session_id)
            self._broadcast(ticket, "system",
                f"💡 Good moment to ⟳ Compact before testing: every call in this session now "
                f"re-reads {agent.context_tokens // 1000}k tokens of context. Compact keeps the work "
                f"and the test plan, drops the implementation exploration.")

    def _check_cache_anomaly(self, ticket: str, stage: Stage, turn: int, cache_read: int) -> None:
        """Detect cache growth anomalies: threshold-based (>1M) and ratio-based (2x+ growth).

        Alerts the manager via a system message when cache is unusual for the current stage.
        """
        stage_key = stage.value
        history = self._cache_history.setdefault(ticket, {})

        # Check threshold-based anomaly: a single session past its stage's alert line
        if cache_read > _CACHE_ALERT_TOKENS.get(stage, _CACHE_ALERT_DEFAULT):
            self._broadcast(ticket, "system",
                f"⚠️ CACHE ALERT: {cache_read / 1_000_000:.1f}M tokens cached in {stage.value} "
                f"(turn {turn}). Large cache may indicate repeated iterations. "
                f"Consider ⟳ Compact (keeps the work, drops exploration noise).")

        # Check ratio-based anomaly: 2x growth from the previous reading
        if stage_key in history:
            prev_turn, prev_cache = history[stage_key]
            if prev_cache > 0 and cache_read > prev_cache * 2:
                growth = (cache_read / prev_cache)
                self._broadcast(ticket, "system",
                    f"⚠️ CACHE GROWTH: cache grew {growth:.1f}x from turn {prev_turn} to {turn} "
                    f"in {stage.value} ({prev_cache / 1_000_000:.1f}M → {cache_read / 1_000_000:.1f}M). "
                    f"Rapid cache growth suggests repeated context expansion (plan iterations). "
                    f"Consider ⟳ Compact to break the cycle.")

        # Update history with current reading
        history[stage_key] = (turn, cache_read)

    # ----- pr-open dual-channel poll (CI verdict + code review, every PR_POLL_SECONDS) -------

    def start_polling(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())
        if self._bg_poll_task is None:
            self._bg_poll_task = asyncio.create_task(self._bg_job_poll_loop())

    async def _poll_loop(self) -> None:
        """Every PR_POLL_SECONDS, check each pr-open ticket's two feedback channels (CI + review).

        Gate: stage is pr-open, the ticket is resting (waiting_external/waiting_user — never while a
        turn is live), and NOT both channels have already fired this wait-cycle. That last clause is
        what implements "keep polling until BOTH fired, then stop" — the flags are re-armed when the
        ticket next enters waiting_external (see _run_turn's finally). The per-ticket check is
        exception-guarded so one bad ticket can't kill the loop.
        """
        while True:
            await asyncio.sleep(config.PR_POLL_SECONDS)
            for status in state.all_statuses():
                if status.stage != Stage.PR_OPEN:
                    continue
                if status.activity not in (Activity.WAITING_EXTERNAL, Activity.WAITING_USER):
                    continue
                if self._ci_done(status) and status.review_fired:
                    continue  # both channels already reported — stopped until re-armed
                try:
                    await self._check_external(status.ticket)
                    self._poll_error_seen.pop((status.ticket, "general"), None)
                except Exception as exc:  # noqa: BLE001 - keep polling the other tickets
                    self._report_poll_error(status.ticket, "general", "Poll error", "the network", exc)

    def _report_poll_error(self, ticket: str, channel: str, prefix: str, service: str,
                            exc: BaseException) -> None:
        """Broadcast a poll failure, translated to something actionable — but only once per outage.

        A dropped VPN makes every poll fail identically for as long as it's down; without this,
        every ticket in pr-open would repeat the same line to the transcript every PR_POLL_SECONDS
        for hours. Only re-notify when the message actually changes (a different failure, or the
        SAME failure recurring after a success cleared it — see the ``.pop(...)`` calls at the
        success sites in ``_check_external``/``_poll_loop``).
        """
        msg = _friendly_poll_error(exc, service)
        key = (ticket, channel)
        if self._poll_error_seen.get(key) == msg:
            return
        self._poll_error_seen[key] = msg
        self._broadcast(ticket, "system", f"{prefix}: {msg}")

    async def _check_external(self, ticket: str) -> None:
        """Probe both PR feedback channels and, on any NEW signal, flip to waiting_user and report.

        Semantics (per design): each channel fires on ANY change vs its watermark — CI on a new
        terminal verdict (new run id or changed verdict); review on a higher comment count OR a
        changed decision. A fire flips the ticket to waiting_user (supplementing, never replacing,
        the manual Approve gate) and posts a notification — NO agent turn — EXCEPT a CI *failure*
        also auto-triages (one Sonnet turn). Watermarks + fired flags advance only for channels that
        actually fired. All writes are on the event loop; the blocking probes run in threads.
        """
        if ticket in self._running:
            return  # a turn is live for this ticket — don't collide; catch it next cycle
        stored = state.read(ticket)
        if not stored:
            return
        info = await asyncio.to_thread(pr.open_pr_info, ticket)
        if info is None:
            return  # no open PR yet (branch pushed but PR not opened, or it was closed)
        number = info["number"]

        # Heal a stale pr_url: if the branch's open PR isn't the one we last recorded, the old PR
        # was closed and a fresh one opened for the same ticket (nothing else updates pr_url after
        # ship). The old PR's CI/review watermarks describe a PR that no longer exists — carrying
        # them forward would either mask the new PR's first real signal (if it happens to match) or
        # misfire on it — so reset everything scoped to "the currently open PR" along with the link.
        if stored.pr_url and info["url"] != stored.pr_url:
            stored = state.update(
                ticket, pr_url=info["url"], ci_status="", ci_run_id="", ci_fired=False,
                pr_comment_count=0, review_decision="", review_fired=False,
            )
            self._broadcast(ticket, "system",
                f"PR replaced — the previous PR was closed; PR #{number} is now open for this "
                f"ticket. Link and poll watermarks updated.")
            self._broadcast_status(stored)
        elif not stored.pr_url:
            stored = state.update(ticket, pr_url=info["url"])  # heal a never-recorded URL

        ci_result, rev = await asyncio.gather(
            asyncio.to_thread(self._ci_verdict, ticket, number),
            asyncio.to_thread(self._review_signal, stored, number),
            return_exceptions=True,
        )

        updates: dict = {}
        notices: list[str] = []
        ci_failed = False

        if isinstance(ci_result, BaseException):
            self._report_poll_error(ticket, "ci", "CI poll error", "the CI server", ci_result)
        else:
            self._poll_error_seen.pop((ticket, "ci"), None)
            if ci_result.get("verdict") == ci.RUNNING and stored.ci_status != ci.RUNNING:
                # Remember we saw it running: the terminal verdict of THIS run then always differs
                # from the watermark and fires — even a re-run with the same run id and verdict.
                # A silent watermark write: not a signal, no notification.
                state.update(ticket, ci_status=ci.RUNNING)
            if ci_result.get("verdict") in (ci.PASSED, ci.FAILED):
                verdict = ci_result["verdict"]
                run_id = str((ci_result.get("run") or {}).get("id") or "")
                # Fire on a new build (run id) or a changed verdict — not on re-seeing the same result.
                if run_id != stored.ci_run_id or verdict != stored.ci_status:
                    updates.update(ci_status=verdict, ci_run_id=run_id, ci_fired=True)
                    notices.append(f"CI build finished: **{verdict}**.")
                    ci_failed = verdict == ci.FAILED

        if isinstance(rev, BaseException):
            self._report_poll_error(ticket, "review", "PR review poll error", "GitHub", rev)
        else:
            self._poll_error_seen.pop((ticket, "review"), None)
            if rev is not None:
                seen_decision = stored.review_decision or "none"  # "" = never polled = "none"
                if rev["count"] > stored.pr_comment_count or rev["decision"] != seen_decision:
                    updates.update(pr_comment_count=rev["count"], review_decision=rev["decision"],
                                   review_fired=True)
                    if rev["decision"] in ("approved", "changes") and rev["decision"] != seen_decision:
                        notices.append(f"Code review: **{rev['decision']}**.")
                    else:
                        notices.append("New code review comment(s) on the PR.")

        if not updates:
            return

        # Flip to waiting_user so you're pulled in (you still press Approve to advance), persist the
        # fired channels' watermarks/flags, and push the status so the tab indicators light up.
        updates["activity"] = Activity.WAITING_USER
        updates["last_signal"] = " ".join(notices)  # seeds the next triage episode's kickoff
        final = state.update(ticket, **updates)
        for note in notices:
            self._broadcast(ticket, "system", "PR update — " + note)
        self._broadcast_status(final)

        # Auto-triage ONLY on CI failure — a background turn (doesn't block the poll loop). Every
        # other signal is notify-only; you engage the agent when you choose.
        if ci_failed:
            self._spawn(ticket, self._locked(ticket, self._chat(ticket, _CI_FAILURE_PROMPT, "system")))

    def _ci_done(self, status: TicketStatus) -> bool:
        """Has the CI channel nothing more to report this wait-cycle? (Always, with no CI.)"""
        if status.ci_fired:
            return True
        try:
            return ci.provider(self._project(status.ticket)) == "none"
        except project_mod.ProjectError:
            return False

    def _review_signal(self, stored: TicketStatus, number: int) -> dict | None:
        """The PR's review signal, with the task's tracker-side comments folded into the count: a
        GitHub issue's comments ride in the same GraphQL call; a source with its own
        ``feedback_count`` (a Slack thread's replies) adds one call (blocking; run in a thread)."""
        src = sources.get(stored.tracker)
        linked = getattr(src, "linked_issue", lambda _s: None)(stored)
        rev = pr.review_signal(stored.ticket, number, linked)
        if rev is None:
            return None
        extra = src.feedback_count(stored, self._project(stored.ticket))
        if extra:
            rev = {**rev, "count": rev["count"] + extra}
        return rev

    def _ci_verdict(self, ticket: str, number: int) -> dict:
        """The PR's CI verdict from the project's CI provider (blocking; run in a thread)."""
        return ci.verdict(self._project(ticket), number)

    # ----- background shell job poll (a slow compile/build, every BG_JOB_POLL_SECONDS) --------

    async def _bg_job_poll_loop(self) -> None:
        """Every BG_JOB_POLL_SECONDS, check every waiting_external ticket for a finished background
        job (started via ``background.py start``) — this app is turn-based, so an agent that
        launches a slow compile and says "I'll continue when it's done" has no way to actually wake
        itself back up; this loop is that wake-up. Stage-agnostic by construction: it only acts on
        tickets that actually have a tracked job (``background.status`` returns inactive otherwise),
        so it can never collide with pr-open's own use of ``waiting_external``. Cheap local
        file/PID check only — no external API — so a tight cadence costs nothing.
        """
        while True:
            await asyncio.sleep(config.BG_JOB_POLL_SECONDS)
            for status in state.all_statuses():
                if status.activity != Activity.WAITING_EXTERNAL:
                    continue
                if status.ticket in self._running:
                    continue  # a turn is live — don't collide; catch it next cycle
                try:
                    await self._check_background_job(status.ticket)
                    self._poll_error_seen.pop((status.ticket, "bg-job"), None)
                except Exception as exc:  # noqa: BLE001 - keep polling the other tickets
                    self._report_poll_error(status.ticket, "bg-job", "Background-job poll error",
                                             "the filesystem", exc)

    async def _check_background_job(self, ticket: str) -> None:
        """If this ticket has a finished background job, flip it to waiting_user and notify — no
        agent turn spent. The manager decides what to do with the result, same as every other poll
        signal; on failure they can just chat the agent to go look at the log."""
        result = await asyncio.to_thread(background.status, ticket)
        if not result.get("active") or not result.get("done"):
            return
        exit_code = result.get("exit_code")
        verdict = "succeeded" if exit_code == 0 else ("failed" if exit_code is not None else "ended unexpectedly (no exit code — killed or crashed)")
        tail = "\n".join(result.get("log_tail") or [])
        final = state.update(ticket, activity=Activity.WAITING_USER,
                              note=f"Background job {verdict} (exit {exit_code}).")
        self._broadcast(ticket, "system",
            f"Background job {verdict} (exit code {exit_code}). Log: {result.get('log_path')}"
            + (f"\n\nLast lines:\n{tail}" if tail else ""))
        await asyncio.to_thread(background.clear, ticket)
        self._broadcast_status(final)
