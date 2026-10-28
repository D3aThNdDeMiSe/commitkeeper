"""Audit log. Every tool call, memory read/write, LLM call, decision and error is
written to the `events` table and mirrored to logs/events.jsonl."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Logbook:
    def __init__(self, conn: sqlite3.Connection, log_dir: Path):
        self.conn = conn
        self.log_dir = log_dir
        self.profile_id: str | None = None
        self.cycle_id: str | None = None

    def bind(self, profile_id: str | None = None, cycle_id: str | None = None) -> None:
        if profile_id is not None:
            self.profile_id = profile_id
        if cycle_id is not None:
            self.cycle_id = cycle_id

    def event(self, kind: str, name: str, detail: Any = None, outcome: str = "ok",
              duration_ms: int | None = None) -> int:
        ts = utc_now()
        detail_json = json.dumps(detail, default=str, ensure_ascii=False) if detail is not None else None
        cur = self.conn.execute(
            "INSERT INTO events (ts, profile_id, cycle_id, kind, name, outcome, duration_ms, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, self.profile_id, self.cycle_id, kind, name, outcome, duration_ms, detail_json),
        )
        self.conn.commit()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        record = {"event_id": cur.lastrowid, "ts": ts, "profile": self.profile_id, "cycle": self.cycle_id,
                  "kind": kind, "name": name, "outcome": outcome, "duration_ms": duration_ms,
                  "detail": detail}
        with open(self.log_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        return cur.lastrowid

    @contextmanager
    def tool(self, name: str, **args: Any) -> Iterator[dict]:
        """Time a tool call and log its outcome. The body can put a summary in the yielded dict."""
        result: dict = {}
        start = time.perf_counter()
        try:
            yield result
        except Exception as exc:
            self.event("tool_call", name, {"args": args, "error": f"{type(exc).__name__}: {exc}"},
                       outcome="error", duration_ms=int((time.perf_counter() - start) * 1000))
            raise
        self.event("tool_call", name, {"args": args, "result": result},
                   duration_ms=int((time.perf_counter() - start) * 1000))
