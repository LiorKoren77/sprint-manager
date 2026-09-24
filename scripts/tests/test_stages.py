"""Tests for the explore/work/pr-open stage model: migration aliases, the per-stage notes view,
and ship's PR-block parsing. Stdlib unittest; the orchestrator test needs the venv (SDK import).

    cd scripts && PYTHONPATH=$PWD python3 -m unittest discover -s tests -v
"""

import unittest

import support  # noqa: F401  (isolated state/config dirs — must come first)

from sprint_manager import models, notes  # noqa: E402
from sprint_manager.models import Activity, Stage, TicketStatus  # noqa: E402


class StageMigrationTest(unittest.TestCase):
    def test_old_names_resolve(self):
        for old, new in [("orient", Stage.EXPLORE), ("plan", Stage.EXPLORE),
                         ("implementation", Stage.WORK), ("testing", Stage.WORK),
                         ("ship", Stage.WORK), ("ci-build", Stage.PR_OPEN)]:
            self.assertEqual(Stage(old), new)

    def test_aliased_working_stage_drops_session_and_parks_idle(self):
        s = TicketStatus.from_dict({"ticket": "ABC-1", "stage": "plan",
                                    "activity": "waiting_user", "session_id": "abc"})
        self.assertEqual((s.stage, s.activity, s.session_id), (Stage.EXPLORE, Activity.IDLE, ""))

    def test_aliased_pr_open_keeps_activity(self):
        s = TicketStatus.from_dict({"ticket": "ABC-1", "stage": "ci-build",
                                    "activity": "waiting_external", "session_id": "abc"})
        self.assertEqual((s.stage, s.activity, s.session_id),
                         (Stage.PR_OPEN, Activity.WAITING_EXTERNAL, ""))

    def test_current_stage_keeps_session(self):
        s = TicketStatus.from_dict({"ticket": "ABC-1", "stage": "work",
                                    "activity": "waiting_user", "session_id": "abc"})
        self.assertEqual((s.stage, s.activity, s.session_id),
                         (Stage.WORK, Activity.WAITING_USER, "abc"))

    def test_old_model_overrides_ignored(self):
        models.set_model_overrides({"plan": {"model": "x"}, "work": {"model": "y", "effort": "low"}})
        try:
            self.assertEqual(models.model_for_stage(Stage.EXPLORE), models.MODEL_BY_STAGE[Stage.EXPLORE])
            self.assertEqual(models.model_for_stage(Stage.WORK), ("y", "low"))
        finally:
            models.set_model_overrides({})

    def test_flow(self):
        self.assertEqual(models.next_stage(Stage.EXPLORE), Stage.WORK)
        self.assertEqual(models.next_stage(Stage.PR_OPEN), Stage.DONE)
        self.assertFalse(models.uses_worktree(Stage.EXPLORE))
        self.assertTrue(models.uses_worktree(Stage.WORK) and models.uses_worktree(Stage.PR_OPEN))


def _notes(*sections):
    return "# ABC-1 — working notes\n\n" + "".join(f"## {t}\n\n{b}\n\n" for t, b in sections)


class NotesViewTest(unittest.TestCase):
    def test_latest_section_per_stage_name_wins(self):
        text = _notes(("orient — summary", "O"), ("plan — summary", "P1"),
                      ("plan — mid-stage compact", "P2"), ("implementation — summary", "I1"),
                      ("pr-open — summary (before jump to plan)", "J"),
                      ("plan — summary", "P3"), ("implementation — summary", "I2"))
        view = notes.filter_view(text, Stage.WORK)
        for kept in ("O\n", "J\n", "P3", "I2"):
            self.assertIn(kept, view)
        for dropped in ("P1", "P2", "I1"):
            self.assertNotIn(dropped, view)
        self.assertIn("3 superseded section(s) omitted", view)

    def test_pr_open_sees_latest_three_episodes(self):
        eps = [(f"pr-open — triage episode (2026-09-2{i} 10:00)", f"E{i}") for i in range(5)]
        text = _notes(("explore — summary", "PLAN"), ("work — summary", "WORK"), *eps)
        view = notes.filter_view(text, Stage.PR_OPEN)
        for kept in ("PLAN", "WORK", "E2", "E3", "E4"):
            self.assertIn(kept, view)
        for dropped in ("E0", "E1"):
            self.assertNotIn(dropped, view)
        # A jump back to work (re-plan / rework) still sees every episode.
        self.assertIn("E0", notes.filter_view(text, Stage.WORK))

    def test_inner_level2_heading_is_not_a_section(self):
        text = _notes(("work — summary", "## Details\nstuff"))
        self.assertEqual(notes.filter_view(text, Stage.PR_OPEN), text)

    def test_no_omission_marker_when_nothing_dropped(self):
        text = _notes(("explore — summary", "PLAN"))
        self.assertEqual(notes.filter_view(text, Stage.WORK), text)


try:
    from sprint_manager.orchestrator import _parse_pr_block
except ImportError:  # no venv (claude_agent_sdk) — skip the orchestrator-level test
    _parse_pr_block = None


@unittest.skipIf(_parse_pr_block is None, "needs the venv (claude_agent_sdk)")
class ParsePrBlockTest(unittest.TestCase):
    def test_parses_block(self):
        recap = ("### Summary\n- did X\n\n### PR\ntitle: ABC-1: Fix the thing\nbody:\n"
                 "Fixes the thing by doing X. Verified with the manager suite.")
        self.assertEqual(_parse_pr_block(recap, "ABC-1", "Jira summary"),
                         ("ABC-1: Fix the thing",
                          "Fixes the thing by doing X. Verified with the manager suite."))

    def test_fallbacks(self):
        title, body = _parse_pr_block("### Summary\n\nChanged the parser to cope.", "ABC-1", "Parser bug")
        self.assertEqual(title, "ABC-1: Parser bug")
        self.assertEqual(body, "Changed the parser to cope.")

    def test_forces_ticket_key_into_title(self):
        title, _ = _parse_pr_block("### PR\ntitle: Fix it\nbody:\nx", "ABC-1", "")
        self.assertEqual(title, "ABC-1: Fix it")


if __name__ == "__main__":
    unittest.main()
