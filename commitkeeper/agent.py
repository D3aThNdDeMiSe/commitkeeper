"""The CommitKeeper review cycle.

  1. retrieve memory   - preferences, active commitments, earlier actions (scoped to one profile)
  2. fetch             - tool: new emails from IMAP or an .eml export (checkpoint: fetched)
  3. extract           - LLM proposes operations per email; code validates evidence, participants,
                         dates and untrusted content before anything is written (checkpoint per email)
  4. plan              - code computes facts; the LLM chooses the next action for commitments that
                         need attention; the policy layer enforces hard limits (checkpoint: planned)
  5. user approval     - nudges, deadline changes and completion claims wait for the user
  6. controlled action - approved nudges are delivered once (idempotency key + status)

A failed cycle is resumed from its checkpoint by the next `run-cycle`.
"""
from __future__ import annotations

import difflib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Config
from .dates import resolve_due
from .llm import LLM
from .logbook import Logbook, utc_now
from .memory import IsolationError, Memory, get_profile
from .prompts import (DECIDE_SYSTEM, EXTRACT_SYSTEM, decide_schema, extract_schema, render_decide,
                      render_extract)
from .safety import ALLOWED, apply_action_policy, has_delivery_evidence, quote_supported, scan_untrusted
from .tools.mail_sender import DeliveryError, RecipientRefused, deliver
from .tools.mail_source import EmlExportSource, ImapSource, strip_quoted


class CycleFailed(RuntimeError):
    pass


@dataclass
class CycleReport:
    cycle_id: str
    clock_now: str
    memory_enabled: bool
    resumed: bool = False
    fetched: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)


def make_source(cfg: Config, profile_id: str, name: str):
    if name == "imap":
        return ImapSource(cfg)
    return EmlExportSource(cfg.fixtures_dir / profile_id)


class Agent:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, log: Logbook):
        self.cfg = cfg
        self.conn = conn
        self.log = log
        self.llm = LLM(cfg, log)

    # ================================================================ cycle
    def run_cycle(self, profile_id: str, now: datetime, source_name: str = "eml",
                  memory_enabled: bool = True) -> CycleReport:
        cycle, resumed = self._start_or_resume(profile_id, now, source_name, memory_enabled)
        now = datetime.fromisoformat(cycle["clock_now"])
        mem = Memory(self.conn, self.log, profile_id, enabled=bool(cycle["memory_enabled"]),
                     cycle_id=cycle["cycle_id"], clock=now)
        self.log.bind(profile_id, cycle["cycle_id"])
        report = CycleReport(cycle["cycle_id"], cycle["clock_now"], bool(cycle["memory_enabled"]), resumed)
        self.log.event("cycle", "resume" if resumed else "start",
                       {"clock_now": cycle["clock_now"], "stage": cycle["stage"], "memory": mem.enabled})
        stage = cycle["stage"]
        try:
            if stage == "started":
                self._fetch(mem, now, cycle["source"], report)
                stage = self._checkpoint(cycle["cycle_id"], "fetched")
            if stage == "fetched":
                for msg in mem.unprocessed_messages():
                    self._process_message(mem, msg, report)
                stage = self._checkpoint(cycle["cycle_id"], "extracted")
            if stage == "extracted":
                self._plan(mem, now, report)
                self._checkpoint(cycle["cycle_id"], "planned")
        except Exception as exc:
            self.conn.execute("UPDATE cycles SET status = 'failed', summary = ? WHERE cycle_id = ?",
                              (f"{type(exc).__name__}: {exc}", cycle["cycle_id"]))
            self.conn.commit()
            self.log.event("error", "cycle.failed", {"stage": stage, "error": f"{type(exc).__name__}: {exc}"},
                           outcome="error")
            raise CycleFailed(f"cycle {cycle['cycle_id']} failed during '{stage}': {exc}. "
                              "State is saved; run `run-cycle` again to resume.") from exc

        summary = {k: v for k, v in report.__dict__.items() if k != "plans"}
        self.conn.execute("UPDATE cycles SET status = 'completed', finished_at = ?, summary = ? WHERE cycle_id = ?",
                          (utc_now(), json.dumps(summary), cycle["cycle_id"]))
        self.conn.commit()
        self.log.event("cycle", "completed", summary)
        return report

    def _start_or_resume(self, profile_id: str, now: datetime, source: str, memory_enabled: bool):
        if get_profile(self.conn, profile_id) is None:
            raise KeyError(f"unknown profile '{profile_id}'")
        last = self.conn.execute("SELECT * FROM cycles WHERE profile_id = ? ORDER BY seq DESC LIMIT 1",
                                 (profile_id,)).fetchone()
        if last and last["status"] in ("running", "failed"):
            self.conn.execute("UPDATE cycles SET status = 'running' WHERE cycle_id = ?", (last["cycle_id"],))
            self.conn.commit()
            return last, True
        seq = (last["seq"] + 1) if last else 1
        cycle_id = f"{profile_id}-c{seq:03d}"
        self.conn.execute("INSERT INTO cycles (cycle_id, profile_id, seq, clock_now, memory_enabled, source, status,"
                          " stage, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', 'started', ?)",
                          (cycle_id, profile_id, seq, now.isoformat(), int(memory_enabled), source, utc_now()))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone(), False

    def _checkpoint(self, cycle_id: str, stage: str) -> str:
        self.conn.execute("UPDATE cycles SET stage = ? WHERE cycle_id = ?", (stage, cycle_id))
        self.conn.commit()
        self.log.event("checkpoint", stage, {"cycle_id": cycle_id})
        return stage

    # ================================================================ fetch
    def _fetch(self, mem: Memory, now: datetime, source_name: str, report: CycleReport) -> None:
        source = make_source(self.cfg, mem.profile_id, source_name)
        with self.log.tool(f"fetch_mail.{source.name}", until=now.isoformat()) as res:
            if self.cfg.simulate_failure == "fetch":
                raise ConnectionError("simulated mailbox outage (CK_SIMULATE_FAILURE=fetch)")
            fetched = source.fetch(now, mem.known_message_ids())
            own = [m["message_id"] for m in fetched if m["own_nudge"]]
            messages = sorted((m for m in fetched if m["message_id"] not in own), key=lambda m: m["sent_at"])
            res["new_messages"] = [m["message_id"] for m in messages]
            res["skipped_own_nudges"] = own
        for m in messages:
            m["duplicate_of"] = mem.find_duplicate(m["content_hash"])
            if mem.store_message(m):
                report.fetched.append(m["message_id"])

    # ============================================================== extract
    def _process_message(self, mem: Memory, msg: sqlite3.Row, report: CycleReport) -> None:
        mid = msg["message_id"]
        if msg["duplicate_of"]:
            self.log.event("decision", "message.duplicate_skipped", {"message_id": mid, "duplicate_of": msg["duplicate_of"]})
            report.duplicates.append(mid)
            mem.mark_processed(mid)
            return

        owner_email = mem.owner_email
        participants = sorted({owner_email, msg["from_addr"], *[a for a in msg["to_addrs"].split(",") if a]})
        others = set(participants) - {owner_email}
        known = mem.commitments_with(others)
        profile = get_profile(self.conn, mem.profile_id)
        msg = {**dict(msg), "body": strip_quoted(msg["body"])}     # quoted replies are old evidence
        result = self.llm.json_call(
            "extract", EXTRACT_SYSTEM, render_extract(owner_email, profile["display_name"], msg, known),
            extract_schema(participants, [c.commitment_id for c in known]))

        hits = scan_untrusted(f"{msg['subject']}\n{msg['body']}")
        if result.get("contains_instructions_to_assistant") and not hits:
            hits.append(("llm-detector", "model reported instructions aimed at the assistant"))
        if hits:
            for detector, excerpt in hits:
                mem.add_flag(mid, detector, excerpt)
            for op in result.get("operations", []):
                self._block(report, mid, op, "message contains instructions aimed at the agent; quarantined")
            report.flagged.append(mid)
            mem.mark_processed(mid, untrusted_flag=",".join(d for d, _ in hits))
            return

        for op in result.get("operations", []):
            try:
                self._apply_operation(mem, msg, op, known, report)
            except (KeyError, IsolationError) as exc:
                self._block(report, mid, op, exc.args[0] if exc.args else str(exc))
        mem.mark_processed(mid)

    def _block(self, report: CycleReport, mid: str, op: dict, reason: str) -> None:
        self.log.event("policy", "operation.blocked", {"message_id": mid, "operation": op, "reason": reason},
                       outcome="blocked")
        report.blocked.append(f"{mid}: {op.get('op')} {op.get('commitment_id') or ''} - {reason}")

    def _apply_operation(self, mem: Memory, msg: dict, op: dict, known: list, report: CycleReport) -> None:
        mid, kind, quote = msg["message_id"], op["op"], op.get("evidence_quote", "")
        sent_at = datetime.fromisoformat(msg["sent_at"])
        attachments = [a for a in msg["attachments"].split(",") if a]
        if not quote_supported(quote, msg["body"]):
            return self._block(report, mid, op, "evidence quote does not appear in the email")
        if op["due_text"] and op["due_text"].lower() not in msg["body"].lower():
            self.log.event("decision", "due_text.not_in_email",
                           {"message_id": mid, "due_text": op["due_text"], "using": "evidence quote"})
            op = {**op, "due_text": quote}      # resolve the date from text that is verified to be in the email

        if kind == "new":
            owner = op["owner"].lower()
            user = mem.owner_email
            if owner == user:
                direction = "i_owe"
                counterparty = msg["from_addr"] if msg["from_addr"] != user else next(
                    (a for a in msg["to_addrs"].split(",") if a and a != user), "")
            elif user in msg["to_addrs"].split(",") or msg["from_addr"] == user:
                direction, counterparty = "owed_to_me", owner
            else:
                return self._block(report, mid, op, "commitment does not involve the mailbox user")
            for c in known:
                similar = difflib.SequenceMatcher(None, c.description.lower(), op["description"].lower()).ratio()
                if c.counterparty == counterparty and c.direction == direction and similar >= 0.6:
                    self.log.event("decision", "commitment.already_tracked",
                                   {"message_id": mid, "existing": c.commitment_id, "similarity": round(similar, 2)})
                    return
            due = resolve_due(op["due_text"], sent_at)
            status = "needs_clarification" if due.kind == "ambiguous" else "open"
            cid = mem.create_commitment(
                direction=direction, owner=owner, counterparty=counterparty, thread_key=msg["thread_key"],
                description=op["description"], due_date=due.due.isoformat() if due.due else None,
                due_text=op["due_text"], message_id=mid, evidence_quote=quote, status=status,
                status_reason=due.note if status != "open" else None)
            self.log.event("decision", "deadline.resolved", {"commitment_id": cid, "due_text": op["due_text"],
                                                             "resolved": str(due.due), "kind": due.kind, "note": due.note})
            report.created.append(cid)
            if status == "needs_clarification":
                mem.propose_action(
                    commitment_id=cid, action_type="ask_clarification", idempotency_key=f"clarify:{cid}:{mid}",
                    requires_approval=False, status="proposed", evidence=[{"message_id": mid, "quote": quote}],
                    rationale=f"Deadline unclear: {due.note}. Please confirm the date with `correct {cid} --due YYYY-MM-DD`.")
            return

        c = mem.get_commitment(op["commitment_id"])
        if c.counterparty not in {msg["from_addr"], *msg["to_addrs"].split(",")}:
            return self._block(report, mid, op, f"{c.commitment_id} is with {c.counterparty}, who is not on this email")
        if c.evidence_quote and quote_supported(quote, c.evidence_quote):
            return self._block(report, mid, op, "evidence is the original promise, not new evidence")

        if kind == "update_deadline":
            due = resolve_due(op["due_text"], sent_at)
            if due.due is None:
                mem.propose_action(
                    commitment_id=c.commitment_id, action_type="ask_clarification",
                    idempotency_key=f"clarify:{c.commitment_id}:{mid}", requires_approval=False, status="proposed",
                    evidence=[{"message_id": mid, "quote": quote}],
                    rationale=f"A new deadline was mentioned but is unclear ({due.note}).")
                report.updates.append(f"{c.commitment_id}: unclear new deadline -> clarification")
                return
            if due.due.isoformat() == c.due_date:
                return
            version = mem.add_version(c.commitment_id, due_date=due.due.isoformat(), due_text=op["due_text"],
                                      message_id=mid, evidence_quote=quote, approval="pending",
                                      reason=f"new deadline proposed in {mid}")
            mem.propose_action(
                commitment_id=c.commitment_id, action_type="approve_deadline_change",
                idempotency_key=f"deadline:{c.commitment_id}:{mid}", requires_approval=True, status="proposed",
                evidence=[{"message_id": mid, "quote": quote}],
                payload={"version_no": version, "old": c.due_date, "new": due.due.isoformat()},
                rationale=f"Deadline change {c.due_date} -> {due.due.isoformat()} ({due.note}). Needs your approval.")
            report.updates.append(f"{c.commitment_id}: deadline {c.due_date} -> {due.due} (pending approval)")
            return

        if kind == "fulfilled":
            if msg["from_addr"] == c.owner and has_delivery_evidence(msg["body"], attachments):
                proof = ", ".join(attachments) or "link in body"
                mem.set_status(c.commitment_id, "done", f"delivered in {mid} ({proof})")
                report.updates.append(f"{c.commitment_id}: done (evidence: {proof})")
            else:
                mem.set_status(c.commitment_id, "claimed_done",
                               f"completion claimed in {mid} but no attachment or link was found")
                mem.propose_action(
                    commitment_id=c.commitment_id, action_type="confirm_completion",
                    idempotency_key=f"confirm:{c.commitment_id}:{mid}", requires_approval=True, status="proposed",
                    evidence=[{"message_id": mid, "quote": quote}],
                    rationale="Completion is claimed but not evidenced in the mailbox. Confirm only if you received it.")
                report.updates.append(f"{c.commitment_id}: completion claimed without evidence -> needs your confirmation")

    # ================================================================= plan
    def _plan(self, mem: Memory, now: datetime, report: CycleReport) -> None:
        prefs = mem.prefs()
        today = now.date()
        first_name = get_profile(self.conn, mem.profile_id)["display_name"].split()[0]
        for c in mem.active_commitments():
            if c.status == "needs_clarification":
                self._record(mem, report, c, "ask_clarification", "rule", c.status_reason or "deadline unclear", [])
                continue
            if c.status == "claimed_done":
                self._record(mem, report, c, "confirm_completion", "rule", c.status_reason or "", [])
                continue
            if not c.due_date:
                self._record(mem, report, c, "wait", "rule", "no deadline stated", [])
                continue

            history = mem.action_history(c.commitment_id)
            facts = self._facts(c, today, prefs, history)
            recent = mem.messages_with(c.counterparty)
            new_evidence = [m["message_id"] for m in recent if m["cycle_id"] == mem.cycle_id
                            and m["message_id"] != c.source_message_id]
            needs_attention = facts["days_to_due"] <= prefs["remind_days_before"] or facts["pending_change"] or new_evidence
            if not needs_attention:
                self._record(mem, report, c, "wait", "rule", f"due in {facts['days_to_due']} days; nothing new", [])
                continue

            allowed = sorted(ALLOWED[c.direction])
            decision = self.llm.json_call("decide_next_action", DECIDE_SYSTEM,
                                          render_decide(first_name, c, facts, history, recent),
                                          decide_schema(allowed), think=self.cfg.think_decide)
            policy = apply_action_policy(
                decision["action"], direction=c.direction, overdue_days=facts["overdue_days"],
                reminders_sent=facts["reminders_sent"], max_reminders=prefs["max_reminders"],
                days_since_last_nudge=facts["days_since_last_nudge"], cooldown_days=prefs["nudge_cooldown_days"],
                recipient=c.counterparty, allowlist=self.cfg.allowlist)
            reason = decision["reason"] + (f" [policy: {policy.reason}; LLM proposed {decision['action']}]"
                                           if policy.changed else "")
            if policy.changed:
                self.log.event("policy", "action.downgraded", {"commitment_id": c.commitment_id,
                                                               "proposed": decision["action"], "final": policy.action,
                                                               "reason": policy.reason}, outcome="blocked")
            evidence = decision.get("evidence_message_ids", [])
            self._record(mem, report, c, policy.action, "llm+policy" if policy.changed else "llm", reason, evidence)
            self._materialise(mem, c, policy.action, reason, evidence, facts, decision.get("nudge_text", ""),
                              first_name, today)

    def _facts(self, c, today: date, prefs: dict, history: list) -> dict:
        days_to_due = (date.fromisoformat(c.due_date) - today).days
        nudges = [a for a in history if a["action_type"] == "draft_nudge" and a["status"] in ("sent", "unsent_draft")]
        last = max((a["sent_at"] for a in nudges if a["sent_at"]), default=None)
        pending = [json.loads(a["payload"]) for a in history
                   if a["action_type"] == "approve_deadline_change" and a["status"] == "proposed"]
        return {
            "today": today.isoformat(), "weekday": today.strftime("%A"), "days_to_due": days_to_due,
            "overdue_days": max(0, -days_to_due), "reminders_sent": len(nudges),
            "max_reminders": prefs["max_reminders"], "last_nudge": last,
            "days_since_last_nudge": (today - datetime.fromisoformat(last).date()).days if last else None,
            "pending_change": f"{pending[-1]['new']} (was {pending[-1]['old']})" if pending else None,
        }

    def _record(self, mem: Memory, report: CycleReport, c, action: str, decided_by: str, reason: str,
                evidence: list) -> None:
        mem.record_plan(c.commitment_id, action, decided_by, reason, evidence)
        self.log.event("decision", "next_action", {"commitment_id": c.commitment_id, "action": action,
                                                   "decided_by": decided_by, "reason": reason, "evidence": evidence})
        report.plans.append({"commitment_id": c.commitment_id, "action": action, "decided_by": decided_by,
                             "reason": reason})

    def _materialise(self, mem: Memory, c, action: str, reason: str, evidence: list, facts: dict,
                     nudge_text: str, first_name: str, today: date) -> None:
        ev = [{"message_id": m} for m in evidence]
        if action == "remind_me":
            mem.propose_action(commitment_id=c.commitment_id, action_type="remind_me",
                               idempotency_key=f"remind:{c.commitment_id}:{today.isoformat()}",
                               requires_approval=False, status="shown", rationale=reason, evidence=ev)
        elif action == "escalate":
            mem.propose_action(commitment_id=c.commitment_id, action_type="escalate",
                               idempotency_key=f"escalate:{c.commitment_id}:v{c.version_no}",
                               requires_approval=False, status="shown", rationale=reason, evidence=ev)
        elif action == "draft_nudge":
            body = nudge_text.strip() or (
                f"Hi,\n\nJust following up on \"{c.description}\", which was due on {c.due_date}. "
                f"Could you let me know when I can expect it?\n\nThanks,\n{first_name}")
            mem.propose_action(commitment_id=c.commitment_id, action_type="draft_nudge",
                               idempotency_key=f"nudge:{c.commitment_id}:v{c.version_no}:n{facts['reminders_sent'] + 1}",
                               requires_approval=True, status="proposed", rationale=reason, evidence=ev,
                               recipient=c.counterparty, subject=f"Follow-up: {c.description}", body=body)


# ==================================================================== approvals
def approve(mem: Memory, action_id: str, now: datetime, note: str = "") -> str:
    a = mem.get_action(action_id)
    if a["status"] != "proposed" or not a["requires_approval"]:
        raise ValueError(f"{action_id} is '{a['status']}' ({a['action_type']}) and cannot be approved")
    kind, cid = a["action_type"], a["commitment_id"]
    if kind == "approve_deadline_change":
        payload = json.loads(a["payload"])
        mem.approve_version(cid, payload["version_no"])
        if mem.get_commitment(cid).status == "needs_clarification":
            mem.set_status(cid, "open", "deadline confirmed")
        result = f"{cid} due date is now {payload['new']} (was {payload['old']}; history kept)"
    elif kind == "confirm_completion":
        mem.set_status(cid, "done", f"completion confirmed by user {note}".strip())
        result = f"{cid} marked done"
    elif kind == "draft_nudge":
        result = f"nudge to {a['recipient']} approved; run `send` to deliver it"
    else:
        raise ValueError(f"{kind} cannot be approved")
    mem.update_action(action_id, status="approved", decided_at=now.isoformat(), decision_note=note or None)
    return result


def reject(mem: Memory, action_id: str, now: datetime, note: str = "") -> str:
    a = mem.get_action(action_id)
    if a["status"] != "proposed":
        raise ValueError(f"{action_id} is already '{a['status']}'")
    kind, cid = a["action_type"], a["commitment_id"]
    if kind == "approve_deadline_change":
        mem.reject_version(cid, json.loads(a["payload"])["version_no"])
    elif kind == "confirm_completion":
        mem.set_status(cid, "open", f"completion claim rejected by user {note}".strip())
    mem.update_action(action_id, status="rejected", decided_at=now.isoformat(), decision_note=note or None)
    return f"{action_id} rejected"


def correct(mem: Memory, commitment_id: str, due: date | None, reason: str, description: str | None = None) -> int:
    """A user correction supersedes the current version immediately (history kept)."""
    c = mem.get_commitment(commitment_id)
    version = mem.add_version(commitment_id, due_date=due.isoformat() if due else c.due_date,
                              due_text="set by user" if due else c.due_text, message_id=None, evidence_quote=None,
                              approval="user_correction", reason=reason, description=description)
    if c.status == "needs_clarification":
        mem.set_status(commitment_id, "open", f"clarified by user: {reason}")
    for a in mem.open_items_for(commitment_id, "ask_clarification"):
        mem.update_action(a["action_id"], status="answered", decision_note=reason)
    return version


def resolve(mem: Memory, commitment_id: str, status: str, reason: str) -> None:
    mem.set_status(commitment_id, status, f"set by user: {reason}")
    for a in mem.actions(("proposed",)):
        if a["commitment_id"] == commitment_id:
            mem.update_action(a["action_id"], status="superseded", decision_note=f"commitment {status}")


def send_approved(cfg: Config, mem: Memory, now: datetime, outbox: Path) -> list[str]:
    """Deliver approved (or previously failed) nudges exactly once."""
    results = []
    for a in mem.actions(("approved", "failed")):
        if a["action_type"] != "draft_nudge":
            continue
        if a["attempts"] >= 3:
            results.append(f"{a['action_id']}: gave up after {a['attempts']} attempts ({a['last_error']})")
            continue
        try:
            with mem.log.tool("send_nudge", action_id=a["action_id"], recipient=a["recipient"],
                              mode=cfg.send_mode, attempt=a["attempts"] + 1) as res:
                status, delivered_to = deliver(cfg, a, outbox)
                res.update(status=status, delivered_to=delivered_to)
        except RecipientRefused as exc:
            mem.update_action(a["action_id"], status="blocked", attempts=a["attempts"] + 1, last_error=str(exc))
            results.append(f"{a['action_id']}: BLOCKED ({exc}) - will not be retried")
            continue
        except DeliveryError as exc:
            mem.update_action(a["action_id"], status="failed", attempts=a["attempts"] + 1, last_error=str(exc))
            results.append(f"{a['action_id']}: FAILED ({exc}) - state kept, safe to retry with `send`")
            continue
        mem.update_action(a["action_id"], status=status, attempts=a["attempts"] + 1, last_error=None,
                          delivered_to=delivered_to, sent_at=now.isoformat())
        label = "SENT" if status == "sent" else "UNSENT DRAFT written (dry_run)"
        results.append(f"{a['action_id']}: {label} -> {delivered_to}")
    return results
