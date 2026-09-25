"""FastAPI service: REST for status/control + WebSocket for live agent chat, serving the dashboard.

Run it with:  python -m sprint_manager.server   (or via the /sprint-manager launch command)
Then open http://127.0.0.1:8766 .
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import sys
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from sprint_manager import config, models as _models, state  # noqa: E402
from sprint_manager import project as project_mod  # noqa: E402
from sprint_manager.jira_client import JiraError  # noqa: E402
from sprint_manager.models import Stage  # noqa: E402
from sprint_manager.orchestrator import Orchestrator  # noqa: E402
from sprint_manager.sprints import list_sprints  # noqa: E402

WEB_DIR = config.APP_ROOT / "web"
CONFIG_FILE = config.RUNTIME_CONFIG_FILE


def _load_runtime_config() -> None:
    """Load persisted settings overrides (models etc.) on startup."""
    if not CONFIG_FILE.exists():
        return
    try:
        data = json.loads(CONFIG_FILE.read_text())
        _models.set_model_overrides(data.get("models", {}))  # same {stage: {model, effort}} shape
    except Exception:  # noqa: BLE001 — bad config file; just use defaults
        pass

orchestrator = Orchestrator(max_active=int(os.environ.get("SPRINT_MANAGER_MAX_ACTIVE", "1")))

# ----- local-only access control -----------------------------------------------------------------
#
# The API drives agents that have a shell and your credentials, so "listening on 127.0.0.1" is not
# enough: any web page you visit can make your browser talk to localhost (WebSockets are exempt
# from CORS, and "simple" cross-site POSTs go through). Three checks, on every HTTP request and
# WebSocket handshake:
#   1. Host must be 127.0.0.1/localhost:<port>  — defeats DNS rebinding.
#   2. Origin, when a browser sends one, must be this dashboard — required on WebSockets and on
#      state-changing requests, which is what stops a foreign page.
#   3. /api and /ws need the per-launch token (printed at startup: open the URL it prints). It also
#      keeps out other local processes/users, and a custom header can't be sent cross-site at all.
# Static files (the page itself) need no token; they carry no data.
TOKEN = os.environ.get("SPRINT_MANAGER_TOKEN") or secrets.token_urlsafe(24)
_EXTRA_HOSTS = {h.strip() for h in os.environ.get("SPRINT_MANAGER_ALLOWED_HOSTS", "").split(",") if h.strip()}
_TICKET_ROUTE = re.compile(
    r"^/(?:api/(?:ticket|start|interrupt|compact|goto-stage|approve|ship|trigger-ci|rearm-review|"
    r"task-text|file-issue)|ws)/([^/]+)$")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_hosts() -> set[str]:
    return {f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}"} | _EXTRA_HOSTS


def access_denial(kind: str, method: str, path: str, headers: dict[str, str],
                  query: str = "") -> tuple[int, str] | None:
    """``(status, reason)`` if the request must be refused, else None. Pure — unit-tested."""
    hosts = allowed_hosts()
    if headers.get("host", "") not in hosts:
        return 403, "Forbidden host (the dashboard only answers on 127.0.0.1/localhost)."
    origin = headers.get("origin")
    origins = {f"http://{h}" for h in hosts}
    if kind == "websocket" and origin not in origins:
        return 403, "Forbidden origin."
    if kind == "http" and method not in _SAFE_METHODS and origin is not None and origin not in origins:
        return 403, "Forbidden origin."
    m = _TICKET_ROUTE.match(path)
    if m and not state.valid_ticket(m.group(1)):
        return 400, f"Invalid ticket id: {m.group(1)!r}"
    if path.startswith("/api/") or path.startswith("/ws/"):
        given = headers.get("x-sm-token", "")
        if not given and kind == "websocket":  # browsers can't set headers on a WebSocket
            given = (parse_qs(query).get("token") or [""])[0]
        if not given or not hmac.compare_digest(given, TOKEN):
            return 401, "Missing or wrong access token — open the URL the server printed at startup."
    return None


class LocalOnlyMiddleware:
    """ASGI middleware applying ``access_denial`` to HTTP requests and WebSocket handshakes."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        denial = access_denial(scope["type"], scope.get("method", "GET"), scope.get("path", ""),
                               headers, scope.get("query_string", b"").decode("latin-1"))
        if denial is None:
            return await self.app(scope, receive, send)
        status, reason = denial
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4000 + status, "reason": reason})
            return
        body = json.dumps({"error": reason}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the manager loop (CI/PR polling) when the service comes up."""
    _load_runtime_config()
    orchestrator.start_polling()
    # On restart the in-memory live sets are empty, so any persisted working/queued is by definition
    # dead. Normalize it up front (the status read path also does this continuously) so the table is
    # correct even before the first UI poll.
    healed = orchestrator.reconcile_all()
    if healed:
        import logging
        logging.getLogger(__name__).warning("Normalized %d stale transient states on startup: %s", len(healed), healed)
    # Repopulate Jira metadata (status, etc.) for persisted tickets in the background, so the table
    # looks right after a restart without blocking startup if Jira/VPN is slow or down.
    asyncio.create_task(asyncio.to_thread(orchestrator.rehydrate_meta))
    yield


app = FastAPI(title="Sprint Manager", lifespan=_lifespan)
app.add_middleware(LocalOnlyMiddleware)


def _status_rows() -> list[dict]:
    """Status records merged with the live Jira metadata the UI shows (priority, url)."""
    rows = []
    for status in state.all_statuses():
        meta = orchestrator._meta.get(status.ticket, {})
        row = status.to_dict()
        # Overlay the ground-truth activity: working/queued is only real if a live turn backs it.
        # This reconciles (and lazily heals) any stale persisted transient state on every poll, so a
        # ticket can never appear stuck-working without an in-flight turn.
        row["activity"] = orchestrator.reconcile_activity(status).value
        # Prefer the live Jira status; fall back to the persisted snapshot (e.g. right after a
        # restart, before the background rehydrate has run or while Jira is unreachable).
        row["jira_status"] = meta.get("status") or status.jira_status  # "To Do", "In Progress", ...
        row["tracker_status"] = row["jira_status"]  # the tracker-neutral name the UI reads
        # Always provide a Jira link, even before the sprint's metadata is loaded.
        row["url"] = meta.get("url") or status.external_url
        if not row["url"] and status.tracker == "jira":
            try:
                row["url"] = config.browse_url(project_mod.for_ticket(status.ticket), status.ticket)
            except project_mod.ProjectError:
                pass
        if not row["project"]:
            row["project"] = project_mod.default_project_name() or ""
        rows.append(row)
    return rows


@app.get("/api/sprints")
async def api_sprints(project: str | None = None) -> JSONResponse:
    """The project's Jira board's current + upcoming sprints, for the dashboard's sprint picker."""
    try:
        proj = project_mod.resolve(project) if project else orchestrator._default_project()
        sprints = await asyncio.to_thread(list_sprints, proj)
        return JSONResponse(sprints)
    except (config.ConfigError, JiraError, project_mod.ProjectError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_status_rows())


@app.get("/api/ticket/{ticket}")
async def api_ticket(ticket: str) -> JSONResponse:
    status = state.read(ticket)
    return JSONResponse(
        {
            "status": status.to_dict() if status else None,
            "meta": orchestrator._meta.get(ticket, {}),
            "transcript": orchestrator.transcript(ticket),
        }
    )


@app.delete("/api/ticket/{ticket}")
async def api_remove(ticket: str) -> JSONResponse:
    """Remove a ticket from the dashboard (deletes its state/transcript/notes; keeps the worktree)."""
    result = await orchestrator.remove_ticket(ticket)
    return JSONResponse(result)


@app.post("/api/load")
async def api_load(request: Request) -> JSONResponse:
    """Load a sprint's tickets assigned to you. Body: {sprint, assignee?}."""
    body = await request.json()
    sprint = body["sprint"]
    assignee = body.get("assignee", "me")
    try:
        issues = await asyncio.to_thread(orchestrator.load_sprint, sprint, assignee,
                                         body.get("project"))
    except (config.ConfigError, JiraError, project_mod.ProjectError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"loaded": len(issues), "tickets": [i["key"] for i in issues]})


@app.post("/api/add")
async def api_add(request: Request) -> JSONResponse:
    """Add a single issue (in the sprint or not) by browse URL or key. Body: {url}."""
    body = await request.json()
    try:
        result = await asyncio.to_thread(orchestrator.add_issue, body.get("url", ""),
                                         body.get("project"))
        return JSONResponse(result)
    except (ValueError, project_mod.ProjectError) as exc:  # unparseable URL/key, bad project
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/projects")
async def api_projects() -> JSONResponse:
    """Registered projects (for the project selector / New-task dialog)."""
    rows = []
    default = project_mod.default_project_name()
    for name in project_mod.list_projects():
        try:
            p = project_mod.load(name)
        except project_mod.ProjectError as exc:
            rows.append({"name": name, "error": str(exc)})
            continue
        rows.append({"name": name, "repo": str(p.repo), "default": name == default,
                     "jira": p.has_jira, "ci": p.ci.get("provider")})
    return JSONResponse({"projects": rows, "default": default})


@app.post("/api/projects")
async def api_add_project(request: Request) -> JSONResponse:
    """Register a repo as a project. Body: {repo, name?}."""
    body = await request.json()
    try:
        p = await asyncio.to_thread(project_mod.register, body.get("repo", ""), body.get("name") or None)
    except project_mod.ProjectError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"name": p.name, "repo": str(p.repo)})


@app.post("/api/tasks")
async def api_create_task(request: Request) -> JSONResponse:
    """Create a task. Body: {project?, tracker: text|github|slack, title?, kind?, body?, ref?}."""
    from sprint_manager import sources
    b = await request.json()
    try:
        result = await asyncio.to_thread(
            orchestrator.create_task, b.get("project"), b.get("tracker", ""), b.get("title", ""),
            b.get("kind", ""), b.get("body", ""), b.get("ref", ""))
    except (ValueError, sources.SourceError, project_mod.ProjectError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


@app.post("/api/load-issues")
async def api_load_issues(request: Request) -> JSONResponse:
    """Load a project's open GitHub issues as tasks. Body: {project?, assignee?, label?, milestone?}."""
    from sprint_manager import sources
    b = await request.json()
    try:
        keys = await asyncio.to_thread(orchestrator.load_github_issues, b.get("project"),
                                       b.get("assignee", "@me"), b.get("label", ""),
                                       b.get("milestone", ""))
    except (sources.SourceError, project_mod.ProjectError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"loaded": len(keys), "tickets": keys})


@app.post("/api/file-issue/{ticket}")
async def api_file_issue(ticket: str) -> JSONResponse:
    """Promote a text / Slack task to a GitHub issue in its project's repo."""
    result = await asyncio.to_thread(orchestrator.file_as_issue, ticket)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.get("/api/task-text/{ticket}")
async def api_get_task_text(ticket: str) -> JSONResponse:
    from sprint_manager import taskfile
    return JSONResponse({"ticket": ticket, "body": taskfile.read(ticket)})


@app.put("/api/task-text/{ticket}")
async def api_put_task_text(ticket: str, request: Request) -> JSONResponse:
    """Edit a text task's problem statement. Body: {body}."""
    result = orchestrator.update_task_text(ticket, (await request.json()).get("body", ""))
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/start/{ticket}")
async def api_start(ticket: str) -> JSONResponse:
    orchestrator.start(ticket)
    return JSONResponse({"started": ticket})


@app.post("/api/interrupt/{ticket}")
async def api_interrupt(ticket: str) -> JSONResponse:
    """Interrupt the ticket's in-flight turn (the agent stops; you keep the session and can resend)."""
    orchestrator.interrupt(ticket)
    return JSONResponse({"interrupted": ticket})


@app.post("/api/compact/{ticket}")
async def api_compact(ticket: str) -> JSONResponse:
    """Compact the current stage: save the last summary to notes and restart with a fresh session."""
    orchestrator.compact(ticket)
    return JSONResponse({"compacted": ticket})


@app.post("/api/goto-stage/{ticket}")
async def api_goto_stage(ticket: str, request: Request) -> JSONResponse:
    """Jump to any working stage: dispose the current session and reset for a fresh start."""
    body = await request.json()
    stage_str = body.get("stage", "")
    try:
        stage = Stage(stage_str)
    except ValueError:
        return JSONResponse({"error": f"Unknown stage: {stage_str!r}"}, status_code=400)
    if stage in (Stage.TODO, Stage.DONE):
        return JSONResponse({"error": "Cannot jump to to-do or done."}, status_code=400)
    orchestrator.goto_stage(ticket, stage)
    return JSONResponse({"ticket": ticket, "stage": stage.value})


@app.post("/api/approve/{ticket}")
async def api_approve(ticket: str) -> JSONResponse:
    """Approve: explore → work (plan approved) or pr-open → done (PR merged). 400 in work — that
    stage ends with Ship, not Approve."""
    result = orchestrator.approve(ticket)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/ship/{ticket}")
async def api_ship(ticket: str) -> JSONResponse:
    """Ship (work only): commit check, final recap + PR text, push, open the PR → pr-open."""
    result = orchestrator.ship(ticket)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/trigger-ci/{ticket}")
async def api_trigger_ci(ticket: str) -> JSONResponse:
    """Trigger a Jenkins build for the ticket's open PR (pr-open only) — the only way CI starts."""
    result = await orchestrator.trigger_ci(ticket)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/rearm-review/{ticket}")
async def api_rearm_review(ticket: str) -> JSONResponse:
    """Re-arm the code-review poll channel (dismiss a useless review event; keep watching)."""
    result = orchestrator.rearm_review(ticket)
    status = 400 if result.get("error") else 200
    return JSONResponse(result, status_code=status)


@app.websocket("/ws/{ticket}")
async def ws_ticket(websocket: WebSocket, ticket: str) -> None:
    """Bidirectional chat with one ticket's agent: stream events out, take messages in."""
    await websocket.accept()
    queue = orchestrator.subscribe(ticket)

    async def pump_events() -> None:
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    pump = asyncio.create_task(pump_events())
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip() == "/compact":
                orchestrator.compact(ticket)
            else:
                orchestrator.chat(ticket, message)
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        orchestrator.unsubscribe(ticket, queue)


@app.get("/api/config")
async def api_get_config() -> JSONResponse:
    """Effective runtime configuration plus the option lists the settings/goto-stage UI renders —
    served from models.py so the frontend never hardcodes stage names or model ids."""
    return JSONResponse({
        "models": _models.get_model_config(),
        "stages": [s.value for s in _models.WORKING_STAGES],
        "model_options": _models.AVAILABLE_MODELS,
        "no_effort_models": sorted(_models.NO_EFFORT_MODELS),
        "context_warn_tokens": config.CONTEXT_WARN_TOKENS,
    })


@app.post("/api/config/models")
async def api_set_model_config(request: Request) -> JSONResponse:
    """Save model+effort overrides. Body: [{stage, model, effort?}, ...]. Takes effect immediately."""
    items = await request.json()
    models = {m["value"] for m in _models.AVAILABLE_MODELS}
    stages = {s.value for s in _models.WORKING_STAGES}
    efforts = {None, "", "low", "medium", "high", "xhigh", "max"}
    if not isinstance(items, list) or not all(
            isinstance(i, dict) and i.get("stage") in stages and i.get("model") in models
            and i.get("effort") in efforts for i in items):
        return JSONResponse({"error": "Expected [{stage, model, effort?}] with known stages, "
                                      "models and efforts."}, status_code=400)
    cfg = {item["stage"]: {"model": item["model"], "effort": item.get("effort") or None}
           for item in items}
    _models.set_model_overrides(cfg)
    CONFIG_FILE.write_text(json.dumps({"models": cfg}, indent=2))
    return JSONResponse({"saved": True})


@app.get("/api/config/credentials")
async def api_get_credentials() -> JSONResponse:
    """Status of every editable credential (secrets masked) + the two non-env external logins."""
    return JSONResponse({
        "fields": config.credentials_status(),
        "external": await asyncio.to_thread(config.external_auth_status),
    })


@app.post("/api/config/credentials")
async def api_set_credential(request: Request) -> JSONResponse:
    """Save one credential to .env. Body: {key, value}. Applied to this process immediately."""
    body = await request.json()
    try:
        config.set_credential(body.get("key", ""), body.get("value", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"saved": True, "fields": config.credentials_status()})


# Static dashboard (registered last so it does not shadow the API/WS routes above).
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    url = f"http://127.0.0.1:{config.WEB_PORT}/?token={TOKEN}"
    print(f"\n  Sprint Manager → open {url}\n  (the token changes each start unless "
          f"SPRINT_MANAGER_TOKEN is set)\n", flush=True)
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)


if __name__ == "__main__":
    main()
