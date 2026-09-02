# Known Issues

Tracked defects that were found, understood, and deliberately not fixed on
the spot — or fixed later once the risk was reassessed. Not a TODO list;
each entry states what's actually wrong, where, and what breaks if it stays
broken, per CLAUDE.md's evidence discipline (no bare assertions).

## Issue 1: FK violation mislogged as IdempotencyCollision

Found: Phase 14, 1 Sep 2026 (`experiments/thresholds/run_retry_cap.py`,
per BUILD_LOG.md's "One bug caught before it produced a wrong number, not
after" entry).

Location: [`src/db/repo.py:170`](src/db/repo.py#L170), inside
`insert_execution()`.

Description: `insert_execution()` wraps its `INSERT INTO execution` in a
single `except sqlite3.IntegrityError`, and unconditionally re-raises
whatever it catches as `IdempotencyCollision` — the label this codebase
uses everywhere else for "the UNIQUE(idempotency_key) constraint did its
job, this is the success path." But `sqlite3.IntegrityError` is also what
SQLite raises for a **foreign-key** violation (this DB runs with `PRAGMA
foreign_keys=ON`, confirmed in `src/db/migrate.py:21`), and `execution` has
two FK columns (`episode_id REFERENCES episode`, `run_id REFERENCES run`).
The clause cannot tell the two apart. Confirmed directly: Phase 14's
threshold-sweep harness logged `IdempotencyCollision` warnings on episodes
that had never been inserted before, traced to `execution.run_id`
referencing a `run` row that didn't exist because the harness never called
`start_run()` first — a genuine referential-integrity bug, reported as a
false-positive dedup success.

Status: not fixed, deferred. BUILD_LOG.md's Phase 14 entry states this
explicitly: fixed the calling harness instead of narrowing the except
clause, and named the clause's breadth as "itself worth narrowing, but out
of this phase's scope." Still true as of this entry — the clause is
unchanged.

Risk if unfixed: any future caller of `insert_execution()` that passes a
bad `episode_id` or `run_id` (a real bug — a typo, a stale reference, a
caller that skips `start_run()`/`insert_episode()`) gets told
`IdempotencyCollision` instead of a foreign-key error. The executor's own
handling of `IdempotencyCollision` treats it as expected control flow (see
`src/execute/executor.py`'s `_record_execution()` call sites) — a real
data-integrity bug would surface as a silently "successful" duplicate
suppression, not a crash. During a live demo run this could mask an actual
wiring mistake as the exact behavior the demo is trying to prove works
(duplicate-link avoidance), which is the worst place for it to hide.

## Issue 2: Unicode/box-drawing crash risk in src/audit/verify.py

Found: Phase 15, 2 Sep 2026 (this session), while verifying the terminal
demo surface (`src/ui/`) built in the prior session. That session already
found and fixed the same crash pattern in `src/ui/theme.py` (glyphs),
`src/ui/live.py` (box styles, bar characters), `src/ui/approve.py` (table
overflow), and `src/execute/executor.py` (an em-dash in a log message hit
directly by this session's real fault-injection run — see below); this
entry is `src/audit/verify.py`, which had not been touched.

Description: `verify_chain()`'s CLI (`make verify-audit`) printed an
em-dash (`—`) in three places (`chain intact — N records`, twice; `chain
BROKEN at seq N — expected ... got ...`) and an ellipsis (`…`) in
`_short()`'s hash truncation. Reproduced directly in this session, on this
project's own dev machine: `make verify-audit`'s equivalent
(`python -m src.audit.verify --all`) printed `chain intact <mangled> 15412
records` before the fix — the same `UnicodeEncodeError`-adjacent failure
mode `scripts/demo.py`'s mode banner already had a comment warning about,
just never applied here.

Status: **fixed**, this session. All four sites in `src/audit/verify.py`
now use plain ASCII (`-` for the em-dash, `...` for the ellipsis).
Re-verified after the fix: `python -m src.audit.verify --all` -> `chain
intact - 15454 records (0.91s)`, and `--tamper-test` -> `chain BROKEN at
seq 10 - expected sha256:7289... got sha256:7289...` — both clean, no
mangled characters. `CLAUDE.md`'s Commands section (the one place outside
this file that quoted the em-dash wording verbatim) updated to match; the
build blueprint's illustrative quotes of the same wording were left as
historical/planning text, out of scope for this fix.

**Recording-environment reasoning, per the task that raised this issue:**
this repository's own dev environment is Windows (win32), and this
session's actual verification work reproduced the legacy-console
`UnicodeEncodeError`-class crash twice, directly, against this project's
own code, on this machine (once against a Rich `Panel` using a
Unicode box style forced through `legacy_windows_render`, once against a
plain `Console().print()` of an em-dash going through the same code path).
That is not a hypothetical "there's some chance" risk — it is a confirmed,
reproduced failure mode on the actual class of machine this project is
being built on. Nothing in this repository states or implies the demo
video will be recorded on a different, safer terminal (modern Windows
Terminal, macOS, or Linux) — no such claim exists to rely on. Given the
task's own standard ("if there is ANY realistic chance it's a legacy
Windows console... fix it now — it's a P0 risk given `make verify-audit`
is called on camera at video beat 2:42"), and given the risk is not merely
plausible but already reproduced, the only defensible action was to fix it
now, not defer it. Fixed above.

## Issue 3: FK violation mislogged as DuplicateEventError, in a second function

Found: Phase 16, 2 Sep 2026, while smoke-testing the web dashboard
(`src/webui/`) built this session — a test call to `insert_episode()` with
a `customer_id` that hadn't been inserted yet failed in a way that looked,
at first, like a stale/duplicate test fixture rather than a real bug.

Location: [`src/db/repo.py:102`](src/db/repo.py#L102), inside
`insert_episode()`. Same defect class as Issue 1
([`src/db/repo.py:170`](src/db/repo.py#L170), `insert_execution()`), a
different function.

Description: `insert_episode()` wraps its `INSERT INTO episode` in
`except sqlite3.IntegrityError`, and unconditionally re-raises whatever it
catches as `DuplicateEventError` (`code=DUPLICATE_PAYMENT_ID`, message
"episode with payment_id=... already exists", remediation "expected
control flow for a replayed webhook — not a bug"). But `episode` has a
foreign key the clause doesn't distinguish from the two constraints that
label actually applies to: `episode_id TEXT PRIMARY KEY` and
`payment_id TEXT NOT NULL UNIQUE` are the genuine "this is a replay"
cases, but `customer_id TEXT REFERENCES customer(customer_id)` (schema.sql)
is not — a missing customer row raises the identical
`sqlite3.IntegrityError`, with no field in the exception to tell it apart
from a real duplicate.

Reproduced fresh, on a brand-new database, for this entry:
```
DuplicateEventError: [DUPLICATE_PAYMENT_ID] episode with payment_id='pay_repro'
already exists - expected control flow for a replayed webhook - not a bug
```
`pay_repro` had never been inserted — the only thing wrong was that
`cust_does_not_exist` wasn't a real row in `customer` yet. Inserting the
customer row first and re-running the identical `insert_episode()` call
succeeded.

**This is now confirmed in two places, not one — worth treating as a
pattern, not two unrelated bugs.** A grep of `src/db/repo.py` for
`except sqlite3.IntegrityError` finds exactly three call sites: this one,
Issue 1's `insert_execution()`, and `insert_webhook_event()` (line 218).
Checked each against its own table's schema rather than assuming: the
first two both have a `REFERENCES` column that the same catch-all clause
would mislabel (`episode.customer_id`, `execution.episode_id` /
`execution.run_id`); `webhook_event` has no foreign key at all, so its
occurrence of the same *code shape* is not currently exploitable the same
way — a useful data point, since it means the risk tracks table shape, not
just "this file's style," and a future audit should check schema
references at each site rather than pattern-matching the except clause
alone. The underlying habit — catch `sqlite3.IntegrityError` broadly and
assume it always means the one constraint a function's docstring cares
about — is what should get a deliberate, whole-codebase grep/audit later
(not scoped to `src/db/repo.py` alone; the same shape could exist anywhere
else that does its own `INSERT` inside a bare `try/except IntegrityError`),
rather than patching each instance as it happens to surface.

Status: not fixed, deferred. Audited every current caller of
`insert_episode()` (grepped the whole repo, not just `src/`) to check
whether the actual exploit condition — a `customer_id` that isn't in
`customer` yet — is reachable anywhere real, rather than assuming:

- `data/generator.py`: `customers = _generate_customers(...)` runs once,
  before any episode is built, and every episode-building function
  (`_generate_train_edge_cases`, `_generate_train_regular`,
  `_generate_sealed_forced`, `_generate_sealed_regular`) draws its
  customer from that same in-memory list (`rng.choice`/`rng.sample`/
  indexing — never a fresh/independent id). `data/train.jsonl`,
  `holdout/sealed.jsonl`, and `data/customers.jsonl` are generated from one
  consistent pass, so the generator cannot produce a mismatch by
  construction.
- `src/ingest/service.py:227` (the real, live webhook path — the one place
  outside the synthetic generator that inserts episodes): always passes
  `customer_id=None`. A NULL foreign-key value is exempt from SQLite's FK
  check entirely (standard SQL semantics), so this path was never at risk
  regardless of what triggered it.
- `experiments/thresholds/_common.py` (used by `run_auto_approve.py` /
  `run_outage_cluster.py`): calls `load_and_upsert_customers()` before any
  `insert_episode()`, episodes sourced from `data/train.jsonl` (see above).
  `experiments/thresholds/run_retry_cap.py` (the file that actually
  triggered Issue 1): passes `customer_id=None` explicitly, with its own
  comment already naming the FK-mislabeling risk for `run_id`/`episode_id`
  — not exploitable via `customer_id` either.
- `scripts/eval.py`: both `run_second_rail()` and `run_baseline()` call
  `load_and_upsert_customers()` before touching `insert_episode()`.
- `src/runner.py`'s `_ensure_episode_row()` (the real pipeline entry
  point): both the CLI (`src/runner.py`'s own `main()`) and
  `scripts/demo.py` call `load_and_upsert_customers()` before
  `Runner.run()` starts — confirmed by reading the call order directly,
  not inferred.

Checked for actual impact, not just reachability: no committed evidence
artifact (`evidence/*.md`, `evidence/*.json`, `evidence/exceptions_sample.md`)
contains `DUPLICATE_PAYMENT_ID` or an "already exists" episode error —
`evidence/exceptions_sample.md`'s only recorded reason codes are
`duplicate_episode_this_run` (a real, gate-level duplicate — unrelated to
this DB-insert-level bug) and `episode_age_exceeds_cap`. **This confirms
Issue 3 is a real defect in the code (the except clause is still wrong)
but a purely theoretical one in practice: no current code path can trigger
it, and it has not fired in any committed run, so nothing in
`evidence/report.md` or any other evidence artifact is affected by it.**
Narrowing the clause is still out of scope for this entry — recorded so
the reasoning doesn't have to be re-derived if a future caller changes
this.

Risk if unfixed: identical in shape to Issue 1 — any future caller of
`insert_episode()` that passes a `customer_id` not yet on file (a real
bug: a race, a missing upsert step, a caller that skips
`load_and_upsert_customers()`) is told "this payment was already
processed" instead of "this customer doesn't exist," and
`DuplicateEventError`'s own remediation text ("not a bug") actively
discourages investigating it. A judge or an operator debugging a
suppressed/skipped episode during a live run would be pointed at the wrong
explanation.
