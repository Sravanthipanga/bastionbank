"""LangGraph StateGraph: coordinator -> one domain agent -> finalize."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .agents import Agents, coordinator_entry, finalize, run_agent_node


class ChatState(TypedDict, total=False):
    session_id: str
    user_id: int
    customer_id: int | None
    username: str
    role: str
    message: str
    agent: str
    intent: dict
    reply: str
    status: str
    trace_id: str
    guard: Any
    redactor: Any


def build_graph(runtime) -> Any:
    agents = Agents(runtime)
    graph = StateGraph(ChatState)

    graph.add_node("coordinator", lambda state: coordinator_entry(runtime, state))
    graph.add_node("agent", lambda state: run_agent_node(runtime, agents, state))
    graph.add_node("finalize", lambda state: finalize(runtime, state))

    graph.set_entry_point("coordinator")
    graph.add_edge("coordinator", "agent")
    graph.add_edge("agent", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
