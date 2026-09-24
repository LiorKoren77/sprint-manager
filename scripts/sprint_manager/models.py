"""Core domain types: the pipeline ``Stage``, the orthogonal ``Activity``, the per-stage model
policy, and the ``TicketStatus`` record persisted to the status store.

STAGE answers "where in the pipeline is this ticket?".
ACTIVITY answers "what is the agent doing right now?" and is what the UI uses to decide whether
*you* are needed:

* ``working``          - the agent is actively thinking/doing work.
* ``queued``           - your message is accepted but another ticket holds the single turn slot;
                         it runs next (serial mode). Purely a waiting-for-its-turn state.
* ``waiting_user``     - the agent hit a judgment call for you (round-robin jumps here).
* ``waiting_external`` - PR open (CI running / awaiting review), a background job, or a plan awaiting
                         tech-leader sign-off; the orchestrator polls and notifies you.
* ``idle``             - not started, or terminal (stage == done).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class Stage(str, Enum):
    """Position in the ticket lifecycle.

    Session boundaries sit where context changes character, NOT at every approval gate: explore
    and work are each ONE session with internal gates (``waiting_user`` + chat); ship is a zero-LLM
    orchestrator action (Ship ▶), not a stage; pr-open runs one short "triage episode" session per
    CI/review signal. See references/stages/<stage>.md and docs/design-explore-work-triage.md.
    """

    TODO = "to-do"
    EXPLORE = "explore"               # recap + ask where-to-look, then draft/iterate the plan (RO)
    WORK = "work"                     # implement + refine, test plan, run suites; ends at Ship ▶
    PR_OPEN = "pr-open"               # PR in flight: CI verdict + code reviews arrive in any order
    DONE = "done"

    @classmethod
    def _missing_(cls, value: object) -> "Stage | None":
        alias = STAGE_ALIASES.get(value)  # type: ignore[arg-type]
        return cls(alias) if alias else None


# Migration aliases: renamed/merged stages keep resolving from old persisted values (state/*.json
# and in-flight sessions prompted with the old name). Single source — consumed by Stage._missing_
# above, TicketStatus.from_dict (which drops the stale session on an aliased stage), and
# report_stage's argparse choices, so a rename can't drift between them.
STAGE_ALIASES: dict[str, str] = {
    "orient": "explore",
    "plan": "explore",
    "implementation": "work",
    "testing": "work",
    "ship": "work",           # Ship ▶ is idempotent (push + existing-PR lookup), so this is safe
    "ci-build": "pr-open",
}


# The ordered working flow. ``Approve`` advances explore → work and pr-open → done; work leaves
# only via the Ship ▶ action (see Orchestrator.ship).
STAGE_FLOW: list[Stage] = [Stage.TODO, Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN, Stage.DONE]

# Stages that read the repo but must NOT edit it (enforced via disallowed_tools + cwd = the project's main checkout).
READ_ONLY_STAGES: set[Stage] = {Stage.EXPLORE}


def next_stage(stage: Stage) -> Stage:
    """The stage that ``Approve`` advances to (terminates at ``done``)."""
    idx = STAGE_FLOW.index(stage)
    return STAGE_FLOW[idx + 1] if idx + 1 < len(STAGE_FLOW) else Stage.DONE


def uses_worktree(stage: Stage) -> bool:
    """True once the agent works in the ticket's worktree (work onward)."""
    return stage in {Stage.WORK, Stage.PR_OPEN}


# Per-stage turn cap, passed to the CLI as --max-turns (sums of the old per-stage caps: explore =
# orient 15 + plan 30, work = implementation 80 + testing 50). The "infra auto-fix ≤10 turns" rule
# is enforced in the work prompt within this larger ceiling.
MAX_TURNS_BY_STAGE: dict[Stage, int] = {Stage.EXPLORE: 45, Stage.WORK: 130, Stage.PR_OPEN: 50}


def max_turns_for(stage: Stage) -> int:
    return MAX_TURNS_BY_STAGE.get(stage, 40)


class Activity(str, Enum):
    """Liveness of the agent, orthogonal to Stage."""

    WORKING = "working"
    QUEUED = "queued"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    IDLE = "idle"


# Which model + effort drives each stage — tiered for cost (the big cost fix is per-stage
# compaction; this trims further). ``effort`` is None for Haiku (no effort parameter).
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"
FABLE = "claude-fable-5"

# Choices offered by the settings UI (served via /api/config so the frontend never hardcodes
# model ids). NO_EFFORT_MODELS have no effort parameter — the UI disables the effort column.
AVAILABLE_MODELS: list[dict] = [
    {"value": OPUS, "label": "Opus 4.8"},
    {"value": SONNET, "label": "Sonnet 4.6"},
    {"value": HAIKU, "label": "Haiku 4.5"},
    {"value": FABLE, "label": "Fable 5"},
]
NO_EFFORT_MODELS: frozenset[str] = frozenset({HAIKU})

MODEL_BY_STAGE: dict[Stage, tuple[str, str | None]] = {
    # One model per session: switching mid-session (ClaudeSDKClient.set_model) would invalidate the
    # prompt cache, which costs more than tiering saves. So the opening recap runs on Opus too (a
    # couple of turns), and testing runs on the same model that wrote the code.
    Stage.EXPLORE: (OPUS, "high"),          # understanding + the plan — the important reasoning
    Stage.WORK: (OPUS, "high"),             # coding, refinement, and test triage in one context
    Stage.PR_OPEN: (SONNET, "high"),        # short triage episodes — CI logs / review comments
}

# Default engine for any stage not listed.
DEFAULT_MODEL: tuple[str, str | None] = (SONNET, "high")

# Runtime overrides written by the settings UI. Keys are Stage enum values; checked before
# MODEL_BY_STAGE so a live update takes effect on the next _attach() without a server restart.
_model_overrides: dict[Stage, tuple[str, str | None]] = {}

# Ordered list of working stages for the config API / settings UI.
WORKING_STAGES: list[Stage] = [Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN]


def model_for_stage(stage: Stage) -> tuple[str, str | None]:
    """Return ``(model_id, effort)`` for a stage, checking runtime overrides first."""
    return _model_overrides.get(stage) or MODEL_BY_STAGE.get(stage, DEFAULT_MODEL)


def get_model_config() -> list[dict]:
    """Effective model+effort for every working stage (overrides applied)."""
    rows = []
    for s in WORKING_STAGES:
        model, effort = model_for_stage(s)
        rows.append({"stage": s.value, "model": model, "effort": effort})
    return rows


def set_model_overrides(overrides: dict[str, dict]) -> None:
    """Replace the runtime overrides from a ``{stage: {model, effort?}}`` mapping.

    This is the same shape ``state/config.json`` persists, so the load and save paths pass it
    through without conversion. Unknown stages and empty models are skipped — and so are OLD stage
    names (``STAGE_ALIASES``): several old stages merge into one new stage, so honouring them would
    silently pick whichever key happened to come last. Defaults apply until the manager re-saves.
    """
    global _model_overrides
    new: dict[Stage, tuple[str, str | None]] = {}
    for stage_str, cfg in (overrides or {}).items():
        if stage_str in STAGE_ALIASES:
            continue
        try:
            stage = Stage(stage_str)
        except ValueError:
            continue
        model = (cfg or {}).get("model") or ""
        if model:
            new[stage] = (model, (cfg or {}).get("effort") or None)
    _model_overrides = new


# Bumped whenever what a session carries in its own history changes shape. 2 = ticket context + notes
# travel in the session's FIRST MESSAGE (the system prompt is ticket-agnostic). A format-1 session
# had them only in its system prompt, so resuming it under the new prompt would leave the agent with
# no ticket context at all — such sessions are started fresh from notes instead.
SESSION_FORMAT = 2


@dataclass
class TicketStatus:
    """The full state of one ticket, persisted as ``state/<ticket>.json``."""

    ticket: str
    summary: str = ""
    issue_type: str = ""
    jira_status: str = ""  # snapshot of the Jira status; fallback for the column after a restart
    source: str = "sprint"  # intake: "sprint" (loaded in a batch) or "manual" (added one at a time)
    project: str = ""       # project profile name (project.py); "" = the default project
    tracker: str = "jira"   # task source (sources/): jira | github | slack | text
    kind: str = ""          # bug | feature — the branch prefix; "" = derive from issue_type
    external_ref: str = ""  # tracker-side id: Jira key, GitHub issue number, Slack channel/ts
    external_url: str = ""  # browse link on the tracker, if any
    stage: Stage = Stage.TODO
    activity: Activity = Activity.IDLE
    note: str = ""
    branch: str = ""
    worktree: str = ""
    pr_url: str = ""
    ci_status: str = ""
    updated_at: str = ""
    cost_usd: float = 0.0  # CUMULATIVE across all stages (per-stage baseline + current agent spend)
    session_id: str = ""  # Agent SDK session id, for resuming across service restarts
    # Diagnostic counters. total_turns is CUMULATIVE across all stages (like cost_usd, via the
    # orchestrator's per-stage baseline). The token counters are PER-STAGE (current session) on
    # purpose — the panel's cache indicator warns at 1M to catch a single session ballooning, which
    # only works if cache_read reflects the current session, not the ticket's lifetime total.
    total_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0   # cache_read_input_tokens (current session)
    cache_write_tokens: int = 0  # cache_creation_input_tokens (current session)
    # Context size of the current session's latest API call (tokens every next call re-reads) and
    # its peak — the real "session too big?" signal, unlike the cumulative cache_read above.
    context_tokens: int = 0
    peak_context_tokens: int = 0
    # ----- pr-open dual-channel polling (CI verdict + code review) -----
    # Watermarks dedup "what's new"; the *_fired flags gate "both seen → stop polling"; they are
    # reset (re-armed) whenever the ticket re-enters waiting_external (e.g. after a fix push).
    pr_comment_count: int = 0    # review-item count watermark (comments + reviews + inline)
    ci_run_id: str = ""          # Jenkins run id of the last reported verdict (new run ⇒ re-fire)
    ci_fired: bool = False       # CI reported a terminal verdict this wait-cycle
    review_decision: str = ""    # normalized review status: "" | commented | changes | approved
    review_fired: bool = False   # a review signal (comment or decision) reported this wait-cycle
    # Which prompt layout ``session_id`` was created under (see SESSION_FORMAT). A session from an
    # older layout is never resumed — see Orchestrator._resumable.
    session_format: int = 0
    # The notice text of the most recent poll fire (e.g. "CI build finished: **failed**."). Seeds
    # the kickoff of the next pr-open triage episode, which starts with no session of its own.
    last_signal: str = ""

    @property
    def branch_kind(self) -> str:
        """``bug`` or ``feature`` — explicit ``kind``, else derived from the tracker's issue type."""
        if self.kind in ("bug", "feature"):
            return self.kind
        return "bug" if self.issue_type.lower() == "bug" else "feature"

    def to_dict(self) -> dict:
        """Serialize to a plain JSON-ready dict (enums become their string values)."""
        data = asdict(self)
        data["stage"] = self.stage.value
        data["activity"] = self.activity.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TicketStatus":
        """Rebuild from a stored dict, tolerating missing/unknown fields."""
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        # Rows persisted before task sources existed were all Jira issues, keyed by their Jira key.
        clean.setdefault("tracker", "jira")
        if clean["tracker"] == "jira":
            clean.setdefault("external_ref", clean.get("ticket", ""))
        if "stage" in clean:
            # A stage persisted under an OLD name (pre-merge) carries a session that was prompted
            # with that old stage's instructions — resuming it under the new prompt would keep
            # following them. Drop it; for a merged working stage also park at idle so Start ▶
            # begins a fresh session from notes. (pr-open keeps its activity: the poll loop gates on
            # it, and pr-open has no standing session anyway.)
            alias = STAGE_ALIASES.get(clean["stage"])
            if alias:
                clean["session_id"] = ""
                if alias != Stage.PR_OPEN.value:
                    clean["activity"] = Activity.IDLE.value
            try:
                clean["stage"] = Stage(clean["stage"])
            except ValueError:  # an older/unknown stage value — fall back to the start
                clean["stage"] = Stage.TODO
        if "activity" in clean:
            try:
                clean["activity"] = Activity(clean["activity"])
            except ValueError:
                clean["activity"] = Activity.IDLE
        return cls(**clean)
