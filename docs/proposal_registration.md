# Proposal approval row (shared workbook)

Paste one field per column in the **Proposal approval** sheet. Leave *Teacher decision* and *Teacher comments* empty.

---

**A. Student ID/Rollnumber**

I22-0395

**B. Student name**

Immad Shah

**C. Idea ID or own title**

D13 – Inbox commitment tracker (CommitKeeper)

**D. User and minimum workflow**

User: a university student who also works as a research assistant and loses track of promises and deadlines spread across supervisor, teammate and course emails. Each review cycle the agent: (1) reads only the new emails from an owned test Gmail inbox (IMAP, read-only) or a clearly labelled .eml export; (2) uses the LLM to extract commitments in both directions (what I owe others and what others promised me), each with an exact evidence quote, while code converts the deadline phrase ("by Thursday") into a date from the email's own date and asks me when it is vague ("sometime next week"); (3) matches new emails to existing commitments: a revised promise becomes a deadline change that waits for my approval, "attached is…" closes an item, and a "done" claim with no attachment or link is marked unverified; (4) lets the LLM choose the next action for each item that needs attention (wait / remind me / draft a follow-up nudge / escalate) from code-computed facts and the recent thread, while code enforces the reminder limit, cooldown and recipient allowlist; (5) sends a nudge only after I approve it, exactly once, by SMTP to an allowlisted test address, or saves it as a clearly labelled UNSENT draft; (6) exports an Excel register and an .ics calendar. Demo: 13 fictional emails over 4 cycles, covering a revised promise, a forwarded duplicate, a vague deadline, an unsupported completion claim, an approved deadline extension, a prompt-injection email and a restart.

**E. Memory and restart demonstration**

SQLite is the authoritative store, keyed by profile ID (two isolated profiles). It remembers: every ingested email (Message-ID, a content hash for duplicate detection, a processed flag); each commitment's direction, owner and counterparty with its full version history (current and superseded due dates, who approved each change, source email and quote); every action with an idempotency key, approval decision, delivery outcome, attempts and errors; what the agent recommended in each cycle; versioned preferences (reminder limit, cooldown); and quarantined emails. Restart demo: every CLI command runs as a separate process. After cycle 2 the program exits. Cycle 3 reloads the store, fetches only the 3 new emails (no re-upload), uses the deadline I approved in between (Bilal's new 6 Nov date instead of 3 Nov) to decide he is now overdue and drafts one nudge, and closes my chapter commitment because the email has an attachment. In cycle 4 it remembers the nudge was already sent and reads Bilal's out-of-office reply, so it waits instead of nudging again. A `--no-memory` switch reruns the same inputs to show what breaks without memory.

**F. Tools and access plan**

Python CLI with a local LLM (qwen3:8b through Ollama; free, no API key). Tools: (1) mail reader: read-only IMAP on an owned test Gmail account (App Password kept in a git-ignored .env), or an .eml export folder labelled as an export; (2) commitment store: SQLite reads and writes with version history and idempotency keys; (3) deterministic deadline resolver and policy checker: dates, reminder limit, cooldown, allowlist, evidence-quote verification and a prompt-injection detector; (4) nudge sender: SMTP to allowlisted owned test addresses, only after my approval, otherwise dry-run drafts labelled UNSENT; (5) exporter: an Excel review register (my manual columns are kept on re-export) and an .ics calendar. All demo data is fictional (example.com addresses). Email content is treated as untrusted and cannot change recipients, permissions or statuses.
