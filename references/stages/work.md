Your branch and worktree already exist (this is your cwd) and the task's tracker (if any) has
been updated to show work started. This is ONE session that takes the **approved plan** (in the notes above) from code
to locally-green tests. It has four gates — **stop at every one**, and report the gate in your note
(`--note "G1: …"`, `"G2: …"`, …) so the manager can see where you are. A "yes"/"go on" in chat
moves you to the next gate; it never means skip ahead two.

**G1 — implement.**

- Make focused, **atomic commits**, each message starting with `$SM_REF` when it is set (e.g.
  `ABC-12: …`, `#412 …`). The reference goes in the **commit message only** — **never** write it
  (or `$SM_TICKET`) into source code or comments, even if surrounding code does. Comments should explain the *why* of the logic on its own terms.
- **Compile-check before you commit:** build what you touched, the way the project builds it
  (the project notes / test commands below say how).
- When you believe the change is complete, summarise what you changed (files + rationale), ask the
  manager to review it, set `waiting_user` (note `G1: code ready for review`), and STOP.
- If the manager gives feedback, refine and re-summarise after each round. Stay at G1 until they
  say to move on to testing.

**G2 — test plan.** Propose which suites are relevant and why. <<TEST_COMMANDS>> Present the test plan as
your `### Summary`, set `waiting_user` (note `G2: test plan`), and STOP. Do not run anything yet.

**G3 — run (only on an explicit yes).** Ask whether to run the tests; run nothing unprompted.

<<IF:preflight>>
- **Check infrastructure first** with one deterministic command instead of probing by hand:

      <<PREFLIGHT_CMD>> [--need <checks>]

  It returns JSON of which services/paths/env the project needs are up or missing (the project
  notes say which checks each suite needs) — use it to decide readiness.
<<ENDIF:preflight>>
- **Run each suite through the wrapper**, which captures the full log to disk and returns a compact
  JSON verdict (counts + failure tails + `log_path`) — so you don't pull thousands of lines of suite
  output into context. Pass the exact command:

      <<TESTS_CMD>> --cmd "<the documented suite command>" --cwd $SM_WORKTREE

  Read the JSON; only open `log_path` if the failure tails aren't enough. Name the command you ran
  in your `### Summary`.
- If a run fails due to an **infrastructure** problem (build not done, a server not up, env/config<<IF:preflight>> —
  the preflight output usually pinpoints it<<ENDIF:preflight>>), you MAY try to fix it autonomously — but for **no more
  than 10 turns**. Then stop and ask the manager. Do not chase a genuine test failure as infra.
- A **genuine test failure** caused by your change: you have the implementation context right here —
  diagnose, propose the fix, and on the manager's yes fix it, commit, and re-run the failing suite.

**G4 — ready to ship.** When the local tests pass, make sure **everything is committed**
(`git -C $SM_WORKTREE status --porcelain` is empty), summarise the results, and tell the manager the
branch is ready: they press **Ship ▶**, which pushes, opens the PR, and updates the task's tracker.
If the notes contain approved **acceptance criteria**, go through each one in this summary —
✅ met (with the evidence: test, output, file) or ❌ not met (and why) — before claiming readiness.
Set `waiting_user` (note `G4: ready to ship`) and STOP. **Do not push, open a PR, or trigger CI
yourself** — shipping is the manager's action, and CI is started only from the dashboard.

**Slow builds/suites (any gate).** This system is turn-based — you cannot "continue when it
finishes" on your own. If a compile or suite is slow enough that you'd otherwise background it and
pick up later, do it for real:

    <<BACKGROUND_CMD>> start --ticket $SM_TICKET --cmd "<the command>" --cwd $SM_WORKTREE

Then report `--activity waiting_external --note "<gate>: running in background; will notify when
done."` and STOP. The system polls it every ~20s and flips you back to `waiting_user` with the
result the moment it finishes — no agent turn spent waiting.

**Branch drift.** If the branch has drifted from `<<BASE_BRANCH>>` (long-lived work, or the manager asks you
to rebase on the latest), bring it up to date deterministically rather than by hand:

    <<WORKTREE_CMD>> sync --ticket $SM_TICKET

This merges `origin/<<BASE_BRANCH>>` into your branch; if it reports conflicts, resolve them here and commit
(`sync --abort` backs out a conflicted merge).

**Re-entry.** If the notes show a PR already exists (you were sent back from pr-open), the fix goes
on the same branch; Ship ▶ afterwards simply pushes to the existing PR.

Do **not** run `/strict-review` or any review unless the manager explicitly asks.
