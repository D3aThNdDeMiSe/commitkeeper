# Memory design and lifecycle

**Authoritative store:** one SQLite file (`data/commitkeeper.db`, schema in [`commitkeeper/db.py`](../commitkeeper/db.py)). The Excel register and the `.ics` calendar are regenerated *views*; the only thing read back from Excel is the two manual columns, which are stored in `manual_reviews` and never overwritten. SQLite fits this workflow because commitments, deadlines and actions are structured records that need exact lookups, versioning and uniqueness constraints. There is no vector database, because nothing here needs semantic search.

## Components

| Component | Where | Content | Bounded how |
|---|---|---|---|
| Working state | `cycles` (stage checkpoint), `messages.processed`, in-memory `CycleReport` | current cycle, clock, stage reached, which emails are already extracted | one open cycle per profile; resumes instead of starting a new one |
| Long-term memory | `commitments`, `commitment_versions`, `actions`, `plans`, `preferences`, `manual_reviews` | promises and deadlines (current and superseded), approvals, nudges sent and their outcomes, recommendations per cycle, preferences | only facts that affect a later decision are kept |
| Source knowledge | `messages` | email headers, body, attachments list, content hash, duplicate link, quarantine flag | Message-ID primary key: an email is stored once |
| Audit | `events` (+ `logs/events.jsonl` mirror) | tool calls, memory reads/writes, LLM prompts/responses/latency/tokens, decisions, policy blocks, errors | append-only |

## Lifecycle

| Record | Saved when | Why | Retrieved when | Corrected / deleted how |
|---|---|---|---|---|
| Email (`messages`) | fetch step | evidence for every commitment; duplicate detection | extraction (thread context), planning (recent messages with the contact) | never edited; deleted with `purge` |
| Commitment + version 1 | an extracted operation passes validation | the thing being tracked | every cycle (`commitments_with`, `active_commitments`) | new version (`pending` → `user_approved`, or `user_correction`); old version kept with `superseded_at` |
| Pending deadline change | a later email proposes a new date | approved timelines change only with confirmation | planning shows it as a fact; `approve`/`reject` | `reject` marks it `rejected`; history kept |
| Action | planner/extractor proposes it | approvals, nudge history, reminder limit, cooldown, duplicate prevention | planning (reminder count, last nudge), status, export | status transitions only (`proposed → approved → sent/unsent_draft/failed`, `rejected`, `superseded`) |
| Plan | each cycle | explains *why* the agent recommended an action (episode memory) | status view, Excel register | replaced only within the same cycle |
| Preference | `prefs set` | user-tunable limits | start of planning | new version supersedes the old one; history shown by `prefs show` |
| Manual review | `export` reads the yellow columns | keep the user's decisions separate from agent suggestions | written back into every new export | new row per change (history) |
| Security flag | detector or LLM flags an email | quarantine evidence | status view | deleted with `purge` |

**Metadata kept:** stable IDs (`profile_id`, `C-<profile>-NNN`, `A-<profile>-NNN`, cycle IDs), source `message_id` and evidence quote per version, approval type, `created_at`/`superseded_at` (on the review clock), plus real-time timestamps in the audit log.

**Who may access what:** every query in [`memory.py`](../commitkeeper/memory.py) filters by `profile_id`. Looking up another profile's commitment or action raises `IsolationError`, which is logged as `isolation.block`. The LLM only ever receives the current profile's records.

**Deletion:** `python -m commitkeeper purge --profile <id> --yes` removes all rows for the profile (including audit events). The JSONL log mirror in `logs/` must be deleted by hand.

**Memory switch for evaluation:** `run-cycle --no-memory` makes the memory layer return only records created in the current cycle and default preferences. The comparison then shows which decisions depend on remembered state.
