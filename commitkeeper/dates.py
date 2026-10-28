"""Deterministic deadline resolution.

The LLM only copies the deadline phrase verbatim (e.g. "by Thursday"). This module
turns it into a calendar date relative to the email's own Date header. Vague
phrases are reported as ambiguous so the agent asks instead of guessing.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil import parser as dateparser

WEEKDAYS = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
WEEKDAY_ABBR = {name[:3]: i for name, i in WEEKDAYS.items()}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "ten": 10, "fourteen": 14}
VAGUE = ["next week", "sometime", "soon", "asap", "as soon as", "a few days", "later", "when you can",
         "at some point", "in a bit", "shortly", "coming days"]


@dataclass(frozen=True)
class DueResolution:
    due: date | None
    kind: str      # exact | relative | ambiguous | none
    note: str


def resolve_due(due_text: str | None, sent_at: datetime) -> DueResolution:
    text = (due_text or "").strip().lower()
    ref = sent_at.date()
    if not text or text in {"none", "n/a", "not specified", "unspecified", "no deadline"}:
        return DueResolution(None, "none", "no deadline stated")

    explicit = _explicit_date(text, ref)
    if explicit:
        return DueResolution(explicit, "exact", f"explicit date in '{due_text}'")

    if re.search(r"\b(today|tonight|eod|end of (the )?day|this evening|cob)\b", text) and not re.search(
            r"\b(mon|tue|wed|thu|fri|sat|sun)", text):
        return DueResolution(ref, "relative", "same day as the email")
    if "day after tomorrow" in text:
        return DueResolution(ref + timedelta(days=2), "relative", "day after tomorrow")
    if "tomorrow" in text:
        return DueResolution(ref + timedelta(days=1), "relative", "day after the email")

    m = re.search(r"\bin\s+(\d+|[a-z]+)\s+(day|days|week|weeks)\b", text)
    if m:
        qty = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS.get(m.group(1))
        if qty:
            days = qty * (7 if m.group(2).startswith("week") else 1)
            return DueResolution(ref + timedelta(days=days), "relative", f"{days} days after the email")
    if re.search(r"\b(within|in) (a|one) week\b", text):
        return DueResolution(ref + timedelta(days=7), "relative", "one week after the email")
    if re.search(r"\bend of (the )?month\b", text):
        last = calendar.monthrange(ref.year, ref.month)[1]
        return DueResolution(ref.replace(day=last), "relative", "last day of the email's month")
    if re.search(r"\bend of (the|this) week\b", text):
        return DueResolution(ref + timedelta(days=(4 - ref.weekday()) % 7), "relative", "Friday of that week")

    weekday = _weekday(text)
    if weekday is not None:
        ahead = (weekday - ref.weekday()) % 7
        if ahead == 0 and not re.search(r"\b(today|this)\b", text):
            ahead = 7
        due = ref + timedelta(days=ahead)
        if re.search(r"\bnext\s+(mon|tue|wed|thu|fri|sat|sun)", text):
            return DueResolution(None, "ambiguous",
                                 f"'{due_text}' could mean {due.isoformat()} or {(due + timedelta(days=7)).isoformat()}")
        return DueResolution(due, "relative", f"first {calendar.day_name[weekday]} on/after the email date")

    if any(v in text for v in VAGUE):
        return DueResolution(None, "ambiguous", f"'{due_text}' does not name a specific date")
    return DueResolution(None, "ambiguous", f"could not resolve '{due_text}' to a date")


def _weekday(text: str) -> int | None:
    for name, idx in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            return idx
    for abbr, idx in WEEKDAY_ABBR.items():
        if re.search(rf"\b{abbr}\b", text):
            return idx
    return None


DATE_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}\b",                                                    # 2026-11-09
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{MONTH_RE}\b(,?\s+\d{{4}})?",           # 9 November 2026
    rf"\b{MONTH_RE}\.?\s+\d{{1,2}}(st|nd|rd|th)?\b(,?\s+\d{{4}})?",                # Nov 9th, 2026
    r"\b\d{1,2}[/.]\d{1,2}([/.]\d{2,4})?\b",                                     # 09/11/2026 (day first)
]


def _explicit_date(text: str, ref: date) -> date | None:
    """Find one explicit date phrase and parse only that phrase (other numbers in the
    sentence, e.g. 'Assignment 3', must not leak into the date)."""
    match = next((m for m in (re.search(p, text) for p in DATE_PATTERNS) if m), None)
    if not match:
        return None
    try:
        parsed = dateparser.parse(match.group(0).replace(" of ", " "), dayfirst=True,
                                  default=datetime(ref.year, ref.month, 1))
    except (ValueError, OverflowError):
        return None
    result = parsed.date()
    if not re.search(r"\b20\d{2}\b", text) and result < ref - timedelta(days=60):
        result = result.replace(year=result.year + 1)      # "Jan 5" written in December
    return result
