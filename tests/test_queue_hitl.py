"""Queue + human-in-the-loop: small transfers execute, large ones pause,
approval executes, rejection cancels, failures dead-letter."""
import time

from bastionbank import queue as queue_mod
from tests.conftest import chat, login


def _drain(runtime):
    queue_mod.process_available_jobs(runtime.store, backoff_base_s=0.05)


def _balance(runtime, account_id: int) -> int:
    return runtime.store.get_account(account_id)["balance_cents"]


def test_small_transfer_executes_end_to_end(client, runtime):
    maya = login(client, "maya")
    out = chat(client, maya, "Transfer $100 to ComEd")
    assert "queued" in out["reply"]
    _drain(runtime)
    transfer = runtime.store.query_one("SELECT * FROM transfers WHERE id=1")
    assert transfer["status"] == "completed"
    assert _balance(runtime, 1) == 425_075 - 10_000


def test_large_transfer_pauses_then_approval_executes(client, runtime):
    maya = login(client, "maya")
    banker = login(client, "banker")
    out = chat(client, maya, "Transfer $2,500 to Maple Properties")
    assert "awaiting banker approval" in out["reply"]
    _drain(runtime)  # worker must NOT execute an unapproved job
    assert runtime.store.query_one("SELECT * FROM transfers WHERE id=1")["status"] == "awaiting_approval"
    assert _balance(runtime, 1) == 425_075
    job_id = queue_mod.list_approvals(runtime.store)[0]["id"]
    r = client.post(f"/api/approvals/{job_id}/approve",
                    headers={"Authorization": f"Bearer {banker}"})
    assert r.status_code == 200
    _drain(runtime)
    assert runtime.store.query_one("SELECT * FROM transfers WHERE id=1")["status"] == "completed"
    assert _balance(runtime, 1) == 425_075 - 250_000


def test_rejection_cancels_transfer(client, runtime):
    jordan = login(client, "jordan")
    banker = login(client, "banker")
    chat(client, jordan, "Transfer $1,500 to City Water")
    job_id = queue_mod.list_approvals(runtime.store)[0]["id"]
    r = client.post(f"/api/approvals/{job_id}/reject",
                    headers={"Authorization": f"Bearer {banker}"})
    assert r.status_code == 200
    _drain(runtime)
    transfer = runtime.store.query_one("SELECT * FROM transfers WHERE id=1")
    assert transfer["status"] == "cancelled"
    assert _balance(runtime, 3) == 112_040  # unchanged


def test_failing_job_dead_letters_after_retries(client, runtime):
    """Jordan's savings hold $860; a $900 transfer can never succeed."""
    jordan = login(client, "jordan")
    chat(client, jordan, "Transfer $900 from my savings to City Water")
    now = time.time()
    for i in range(3):
        queue_mod.process_available_jobs(runtime.store, backoff_base_s=0.05, now=now + i * 10)
    dlq = queue_mod.list_dlq(runtime.store)
    assert len(dlq) == 1
    assert "InsufficientFunds" in dlq[0]["last_error"]
    transfer = runtime.store.query_one("SELECT * FROM transfers WHERE id=1")
    assert transfer["status"] == "failed"
    assert _balance(runtime, 4) == 86_000  # unchanged


def test_transfer_over_hard_limit_is_refused(client, runtime):
    maya = login(client, "maya")
    out = chat(client, maya, "Transfer $20,000 to Maple Properties")
    assert out["status"] == "refused"
    assert "403" in out["reply"]
    assert runtime.store.query_one("SELECT COUNT(*) AS n FROM transfers")["n"] == 0
