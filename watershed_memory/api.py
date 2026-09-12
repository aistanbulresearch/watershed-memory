"""Same-origin operator API; serves only the packaged public interface."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .agentcore_client import AgentCoreTurnError
from .budget import DemoBudgetExhausted
from .public_http import BoundaryConfig, BurstLimiter, project_public
from .service import Conflict, InProgress, Service, SessionNotFound
from .strands_agent import AgentTurnError


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdvanceBody(StrictBody):
    request_id: str = Field(min_length=1, max_length=128)


class ResponseBody(AdvanceBody):
    task_id: str = Field(min_length=1, max_length=128)
    action: Literal["acknowledge", "complete_review"]
    note: str = Field(min_length=1, max_length=1000)


def create_app(service: Service | None = None, *, boundary_config: BoundaryConfig | None = None) -> FastAPI:
    ledger = service or Service(Path(".local/runtime/cases.sqlite"))
    config = boundary_config or BoundaryConfig()
    limiter = BurstLimiter(config)
    app = FastAPI(title="Watershed Memory", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts),
                       www_redirect=False)
    public = Path(__file__).parent / "static"

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        def reject(detail: str, status: int, headers: dict[str, str] | None = None):
            response = JSONResponse({"detail": detail}, status_code=status, headers=headers or {})
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            return response

        if request.method == "POST":
            host_header = request.headers.get("host", "")
            host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
            if config.allowed_origins and host_header not in config.allowed_hosts:
                return reject("Use the configured workspace host.", 403)
            origin = request.headers.get("origin")
            if config.allowed_origins and origin not in config.allowed_origins:
                return reject("Use the configured workspace origin.", 403)
            if origin and config.allowed_origins:
                parsed_origin = urlsplit(origin)
                if parsed_origin.hostname != host or origin != f"https://{parsed_origin.hostname}":
                    return reject("Use this workspace to submit the action.", 403)
            elif origin:
                if origin != f"{request.url.scheme}://{host_header}":
                    return reject("Use this workspace to submit the action.", 403)
            kind = "session" if request.url.path == "/api/sessions" else "post"
            allowed, retry = limiter.allowed(kind)
            if not allowed:
                return reject("Request allowance reached; retry shortly.", 429, {"Retry-After": str(retry)})
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return reject("Send a JSON request.", 415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SessionNotFound)
    async def missing(_request: Request, _error: SessionNotFound):
        return JSONResponse({"detail": "This replay session was not found."}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, error: Conflict):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(InProgress)
    async def pending(_request: Request, error: InProgress):
        return JSONResponse({"detail": str(error)}, status_code=503, headers={"Retry-After": "2"})

    @app.exception_handler(DemoBudgetExhausted)
    async def exhausted(_request: Request, error: DemoBudgetExhausted):
        return JSONResponse({"detail": str(error)}, status_code=429)

    @app.exception_handler(AgentTurnError)
    @app.exception_handler(AgentCoreTurnError)
    async def agent_failed(_request: Request, _error: RuntimeError):
        return JSONResponse({"detail": "The agent could not finish this observation. "
                             "Your saved case is unchanged. Retry when ready."}, status_code=503)

    @app.get("/api/health")
    def health():
        return project_public({"status": "ok", "version": __version__, "mode": ledger.planner.mode})

    @app.post("/api/sessions", status_code=201)
    def create_session(_body: StrictBody):
        return project_public(ledger.create_session())

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str):
        return project_public(ledger.snapshot(session_id))

    @app.post("/api/sessions/{session_id}/advance")
    def advance(session_id: str, body: AdvanceBody):
        return project_public(ledger.advance(session_id, body.request_id))

    @app.post("/api/sessions/{session_id}/responses")
    def respond(session_id: str, body: ResponseBody):
        return project_public(ledger.respond(session_id, body.request_id, body.task_id, body.action, body.note))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(public / "index.html")

    app.mount("/static", StaticFiles(directory=public), name="static")
    return app
