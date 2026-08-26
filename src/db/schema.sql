-- Second Rail persistence schema.
--
-- SQLite via stdlib sqlite3, no ORM. Every table is CREATE TABLE IF NOT
-- EXISTS so migrate() is idempotent. Money is always INTEGER paise — no
-- float anywhere in this schema, no column named "amount" without an
-- "_paise" suffix. Timestamps are TEXT holding ISO-8601 with a +05:30
-- offset. PRAGMA journal_mode=WAL and PRAGMA foreign_keys=ON are set at
-- connection time in migrate.py, not here.

-- No real PII, ever (DPDP Act 2023). contact_hash and email_hash are
-- sha256 hex digests of the customer's phone/email. A raw phone number or
-- email address must never be stored in this table, or anywhere else in
-- this database.
CREATE TABLE IF NOT EXISTS customer (
    customer_id TEXT PRIMARY KEY,
    synthetic_name TEXT,
    contact_hash TEXT NOT NULL,
    email_hash TEXT,
    segment TEXT CHECK(segment IN ('first_time','repeat','high_value')),
    opted_out INTEGER NOT NULL DEFAULT 0,
    opt_out_ts TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customer_optout ON customer(opted_out);


CREATE TABLE IF NOT EXISTS episode (
    episode_id TEXT PRIMARY KEY,
    -- UNIQUE(payment_id) is the webhook dedup boundary: a replayed
    -- payment.failed webhook for a payment_id already seen must not create
    -- a second episode. C-02.
    payment_id TEXT NOT NULL UNIQUE,
    order_id TEXT,
    customer_id TEXT REFERENCES customer(customer_id),
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    instrument TEXT CHECK(instrument IN ('upi','card','netbanking','wallet')),
    issuer_family TEXT,
    error_code TEXT,
    error_description TEXT,
    error_source TEXT,
    error_step TEXT,
    error_reason TEXT,
    failed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    split TEXT CHECK(split IN ('train','sealed')),
    is_synthetic INTEGER NOT NULL DEFAULT 1,
    harvested_from TEXT
);

CREATE INDEX IF NOT EXISTS idx_episode_customer_time ON episode(customer_id, failed_at); -- frequency cap
CREATE INDEX IF NOT EXISTS idx_episode_split ON episode(split);
CREATE INDEX IF NOT EXISTS idx_episode_issuer_code ON episode(issuer_family, error_code);


-- The non-circular evidence anchor (M-08): real error strings harvested
-- from forced Razorpay test-mode failures, also committed verbatim as
-- evidence/harvested_errors.jsonl. This table is a queryable mirror of
-- that file, not a second source of truth.
CREATE TABLE IF NOT EXISTS harvested_error (
    harvest_id TEXT PRIMARY KEY,
    payment_id TEXT,
    error_code TEXT,
    error_description TEXT,
    error_source TEXT,
    error_step TEXT,
    error_reason TEXT,
    instrument TEXT,
    captured_at TEXT,
    forced_by TEXT,
    assigned_class TEXT,
    doc_reference TEXT
);


CREATE TABLE IF NOT EXISTS taxonomy_class (
    class_id TEXT PRIMARY KEY,
    label TEXT,
    definition TEXT,
    anchor_error_strings TEXT,
    recoverable_in_principle INTEGER,
    source TEXT CHECK(source IN ('harvested','doc','inferred'))
);


CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    mode TEXT CHECK(mode IN ('dry_run','execute','fixture')),
    episode_count INTEGER,
    git_sha TEXT,
    config_hash TEXT,
    stopped_reason TEXT,
    llm_cost_paise INTEGER,
    throughput_epm REAL
);


CREATE TABLE IF NOT EXISTS gate_check (
    check_id TEXT PRIMARY KEY,
    episode_id TEXT REFERENCES episode(episode_id),
    check_name TEXT,
    result TEXT CHECK(result IN ('pass','fail')),
    reason TEXT,
    evaluated_at TEXT,
    order_index INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gate_episode_order ON gate_check(episode_id, order_index);


CREATE TABLE IF NOT EXISTS diagnosis (
    diagnosis_id TEXT PRIMARY KEY,
    episode_id TEXT UNIQUE REFERENCES episode(episode_id),
    method TEXT CHECK(method IN ('regex','llm','regex_then_llm')),
    class_id TEXT,
    confidence REAL,
    rationale TEXT,
    llm_model TEXT,
    prompt_hash TEXT,
    cache_hit INTEGER,
    latency_ms INTEGER,
    cost_paise INTEGER,
    llm_degraded INTEGER NOT NULL DEFAULT 0
);


CREATE TABLE IF NOT EXISTS policy_rule (
    policy_rule_id TEXT PRIMARY KEY,
    cause_class TEXT,
    amount_band TEXT,
    segment TEXT,
    instrument TEXT,
    admissible_actions TEXT,
    escalation_tier TEXT CHECK(escalation_tier IN ('auto','human_keystroke','hard_refuse')),
    justification TEXT,
    -- This UNIQUE constraint is what guarantees the policy engine is
    -- deterministic: at most one rule can ever match a given
    -- (cause_class, amount_band, segment, instrument) combination.
    UNIQUE(cause_class, amount_band, segment, instrument)
);


CREATE TABLE IF NOT EXISTS decision (
    decision_id TEXT PRIMARY KEY,
    episode_id TEXT UNIQUE REFERENCES episode(episode_id),
    policy_rule_id TEXT REFERENCES policy_rule(policy_rule_id),
    candidate_actions TEXT,
    chosen_action TEXT,
    features_used TEXT,
    inside_admissible_set INTEGER NOT NULL,
    escalation_tier TEXT,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_admissible ON decision(inside_admissible_set);


CREATE TABLE IF NOT EXISTS approval (
    approval_id TEXT PRIMARY KEY,
    episode_id TEXT REFERENCES episode(episode_id),
    required INTEGER,
    tier TEXT,
    approved_by TEXT,
    approved_at TEXT,
    rejected_reason TEXT,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_pending ON approval(required, approved_at); -- the pending queue


CREATE TABLE IF NOT EXISTS execution (
    execution_id TEXT PRIMARY KEY,
    episode_id TEXT REFERENCES episode(episode_id),
    -- UNIQUE(idempotency_key) is the single most load-bearing constraint in
    -- this schema. idempotency_key = sha256(payment_id + ':' + policy_rule_id)[:32],
    -- never the webhook event id. This is what makes re-running an episode
    -- safe and proves no duplicate Payment Link can ever be created for the
    -- same (payment_id, policy_rule_id) pair. E-01, E-02, E-03.
    idempotency_key TEXT NOT NULL UNIQUE,
    reference_id TEXT,
    api TEXT,
    plink_id TEXT,
    short_url TEXT,
    request_body_hash TEXT,
    response_code INTEGER,
    attempt INTEGER,
    delay_ms INTEGER,
    status TEXT CHECK(status IN ('created','duplicate_suppressed','failed','cancelled')),
    run_id TEXT REFERENCES run(run_id),
    created_at TEXT,
    cancelled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_run_status ON execution(run_id, status);


CREATE TABLE IF NOT EXISTS webhook_event (
    event_id TEXT PRIMARY KEY,
    event_type TEXT,
    payment_id TEXT,
    plink_id TEXT,
    raw_body_hash TEXT,
    signature_valid INTEGER,
    received_at TEXT,
    processed INTEGER,
    dedup_result TEXT CHECK(dedup_result IN ('new','duplicate','out_of_order'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_payment_time ON webhook_event(payment_id, received_at); -- out-of-order detection


CREATE TABLE IF NOT EXISTS attribution (
    attribution_id TEXT PRIMARY KEY,
    episode_id TEXT REFERENCES episode(episode_id),
    execution_id TEXT REFERENCES execution(execution_id),
    outcome TEXT CHECK(outcome IN ('recovered','not_recovered','pending','suppressed','execution_failed')),
    recovered_amount_paise INTEGER,
    window_hours INTEGER,
    attributed_at TEXT,
    attribution_rule_id TEXT
);


CREATE TABLE IF NOT EXISTS ledger_entry (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES run(run_id),
    episode_id TEXT REFERENCES episode(episode_id),
    kind TEXT CHECK(kind IN ('gross_recovery','fp_cost','net')),
    amount_paise INTEGER NOT NULL,
    basis TEXT
);

CREATE INDEX IF NOT EXISTS idx_ledger_run_kind ON ledger_entry(run_id, kind);


CREATE TABLE IF NOT EXISTS exception_entry (
    exception_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES run(run_id),
    episode_id TEXT REFERENCES episode(episode_id),
    stage TEXT,
    reason_code TEXT,
    reason_text TEXT,
    excluded_from_recovery INTEGER NOT NULL DEFAULT 1
);


-- This table is a queryable INDEX over the append-only, hash-chained
-- evidence/audit/*.jsonl file — not the source of truth. `make verify-audit`
-- walks the JSONL chain itself; this table exists so a query can find a
-- record fast without re-walking the whole file.
CREATE TABLE IF NOT EXISTS audit_record (
    event_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES run(run_id),
    episode_id TEXT REFERENCES episode(episode_id),
    actor TEXT CHECK(actor IN ('agent','human','system')),
    stage TEXT CHECK(stage IN ('gate','diagnose','choose','approve','execute','attribute','rollback','stop')),
    inputs_hash TEXT,
    prev_hash TEXT,
    hash TEXT,
    seq INTEGER,
    UNIQUE(run_id, seq)
);
