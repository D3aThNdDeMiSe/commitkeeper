"""Tool 1 - evidence retrieval: read new emails.

Two sources share one parser:
  * ImapSource      - real read-only integration with an owned test mailbox (e.g. Gmail + app password)
  * EmlExportSource - a folder of exported .eml files (clearly labelled 'eml_export', not a live inbox)

Both return only messages that are not already stored and whose Date is not after
the (possibly simulated) clock, so a review cycle never re-reads old mail.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import re
from datetime import datetime
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from ..config import Config
from .mail_sender import OWN_MESSAGE_SUFFIX


def _addresses(value: str | None) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([value or ""]) if addr]


def canonical_body(body: str) -> str:
    """Normalise a body for duplicate detection: drop forward headers, quoted replies and whitespace."""
    text = body
    fwd = re.search(r"-{2,}\s*forwarded message\s*-{2,}", text, re.I)
    if fwd:
        rest = text[fwd.end():]
        parts = re.split(r"\n\s*\n", rest, maxsplit=1)   # skip the From:/Date:/Subject:/To: block
        text = parts[1] if len(parts) > 1 else rest
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip().lower()


def strip_quoted(body: str) -> str:
    """Remove quoted earlier messages ('> ...' lines and 'On ... wrote:' tails): they are old evidence."""
    kept = []
    for line in body.splitlines():
        if re.match(r"^\s*On .{5,200} wrote:\s*$", line):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def thread_key(msg: email.message.EmailMessage, message_id: str) -> str:
    refs = (msg.get("References") or "").split()
    if refs:
        return refs[0].strip()
    if msg.get("In-Reply-To"):
        return msg["In-Reply-To"].strip()
    return message_id


def parse_eml(raw: bytes, source: str, source_ref: str) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = "<sha-" + hashlib.sha256(raw).hexdigest()[:24] + "@commitkeeper>"
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    if body_part is not None and body_part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    attachments = [part.get_filename() or "attachment" for part in msg.iter_attachments()]
    sent_at = parsedate_to_datetime(msg["Date"]) if msg.get("Date") else datetime.now().astimezone()
    return {
        "message_id": message_id,
        "source": source,
        "source_ref": source_ref,
        "thread_key": thread_key(msg, message_id),
        "from_addr": (_addresses(msg.get("From")) or ["unknown@unknown"])[0],
        "to_addrs": _addresses(msg.get("To")) + _addresses(msg.get("Cc")),
        "subject": str(msg.get("Subject") or ""),
        "sent_at": sent_at.isoformat(),
        "body": body.strip(),
        "attachments": attachments,
        "content_hash": hashlib.sha256(canonical_body(body).encode()).hexdigest(),
        "own_nudge": bool(msg.get("X-CommitKeeper-Action")) or message_id.endswith(OWN_MESSAGE_SUFFIX),
    }


class EmlExportSource:
    name = "eml_export"

    def __init__(self, folder: Path):
        self.folder = folder

    def fetch(self, now: datetime, known_ids: set[str]) -> list[dict]:
        if not self.folder.exists():
            raise FileNotFoundError(f"export folder not found: {self.folder}")
        found = []
        for path in sorted(self.folder.glob("*.eml")):
            parsed = parse_eml(path.read_bytes(), self.name, path.name)
            if parsed["message_id"] in known_ids or datetime.fromisoformat(parsed["sent_at"]) > now:
                continue
            found.append(parsed)
        return found


class ImapSource:
    name = "imap"

    def __init__(self, cfg: Config):
        if not (cfg.imap_user and cfg.imap_password):
            raise RuntimeError("IMAP is not configured: set CK_IMAP_USER and CK_IMAP_PASSWORD in .env")
        self.cfg = cfg

    def fetch(self, now: datetime, known_ids: set[str]) -> list[dict]:
        found = []
        with imaplib.IMAP4_SSL(self.cfg.imap_host) as conn:
            conn.login(self.cfg.imap_user, self.cfg.imap_password)
            for folder in self.cfg.imap_folders:
                status, _ = conn.select(f'"{folder}"', readonly=True)     # read-only: never changes the mailbox
                if status != "OK":
                    continue
                _, data = conn.uid("SEARCH", None, "ALL")
                for uid in data[0].split():
                    _, head = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    header = head[0][1].decode(errors="replace") if head and head[0] else ""
                    mid = header.split(":", 1)[1].strip() if ":" in header else ""
                    if mid and mid in known_ids:
                        continue
                    _, full = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                    parsed = parse_eml(full[0][1], self.name, f"{folder}:{uid.decode()}")
                    if parsed["message_id"] in known_ids or datetime.fromisoformat(parsed["sent_at"]) > now:
                        continue
                    found.append(parsed)
        return found


def seed_imap(cfg: Config, files: list[Path]) -> list[str]:
    """Copy fixture .eml files into the owned test inbox (IMAP APPEND) for the live demo."""
    appended = []
    with imaplib.IMAP4_SSL(cfg.imap_host) as conn:
        conn.login(cfg.imap_user, cfg.imap_password)
        conn.select('"INBOX"')
        for path in files:
            raw = path.read_bytes()
            mid = parse_eml(raw, "seed", path.name)["message_id"]
            _, hits = conn.search(None, "HEADER", "Message-ID", mid)
            if hits and hits[0]:
                continue
            date = imaplib.Time2Internaldate(parsedate_to_datetime(
                email.message_from_bytes(raw, policy=policy.default)["Date"]))
            conn.append('"INBOX"', None, date, raw)
            appended.append(path.name)
    return appended
