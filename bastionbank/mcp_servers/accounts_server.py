"""Accounts MCP server (official MCP Python SDK, stdio transport).

Run:  python -m bastionbank.mcp_servers.accounts_server
Reads the bank DB path from BASTIONBANK_DB. Agents never import this module —
they reach it only through the MCP client + policy gate.

Note: card status lives in the accounts domain in this design, so this server
also exposes set_card_status (used by cards_agent, gated by policy).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())

from mcp.server.mcpserver import MCPServer

from bastionbank.store import Store

server = MCPServer("bastionbank-accounts")


def _store() -> Store:
    return Store(os.environ.get("BASTIONBANK_DB", "bastionbank.db"))


@server.tool()
def get_customer_profile(customer_id: int) -> dict:
    """Full customer profile (contains PII — in-bank only)."""
    store = _store()
    customer = store.get_customer(customer_id)
    if not customer:
        return {"error": f"customer {customer_id} not found"}
    cards = [{k: c[k] for k in ("id", "network", "last4", "status")} for c in store.list_cards(customer_id)]
    return {**customer, "cards": cards}


@server.tool()
def list_accounts(customer_id: int) -> dict:
    """List the customer's accounts with balances."""
    store = _store()
    return {"accounts": store.list_accounts(customer_id)}


@server.tool()
def get_account_balance(account_id: int) -> dict:
    """Current balance for one account."""
    store = _store()
    account = store.get_account(account_id)
    if not account:
        return {"error": f"account {account_id} not found"}
    return {"account_id": account["id"], "type": account["type"],
            "account_number": account["account_number"],
            "balance_cents": account["balance_cents"]}


@server.tool()
def get_transactions(account_id: int, limit: int = 10) -> dict:
    """Recent transactions for one account."""
    store = _store()
    return {"account_id": account_id, "transactions": store.get_transactions(account_id, limit)}


@server.tool()
def set_card_status(card_id: int, status: str) -> dict:
    """Freeze or unfreeze a card. status must be 'frozen' or 'active'."""
    if status not in ("frozen", "active"):
        return {"error": "status must be 'frozen' or 'active'"}
    store = _store()
    card = store.set_card_status(card_id, status)
    if not card:
        return {"error": f"card {card_id} not found"}
    return {"card_id": card["id"], "network": card["network"],
            "last4": card["last4"], "status": card["status"]}


if __name__ == "__main__":
    server.run(transport="stdio")
