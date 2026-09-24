"""Phase 4 — CI providers: GitHub checks (verdict / run watermark / logs / re-run, gh faked), the
"none" provider, dispatch, provider-specific prompt wording, and the agent's CI-command block.

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT, write_profile

from sprint_manager import ci, project  # noqa: E402
from sprint_manager.models import Stage  # noqa: E402

RUN = "https://github.com/acme/w/actions/runs/{}/job/1"


def rollup(*checks):
    return {"headRefOid": "abcdef1234567890", "statusCheckRollup": list(checks)}


def check(name, status="COMPLETED", conclusion="SUCCESS", run=11, workflow="CI"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": RUN.format(run), "workflowName": workflow}


class GitHubChecksTest(unittest.TestCase):
    def setUp(self):
        self.p = project.load("demo")   # no [ci] → github

    def _verdict(self, data):
        with mock.patch.object(ci, "_gh_json", return_value=data):
            return ci.verdict(self.p, 3)

    def test_default_provider_is_github_and_auto(self):
        self.assertEqual((ci.provider(self.p), ci.auto_triggers(self.p)), ("github", True))

    def test_verdicts(self):
        self.assertEqual(self._verdict(rollup())["verdict"], ci.NONE)
        self.assertEqual(self._verdict(rollup(check("a", conclusion="SKIPPED")))["verdict"], ci.NONE)
        self.assertEqual(self._verdict(rollup(check("a"), check("b", status="IN_PROGRESS", conclusion="")))["verdict"], ci.RUNNING)
        self.assertEqual(self._verdict(rollup(check("a"), check("b", conclusion="FAILURE")))["verdict"], ci.FAILED)
        passed = self._verdict(rollup(check("a"), check("b", conclusion="NEUTRAL")))
        self.assertEqual(passed["verdict"], ci.PASSED)
        legacy = {"__typename": "StatusContext", "context": "ext", "state": "PENDING", "targetUrl": "u"}
        self.assertEqual(self._verdict(rollup(check("a"), legacy))["verdict"], ci.RUNNING)

    def test_run_watermark_changes_with_commit_or_run(self):
        a = self._verdict(rollup(check("a", run=11)))["run"]["id"]
        b = self._verdict(rollup(check("a", run=12)))["run"]["id"]
        c = self._verdict({**rollup(check("a", run=11)), "headRefOid": "ffff00001111"})["run"]["id"]
        self.assertEqual(a, "abcdef123456:11")
        self.assertEqual(len({a, b, c}), 3)

    def test_rerun_only_failed_actions_runs(self):
        data = rollup(check("a", run=11), check("b", conclusion="FAILURE", run=12),
                      check("c", conclusion="FAILURE", run=12))
        calls = []
        with mock.patch.object(ci, "_gh_json", return_value=data), \
             mock.patch.object(ci.subprocess, "run",
                               side_effect=lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr="")):
            self.assertEqual(ci.trigger(self.p, 3), {"triggered": True, "reran": ["12"]})
        self.assertEqual(calls, [["gh", "run", "rerun", "12", "--failed"]])
        with mock.patch.object(ci, "_gh_json", return_value=rollup(check("a"))):
            self.assertFalse(ci.trigger(self.p, 3)["triggered"])

    def test_logs_for_failed_runs_and_external_checks(self):
        ext = {"__typename": "StatusContext", "context": "codecov", "state": "FAILURE", "targetUrl": "https://cc"}
        data = rollup(check("b", conclusion="FAILURE", run=12), ext)
        log = SimpleNamespace(returncode=0, stdout="\n".join(f"line {i}" for i in range(100)), stderr="")
        with mock.patch.object(ci, "_gh_json", return_value=data), \
             mock.patch.object(ci.subprocess, "run", return_value=log):
            out = ci.logs(self.p, 3, tail=5)
        stages = {s["stage"]: s["log_tail"] for s in out["failing_stages"]}
        self.assertEqual(stages["actions run 12"].splitlines(), [f"line {i}" for i in range(95, 100)])
        self.assertIn("https://cc", stages["codecov"])


class OtherProvidersTest(unittest.TestCase):
    def test_none_and_jenkins_dispatch(self):
        write_profile("noci", f'repo = "{ROOT}"\n[ci]\nprovider = "none"\n')
        p = project.load("noci")
        self.assertEqual(ci.verdict(p, 1)["verdict"], ci.NONE)
        self.assertFalse(ci.trigger(p, 1)["triggered"])
        self.assertFalse(ci.auto_triggers(p))
        write_profile("jen", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\nurl = "http://j"\njob = "/job/x/PR-{{pr}}"\n')
        j = project.load("jen")
        with mock.patch("sprint_manager.jenkins.verdict", return_value={"verdict": "passed"}) as v:
            self.assertEqual(ci.verdict(j, 4), {"verdict": "passed"})
        v.assert_called_once_with(j, 4)
        self.assertFalse(ci.auto_triggers(j))


@support.needs_sdk
class CiPromptAndGuardTest(unittest.TestCase):
    def test_prompt_wording_follows_the_provider(self):
        from sprint_manager.agent import build_system_prompt
        write_profile("pj", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\nurl = "http://j"\njob = "/j/PR-{{pr}}"\n')
        write_profile("pn", f'repo = "{ROOT}"\n[ci]\nprovider = "none"\n')
        gh = build_system_prompt(Stage.PR_OPEN, project.load("demo"))
        jk = build_system_prompt(Stage.PR_OPEN, project.load("pj"))
        no = build_system_prompt(Stage.PR_OPEN, project.load("pn"))
        self.assertIn("runs automatically on every push", gh)
        self.assertIn("Re-run CI", gh)
        self.assertIn("press **Trigger CI**", jk)
        self.assertIn("no CI configured", no)
        for text in (gh, jk, no):
            self.assertNotIn("<<", text)
            self.assertIn("sprint_manager.ci status --ticket", text)   # one CI command for all providers
            self.assertNotIn("sprint_manager.jenkins", text)

    def test_agent_cannot_start_ci_on_any_provider(self):
        from sprint_manager.agent import _merge_guard
        for cmd in ("python3 -m sprint_manager.ci trigger --ticket T", "gh run rerun 12 --failed",
                    "gh workflow run ci.yml", "python3 -m sprint_manager.jenkins trigger --ticket T"):
            verdict = asyncio.run(_merge_guard("Bash", {"command": cmd}, None))
            self.assertEqual(type(verdict).__name__, "PermissionResultDeny", cmd)
        ok = asyncio.run(_merge_guard("Bash", {"command": "python3 -m sprint_manager.ci status --ticket T"}, None))
        self.assertEqual(type(ok).__name__, "PermissionResultAllow")


if __name__ == "__main__":
    unittest.main()
