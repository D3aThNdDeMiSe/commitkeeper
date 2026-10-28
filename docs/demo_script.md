# Live demonstration script (8-10 minutes)

Preparation (before the viva): start Ollama, run `ollama run qwen3:8b ""` once to load the model, delete `data/commitkeeper.db`, and open the terminal in the repository root. Use the IDs your run prints under **Needs you**; the ones below are from the reference run in `results/runs/main/transcript.txt`.

| Time | What to show | Command / what to point at |
|---|---|---|
| 0:00-1:00 | **Problem and design.** Promises are scattered across emails; the agent tracks both directions, remembers, and asks before acting. Show the flowchart in the README. | `README.md` |
| 1:00-2:30 | **Onboarding + initial action (cycle 1).** 4 emails become 5 commitments. Point out the directions, that "by Thursday" was resolved by code from the email date, the evidence quotes, and the reminder for the script due today. | `profile add maryam ...` then `run-cycle --profile maryam --now 2026-11-02T09:00:00+05:00` |
| 2:30-4:30 | **Later update (cycle 2).** Forward skipped as a duplicate; injection email QUARANTINED; script closed by the link; Bilal's "need until Friday" attached to the *existing* commitment as a pending change; the LLM decided **wait** (read the reason). Poster deadline is vague, so it asks instead of guessing. | `run-cycle --now 2026-11-04T09:00:00+05:00`; `log --profile maryam --kind security` |
| 4:30-5:30 | **Correction.** Approve Bilal's new date; answer the poster clarification. Show both versions in history. | `approve A-... --profile maryam`; `correct C-... --due 2026-11-12 --reason "..."`; `history C-... --profile maryam` |
| 5:30-7:00 | **Restart.** Point out that each command is a new process. Cycle 3 reads only 3 new emails, closes the chapter from the attachment, holds Dr. Imran's "already sent" claim for confirmation, and drafts ONE nudge to Bilal measured against the approved 6 Nov. | `run-cycle --now 2026-11-09T09:00:00+05:00` |
| 7:00-8:00 | **Failure and retry.** Approve the nudge, simulate an SMTP outage, retry, retry again (no duplicate). | `approve A-...`; `CK_SIMULATE_FAILURE=smtp_once python -m commitkeeper send --profile maryam`; `send` twice |
| 8:00-9:00 | **Memory pays off (cycle 4).** It remembers one nudge was sent, reads Bilal's auto-reply and waits; it reminds about Assignment 3 on the approved 12 Nov rather than reporting it overdue. | `run-cycle --now 2026-11-12T09:00:00+05:00` |
| 9:00-10:00 | **Inspect records.** Excel register (yellow manual columns survive re-export), audit log, isolation (`approve` on Maryam's action with `--profile omar` is refused). | `export --profile maryam`; open `exports/maryam_register.xlsx`; `log --profile maryam --tail 15`; `approve A-maryam-001 --profile omar` |

Backup: if Ollama is unavailable, show `results/runs/main/transcript.txt` (the recorded run) and open `results/runs/main/commitkeeper.db` in any SQLite viewer.

## Viva questions to be ready for

- *Which store is authoritative?* SQLite. Excel/ICS are regenerated views; only the two manual columns are read back, into `manual_reviews`.
- *Where exactly does the LLM decide something?* `agent.py::_process_message` (extraction) and `agent.py::_plan` (next action). Everything the LLM returns is validated in `_apply_operation` and `safety.apply_action_policy`.
- *Why not let the LLM compute dates?* Small models get relative dates wrong. `dates.resolve_due` is deterministic and unit-tested, and anything vague goes to the user.
- *How are duplicates prevented?* Message-ID primary key; content hash for forwards; the `idempotency_key` UNIQUE constraint on actions; delivery only for `approved`/`failed` actions, recorded as `sent` once done.
- *What happens if the process dies mid-cycle?* The cycle row keeps its `stage`; each email has a `processed` flag. The next `run-cycle` resumes the same cycle (scenario S11).
- *How do you stop prompt injection?* Detector plus LLM flag leads to quarantine. More importantly, no code path lets email content change recipients, the allowlist, preferences or another commitment's status, and every operation must target a participant of that email.
