"""Phase 2 — task sources: legacy-row migration, the registry, the Jira source's lifecycle hooks
(Jira client faked), and how the orchestrator uses a source (hooks never raise; the ship title and
body take the source's reference and footer).

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import unittest
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT, write_profile

from sprint_manager import project, sources, state  # noqa: E402
from sprint_manager.jira_client import JiraError  # noqa: E402
from sprint_manager.models import Stage, TicketStatus  # noqa: E402

try:
    from sprint_manager import orchestrator as orch_mod
except ImportError:  # no venv
    orch_mod = None


class MigrationAndRegistryTest(unittest.TestCase):
    def test_legacy_rows_are_jira_tasks(self):
        s = TicketStatus.from_dict({"ticket": "AB-1", "stage": "work", "issue_type": "Bug"})
        self.assertEqual((s.tracker, s.external_ref, s.branch_kind), ("jira", "AB-1", "bug"))
        t = TicketStatus.from_dict({"ticket": "t-1", "tracker": "text", "kind": "feature"})
        self.assertEqual((t.external_ref, t.branch_kind), ("", "feature"))

    def test_registry(self):
        self.assertIsInstance(sources.get(""), type(sources.get("jira")))
        self.assertIn("jira", sources.names())
        with self.assertRaises(sources.SourceError):
            sources.get("carrier-pigeon")


class JiraSourceTest(unittest.TestCase):
    def setUp(self):
        write_profile("jp", f'repo = "{ROOT}"\n[jira]\nurl = "https://j.example"\n')
        self.p = project.load("jp")
        self.status = TicketStatus(ticket="AB-7", tracker="jira", external_ref="AB-7")
        self.src = sources.get("jira")
        self.client = mock.MagicMock()
        self.client.transition_issue.return_value = True
        patcher = mock.patch("sprint_manager.sources.jira.JiraClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lifecycle_hooks(self):
        self.assertEqual(self.src.on_work_start(self.status, self.p), "Jira → In Progress")
        self.client.transition_issue.assert_called_with("AB-7", "In Progress")
        self.src.on_shipped(self.status, self.p, "https://pr/1", True)
        self.client.transition_issue.assert_called_with("AB-7", "In Review")
        self.client.add_comment.assert_called_once_with("AB-7", "PR opened: https://pr/1")
        self.src.on_shipped(self.status, self.p, "https://pr/1", False)   # re-ship: no re-comment
        self.assertEqual(self.client.add_comment.call_count, 1)
        self.client.transition_issue.return_value = False
        self.assertIn("no 'Done' transition", self.src.on_done(self.status, self.p))
        self.assertEqual((self.src.reference(self.status), self.src.pr_body_footer(self.status)),
                         ("AB-7", ""))

    def test_load_errors_become_source_errors(self):
        self.client.get_issue.side_effect = JiraError("HTTP 404")
        with self.assertRaises(sources.SourceError):
            self.src.load(self.status, self.p)


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class OrchestratorSourceUseTest(unittest.TestCase):
    def test_hook_failures_are_reported_not_raised(self):
        o = orch_mod.Orchestrator()
        state.update("HK-1", project="demo", tracker="jira")
        seen = []
        o._broadcast = lambda t, kind, text: seen.append(text)
        broken = mock.MagicMock()
        broken.on_done.side_effect = sources.SourceError("site down")
        with mock.patch.object(orch_mod.sources, "get", return_value=broken):
            o._hook("HK-1", "on_done")
        self.assertEqual(seen, ["jira update skipped: site down"])
        state.delete("HK-1")

    def test_pr_title_uses_the_source_reference(self):
        parse = orch_mod._parse_pr_block
        self.assertEqual(parse("### PR\ntitle: Fix it\nbody:\nb", "#412", "")[0], "#412: Fix it")
        self.assertEqual(parse("### PR\ntitle: Fix it\nbody:\nb", "", "")[0], "Fix it")
        self.assertEqual(parse("### Summary\n\nDid it.", "", "Parser bug"), ("Parser bug", "Did it."))
        self.assertEqual(parse("", "", "")[0], "Change")

    def test_ship_recap_prompt_prefix(self):
        self.assertIn("title: AB-1: <short", orch_mod._SHIP_RECAP_PROMPT.format(title_prefix="AB-1: "))
        self.assertIn("title: <short", orch_mod._SHIP_RECAP_PROMPT.format(title_prefix=""))

    def test_stage_enum_unchanged_by_sources(self):
        self.assertEqual(Stage("work"), Stage.WORK)


if __name__ == "__main__":
    unittest.main()
