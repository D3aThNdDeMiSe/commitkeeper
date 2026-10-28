"""Tool 3 - controlled action: deliver an approved follow-up nudge.

* send_mode=smtp    -> real SMTP delivery to an allowlisted / owned test address
* send_mode=dry_run -> an .eml file in exports/outbox marked "UNSENT DRAFT"

The Message-ID is derived from the action's idempotency key, so a delivered nudge
can be recognised if it ever shows up again.
"""
from __future__ import annotations

import hashlib
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..config import Config


OWN_MESSAGE_SUFFIX = "@commitkeeper.local>"   # nudges we sent; ignored when they come back via IMAP


class DeliveryError(RuntimeError):
    pass


class RecipientRefused(DeliveryError):
    """Permanent: the recipient is not allowlisted, so retrying can never succeed."""


def build_message(cfg: Config, action, deliver_to: str) -> EmailMessage:
    msg = EmailMessage()
    original = action["recipient"]
    msg["From"] = cfg.smtp_from
    msg["To"] = deliver_to
    subject = action["subject"] or "Follow-up"
    msg["Subject"] = subject if deliver_to == original else f"[test route for {original}] {subject}"
    digest = hashlib.sha256(action["idempotency_key"].encode()).hexdigest()[:20]
    msg["Message-ID"] = f"<ck-{digest}{OWN_MESSAGE_SUFFIX}"
    msg["X-CommitKeeper-Action"] = action["action_id"]
    msg.set_content(action["body"] or "")
    return msg


def deliver(cfg: Config, action, outbox: Path) -> tuple[str, str]:
    """Return (status, delivered_to). Raises DeliveryError on failure."""
    if cfg.simulate_failure == "smtp_once" and action["attempts"] == 0:
        raise DeliveryError("simulated SMTP failure: 421 service not available (CK_SIMULATE_FAILURE)")
    recipient = action["recipient"].lower()
    if recipient not in cfg.allowlist:
        raise RecipientRefused(f"refused: {recipient} is not on the allowlist")
    deliver_to = cfg.route_to or recipient

    if cfg.send_mode != "smtp":
        outbox.mkdir(parents=True, exist_ok=True)
        msg = build_message(cfg, action, deliver_to)
        msg["X-CommitKeeper-Status"] = "UNSENT DRAFT (dry_run mode - nothing was sent)"
        (outbox / f"{action['action_id']}.eml").write_bytes(bytes(msg))
        return "unsent_draft", deliver_to

    msg = build_message(cfg, action, deliver_to)
    try:
        smtp_cls = smtplib.SMTP_SSL if cfg.smtp_ssl else smtplib.SMTP
        with smtp_cls(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            if cfg.smtp_user:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"{type(exc).__name__}: {exc}") from exc
    return "sent", deliver_to
