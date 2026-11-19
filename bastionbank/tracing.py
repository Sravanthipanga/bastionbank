"""Observability: per-request traces with spans, persisted to the store.

One trace_id follows a chat request through coordinator -> agent -> each MCP
tool call -> each LLM call, so the /traces UI can show exactly what happened.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager

from .store import Store, utcnow


class Tracer:
    def __init__(self, store: Store):
        self.store = store

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def start_trace(self, trace_id: str, session_id: str | None, user_id: int | None) -> None:
        self.store.execute(
            "INSERT OR REPLACE INTO traces (trace_id, session_id, user_id, started_at, status) "
            "VALUES (?,?,?,?, 'running')",
            (trace_id, session_id, user_id, utcnow()),
        )

    def finish_trace(self, trace_id: str, agent: str | None, status: str, summary: str = "") -> None:
        self.store.execute(
            "UPDATE traces SET finished_at=?, agent=?, status=?, summary=? WHERE trace_id=?",
            (utcnow(), agent, status, summary[:300], trace_id),
        )

    def add_span(self, trace_id: str, name: str, kind: str, duration_ms: float,
                 attributes: dict | None = None) -> None:
        import json
        self.store.execute(
            "INSERT INTO spans (trace_id, name, kind, started_at, duration_ms, attributes) "
            "VALUES (?,?,?,?,?,?)",
            (trace_id, name, kind, utcnow(), round(duration_ms, 3), json.dumps(attributes or {})),
        )

    @contextmanager
    def span(self, trace_id: str, name: str, kind: str, attributes: dict | None = None):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add_span(trace_id, name, kind, (time.perf_counter() - start) * 1000, attributes)

    def list_traces(self, limit: int = 50) -> list[dict]:
        return self.store.query(
            "SELECT * FROM traces ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))

    def get_trace(self, trace_id: str) -> dict | None:
        trace = self.store.query_one("SELECT * FROM traces WHERE trace_id=?", (trace_id,))
        if not trace:
            return None
        trace["spans"] = self.store.query(
            "SELECT * FROM spans WHERE trace_id=? ORDER BY id", (trace_id,))
        return trace
