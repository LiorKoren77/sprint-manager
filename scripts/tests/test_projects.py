"""Phase 1 — projects: profile loading/defaults/merging, resolution order, prompt rendering from a
profile, preflight from data, worktrees against a real (temp) git repo with a non-default base
branch, Jenkins/Jira parameterisation, and the guard against re-coupling to any one repo/company.

    cd scripts && PYTHONPATH=$PWD python3 -m unittest discover -s tests
"""

import os
import re
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import PROJECTS as PROJECTS_DIR, ROOT, write_profile

from sprint_manager import config, preflight, project, state  # noqa: E402
from sprint_manager.models import Stage  # noqa: E402

APP = Path(__file__).resolve().parents[2]


class ProfileLoadingTest(unittest.TestCase):
    def test_minimal_profile_defaults(self):
        repo = ROOT / "minimal-repo"
        write_profile("minimal", f'repo = "{repo}"\n')
        p = project.load("minimal")
        self.assertEqual(p.repo, repo)
        self.assertEqual(p.worktrees, ROOT / "minimal-repo-worktrees")
        self.assertEqual(p.ci["provider"], "github")
        self.assertEqual((p.checkout_env, p.skills, p.extra_dirs, p.jira), ("", [], [], {}))
        self.assertFalse(p.has_jira or p.has_confluence or p.has_preflight)

    def test_repo_local_profile_overrides_personal_and_resolves_its_own_paths(self):
        repo = ROOT / "layered-repo"
        personal = write_profile("layered", f'repo = "{repo}"\nskills = ["a"]\n'
                                 '[ci]\nprovider = "jenkins"\nurl = "http://ci"\njob = "/job/x/PR-{pr}"\n'
                                 '[prompts]\nnotes = "mine.md"\n', {"mine.md": "personal notes"})
        local = write_profile("", 'skills = ["b"]\n[ci]\nurl = "http://other"\n'
                              '[prompts]\nwork = "work.md"\n', {"work.md": "repo work notes"},
                              where=repo / ".sprint-manager")
        p = project.load("layered")
        self.assertEqual(p.skills, ["b"])                               # repo-local wins
        self.assertEqual(p.ci, {"provider": "jenkins", "url": "http://other", "job": "/job/x/PR-{pr}"})
        self.assertEqual(p.prompts["notes"], (personal / "mine.md").resolve())
        self.assertEqual(p.prompts["work"], (local / "work.md").resolve())
        self.assertEqual(p.prompt_text("work"), "repo work notes")
        self.assertEqual(len(p.profile_files), 2)

    def test_invalid_profiles_are_rejected_with_a_hint(self):
        write_profile("badkey", f'repo = "{ROOT}"\n[prompts]\nwhatever = "x.md"\n')
        write_profile("badci", f'repo = "{ROOT}"\n[ci]\nprovider = "travis"\n')
        write_profile("norepo", 'base_branch = "main"\n')
        for name in ("badkey", "badci", "norepo", "does-not-exist"):
            with self.assertRaises(project.ProjectError):
                project.load(name)

    def test_base_branch_setting_wins_over_detection(self):
        self.assertEqual(project.load("demo").base_branch, "main")


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        write_profile("other", f'repo = "{ROOT / "other-repo"}"\n')
        state.update("RES-1", project="other")
        state.update("RES-2")  # no project → default

    def tearDown(self):
        os.environ.pop("SM_PROJECT", None)
        for t in ("RES-1", "RES-2"):
            state.delete(t)

    def test_order_explicit_env_ticket_default(self):
        self.assertEqual(project.resolve().name, "demo")                       # default
        self.assertEqual(project.resolve(ticket="RES-1").name, "other")        # ticket's project
        self.assertEqual(project.resolve(ticket="RES-2").name, "demo")
        os.environ["SM_PROJECT"] = "demo"
        self.assertEqual(project.resolve(ticket="RES-1").name, "demo")         # agent env wins
        self.assertEqual(project.resolve("other", ticket="RES-1").name, "other")  # explicit wins

    def test_for_ticket_ignores_agent_env(self):
        os.environ["SM_PROJECT"] = "demo"
        self.assertEqual(project.for_ticket("RES-1").name, "other")

    def test_register_is_idempotent_and_refuses_a_clash(self):
        repo = ROOT / "regrepo"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        p1 = project.register(str(repo))
        self.assertEqual((p1.name, p1.repo), ("regrepo", repo.resolve()))
        self.assertEqual(project.register(str(repo)).name, "regrepo")
        other = ROOT / "elsewhere" / "regrepo"
        (other / ".git").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(project.ProjectError):
            project.register(str(other))
        with self.assertRaises(project.ProjectError):
            project.register(str(ROOT / "not-a-repo"))


@support.needs_sdk
class PromptRenderingTest(unittest.TestCase):
    def _proj(self, name, extra="", files=None):
        write_profile(name, f'repo = "{ROOT / (name + "-repo")}"\nbase_branch = "trunk"\n' + extra, files)
        return project.load(name)

    def test_bare_project_gets_generic_prompts(self):
        from sprint_manager.agent import build_system_prompt
        p = self._proj("bare")
        for stage in (Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN):
            text = build_system_prompt(stage, p)
            self.assertNotIn("<<", text)
            self.assertNotIn("## Project notes", text)
            self.assertIn("base branch `trunk`", text)
        work = build_system_prompt(Stage.WORK, p)
        self.assertIn("documents no test commands", work)
        self.assertNotIn("sprint_manager.preflight", work)   # the preflight section is dropped
        self.assertNotIn("Publishing to Confluence", build_system_prompt(Stage.EXPLORE, p))

    def test_profile_notes_tests_confluence_and_preflight_render(self):
        from sprint_manager.agent import build_system_prompt
        p = self._proj("rich", '[prompts]\nnotes = "n.md"\nwork = "w.md"\ntest_commands = "t.md"\n'
                       '[preflight.ports]\napi = { port = 1 }\n'
                       '[jira]\nurl = "https://j.example"\nconfluence_space = "ENG"\n',
                       {"n.md": "ALL-STAGES-NOTE", "w.md": "WORK-ONLY-NOTE", "t.md": "suites"})
        work = build_system_prompt(Stage.WORK, p)
        explore = build_system_prompt(Stage.EXPLORE, p)
        self.assertIn("## Project notes (rich)", work)
        self.assertIn("ALL-STAGES-NOTE", work)
        self.assertIn("WORK-ONLY-NOTE", work)
        self.assertNotIn("WORK-ONLY-NOTE", explore)
        self.assertIn(str(p.prompts["test_commands"]), work)
        self.assertIn("preflight", work)
        self.assertIn("Publishing to Confluence", explore)
        self.assertNotIn("<<", work + explore)

    def test_agent_env_carries_project_and_checkout_var(self):
        from sprint_manager.agent import _agent_env
        p = self._proj("envproj", 'checkout_env = "MYCHECKOUT"\n')
        env = _agent_env("T-1", "/wt/T-1", "/wt/T-1", p)
        self.assertEqual((env["SM_PROJECT"], env["SM_REPO"], env["MYCHECKOUT"], env["SM_TICKET"]),
                         ("envproj", str(p.repo), "/wt/T-1", "T-1"))
        self.assertNotIn("MYCHECKOUT", _agent_env("T-1", "/x", "", self._proj("noenv")))


class PreflightTest(unittest.TestCase):
    def test_ports_paths_env_and_unknown(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        open_port = listener.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        checkout = Path(tempfile.mkdtemp())
        (checkout / "venv").mkdir()
        spec = {"ports": {"up": {"port": open_port}, "down": {"port": closed_port, "hint": "start it"}},
                "paths": {"rel": "venv", "abs": str(checkout / "missing")},
                "env": {"SM_TEST_UNSET_VAR": "set it"}}
        try:
            result = preflight.run(spec, None, checkout)
        finally:
            listener.close()
        by = {c["name"]: c for c in result["checks"]}
        self.assertTrue(by["port:up"]["ok"])
        self.assertEqual((by["port:down"]["ok"], by["port:down"]["hint"]), (False, "start it"))
        self.assertTrue(by["rel"]["ok"])
        self.assertFalse(by["abs"]["ok"])
        self.assertFalse(by["env:SM_TEST_UNSET_VAR"]["ok"])
        self.assertFalse(result["all_ok"])
        only = preflight.run(spec, ["up", "nope"], checkout)
        self.assertNotIn("port:down", {c["name"] for c in only["checks"]})
        self.assertEqual(only["unknown"], ["nope"])

    def test_no_preflight_table_is_ok(self):
        self.assertEqual(preflight.run({}, None, Path(".")), {"all_ok": True, "checks": []})


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class WorktreeAgainstRealGitTest(unittest.TestCase):
    """A real origin (bare) + clone whose base branch is 'trunk' — nothing assumes develop/main."""

    @classmethod
    def setUpClass(cls):
        base = Path(tempfile.mkdtemp(prefix="sm-git-"))
        origin, seed, clone = base / "origin.git", base / "seed", base / "repo"
        _git("init", "--bare", "-b", "trunk", str(origin), cwd=base)
        _git("init", "-b", "trunk", str(seed), cwd=base)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
            _git("config", k, v, cwd=seed)
        (seed / "a.txt").write_text("a\n")
        _git("add", ".", cwd=seed)
        _git("commit", "-m", "init", cwd=seed)
        _git("remote", "add", "origin", str(origin), cwd=seed)
        _git("push", "origin", "trunk", cwd=seed)
        _git("clone", str(origin), str(clone), cwd=base)
        write_profile("gitproj", f'repo = "{clone}"\nworktrees = "{base / "wts"}"\n')
        cls.base, cls.clone = base, clone
        state.update("GIT-1", project="gitproj", summary="Fix the parser")

    def test_detects_base_branch_and_creates_worktree_from_it(self):
        from sprint_manager import worktree
        p = project.load("gitproj")
        self.assertEqual(p.base_branch, "trunk")          # detected from origin/HEAD
        tree = worktree.add("GIT-1", "Bug", "Fix the parser")
        self.assertEqual(Path(tree["path"]), self.base / "wts" / "GIT-1")
        self.assertTrue(tree["branch"].startswith("bugfix/GIT-1-fix-parser"))
        path = Path(tree["path"])
        for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
            _git("config", k, v, cwd=path)
        (path / "b.txt").write_text("b\n")
        st = worktree.branch_state("GIT-1")
        self.assertEqual((st["ok"], st["dirty"], st["ahead"]), (True, ["b.txt"], 0))
        _git("add", ".", cwd=path)
        _git("commit", "-m", "GIT-1 add b", cwd=path)
        st = worktree.branch_state("GIT-1")
        self.assertEqual((st["dirty"], st["ahead"]), ([], 1))
        self.assertIn("b.txt", st["diffstat"])
        self.assertEqual(worktree.sync("GIT-1")["action"], "up-to-date")
        self.assertEqual(worktree.add("GIT-1", "Bug", "Fix the parser"), tree)  # idempotent


class JenkinsAndJiraParameterisationTest(unittest.TestCase):
    def setUp(self):
        write_profile("cij", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\nurl = "http://ci.example"\n'
                      'job = "/job/pipe/job/PR-{pr}"\nuser_env = "X_CI_USER"\ntoken_env = "X_CI_TOKEN"\n'
                      '[jira]\nurl = "https://j.example/"\nboard = 7\nemail_env = "X_J_EMAIL"\n'
                      'token_env = "X_J_TOKEN"\ndescription_fields = { Bug = "customfield_1" }\n')
        self.p = project.load("cij")

    def test_jenkins_job_path_and_credential_env_names(self):
        from sprint_manager import jenkins
        self.assertEqual(jenkins._branch_job_path(self.p, 12), "/job/pipe/job/PR-12")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_CI_USER", None)
            with self.assertRaises(config.ConfigError) as ctx:
                config.jenkins_credentials(self.p)
        self.assertIn("X_CI_USER", str(ctx.exception))
        with mock.patch.dict(os.environ, {"X_CI_USER": "u", "X_CI_TOKEN": "t"}):
            self.assertEqual(config.jenkins_credentials(self.p), ("http://ci.example", "u", "t"))
        write_profile("badjob", f'repo = "{ROOT}"\n[ci]\nprovider = "jenkins"\njob = "/job/x"\n')
        with self.assertRaises(config.ConfigError):
            jenkins._branch_job_path(project.load("badjob"), 1)

    def test_jira_site_board_fields_and_browse_url(self):
        from sprint_manager import jira_client
        from sprint_manager.fetch_sprint import simplify_issue
        self.assertEqual(config.browse_url(self.p, "AB-1"), "https://j.example/browse/AB-1")
        self.assertEqual(config.browse_url(project.load("demo"), "AB-1"), "")
        self.assertEqual(config.board_id(self.p), "7")
        self.assertIn("customfield_1", jira_client.issue_fields(self.p))
        with mock.patch.dict(os.environ, {"X_J_EMAIL": "e", "X_J_TOKEN": "t"}):
            self.assertEqual(config.jira_credentials(self.p), ("https://j.example", "e", "t"))
        with self.assertRaises(config.ConfigError):
            config.jira_credentials(project.load("demo"))   # no [jira] at all
        issue = {"key": "AB-1", "fields": {"issuetype": {"name": "Bug"}, "summary": "S",
                                           "customfield_1": "bug body", "description": "plain"}}
        self.assertEqual(simplify_issue(issue, self.p)["description"], "bug body")
        issue["fields"]["issuetype"]["name"] = "Story"
        self.assertEqual(simplify_issue(issue, self.p)["description"], "plain")

    def test_settings_ui_credentials_come_from_profiles(self):
        keys = {f["key"] for f in config.credential_fields()}
        self.assertTrue({"X_CI_USER", "X_CI_TOKEN", "X_J_EMAIL", "X_J_TOKEN", "SLACK_BOT_TOKEN"} <= keys)


@support.needs_sdk
class ExampleProfilesTest(unittest.TestCase):
    def test_every_example_loads_and_renders(self):
        import shutil
        from sprint_manager.agent import build_system_prompt
        examples = sorted((APP / "examples").glob("*/project.toml"))
        self.assertGreaterEqual(len(examples), 2)
        for toml in examples:
            name = "ex-" + toml.parent.name
            dest = PROJECTS_DIR / name
            shutil.copytree(toml.parent, dest, dirs_exist_ok=True)
            p = project.load(name)
            for stage in (Stage.EXPLORE, Stage.WORK, Stage.PR_OPEN):
                self.assertNotIn("<<", build_system_prompt(stage, p), (name, stage))
            if "test_commands" in p.prompts:
                self.assertTrue(p.prompts["test_commands"].exists(), name)


class NoRecouplingGuardTest(unittest.TestCase):
    """Core code, prompts, UI and examples must not name any particular repo or company — project
    specifics belong in a project profile (outside this codebase).

    The words to forbid are YOURS (your company, your repos), so they live in a gitignored file,
    ``scripts/tests/.forbidden-words`` — one regular expression per line, ``#`` for comments,
    matched case-insensitively. Without that file this test is skipped.
    """

    WORDS_FILE = Path(__file__).with_name(".forbidden-words")
    SCOPE = ["scripts/sprint_manager", "references", "web", "sprint-manager", ".env.example", "examples"]

    def test_no_project_specific_names(self):
        if not self.WORDS_FILE.exists():
            self.skipTest(f"no {self.WORDS_FILE.name} (list your company/repo names there to enable)")
        words = [w.strip() for w in self.WORDS_FILE.read_text().splitlines()
                 if w.strip() and not w.lstrip().startswith("#")]
        if not words:
            self.skipTest(f"{self.WORDS_FILE.name} is empty")
        forbidden = re.compile("|".join(f"(?:{w})" for w in words), re.IGNORECASE)
        hits = []
        for rel in self.SCOPE:
            root = APP / rel
            files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file()]
            for f in files:
                if "__pycache__" in f.parts or f.suffix in (".pyc", ".svg"):
                    continue
                for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if forbidden.search(line):
                        hits.append(f"{f.relative_to(APP)}:{n}: {line.strip()[:100]}")
        self.assertEqual(hits, [], "project-specific names in core:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
