"""Authorization (AuthZ): the policy gate.

This is plain, declarative code. It is NOT influenced by anything an LLM or a
tool output says — data content cannot alter it. Every MCP tool call an agent
makes passes through `decide()` first. A valid login (AuthN) says nothing
about authorization: a fully authenticated agent still gets a structured
refusal (403 semantics) for tools outside its mandate.
"""
from __future__ import annotations

from dataclasses import dataclass

ALLOW = "allow"
DENY = "deny"
REQUIRES_HITL = "requires_hitl"


@dataclass
class Decision:
    action: str            # allow | deny | requires_hitl
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action in (ALLOW, REQUIRES_HITL)


# Declarative table: (agent, tool) -> rule. Anything not listed is DENIED.
# Rules: ALLOW, DENY, or ("transfer",) for initiate_transfer, which is
# amount-sensitive and resolved in decide().
POLICY: dict[tuple[str, str], str] = {
    # accounts_agent — read-only on accounts domain
    ("accounts_agent", "get_customer_profile"): ALLOW,
    ("accounts_agent", "list_accounts"): ALLOW,
    ("accounts_agent", "get_account_balance"): ALLOW,
    ("accounts_agent", "get_transactions"): ALLOW,
    ("accounts_agent", "set_card_status"): DENY,
    ("accounts_agent", "list_payees"): DENY,
    ("accounts_agent", "initiate_transfer"): DENY,
    ("accounts_agent", "get_transfer_status"): DENY,
    ("accounts_agent", "search_knowledge"): DENY,
    # payments_agent — transfers within limits; no balance snooping
    ("payments_agent", "list_payees"): ALLOW,
    ("payments_agent", "initiate_transfer"): "transfer",
    ("payments_agent", "get_transfer_status"): ALLOW,
    ("payments_agent", "get_account_balance"): DENY,
    ("payments_agent", "get_customer_profile"): DENY,
    ("payments_agent", "list_accounts"): ALLOW,   # needs to pick a source account
    ("payments_agent", "get_transactions"): DENY,
    ("payments_agent", "set_card_status"): DENY,
    ("payments_agent", "search_knowledge"): DENY,
    # cards_agent — profile + card status writes only
    ("cards_agent", "get_customer_profile"): ALLOW,
    ("cards_agent", "list_accounts"): ALLOW,
    ("cards_agent", "set_card_status"): ALLOW,
    ("cards_agent", "get_account_balance"): DENY,
    ("cards_agent", "get_transactions"): DENY,
    ("cards_agent", "list_payees"): DENY,
    ("cards_agent", "initiate_transfer"): DENY,
    ("cards_agent", "get_transfer_status"): DENY,
    ("cards_agent", "search_knowledge"): DENY,
    # support_agent — knowledge only, no customer data at all
    ("support_agent", "search_knowledge"): ALLOW,
    ("support_agent", "get_customer_profile"): DENY,
    ("support_agent", "list_accounts"): DENY,
    ("support_agent", "get_account_balance"): DENY,
    ("support_agent", "get_transactions"): DENY,
    ("support_agent", "list_payees"): DENY,
    ("support_agent", "initiate_transfer"): DENY,
    ("support_agent", "get_transfer_status"): DENY,
    ("support_agent", "set_card_status"): DENY,
}


def decide(agent: str, tool: str, *, amount_cents: int | None = None,
           hitl_threshold_cents: int = 100_000, hard_limit_cents: int = 1_000_000) -> Decision:
    rule = POLICY.get((agent, tool), DENY)
    if rule == DENY:
        return Decision(DENY, f"policy: {agent} is not authorized to call {tool}")
    if rule == "transfer":
        if amount_cents is None:
            return Decision(DENY, "policy: transfer amount is required for authorization")
        if amount_cents > hard_limit_cents:
            return Decision(
                DENY,
                f"policy: transfer of ${amount_cents / 100:,.2f} exceeds the "
                f"${hard_limit_cents / 100:,.0f} online hard limit",
            )
        if amount_cents > hitl_threshold_cents:
            return Decision(
                REQUIRES_HITL,
                f"policy: transfers over ${hitl_threshold_cents / 100:,.0f} require human approval",
            )
        return Decision(ALLOW, "policy: within instant-transfer limit")
    return Decision(ALLOW, f"policy: {agent} may call {tool}")
