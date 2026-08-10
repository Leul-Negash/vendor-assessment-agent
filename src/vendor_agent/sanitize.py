"""Untrusted content handling.

Retrieved text is data. It is never appended to the agent's instructions, so a
document cannot reach the planner as a directive in the first place; the
structural defence is the authority tier, not this scanner. What the scanner
adds is detection: an injection attempt is quarantined, reported in the
decision, and kept out of fact resolution so it cannot influence an outcome.
"""

from __future__ import annotations

import re

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("policy_override", re.compile(r"\bignore\b[^.]{0,40}\b(all|any|the)?\s*(company\s+)?polic", re.I)),
    ("instruction_override", re.compile(r"\b(disregard|override|bypass|forget)\b[^.]{0,40}\b(instruction|polic|rule|prompt)", re.I)),
    ("forced_action", re.compile(r"\b(approve|authorise|authorize|accept)\b[^.]{0,30}\b(immediately|now|without)", re.I)),
    ("tool_suppression", re.compile(r"\bdo not\b[^.]{0,40}\b(call|use|check|verify|consult)\b", re.I)),
    ("privilege_claim", re.compile(r"\b(you are|act as)\b[^.]{0,30}\b(admin|administrator|approver|system)\b", re.I)),
    ("prompt_probe", re.compile(r"\b(system prompt|previous instructions|your instructions)\b", re.I)),
)


def scan(text: str | None) -> list[str]:
    """Return the names of injection patterns present in `text`."""
    if not text:
        return []
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]


def redact(text: str, max_length: int = 160) -> str:
    """Flatten untrusted text for logging: single line, bounded, quoted."""
    flat = " ".join(text.split())
    if len(flat) > max_length:
        flat = flat[: max_length - 1] + "…"
    return f"<untrusted>{flat}</untrusted>"
