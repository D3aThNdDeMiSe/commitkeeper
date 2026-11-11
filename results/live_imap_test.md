# Live Gmail integration test (IMAP read + SMTP send)

Owned test Gmail account created for this project (address and App Password kept in the git-ignored `.env`; not reproduced here; shown as `<test inbox>` below). The full four-cycle sample story was run against the real Gmail servers: cycle 1 on 2026-10-29, cycles 2–4 on 2026-11-10. Settings as in the evaluation: qwen3:8b, temperature 0, seed 42, 8k context, `CK_SEND_MODE=smtp`, `CK_ROUTE_TO` set to the test inbox, so the nudge addressed to `bilal@example.com` is delivered to the owned inbox instead.

## Cycle 1 (2026-10-29)

| Step | Command | Outcome |
|---|---|---|
| 1 | `profile add maryam ...` | profile created |
| 2 | `seed-imap --profile maryam --until 2026-11-02T09:00:00+05:00` | IMAP APPEND of ['m01.eml', 'm02.eml', 'm03.eml', 'm04.eml'] into INBOX (2026-10-28T21:02:08+00:00 UTC) |
| 3 | `run-cycle --profile maryam --source imap --now 2026-11-02T09:00:00+05:00` | read-only IMAP fetch (`select(readonly=True)`, `BODY.PEEK`) of 4 new messages in 4659 ms; cycle completed |
| 4 | SMTP login check (no message sent) | `smtp.gmail.com:465` login accepted (235) |

Messages stored by the cycle (source and IMAP reference):

| Message-ID | Source | IMAP ref | Subject |
|---|---|---|---|
| <m01.maryam@fixtures.commitkeeper> | imap | INBOX:4 | Literature review chapter |
| <m02.maryam@fixtures.commitkeeper> | imap | INBOX:5 | Data cleaning script |
| <m03.maryam@fixtures.commitkeeper> | imap | INBOX:6 | Re: Data cleaning script |
| <m04.maryam@fixtures.commitkeeper> | imap | INBOX:7 | CS-401: Assignment 3 deadline |

Commitments extracted from the live inbox (identical to the offline reference run):

| ID | Direction | Description | Due |
|---|---|---|---|
| C-maryam-001 | i_owe | Send revised literature review chapter to Dr. Imran Qadir | 2026-11-05 |
| C-maryam-002 | owed_to_me | Send comments on proposal draft to Maryam Siddiqui | 2026-11-04 |
| C-maryam-003 | i_owe | Push data-cleaning script to project repo | 2026-11-02 |
| C-maryam-004 | owed_to_me | Share annotated dataset with Maryam | 2026-11-03 |
| C-maryam-005 | i_owe | Submit CS-401 Assignment 3 | 2026-11-09 |

## Cycles 2–4 (2026-11-10)

Each command ran as a new process against the same live store (`data/commitkeeper.db`), continuing from cycle 1 twelve days later without re-reading m01–m04.

| Step | Command | Outcome |
|---|---|---|
| 5 | `seed-imap --until 2026-11-04T09:00:00+05:00` | APPEND of m05–m09 (4 already in the inbox) |
| 6 | `run-cycle --source imap --now 2026-11-04T09:00:00+05:00` | cycle `maryam-c002`: 5 fetched in 4778 ms, 1 duplicate (m06 → m04), 1 quarantined (m07, 5 detectors), new `C-maryam-006` (poster, no date), pending change 3 Nov → 6 Nov on `C-maryam-004` (`A-maryam-003`), script closed from the link; 66 s |
| 7 | `approve A-maryam-003` | `C-maryam-004` due 2026-11-06 (v1 kept) |
| 8 | `correct C-maryam-006 --due 2026-11-12 --reason "Hina confirmed by phone"` | poster v2 = 2026-11-12 |
| 9 | `seed-imap --until 2026-11-09T09:00:00+05:00` | APPEND of m10–m12 |
| 10 | `run-cycle --source imap --now 2026-11-09T09:00:00+05:00` | cycle `maryam-c003`: 3 fetched in 3979 ms; chapter closed (attachment `lit_review_v2.pdf`); Dr. Imran's claim held (`A-maryam-005`); extension 9 → 12 Nov proposed (`A-maryam-006`); nudge to Bilal drafted (`A-maryam-007`); 28 s |
| 11 | `approve A-maryam-006`, `approve A-maryam-007`, `reject A-maryam-005 --note "no comments received"` | Assignment 3 due 2026-11-12; nudge approved; claim rejected |
| 12 | `CK_SIMULATE_FAILURE=smtp_once send` | `A-maryam-007: FAILED (simulated SMTP failure: 421 ...) - state kept, safe to retry` |
| 13 | `send` | `A-maryam-007: SENT -> <test inbox>` (real Gmail SMTP, 2191 ms) |
| 14 | `send` | `nothing approved to send` |
| 15 | `seed-imap --until 2026-11-12T09:00:00+05:00` | APPEND of m13 (Bilal's automatic reply) |
| 16 | `run-cycle --source imap --now 2026-11-12T09:00:00+05:00` | cycle `maryam-c004`: 1 fetched in 4064 ms; **the real nudge already in the inbox was skipped as the agent's own message**; 46 s |

Plans per live cycle, compared with the offline reference run (`results/runs/main`):

| Cycle | Live plans | Same as reference |
|---|---|---|
| c002 | 001 = remind_me, 002 = wait, 004 = wait, 005 = wait (rule), 006 = ask_clarification (rule) | yes |
| c003 | 002 = confirm_completion (rule), 004 = draft_nudge, 005 = wait, 006 = wait (rule) | yes |
| c004 | 002 = draft_nudge (left unapproved, never sent), 004 = wait (auto-reply), 005 = remind_me, 006 = remind_me | yes |

Messages stored by cycles 2–4:

| Message-ID | IMAP ref | Cycle | Note |
|---|---|---|---|
| <m05.maryam@fixtures.commitkeeper> | INBOX:8 | c002 | |
| <m06.maryam@fixtures.commitkeeper> | INBOX:9 | c002 | `duplicate_of` m04 |
| <m07.maryam@fixtures.commitkeeper> | INBOX:10 | c002 | quarantined |
| <m08.maryam@fixtures.commitkeeper> | INBOX:11 | c002 | |
| <m09.maryam@fixtures.commitkeeper> | INBOX:12 | c002 | |
| <m10.maryam@fixtures.commitkeeper> | INBOX:13 | c003 | |
| <m11.maryam@fixtures.commitkeeper> | INBOX:14 | c003 | |
| <m12.maryam@fixtures.commitkeeper> | INBOX:15 | c003 | |
| <m13.maryam@fixtures.commitkeeper> | INBOX:17 | c004 | INBOX:16 is the real nudge, not stored |

### The real delivery

Action record `A-maryam-007` after the run (approval, delivery and failure are separate fields):

| Field | Value |
|---|---|
| `action_type` / `status` | draft_nudge / **sent** |
| `recipient` (as drafted) | bilal@example.com (allowlisted) |
| `delivered_to` | `<test inbox>` (route-to) |
| `idempotency_key` | `nudge:C-maryam-004:v2:n1` |
| `decided_at` (approval, review clock) | 2026-11-09T09:00:00+05:00 |
| `attempts` / `last_error` | 2 / none (attempt 1 failed by simulation, attempt 2 delivered) |
| `sent_at` (review clock) | 2026-11-09T09:00:00+05:00 |
| real delivery time (audit log) | 2026-11-10T18:14:43+00:00 |
| Message-ID | `<ck-eb19c97d3ee2ca4d6a8c@commitkeeper.local>` |

Audit log (`events`, kind `tool_call`, name `send_nudge`):

```
2026-11-10T18:14:35+00:00 error attempt 1  DeliveryError: simulated SMTP failure: 421 service not available (CK_SIMULATE_FAILURE)
2026-11-10T18:14:43+00:00 ok    attempt 2  {"status": "sent", "delivered_to": "<test inbox>"}   2191 ms
```

Own-message guard, first live test (cycle 4 fetch, `commitkeeper log --profile maryam --kind tool_call`):

```
2026-11-10T18:15:02+00:00 fetch_mail.imap ok 4064 ms
  {"new_messages": ["<m13.maryam@fixtures.commitkeeper>"],
   "skipped_own_nudges": ["<ck-eb19c97d3ee2ca4d6a8c@commitkeeper.local>"]}
```

Gmail kept the agent's Message-ID unchanged, so the Message-ID check alone caught it.

### Inbox check (read-only IMAP, after cycle 4)

| Folder | Messages | CommitKeeper nudges |
|---|---|---|
| INBOX | 14 (13 fixtures + 1 nudge) | **1**: UID 16, `X-CommitKeeper-Action: A-maryam-007`, subject `[test route for bilal@example.com] Follow-up: Share annotated dataset with Maryam` |
| [Gmail]/Sent Mail | 1 | 1 (the same message) |

Exactly one real email was sent across the failure, the retry and the extra `send`. The second "Follow-up" subject in the inbox is m13, the fictional automatic reply from the fixtures.

### Observations

- All live decisions matched the offline reference run, including the two decisions where the model's reading of the evidence overrides what policy would allow (cycle 2 `wait` on Bilal's delay, cycle 4 `wait` on the auto-reply).
- Wording slip in one rationale: in cycle 2 the model described the 6 Nov change as "approved by user" while it was still pending. The action (`wait`) and the stored state (pending version, approval required) were correct; the deterministic layer, not the rationale, controls the approval.
