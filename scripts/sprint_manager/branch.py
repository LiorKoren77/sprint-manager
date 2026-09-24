"""Derive a git branch name from a ticket's key/type/summary.

The naming convention matches the shared ``create-branch.sh`` (Bug -> ``bugfix/``, everything
else -> ``feature/``) so our branches look like every other branch in the repo. On top of that we
enforce your hard rule: the **whole branch name is trimmed to <= 80 characters**, truncating the
slug while always preserving the ``<prefix>/<KEY>-`` part.
"""

from __future__ import annotations

import argparse
import re

MAX_BRANCH_LEN = 80

# Short, common words dropped from the slug so the meaningful words survive the length budget.
_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "to", "with", "and", "or", "is", "at", "by", "from",
}


def slugify(summary: str) -> str:
    """Turn a free-text summary into a lowercase hyphenated slug (no stopwords, no punctuation)."""
    words = re.split(r"[^a-zA-Z0-9]+", summary.lower())
    kept = [w for w in words if w and w not in _STOPWORDS]
    return "-".join(kept)


def branch_prefix(issue_type: str) -> str:
    """Bugs go on ``bugfix/``; Stories/Tasks/everything else on ``feature/``."""
    return "bugfix" if issue_type.lower() == "bug" else "feature"


def branch_name(ticket: str, issue_type: str, summary: str) -> str:
    """Build ``<prefix>/<TICKET>-<slug>``, trimmed so the total length is <= 80 chars."""
    prefix = branch_prefix(issue_type)
    head = f"{prefix}/{ticket}-"  # always preserved
    budget = MAX_BRANCH_LEN - len(head)
    slug = slugify(summary)[:budget].rstrip("-")
    return f"{head}{slug}" if slug else head.rstrip("-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the branch name for a ticket.")
    parser.add_argument("--ticket", required=True, help="Ticket key, e.g. ABC-1234")
    parser.add_argument("--type", required=True, dest="issue_type", help="Issue type, e.g. Bug")
    parser.add_argument("--summary", required=True, help="Jira summary text")
    args = parser.parse_args(argv)
    print(branch_name(args.ticket, args.issue_type, args.summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
