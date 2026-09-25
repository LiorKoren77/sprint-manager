"""Regressions for the code-review findings in the orchestrator and CLI layer: pr ready (#3),
done tasks (#4), the review watermark (#5), re-run CI (#6), thread-safe broadcasts (#9), id reuse
(#10), the double-click guard (#12), review re-arm (#13), poll arming per CI provider (#14) and
base-branch wording (#15). Agent, git/gh and trackers are faked.

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import asyncio
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT, write_profile

from sprint_manager import project, sources, state  # noqa: E402
from sprint_manager.models import Activity, Stage  # noqa: E402

try:
    from sprint_manager import orchestrator as orch_mod
    from test_orchestrator_flows import FakeAgent, FakeSource
except ImportError:  # no venv
    orch_mod = None

T = "demo-t-50"


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class ReviewFixesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        support.clean_state()
        FakeAgent.prompts = []
        FakeAgent.script = staticmethod(lambda prompt: ("### Summary\n- ok", {}))
        self.calls = []
        self.patches = [
            mock.patch.object(orch_mod, "TicketAgent", FakeAgent),
            mock.patch.object(orch_mod.Orchestrator, "_ensure_meta",
                              new=mock.AsyncMock(return_value={"key": T, "summary": "S", "type": "Bug",
                                                               "comments": []})),
            mock.patch.object(orch_mod.sources, "get", return_value=FakeSource(self.calls)),
        ]
        for p in self.patches:
            p.start()
        self.o = orch_mod.Orchestrator()
        self.seen = []
        real = self.o._broadcast
        self.o._broadcast = lambda t, k, text: (self.seen.append(text), real(t, k, text))
        state.update(T, project="demo", tracker="text", stage=Stage.PR_OPEN,
                     activity=Activity.WAITING_EXTERNAL, pr_url="https://github.com/o/r/pull/7",
                     worktree="/tmp/wt")

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()

    async def _poll(self, verdict, review=None):
        with mock.patch.object(orch_mod.pr, "open_pr_info",
                               return_value={"number": 7, "url": "https://github.com/o/r/pull/7"}), \
             mock.patch.object(orch_mod.Orchestrator, "_ci_verdict", return_value=verdict), \
             mock.patch.object(orch_mod.Orchestrator, "_review_signal",
                               return_value=review or {"count": 0, "decision": "none"}), \
             mock.patch.object(orch_mod.Orchestrator, "_chat", new=mock.AsyncMock()):
            await self.o._check_external(T)
        return state.read(T)

    # ---- #5
    async def test_first_poll_with_no_reviews_does_not_fire(self):
        s = await self._poll({"verdict": "running", "run": {"id": "a"}})
        self.assertFalse(s.review_fired)
        self.assertEqual(s.activity, Activity.WAITING_EXTERNAL)
        self.assertFalse(any("review comment" in t for t in self.seen), self.seen)
        s = await self._poll({"verdict": "running", "run": {"id": "a"}}, {"count": 1, "decision": "commented"})
        self.assertTrue(s.review_fired)   # a real comment still fires

    # ---- #6
    async def test_rerun_with_the_same_run_id_fires_again(self):
        s = await self._poll({"verdict": "failed", "run": {"id": "sha:11"}})
        self.assertTrue(s.ci_fired)
        state.update(T, ci_fired=False, activity=Activity.WAITING_EXTERNAL)       # Re-run CI re-arms
        s = await self._poll({"verdict": "failed", "run": {"id": "sha:11"}})       # not re-run yet
        self.assertFalse(s.ci_fired)
        s = await self._poll({"verdict": "running", "run": {"id": "sha:11"}})      # re-run starts
        self.assertEqual((s.ci_status, s.ci_fired, s.activity), ("running", False, Activity.WAITING_EXTERNAL))
        s = await self._poll({"verdict": "failed", "run": {"id": "sha:11"}})       # …and fails again
        self.assertTrue(s.ci_fired)
        self.assertEqual(s.activity, Activity.WAITING_USER)

    # ---- #4
    async def test_chat_to_a_done_task_does_not_start_an_agent(self):
        state.update(T, stage=Stage.DONE, activity=Activity.IDLE, session_id="old-sess")
        await self.o._chat(T, "one more thing", "user")
        self.assertEqual(FakeAgent.prompts, [])
        self.assertNotIn(T, self.o._agents)
        self.assertTrue(any("This task is done" in t for t in self.seen))

    async def test_marking_done_clears_the_session(self):
        state.update(T, stage=Stage.PR_OPEN, activity=Activity.WAITING_USER, session_id="s1")
        await self.o._advance(T)
        s = state.read(T)
        self.assertEqual((s.stage, s.session_id), (Stage.DONE, ""))
        self.assertIn(("done",), self.calls)

    # ---- #13
    async def test_rearm_review_guards(self):
        state.update(T, review_fired=True, ci_fired=True, activity=Activity.WAITING_USER)
        self.o._running.add(T)
        self.assertIn("error", self.o.rearm_review(T))
        self.o._running.discard(T)
        self.o.rearm_review(T)
        s = state.read(T)
        self.assertEqual((s.review_fired, s.activity), (False, Activity.WAITING_USER))  # CI result still needs you
        state.update(T, review_fired=True, ci_fired=False)
        self.o.rearm_review(T)
        self.assertEqual(state.read(T).activity, Activity.WAITING_EXTERNAL)

    # ---- #12
    async def test_transition_stays_marked_advancing_after_the_recap(self):
        agent = FakeAgent(T, "sys")
        agent.total_turns = 5
        self.o._agents[T] = agent
        self.o._advancing.add(T)
        with mock.patch.object(orch_mod.Orchestrator, "_save_summary_and_dispose", new=mock.AsyncMock()):
            await self.o._recap_then_dispose(T, "x", "Advancing")
        self.assertIn(T, self.o._advancing)

    # ---- #14
    async def test_poll_arming_per_ci_provider(self):
        write_profile("noci", f'repo = "{ROOT}"\n[ci]\nprovider = "none"\n')
        state.update(T, project="noci", ci_fired=False, review_fired=True)
        self.assertTrue(self.o._ci_done(state.read(T)))         # no CI: nothing more to wait for
        state.update(T, project="demo")
        self.assertFalse(self.o._ci_done(state.read(T)))

    async def test_manual_ci_reship_rearms_review(self):
        write_profile("jk2", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\n')
        state.update(T, project="jk2", stage=Stage.WORK, review_fired=True, ci_fired=True)
        with mock.patch.object(orch_mod.worktree, "branch_state",
                               return_value={"ok": True, "dirty": [], "ahead": 1, "log": "", "diffstat": ""}), \
             mock.patch.object(orch_mod.pr, "push"), \
             mock.patch.object(orch_mod.pr, "open_pr", return_value="https://github.com/o/r/pull/7"):
            self.o._advancing.add(T)
            await self.o._ship(T)
        s = state.read(T)
        self.assertEqual((s.stage, s.review_fired, s.ci_fired), (Stage.PR_OPEN, False, False))

    # ---- #15
    async def test_messages_name_the_real_base_branch(self):
        write_profile("trunkp", f'repo = "{ROOT}"\nbase_branch = "trunk"\n')
        state.update(T, project="trunkp", stage=Stage.WORK)
        with mock.patch.object(orch_mod.worktree, "branch_state",
                               return_value={"ok": True, "dirty": [], "ahead": 0, "log": "", "diffstat": ""}):
            await self.o._ship(T)
            kickoff = await self.o._episode_kickoff(T, None)
        self.assertTrue(any("origin/trunk" in t for t in self.seen), self.seen)
        self.assertIn("vs trunk", kickoff)
        self.assertNotIn("develop", kickoff)

    # ---- #9
    async def test_broadcast_from_a_worker_thread_is_handed_to_the_loop(self):
        self.o._loop = asyncio.get_running_loop()
        q = self.o.subscribe(T)
        th = threading.Thread(target=lambda: orch_mod.Orchestrator._broadcast(self.o, T, "system", "from thread"))
        th.start()
        th.join()
        event = await asyncio.wait_for(q.get(), 2)
        self.assertEqual(event["text"], "from thread")


class IdReuseTest(unittest.TestCase):
    # ---- #10
    def test_a_removed_tasks_id_is_never_reused(self):
        support.clean_state()
        first = sources.new_task_id("demo", "text")
        state.update(first)
        state.delete(first)                          # task removed (its worktree stays on disk)
        self.assertNotEqual(sources.new_task_id("demo", "text"), first)


class PrReadyTest(unittest.TestCase):
    # ---- #3
    def test_ready_uses_the_projects_ci_provider(self):
        from sprint_manager import ci, pr
        write_profile("noci2", f'repo = "{ROOT}"\n[ci]\nprovider = "none"\n')
        state.update("RD-1", project="noci2")
        gh = '{"url": "u", "state": "OPEN", "reviewDecision": "APPROVED", "mergeable": "MERGEABLE"}'
        with mock.patch.object(pr, "pr_number", return_value=7), \
             mock.patch.object(pr, "worktree_path", return_value=ROOT), \
             mock.patch.object(pr, "_run", return_value=gh):
            out = pr.ready("RD-1")
            self.assertEqual((out["ci"], out["ready"]), (ci.NONE, True))     # no CI: review decides
            state.update("RD-1", project="demo")
            with mock.patch.object(ci, "_github_verdict", return_value={"verdict": ci.FAILED}):
                self.assertFalse(pr.ready("RD-1")["ready"])
        state.delete("RD-1")
        self.assertEqual(project.load("noci2").ci["provider"], "none")


if __name__ == "__main__":
    unittest.main()
