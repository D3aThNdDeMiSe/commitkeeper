"""SQLite schema. SQLite is the single authoritative store; Excel/ICS files are views."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_id   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    owner_email  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Versioned preferences: a change supersedes the old row instead of overwriting it.
CREATE TABLE IF NOT EXISTS preferences (
    pref_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    TEXT NOT NULL REFERENCES profiles(profile_id),
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    version       INTEGER NOT NULL,
    is_current    INTEGER NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (profile_id, key, version)
);

-- One row per review cycle; `stage` is the resume checkpoint.
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id       TEXT PRIMARY KEY,
    profile_id     TEXT NOT NULL REFERENCES profiles(profile_id),
    seq            INTEGER NOT NULL,
    clock_now      TEXT NOT NULL,
    memory_enabled INTEGER NOT NULL,
    source         TEXT NOT NULL,
    status         TEXT NOT NULL,          -- running | failed | completed
    stage          TEXT NOT NULL,          -- started | fetched | extracted | planned
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    summary        TEXT,
    UNIQUE (profile_id, seq)
);

-- Source knowledge: every ingested email, never re-uploaded.
CREATE TABLE IF NOT EXISTS messages (
    profile_id     TEXT NOT NULL REFERENCES profiles(profile_id),
    message_id     TEXT NOT NULL,
    source         TEXT NOT NULL,          -- imap | eml_export
    source_ref     TEXT,
    thread_key     TEXT NOT NULL,
    from_addr      TEXT NOT NULL,
    to_addrs       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    sent_at        TEXT NOT NULL,
    body           TEXT NOT NULL,
    attachments    TEXT NOT NULL,          -- comma separated file names
    content_hash   TEXT NOT NULL,
    duplicate_of   TEXT,
    untrusted_flag TEXT,
    processed      INTEGER NOT NULL DEFAULT 0,
    cycle_id       TEXT,
    ingested_at    TEXT NOT NULL,
    PRIMARY KEY (profile_id, message_id)
);

-- Long-term memory: one row per tracked commitment...
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL REFERENCES profiles(profile_id),
    direction     TEXT NOT NULL CHECK (direction IN ('i_owe', 'owed_to_me')),
    owner         TEXT NOT NULL,
    counterparty  TEXT NOT NULL,
    thread_key    TEXT NOT NULL,
    status        TEXT NOT NULL,           -- open | needs_clarification | claimed_done | done
    status_reason TEXT,
    created_cycle TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- ...and its full version history (deadline/description changes are never overwritten).
CREATE TABLE IF NOT EXISTS commitment_versions (
    version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id     TEXT NOT NULL REFERENCES commitments(commitment_id),
    version_no        INTEGER NOT NULL,
    description       TEXT NOT NULL,
    due_date          TEXT,
    due_text          TEXT,
    source_message_id TEXT,
    evidence_quote    TEXT,
    approval          TEXT NOT NULL,       -- initial | pending | user_approved | user_correction | rejected
    is_current        INTEGER NOT NULL,
    change_reason     TEXT,
    created_at        TEXT NOT NULL,
    superseded_at     TEXT,
    UNIQUE (commitment_id, version_no)
);

-- Everything the agent proposed or did. idempotency_key makes retries safe.
CREATE TABLE IF NOT EXISTS actions (
    action_id         TEXT PRIMARY KEY,
    profile_id        TEXT NOT NULL REFERENCES profiles(profile_id),
    commitment_id     TEXT,
    cycle_id          TEXT,
    action_type       TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    requires_approval INTEGER NOT NULL,
    status            TEXT NOT NULL,
    recipient         TEXT,
    delivered_to      TEXT,
    subject           TEXT,
    body              TEXT,
    payload           TEXT,
    rationale         TEXT,
    evidence          TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    decided_at        TEXT,
    decision_note     TEXT,
    sent_at           TEXT
);

-- Episode memory: what the agent recommended for each commitment in each cycle.
CREATE TABLE IF NOT EXISTS plans (
    plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    TEXT NOT NULL,
    cycle_id      TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    action        TEXT NOT NULL,
    decided_by    TEXT NOT NULL,           -- rule | llm | llm+policy
    reason        TEXT,
    evidence      TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (cycle_id, commitment_id)
);

-- Manual columns read back from the Excel review view (kept separate from agent output).
CREATE TABLE IF NOT EXISTS manual_reviews (
    review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    user_decision TEXT,
    user_comments TEXT,
    imported_at   TEXT NOT NULL
);

-- Untrusted-instruction detections.
CREATE TABLE IF NOT EXISTS security_flags (
    flag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    detector    TEXT NOT NULL,
    excerpt     TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (profile_id, message_id, detector)
);

-- Audit log: tool calls, memory reads/writes, LLM calls, decisions, policy blocks, errors.
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    profile_id  TEXT,
    cycle_id    TEXT,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    duration_ms INTEGER,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_commitments_profile ON commitments(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_actions_profile ON actions(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_events_profile ON events(profile_id, cycle_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
