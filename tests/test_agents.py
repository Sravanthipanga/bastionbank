"""Routing, session memory, cards, tracing, costs, and the ops pages."""
import pytest

from bastionbank.agents import classify
from bastionbank.gateway import RunAborted, RunGuard
from tests.conftest import chat, login


@pytest.mark.parametrize("message,agent", [
    ("What is my checking balance?", "accounts_agent"),
    ("Show my recent transactions", "accounts_agent"),
    ("Show my profile", "accounts_agent"),
    ("List my accounts", "accounts_agent"),
    ("Transfer $50 to ComEd", "payments_agent"),
    ("Show my payees", "payments_agent"),
    ("What is the status of transfer 2?", "payments_agent"),
    ("Freeze my card", "cards_agent"),
    ("I lost my card", "cards_agent"),
    ("What are your fees?", "support_agent"),
    ("What are the branch hours?", "support_agent"),
])
def test_routing(message, agent):
    assert classify(message)[0] == agent


def test_balance_flow(client):
    maya = login(client, "maya")
    out = chat(client, maya, "What is my checking account balance?")
    assert out["agent"] == "accounts_agent"
    assert "$4,250.75" in out["reply"]
    out2 = chat(client, maya, "And what about my savings balance?", out["session_id"])
    assert "$12,480.00" in out2["reply"]


def test_session_memory_pending_payee(client, runtime):
    """Two-turn flow: payee first, amount second — the blackboard carries it."""
    from bastionbank import queue as queue_mod
    maya = login(client, "maya")
    out1 = chat(client, maya, "I want to transfer money to Maple Properties")
    assert "How much" in out1["reply"]
    out2 = chat(client, maya, "250", out1["session_id"])
    assert "queued" in out2["reply"]
    queue_mod.process_available_jobs(runtime.store, backoff_base_s=0.05)
    transfer = runtime.store.query_one("SELECT * FROM transfers WHERE id=1")
    assert transfer["status"] == "completed"
    assert transfer["amount_cents"] == 25_000
    assert transfer["payee_id"] == 1


def test_card_freeze_unfreeze(client, runtime):
    maya = login(client, "maya")
    out = chat(client, maya, "Freeze my card")
    assert out["agent"] == "cards_agent"
    assert "frozen" in out["reply"]
    assert runtime.store.query_one("SELECT status FROM cards WHERE id=1")["status"] == "frozen"
    out2 = chat(client, maya, "Unfreeze my card", out["session_id"])
    assert "active" in out2["reply"]
    assert runtime.store.query_one("SELECT status FROM cards WHERE id=1")["status"] == "active"


def test_trace_is_recorded_with_spans(client, runtime):
    maya = login(client, "maya")
    out = chat(client, maya, "What is my checking balance?")
    trace = runtime.tracer.get_trace(out["trace_id"])
    assert trace is not None
    kinds = {s["kind"] for s in trace["spans"]}
    assert "tool_call" in kinds and "llm" in kinds
    names = {s["name"] for s in trace["spans"]}
    assert "tool:get_account_balance" in names


def test_cost_ledger_and_api(client):
    maya = login(client, "maya")
    chat(client, maya, "What is my checking balance?")
    r = client.get("/api/costs", headers={"Authorization": f"Bearer {maya}"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_cost_usd"] > 0
    assert body["prices_simulated"] is True


def test_budget_cap_aborts_run(client, runtime):
    maya = login(client, "maya")
    runtime.llm.session_budget_usd = 0.0
    try:
        out = chat(client, maya, "What is my checking balance?")
    finally:
        runtime.llm.session_budget_usd = runtime.config.session_budget_usd
    assert out["status"] == "budget_exceeded"
    assert "budget" in out["reply"].lower()


def test_loop_detector_breaks_identical_calls(runtime):
    guard = RunGuard(runtime.config)
    args = {"account_id": 1}
    for _ in range(runtime.config.loop_repeat_limit - 1):
        guard.check_tool_call("accounts_agent", "get_account_balance", args)
    with pytest.raises(RunAborted):
        guard.check_tool_call("accounts_agent", "get_account_balance", args)


def test_max_steps_guard(runtime):
    guard = RunGuard(runtime.config)
    for _ in range(runtime.config.max_steps):
        guard.step()
    with pytest.raises(RunAborted):
        guard.step()


def test_ops_pages_render(client):
    for path in ("/", "/approvals", "/traces", "/costs", "/health", "/ready"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Bastion" in client.get("/").text
