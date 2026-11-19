# Example flow 3 — $2,500 transfer: human approval, then execution

Transfers over $1,000 park in `awaiting_approval`. A banker approves in the inbox; the queue worker then executes and the balance moves.

```http
POST /chat
{"message": "Transfer $2,500 to Maple Properties"}
```

Response:

```json
{
  "reply": "Transfer of $2,500.00 to Maple Properties from your checking account is awaiting banker approval (transfer #1) because it exceeds the instant-transfer limit.",
  "agent": "payments_agent",
  "trace_id": "aaac9521afa64f34",
  "status": "ok",
  "session_id": "b6ec8ef2f9e4"
}
```

```http
GET /api/approvals  (banker token) -> job #1 awaiting approval
POST /api/approvals/1/approve  (banker token)
```

Response: `200 {"job_id":1,"status":"queued"}`

After the worker runs, the customer asks for the status and the new balance:

```json
{
  "reply": "Transfer #1: $2,500.00 to Maple Properties \u2014 status: completed.",
  "agent": "payments_agent",
  "trace_id": "053dc46802fa4b85",
  "status": "ok",
  "session_id": "b6ec8ef2f9e4"
}
```

```json
{
  "reply": "Your checking account ending in 5678 has a balance of $1,750.75.",
  "agent": "accounts_agent",
  "trace_id": "e325dfebe0034345",
  "status": "ok",
  "session_id": "b6ec8ef2f9e4"
}
```
