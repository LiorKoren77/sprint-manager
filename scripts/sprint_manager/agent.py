"""TicketAgent: a Claude Agent SDK session scoped to ONE stage of ONE ticket.

explore and work are each one session (with internal gates); pr-open runs one short session per
triage episode. The orchestrator builds the stage's system prompt (base + the stage's instruction
file — ticket-agnostic so it caches across sessions; the ticket + notes view go in the first
message) and constructs a TicketAgent with the right model, effort, cwd, and turn cap. The read-only stage (explore) also has Edit/Write disabled as a backstop. A
permission guard blocks merge/force-push and CI triggering (the manager does those from the UI).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from sprint_manager import config  # noqa: E402
from sprint_manager.models import Stage  # noqa: E402

SCRIPTS_DIR = config.APP_ROOT / "scripts"
REFERENCES = config.APP_ROOT / "references"
BASE_PROMPT = REFERENCES / "agent-system-prompt.md"
STAGES_DIR = REFERENCES / "stages"

# Stage -> its instruction file under references/stages/.
_STAGE_FILES = {
    Stage.EXPLORE: "explore.md",
    Stage.WORK: "work.md",
    Stage.PR_OPEN: "pr-open.md",
}

# Stages that need MCP servers (strict_mcp_config=False). EMPTY now: every stage runs MCP-free.
# Plan used to be the exception (Confluence publishing via the "claude.ai Atlassian" OAuth server),
# but that moved to sprint_manager.confluence over REST — so no stage loads the 30+ Atlassian/Slack
# tool schemas into its context, and there is no OAuth marketplace server to break. The mechanism
# is kept for any future genuine MCP need.
MCP_OPEN_STAGES: frozenset[Stage] = frozenset()

# Bash command fragments the agent may never run: merging and force-pushing are manual, and CI is
# triggered only by the manager's Trigger CI button (Orchestrator.trigger_ci) — never by an agent.
_BLOCKED_FRAGMENTS = ("gh pr merge", "push --force", "push -f", "push --force-with-lease")
_CI_TRIGGER_FRAGMENTS = ("jenkins trigger", "ci trigger", "gh run rerun", "gh workflow run")


def _script_cmd(module: str) -> str:
    """A copy-pasteable shell command that runs one of our modules with PYTHONPATH set."""
    return f"PYTHONPATH={SCRIPTS_DIR} python3 -m sprint_manager.{module}"


TICKET_CONTEXT = REFERENCES / "ticket-context.md"

# The CI module the prompts call (``<<CI_CMD>> status|logs``) — dispatches to the project's provider.
CI_MODULE = "ci"

# What the prompts say about CI, per provider kind (see ci.py). "manual" = builds start only when the
# manager triggers them (Jenkins); "auto" = every push starts a run (GitHub checks).
_CI_TEXT = {
    "manual": {
        "<<CI_POLICY>>": (
            "**CI is started only by the manager** with the dashboard's **Trigger CI** button — you "
            "cannot trigger it (the command is blocked). Whenever a build is needed (none has run "
            "yet, or you pushed a fix), say so plainly: \"Pushed — press **Trigger CI** when you're "
            "ready.\" Then set `waiting_user` and STOP."),
        "<<CI_NO_BUILD>>": "CI was never triggered for this PR — tell the manager to press Trigger CI",
        "<<CI_AFTER_PUSH>>": "ask the manager to press **Trigger CI** if a new build is wanted",
        "<<CI_GUARD>>": (
            "- **CI is started only by the manager**, with the dashboard's **Trigger CI** button. "
            "You cannot trigger it (the command is blocked), and a commit/push does NOT start a "
            "build by itself. When a build is needed, say so and stop — never claim CI is running "
            "unless `<<CI_CMD>> status` shows it."),
    },
    "auto": {
        "<<CI_POLICY>>": (
            "**CI runs automatically on every push** (GitHub checks) — pushing a fix starts a new "
            "run by itself. You never re-run or dispatch CI yourself (the commands are blocked); if "
            "a flaky run needs re-running, ask the manager to press **Re-run CI**."),
        "<<CI_NO_BUILD>>": "no checks have reported for the PR's latest commit yet (they may still be queued)",
        "<<CI_AFTER_PUSH>>": "tell the manager CI re-runs on its own for the new commit",
        "<<CI_GUARD>>": (
            "- **CI runs automatically on every push** (GitHub checks). You never re-run or "
            "dispatch it yourself (blocked) — the manager has **Re-run CI**. Never claim CI passed "
            "unless `<<CI_CMD>> status` shows it."),
    },
    "none": {
        "<<CI_POLICY>>": "This project has **no CI configured** — there are no builds to wait for; "
                         "only code review arrives.",
        "<<CI_NO_BUILD>>": "this project has no CI",
        "<<CI_AFTER_PUSH>>": "note that there is no CI to re-run",
        "<<CI_GUARD>>": "- This project has **no CI**; never claim anything passed in CI.",
    },
}

# Install-wide constants substituted into the static system prompt (identical for every ticket).
def _static_replacements() -> dict[str, str]:
    return {
        "<<REPORT_CMD>>": _script_cmd("report_stage"),
        "<<PR_CMD>>": _script_cmd("pr"),
        "<<CI_CMD>>": _script_cmd(CI_MODULE),
        "<<CONFLUENCE_CMD>>": _script_cmd("confluence"),
        "<<SLACK_CMD>>": _script_cmd("slack"),
        "<<TESTS_CMD>>": _script_cmd("run_tests"),
        "<<PREFLIGHT_CMD>>": _script_cmd("preflight"),
        "<<WORKTREE_CMD>>": _script_cmd("worktree"),
        "<<BACKGROUND_CMD>>": _script_cmd("background"),
    }


_CONDITIONAL = re.compile(r"<<IF:(\w+)>>\n?(.*?)<<ENDIF:\1>>\n?", re.DOTALL)

# Used when a project's profile names no test-commands file.
_DISCOVER_TESTS = (
    "This project documents no test commands for this tool: find how its tests run (README, "
    "CONTRIBUTING, Makefile / package.json / pyproject, CI config) and state the **exact commands** "
    "you propose in the test plan — the manager confirms them at this gate.")


def _project_values(project) -> tuple[dict[str, str], dict[str, bool]]:
    """Placeholders + conditional flags for a project (fixed per project, so still cacheable)."""
    tests = project.prompts.get("test_commands")
    test_text = (f"The project's suites and their **exact commands** are documented in `{tests}` — "
                 f"read that file first." if tests else _DISCOVER_TESTS)
    kind = {"jenkins": "manual", "github": "auto"}.get(project.ci.get("provider", "github"), "none")
    values = {
        **_CI_TEXT[kind],
        "<<PROJECT>>": project.name,
        "<<REPO>>": str(project.repo),
        "<<BASE_BRANCH>>": project.base_branch,
        "<<TEST_COMMANDS>>": test_text,
    }
    flags = {"confluence": project.has_confluence, "preflight": project.has_preflight}
    return values, flags


def _project_notes(project, stage: Stage) -> str:
    parts = [project.prompt_text("notes").strip(), project.prompt_text(stage.value).strip()]
    body = "\n\n".join(p for p in parts if p)
    return f"## Project notes ({project.name})\n\n{body}\n" if body else ""


def build_system_prompt(stage: Stage, project) -> str:
    """The stage's system prompt: base rules + the stage's instructions — deliberately TICKET-AGNOSTIC.

    Project-specific text (the profile's notes files, test-commands pointer, base branch, and the
    ``<<IF:confluence>>`` / ``<<IF:preflight>>`` sections) is fixed per project, so the prompt is
    byte-identical for every session of the same (project, stage).

    Prompt caching only matches at content-block boundaries, so a system prompt that embeds the
    ticket or notes is a cache miss for every new session. Kept free of per-ticket text, it is
    byte-identical for every session of the same stage (any ticket), so a fresh session started
    within the cache TTL (1h on a subscription) reads it instead of re-writing it. Per-ticket values
    reach commands via the ``SM_TICKET``/``SM_WORKTREE`` env vars; the ticket and notes travel in the
    session's first message (``build_context_message``).
    """
    stage_text = (STAGES_DIR / _STAGE_FILES[stage]).read_text() if stage in _STAGE_FILES else ""
    combined = BASE_PROMPT.read_text().replace("<<STAGE_INSTRUCTIONS>>", stage_text)
    combined = combined.replace("<<PROJECT_NOTES>>", _project_notes(project, stage))
    values, flags = _project_values(project)
    combined = _CONDITIONAL.sub(lambda m: m.group(2) if flags.get(m.group(1)) else "", combined)
    # Project values first: some (the CI guard) contain install placeholders like <<CI_CMD>>.
    for token, value in {**values, **_static_replacements(), "<<STAGE>>": stage.value}.items():
        combined = combined.replace(token, value)
    return combined


# How the first message describes where a task came from. Everything but a groomed tracker ticket is
# marked "not groomed", which switches on explore's assumptions + acceptance-criteria step (G1).
_NOT_GROOMED = "— **not groomed**: follow the acceptance-criteria step at explore G1"
_SOURCE_TEXT = {
    "jira": "Jira issue (a groomed tracker ticket)",
    "github": f"GitHub issue {_NOT_GROOMED}",
    "slack": f"Slack thread (the discussion below is the problem statement) {_NOT_GROOMED}",
    "text": f"free text typed into the dashboard {_NOT_GROOMED}",
}


def build_context_message(meta: dict, notes: str, status=None) -> str:
    """The per-ticket half of a session's context, sent as the start of its FIRST message: the Jira
    ticket (key/type/summary/url/description/comments), its worktree/branch, and the stage's view
    of the notes. ``meta`` carries the ticket fields merged with the stored worktree/branch."""
    comments = "\n".join(
        f"- {c.get('author', '?')} ({c.get('created', '')[:10]}): {c.get('body', '').strip()}"
        for c in meta.get("comments", [])
    )
    tracker = (status.tracker if status else "") or "jira"
    ref = ""
    if status is not None:
        from sprint_manager import sources
        try:
            ref = sources.get(tracker).reference(status)
        except sources.SourceError:
            ref = ""
    replacements = {
        "<<REF>>": ref or "(none)",
        "<<SOURCE>>": _SOURCE_TEXT.get(tracker, tracker),
        "<<TICKET>>": meta.get("key", ""),
        "<<ISSUE_TYPE>>": meta.get("type", ""),
        "<<SUMMARY>>": meta.get("summary", ""),
        "<<URL>>": meta.get("url", "") or "(none)",
        "<<DESCRIPTION>>": (meta.get("description", "") or "(no description)").strip(),
        "<<COMMENTS>>": comments or "(no comments)",
        "<<WORKTREE>>": meta.get("worktree", "") or "(created when the work stage starts)",
        "<<BRANCH>>": meta.get("branch", "") or "(created when the work stage starts)",
        "<<NOTES>>": notes.strip() or "(nothing yet — this is the first session)",
    }
    text = TICKET_CONTEXT.read_text()
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def _agent_env(ticket: str, cwd: str, worktree: str, project, ref: str = "") -> dict[str, str]:
    env = {"PYTHONPATH": str(SCRIPTS_DIR), "ANTHROPIC_API_KEY": "",
           "BASH_MAX_OUTPUT_LENGTH": str(config.AGENT_BASH_MAX_OUTPUT),
           "SM_TICKET": ticket, "SM_WORKTREE": worktree, "SM_REF": ref}
    if project is not None:
        env.update(SM_PROJECT=project.name, SM_REPO=str(project.repo))
        if project.checkout_env:
            env[project.checkout_env] = cwd
    return env


async def _merge_guard(tool_name, tool_input, _context):
    """Permission callback: allow everything except blocked Bash commands (merge / force-push /
    CI trigger)."""
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if any(fragment in command for fragment in _CI_TRIGGER_FRAGMENTS):
            return PermissionResultDeny(
                message="Blocked: CI is triggered only by the manager, with the Trigger CI button "
                "in the dashboard. Tell them the branch is pushed and ready for CI, set "
                "activity=waiting_user, and stop."
            )
        if any(fragment in command for fragment in _BLOCKED_FRAGMENTS):
            return PermissionResultDeny(
                message="Blocked: merging is manual — the manager merges on GitHub, outside this "
                "system. Force-pushing is never allowed. Set activity=waiting_user and report."
            )
    return PermissionResultAllow()


# A streamed block handed to the UI: ("thinking"|"text"|"tool"|"result", text).
EventHandler = Callable[[str, str], Awaitable[None] | None]


class TicketAgent:
    """A Claude session for one stage of one ticket. Disposed when the stage advances (compaction)."""

    def __init__(
        self,
        ticket: str,
        system_prompt: str,
        cwd: str,
        model: str,
        effort: str | None,
        max_turns: int,
        read_only: bool = False,
        resume_session_id: str | None = None,
        skills: list[str] | None = None,
        strict_mcp: bool = True,
        worktree: str = "",
        project=None,
        ref: str = "",
    ) -> None:
        self.ticket = ticket
        self.session_id: str | None = resume_session_id
        self.total_cost_usd = 0.0
        self.total_turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        # Context size = tokens the LATEST API call processed (input + cache read + cache write),
        # i.e. what every next call re-reads. Unlike the cumulative counters above, this is the real
        # "how big is this session" signal. peak = the session's maximum so far.
        self.context_tokens = 0
        self.peak_context_tokens = 0
        options = ClaudeAgentOptions(
            model=model,
            effort=effort,                       # None for Haiku (no effort param)
            max_turns=max_turns,                 # per-stage turn cap
            cwd=cwd,
            system_prompt=system_prompt,
            permission_mode="acceptEdits",
            can_use_tool=_merge_guard,
            # Subtractive tool trim — remove tools no stage uses (and that the prompt forbids),
            # which drops them from context without the allowlist risk of silently stripping a
            # needed tool. NotebookEdit (no notebooks), AskUserQuestion (prompt mandates chat +
            # waiting_user instead — the console has no handler for it, so a call would hang),
            # WebSearch/WebFetch (CI/Jenkins go through curl/Python via Bash), Agent (no stage
            # spawns subagents; keeps work single-threaded). The read-only stage also loses Edit/Write.
            disallowed_tools=(["Edit", "Write"] if read_only else [])
            + ["NotebookEdit", "AskUserQuestion", "WebSearch", "WebFetch", "Agent"],
            setting_sources=["user", "project", "local"],  # load company plugins/skills + CLAUDE.md
            # strict_mcp_config is stage-dependent (see MCP_OPEN_STAGES in this module). True for
            # every stage today (blocks 30+ Atlassian/Slack tool schemas from riding in every turn's
            # context). Jira and Confluence go through Python REST clients — no MCP.
            # skills=[] suppresses every skill from the model's listing. Skill *metadata* (~all
            # skills) was being re-sent in context on every turn though stages call almost none —
            # the single largest source of wasted cache-read tokens. Plugins are still reachable by
            # path via Read/Bash (how the stage prompts use them); re-enable specific skills per
            # stage via SKILLS_BY_STAGE (passed as the ``skills`` constructor arg).
            skills=skills or [],
            strict_mcp_config=strict_mcp,
            add_dirs=[*(str(d) for d in (project.extra_dirs if project else [])),
                      str(SCRIPTS_DIR), str(config.STATE_DIR)],
            # Authenticate the spawned CLI with the logged-in Claude account (team/subscription
            # OAuth in ~/.claude/.credentials.json), NOT a pay-as-you-go API key. The SDK inherits
            # our whole environment, and the CLI prefers ANTHROPIC_API_KEY whenever it is truthy —
            # so we blank it here (empty string reads as "no key" → OAuth fallback). This override
            # only affects the subprocess; the parent process env is left untouched.
            # BASH_MAX_OUTPUT_LENGTH caps each Bash result in context (see config.AGENT_BASH_MAX_OUTPUT).
            # SM_TICKET/SM_WORKTREE/SM_PROJECT/SM_REPO carry the per-ticket/per-project values the
            # (cacheable) system prompt's commands reference — see build_system_prompt; the CLI
            # modules resolve their project from SM_PROJECT. A profile's ``checkout_env`` names one
            # more var set to this agent's checkout (cwd) for the project's own build tooling.
            env=_agent_env(ticket, cwd, worktree, project, ref),
            resume=resume_session_id,
        )
        self._client = ClaudeSDKClient(options)

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def interrupt(self) -> None:
        """Interrupt the in-flight turn (streaming mode); the running ``send`` then returns early."""
        await self._client.interrupt()

    async def send(self, prompt: str, on_event: EventHandler | None = None) -> ResultMessage | None:
        """Send one message, stream the response (via ``on_event``), and return the result."""
        await self._client.query(prompt)
        result: ResultMessage | None = None
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                if message.session_id:           # capture early for resume safety
                    self.session_id = message.session_id
                self._track_context(message.usage)
                await self._emit_assistant(message, on_event)
            elif isinstance(message, ResultMessage):
                result = message
                self.session_id = message.session_id
                self.total_cost_usd += message.total_cost_usd or 0.0
                self.total_turns += message.num_turns or 0
                usage = message.usage or {}
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)
                self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                self.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
                await self._emit(on_event, "result", message.result or "")
        return result

    def _track_context(self, usage: dict | None) -> None:
        """Record the context size of the API call this message came from. Several messages (one per
        content block) share one call's usage, so overwriting is correct. Sub-agent messages don't
        occur here (the Agent tool is disallowed)."""
        if not usage:
            return
        size = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0))
        if size:
            self.context_tokens = size
            self.peak_context_tokens = max(self.peak_context_tokens, size)

    async def _emit_assistant(self, message: AssistantMessage, on_event: EventHandler | None) -> None:
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                await self._emit(on_event, "thinking", block.thinking)
            elif isinstance(block, TextBlock):
                await self._emit(on_event, "text", block.text)
            elif isinstance(block, ToolUseBlock):
                await self._emit(on_event, "tool", f"{block.name} {block.input}")

    @staticmethod
    async def _emit(on_event: EventHandler | None, kind: str, text: str) -> None:
        if on_event is None:
            return
        outcome = on_event(kind, text)
        if outcome is not None:  # support both sync and async handlers
            await outcome
