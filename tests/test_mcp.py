"""Real MCP protocol round-trips through the stdio subprocess client."""
from tests.conftest import login


def test_mcp_tools_list_accounts(runtime):
    tools = runtime.mcp.list_tools("accounts")
    for expected in ("get_customer_profile", "list_accounts", "get_account_balance",
                     "get_transactions", "set_card_status"):
        assert expected in tools


def test_mcp_tools_list_payments(runtime):
    tools = runtime.mcp.list_tools("payments")
    for expected in ("list_payees", "initiate_transfer", "get_transfer_status"):
        assert expected in tools


def test_mcp_call_roundtrip_balance(runtime):
    result = runtime.mcp.call_tool("accounts", "get_account_balance", {"account_id": 1})
    assert result["balance_cents"] == 425_075
    assert result["account_number"] == "1002345678"


def test_mcp_call_roundtrip_profile(runtime):
    result = runtime.mcp.call_tool("accounts", "get_customer_profile", {"customer_id": 1})
    assert result["full_name"] == "Maya Chen"
    assert result["ssn"] == "078-05-1120"
    assert result["cards"][0]["last4"] == "1111"


def test_ready_endpoint_reports_mcp_tools(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert "get_account_balance" in r.json()["mcp_accounts_tools"]
