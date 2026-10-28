"""Memory layer over SQLite.

Every query is scoped to one profile (isolation) and every read/write is logged.
With `enabled=False` the agent cannot retrieve anything from earlier cycles; this
is used for the memory-on vs memory-off comparison.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .logbook import Logbook, utc_now

DEFAULT_PREFS: dict[str, tuple[str, str]] = {
    "max_reminders": ("2", "Maximum follow-up nudges per commitment before escalating to you"),
    "nudge_cooldown_days": ("2", "Minimum days between two nudges for the same commitment"),
    "remind_days_before": ("1", "Remind you this many days before your own deadlines"),
}
ACTIVE_STATUSES = ("open", "needs_clarification", "claimed_done")


class IsolationError(PermissionError):
    pass


@dataclass
class Commitment:
    commitment_id: str
    direction: str
    owner: str
    counterparty: str
    thread_key: str
    status: str
    status_reason: str | None
    created_cycle: str | None
    version_no: int
    description: str
    due_date: str | None
    due_text: str | None
    source_message_id: str | None
    evidence_quote: str | None
    approval: str
    version_created_at: str


def create_profile(conn: sqlite3.Connection, log: Logbook, profile_id: str, name: str, email: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?)",
                       (profile_id, name, email.lower(), utc_now()))
    conn.commit()
    if cur.rowcount:
        log.event("memory_write", "profiles.insert", {"profile_id": profile_id, "owner_email": email})
    return bool(cur.rowcount)


def get_profile(conn: sqlite3.Connection, profile_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM profiles WHERE profile_id = ?", (profile_id,)).fetchone()


class Memory:
    def __init__(self, conn: sqlite3.Connection, log: Logbook, profile_id: str, *,
                 enabled: bool = True, cycle_id: str | None = None, clock: datetime | None = None):
        if get_profile(conn, profile_id) is None:
            raise KeyError(f"unknown profile '{profile_id}' (create it with `profile add`)")
        self.conn = conn
        self.log = log
        self.profile_id = profile_id
        self.enabled = enabled
        self.cycle_id = cycle_id
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        """Domain timestamps follow the (possibly simulated) review clock; the audit log uses real time."""
        return self.clock.isoformat(timespec="seconds") if self.clock else utc_now()

    def _read(self, name: str, **detail) -> None:
        self.log.event("memory_read", name, {"memory_enabled": self.enabled, **detail})

    def _write(self, name: str, **detail) -> None:
        self.log.event("memory_write", name, detail)

    def _scope_sql(self, alias: str = "c") -> tuple[str, tuple]:
        """With memory disabled only records created in the current cycle are visible."""
        if self.enabled:
            return "", ()
        return f" AND {alias}.created_cycle = ?", (self.cycle_id,)

    def _next_id(self, prefix: str, table: str, column: str) -> str:
        n = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?",
                              (self.profile_id,)).fetchone()[0] + 1
        while True:
            candidate = f"{prefix}-{self.profile_id}-{n:03d}"
            if not self.conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (candidate,)).fetchone():
                return candidate
            n += 1

    @property
    def owner_email(self) -> str:
        return get_profile(self.conn, self.profile_id)["owner_email"]

    # ------------------------------------------------------------- preferences
    def prefs(self) -> dict[str, int]:
        values = {k: v for k, (v, _) in DEFAULT_PREFS.items()}
        if self.enabled:
            for row in self.conn.execute(
                    "SELECT key, value FROM preferences WHERE profile_id = ? AND is_current = 1",
                    (self.profile_id,)):
                values[row["key"]] = row["value"]
        self._read("preferences.current", values=values)
        return {k: int(v) for k, v in values.items()}

    def set_pref(self, key: str, value: str, reason: str) -> int:
        if key not in DEFAULT_PREFS:
            raise KeyError(f"unknown preference '{key}'. Known: {', '.join(DEFAULT_PREFS)}")
        int(value)  # all current preferences are integers
        now = self._now()
        prev = self.conn.execute(
            "SELECT version FROM preferences WHERE profile_id = ? AND key = ? AND is_current = 1",
            (self.profile_id, key)).fetchone()
        version = (prev["version"] + 1) if prev else 1
        self.conn.execute("UPDATE preferences SET is_current = 0, superseded_at = ? "
                          "WHERE profile_id = ? AND key = ? AND is_current = 1", (now, self.profile_id, key))
        self.conn.execute("INSERT INTO preferences (profile_id, key, value, version, is_current, reason, created_at)"
                          " VALUES (?, ?, ?, ?, 1, ?, ?)", (self.profile_id, key, value, version, reason, now))
        self.conn.commit()
        self._write("preferences.supersede", key=key, value=value, version=version, reason=reason)
        return version

    def pref_history(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM preferences WHERE profile_id = ? ORDER BY key, version",
                                 (self.profile_id,)).fetchall()

    # ---------------------------------------------------------------- messages
    def known_message_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT message_id FROM messages WHERE profile_id = ?",
                                                (self.profile_id,))}

    def find_duplicate(self, content_hash: str) -> str | None:
        if not self.enabled:
            return None
        row = self.conn.execute(
            "SELECT message_id FROM messages WHERE profile_id = ? AND content_hash = ? ORDER BY sent_at LIMIT 1",
            (self.profile_id, content_hash)).fetchone()
        self._read("messages.by_content_hash", content_hash=content_hash[:12], found=bool(row))
        return row["message_id"] if row else None

    def store_message(self, m: dict) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages (profile_id, message_id, source, source_ref, thread_key, from_addr,"
            " to_addrs, subject, sent_at, body, attachments, content_hash, duplicate_of, cycle_id, ingested_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.profile_id, m["message_id"], m["source"], m.get("source_ref"), m["thread_key"], m["from_addr"],
             ",".join(m["to_addrs"]), m["subject"], m["sent_at"], m["body"], ",".join(m["attachments"]),
             m["content_hash"], m.get("duplicate_of"), self.cycle_id, utc_now()))
        self.conn.commit()
        if cur.rowcount:
            self._write("messages.insert", message_id=m["message_id"], duplicate_of=m.get("duplicate_of"))
        return bool(cur.rowcount)

    def unprocessed_messages(self) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM messages WHERE profile_id = ? AND processed = 0 ORDER BY sent_at",
                                 (self.profile_id,)).fetchall()
        self._read("messages.unprocessed", count=len(rows))
        return rows

    def mark_processed(self, message_id: str, untrusted_flag: str | None = None) -> None:
        self.conn.execute("UPDATE messages SET processed = 1, untrusted_flag = COALESCE(?, untrusted_flag) "
                          "WHERE profile_id = ? AND message_id = ?", (untrusted_flag, self.profile_id, message_id))
        self.conn.commit()
        self._write("messages.processed", message_id=message_id, untrusted_flag=untrusted_flag)

    def get_message(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE profile_id = ? AND message_id = ?",
                                 (self.profile_id, message_id)).fetchone()

    def messages_with(self, address: str, limit: int = 3) -> list[sqlite3.Row]:
        """Most recent non-duplicate, trusted messages exchanged with one contact."""
        scope = "" if self.enabled else " AND cycle_id = ?"
        params: tuple = (self.profile_id, address, f"%{address}%") + (() if self.enabled else (self.cycle_id,))
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE profile_id = ? AND (from_addr = ? OR to_addrs LIKE ?)"
            " AND duplicate_of IS NULL AND untrusted_flag IS NULL" + scope +
            " ORDER BY sent_at DESC LIMIT ?", params + (limit,)).fetchall()
        self._read("messages.with_contact", contact=address, count=len(rows))
        return rows

    # ------------------------------------------------------------- commitments
    def _commitment_rows(self, where: str, params: tuple) -> list[Commitment]:
        scope, scope_params = self._scope_sql()
        rows = self.conn.execute(
            "SELECT c.*, v.version_no, v.description, v.due_date, v.due_text, v.source_message_id,"
            " v.evidence_quote, v.approval, v.created_at AS version_created_at FROM commitments c"
            " JOIN commitment_versions v ON v.commitment_id = c.commitment_id AND v.is_current = 1"
            f" WHERE c.profile_id = ? {where}{scope} ORDER BY c.commitment_id",
            (self.profile_id, *params, *scope_params)).fetchall()
        return [Commitment(**{k: r[k] for k in Commitment.__dataclass_fields__}) for r in rows]

    def active_commitments(self) -> list[Commitment]:
        items = self._commitment_rows(f"AND c.status IN {ACTIVE_STATUSES}", ())
        self._read("commitments.active", ids=[c.commitment_id for c in items])
        return items

    def all_commitments(self) -> list[Commitment]:
        return self._commitment_rows("", ())

    def commitments_with(self, participants: set[str]) -> list[Commitment]:
        items = [c for c in self._commitment_rows(f"AND c.status IN {ACTIVE_STATUSES}", ())
                 if c.counterparty in participants]
        self._read("commitments.with_participants", participants=sorted(participants),
                   ids=[c.commitment_id for c in items])
        return items

    def get_commitment(self, commitment_id: str) -> Commitment:
        """Scoped lookup: another profile's ID raises IsolationError instead of leaking data."""
        owner = self.conn.execute("SELECT profile_id FROM commitments WHERE commitment_id = ?",
                                  (commitment_id,)).fetchone()
        if owner is None:
            raise KeyError(f"no commitment {commitment_id}")
        if owner["profile_id"] != self.profile_id:
            self.log.event("policy", "isolation.block", {"requested": commitment_id}, outcome="blocked")
            raise IsolationError(f"{commitment_id} does not belong to profile '{self.profile_id}'")
        found = self._commitment_rows("AND c.commitment_id = ?", (commitment_id,))
        if not found:
            raise KeyError(f"{commitment_id} is not visible in this cycle (memory disabled)")
        return found[0]

    def create_commitment(self, *, direction: str, owner: str, counterparty: str, thread_key: str,
                          description: str, due_date: str | None, due_text: str, message_id: str,
                          evidence_quote: str, status: str, status_reason: str | None) -> str:
        cid = self._next_id("C", "commitments", "commitment_id")
        now = self._now()
        self.conn.execute("INSERT INTO commitments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (cid, self.profile_id, direction, owner, counterparty, thread_key, status,
                           status_reason, self.cycle_id, now, now))
        self.conn.execute(
            "INSERT INTO commitment_versions (commitment_id, version_no, description, due_date, due_text,"
            " source_message_id, evidence_quote, approval, is_current, created_at)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, 'initial', 1, ?)",
            (cid, description, due_date, due_text, message_id, evidence_quote, now))
        self.conn.commit()
        self._write("commitments.insert", commitment_id=cid, direction=direction, due_date=due_date,
                    status=status, source=message_id)
        return cid

    def versions(self, commitment_id: str) -> list[sqlite3.Row]:
        self.get_commitment(commitment_id)
        return self.conn.execute("SELECT * FROM commitment_versions WHERE commitment_id = ? ORDER BY version_no",
                                 (commitment_id,)).fetchall()

    def add_version(self, commitment_id: str, *, due_date: str | None, due_text: str, message_id: str | None,
                    evidence_quote: str | None, approval: str, reason: str, description: str | None = None) -> int:
        """Add a new version. 'pending' versions wait for approval; others supersede the current one."""
        current = self.get_commitment(commitment_id)
        now = self._now()
        version_no = self.conn.execute("SELECT MAX(version_no) FROM commitment_versions WHERE commitment_id = ?",
                                       (commitment_id,)).fetchone()[0] + 1
        make_current = approval != "pending"
        if make_current:
            self.conn.execute("UPDATE commitment_versions SET is_current = 0, superseded_at = ? "
                              "WHERE commitment_id = ? AND is_current = 1", (now, commitment_id))
        self.conn.execute(
            "INSERT INTO commitment_versions (commitment_id, version_no, description, due_date, due_text,"
            " source_message_id, evidence_quote, approval, is_current, change_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (commitment_id, version_no, description or current.description, due_date, due_text, message_id,
             evidence_quote, approval, int(make_current), reason, now))
        self.conn.execute("UPDATE commitments SET updated_at = ? WHERE commitment_id = ?", (now, commitment_id))
        self.conn.commit()
        self._write("commitment_versions.insert", commitment_id=commitment_id, version_no=version_no,
                    due_date=due_date, approval=approval, reason=reason)
        return version_no

    def approve_version(self, commitment_id: str, version_no: int) -> None:
        self.get_commitment(commitment_id)
        now = self._now()
        self.conn.execute("UPDATE commitment_versions SET is_current = 0, superseded_at = ? "
                          "WHERE commitment_id = ? AND is_current = 1", (now, commitment_id))
        self.conn.execute("UPDATE commitment_versions SET is_current = 1, approval = 'user_approved' "
                          "WHERE commitment_id = ? AND version_no = ?", (commitment_id, version_no))
        self.conn.execute("UPDATE commitments SET updated_at = ? WHERE commitment_id = ?", (now, commitment_id))
        self.conn.commit()
        self._write("commitment_versions.approve", commitment_id=commitment_id, version_no=version_no)

    def reject_version(self, commitment_id: str, version_no: int) -> None:
        self.get_commitment(commitment_id)
        self.conn.execute("UPDATE commitment_versions SET approval = 'rejected', superseded_at = ? "
                          "WHERE commitment_id = ? AND version_no = ?", (self._now(), commitment_id, version_no))
        self.conn.commit()
        self._write("commitment_versions.reject", commitment_id=commitment_id, version_no=version_no)

    def set_status(self, commitment_id: str, status: str, reason: str) -> None:
        self.get_commitment(commitment_id)
        self.conn.execute("UPDATE commitments SET status = ?, status_reason = ?, updated_at = ? "
                          "WHERE commitment_id = ?", (status, reason, self._now(), commitment_id))
        self.conn.commit()
        self._write("commitments.status", commitment_id=commitment_id, status=status, reason=reason)

    # ----------------------------------------------------------------- actions
    def propose_action(self, *, commitment_id: str | None, action_type: str, idempotency_key: str,
                       requires_approval: bool, status: str, rationale: str, evidence: list | None = None,
                       recipient: str | None = None, subject: str | None = None, body: str | None = None,
                       payload: dict | None = None) -> tuple[str, bool]:
        """Insert an action unless one with the same idempotency key exists. Returns (action_id, created)."""
        existing = self.conn.execute("SELECT action_id FROM actions WHERE idempotency_key = ?",
                                     (idempotency_key,)).fetchone()
        if existing:
            self.log.event("policy", "duplicate_action.suppressed",
                           {"idempotency_key": idempotency_key, "existing": existing["action_id"]})
            return existing["action_id"], False
        aid = self._next_id("A", "actions", "action_id")
        self.conn.execute(
            "INSERT INTO actions (action_id, profile_id, commitment_id, cycle_id, action_type, idempotency_key,"
            " requires_approval, status, recipient, subject, body, payload, rationale, evidence, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, self.profile_id, commitment_id, self.cycle_id, action_type, idempotency_key,
             int(requires_approval), status, recipient, subject, body, json.dumps(payload or {}), rationale,
             json.dumps(evidence or []), self._now()))
        self.conn.commit()
        self._write("actions.insert", action_id=aid, action_type=action_type, commitment_id=commitment_id,
                    status=status, idempotency_key=idempotency_key)
        return aid, True

    def get_action(self, action_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(f"no action {action_id}")
        if row["profile_id"] != self.profile_id:
            self.log.event("policy", "isolation.block", {"requested": action_id}, outcome="blocked")
            raise IsolationError(f"{action_id} does not belong to profile '{self.profile_id}'")
        return row

    def update_action(self, action_id: str, **fields) -> None:
        self.get_action(action_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE actions SET {cols} WHERE action_id = ?", (*fields.values(), action_id))
        self.conn.commit()
        self._write("actions.update", action_id=action_id, **{k: v for k, v in fields.items() if k != "body"})

    def actions(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM actions WHERE profile_id = ?", [self.profile_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        return self.conn.execute(sql + " ORDER BY created_at, action_id", params).fetchall()

    def action_history(self, commitment_id: str) -> list[sqlite3.Row]:
        scope = "" if self.enabled else " AND cycle_id = ?"
        params = (self.profile_id, commitment_id) + (() if self.enabled else (self.cycle_id,))
        rows = self.conn.execute("SELECT * FROM actions WHERE profile_id = ? AND commitment_id = ?" + scope +
                                 " ORDER BY created_at", params).fetchall()
        self._read("actions.history", commitment_id=commitment_id, count=len(rows))
        return rows

    def open_items_for(self, commitment_id: str, action_type: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM actions WHERE profile_id = ? AND commitment_id = ? AND "
                                 "action_type = ? AND status = 'proposed'",
                                 (self.profile_id, commitment_id, action_type)).fetchall()

    # ------------------------------------------------------- plans and flags
    def record_plan(self, commitment_id: str, action: str, decided_by: str, reason: str, evidence: list) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO plans (profile_id, cycle_id, commitment_id, action, decided_by, reason,"
            " evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.profile_id, self.cycle_id, commitment_id, action, decided_by, reason, json.dumps(evidence),
             self._now()))
        self.conn.commit()
        self._write("plans.upsert", commitment_id=commitment_id, action=action, decided_by=decided_by)

    def latest_plans(self) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT p.* FROM plans p JOIN (SELECT commitment_id, MAX(plan_id) AS pid FROM plans"
            " WHERE profile_id = ? GROUP BY commitment_id) x ON p.plan_id = x.pid", (self.profile_id,)).fetchall()
        return {r["commitment_id"]: r for r in rows}

    def add_flag(self, message_id: str, detector: str, excerpt: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO security_flags (profile_id, message_id, detector, excerpt,"
                          " created_at) VALUES (?, ?, ?, ?, ?)",
                          (self.profile_id, message_id, detector, excerpt, self._now()))
        self.conn.commit()
        self.log.event("security", "untrusted_instruction.flagged",
                       {"message_id": message_id, "detector": detector, "excerpt": excerpt})

    def flags(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM security_flags WHERE profile_id = ? ORDER BY flag_id",
                                 (self.profile_id,)).fetchall()

    def purge(self) -> dict[str, int]:
        """Delete everything stored for this profile (memory lifecycle: deletion on request)."""
        counts = {}
        ids = [r[0] for r in self.conn.execute("SELECT commitment_id FROM commitments WHERE profile_id = ?",
                                                 (self.profile_id,))]
        counts["commitment_versions"] = self.conn.execute(
            f"DELETE FROM commitment_versions WHERE commitment_id IN ({','.join('?' * len(ids))})", ids
        ).rowcount if ids else 0
        for table in ("plans", "actions", "commitments", "messages", "manual_reviews", "security_flags",
                      "preferences", "cycles", "events", "profiles"):
            counts[table] = self.conn.execute(f"DELETE FROM {table} WHERE profile_id = ?",
                                              (self.profile_id,)).rowcount
        self.conn.commit()
        self.log.event("memory_write", "profile.purge", counts)
        return counts


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
