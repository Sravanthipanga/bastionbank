"""Redactor, scanner, and the headline property: PII never leaves the bank."""
from bastionbank.redactor import Redactor
from bastionbank.scanner import QUARANTINE_MARKER, scan
from bastionbank.seed import POISONED_ARTICLE_BODY, POISONED_PAYEE_NOTE, all_pii_values
from tests.conftest import chat, login


def test_redactor_tokenizes_each_pii_kind():
    r = Redactor()
    out = r.redact_text("SSN 078-05-1120, card 4111111111111111, mail maya.chen@example.com, "
                        "phone +1-312-555-0147, acct 1002345678")
    for pii in ("078-05-1120", "4111111111111111", "maya.chen@example.com",
                "+1-312-555-0147", "1002345678"):
        assert pii not in out
    for token in ("[SSN_1]", "[CARD_1]", "[EMAIL_1]", "[PHONE_1]", "[ACCT_1]"):
        assert token in out
    # Detokenization restores the original text exactly (inside the bank).
    assert "078-05-1120" in r.detokenize(out)


def test_redactor_obj_fields():
    r = Redactor()
    out = r.redact_obj({"ssn": "078-05-1120", "name": "Maya Chen",
                        "nested": {"account_number": "1002345678"}})
    assert out["ssn"].startswith("[SSN_")
    assert out["nested"]["account_number"].startswith("[ACCT_")
    assert out["name"] == "Maya Chen"


def test_scanner_flags_both_attack_fixtures():
    assert scan(POISONED_PAYEE_NOTE).flagged
    assert scan(POISONED_ARTICLE_BODY).flagged


def test_scanner_passes_clean_text():
    assert not scan("Monthly rent — unit 4B").flagged
    assert not scan("What is my checking balance?").flagged
    assert not scan("Transfer $100 to ComEd").flagged


def test_no_pii_egress(client, runtime):
    """Run PII-adjacent scenarios, then scan EVERY frontier payload."""
    maya = login(client, "maya")
    chat(client, maya, "Show my profile")                    # PII-bearing -> local only
    chat(client, maya, "What is my checking balance?")       # frontier
    chat(client, maya, "Show my accounts")                   # frontier
    chat(client, maya, "Show my payees")                     # frontier
    chat(client, maya, "What are your fees?")                # frontier
    payloads = runtime.store.all_egress_payloads()
    assert payloads, "frontier model must actually have been used (non-vacuous test)"
    joined = "\n".join(payloads)
    for pii in all_pii_values():
        assert pii not in joined, f"PII leaked to frontier: {pii}"
    # And the profile really was rendered locally (both models exercised).
    models = {r["model"] for r in runtime.store.query("SELECT DISTINCT model FROM cost_ledger")}
    assert "bastion-local-7b" in models and "frontier-sim-x1" in models


def test_profile_reply_stays_in_bank(client):
    maya = login(client, "maya")
    out = chat(client, maya, "Show my profile")
    assert out["agent"] == "accounts_agent"
    assert "078-05-1120" in out["reply"]  # owner sees their own data, served locally
