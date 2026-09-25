"""Phase 3 — text, GitHub-issue and Slack-thread tasks: ids, each source's content and lifecycle
hooks (gh / Slack faked), task creation and idempotence, batch GitHub intake, editing a text task,
and tracker comments folded into the pr-open review watermark (one GraphQL call for GitHub).

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT, write_profile

from sprint_manager import pr, project, sources, state, taskfile  # noqa: E402
from sprint_manager.models import TicketStatus  # noqa: E402
from sprint_manager.sources import github as gh_src, slack as slack_src  # noqa: E402

try:
    from sprint_manager import orchestrator as orch_mod
except ImportError:  # no venv
    orch_mod = None

ISSUE = {"number": 412, "title": "Crash on empty input", "body": "Steps: …",
         "labels": [{"name": "bug"}], "state": "OPEN",
         "url": "https://github.com/acme/widgets/issues/412",
         "comments": [{"author": {"login": "ann"}, "createdAt": "2026-09-01", "body": "me too"}]}
THREAD = {"channel": "C1", "channel_name": "eng", "url": "https://x.slack.com/archives/C1/p1700000000000100",
          "messages": [{"user": "Ann", "text": "Login times out after 30s\nsince Monday", "ts": "1"},
                       {"user": "Bob", "text": "repro on staging", "ts": "2"}]}


def _clean():
    support.clean_state()


class IdsAndTextSourceTest(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_ids_are_per_project_counters_or_issue_numbers(self):
        self.assertEqual(sources.new_task_id("demo", "text"), "demo-t-1")
        state.update("demo-t-1")
        state.update("demo-t-7")
        state.update("other-t-9")
        self.assertEqual(sources.new_task_id("demo", "text"), "demo-t-8")
        self.assertEqual(sources.new_task_id("demo", "slack"), "demo-slack-1")
        self.assertEqual(sources.new_task_id("demo", "github", 412), "demo-gh-412")

    def test_text_source_loads_the_taskfile(self):
        taskfile.write("demo-t-1", "The export button does nothing.")
        st = TicketStatus(ticket="demo-t-1", tracker="text", summary="Export broken", kind="bug")
        meta = sources.get("text").load(st, project.load("demo"))
        self.assertEqual((meta["summary"], meta["type"], meta["description"], meta["url"]),
                         ("Export broken", "Bug", "The export button does nothing.\n", ""))
        self.assertEqual((sources.get("text").reference(st), sources.get("text").pr_body_footer(st)), ("", ""))


class RegistryRegressionTest(unittest.TestCase):
    def test_importing_one_source_first_does_not_hide_the_others(self):
        # Regression: _load_builtin used to skip loading when the registry was non-empty, so a
        # direct `import sprint_manager.sources.github` left jira/text unregistered.
        import subprocess
        import sys
        from pathlib import Path
        code = ("import sprint_manager.sources.github\nfrom sprint_manager import sources\n"
                "print(','.join(sources.names()))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.assertEqual(out.stdout.strip(), "github,jira,slack,text", out.stderr)


class GitHubSourceTest(unittest.TestCase):
    def setUp(self):
        write_profile("ghp", f'repo = "{ROOT}"\n[github]\nassign_self = true\nin_progress_label = "wip"\n')
        self.p = project.load("ghp")
        self.src = sources.get("github")
        self.st = TicketStatus(ticket="ghp-gh-412", tracker="github", external_ref="412",
                               external_url=ISSUE["url"])

    def test_parse_refs(self):
        self.assertEqual(gh_src.parse_issue_ref(ISSUE["url"]), ("acme", "widgets", 412))
        self.assertEqual(gh_src.parse_issue_ref("#7"), ("", "", 7))
        self.assertEqual(gh_src.parse_issue_ref(" 7 "), ("", "", 7))
        self.assertIsNone(gh_src.parse_issue_ref("https://github.com/acme/widgets/pull/3"))

    def test_meta_reference_footer_and_linked_issue(self):
        meta = self.src.to_meta("ghp-gh-412", ISSUE)
        self.assertEqual((meta["type"], meta["status"], meta["summary"]), ("Bug", "open", ISSUE["title"]))
        self.assertEqual(meta["comments"][0]["author"], "ann")
        self.assertEqual(self.src.reference(self.st), "#412")
        self.assertEqual(self.src.pr_body_footer(self.st), "Fixes acme/widgets#412")
        self.assertEqual(self.src.linked_issue(self.st), ("acme", "widgets", 412))
        bare = TicketStatus(ticket="x", tracker="github", external_ref="9")
        self.assertEqual((self.src.pr_body_footer(bare), self.src.linked_issue(bare)), ("Fixes #9", None))

    def test_hooks_call_gh(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            out = json.dumps({**ISSUE, "state": "CLOSED"}) if args[1:3] == ["issue", "view"] else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        with mock.patch.object(gh_src.subprocess, "run", side_effect=fake_run):
            msg = self.src.on_work_start(self.st, self.p)
            self.assertEqual(calls[-1], ["gh", "issue", "edit", ISSUE["url"], "--add-assignee", "@me",
                                         "--add-label", "wip"])
            self.assertIn("assigned to you", msg)
            self.assertIn("is closed", self.src.on_done(self.st, self.p))
        self.assertIsNone(self.src.on_work_start(self.st, project.load("demo")))   # nothing configured
        self.assertIn("Fixes acme/widgets#412", self.src.on_shipped(self.st, self.p, "u", True))
        self.assertIsNone(self.src.on_shipped(self.st, self.p, "u", False))

    def test_gh_failure_is_a_source_error(self):
        with mock.patch.object(gh_src.subprocess, "run",
                               return_value=SimpleNamespace(returncode=1, stdout="", stderr="not found")):
            with self.assertRaises(sources.SourceError):
                self.src.load(self.st, self.p)


class SlackSourceTest(unittest.TestCase):
    def setUp(self):
        write_profile("slp", f'repo = "{ROOT}"\n[slack]\nreact = true\nreply_on_ship = true\n')
        self.p = project.load("slp")
        self.src = sources.get("slack")
        self.st = TicketStatus(ticket="slp-slack-1", tracker="slack", external_ref=THREAD["url"],
                               summary="Login timeout")

    def test_rendering(self):
        text = slack_src.render_thread(THREAD)
        self.assertIn("#eng", text)
        self.assertIn("**Bob**: repro on staging", text)
        self.assertEqual(slack_src.first_line(THREAD), "Login times out after 30s")

    def test_load_hooks_and_feedback(self):
        with mock.patch.object(slack_src.slack_api, "fetch_thread", return_value=THREAD), \
             mock.patch.object(slack_src.slack_api, "add_reaction") as react, \
             mock.patch.object(slack_src.slack_api, "post_reply") as reply:
            meta = self.src.load(self.st, self.p)
            self.assertEqual((meta["summary"], meta["url"]), ("Login timeout", THREAD["url"]))
            self.assertIn("repro on staging", meta["description"])
            self.src.on_work_start(self.st, self.p)
            react.assert_called_with(THREAD["url"], "eyes")
            self.src.on_shipped(self.st, self.p, "https://pr/1", True)
            reply.assert_called_once_with(THREAD["url"], "PR opened: https://pr/1")
            self.src.on_done(self.st, self.p)
            react.assert_called_with(THREAD["url"], "white_check_mark")
            self.assertEqual(self.src.feedback_count(self.st, self.p), 1)
            # write-backs are off unless the profile enables them
            react.reset_mock()
            self.assertIsNone(self.src.on_work_start(self.st, project.load("demo")))
            react.assert_not_called()
        self.assertEqual(self.src.pr_body_footer(self.st), f"Slack thread: {THREAD['url']}")


class ReviewSignalQueryTest(unittest.TestCase):
    def _run(self, linked):
        captured = {}

        def fake_run(args, cwd):
            captured["query"] = args[args.index("-f") + 1]
            captured["args"] = args
            data = {"repository": {"pullRequest": {
                "reviewDecision": None, "comments": {"totalCount": 1}, "reviews": {"totalCount": 1},
                "reviewThreads": {"nodes": [{"comments": {"totalCount": 2}}]}}}}
            if linked:
                data["linked"] = {"issue": {"comments": {"totalCount": 5}}}
            return json.dumps({"data": data})

        with mock.patch.object(pr, "worktree_path", return_value=ROOT), \
             mock.patch.object(pr, "_repo_slug", return_value="acme/widgets"), \
             mock.patch.object(pr, "_run", side_effect=fake_run):
            return pr.review_signal("T", 3, linked), captured

    def test_pr_only(self):
        rev, cap = self._run(None)
        self.assertEqual(rev, {"count": 4, "decision": "commented"})
        self.assertNotIn("linked", cap["query"])
        self.assertEqual(cap["query"].count("{"), cap["query"].count("}"))

    def test_linked_issue_rides_in_the_same_call(self):
        rev, cap = self._run(("acme", "widgets", 412))
        self.assertEqual(rev["count"], 9)
        self.assertIn("linked:repository(owner:$iowner,name:$iname){issue(number:$inum)", cap["query"])
        self.assertEqual(cap["query"].count("{"), cap["query"].count("}"))
        self.assertIn("inum=412", cap["args"])


@unittest.skipIf(orch_mod is None, "needs the venv (claude_agent_sdk)")
class TaskIntakeTest(unittest.TestCase):
    def setUp(self):
        _clean()
        self.o = orch_mod.Orchestrator()
        self.o._broadcast = lambda *a: None

    def test_text_task(self):
        res = self.o.create_task("demo", "text", title="Export broken", kind="bug",
                                 body="The export button does nothing.")
        self.assertEqual(res, {"key": "demo-t-1"})
        st = state.read("demo-t-1")
        self.assertEqual((st.tracker, st.project, st.kind, st.summary, st.external_ref),
                         ("text", "demo", "bug", "Export broken", ""))
        self.assertIn("does nothing", taskfile.read("demo-t-1"))
        for bad in ({"title": "", "body": "x"}, {"title": "x", "body": " "}):
            with self.assertRaises(ValueError):
                self.o.create_task("demo", "text", **bad)
        with self.assertRaises(ValueError):
            self.o.create_task("demo", "carrier-pigeon")

    def test_github_task_is_idempotent_by_issue_number(self):
        with mock.patch.object(gh_src.GitHubSource, "fetch", return_value=ISSUE):
            self.assertEqual(self.o.create_task("demo", "github", ref=ISSUE["url"]), {"key": "demo-gh-412"})
            self.o.create_task("demo", "github", ref="#412")
        st = state.read("demo-gh-412")
        self.assertEqual((st.tracker, st.kind, st.external_ref, st.external_url, st.summary),
                         ("github", "bug", "412", ISSUE["url"], ISSUE["title"]))
        self.assertEqual(len([s for s in state.all_statuses() if s.tracker == "github"]), 1)
        with self.assertRaises(ValueError):
            self.o.create_task("demo", "github", ref="not an issue")

    def test_slack_task_is_idempotent_by_thread(self):
        with mock.patch.object(slack_src.SlackSource, "fetch", return_value=THREAD):
            self.assertEqual(self.o.create_task("demo", "slack", ref=THREAD["url"], kind="bug"),
                             {"key": "demo-slack-1"})
            again = self.o.create_task("demo", "slack", ref=THREAD["url"])
        self.assertEqual(again, {"key": "demo-slack-1", "existing": True})
        st = state.read("demo-slack-1")
        self.assertEqual((st.summary, st.kind, st.external_ref),
                         ("Login times out after 30s", "bug", THREAD["url"]))
        self.assertIn("repro on staging", taskfile.read("demo-slack-1"))

    def test_load_github_issues(self):
        out = SimpleNamespace(returncode=0, stdout=json.dumps([ISSUE, {**ISSUE, "number": 5, "labels": []}]),
                              stderr="")
        with mock.patch("subprocess.run", return_value=out) as run:
            keys = self.o.load_github_issues("demo", assignee="@me", label="triage")
        self.assertEqual(keys, ["demo-gh-412", "demo-gh-5"])
        args = run.call_args.args[0]
        self.assertIn("--label", args)
        self.assertEqual(state.read("demo-gh-5").kind, "feature")

    def test_edit_text_task(self):
        self.o.create_task("demo", "text", title="T", body="old")
        self.assertTrue(self.o.update_task_text("demo-t-1", "new text")["updated"])
        self.assertEqual(taskfile.read("demo-t-1"), "new text\n")
        self.assertIn("error", self.o.update_task_text("demo-t-1", "  "))
        state.update("J-1", tracker="jira")
        self.assertIn("error", self.o.update_task_text("J-1", "x"))

    def test_review_signal_adds_slack_replies(self):
        st = TicketStatus(ticket="demo-slack-1", tracker="slack", external_ref=THREAD["url"])
        state.update("demo-slack-1", tracker="slack", external_ref=THREAD["url"])
        with mock.patch.object(orch_mod.pr, "review_signal", return_value={"count": 2, "decision": "none"}) as rs, \
             mock.patch.object(slack_src.SlackSource, "fetch", return_value=THREAD):
            rev = self.o._review_signal(st, 3)
        self.assertEqual(rev["count"], 3)          # 2 on the PR + 1 thread reply
        self.assertIsNone(rs.call_args.args[2])    # no linked GitHub issue for a Slack task

    def test_fresh_session_reloads_tracker_backed_tasks(self):
        import asyncio
        state.update("demo-gh-412", tracker="github", external_ref="412", project="demo")
        self.o._meta["demo-gh-412"] = {"description": "stale"}
        with mock.patch.object(orch_mod.Orchestrator, "_attach", new=mock.AsyncMock()), \
             mock.patch.object(orch_mod.Orchestrator, "_run_turn", new=mock.AsyncMock()), \
             mock.patch.object(orch_mod.Orchestrator, "_fetch_meta",
                               return_value={"key": "demo-gh-412", "description": "fresh", "comments": []}):
            asyncio.run(self.o._enter_stage("demo-gh-412", orch_mod.Stage.EXPLORE))
        self.assertEqual(self.o._meta["demo-gh-412"]["description"], "fresh")


if __name__ == "__main__":
    unittest.main()
