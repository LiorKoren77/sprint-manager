"""Phase 5 — tasks that aren't groomed tracker tickets: the first message marks them, explore's G1
asks for assumptions + acceptance criteria, work's G4 checks them, the PR body carries them, and a
text / Slack task can be filed as a GitHub issue. Also: the prompts no longer assume Jira.

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)

from sprint_manager import notes, project, state, taskfile  # noqa: E402
from sprint_manager.models import Stage, TicketStatus  # noqa: E402

try:
    from sprint_manager import orchestrator as orch_mod
    from sprint_manager.agent import _agent_env, build_context_message, build_system_prompt
except ImportError:  # no venv
    orch_mod = None


def _meta(key, url=""):
    return {"key": key, "type": "Bug", "summary": "S", "url": url, "description": "D", "comments": []}


@support.needs_sdk
class FirstMessageTest(unittest.TestCase):
    def test_source_and_reference_per_tracker(self):
        jira = build_context_message(_meta("AB-1", "https://j/browse/AB-1"), "",
                                     TicketStatus(ticket="AB-1", tracker="jira"))
        self.assertIn("Source: Jira issue (a groomed tracker ticket)", jira)
        self.assertIn("Reference: AB-1", jira)
        self.assertNotIn("not groomed", jira)
        gh = build_context_message(_meta("demo-gh-4"), "",
                                   TicketStatus(ticket="demo-gh-4", tracker="github", external_ref="4"))
        self.assertIn("Reference: #4", gh)
        self.assertIn("not groomed", gh)
        text = build_context_message(_meta("demo-t-1"), "", TicketStatus(ticket="demo-t-1", tracker="text"))
        self.assertIn("free text", text)
        self.assertIn("Reference: (none)", text)
        self.assertIn("Link: (none)", text)
        self.assertNotIn("<<", jira + gh + text)

    def test_agent_env_carries_the_reference(self):
        self.assertEqual(_agent_env("demo-gh-4", "/c", "/c", project.load("demo"), "#4")["SM_REF"], "#4")
        self.assertEqual(_agent_env("demo-t-1", "/c", "/c", None)["SM_REF"], "")


@support.needs_sdk
class PromptWordingTest(unittest.TestCase):
    def setUp(self):
        self.p = project.load("demo")

    def test_prompts_are_tracker_neutral(self):
        for stage in (Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN):
            text = build_system_prompt(stage, self.p)
            for phrase in ("Jira ticket", "ticket key", "the Jira issue", "moves Jira"):
                self.assertNotIn(phrase, text, (stage, phrase))
            self.assertIn("$SM_REF", text)

    def test_acceptance_criteria_steps(self):
        explore = build_system_prompt(Stage.EXPLORE, self.p)
        self.assertIn("marks the task as not groomed", explore)
        self.assertIn("### Acceptance criteria", explore)
        self.assertIn("acceptance criteria", build_system_prompt(Stage.WORK, self.p).lower())

    @unittest.skipIf(orch_mod is None, "needs the venv")
    def test_pr_body_asks_for_the_criteria_checklist(self):
        self.assertIn("acceptance criteria", orch_mod._SHIP_RECAP_PROMPT.lower())


class AcceptanceCriteriaExtractionTest(unittest.TestCase):
    def test_latest_block_wins_and_stops_at_next_heading(self):
        state.update("AC-1")
        notes.append_section("AC-1", "explore — summary",
                             "Plan…\n\n### Acceptance criteria\n\n1. old\n\n### Risks\n- r")
        notes.append_section("AC-1", "explore — mid-stage compact",
                             "### Acceptance criteria\n1. Export works\n2. Errors are shown\n\n## next")
        self.assertEqual(notes.latest_heading_block("AC-1", "Acceptance criteria"),
                         "1. Export works\n2. Errors are shown")
        self.assertEqual(notes.latest_heading_block("AC-1", "Nothing here"), "")
        notes.delete("AC-1")
        state.delete("AC-1")


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class FileAsIssueTest(unittest.TestCase):
    def setUp(self):
        for s in state.all_statuses():
            state.delete(s.ticket)
        self.o = orch_mod.Orchestrator()
        self.o._broadcast = lambda *a: None

    def test_text_task_becomes_a_github_task_keeping_its_id(self):
        self.o.create_task("demo", "text", title="Export broken", body="Nothing happens.")
        notes.append_section("demo-t-1", "explore — summary", "### Acceptance criteria\n1. Export works")
        created = SimpleNamespace(returncode=0, stdout="https://github.com/acme/w/issues/77\n", stderr="")
        with mock.patch("subprocess.run", return_value=created) as run:
            res = self.o.file_as_issue("demo-t-1")
        self.assertEqual(res["issue"], 77)
        args = run.call_args.args[0]
        body = args[args.index("--body") + 1]
        self.assertIn("Nothing happens.", body)
        self.assertIn("### Acceptance criteria\n\n1. Export works", body)
        self.assertEqual(args[args.index("--title") + 1], "Export broken")
        st = state.read("demo-t-1")
        self.assertEqual((st.ticket, st.tracker, st.external_ref, st.external_url),
                         ("demo-t-1", "github", "77", "https://github.com/acme/w/issues/77"))
        self.assertTrue(taskfile.read("demo-t-1"))  # the problem text stays on disk
        notes.delete("demo-t-1")

    def test_refusals(self):
        state.update("J-1", tracker="jira")
        self.assertIn("error", self.o.file_as_issue("J-1"))
        self.o.create_task("demo", "text", title="T", body="B")
        failed = SimpleNamespace(returncode=1, stdout="", stderr="not authenticated")
        with mock.patch("subprocess.run", return_value=failed):
            self.assertIn("not authenticated", self.o.file_as_issue("demo-t-1")["error"])
        self.assertEqual(state.read("demo-t-1").tracker, "text")


if __name__ == "__main__":
    unittest.main()
