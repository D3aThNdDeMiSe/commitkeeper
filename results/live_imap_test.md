# Live Gmail (IMAP) integration test

Owned test Gmail account created for this project (address and App Password kept in the git-ignored `.env`; not reproduced here). Run against the real Gmail servers on 2026-10-29.

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
