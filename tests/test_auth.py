"""AuthN (401) vs AuthZ (403) — demonstrated separately, as designed."""
from bastionbank import queue as queue_mod
from tests.conftest import chat, login


def test_login_success(client):
    r = client.post("/auth/login", json={"username": "maya", "password": "maya-demo-123"})
    assert r.status_code == 200
    assert r.json()["role"] == "customer"


def test_login_wrong_password_is_401(client):
    r = client.post("/auth/login", json={"username": "maya", "password": "wrong"})
    assert r.status_code == 401


def test_chat_without_token_is_401(client):
    r = client.post("/chat", json={"message": "hello"})
    assert r.status_code == 401


def test_chat_with_garbage_token_is_401(client):
    r = client.post("/chat", json={"message": "hello"},
                    headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_customer_cannot_approve_is_403(client, runtime):
    maya = login(client, "maya")
    chat(client, maya, "Transfer $2,500 to Maple Properties")
    approvals = queue_mod.list_approvals(runtime.store)
    assert len(approvals) == 1
    job_id = approvals[0]["id"]
    # Authenticated (valid JWT) but not authorized -> 403, distinctly not 401.
    r = client.post(f"/api/approvals/{job_id}/approve",
                    headers={"Authorization": f"Bearer {maya}"})
    assert r.status_code == 403
    # And without any token the same call is a 401.
    r2 = client.post(f"/api/approvals/{job_id}/approve")
    assert r2.status_code == 401


def test_banker_can_approve(client, runtime):
    maya = login(client, "maya")
    banker = login(client, "banker")
    chat(client, maya, "Transfer $2,500 to Maple Properties")
    job_id = queue_mod.list_approvals(runtime.store)[0]["id"]
    r = client.post(f"/api/approvals/{job_id}/approve",
                    headers={"Authorization": f"Bearer {banker}"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_policy_table_denies_by_default():
    from bastionbank.policy import decide, DENY
    assert decide("support_agent", "initiate_transfer", amount_cents=100).action == DENY
    assert decide("accounts_agent", "initiate_transfer", amount_cents=100).action == DENY
    assert decide("unknown_agent", "list_accounts").action == DENY


def test_policy_transfer_thresholds():
    from bastionbank.policy import decide, ALLOW, REQUIRES_HITL, DENY
    assert decide("payments_agent", "initiate_transfer", amount_cents=50_000).action == ALLOW
    assert decide("payments_agent", "initiate_transfer", amount_cents=250_000).action == REQUIRES_HITL
    assert decide("payments_agent", "initiate_transfer", amount_cents=2_000_000).action == DENY


def test_gateway_denial_is_structured_403(runtime):
    """payments_agent asking for a balance: valid everything, forbidden tool."""
    from bastionbank.gateway import RunGuard
    rt = runtime
    trace_id = rt.tracer.new_trace_id()
    rt.tracer.start_trace(trace_id, "s-authz", 1)
    result = rt.gateway.call(guard=RunGuard(rt.config), trace_id=trace_id,
                             agent="payments_agent", tool="get_account_balance",
                             args={"account_id": 1})
    assert result["refused"] is True
    assert result["status"] == 403
    events = [e for e in rt.store.list_security_events() if e["kind"] == "authz_denied"]
    assert events, "denial must be logged as a security event"
