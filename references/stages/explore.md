This stage is **read-only — do NOT edit any files.** It is ONE session with two gates; stop at
each, and report the gate in your note (`--note "G1: …"` / `--note "G2: …"`) so the manager can see
where you are.

**G1 — recap and ask.** Read the task (its summary, description, and recent comments are in your
first message). If the description/comments link a Slack thread, fetch it first (see "Slack
context" below) — background discussion often carries scope/decisions the task text doesn't.
Then:

1. Post a brief **recap** of the issue in your own words — what's broken / what's wanted and the
   apparent scope — as 3–6 bullets.
2. Ask the manager, in one message, then STOP (`waiting_user`, note `G1: recap + questions`):
   - **Where in the code should you look?** (components, directories, files — or "you decide").
   - Any further G1 question the project notes below add.
3. **If your first message marks the task as not groomed** (free text, a GitHub issue, a Slack
   thread — as opposed to a groomed tracker ticket), the problem statement is probably incomplete.
   In the same message also give:
   - the **assumptions** you are making and the **open questions** that change the solution, and
   - proposed **acceptance criteria**: a numbered, testable list under a `### Acceptance criteria`
     heading. The manager corrects or approves them here; the approved list is the definition of
     done for the work stage and goes into the PR description, so keep that heading in your plan.

Do **not** read code or draft a plan before the manager answers.

**G2 — plan.** Using the manager's answers and reading **only** where they pointed you, draft a
concise implementation **plan**: the approach, the files/areas you intend to change, the risks, and
how you would verify it (which test suites). Restate the manager's G1 guidance at the top of the
plan so it is captured.

- Present the plan as your `### Summary`, set `waiting_user` (note `G2: plan`), and STOP.
- If the manager gives feedback (chat), revise the plan **in this same session** and re-present.
  Iterate until they press **Approve**. The approved plan is what the work stage will follow, so
  make it concrete.
- If the manager pastes a Slack link during review, or the notes mention relevant Slack discussion,
  fetch it (see "Slack context" below) before revising — don't ask them to paste the content by hand.
<<IF:confluence>>
- **Publishing to Confluence** (when the manager asks) — done over REST, no MCP:
  1. Write the plan as Markdown to a temp file (this stage is repo-read-only, so write OUTSIDE the
     repo, e.g. via a Bash heredoc to `/tmp/$SM_TICKET-plan.md` — do not use Edit/Write).
  2. Publish it (idempotent by title — re-publishing a revision updates the same page, and the
     page link is posted to the task's Jira issue automatically, if it has one):

         <<CONFLUENCE_CMD>> publish --title "$SM_TICKET — plan" --file /tmp/$SM_TICKET-plan.md --ticket $SM_TICKET

  3. Then set `--activity waiting_external --note "Plan published to Confluence — awaiting tech
     leader review."` and STOP. The manager presses **Approve** once the tech leader has signed off.
  4. Reviewers often leave feedback as **inline comments directly on the page** rather than in the tracker
     or chat. Before assuming the plan is unreviewed, or whenever the manager says review is in,
     check for them instead of asking the manager to relay everything by hand:

         <<CONFLUENCE_CMD>> comments --title "$SM_TICKET — plan"

     (unresolved comments only by default — add `--all` for the full history, including resolved).
     Each comment includes the exact text it's anchored to and the reviewer's own words. Fold
     anything found into the plan discussion like any other feedback: revise, re-present, wait for
     **Approve**.
<<ENDIF:confluence>>

**Re-entry (re-planning):** if the notes show work/PR activity already happened, you were sent back
here — usually because a CI failure or review comment demands a plan change (the reason is in the
notes). Skip G1 unless the manager asks for it. This repo checkout is `<<BASE_BRANCH>>`, NOT your branch:
inspect the branch's current state read-only via the worktree first, e.g.
`git -C $SM_WORKTREE log --oneline origin/<<BASE_BRANCH>>..HEAD` and
`git -C $SM_WORKTREE diff origin/<<BASE_BRANCH>>...HEAD`.
Your revised plan must state what changes relative to the code already on the branch — what to
keep, rework, or revert — not restart from scratch.
