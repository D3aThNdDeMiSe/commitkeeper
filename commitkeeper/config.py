"""Runtime configuration.

Everything comes from environment variables (optionally loaded from a local .env
file that is git-ignored). No credentials live in code or in submitted files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).resolve() if raw else default


@dataclass(frozen=True)
class Config:
    db_path: Path
    log_dir: Path
    export_dir: Path
    fixtures_dir: Path
    # LLM (local Ollama)
    ollama_url: str
    model: str
    num_ctx: int
    seed: int
    llm_timeout_s: int
    think_decide: bool        # let qwen3 reason before choosing the next action
    # Mail read integration
    imap_host: str
    imap_user: str
    imap_password: str
    imap_folders: tuple[str, ...]
    # Mail send integration (controlled action)
    send_mode: str            # "dry_run" writes UNSENT drafts, "smtp" really sends
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    smtp_user: str
    smtp_password: str
    smtp_from: str
    allowlist: frozenset[str]  # only these recipients may ever be nudged
    route_to: str              # if set, every nudge is delivered to this owned test inbox
    # Testing hooks
    simulate_failure: str      # "", "fetch", "smtp_once", "llm_after:N"


def load_config() -> Config:
    allow = os.getenv("CK_ALLOWLIST", "")
    return Config(
        db_path=_path("CK_DB", ROOT / "data" / "commitkeeper.db"),
        log_dir=_path("CK_LOG_DIR", ROOT / "logs"),
        export_dir=_path("CK_EXPORT_DIR", ROOT / "exports"),
        fixtures_dir=_path("CK_FIXTURES", ROOT / "data" / "fixtures"),
        ollama_url=os.getenv("CK_OLLAMA_URL", "http://localhost:11434"),
        model=os.getenv("CK_MODEL", "qwen3:8b"),
        num_ctx=int(os.getenv("CK_NUM_CTX", "8192")),
        seed=int(os.getenv("CK_SEED", "42")),
        llm_timeout_s=int(os.getenv("CK_LLM_TIMEOUT", "180")),
        think_decide=_bool("CK_THINK_DECIDE", True),
        imap_host=os.getenv("CK_IMAP_HOST", "imap.gmail.com"),
        imap_user=os.getenv("CK_IMAP_USER", ""),
        imap_password=os.getenv("CK_IMAP_PASSWORD", ""),
        imap_folders=tuple(f.strip() for f in os.getenv("CK_IMAP_FOLDERS", "INBOX").split(",") if f.strip()),
        send_mode=os.getenv("CK_SEND_MODE", "dry_run").strip().lower(),
        smtp_host=os.getenv("CK_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("CK_SMTP_PORT", "465")),
        smtp_ssl=_bool("CK_SMTP_SSL", True),
        smtp_user=os.getenv("CK_SMTP_USER", ""),
        smtp_password=os.getenv("CK_SMTP_PASSWORD", ""),
        smtp_from=os.getenv("CK_SMTP_FROM", os.getenv("CK_SMTP_USER", "commitkeeper@localhost")),
        allowlist=frozenset(a.strip().lower() for a in allow.split(",") if a.strip()),
        route_to=os.getenv("CK_ROUTE_TO", "").strip(),
        simulate_failure=os.getenv("CK_SIMULATE_FAILURE", "").strip().lower(),
    )
