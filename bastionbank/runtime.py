"""Runtime: wires config, store, MCP client, gateway, LLM router, graph."""
from __future__ import annotations

import os

from .config import Config
from .gateway import ToolGateway
from .graph import build_graph
from .llm import LLMRouter
from .mcp_client import MCPClient
from .seed import seed_if_empty
from .store import Store, open_store
from .tracing import Tracer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.store: Store = open_store(config)
        seed_if_empty(self.store)
        self.tracer = Tracer(self.store)
        self.mcp = MCPClient(config.db_path, PROJECT_ROOT)
        self.gateway = ToolGateway(self.store, self.mcp, self.tracer, config)
        self.llm = LLMRouter(self.store, session_budget_usd=config.session_budget_usd)
        self._graph = None
        self._started = False

    def ensure_started(self) -> None:
        if not self._started:
            self.mcp.start()
            self._graph = build_graph(self)
            self._started = True

    def run_chat(self, *, user: dict, session_id: str, message: str) -> dict:
        self.ensure_started()
        state = {
            "session_id": session_id,
            "user_id": user["user_id"],
            "customer_id": user.get("customer_id"),
            "username": user["sub"],
            "role": user["role"],
            "message": message,
        }
        result = self._graph.invoke(state)
        return {
            "reply": result["reply"],
            "agent": result["agent"],
            "trace_id": result["trace_id"],
            "status": result["status"],
            "session_id": session_id,
        }
