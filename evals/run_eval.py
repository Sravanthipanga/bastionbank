"""BastionBank eval pipeline: golden routing/answer scenarios + security scenarios.

Runs the real app in-process (TestClient) against a throwaway database.
Usage:  .venv/bin/python evals/run_eval.py
Prints a per-scenario result and a summary; exits nonzero on any failure.
This is the same gate CI runs (see .github/workflows/ci.yml).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dataclasses

from fastapi.testclient import TestClient

from bastionbank import queue as queue_mod
from bastionbank.app import create_app
from bastionbank.config import load_config
from bastionbank.runtime import Runtime
from bastionbank.seed import all_pii_values

PASSWORDS = {"maya": "maya-demo-123", "jordan": "jordan-demo-123", "banker": "banker-demo-123"}
HERE = os.path.dirname(os.path.abspath(__file__))


class Eval:
    def __init__(self):
        tmp = tempfile.mkdtemp(prefix="bastionbank-eval-")
        cfg = dataclasses.replace(
            load_config(db_path=os.path.join(tmp, "eval.db")),
            worker_poll_s=0.2, job_backoff_base_s=0.05)
        self.runtime = Runtime(cfg)
        self.client = TestClient(create_app(self.runtime))
        self.tokens: dict[str, str] = {}
        self.last_transfer: int | None = None
        self.failures: list[str] = []

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        self.runtime.mcp.stop()

    def token(self, username: str) -> str:
        if username not in self.tokens:
            r = self.client.post("/auth/login",
                                 json={"username": username, "password": PASSWORDS[username]})
            assert r.status_code == 200, r.text
            self.tokens[username] = r.json()["access_token"]
        return self.tokens[username]

    def chat(self, username: str, session_id: str, message: str) -> dict:
        message = message.replace("{last_transfer}", str(self.last_transfer))
        r = self.client.post("/chat", json={"message": message, "session_id": session_id},
                             headers={"Authorization": f"Bearer {self.token(username)}"})
        assert r.status_code == 200, r.text
        out = r.json()
        m = re.search(r"transfer #(\d+)", out["reply"])
        if m:
            self.last_transfer = int(m.group(1))
        return out

    def check(self, scenario: str, condition: bool, detail: str) -> None:
        if not condition:
            raise AssertionError(f"{scenario}: {detail}")

    def run_scenario(self, scenario: dict, kind: str) -> None:
        name = scenario["name"]
        store = self.runtime.store
        transfers_before = store.query_one("SELECT COUNT(*) AS n FROM transfers")["n"]
        session_id = f"eval-{kind}-{name}"
        user = scenario["user"]
        for step in scenario["steps"]:
            if "sleep_s" in step:
                time.sleep(step["sleep_s"])
                continue
            if step.get("action") == "approve_first_pending":
                approvals = queue_mod.list_approvals(store)
                self.check(name, bool(approvals), "no pending approval to act on")
                job_id = approvals[0]["id"]
                r = self.client.post(
                    f"/api/approvals/{job_id}/approve",
                    headers={"Authorization": f"Bearer {self.token(step['as'])}"})
                self.check(name, r.status_code == step["expect_http"],
                           f"approve as {step['as']}: HTTP {r.status_code} != {step['expect_http']}")
                continue
            out = self.chat(user, session_id, step["message"])
            if "expect_agent" in step:
                self.check(name, out["agent"] == step["expect_agent"],
                           f"agent {out['agent']} != {step['expect_agent']}")
            if "expect_status" in step:
                self.check(name, out["status"] == step["expect_status"],
                           f"status {out['status']} != {step['expect_status']}")
            for needle in step.get("expect_contains", []):
                self.check(name, needle in out["reply"], f"reply missing {needle!r}: {out['reply']!r}")
            for needle in step.get("expect_not_contains", []):
                self.check(name, needle.lower() not in out["reply"].lower(),
                           f"reply must not contain {needle!r}: {out['reply']!r}")
        for check_name in scenario.get("checks", []):
            if check_name == "no_new_transfers":
                after = store.query_one("SELECT COUNT(*) AS n FROM transfers")["n"]
                self.check(name, after == transfers_before,
                           f"transfers created: {transfers_before} -> {after}")
            elif check_name.startswith("security_event:"):
                kind_wanted = check_name.split(":", 1)[1]
                kinds = [e["kind"] for e in store.list_security_events()]
                self.check(name, kind_wanted in kinds, f"security event {kind_wanted} not logged")
            elif check_name == "egress_clean":
                joined = "\n".join(store.all_egress_payloads())
                for pii in all_pii_values():
                    self.check(name, pii not in joined, f"PII egressed to frontier: {pii}")
            elif check_name == "egress_nonempty":
                self.check(name, bool(store.all_egress_payloads()),
                           "egress log empty — frontier path never exercised")


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    with Eval() as ev:
        for filename, kind in (("golden_scenarios.json", "golden"),
                               ("security_scenarios.json", "security")):
            scenarios = json.load(open(os.path.join(HERE, filename)))
            for scenario in scenarios:
                try:
                    ev.run_scenario(scenario, kind)
                    results.append((scenario["name"], True, ""))
                    print(f"  PASS  [{kind}] {scenario['name']}")
                except AssertionError as exc:
                    results.append((scenario["name"], False, str(exc)))
                    print(f"  FAIL  [{kind}] {scenario['name']}: {exc}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"Eval: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
