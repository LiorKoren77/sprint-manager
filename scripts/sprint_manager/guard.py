"""The agent's Bash permission policy (zero-LLM, stdlib only): which shell commands a stage may run.

This is **defense in depth, not a sandbox**. The agent runs as you, with your ``gh`` login; a
determined, prompt-injected agent can find a path this parser doesn't anticipate. What it does do
reliably is stop the obvious and the accidental: merging, force-pushing, starting/re-running CI
(all the manager's actions), pushing outside pr-open, and — in the read-only explore stage —
anything that isn't on a short list of read-only commands. The real boundaries are elsewhere:
credentials scoped per stage (``agent._agent_env``), the local API's origin/token check
(``server.py``), and your review of every stage transition.

``check(command, stage)`` returns a human-readable denial reason, or ``None`` to allow. Commands
are split into segments on shell operators (``;``, ``&&``, ``||``, ``|``, newlines, ``$( … )`` and
backticks), each segment is tokenized with ``shlex`` (so ``gh pr  merge`` and quoting tricks don't
slip through), and each segment is checked on its own.
"""

from __future__ import annotations

import re
import shlex

# ----- rules for every stage -------------------------------------------------------------

MERGE = "Blocked: merging is manual — the manager merges on GitHub. Set activity=waiting_user and report."
FORCE = "Blocked: force-pushing is never allowed."
CI = ("Blocked: CI is started / re-run only by the manager from the dashboard. Say the branch is "
      "ready for CI, set activity=waiting_user, and stop.")
PUSH = ("Blocked: pushing happens at Ship ▶ (the manager's action) — in this stage just commit. "
        "In pr-open, push small fixes with the pr push command.")
WRITE_HTTP = ("Blocked: no state-changing HTTP calls (POST/PUT/PATCH/DELETE) from the agent — "
              "use the provided sprint_manager commands.")
EXPLORE_RO = ("Blocked: explore is read-only. Allowed: reading/searching commands, read-only git, "
              "the sprint_manager report/slack/confluence/ci-status tools, and writing under /tmp.")

# Read-only commands explore may run (first token of a segment).
_EXPLORE_ALLOWED = {
    "ls", "cat", "head", "tail", "less", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "wc",
    "sort", "uniq", "cut", "tr", "awk", "sed", "jq", "yq", "diff", "cmp", "file", "stat", "du",
    "tree", "realpath", "dirname", "basename", "pwd", "echo", "printf", "true", "false", "test",
    "[", "date", "env", "which", "type", "column", "nl", "xargs", "git", "python3", "python",
    "PYTHONPATH", "cd", "mkdir", "tee", "sleep",
}
_GIT_READONLY = {"log", "show", "diff", "status", "branch", "blame", "grep", "rev-parse",
                 "ls-files", "ls-tree", "cat-file", "describe", "shortlog", "reflog", "remote",
                 "config", "rev-list", "merge-base", "name-rev", "worktree", "tag", "fetch"}
# sprint_manager modules explore may run (read-only ones, plus report_stage for its status).
_EXPLORE_MODULES = {"report_stage", "slack", "confluence", "ci", "pr", "worktree", "preflight"}
_EXPLORE_MODULE_DENY = {("ci", "trigger"), ("pr", "push"), ("pr", "open"), ("worktree", "sync"),
                        ("worktree", "add"), ("worktree", "remove")}

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*([^\s;&|]+)")


def segments(command: str) -> list[str]:
    """Split a shell command into simple-command segments, quote-aware: operators (``;`` ``&&``
    ``||`` ``|`` ``&`` newline) split only outside quotes, and the bodies of ``$( … )`` / backtick
    substitutions — which run even inside double quotes — become segments of their own."""
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(command)
    quote = ""

    def flush() -> None:
        seg = "".join(cur).strip()
        if seg:
            out.append(seg)
        cur.clear()

    while i < n:
        c = command[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            cur.append(command[i:i + 2]); i += 2; continue
        if quote == "'":
            cur.append(c); quote = "" if c == "'" else quote; i += 1; continue
        if c == "$" and command.startswith("$(", i) or c == "`":
            # command substitution: extract its body (balanced parens / matching backtick)
            if c == "`":
                j = command.find("`", i + 1)
                j = n if j < 0 else j
                out.extend(segments(command[i + 1:j])); i = j + 1; continue
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0); j += 1
            out.extend(segments(command[i + 2:j - 1])); i = j; continue
        if quote == '"':
            cur.append(c); quote = "" if c == '"' else quote; i += 1; continue
        if c in "'\"":
            quote = c; cur.append(c); i += 1; continue
        if c in ";|&\n":
            flush(); i += 2 if command[i:i + 2] in ("&&", "||") else 1; continue
        cur.append(c); i += 1
    flush()
    return out


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False)
    except ValueError:  # unbalanced quotes: fall back to whitespace split (still conservative)
        return segment.split()


def _strip_env(tokens: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments and ``env``/``command``/``sudo`` wrappers."""
    i = 0
    while i < len(tokens) and (_ENV_ASSIGN.match(tokens[i]) or tokens[i] in ("env", "command", "sudo", "nohup", "time")):
        i += 1
    return tokens[i:]


def _module(tokens: list[str]) -> tuple[str, str] | None:
    """``("ci", "trigger")`` for ``python3 -m sprint_manager.ci trigger …``, else None."""
    for i, tok in enumerate(tokens[:-1]):
        if tok == "-m" and tokens[i + 1].startswith("sprint_manager."):
            mod = tokens[i + 1].split(".", 1)[1]
            sub = next((t for t in tokens[i + 2:] if not t.startswith("-")), "")
            return mod, sub
    return None


def _git_sub(tokens: list[str]) -> tuple[str, list[str]]:
    """The git subcommand and its args, skipping global options like ``-C <dir>``."""
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return (tokens[i] if i < len(tokens) else ""), tokens[i + 1:]


def _check_always(tokens: list[str], segment: str, stage: str) -> str | None:
    if not tokens:
        return None
    cmd = tokens[0].rsplit("/", 1)[-1]
    if cmd == "git":
        sub, args = _git_sub(tokens)
        if sub == "push":
            if any(a in ("-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror",
                         "--delete", "-d") or a.startswith("--force") or a.startswith("+")
                   or (a.startswith(":") and len(a) > 1) for a in args):
                return FORCE
            if stage != "pr-open":
                return PUSH
    if cmd == "gh":
        words = [t for t in tokens[1:] if not t.startswith("-")]
        if words[:2] == ["pr", "merge"]:
            return MERGE
        if words[:2] in (["run", "rerun"], ["workflow", "run"], ["run", "cancel"]):
            return CI
        if words[:1] == ["api"]:
            joined = " ".join(tokens)
            if re.search(r"/merge\b", joined):
                return MERGE
            if re.search(r"/(rerun|rerun-failed-jobs|dispatches|cancel)\b", joined):
                return CI
    if cmd in ("curl", "wget", "http", "https"):
        joined = " ".join(tokens)
        if re.search(r"(-X|--request)\s*(POST|PUT|PATCH|DELETE)\b|(^|\s)(-d|--data\S*|-F|--form|--post-data|--method)(\s|=)", joined, re.I):
            return WRITE_HTTP
    module = _module(tokens)
    if module and module[0] in ("ci", "jenkins") and module[1] == "trigger":
        return CI
    if module and module[0] == "pr" and module[1] == "push" and stage != "pr-open":
        return PUSH
    if module and module[0] == "pr" and module[1] == "open":
        return PUSH
    # python -c / heredoc code that reaches the trigger/merge functions directly
    if cmd.startswith("python") and "-m" not in tokens and re.search(r"\btrigger\s*\(|\bmerge\s*\(|run_rerun", segment):
        return CI
    return None


def _check_explore(tokens: list[str], segment: str) -> str | None:
    if not tokens:
        return None
    cmd = tokens[0].rsplit("/", 1)[-1]
    if cmd not in _EXPLORE_ALLOWED and not cmd.startswith("python"):
        return EXPLORE_RO
    if cmd == "git":
        sub, args = _git_sub(tokens)
        if sub not in _GIT_READONLY:
            return EXPLORE_RO
        if sub in ("branch", "tag", "remote", "config", "worktree") and any(
                a in ("-d", "-D", "-m", "-M", "--delete", "add", "remove", "rename", "set-url",
                      "--unset", "--add", "prune", "move") for a in args):
            return EXPLORE_RO
    if cmd in ("sed",) and any(t.startswith("-i") or t == "--in-place" for t in tokens):
        return EXPLORE_RO
    if cmd.startswith("python"):
        module = _module(tokens)
        if not module or module[0] not in _EXPLORE_MODULES or module in _EXPLORE_MODULE_DENY:
            return EXPLORE_RO
    if cmd == "xargs" and len(tokens) > 1 and tokens[1].rsplit("/", 1)[-1] not in _EXPLORE_ALLOWED:
        return EXPLORE_RO
    if cmd == "tee" and any(not t.startswith("/tmp/") for t in tokens[1:] if not t.startswith("-")):
        return EXPLORE_RO
    if cmd == "mkdir" and any(not t.startswith("/tmp/") for t in tokens[1:] if not t.startswith("-")):
        return EXPLORE_RO
    for target in _REDIRECT.findall(segment):
        if not (target.startswith("/tmp/") or target in ("/dev/null", "/dev/stderr", "/dev/stdout")):
            return EXPLORE_RO
    return None


def check(command: str, stage: str) -> str | None:
    """Denial reason for running ``command`` in ``stage`` ("explore" / "work" / "pr-open"), or None."""
    # Heredoc bodies are data, not commands: check only the line that starts the heredoc.
    head = re.split(r"<<-?\s*['\"]?\w+['\"]?", command, maxsplit=1)
    body_free = head[0] + (" " + head[1].split("\n", 1)[0] if len(head) > 1 else "")
    for segment in segments(body_free):
        tokens = _strip_env(_tokens(segment))
        reason = _check_always(tokens, segment, stage)
        if reason is None and stage == "explore":
            reason = _check_explore(tokens, segment)
        if reason:
            return reason
    # python code passed via a heredoc can still call trigger()/merge(): check the body too.
    if len(head) > 1 and re.search(r"\btrigger\s*\(|\.merge\s*\(|run_rerun", head[1]):
        return CI
    return None
