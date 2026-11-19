"""FastAPI app + ops console.

Run:  uvicorn bastionbank.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import jwt as pyjwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import queue as queue_mod
from .auth import create_token, decode_token, verify_password
from .config import load_config
from .runtime import PROJECT_ROOT, Runtime
from .store import fmt_money

import os

templates = Jinja2Templates(directory=os.path.join(PROJECT_ROOT, "bastionbank", "templates"))
templates.env.filters["money"] = fmt_money


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or Runtime(load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.ensure_started()

        async def worker_loop():
            while True:
                try:
                    await asyncio.to_thread(
                        queue_mod.process_available_jobs, runtime.store,
                        backoff_base_s=runtime.config.job_backoff_base_s)
                except Exception:
                    pass
                await asyncio.sleep(runtime.config.worker_poll_s)

        task = asyncio.create_task(worker_loop())
        app.state.worker_task = task
        yield
        task.cancel()
        runtime.mcp.stop()

    app = FastAPI(title="BastionBank — Multi-Agent Banking Platform", lifespan=lifespan)
    app.state.runtime = runtime
    app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "bastionbank", "static")),
              name="static")

    # -- auth ---------------------------------------------------------------
    def current_user(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            return decode_token(runtime.config.jwt_secret, header[7:].strip())
        except pyjwt.PyJWTError:
            raise HTTPException(401, "invalid or expired token")

    def banker_user(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "banker":
            raise HTTPException(403, "banker role required for approvals")
        return user

    @app.post("/auth/login")
    def login(body: LoginRequest):
        user = runtime.store.get_user(body.username)
        if not user or not verify_password(body.password, user["password_hash"], user["salt"]):
            raise HTTPException(401, "invalid username or password")
        token = create_token(runtime.config.jwt_secret, user, runtime.config.jwt_exp_minutes)
        return {"access_token": token, "username": user["username"], "role": user["role"]}

    # -- chat -----------------------------------------------------------------
    @app.post("/chat")
    def chat(body: ChatRequest, user: dict = Depends(current_user)):
        session_id = body.session_id or uuid.uuid4().hex[:12]
        return runtime.run_chat(user=user, session_id=session_id, message=body.message)

    # -- ops APIs ---------------------------------------------------------------
    @app.get("/api/traces")
    def api_traces(user: dict = Depends(current_user)):
        return {"traces": runtime.tracer.list_traces()}

    @app.get("/api/approvals")
    def api_approvals(user: dict = Depends(current_user)):
        return {"approvals": queue_mod.list_approvals(runtime.store)}

    @app.post("/api/approvals/{job_id}/approve")
    def api_approve(job_id: int, user: dict = Depends(banker_user)):
        if not queue_mod.approve_job(runtime.store, job_id):
            raise HTTPException(404, "no awaiting-approval job with that id")
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/approvals/{job_id}/reject")
    def api_reject(job_id: int, user: dict = Depends(banker_user)):
        if not queue_mod.reject_job(runtime.store, job_id):
            raise HTTPException(404, "no awaiting-approval job with that id")
        return {"job_id": job_id, "status": "cancelled"}

    @app.get("/api/costs")
    def api_costs(user: dict = Depends(current_user)):
        return runtime.llm.cost_summary()

    # -- health -------------------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            runtime.store.query_one("SELECT 1 AS ok")
            runtime.ensure_started()
            tools = runtime.mcp.list_tools("accounts")
            return {"ready": True, "mcp_accounts_tools": tools}
        except Exception as exc:
            return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)

    # -- pages ----------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request):
        return templates.TemplateResponse(
            request, "approvals.html",
            {"approvals": queue_mod.list_approvals(runtime.store),
             "dlq": queue_mod.list_dlq(runtime.store)})

    @app.get("/traces", response_class=HTMLResponse)
    def traces_page(request: Request):
        return templates.TemplateResponse(
            request, "traces.html", {"traces": runtime.tracer.list_traces()})

    @app.get("/traces/{trace_id}", response_class=HTMLResponse)
    def trace_detail(request: Request, trace_id: str):
        trace = runtime.tracer.get_trace(trace_id)
        if not trace:
            raise HTTPException(404, "trace not found")
        for span in trace["spans"]:
            try:
                span["attributes_dict"] = json.loads(span["attributes"])
            except Exception:
                span["attributes_dict"] = {}
        return templates.TemplateResponse(request, "trace_detail.html", {"trace": trace})

    @app.get("/costs", response_class=HTMLResponse)
    def costs_page(request: Request):
        return templates.TemplateResponse(
            request, "costs.html", {"summary": runtime.llm.cost_summary()})

    return app


app = create_app()
