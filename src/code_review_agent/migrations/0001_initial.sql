CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_specs (
    spec_id TEXT PRIMARY KEY,
    fixed_conditions_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    control_state TEXT NOT NULL CHECK (control_state IN ('ready','running','paused','terminated')),
    phase TEXT NOT NULL CHECK (phase IN ('created','input_acquired','input_normalized','planned','reviewing','consolidating','result_finalized')),
    result_state TEXT NOT NULL CHECK (result_state IN ('pending','no_changes','complete_no_findings','complete_with_findings','partial','failed','unknown')),
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('not_ready','pending','succeeded','failed','unknown')),
    version INTEGER NOT NULL CHECK (version > 0),
    input_binding_id TEXT,
    latest_checkpoint_id TEXT,
    latest_result_snapshot_id TEXT,
    latest_delivery_attempt_id TEXT,
    pause_reason TEXT,
    terminal_reason TEXT,
    created_at TEXT,
    updated_at TEXT,
    retention_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS task_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    purged INTEGER NOT NULL DEFAULT 0 CHECK (purged IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_command_receipts (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    command TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    purpose TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_revision INTEGER NOT NULL CHECK (lease_revision > 0),
    released_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_lease
ON task_leases(task_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS execution_sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_requests (
    stop_request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    stop_revision INTEGER NOT NULL CHECK (stop_revision > 0),
    requested_action TEXT NOT NULL CHECK (requested_action IN ('pause','terminate')),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, stop_revision)
);

CREATE TABLE IF NOT EXISTS input_bindings (
    binding_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    input_type TEXT NOT NULL,
    object_identity TEXT,
    base_sha TEXT,
    head_sha TEXT,
    content_digest TEXT NOT NULL,
    completeness_digest TEXT,
    changeset_ref TEXT,
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_input_binding_per_task ON input_bindings(task_id);

CREATE TABLE IF NOT EXISTS input_acquisitions (
    acquisition_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    state TEXT NOT NULL,
    content_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS input_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    security_attestation_id TEXT,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS input_completeness_proofs (
    proof_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    content_digest TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_sets (
    changeset_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_files (
    change_file_id TEXT PRIMARY KEY,
    changeset_id TEXT NOT NULL REFERENCES change_sets(changeset_id),
    path_digest TEXT NOT NULL,
    change_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_hunks (
    hunk_id TEXT PRIMARY KEY,
    change_file_id TEXT NOT NULL REFERENCES change_files(change_file_id),
    content_digest TEXT NOT NULL,
    old_start INTEGER,
    new_start INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    predecessor_id TEXT,
    budget_ledger_version INTEGER NOT NULL CHECK (budget_ledger_version >= 0),
    trace_start_sequence INTEGER NOT NULL,
    trace_end_sequence INTEGER NOT NULL,
    created_task_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sequence)
);

CREATE TABLE IF NOT EXISTS work_item_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    work_unit_id TEXT NOT NULL,
    state TEXT NOT NULL,
    checkpoint_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
    result_state TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, snapshot_version)
);
CREATE TABLE IF NOT EXISTS cleanup_attempts (
    cleanup_attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_tombstones (
    task_id TEXT PRIMARY KEY,
    final_state TEXT NOT NULL,
    cleanup_state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_accounts (
    account_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    unit TEXT NOT NULL CHECK (unit = 'token'),
    state TEXT NOT NULL CHECK (state IN ('open','frozen_overage','closed')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
    provider_capability_baseline_ref TEXT NOT NULL,
    active_freeze_id TEXT,
    pending_revalidation INTEGER NOT NULL DEFAULT 0 CHECK (pending_revalidation IN (0,1))
);
CREATE TABLE IF NOT EXISTS budget_authorizations (
    authorization_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
    model_call_id TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL CHECK (amount > 0),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
    sequence INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    reservation_amount INTEGER NOT NULL DEFAULT 0 CHECK (reservation_amount >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(account_id, sequence)
);
CREATE TABLE IF NOT EXISTS budget_freezes (
    freeze_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
    state TEXT NOT NULL,
    overage INTEGER NOT NULL CHECK (overage >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_budget_capabilities (
    capability_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES budget_accounts(account_id),
    capability_digest TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK (valid IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL CHECK (event_version > 0),
    category TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    schema_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sequence),
    UNIQUE(task_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS trace_edges (
    edge_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_event_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE(task_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS comment_trace_links (
    trace_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    finding_id TEXT NOT NULL,
    link_version INTEGER NOT NULL CHECK (link_version > 0),
    direct_evidence_json TEXT NOT NULL,
    created_event_id TEXT NOT NULL,
    UNIQUE(finding_id, link_version)
);
CREATE TABLE IF NOT EXISTS trace_intents (
    intent_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    idempotency_key TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS trace_integrity_checks (
    check_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    valid INTEGER NOT NULL CHECK (valid IN (0,1)),
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_policies (
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(policy_id, policy_version)
);
CREATE TABLE IF NOT EXISTS security_attestations (
    attestation_id TEXT PRIMARY KEY,
    task_id TEXT,
    artifact_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    decision TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    sanitized_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sanitized_artifacts (
    artifact_id TEXT PRIMARY KEY,
    attestation_id TEXT NOT NULL UNIQUE REFERENCES security_attestations(attestation_id),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_provenance (
    artifact_id TEXT NOT NULL REFERENCES sanitized_artifacts(artifact_id),
    source_attestation_id TEXT NOT NULL,
    PRIMARY KEY(artifact_id, source_attestation_id)
);
CREATE TABLE IF NOT EXISTS security_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT,
    event_code TEXT NOT NULL,
    artifact_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_compatibility (
    compatibility_id TEXT PRIMARY KEY,
    source_policy_digest TEXT NOT NULL,
    target_policy_digest TEXT NOT NULL,
    compatible INTEGER NOT NULL CHECK (compatible IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    delivery_attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    snapshot_id TEXT NOT NULL,
    delivery_key TEXT NOT NULL UNIQUE,
    expected_content_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS report_artifacts (
    report_artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    snapshot_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_target_locks (
    normalized_target TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
