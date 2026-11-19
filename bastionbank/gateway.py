"""The tool gateway: every agent data access passes through here.

Order of enforcement for each call:
  1. Run guard (max steps + identical-call loop detection)
  2. Policy gate (AuthZ) — denials are logged and returned as structured
     refusals with 403 semantics; the tool is never invoked.
  3. MCP call (for MCP-backed tools) or in-process knowledge search.
  4. Injection scan of the output — flagged fields are quarantined and a
     security event is recorded.
  5. Trace span recorded.

====================================================================
INSTRUCTION HIERARCHY: tool outputs are DATA, never instructions.
Agents receive tool output wrapped in <untrusted_data> delimiters and
must treat its content as inert data to report or reason about — never
as commands to follow. The scanner + this plain-code policy gate are
the enforcement; the delimiters are defence in depth for real LLMs.
====================================================================
"""
from __future__ import annotations

import hashlib
import json

from . import policy as policy_mod
from . import scanner as scanner_mod
from .config import Config
from .mcp_client import MCPClient
from .store import Store
from .tracing import Tracer

TOOL_SERVER = {
    "get_customer_profile": "accounts",
    "list_accounts": "accounts",
    "get_account_balance": "accounts",
    "get_transactions": "accounts",
    "set_card_status": "accounts",
    "list_payees": "payments",
    "initiate_transfer": "payments",
    "get_transfer_status": "payments",
    # search_knowledge is served in-process (public content, not customer data)
}


class RunAborted(Exception):
    """Raised by the run guard: budget/steps/loop protection."""


class RunGuard:
    """Per-chat-run runaway protection."""

    def __init__(self, config: Config):
        self.config = config
        self.steps = 0
        self._call_counts: dict[str, int] = {}

    def step(self) -> None:
        self.steps += 1
        if self.steps > self.config.max_steps:
            raise RunAborted(
                f"max_steps ({self.config.max_steps}) exceeded — run aborted by runaway protection")

    def check_tool_call(self, agent: str, tool: str, args: dict) -> None:
        self.step()
        sig = hashlib.sha256(
            json.dumps([agent, tool, args], sort_keys=True, default=str).encode()).hexdigest()
        self._call_counts[sig] = self._call_counts.get(sig, 0) + 1
        if self._call_counts[sig] >= self.config.loop_repeat_limit:
            raise RunAborted(
                f"loop detected: identical call {agent}/{tool} repeated "
                f"{self._call_counts[sig]} times — run aborted by runaway protection")


class ToolGateway:
    def __init__(self, store: Store, mcp_client: MCPClient, tracer: Tracer, config: Config):
        self.store = store
        self.mcp = mcp_client
        self.tracer = tracer
        self.config = config

    def call(self, *, guard: RunGuard, trace_id: str, agent: str, tool: str,
             args: dict) -> dict:
        guard.check_tool_call(agent, tool, args)
        amount = args.get("amount_cents") if tool == "initiate_transfer" else None
        decision = policy_mod.decide(
            agent, tool, amount_cents=amount,
            hitl_threshold_cents=self.config.hitl_threshold_cents,
            hard_limit_cents=self.config.hard_limit_cents)
        if not decision.allowed:
            self.store.log_security_event(
                "authz_denied", f"{agent} -> {tool}: {decision.reason}", trace_id)
            with self.tracer.span(trace_id, f"policy:{tool}", "policy",
                                  {"agent": agent, "decision": "deny"}):
                pass
            return {"refused": True, "status": 403, "tool": tool, "reason": decision.reason}
        if decision.action == policy_mod.REQUIRES_HITL and tool == "initiate_transfer":
            args = {**args, "requires_approval": True}
        with self.tracer.span(trace_id, f"tool:{tool}", "tool_call",
                              {"agent": agent, "server": TOOL_SERVER.get(tool, "local")}):
            if tool == "search_knowledge":
                raw = {"articles": self.store.search_articles(args.get("query", ""))}
            else:
                raw = self.mcp.call_tool(TOOL_SERVER[tool], tool, args)
        # Scan everything that came back before any agent/LLM sees it.
        cleaned, events = scanner_mod.scan_and_quarantine_obj(raw)
        for path, matches in events:
            self.store.log_security_event(
                "prompt_injection_quarantined",
                f"tool={tool} field={path} patterns={','.join(matches)}", trace_id)
        return cleaned

    @staticmethod
    def wrap_untrusted(data) -> str:
        """Delimit tool output as data-not-instructions for agent context."""
        return ("<untrusted_data>\n"
                + json.dumps(data, default=str)
                + "\n</untrusted_data>")
