# Example flow 1 — balance question routed to accounts_agent

Captured from a live `uvicorn bastionbank.app:app` run (simulated data, real MCP tool calls over stdio).

```http
POST /auth/login {"username": "maya", "password": "maya-demo-123"}
POST /chat
{"message": "What is my checking account balance?"}
```

Response:

```json
{
  "reply": "Your checking account ending in 5678 has a balance of $4,250.75.",
  "agent": "accounts_agent",
  "trace_id": "64e0b6dff740400c",
  "status": "ok",
  "session_id": "180d3864c87d"
}
```
