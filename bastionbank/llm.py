"""Hybrid LLM strategy — why PII never leaves the bank.

Two deterministic simulated models stand in for the real deployment:

* LocalLLM  ("bastion-local-7b") — runs "inside the bank". It MAY see raw PII.
* FrontierLLM ("frontier-sim-x1") — stands in for an external frontier API.
  It receives ONLY redacted (tokenized) text. Every payload sent to it is
  written to the egress log so the no-PII-egress property is auditable.
  If GROQ_API_KEY is set, FrontierLLM can call Groq instead (implemented,
  not exercised in the sandbox — no key is present).

The LLMRouter is the enforcement point: callers declare their facts; the
router redacts before any frontier call, detokenizes the reply locally, and
refuses to route PII-bearing renders to the frontier model.

Cost accounting: every render is written to the cost ledger with estimated
tokens (chars/4) and SIMULATED prices (labelled as such). The per-session
budget cap is checked before each call.
"""
from __future__ import annotations

import json
import os

from .redactor import Redactor
from .store import Store, fmt_money, utcnow

# Simulated prices, USD per 1,000 tokens (input+output blended). SIMULATED.
PRICE_TABLE = {
    "bastion-local-7b": 0.02,
    "frontier-sim-x1": 1.50,
    "groq-llama-3.3-70b": 0.90,
}


class BudgetExceeded(Exception):
    pass


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _render_template(template: str, facts: dict) -> str:
    """Deterministic 'generation': structured facts -> natural reply text."""
    f = facts
    if template == "balance":
        return (f"Your {f['account_type']} account ending in {f.get('account_last4', '????')} "
                f"has a balance of {fmt_money(f['balance_cents'])}.")
    if template == "accounts_list":
        lines = [f"- {a['type'].title()} (…{a.get('last4') or a['account_number'][-4:]}): {fmt_money(a['balance_cents'])}"
                 for a in f["accounts"]]
        return "Here are your accounts:\n" + "\n".join(lines)
    if template == "profile":
        c = f["customer"]
        return (f"Profile for {c['full_name']}: email {c['email']}, phone {c['phone']}, "
                f"SSN {c['ssn']}, address {c['address']}.")
    if template == "transactions":
        if not f["transactions"]:
            return f"No recent transactions on your {f['account_type']} account."
        lines = [f"- {t['ts'][:10]} {t['description']}: {fmt_money(t['amount_cents'])}"
                 for t in f["transactions"]]
        return f"Recent transactions on your {f['account_type']} account:\n" + "\n".join(lines)
    if template == "payees":
        lines = []
        for p in f["payees"]:
            last4 = p.get('last4') or p['account_number'][-4:]
            lines.append(f"- {p['name']} (account …{last4}): {p['note']}")
        return "Your saved payees:\n" + "\n".join(lines)
    if template == "transfer_started":
        base = (f"Transfer of {fmt_money(f['amount_cents'])} to {f['payee_name']} "
                f"from your {f['account_type']} account")
        if f.get("status") == "awaiting_approval":
            return base + (f" is awaiting banker approval (transfer #{f['transfer_id']}) "
                           f"because it exceeds the instant-transfer limit.")
        return base + f" has been queued (transfer #{f['transfer_id']}) and will execute shortly."
    if template == "transfer_status":
        return (f"Transfer #{f['transfer_id']}: {fmt_money(f['amount_cents'])} to "
                f"{f['payee_name']} — status: {f['status']}.")
    if template == "card_status":
        return (f"Your {f['network']} card ending in {f['last4']} is now {f['status']}.")
    if template == "cards_list":
        lines = [f"- {c['network']} …{c['last4']}: {c['status']}" for c in f["cards"]]
        return "Your cards:\n" + "\n".join(lines)
    if template == "support_answer":
        return f["answer"]
    if template == "generic":
        return f["text"]
    return json.dumps(facts, default=str)


class _BaseLLM:
    name = "base"

    def render(self, template: str, facts: dict) -> str:
        return _render_template(template, facts)


class LocalLLM(_BaseLLM):
    """In-bank model. May see raw PII. Nothing it receives is egress-logged
    because nothing leaves the process."""
    name = "bastion-local-7b"


class FrontierLLM(_BaseLLM):
    """External-model stand-in. Receives redacted text only; every payload is
    egress-logged by the router before this is called."""

    name = "frontier-sim-x1"

    def __init__(self):
        self.groq_api_key = os.environ.get("GROQ_API_KEY")

    def render(self, template: str, facts: dict) -> str:
        if self.groq_api_key:  # implemented, not exercised without a key
            return self._render_via_groq(template, facts)
        return _render_template(template, facts)

    def _render_via_groq(self, template: str, facts: dict) -> str:  # pragma: no cover
        import httpx
        prompt = ("You are a banking assistant. Using only these redacted facts, "
                  f"write the reply for template '{template}': {json.dumps(facts, default=str)}")
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.groq_api_key}"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class LLMRouter:
    def __init__(self, store: Store, session_budget_usd: float = 2.00):
        self.store = store
        self.local = LocalLLM()
        self.frontier = FrontierLLM()
        self.session_budget_usd = session_budget_usd

    def session_spent(self, session_id: str) -> float:
        row = self.store.query_one(
            "SELECT COALESCE(SUM(cost_usd),0) AS total FROM cost_ledger WHERE session_id=?",
            (session_id,))
        return float(row["total"]) if row else 0.0

    def _log_cost(self, trace_id, session_id, model, prompt_text, completion_text):
        in_tok, out_tok = est_tokens(prompt_text), est_tokens(completion_text)
        cost = (in_tok + out_tok) / 1000.0 * PRICE_TABLE[model]
        self.store.execute(
            "INSERT INTO cost_ledger (trace_id, session_id, model, input_tokens, output_tokens, cost_usd, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (trace_id, session_id, model, in_tok, out_tok, cost, utcnow()))
        return cost

    def render(self, *, trace_id: str, session_id: str, template: str, facts: dict,
               redactor: Redactor, pii_bearing: bool = False) -> str:
        """Route a render. PII-bearing renders ALWAYS stay on LocalLLM.
        Non-PII renders go to the frontier model with redacted facts only."""
        if self.session_spent(session_id) >= self.session_budget_usd:
            raise BudgetExceeded(
                f"session budget of ${self.session_budget_usd:.2f} (simulated) reached; "
                "run aborted by cost guardrail")
        if pii_bearing:
            prompt_repr = json.dumps(facts, default=str)
            out = self.local.render(template, facts)
            self._log_cost(trace_id, session_id, self.local.name, prompt_repr, out)
            return out
        # Frontier path: redact EVERYTHING first, log the exact egress payload.
        redacted_facts = redactor.redact_obj(facts)
        payload = json.dumps({"template": template, "facts": redacted_facts}, default=str)
        self.store.log_egress(self.frontier.name, payload, trace_id)
        out = self.frontier.render(template, redacted_facts)
        self._log_cost(trace_id, session_id, self.frontier.name, payload, out)
        # Detokenize locally — real values are restored inside the bank only.
        return redactor.detokenize(out)

    def cost_summary(self) -> dict:
        rows = self.store.query(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS in_tok, "
            "SUM(output_tokens) AS out_tok, SUM(cost_usd) AS cost FROM cost_ledger GROUP BY model")
        total = self.store.query_one("SELECT COALESCE(SUM(cost_usd),0) AS t FROM cost_ledger")
        return {"by_model": rows, "total_cost_usd": float(total["t"]) if total else 0.0,
                "prices_simulated": True}
