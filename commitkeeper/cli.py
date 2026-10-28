"""Command-line interface. Every command is a fresh process: state lives only in SQLite."""
from __future__ import annotations

import argparse
import imaplib
import json
import os
import sys
from datetime import date, datetime

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import agent as ag
from .config import load_config
from .db import connect
from .logbook import Logbook
from .memory import DEFAULT_PREFS, IsolationError, Memory, create_profile, get_profile
from .tools.exporter import export_ics, export_register
from .tools.mail_source import parse_eml, seed_imap

console = Console(width=int(os.getenv("COLUMNS", "150")))


def _clock(args, conn=None, profile: str | None = None) -> datetime:
    """--now > CK_NOW > (for read-only views) the last cycle's clock > real time."""
    raw = getattr(args, "now", None) or os.getenv("CK_NOW")
    if raw:
        return datetime.fromisoformat(raw)
    if conn is not None and profile:
        row = conn.execute("SELECT clock_now FROM cycles WHERE profile_id = ? ORDER BY seq DESC LIMIT 1",
                           (profile,)).fetchone()
        if row:
            return datetime.fromisoformat(row["clock_now"])
    return datetime.now().astimezone()


def _days(due: str | None, today: date) -> str:
    if not due:
        return ""
    n = (date.fromisoformat(due) - today).days
    return "today" if n == 0 else (f"in {n}d" if n > 0 else f"[red]{-n}d overdue[/red]")


# ---------------------------------------------------------------- views
def show_status(mem: Memory, now: datetime) -> None:
    conn, today = mem.conn, now.date()
    profile = get_profile(conn, mem.profile_id)
    last = conn.execute("SELECT * FROM cycles WHERE profile_id = ? ORDER BY seq DESC LIMIT 1",
                        (mem.profile_id,)).fetchone()
    cycle_line = (f"last cycle {last['cycle_id']} ({last['status']}, stage {last['stage']}, clock {last['clock_now']})"
                  if last else "no cycles yet")
    console.print(Panel(f"[bold]{profile['display_name']}[/bold] <{profile['owner_email']}>  |  {cycle_line}\n"
                        f"today (clock): {today:%a %d %b %Y}  |  store: SQLite (authoritative)",
                        title=f"CommitKeeper - {mem.profile_id}", box=box.ROUNDED))

    plans = mem.latest_plans()
    table = Table(title="Commitments", box=box.SIMPLE_HEAVY, show_lines=True)
    for col in ("ID", "Direction", "With", "What", "Due", "Status", "Recommended next action", "Evidence"):
        table.add_column(col, overflow="fold")
    for c in mem.all_commitments():
        older =[v["due_date"] for v in mem.versions(c.commitment_id) if not v["is_current"]
                 and v["approval"] != "pending" and v["due_date"]]
        due = f"{c.due_date or 'unresolved'} {_days(c.due_date, today) if c.status not in ('done',) else ''}"
        if older:
            due += f"\n[dim]was {', '.join(older)}[/dim]"
        plan = plans.get(c.commitment_id)
        nxt = "-" if c.status == "done" else (
            f"[bold]{plan['action']}[/bold] ({plan['decided_by']})\n[dim]{plan['reason']}[/dim]" if plan else "")
        status = {"done": "[green]done[/green]", "claimed_done": "[yellow]claimed done?[/yellow]",
                  "needs_clarification": "[yellow]needs clarification[/yellow]"}.get(c.status, c.status)
        if c.status_reason and c.status != "open":
            status += f"\n[dim]{c.status_reason}[/dim]"
        table.add_row(c.commitment_id, "I owe" if c.direction == "i_owe" else "owed to me", c.counterparty,
                      c.description, due, status, nxt, f"{c.source_message_id or 'user'}\n\"{c.evidence_quote or ''}\"")
    console.print(table)

    pending = [a for a in mem.actions(("proposed", "approved", "failed"))
               if (a["status"] == "proposed" and (a["requires_approval"] or a["action_type"] == "ask_clarification"))
               or (a["status"] != "proposed" and a["action_type"] == "draft_nudge")]
    if pending:
        t = Table(title="Needs you", box=box.SIMPLE_HEAVY, show_lines=True)
        for col in ("Action", "Type", "Commitment", "Details", "How to respond"):
            t.add_column(col, overflow="fold")
        for a in pending:
            if a["action_type"] == "ask_clarification":
                how = f"correct {a['commitment_id']} --due YYYY-MM-DD --reason ..."
            elif a["status"] in ("approved", "failed"):
                how = "send" + (f"  (last error: {a['last_error']})" if a["last_error"] else "")
            else:
                how = f"approve {a['action_id']}  |  reject {a['action_id']}"
            details = a["rationale"] or ""
            if a["action_type"] == "draft_nudge":
                details = f"To: {a['recipient']}\nSubject: {a['subject']}\n{a['body']}\n[dim]why: {a['rationale']}[/dim]"
            t.add_row(a["action_id"], f"{a['action_type']} ({a['status']})", a["commitment_id"], details, how)
        console.print(t)

    if last:
        shown = [a for a in mem.actions(("shown",)) if a["cycle_id"] == last["cycle_id"]]
        for a in shown:
            label = "REMINDER" if a["action_type"] == "remind_me" else "ESCALATE"
            console.print(f"[bold magenta]{label}[/bold magenta] {a['commitment_id']}: {a['rationale']}")
    sent = [a for a in mem.actions(("sent", "unsent_draft"))]
    for a in sent:
        tag = "sent" if a["status"] == "sent" else "UNSENT DRAFT (dry run)"
        console.print(f"[green]delivered[/green] {a['action_id']} {tag} to {a['delivered_to']} at {a['sent_at']}")
    for f in mem.flags():
        console.print(f"[bold red]QUARANTINED[/bold red] {f['message_id']} ({f['detector']}): \"{f['excerpt']}\"")


def show_report(r: ag.CycleReport) -> None:
    console.print(f"[bold]{r.cycle_id}[/bold] clock={r.clock_now} memory={'on' if r.memory_enabled else 'OFF'}"
                  f"{'  (resumed from checkpoint)' if r.resumed else ''}")
    console.print(f"  fetched {len(r.fetched)} new message(s); duplicates skipped: {len(r.duplicates)}; "
                  f"quarantined: {len(r.flagged)}")
    for cid in r.created:
        console.print(f"  + new commitment {cid}")
    for u in r.updates:
        console.print(f"  ~ {u}")
    for b in r.blocked:
        console.print(f"  [red]x blocked[/red] {b}")
    for p in r.plans:
        console.print(f"  > {p['commitment_id']}: {p['action']} ({p['decided_by']})")


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="commitkeeper", description="Inbox commitment tracker with persistent memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the SQLite store")

    pr = sub.add_parser("profile", help="manage isolated user profiles")
    prs = pr.add_subparsers(dest="action", required=True)
    add = prs.add_parser("add")
    add.add_argument("profile_id")
    add.add_argument("--name", required=True)
    add.add_argument("--email", required=True)
    prs.add_parser("list")

    pf = sub.add_parser("prefs", help="show or change versioned preferences")
    pfs = pf.add_subparsers(dest="action", required=True)
    show = pfs.add_parser("show")
    show.add_argument("--profile", required=True)
    setp = pfs.add_parser("set")
    setp.add_argument("key", choices=sorted(DEFAULT_PREFS))
    setp.add_argument("value")
    setp.add_argument("--profile", required=True)
    setp.add_argument("--reason", required=True)

    rc = sub.add_parser("run-cycle", help="run one review cycle")
    rc.add_argument("--profile", required=True)
    rc.add_argument("--source", choices=["eml", "imap"], default="eml")
    rc.add_argument("--now", help="simulated clock, ISO 8601 (default: CK_NOW or real time)")
    rc.add_argument("--no-memory", action="store_true", help="disable retrieval of earlier state (comparison)")

    for name in ("status", "send", "export", "metrics"):
        sp = sub.add_parser(name)
        sp.add_argument("--profile", required=True)
        sp.add_argument("--now")
        if name == "metrics":
            sp.add_argument("--cycle")

    for name in ("approve", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("action_id")
        sp.add_argument("--profile", required=True)
        sp.add_argument("--note", default="")
        sp.add_argument("--now")

    co = sub.add_parser("correct", help="correct a commitment's due date (supersedes, history kept)")
    co.add_argument("commitment_id")
    co.add_argument("--profile", required=True)
    co.add_argument("--due", required=True, type=date.fromisoformat)
    co.add_argument("--reason", required=True)

    rs = sub.add_parser("resolve", help="manually set a commitment's status")
    rs.add_argument("commitment_id")
    rs.add_argument("--profile", required=True)
    rs.add_argument("--status", required=True, choices=["done", "open"])
    rs.add_argument("--reason", required=True)

    hi = sub.add_parser("history", help="all versions and actions for one commitment")
    hi.add_argument("commitment_id")
    hi.add_argument("--profile", required=True)

    lg = sub.add_parser("log", help="show the audit log")
    lg.add_argument("--profile", required=True)
    lg.add_argument("--tail", type=int, default=40)
    lg.add_argument("--kind")
    lg.add_argument("--cycle")

    si = sub.add_parser("seed-imap", help="append fixture emails into your owned test inbox (live demo)")
    si.add_argument("--profile", required=True)
    si.add_argument("--until", required=True, help="only messages dated on/before this ISO time")

    pu = sub.add_parser("purge", help="delete all stored data for a profile")
    pu.add_argument("--profile", required=True)
    pu.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = load_config()
    conn = connect(cfg.db_path)
    log = Logbook(conn, cfg.log_dir)
    profile = getattr(args, "profile", None)
    log.bind(profile_id=profile)
    try:
        return _dispatch(args, cfg, conn, log, profile)
    except (KeyError, ValueError, IsolationError, ag.CycleFailed, RuntimeError, OSError, imaplib.IMAP4.error) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        log.event("error", f"cli.{args.cmd}", {"error": message}, outcome="error")
        console.print("[bold red]error:[/bold red] ", end="")
        console.print(message, markup=False, highlight=False)
        return 1
    finally:
        conn.close()


def _dispatch(args, cfg, conn, log, profile) -> int:
    if args.cmd == "init":
        console.print(f"store ready at {cfg.db_path}")
        return 0
    if args.cmd == "profile":
        if args.action == "add":
            created = create_profile(conn, log, args.profile_id, args.name, args.email)
            console.print(f"profile {args.profile_id} {'created' if created else 'already exists (unchanged)'}")
        else:
            for row in conn.execute("SELECT * FROM profiles ORDER BY profile_id"):
                console.print(f"{row['profile_id']}: {row['display_name']} <{row['owner_email']}>")
        return 0
    if args.cmd == "purge":
        if not args.yes:
            raise ValueError("purge deletes all memory for this profile; re-run with --yes to confirm")
        console.print(Memory(conn, log, profile).purge())
        return 0
    if args.cmd == "seed-imap":      # only copies fixture files into the test inbox; needs no stored profile
        until = datetime.fromisoformat(args.until)
        files = [f for f in sorted((cfg.fixtures_dir / profile).glob("*.eml"))
                 if datetime.fromisoformat(parse_eml(f.read_bytes(), "seed", f.name)["sent_at"]) <= until]
        if not files:
            raise ValueError(f"no fixture emails for '{profile}' dated on/before {args.until} in {cfg.fixtures_dir}")
        appended = seed_imap(cfg, files)
        log.event("tool_call", "seed_imap", {"appended": appended})
        console.print(f"appended {len(appended)} new message(s) to the test inbox "
                      f"({len(files) - len(appended)} already there): {appended}")
        return 0

    mem = Memory(conn, log, profile, clock=_clock(args, conn, profile))
    if args.cmd == "prefs":
        if args.action == "set":
            v = mem.set_pref(args.key, args.value, args.reason)
            console.print(f"{args.key} = {args.value} (version {v}; previous version kept as superseded)")
        cur = mem.prefs()
        for key, (default, desc) in DEFAULT_PREFS.items():
            console.print(f"{key} = {cur[key]}  [dim](default {default}) {desc}[/dim]")
        for row in mem.pref_history():
            state = "current" if row["is_current"] else f"superseded {row['superseded_at']}"
            console.print(f"  [dim]{row['key']} v{row['version']} = {row['value']} ({state}) - {row['reason']}[/dim]")
        return 0
    if args.cmd == "run-cycle":
        report = ag.Agent(cfg, conn, log).run_cycle(profile, _clock(args), args.source, not args.no_memory)
        mem.clock = datetime.fromisoformat(report.clock_now)
        show_report(report)
        console.print()
        show_status(Memory(conn, log, profile), datetime.fromisoformat(report.clock_now))
        return 0
    if args.cmd == "status":
        show_status(mem, _clock(args, conn, profile))
        return 0
    if args.cmd in ("approve", "reject"):
        fn = ag.approve if args.cmd == "approve" else ag.reject
        console.print(fn(mem, args.action_id, _clock(args, conn, profile), args.note))
        return 0
    if args.cmd == "correct":
        v = ag.correct(mem, args.commitment_id, args.due, args.reason)
        console.print(f"{args.commitment_id} corrected to {args.due} (version {v}; earlier versions kept)")
        return 0
    if args.cmd == "resolve":
        ag.resolve(mem, args.commitment_id, args.status, args.reason)
        console.print(f"{args.commitment_id} -> {args.status}")
        return 0
    if args.cmd == "send":
        results = ag.send_approved(cfg, mem, _clock(args, conn, profile), cfg.export_dir / "outbox")
        for line in results or ["nothing approved to send"]:
            console.print(line)
        return 0
    if args.cmd == "export":
        xlsx = export_register(mem, cfg.export_dir / f"{profile}_register.xlsx")
        ics = export_ics(mem, cfg.export_dir / f"{profile}_deadlines.ics")
        log.event("tool_call", "export", {"xlsx": xlsx, "ics": ics})
        console.print(f"Excel register: {xlsx['path']} ({xlsx['rows']} rows, {xlsx['manual_entries']} manual entries kept)")
        console.print(f"Calendar: {ics['path']} ({ics['events']} events)")
        return 0
    if args.cmd == "history":
        c = mem.get_commitment(args.commitment_id)
        console.print(f"[bold]{c.commitment_id}[/bold] {c.description} - status {c.status}")
        for v in mem.versions(c.commitment_id):
            mark = "CURRENT" if v["is_current"] else (v["superseded_at"] and "superseded" or v["approval"])
            console.print(f"  v{v['version_no']} due {v['due_date']} [{v['approval']}] {mark} - "
                          f"{v['change_reason'] or 'initial'} (source {v['source_message_id'] or 'user'})")
        for a in mem.action_history(c.commitment_id):
            console.print(f"  {a['action_id']} {a['action_type']} -> {a['status']} (cycle {a['cycle_id']})")
        return 0
    if args.cmd == "log":
        sql, params = "SELECT * FROM events WHERE profile_id = ?", [profile]
        if args.kind:
            sql += " AND kind = ?"
            params.append(args.kind)
        if args.cycle:
            sql += " AND cycle_id = ?"
            params.append(args.cycle)
        rows = conn.execute(sql + " ORDER BY event_id DESC LIMIT ?", (*params, args.tail)).fetchall()
        for r in reversed(rows):
            detail = (r["detail"] or "")[:220]
            console.print(f"[dim]{r['event_id']:>5} {r['ts']}[/dim] {r['kind']:<12} {r['name']:<34} "
                          f"{r['outcome']:<7} {detail}", markup=False, highlight=False)
        return 0
    if args.cmd == "metrics":
        sql = "SELECT * FROM events WHERE profile_id = ? AND kind = 'llm_call'"
        params = [profile] + ([args.cycle] if args.cycle else [])
        rows = conn.execute(sql + (" AND cycle_id = ?" if args.cycle else ""), params).fetchall()
        ok = [r for r in rows if r["outcome"] == "ok"]
        tokens = [json.loads(r["detail"]) for r in ok]
        latency = [r["duration_ms"] for r in ok]
        console.print(f"model calls: {len(rows)} ({len(ok)} ok) | model: {cfg.model} (local Ollama, $0)")
        if ok:
            console.print(f"latency ms: total {sum(latency)}, mean {sum(latency) // len(latency)}, max {max(latency)}")
            console.print(f"tokens: prompt {sum(t.get('prompt_tokens') or 0 for t in tokens)}, "
                          f"output {sum(t.get('output_tokens') or 0 for t in tokens)}")
        return 0
    raise ValueError(f"unknown command {args.cmd}")
