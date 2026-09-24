"""Shared test setup — import this FIRST in every test module (before any sprint_manager import).

Points the state store and the profile/config dir at a throwaway directory, so tests never touch
the real ``state/`` or ``~/.config/sprint-manager``, and registers a minimal default project
``demo`` (repo = an empty temp dir) so code that resolves "the ticket's project" works.
"""

import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="sm-tests-"))
os.environ["SPRINT_MANAGER_STATE_DIR"] = str(ROOT / "state")
os.environ["SPRINT_MANAGER_CONFIG_DIR"] = str(ROOT / "config")
for var in ("SM_PROJECT", "SPRINT_MANAGER_DEFAULT_PROJECT"):
    os.environ.pop(var, None)

PROJECTS = ROOT / "config" / "projects"
DEMO_REPO = ROOT / "demo-repo"
DEMO_REPO.mkdir(parents=True, exist_ok=True)


def write_profile(name: str, toml: str, files: dict[str, str] | None = None,
                  where: Path | None = None) -> Path:
    """Write ``<where or PROJECTS/name>/project.toml`` (+ side files); return the profile dir."""
    d = where or (PROJECTS / name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.toml").write_text(toml)
    for rel, text in (files or {}).items():
        (d / rel).write_text(text)
    return d


write_profile("demo", f'repo = "{DEMO_REPO}"\nbase_branch = "main"\n')
(ROOT / "config" / "config.toml").write_text('default_project = "demo"\n')

import importlib.util  # noqa: E402
import unittest  # noqa: E402

# The orchestration layer (agent/orchestrator/server) needs the venv (claude_agent_sdk, fastapi);
# the zero-LLM CLI layer doesn't. Tests that touch the former skip cleanly without it.
HAS_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
needs_sdk = unittest.skipUnless(HAS_SDK, "needs the venv (claude_agent_sdk)")
