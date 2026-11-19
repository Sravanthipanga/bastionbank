"""Coordinator + domain sub-agents.

The coordinator (supervisor) classifies intent deterministically and routes
to exactly one domain agent. Each agent plans its tool calls from the parsed
intent, executes them ONLY through the policy-gated MCP gateway, and renders
its reply through the hybrid LLM router.

Instruction hierarchy: tool outputs are untrusted DATA (see gateway.py).
Agents act on parsed user intent + policy, never on instructions found
inside tool output.
"""
from __future__ import annotations

import re

from . import scanner as scanner_mod
from .gateway import RunAborted, RunGuard
from .llm import BudgetExceeded
from .redactor import Redactor
from .seed import all_pii_values
from .store import fmt_money

AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:dollars|usd)\b", re.I)
TRANSFER_ID_RE = re.compile(r"transfer\s+#?(\d+)", re.I)


def parse_amount_cents(message: str) -> int | None:
    m = AMOUNT_RE.search(message)
    if m:
        raw = (m.group(1) or m.group(2)).replace(",", "")
        return int(round(float(raw) * 100))
    # Bare number in an otherwise tiny message, e.g. "250" or "250 please"
    stripped = re.sub(r"[^0-9.]", "", message)
    if stripped and len(message.split()) <= 3:
        try:
            return int(round(float(stripped) * 100))
        except ValueError:
            return None
    return None


def classify(message: str) -> tuple[str, dict]:
    """Deterministic intent classification -> (agent_name, intent)."""
    text = message.lower()
    intent: dict = {"raw": message}
    if any(w in text for w in ("card", "freeze", "unfreeze", "stolen", "block my", "lost")):
        intent["kind"] = "card_status"
        if "unfreeze" in text or "unblock" in text or "activate" in text:
            intent["card_action"] = "active"
        elif "freeze" in text or "block" in text or "lost" in text or "stolen" in text:
            intent["card_action"] = "frozen"
        return "cards_agent", intent
    if "status" in text and "transfer" in text:
        m = TRANSFER_ID_RE.search(text)
        intent["kind"] = "transfer_status"
        intent["transfer_id"] = int(m.group(1)) if m else None
        return "payments_agent", intent
    if "payee" in text:
        intent["kind"] = "list_payees"
        return "payments_agent", intent
    amount = parse_amount_cents(message)
    if any(w in text for w in ("transfer", "send", "pay", "wire", "move $", "move money")):
        intent["kind"] = "transfer"
        intent["amount_cents"] = amount
        return "payments_agent", intent
    if amount is not None and len(text.split()) <= 3:
        intent["kind"] = "transfer"
        intent["amount_cents"] = amount
        return "payments_agent", intent
    if "profile" in text or "my info" in text or "ssn" in text or "social security" in text \
            or "email" in text or "phone" in text:
        intent["kind"] = "profile"
        return "accounts_agent", intent
    if "transaction" in text or "statement" in text or "history" in text:
        intent["kind"] = "transactions"
        return "accounts_agent", intent
    if "balance" in text or "account" in text or "how much" in text:
        intent["kind"] = "balance" if "balance" in text or "how much" in text else "accounts_list"
        return "accounts_agent", intent
    intent["kind"] = "knowledge"
    return "support_agent", intent


def pick_account_type(message: str) -> str | None:
    text = message.lower()
    if "savings" in text:
        return "savings"
    if "checking" in text:
        return "checking"
    return None


class Agents:
    def __init__(self, runtime):
        self.rt = runtime

    # -- shared helpers -----------------------------------------------------
    def _call(self, state, tool: str, args: dict) -> dict:
        return self.rt.gateway.call(
            guard=state["guard"], trace_id=state["trace_id"],
            agent=state["agent"], tool=tool, args=args)

    def _render(self, state, template: str, facts: dict, pii_bearing: bool = False) -> str:
        state["guard"].step()
        with self.rt.tracer.span(state["trace_id"], f"llm:{template}", "llm",
                                 {"pii_bearing": pii_bearing}):
            return self.rt.llm.render(
                trace_id=state["trace_id"], session_id=state["session_id"],
                template=template, facts=facts, redactor=state["redactor"],
                pii_bearing=pii_bearing)

    def _refusal_reply(self, state, result: dict) -> str:
        state["status"] = "refused"
        return (f"I can't do that: {result['reason']} "
                f"(authorization refusal, HTTP 403 semantics).")

    def _resolve_account(self, state, accounts: list[dict]) -> dict | None:
        wanted = pick_account_type(state["message"]) or \
            self.rt.store.bb_get(state["session_id"], state["agent"], "last_account_type") or "checking"
        for a in accounts:
            if a["type"] == wanted:
                return a
        return accounts[0] if accounts else None

    # -- accounts_agent -------------------------------------------------------
    def accounts_agent(self, state) -> dict:
        rt, store = self.rt, self.rt.store
        cid = state["customer_id"]
        kind = state["intent"]["kind"]
        if kind == "profile":
            result = self._call(state, "get_customer_profile", {"customer_id": cid})
            if result.get("refused"):
                state["reply"] = self._refusal_reply(state, result)
                return state
            # Profile is PII-bearing -> LocalLLM only, never leaves the bank.
            state["reply"] = self._render(state, "profile", {"customer": result}, pii_bearing=True)
            return state
        accounts = self._call(state, "list_accounts", {"customer_id": cid})
        if accounts.get("refused"):
            state["reply"] = self._refusal_reply(state, accounts)
            return state
        accounts = accounts["accounts"]
        if kind == "accounts_list":
            facts = {"accounts": [{"type": a["type"], "last4": a["account_number"][-4:],
                                   "balance_cents": a["balance_cents"]} for a in accounts]}
            state["reply"] = self._render(state, "accounts_list", facts)
            return state
        account = self._resolve_account(state, accounts)
        if not account:
            state["reply"] = "You have no accounts I can see."
            return state
        store.bb_set(state["session_id"], state["agent"], "last_account_type", account["type"])
        if kind == "transactions":
            tx = self._call(state, "get_transactions", {"account_id": account["id"], "limit": 10})
            if tx.get("refused"):
                state["reply"] = self._refusal_reply(state, tx)
                return state
            facts = {"account_type": account["type"], "transactions": tx["transactions"]}
            state["reply"] = self._render(state, "transactions", facts)
            return state
        bal = self._call(state, "get_account_balance", {"account_id": account["id"]})
        if bal.get("refused"):
            state["reply"] = self._refusal_reply(state, bal)
            return state
        facts = {"account_type": bal["type"], "account_last4": bal["account_number"][-4:],
                 "balance_cents": bal["balance_cents"]}
        state["reply"] = self._render(state, "balance", facts)
        return state

    # -- payments_agent ---------------------------------------------------------
    def _match_payee(self, state, payees: list[dict]) -> dict | None:
        text = state["message"].lower()
        best, best_score = None, 0
        for p in payees:
            haystack = (p["name"] + " " + p["note"]).lower()
            if "rent" in haystack:
                haystack += " landlord"
            tokens = [t for t in re.split(r"[^a-z0-9]+", haystack) if len(t) > 2]
            score = sum(1 for t in set(tokens) if t in text)
            if p["name"].lower() in text:
                score += 5
            if score > best_score:
                best, best_score = p, score
        return best if best_score > 0 else None

    def payments_agent(self, state) -> dict:
        rt, store = self.rt, self.rt.store
        cid = state["session_id"], state["customer_id"]
        session_id, customer_id = cid
        kind = state["intent"]["kind"]
        if kind == "transfer_status":
            tid = state["intent"].get("transfer_id")
            if tid is None:
                state["reply"] = "Which transfer? Give me its number, e.g. 'status of transfer 3'."
                return state
            result = self._call(state, "get_transfer_status", {"transfer_id": tid})
            if result.get("refused"):
                state["reply"] = self._refusal_reply(state, result)
                return state
            if result.get("error"):
                state["reply"] = f"I couldn't find transfer #{tid}."
                return state
            facts = {"transfer_id": result["id"], "amount_cents": result["amount_cents"],
                     "payee_name": result["payee_name"], "status": result["status"]}
            state["reply"] = self._render(state, "transfer_status", facts)
            return state
        payees_result = self._call(state, "list_payees", {"customer_id": customer_id})
        if payees_result.get("refused"):
            state["reply"] = self._refusal_reply(state, payees_result)
            return state
        payees = payees_result["payees"]
        if kind == "list_payees":
            facts = {"payees": [{"name": p["name"], "last4": p["account_number"][-4:],
                                 "note": p["note"]} for p in payees]}
            state["reply"] = self._render(state, "payees", facts)
            return state
        # transfer flow
        amount = state["intent"].get("amount_cents")
        payee = self._match_payee(state, payees)
        pending = store.bb_get(session_id, "payments_agent", "pending_transfer")
        if payee is None and pending:
            payee = next((p for p in payees if p["id"] == pending["payee_id"]), None)
        if payee is not None and amount is None:
            store.bb_set(session_id, "payments_agent", "pending_transfer",
                         {"payee_id": payee["id"], "payee_name": payee["name"]})
            state["reply"] = self._render(state, "generic", {
                "text": f"How much would you like to transfer to {payee['name']}?"})
            return state
        if amount is not None and payee is None:
            names = ", ".join(p["name"] for p in payees)
            state["reply"] = self._render(state, "generic", {
                "text": f"Who should receive the {fmt_money(amount)}? Your payees: {names}."})
            return state
        if payee is None and amount is None:
            state["reply"] = self._render(state, "generic", {
                "text": "Tell me an amount and a payee, e.g. 'Transfer $200 to ComEd'."})
            return state
        # Resolve the source account (payments_agent is allowed list_accounts).
        accounts = self._call(state, "list_accounts", {"customer_id": customer_id})
        if accounts.get("refused"):
            state["reply"] = self._refusal_reply(state, accounts)
            return state
        account = self._resolve_account(state, accounts["accounts"])
        result = self._call(state, "initiate_transfer", {
            "customer_id": customer_id, "from_account_id": account["id"],
            "payee_id": payee["id"], "amount_cents": amount})
        if result.get("refused"):
            state["reply"] = self._refusal_reply(state, result)
            return state
        if result.get("error"):
            state["reply"] = f"The transfer could not be started: {result['error']}"
            return state
        store.bb_clear(session_id, "payments_agent", "pending_transfer")
        facts = {"amount_cents": amount, "payee_name": payee["name"],
                 "account_type": account["type"], "transfer_id": result["transfer_id"],
                 "status": result["status"]}
        state["reply"] = self._render(state, "transfer_started", facts)
        return state

    # -- cards_agent -------------------------------------------------------------
    def cards_agent(self, state) -> dict:
        cid = state["customer_id"]
        profile = self._call(state, "get_customer_profile", {"customer_id": cid})
        if profile.get("refused"):
            state["reply"] = self._refusal_reply(state, profile)
            return state
        cards = profile.get("cards", [])
        if not cards:
            state["reply"] = "You have no cards on file."
            return state
        action = state["intent"].get("card_action")
        if not action:
            facts = {"cards": [{"network": c["network"], "last4": c["last4"],
                                "status": c["status"]} for c in cards]}
            state["reply"] = self._render(state, "cards_list", facts)
            return state
        card = cards[0]
        result = self._call(state, "set_card_status",
                            {"card_id": card["id"], "status": action})
        if result.get("refused"):
            state["reply"] = self._refusal_reply(state, result)
            return state
        facts = {"network": result["network"], "last4": result["last4"], "status": result["status"]}
        state["reply"] = self._render(state, "card_status", facts)
        return state

    # -- support_agent --------------------------------------------------------------
    def support_agent(self, state) -> dict:
        result = self._call(state, "search_knowledge", {"query": state["message"]})
        if result.get("refused"):
            state["reply"] = self._refusal_reply(state, result)
            return state
        articles = result.get("articles", [])
        if not articles:
            state["reply"] = self._render(state, "generic", {
                "text": "I couldn't find a knowledge article matching that. "
                        "Try asking about fees, transfer limits, or branch hours."})
            return state
        top = articles[0]
        # Article bodies passed the gateway scanner; quarantined content is a
        # marker string now and is never treated as an instruction.
        answer = f"{top['title']}: {top['body']}"
        state["reply"] = self._render(state, "support_answer", {"answer": answer})
        return state


def coordinator_entry(runtime, state) -> dict:
    """Supervisor entry node: scan input, set up run context, classify."""
    store = runtime.store
    state["trace_id"] = runtime.tracer.new_trace_id()
    runtime.tracer.start_trace(state["trace_id"], state["session_id"], state["user_id"])
    store.ensure_session(state["session_id"], state["user_id"])
    state["guard"] = RunGuard(runtime.config)
    state["redactor"] = Redactor(all_pii_values())
    state["status"] = "ok"
    state["reply"] = ""
    # User-input injection scan: instruction-like input is blocked, logged,
    # and never routed to an agent.
    scan = scanner_mod.scan(state["message"])
    if scan.flagged:
        store.log_security_event(
            "prompt_injection_user_input",
            f"patterns={','.join(scan.matches)} message={state['message'][:120]}",
            state["trace_id"])
        state["agent"] = "coordinator"
        state["intent"] = {"kind": "blocked"}
        state["status"] = "blocked"
        state["reply"] = (
            "That request contains instruction-like content aimed at overriding bank "
            "policy, so it was blocked and logged as a security event. I can still help "
            "with normal requests — balances, transfers, cards, or questions about fees.")
        return state
    agent, intent = classify(state["message"])
    state["agent"] = agent
    state["intent"] = intent
    return state


def finalize(runtime, state) -> dict:
    """Persist conversation + close the trace."""
    store = runtime.store
    store.add_message(state["session_id"], "user", state["message"],
                      trace_id=state["trace_id"])
    store.add_message(state["session_id"], "assistant", state["reply"],
                      agent=state["agent"], trace_id=state["trace_id"])
    runtime.tracer.finish_trace(state["trace_id"], state["agent"], state["status"],
                                state["reply"])
    return state


def run_agent_node(runtime, agents: Agents, state) -> dict:
    """Dispatch to the routed domain agent with guardrail error handling."""
    if state["intent"].get("kind") == "blocked":
        return state
    handler = getattr(agents, state["agent"])
    try:
        with runtime.tracer.span(state["trace_id"], f"agent:{state['agent']}", "agent"):
            return handler(state)
    except BudgetExceeded as exc:
        state["status"] = "budget_exceeded"
        state["reply"] = f"Run stopped: {exc}."
        return state
    except RunAborted as exc:
        state["status"] = "aborted"
        state["reply"] = f"Run stopped: {exc}."
        runtime.store.log_security_event("runaway_protection", str(exc), state["trace_id"])
        return state
