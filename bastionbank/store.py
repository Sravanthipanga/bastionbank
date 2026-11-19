"""Data layer.

SQLite is the fully-implemented, exercised store. A connection seam for the
production Postgres store is provided: when DATABASE_URL points at Postgres,
`open_store()` returns a PostgresStore placeholder that raises a clear error —
the compose stack provides Postgres for production, but the sandbox exercises
SQLite only (documented honestly in the README "What ran where" section).
All application code talks to the Store interface, never to sqlite3 directly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import Config


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, salt TEXT NOT NULL,
    role TEXT NOT NULL, customer_id INTEGER
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL,
    phone TEXT NOT NULL, ssn TEXT NOT NULL, address TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, type TEXT NOT NULL,
    account_number TEXT NOT NULL, balance_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
    card_number TEXT NOT NULL, last4 TEXT NOT NULL, network TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, ts TEXT NOT NULL,
    description TEXT NOT NULL, amount_cents INTEGER NOT NULL, balance_after_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payees (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, name TEXT NOT NULL,
    account_number TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS knowledge_articles (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, agent TEXT, trace_id TEXT, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blackboard (
    session_id TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (session_id, namespace, key)
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, from_account_id INTEGER NOT NULL,
    payee_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3, next_run_at TEXT NOT NULL,
    last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, session_id TEXT, user_id INTEGER,
    started_at TEXT NOT NULL, finished_at TEXT, agent TEXT, status TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    id INTEGER PRIMARY KEY, trace_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    started_at TEXT NOT NULL, duration_ms REAL NOT NULL, attributes TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cost_ledger (
    id INTEGER PRIMARY KEY, trace_id TEXT, session_id TEXT, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS egress_log (
    id INTEGER PRIMARY KEY, trace_id TEXT, model TEXT NOT NULL, payload TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY, trace_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL, ts TEXT NOT NULL
);
"""


class Store:
    """SQLite-backed store. Thread-safe via a lock + per-call connections."""

    backend = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with self._lock:
                yield conn
                conn.commit()
        finally:
            conn.close()

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._conn() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- domain helpers --------------------------------------------------
    def get_user(self, username: str) -> dict | None:
        return self.query_one("SELECT * FROM users WHERE username=?", (username,))

    def get_customer(self, customer_id: int) -> dict | None:
        return self.query_one("SELECT * FROM customers WHERE id=?", (customer_id,))

    def list_accounts(self, customer_id: int) -> list[dict]:
        return self.query("SELECT * FROM accounts WHERE customer_id=? ORDER BY id", (customer_id,))

    def get_account(self, account_id: int) -> dict | None:
        return self.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))

    def get_transactions(self, account_id: int, limit: int = 10) -> list[dict]:
        return self.query(
            "SELECT * FROM transactions WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        )

    def list_cards(self, customer_id: int) -> list[dict]:
        return self.query("SELECT * FROM cards WHERE customer_id=? ORDER BY id", (customer_id,))

    def set_card_status(self, card_id: int, status: str) -> dict | None:
        self.execute("UPDATE cards SET status=? WHERE id=?", (status, card_id))
        return self.query_one("SELECT * FROM cards WHERE id=?", (card_id,))

    def list_payees(self, customer_id: int) -> list[dict]:
        return self.query("SELECT * FROM payees WHERE customer_id=? ORDER BY id", (customer_id,))

    def get_payee(self, payee_id: int) -> dict | None:
        return self.query_one("SELECT * FROM payees WHERE id=?", (payee_id,))

    def search_articles(self, query: str) -> list[dict]:
        import re
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        out = []
        for art in self.query("SELECT * FROM knowledge_articles ORDER BY id"):
            text = (art["title"] + " " + art["body"]).lower()
            score = sum(1 for w in words if w in text)
            if score:
                out.append((score, art))
        out.sort(key=lambda t: -t[0])
        return [a for _, a in out[:3]]

    # -- sessions / memory ------------------------------------------------
    def ensure_session(self, session_id: str, user_id: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO sessions (id, user_id, created_at) VALUES (?,?,?)",
            (session_id, user_id, utcnow()),
        )

    def add_message(self, session_id: str, role: str, content: str,
                    agent: str | None = None, trace_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO messages (session_id, role, content, agent, trace_id, ts) VALUES (?,?,?,?,?,?)",
            (session_id, role, content, agent, trace_id, utcnow()),
        )

    def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self.query(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    def bb_set(self, session_id: str, namespace: str, key: str, value) -> None:
        self.execute(
            "INSERT INTO blackboard (session_id, namespace, key, value) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id, namespace, key) DO UPDATE SET value=excluded.value",
            (session_id, namespace, key, json.dumps(value)),
        )

    def bb_get(self, session_id: str, namespace: str, key: str):
        row = self.query_one(
            "SELECT value FROM blackboard WHERE session_id=? AND namespace=? AND key=?",
            (session_id, namespace, key),
        )
        return json.loads(row["value"]) if row else None

    def bb_clear(self, session_id: str, namespace: str, key: str) -> None:
        self.execute(
            "DELETE FROM blackboard WHERE session_id=? AND namespace=? AND key=?",
            (session_id, namespace, key),
        )

    # -- security / audit ---------------------------------------------------
    def log_security_event(self, kind: str, detail: str, trace_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO security_events (trace_id, kind, detail, ts) VALUES (?,?,?,?)",
            (trace_id, kind, detail, utcnow()),
        )

    def list_security_events(self) -> list[dict]:
        return self.query("SELECT * FROM security_events ORDER BY id")

    def log_egress(self, model: str, payload: str, trace_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO egress_log (trace_id, model, payload, ts) VALUES (?,?,?,?)",
            (trace_id, model, payload, utcnow()),
        )

    def all_egress_payloads(self) -> list[str]:
        return [r["payload"] for r in self.query("SELECT payload FROM egress_log ORDER BY id")]


class PostgresStore(Store):
    """Production seam: compose provides Postgres; the sandbox exercises SQLite.

    The full application talks to the Store interface above. Wiring this class
    to psycopg is the production step; it intentionally refuses to run here so
    nobody mistakes an unexercised path for a working one.
    """

    backend = "postgres"

    def __init__(self, database_url: str):  # pragma: no cover - not exercised
        raise NotImplementedError(
            "PostgresStore is the production seam (see docker-compose.yml); "
            "this reference build exercises the SQLite store. "
            f"DATABASE_URL was: {database_url.split('@')[-1] if '@' in database_url else 'set'}"
        )


def open_store(config: Config) -> Store:
    if config.database_url and config.database_url.startswith("postgres"):
        return PostgresStore(config.database_url)
    return Store(config.db_path)
