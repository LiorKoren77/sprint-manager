"""HTTP API tests (FastAPI TestClient, in-process; the poll loops are NOT started because the app's
lifespan only runs inside ``with TestClient(app)``). Covers the project/task endpoints and the
stage-gate refusals, so a UI regression surfaces as a failing request here.

    cd scripts && PYTHONPATH=$PWD .venv/bin/python -m unittest discover -s tests
"""

import unittest

import support  # noqa: F401  (isolated state/config dirs — must come first)
from support import ROOT

try:
    from fastapi.testclient import TestClient

    from sprint_manager import server, state, taskfile
    from sprint_manager.models import Stage
except ImportError:  # no venv
    server = None


@unittest.skipIf(server is None, "needs the venv (fastapi, httpx)")
class ApiTest(unittest.TestCase):
    def setUp(self):
        support.clean_state()
        self.c = TestClient(server.app, headers={"X-SM-Token": support.TOKEN})

    def test_projects_list_and_register(self):
        data = self.c.get("/api/projects").json()
        self.assertEqual(data["default"], "demo")
        self.assertIn("demo", [p["name"] for p in data["projects"]])
        repo = ROOT / "api-repo"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.c.post("/api/projects", json={"repo": str(repo)}).json()["name"], "api-repo")
        bad = self.c.post("/api/projects", json={"repo": str(ROOT / "nope")})
        self.assertEqual(bad.status_code, 400)

    def test_text_task_lifecycle_through_the_api(self):
        r = self.c.post("/api/tasks", json={"project": "demo", "tracker": "text", "title": "Export broken",
                                            "kind": "bug", "body": "Nothing happens on click."})
        self.assertEqual(r.json(), {"key": "demo-t-1"})
        row = next(x for x in self.c.get("/api/status").json() if x["ticket"] == "demo-t-1")
        self.assertEqual((row["tracker"], row["project"], row["kind"], row["url"], row["stage"]),
                         ("text", "demo", "bug", "", "to-do"))
        self.assertIn("Nothing happens", self.c.get("/api/task-text/demo-t-1").json()["body"])
        self.assertEqual(self.c.put("/api/task-text/demo-t-1", json={"body": "Clarified."}).status_code, 200)
        self.assertEqual(taskfile.read("demo-t-1"), "Clarified.\n")
        self.assertEqual(self.c.put("/api/task-text/demo-t-1", json={"body": ""}).status_code, 400)

    def test_bad_task_requests_are_400(self):
        for body in ({"tracker": "text", "title": "", "body": ""},
                     {"tracker": "github", "ref": "not an issue"},
                     {"tracker": "carrier-pigeon"},
                     {"project": "no-such-project", "tracker": "text", "title": "t", "body": "b"}):
            self.assertEqual(self.c.post("/api/tasks", json=body).status_code, 400, body)

    def test_jira_endpoints_refuse_a_project_without_jira(self):
        self.assertEqual(self.c.get("/api/sprints?project=demo").status_code, 400)
        self.assertEqual(self.c.post("/api/load", json={"sprint": "S", "project": "demo"}).status_code, 400)

    def test_stage_gates(self):
        state.update("demo-t-9", project="demo", tracker="text", stage=Stage.WORK)
        self.assertEqual(self.c.post("/api/approve/demo-t-9").status_code, 400)       # work → Ship only
        self.assertEqual(self.c.post("/api/trigger-ci/demo-t-9").status_code, 400)    # not pr-open
        state.update("demo-t-9", stage=Stage.EXPLORE)
        self.assertEqual(self.c.post("/api/ship/demo-t-9").status_code, 400)          # not work

    def test_config_lists_the_three_stages(self):
        self.assertEqual(self.c.get("/api/config").json()["stages"], ["explore", "work", "pr-open"])


if __name__ == "__main__":
    unittest.main()
