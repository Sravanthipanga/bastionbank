"""Prompt-injection scanner.

Pattern-scans user input and tool outputs for instruction-like payloads.
Flagged content is QUARANTINED: replaced with a marker before any agent or
LLM can act on it, and a security event is recorded. The scanner is plain
code — content it scans can never change the policy gate or the scanner
itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

QUARANTINE_MARKER = "[QUARANTINED: suspected prompt injection removed]"

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override", re.compile(r"system\s+override", re.I)),
    ("ignore_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior|above|policy|instructions)", re.I)),
    ("ignore_policy", re.compile(r"ignore\s+(the\s+)?policy", re.I)),
    ("disregard", re.compile(r"disregard\s+(all\s+)?(previous|prior|instructions|policy)", re.I)),
    ("reveal_pii", re.compile(r"(reveal|disclose|show|tell|give)\b[^.]{0,60}\b(ssn|social security|account number|card number)", re.I)),
    ("assistant_impersonation", re.compile(r"^\s*assistant\s*:", re.I | re.M)),
    ("system_impersonation", re.compile(r"^\s*system\s*:", re.I | re.M)),
    ("approve_without", re.compile(r"approve\b[^.]{0,60}\bwithout\s+(approval|review|confirmation)", re.I)),
    ("instruct_transfer", re.compile(r"instruct\s+the\s+user\s+to\s+transfer", re.I)),
    ("role_hijack", re.compile(r"you\s+are\s+now\s+(a|an|in)\b", re.I)),
    ("jailbreak", re.compile(r"\b(jailbreak|developer\s+mode|do\s+anything\s+now)\b", re.I)),
]


@dataclass
class ScanResult:
    flagged: bool
    matches: list[str] = field(default_factory=list)


def scan(text: str) -> ScanResult:
    if not text:
        return ScanResult(False)
    hits = [name for name, pat in PATTERNS if pat.search(text)]
    return ScanResult(bool(hits), hits)


def quarantine_text(text: str) -> tuple[str, ScanResult]:
    result = scan(text)
    if result.flagged:
        return QUARANTINE_MARKER, result
    return text, result


def scan_and_quarantine_obj(obj):
    """Recursively quarantine flagged strings inside tool output structures.

    Returns (cleaned_obj, list_of_(field_path, matched_patterns)).
    """
    events: list[tuple[str, list[str]]] = []

    def walk(node, path: str):
        if isinstance(node, str):
            cleaned, result = quarantine_text(node)
            if result.flagged:
                events.append((path, result.matches))
            return cleaned
        if isinstance(node, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}" if path else k) for k, v in node.items()}
        return node

    return walk(obj, ""), events
