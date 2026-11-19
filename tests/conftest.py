import dataclasses

import pytest
from fastapi.testclient import TestClient

from bastionbank.app import create_app
from bastionbank.config import load_config
from bastionbank.runtime import Runtime
from bastionbank.seed import reseed

PASSWORDS = {"maya": "maya-demo-123", "jordan": "jordan-demo-123", "banker": "banker-demo-123"}


@pytest.fixture(scope="session")
def runtime(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("bb") / "test.db")
    cfg = dataclasses.replace(
        load_config(db_path=db_path),
        worker_poll_s=3600.0,          # background worker inert; tests drain manually
        job_backoff_base_s=0.05,
    )
    rt = Runtime(cfg)
    rt.ensure_started()
    yield rt
    rt.mcp.stop()


@pytest.fixture(autouse=True)
def fresh_data(runtime):
    reseed(runtime.store)
    yield


@pytest.fixture(scope="session")
def client(runtime):
    app = create_app(runtime)
    with TestClient(app) as c:
        yield c


def login(client, username: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def chat(client, token: str, message: str, session_id: str | None = None) -> dict:
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    r = client.post("/chat", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()
