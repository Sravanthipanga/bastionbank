"""PII vault: detection + tokenization.

The redactor replaces PII with opaque tokens ([SSN_1], [ACCT_1], ...). The
token -> value map (the "vault") lives only in this process's memory and is
never sent anywhere. The frontier model only ever sees tokenized text; the
final user-facing reply is detokenized locally, inside the bank.
"""
from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){15}\d\b|\b(?:\d[ -]?){13}\d\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"\b\+?1[-.\s]?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("ACCT", re.compile(r"\b\d{10}\b")),
]

PII_FIELD_NAMES = {"ssn", "card_number", "account_number", "email", "phone"}


class Redactor:
    """Stateful per-run redactor with its own vault."""

    def __init__(self, known_values: list[str] | None = None):
        self.vault: dict[str, str] = {}          # token -> real value
        self._by_value: dict[str, str] = {}      # real value -> token
        self._counters: dict[str, int] = {}
        # Exact-value pass catches seeded PII even in odd formats.
        self._known = sorted(set(known_values or []), key=len, reverse=True)

    def _token_for(self, kind: str, value: str) -> str:
        if value in self._by_value:
            return self._by_value[value]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"[{kind}_{self._counters[kind]}]"
        self.vault[token] = value
        self._by_value[value] = token
        return token

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        for value in self._known:
            if value and value in out:
                kind = self._kind_of(value)
                out = out.replace(value, self._token_for(kind, value))
        for kind, pattern in PATTERNS:
            out = pattern.sub(lambda m: self._token_for(kind, m.group(0)), out)
        return out

    def _kind_of(self, value: str) -> str:
        for kind, pattern in PATTERNS:
            if pattern.fullmatch(value):
                return kind
        return "PII"

    def redact_obj(self, obj):
        """Recursively redact strings in dicts/lists; PII-named fields always tokenized."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k in PII_FIELD_NAMES and isinstance(v, str) and v:
                    kind = {"ssn": "SSN", "card_number": "CARD", "account_number": "ACCT",
                            "email": "EMAIL", "phone": "PHONE"}[k]
                    out[k] = self._token_for(kind, v)
                else:
                    out[k] = self.redact_obj(v)
            return out
        return obj

    def detokenize(self, text: str) -> str:
        out = text
        for token, value in self.vault.items():
            out = out.replace(token, value)
        return out

    def contains_pii(self, obj) -> bool:
        probe = Redactor(self._known)
        if isinstance(obj, str):
            return probe.redact_text(obj) != obj
        import json
        text = json.dumps(obj, default=str)
        return probe.redact_text(text) != text
