"""Orchestrator flow tests for ship, Trigger CI, and pr-open triage episodes.

The agent, git/gh, Jenkins and Jira are faked — no LLM, no network. Needs the venv (the orchestrator
imports claude_agent_sdk). Run: cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)

try:
    from sprint_manager import notes, orchestrator as orch_mod, state
    from sprint_manager.models import Activity, Stage
except ImportError:  # no venv
    orch_mod = None

T = "ABC-9"


class FakeSource:
    """Records lifecycle hooks instead of calling a tracker."""

    name = "jira"

    def __init__(self, calls):
        self.calls = calls

    def load(self, status, proj):
        return {"key": status.ticket, "summary": "Fix parser", "type": "Bug", "comments": []}

    def reference(self, status):
        return status.ticket

    footer = ""

    def pr_body_footer(self, status):
        return self.footer

    def on_work_start(self, status, proj):
        self.calls.append(("work_start",))

    def on_shipped(self, status, proj, url, new_pr):
        self.calls.append(("shipped", url, new_pr))

    def on_done(self, status, proj):
        self.calls.append(("done",))


class FakeAgent:
    """Stands in for TicketAgent: each send() runs ``script(prompt)`` → (reply_text, state_updates)."""

    script = staticmethod(lambda prompt: ("### Summary\n- ok", {}))
    prompts: list[str] = []

    def __init__(self, ticket, system_prompt, **_kw):
        self.ticket, self.system_prompt = ticket, system_prompt
        self.session_id = f"sess-{len(FakeAgent.prompts)}"
        self.total_cost_usd = self.total_turns = 0
        self.input_tokens = self.output_tokens = self.cache_read_tokens = self.cache_write_tokens = 0
        self.context_tokens = self.peak_context_tokens = 0
        FakeAgent.last = self

    async def connect(self): pass
    async def disconnect(self): pass
    async def interrupt(self): pass

    async def send(self, prompt, on_event=None):
        FakeAgent.prompts.append(prompt)
        self.total_turns += 2
        reply, updates = FakeAgent.script(prompt)
        self.context_tokens = getattr(FakeAgent, "next_context", 0)
        self.peak_context_tokens = max(self.peak_context_tokens, self.context_tokens)
        if updates:
            state.update(self.ticket, **updates)
        if on_event:
            on_event("text", reply)
        return SimpleNamespace(result=reply, session_id=self.session_id)


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for f in (state._path_for(T), notes.notes_path(T)):
            f.unlink(missing_ok=True)
        FakeAgent.prompts = []
        FakeAgent.next_context = 0
        FakeAgent.script = staticmethod(lambda prompt: ("### Summary\n- ok", {}))
        self.jira = []
        self.patches = [
            mock.patch.object(orch_mod, "TicketAgent", FakeAgent),
            mock.patch.object(orch_mod.Orchestrator, "_ensure_meta",
                              new=mock.AsyncMock(return_value={"key": T, "summary": "Fix parser",
                                                               "type": "Bug", "comments": []})),
            mock.patch.object(orch_mod.sources, "get", return_value=FakeSource(self.jira)),
            mock.patch.object(orch_mod.pr, "push", return_value="bugfix/x"),
            mock.patch.object(orch_mod.pr, "open_pr", return_value="https://github.com/o/r/pull/7"),
        ]
        for p in self.patches:
            p.start()
        self.open_pr = orch_mod.pr.open_pr
        self.o = orch_mod.Orchestrator()
        state.update(T, stage=Stage.WORK, activity=Activity.WAITING_USER, worktree="/tmp/wt",
                     branch="bugfix/x")

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()

    async def _settle(self):
        for _ in range(50):
            await asyncio.sleep(0)
            if not (self.o._advancing or self.o._running or self.o._queued):
                return
        await asyncio.sleep(0.01)

    def _branch(self, **kw):
        base = {"ok": True, "dirty": [], "ahead": 2, "log": "abc fix", "diffstat": " 1 file changed"}
        return mock.patch.object(orch_mod.worktree, "branch_state", return_value={**base, **kw})

    async def test_approve_refused_in_work(self):
        self.assertIn("error", self.o.approve(T))

    async def test_ship_refuses_dirty_worktree(self):
        with self._branch(dirty=["src/a.py"]):
            self.o.ship(T)
            await self._settle()
        s = state.read(T)
        self.assertEqual(s.stage, Stage.WORK)
        self.open_pr.assert_not_called()

    async def test_ship_opens_pr_and_enters_pr_open(self):
        await self.o._chat(T, "implement it", "user")  # a live work session
        FakeAgent.script = staticmethod(lambda prompt: (
            "### Summary\n- all done\n\n### PR\ntitle: ABC-9: Fix the parser\nbody:\nFixes it.", {}))
        with self._branch():
            self.o.ship(T)
            await self._settle()
        s = state.read(T)
        # demo's CI is GitHub checks: the push already started CI, so the poll is armed at once.
        self.assertEqual((s.stage, s.activity, s.session_id), (Stage.PR_OPEN, Activity.WAITING_EXTERNAL, ""))
        self.assertEqual((s.ci_fired, s.review_fired), (False, False))
        self.assertEqual(s.pr_url, "https://github.com/o/r/pull/7")
        self.open_pr.assert_called_once_with(T, "ABC-9: Fix the parser", "Fixes it.")
        self.assertIn(("shipped", "https://github.com/o/r/pull/7", True), self.jira)
        self.assertIn("## work — summary", notes.read(T))
        self.assertNotIn(T, self.o._agents)

    async def test_ship_with_manual_ci_waits_for_trigger(self):
        support.write_profile("jk", f'repo = "{support.ROOT}"\n[ci]\nprovider = "jenkins"\n')
        state.update(T, project="jk")
        await self.o._chat(T, "implement it", "user")
        with self._branch():
            self.o.ship(T)
            await self._settle()
        s = state.read(T)
        self.assertEqual((s.stage, s.activity), (Stage.PR_OPEN, Activity.WAITING_USER))
        self.assertIn("Trigger CI", s.note)

    async def test_ship_appends_source_footer(self):
        await self.o._chat(T, "implement it", "user")
        FakeSource.footer = "Fixes #7"
        FakeAgent.script = staticmethod(lambda prompt: (
            "### Summary\n- done\n\n### PR\ntitle: ABC-9: X\nbody:\nBody.", {}))
        try:
            with self._branch():
                self.o.ship(T)
                await self._settle()
        finally:
            FakeSource.footer = ""
        self.open_pr.assert_called_once_with(T, "ABC-9: X", "Body.\n\nFixes #7")

    async def test_final_approve_calls_on_done(self):
        state.update(T, stage=Stage.PR_OPEN, activity=Activity.WAITING_USER)
        self.o.approve(T)
        await self._settle()
        self.assertEqual(state.read(T).stage, Stage.DONE)
        self.assertIn(("done",), self.jira)

    async def test_ship_push_failure_keeps_session(self):
        await self.o._chat(T, "implement it", "user")
        err = orch_mod.subprocess.CalledProcessError(1, "git push", stderr="rejected")
        with self._branch(), mock.patch.object(orch_mod.pr, "push", side_effect=err):
            self.o.ship(T)
            await self._settle()
        self.assertEqual(state.read(T).stage, Stage.WORK)
        self.assertIn(T, self.o._agents)

    async def test_trigger_ci_rearms_and_ends_episode(self):
        state.update(T, stage=Stage.PR_OPEN, ci_fired=True, review_fired=True, pr_url="u")
        await self.o._chat(T, "look at the review", "user")  # starts an episode
        self.assertIn(T, self.o._agents)
        with mock.patch.object(orch_mod.ci, "for_ticket",
                               return_value={"pr": 7, "triggered": True}):
            res = await self.o.trigger_ci(T)
        self.assertTrue(res["triggered"])
        s = state.read(T)
        self.assertEqual((s.activity, s.ci_fired, s.review_fired, s.session_id),
                         (Activity.WAITING_EXTERNAL, False, False, ""))
        self.assertNotIn(T, self.o._agents)
        self.assertIn("## pr-open — triage episode", notes.read(T))

    async def test_trigger_ci_not_triggered_changes_nothing(self):
        state.update(T, stage=Stage.PR_OPEN, activity=Activity.WAITING_USER)
        with mock.patch.object(orch_mod.ci, "for_ticket",
                               return_value={"triggered": False, "reason": "not indexed"}):
            res = await self.o.trigger_ci(T)
        self.assertFalse(res["triggered"])
        self.assertEqual(state.read(T).activity, Activity.WAITING_USER)

    async def test_episode_kickoff_and_self_end(self):
        state.update(T, stage=Stage.PR_OPEN, activity=Activity.WAITING_USER, session_id="",
                     last_signal="New code review comment(s) on the PR.", pr_url="u")
        FakeAgent.script = staticmethod(lambda prompt: (
            "### Summary\n- nothing to do", {"activity": Activity.WAITING_EXTERNAL}))
        with self._branch():
            await self.o._chat(T, "what did they say?", "user")
        kickoff = FakeAgent.prompts[0]
        self.assertIn("fresh pr-open triage episode", kickoff)
        self.assertIn("New code review comment(s)", kickoff)
        self.assertIn("abc fix", kickoff)
        self.assertIn("what did they say?", kickoff)
        # The agent rested at waiting_external → the episode closed itself.
        self.assertNotIn(T, self.o._agents)
        self.assertEqual(state.read(T).session_id, "")
        self.assertIn("nothing to do", notes.read(T))

    async def test_context_persisted_and_compact_hint_once_at_g2(self):
        events = []
        orig = self.o._broadcast
        self.o._broadcast = lambda t, kind, text: (events.append(text), orig(t, kind, text))
        FakeAgent.script = staticmethod(lambda prompt: ("### Summary\n- test plan",
                                                        {"note": "G2: test plan"}))
        FakeAgent.next_context = orch_mod.config.CONTEXT_WARN_TOKENS + 1
        await self.o._chat(T, "move to testing", "user")
        await self.o._chat(T, "tweak the plan", "user")
        s = state.read(T)
        self.assertEqual(s.context_tokens, orch_mod.config.CONTEXT_WARN_TOKENS + 1)
        self.assertEqual(sum("Compact before testing" in e for e in events), 1)

    async def test_no_compact_hint_below_threshold(self):
        events = []
        orig = self.o._broadcast
        self.o._broadcast = lambda t, kind, text: (events.append(text), orig(t, kind, text))
        FakeAgent.script = staticmethod(lambda prompt: ("### Summary", {"note": "G2: test plan"}))
        FakeAgent.next_context = 1000
        await self.o._chat(T, "move to testing", "user")
        self.assertFalse(any("Compact before testing" in e for e in events))

    async def test_new_session_resets_context(self):
        state.update(T, context_tokens=500_000, peak_context_tokens=600_000)
        with mock.patch.object(orch_mod.Orchestrator, "_run_turn", new=mock.AsyncMock()):
            await self.o._enter_stage(T, Stage.WORK)
        s = state.read(T)
        self.assertEqual((s.context_tokens, s.peak_context_tokens), (0, 0))

    async def test_episode_prompt_uses_pr_open_notes_view(self):
        for i in range(5):
            notes.append_section(T, f"pr-open — triage episode (2026-09-2{i})", f"EP{i}")
        state.update(T, stage=Stage.PR_OPEN, activity=Activity.WAITING_USER)
        with self._branch():
            await self.o._chat(T, "hi", "user")
        first = FakeAgent.prompts[0]  # the notes view travels in the first message now
        self.assertNotIn("EP0", first)
        self.assertIn("EP4", first)
        self.assertNotIn("EP4", self.o._agents[T].system_prompt)

    async def test_system_prompt_is_ticket_agnostic(self):
        from sprint_manager import project
        from sprint_manager.agent import build_system_prompt
        for stage in (Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN):
            text = build_system_prompt(stage, project.load("demo"))
            self.assertNotIn("<<", text)            # every placeholder resolved
            self.assertIn("$SM_TICKET", text)
        notes.append_section(T, "explore — summary", "THE-PLAN")
        with mock.patch.object(orch_mod.Orchestrator, "_run_turn", new=mock.AsyncMock()) as run:
            await self.o._enter_stage(T, Stage.WORK)
        prompt, kwargs = run.call_args.args[1], run.call_args.kwargs
        self.assertIn("THE-PLAN", prompt)
        self.assertIn("Id: ABC-9", prompt)
        self.assertNotIn(T, self.o._agents[T].system_prompt)
        self.assertNotIn("THE-PLAN", kwargs["display"])  # transcript shows only the kickoff

    async def test_old_format_session_not_resumed(self):
        state.update(T, session_id="old-v1-session", session_format=0)
        await self.o._chat(T, "continue", "user")
        self.assertIn("Id: ABC-9", FakeAgent.prompts[0])  # fresh session with context
        self.assertEqual(state.read(T).session_format, orch_mod.SESSION_FORMAT)

    async def test_current_format_session_resumed(self):
        state.update(T, session_id="sess-x", session_format=orch_mod.SESSION_FORMAT)
        await self.o._chat(T, "continue", "user")
        self.assertEqual(FakeAgent.prompts, ["continue"])  # resumed: no context re-sent


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class TrackContextTest(unittest.TestCase):
    def test_latest_call_and_peak(self):
        from sprint_manager.agent import TicketAgent
        a = object.__new__(TicketAgent)  # skip the SDK client; _track_context is pure bookkeeping
        a.context_tokens = a.peak_context_tokens = 0
        a._track_context({"input_tokens": 10, "cache_read_input_tokens": 90_000,
                          "cache_creation_input_tokens": 5_000})
        a._track_context({"input_tokens": 5, "cache_read_input_tokens": 40_000})
        a._track_context(None)
        self.assertEqual((a.context_tokens, a.peak_context_tokens), (40_005, 95_010))
