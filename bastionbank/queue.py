"""Durable SQLite job queue + human-in-the-loop approvals.

Writes (transfers) are never executed inline by an agent. They are enqueued
as jobs; a worker executes them asynchronously. Transfers over the HITL
threshold park in `awaiting_approval` until a banker approves or rejects.
Failures retry with exponential backoff (max 3 attempts) and then land in
the dead-letter state, visible in the ops console.
"""
from __future__ import annotations

import json
import time

from .store import Store, utcnow


class InsufficientFunds(Exception):
    pass


def enqueue_job(store: Store, kind: str, payload: dict, *, requires_approval: bool,
                max_attempts: int = 3) -> int:
    status = "awaiting_approval" if requires_approval else "queued"
    return store.execute(
        "INSERT INTO jobs (kind, payload, status, attempts, max_attempts, next_run_at, created_at, updated_at) "
        "VALUES (?,?,?,0,?,?,?,?)",
        (kind, json.dumps(payload), status, max_attempts, utcnow(), utcnow(), utcnow()),
    )


def approve_job(store: Store, job_id: int) -> bool:
    job = store.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] != "awaiting_approval":
        return False
    store.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=?", (utcnow(), job_id))
    payload = json.loads(job["payload"])
    if "transfer_id" in payload:
        store.execute("UPDATE transfers SET status='queued', updated_at=? WHERE id=?",
                      (utcnow(), payload["transfer_id"]))
    return True


def reject_job(store: Store, job_id: int) -> bool:
    job = store.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] != "awaiting_approval":
        return False
    store.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (utcnow(), job_id))
    payload = json.loads(job["payload"])
    if "transfer_id" in payload:
        store.execute("UPDATE transfers SET status='cancelled', updated_at=? WHERE id=?",
                      (utcnow(), payload["transfer_id"]))
    return True


def list_approvals(store: Store) -> list[dict]:
    rows = store.query("SELECT * FROM jobs WHERE status='awaiting_approval' ORDER BY id")
    out = []
    for job in rows:
        payload = json.loads(job["payload"])
        transfer = None
        if "transfer_id" in payload:
            transfer = store.query_one("SELECT * FROM transfers WHERE id=?", (payload["transfer_id"],))
            if transfer:
                payee = store.query_one("SELECT name FROM payees WHERE id=?", (transfer["payee_id"],))
                transfer = {**transfer, "payee_name": payee["name"] if payee else "?"}
        out.append({**job, "payload": payload, "transfer": transfer})
    return out


def list_dlq(store: Store) -> list[dict]:
    return store.query("SELECT * FROM jobs WHERE status='dlq' ORDER BY id")


def execute_transfer(store: Store, payload: dict) -> None:
    """Move the money. Raises InsufficientFunds (retryable -> DLQ) on failure."""
    transfer = store.query_one("SELECT * FROM transfers WHERE id=?", (payload["transfer_id"],))
    if not transfer:
        raise ValueError(f"transfer {payload['transfer_id']} not found")
    if transfer["status"] in ("completed", "cancelled"):
        return
    account = store.query_one("SELECT * FROM accounts WHERE id=?", (transfer["from_account_id"],))
    amount = transfer["amount_cents"]
    if account["balance_cents"] < amount:
        raise InsufficientFunds(
            f"account {account['id']} balance {account['balance_cents']} < transfer {amount}")
    new_balance = account["balance_cents"] - amount
    payee = store.query_one("SELECT * FROM payees WHERE id=?", (transfer["payee_id"],))
    store.execute("UPDATE accounts SET balance_cents=? WHERE id=?", (new_balance, account["id"]))
    store.execute(
        "INSERT INTO transactions (account_id, ts, description, amount_cents, balance_after_cents) "
        "VALUES (?,?,?,?,?)",
        (account["id"], utcnow(), f"Transfer to {payee['name']}", -amount, new_balance))
    store.execute("UPDATE transfers SET status='completed', updated_at=? WHERE id=?",
                  (utcnow(), transfer["id"]))


EXECUTORS = {"execute_transfer": execute_transfer}


def process_available_jobs(store: Store, *, backoff_base_s: float = 2.0, now: float | None = None) -> int:
    """Claim and run every due job once. Returns the number of jobs attempted."""
    now = time.time() if now is None else now
    attempted = 0
    while True:
        due = [j for j in store.query(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id")
            if _parse_ts(j["next_run_at"]) <= now]
        if not due:
            return attempted
        job = due[0]
        store.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (utcnow(), job["id"]))
        attempted += 1
        try:
            EXECUTORS[job["kind"]](store, json.loads(job["payload"]))
            store.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (utcnow(), job["id"]))
        except Exception as exc:  # retry with backoff, then DLQ
            attempts = job["attempts"] + 1
            if attempts >= job["max_attempts"]:
                store.execute(
                    "UPDATE jobs SET status='dlq', attempts=?, last_error=?, updated_at=? WHERE id=?",
                    (attempts, f"{type(exc).__name__}: {exc}", utcnow(), job["id"]))
                payload = json.loads(job["payload"])
                if "transfer_id" in payload:
                    store.execute("UPDATE transfers SET status='failed', updated_at=? WHERE id=?",
                                  (utcnow(), payload["transfer_id"]))
            else:
                delay = backoff_base_s * (2 ** (attempts - 1))
                next_run = _fmt_ts(now + delay)
                store.execute(
                    "UPDATE jobs SET status='queued', attempts=?, last_error=?, next_run_at=?, updated_at=? "
                    "WHERE id=?",
                    (attempts, f"{type(exc).__name__}: {exc}", next_run, utcnow(), job["id"]))


def _parse_ts(ts: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _fmt_ts(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")
