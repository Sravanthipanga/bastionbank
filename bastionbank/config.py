"""Runtime configuration, all overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    db_path: str
    database_url: str | None  # seam for the production Postgres store (see store.py)
    jwt_secret: str
    jwt_exp_minutes: int = 120
    hitl_threshold_cents: int = 100_000      # transfers > $1,000.00 need human approval
    hard_limit_cents: int = 1_000_000        # transfers > $10,000.00 are denied outright
    session_budget_usd: float = 2.00         # simulated-dollar cap per chat session
    max_steps: int = 12                      # runaway-loop protection: tool/LLM steps per run
    loop_repeat_limit: int = 3               # identical tool call this many times -> break
    job_max_attempts: int = 3
    job_backoff_base_s: float = 2.0
    worker_poll_s: float = 0.5


def load_config(db_path: str | None = None) -> Config:
    env = os.environ
    return Config(
        db_path=db_path or env.get("BASTIONBANK_DB", os.path.join(os.getcwd(), "bastionbank.db")),
        database_url=env.get("DATABASE_URL"),
        jwt_secret=env.get("BASTIONBANK_JWT_SECRET", "dev-only-secret-change-me"),
        jwt_exp_minutes=int(env.get("BASTIONBANK_JWT_EXP_MINUTES", "120")),
        hitl_threshold_cents=int(env.get("BASTIONBANK_HITL_THRESHOLD_CENTS", "100000")),
        hard_limit_cents=int(env.get("BASTIONBANK_HARD_LIMIT_CENTS", "1000000")),
        session_budget_usd=float(env.get("BASTIONBANK_SESSION_BUDGET_USD", "2.00")),
        max_steps=int(env.get("BASTIONBANK_MAX_STEPS", "12")),
        loop_repeat_limit=int(env.get("BASTIONBANK_LOOP_REPEAT_LIMIT", "3")),
        job_max_attempts=int(env.get("BASTIONBANK_JOB_MAX_ATTEMPTS", "3")),
        job_backoff_base_s=float(env.get("BASTIONBANK_JOB_BACKOFF_BASE_S", "2.0")),
        worker_poll_s=float(env.get("BASTIONBANK_WORKER_POLL_S", "0.5")),
    )
