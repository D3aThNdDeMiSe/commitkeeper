"""Deterministic guards: untrusted-instruction detection, evidence checks and the
action policy. The LLM proposes; this module decides what is actually allowed."""
from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS = [
    (r"ignore (all |any |your )?(previous|prior|above|earlier) (instructions|rules)", "ignore-instructions"),
    (r"\b(ai|llm) (assistant|agent|model)\b", "addresses-ai"),
    (r"\b(dear|attention|note to|hey) (the )?(assistant|bot|agent|ai)\b", "addresses-ai"),
    (r"system (prompt|note|override|message)", "system-override"),
    (r"(forward|send|export) (all|every|the full|your entire) (emails?|inbox|messages|mailbox)", "exfiltration"),
    (r"(add|put) [^.\n]{0,60} (to|on) (the|your) (allow ?list|whitelist)", "permission-change"),
    (r"mark (all|every|each) [^.\n]{0,40}(complete|completed|done|resolved)", "bulk-status-change"),
    (r"you are now|new instructions|disregard", "role-change"),
]


def scan_untrusted(text: str) -> list[tuple[str, str]]:
    """Return (detector, excerpt) pairs for instruction-like content aimed at the agent."""
    hits = []
    lowered = text.lower()
    for pattern, detector in INJECTION_PATTERNS:
        m = re.search(pattern, lowered)
        if m and detector not in {d for d, _ in hits}:
            start = max(0, m.start() - 40)
            hits.append((detector, text[start:m.end() + 60].replace("\n", " ").strip()))
    return hits


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_supported(quote: str, body: str) -> bool:
    """An evidence quote must actually appear in the email (stops invented evidence)."""
    q, b = _norm(quote).strip(" .\"'"), _norm(body)
    if len(q) < 8:
        return False
    if q in b:
        return True
    words = re.findall(r"[a-z0-9']+", q)
    body_words = set(re.findall(r"[a-z0-9']+", b))
    return bool(words) and sum(w in body_words for w in words) / len(words) >= 0.85


def has_delivery_evidence(body: str, attachments: list[str]) -> bool:
    """Attachments or links count as delivery evidence; a bare 'done' does not."""
    return bool(attachments) or bool(re.search(r"https?://\S+", body))


@dataclass(frozen=True)
class PolicyResult:
    action: str
    changed: bool
    reason: str


ALLOWED = {"i_owe": {"wait", "remind_me"}, "owed_to_me": {"wait", "draft_nudge", "escalate"}}


def apply_action_policy(proposed: str, *, direction: str, overdue_days: int, reminders_sent: int,
                        max_reminders: int, days_since_last_nudge: int | None, cooldown_days: int,
                        recipient: str, allowlist: frozenset[str]) -> PolicyResult:
    """Validate the LLM's proposed next action against hard rules."""
    action = proposed
    if action not in ALLOWED[direction]:
        action = "remind_me" if direction == "i_owe" else ("escalate" if overdue_days > 0 else "wait")
        return PolicyResult(action, True, f"'{proposed}' is not valid for direction {direction}")
    if action == "draft_nudge":
        if overdue_days <= 0:
            return PolicyResult("wait", True, "not overdue yet; nudges are only drafted after the due date")
        if reminders_sent >= max_reminders:
            return PolicyResult("escalate", True,
                                f"reminder limit reached ({reminders_sent}/{max_reminders})")
        if days_since_last_nudge is not None and days_since_last_nudge < cooldown_days:
            return PolicyResult("wait", True,
                                f"last nudge was {days_since_last_nudge} day(s) ago (cooldown {cooldown_days})")
        if recipient.lower() not in allowlist:
            return PolicyResult("escalate", True, f"{recipient} is not on the recipient allowlist")
    return PolicyResult(action, False, "allowed")
