"""Authentication (AuthN): password hashing + JWT issue/verify.

AuthN answers "who are you?". It deliberately knows nothing about what an
agent or user is *allowed* to do — that is authorization, in policy.py.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

PBKDF2_ROUNDS = 100_000


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return digest.hex(), salt.hex()


def verify_password(password: str, expected_hash_hex: str, salt_hex: str) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
    )
    return hmac.compare_digest(digest.hex(), expected_hash_hex)


def create_token(secret: str, user: dict, exp_minutes: int = 120) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"],
        "customer_id": user["customer_id"],
        "iat": now,
        "exp": now + timedelta(minutes=exp_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> dict:
    """Raises jwt.PyJWTError on any problem (expired, bad signature, malformed)."""
    return jwt.decode(token, secret, algorithms=["HS256"])
