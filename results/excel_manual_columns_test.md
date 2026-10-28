# Excel review view: manual columns preserved, store stays authoritative

Steps, run on a copy of `results/runs/main/commitkeeper.db`:

1. `python -m commitkeeper export --profile maryam` produced 6 rows, 0 manual entries.
2. Opened `maryam_register.xlsx` and edited row C-maryam-001: *Your decision* = "Agree - follow up in person", *Your comments* = "Imran said comments come Friday", and overwrote the generated *Status* cell with "HAND-EDITED STATUS (should be overwritten)".
3. `python -m commitkeeper export --profile maryam` again reported 1 manual entry kept.

Result after re-export:

| Field | Value |
|---|---|
| Status (generated) | `done`, regenerated from SQLite; the hand edit is not trusted |
| Your decision (manual) | `Agree - follow up in person`, preserved |
| Your comments (manual) | `Imran said comments come Friday`, preserved |
| `manual_reviews` table | `('C-maryam-001', 'Agree - follow up in person', 'Imran said comments come Friday')` |

The exported calendar (`maryam_deadlines.ics`) uses `UID:<commitment>@commitkeeper` and `SEQUENCE:<version>`. For example, the dataset event carries SEQUENCE 2 after the approved deadline change, so calendar apps update the existing event instead of adding a duplicate.
