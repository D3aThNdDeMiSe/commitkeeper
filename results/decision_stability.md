# Next-action decision stability (seed sweep)

The six key next-action decisions from the storyline were rebuilt from database snapshots and sent to the model three times with seeds 42, 7 and 1234 (qwen3:8b, temperature 0, reasoning on). This checks that the decisions do not depend on one lucky seed.

| Cycle | Commitment | Situation | Expected | Seeds 42 / 7 / 1234 |
|---|---|---|---|---|
| c2 | C-maryam-004 dataset | overdue 1 day; Bilal asked for Friday; change pending | wait | wait / wait / wait |
| c3 | C-maryam-004 dataset | approved 6 Nov has passed; no new message | draft_nudge | draft_nudge x3 |
| c4 | C-maryam-004 dataset | 1 nudge sent; auto-reply "away until 14 November" | wait | wait x3 |
| c4 | C-maryam-002 comments | completion claim rejected by user; overdue 8 days | draft_nudge | draft_nudge x3 |
| c4 | C-maryam-006 poster | user corrected the date to today | remind_me | remind_me x3 |
| c4 | C-maryam-005 Assignment 3 | approved extension makes it due today | remind_me | remind_me x3 |

**18/18 expected actions.** Before the "sent before the current due date was set" marker was added, the cycle-4 poster decision was observed as `wait` in one full run, because the model treated the superseded "sometime next week" email as a newer date. A run of the comparison harness with identical inputs chose `remind_me`. This shows some run-to-run variation on GPU even at temperature 0, which is why the marker is computed by code.
