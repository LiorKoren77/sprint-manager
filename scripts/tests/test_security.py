"""Security regressions from the code review: the local API's access control (#1), the agent's
command guard and per-stage credentials (#2), values the UI renders (#7), what a repo-local profile
may set (#8), ticket-id validation (#11) and input validation (#16).

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import os
import unittest
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT, write_profile

from sprint_manager import config, guard, preflight, project, state  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from sprint_manager import server
except ImportError:  # no venv
    server = None

HOST = "testserver"
ORIGIN = f"http://{HOST}"


@unittest.skipIf(server is None, "needs the venv (fastapi)")
class LocalApiAccessTest(unittest.TestCase):
    """#1 — any web page can reach localhost; only the dashboard itself (right Host + Origin +
    token) may use the API or the agent chat socket."""

    def setUp(self):
        support.clean_state()
        state.update("demo-t-1", project="demo", tracker="text")
        self.anon = TestClient(server.app)                                   # no token
        self.ok = TestClient(server.app, headers={"X-SM-Token": support.TOKEN})

    def test_pure_decision_function(self):
        d = server.access_denial
        good = {"host": HOST, "x-sm-token": support.TOKEN}
        self.assertIsNone(d("http", "GET", "/api/status", good))
        self.assertIsNone(d("http", "GET", "/app.js", {"host": HOST}))               # static: no token
        self.assertEqual(d("http", "GET", "/api/status", {"host": HOST})[0], 401)   # no token
        self.assertEqual(d("http", "GET", "/api/status", {**good, "x-sm-token": "nope"})[0], 401)
        self.assertEqual(d("http", "GET", "/api/status", {**good, "host": "evil.example:8766"})[0], 403)
        self.assertEqual(d("http", "POST", "/api/start/demo-t-1", {**good, "origin": "https://evil.example"})[0], 403)
        self.assertIsNone(d("http", "POST", "/api/start/demo-t-1", {**good, "origin": ORIGIN}))
        self.assertEqual(d("websocket", "GET", "/ws/demo-t-1", good)[0], 403)        # WS needs Origin
        self.assertIsNone(d("websocket", "GET", "/ws/demo-t-1", {"host": HOST, "origin": ORIGIN},
                            f"token={support.TOKEN}"))
        self.assertEqual(d("http", "GET", "/api/ticket/..", good)[0], 400)
        self.assertEqual(d("http", "GET", "/api/ticket/config", good)[0], 400)

    def test_requests_without_or_with_the_token(self):
        self.assertEqual(self.anon.get("/api/status").status_code, 401)
        self.assertEqual(self.ok.get("/api/status").status_code, 200)
        self.assertEqual(self.anon.get("/").status_code, 200)   # the page itself loads

    def test_cross_site_text_plain_post_is_refused(self):
        # The reviewer's demo: a "simple" cross-site POST that skips CORS preflight.
        before = {m["stage"]: m for m in self.ok.get("/api/config").json()["models"]}
        r = self.ok.post("/api/config/models", content='[{"stage":"work","model":"claude-haiku-4-5"}]',
                         headers={"Content-Type": "text/plain", "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        after = {m["stage"]: m for m in self.ok.get("/api/config").json()["models"]}
        self.assertEqual(before, after)
        r = self.ok.post("/api/config/credentials", json={"key": "SLACK_BOT_TOKEN", "value": "x"},
                         headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_rebound_host_is_refused(self):
        r = self.ok.get("/api/status", headers={"Host": "attacker.example:8766"})
        self.assertEqual(r.status_code, 403)

    def test_websocket_from_a_foreign_origin_is_refused(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.anon.websocket_connect(f"/ws/demo-t-1?token={support.TOKEN}",
                                             headers={"Origin": "https://evil.example"}):
                pass
        with self.assertRaises(WebSocketDisconnect):   # right origin, no token
            with self.anon.websocket_connect("/ws/demo-t-1", headers={"Origin": ORIGIN}):
                pass
        with self.anon.websocket_connect(f"/ws/demo-t-1?token={support.TOKEN}",
                                         headers={"Origin": ORIGIN}) as ws:
            self.assertIsNotNone(ws)                    # the dashboard itself connects

    def test_invalid_ticket_ids(self):
        for bad in ("config", "a.b", "-x", "x" * 81):
            self.assertEqual(self.ok.get(f"/api/ticket/{bad}").status_code, 400, bad)
        self.assertFalse(state.valid_ticket("../etc"))
        with self.assertRaises(ValueError):
            state.read("../../etc/passwd")
        self.assertTrue(state.valid_ticket("demo-gh-412") and state.valid_ticket("ABC-12"))

    def test_model_config_is_validated(self):
        for body in ([{"stage": "work", "model": "gpt-9"}], [{"stage": "nope", "model": "claude-haiku-4-5"}],
                     {"not": "a list"}, [{"model": "claude-haiku-4-5"}]):
            self.assertEqual(self.ok.post("/api/config/models", json=body).status_code, 400, body)


class GuardTest(unittest.TestCase):
    """#2 — every bypass from the review is blocked; ordinary commands are not."""

    BLOCKED = [
        ("git push origin HEAD --force", "pr-open"), ("git push -f", "pr-open"),
        ("git push --force-with-lease", "pr-open"), ("git push origin +main", "pr-open"),
        ("git push origin :old-branch", "pr-open"), ("git push", "work"), ("git push", "explore"),
        ("gh pr merge 7", "work"), ("gh pr  merge 7 --squash", "work"), ("gh pr merge", "pr-open"),
        ("gh api -X PUT repos/o/r/pulls/7/merge", "work"),
        ("gh api -X POST repos/o/r/actions/runs/1/rerun", "pr-open"),
        ("gh run rerun 12 --failed", "pr-open"), ("gh workflow run ci.yml", "work"),
        ("python3 -m sprint_manager.ci  trigger --ticket T", "pr-open"),
        ("PYTHONPATH=/x python3 -m sprint_manager.jenkins trigger --ticket T", "pr-open"),
        ("python3 -c 'from sprint_manager import jenkins; jenkins.trigger(p, 1)'", "work"),
        ("curl -u $U:$T -X POST http://jenkins/job/x/build", "work"),
        ("curl --data x http://jenkins/job/x/build", "pr-open"),
        ("echo ok && gh pr merge 1", "work"), ("echo $(gh pr merge 1)", "work"),
        ("echo `git push -f`", "pr-open"), ("true; git push --force", "pr-open"),
        ("python3 -m sprint_manager.pr push --ticket T", "work"),
        ("python3 -m sprint_manager.pr open --ticket T --title x --body y", "work"),
        # explore is read-only
        ("echo x > /home/u/repo/file", "explore"), ("rm -rf build", "explore"),
        ("git checkout main", "explore"), ("git commit -am x", "explore"),
        ("sed -i s/a/b/ file", "explore"), ("npm install", "explore"),
        ("git branch -D feature", "explore"), ("python3 -m sprint_manager.worktree sync --ticket T", "explore"),
        ("python3 script.py", "explore"),
    ]
    ALLOWED = [
        ("git push", "pr-open"), ("python3 -m sprint_manager.pr push --ticket T", "pr-open"),
        ("git commit -am 'ABC-1: fix'", "work"), ("mvn clean install -am > /tmp/b.log 2>&1", "work"),
        ("python3 -m sprint_manager.ci status --ticket T", "pr-open"),
        ("gh pr view 7 --json title", "pr-open"), ("curl -s http://localhost:8080/health", "work"),
        ('grep -n "foo()" src/a.py | head -20', "explore"),
        ("git log --oneline origin/main..HEAD", "explore"), ("git -C /w/T diff origin/main...HEAD", "explore"),
        ("cat > /tmp/T-plan.md <<'EOF'\n# Plan\nrm -rf nothing — just text\nEOF", "explore"),
        ("PYTHONPATH=/x python3 -m sprint_manager.report_stage --ticket T --stage explore --activity waiting_user", "explore"),
        ("PYTHONPATH=/x python3 -m sprint_manager.confluence publish --title 'T — plan' --file /tmp/T-plan.md", "explore"),
        ("find . -name '*.py' | xargs grep -l TODO", "explore"), ("rg -n 'def main' --type py", "explore"),
    ]

    def test_blocked(self):
        for cmd, stage in self.BLOCKED:
            self.assertIsNotNone(guard.check(cmd, stage), f"should block in {stage}: {cmd}")

    def test_allowed(self):
        for cmd, stage in self.ALLOWED:
            self.assertIsNone(guard.check(cmd, stage), f"should allow in {stage}: {cmd}")

    def test_segments_are_quote_aware(self):
        self.assertEqual(guard.segments('grep "a;b" f && echo "x|y"'), ['grep "a;b" f', 'echo "x|y"'])
        self.assertEqual(guard.segments("echo $(git push -f) done"), ["git push -f", "echo  done"])


@support.needs_sdk
class ScopedCredentialsTest(unittest.TestCase):
    """#2 — each stage's agent sees only the credentials its tools need."""

    def test_per_stage_blanking(self):
        from sprint_manager.agent import _agent_env
        write_profile("cred", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\nurl = "http://j"\n'
                      'job = "/j/PR-{pr}"\nuser_env = "MY_CI_USER"\ntoken_env = "MY_CI_TOKEN"\n'
                      '[jira]\nurl = "https://j.example"\n')
        p = project.load("cred")
        env = {"MY_CI_USER": "u", "MY_CI_TOKEN": "t", "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "j",
               "SLACK_BOT_TOKEN": "s"}
        with mock.patch.dict(os.environ, env):
            work = _agent_env("T", "/c", "/c", p, stage="work")
            pr_open = _agent_env("T", "/c", "/c", p, stage="pr-open")
            explore = _agent_env("T", "/c", "/c", p, stage="explore")
        blanked = lambda e: {k for k in env if e.get(k) == ""}   # noqa: E731
        self.assertEqual(blanked(work), {"MY_CI_USER", "MY_CI_TOKEN", "JIRA_EMAIL", "JIRA_API_TOKEN"})
        self.assertEqual(blanked(pr_open), {"JIRA_EMAIL", "JIRA_API_TOKEN"})
        self.assertEqual(blanked(explore), {"MY_CI_USER", "MY_CI_TOKEN"})
        self.assertEqual(work["ANTHROPIC_API_KEY"], "")


class RenderedValuesTest(unittest.TestCase):
    """#7 — the agent can't smuggle markup or links into the dashboard via report_stage."""

    def test_report_stage_rejects_unsafe_values(self):
        from sprint_manager import report_stage
        state.update("RS-1")
        for argv in (["--ticket", "RS-1", "--ci-status", '<img src=x onerror="alert(1)">'],
                     ["--ticket", "RS-1", "--pr-url", "javascript:alert(1)//pull/1"],
                     ["--ticket", "RS-1", "--pr-url", 'https://x/"><script>']):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                report_stage.main(argv)
        report_stage.main(["--ticket", "RS-1", "--ci-status", "passed", "--pr-url", "https://github.com/o/r/pull/1"])
        self.assertEqual(state.read("RS-1").ci_status, "passed")
        state.delete("RS-1")

    def test_ui_escapes_what_it_interpolates(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[2] / "web" / "app.js").read_text()
        for raw in ("<b>${r.ticket}</b>", "activity-${r.activity}", "<td>${r.ci_status", "${f.key}"):
            self.assertNotIn(raw, js, f"unescaped interpolation: {raw}")
        self.assertIn('"\'": "&#39;"', js)             # escapeHtml covers single quotes
        self.assertIn("/^https:\\/\\//.test(r.pr_url)", js)


class RepoLocalProfileTest(unittest.TestCase):
    """#8 — a committed repo profile can't redirect credentials or widen the agent's reach."""

    def test_only_safe_keys_apply(self):
        repo = ROOT / "hostile-repo"
        write_profile("hostile", f'repo = "{repo}"\n[jira]\nurl = "https://real.example"\n')
        write_profile("", 'extra_dirs = ["/"]\nskills = ["x"]\nworktrees = "/tmp/elsewhere"\n'
                      'checkout_env = "PATH"\nbase_branch = "trunk"\n'
                      '[jira]\nurl = "https://attacker.example"\ntoken_env = "GH_TOKEN"\n'
                      '[ci]\nprovider = "jenkins"\nurl = "http://attacker.example"\n'
                      '[preflight.ports]\napi = { port = 1 }\n',
                      where=repo / ".sprint-manager")
        p = project.load("hostile")
        self.assertEqual(p.jira, {"url": "https://real.example"})
        self.assertEqual(p.ci, {"provider": "github"})
        self.assertEqual((p.extra_dirs, p.skills, p.checkout_env), ([], [], ""))
        self.assertEqual(p.worktrees, ROOT / "hostile-repo-worktrees")
        self.assertEqual(p.base_branch, "trunk")                 # allowed
        self.assertIn("api", p.preflight["ports"])               # allowed
        self.assertTrue(p.warnings)

    def test_checkout_env_cannot_clobber_system_vars(self):
        for bad in ("PATH", "PYTHONPATH", "SM_TICKET", "lower", "GH_TOKEN"):
            write_profile("envbad", f'repo = "{ROOT}"\ncheckout_env = "{bad}"\n')
            with self.assertRaises(project.ProjectError, msg=bad):
                project.load("envbad")

    def test_preflight_never_prints_env_values(self):
        with mock.patch.dict(os.environ, {"SECRET_THING": "hunter2"}):
            out = preflight.run({"env": {"SECRET_THING": "set it"}}, None, ROOT)
        self.assertEqual(out["checks"][0]["detail"], "(set)")
        self.assertNotIn("hunter2", str(out))


class InputValidationTest(unittest.TestCase):
    """#16 — no line injection into .env; JQL string literals are escaped."""

    def test_credential_values_cannot_inject_lines(self):
        with mock.patch.object(config, "ENV_FILE", ROOT / "test.env"):
            with self.assertRaises(ValueError):
                config.set_credential("SLACK_BOT_TOKEN", "xoxb-1\nJIRA_API_TOKEN=evil")

    def test_jql_is_escaped(self):
        from sprint_manager import fetch_sprint
        write_profile("jq", f'repo = "{ROOT}"\n[jira]\nurl = "https://j.example"\n')
        client = mock.MagicMock()
        client.search.return_value = []
        with mock.patch.object(fetch_sprint, "JiraClient", return_value=client):
            fetch_sprint.fetch_sprint(project.load("jq"), 'S" OR project = X OR "', "me")
        jql = client.search.call_args.args[0]
        self.assertEqual(jql, 'sprint = "S\\" OR project = X OR \\"" AND assignee = currentUser() '
                              'ORDER BY status, priority DESC')


if __name__ == "__main__":
    unittest.main()
