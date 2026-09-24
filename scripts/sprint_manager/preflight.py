"""Deterministically check the infrastructure a project's test suites need — so the work stage's
agent reads one JSON verdict instead of probing ports and paths by hand (a turn sink).

What to check comes from the project profile's ``[preflight]`` table (see project.py)::

    [preflight]
    ports = { api = { port = 8080, hint = "start it with ./run-api.sh" }, db = { port = 5432 } }
    paths = { "service venv" = "service/.venv" }   # relative to the agent's checkout, or absolute/~
    env   = { LICENSE_FILE = "set LICENSE_FILE to your license path" }

    python -m sprint_manager.preflight [--need api,db] [--project NAME] [--cwd DIR]

Nothing here starts services (that stays a human/agent decision); it only reports ground truth. A
project with no ``[preflight]`` table reports ``all_ok: true`` with no checks.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sprint_manager import project as project_mod  # noqa: E402


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(preflight: dict, need: list[str] | None, checkout: Path) -> dict:
    """Evaluate a ``[preflight]`` table. ``need`` limits the PORT checks (paths/env always run)."""
    checks: list[dict] = []
    ports = preflight.get("ports") or {}
    wanted = set(need) if need else set(ports)
    for name, spec in ports.items():
        if name not in wanted:
            continue
        host, port = spec.get("host", "127.0.0.1"), int(spec["port"])
        ok = _port_open(host, port)
        checks.append({"name": f"port:{name}", "ok": ok, "detail": f"{host}:{port}",
                       "hint": "" if ok else spec.get("hint", f"nothing listening on {host}:{port}")})
    for label, spec in (preflight.get("paths") or {}).items():
        path = Path(spec).expanduser()
        if not path.is_absolute():
            path = checkout / path   # relative paths are relative to the agent's checkout
        ok = path.exists()
        checks.append({"name": label, "ok": ok, "detail": str(path),
                       "hint": "" if ok else f"missing: {path}"})
    for var, hint in (preflight.get("env") or {}).items():
        ok = bool(os.environ.get(var))
        checks.append({"name": f"env:{var}", "ok": ok, "detail": os.environ.get(var, "(unset)"),
                       "hint": "" if ok else hint})
    unknown = sorted(set(need or []) - set(ports))
    result = {"all_ok": all(c["ok"] for c in checks), "checks": checks}
    if unknown:
        result["unknown"] = unknown  # asked for checks the profile doesn't define
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a project's test infrastructure.")
    parser.add_argument("--need", default=None,
                        help="Comma-separated port checks to include (default: all the profile defines)")
    parser.add_argument("--project", default=None, help="Project name (default: $SM_PROJECT)")
    parser.add_argument("--cwd", default=None, help="Checkout the path checks are relative to "
                                                    "(default: the current directory)")
    args = parser.parse_args(argv)
    need = [s.strip() for s in args.need.split(",")] if args.need else None
    try:
        proj = project_mod.resolve(args.project)
    except project_mod.ProjectError as exc:
        print(json.dumps({"all_ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(run(proj.preflight, need, Path(args.cwd or os.getcwd())), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
