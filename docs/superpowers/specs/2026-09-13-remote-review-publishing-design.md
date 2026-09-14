# Remote PR/MR Review Publishing Design

## 1. Goal

Extend CR-Agent so an operator can explicitly publish a completed review to the
GitHub.com pull request or GitLab.com merge request from which the immutable
input was acquired. A publication contains line-level comments for findings
that the platform can position and one summary comment for the review as a
whole. Local Markdown delivery remains the default and remote writes never
happen without an explicit publish request.

## 2. Scope

The first remote-publishing release supports:

- GitHub.com pull requests and GitLab.com merge requests already accepted by
  the input providers;
- publishing immediately after `review --url ... --publish`;
- publishing or resuming an existing task with
  `publish <task-id> --confirm`;
- line comments on changed lines, including additions and deletions;
- one review summary comment per immutable result snapshot;
- zero-finding, partially completed, and failed review summaries;
- per-comment persistence, remote reconciliation, safe retry, and partial
  success reporting;
- the existing shared credential alias for each platform.

The release does not support GitHub Enterprise, self-hosted GitLab, draft
reviews that require a later submit action, automatic approval/request-changes
decisions, editing or deleting existing remote comments, or publishing reviews
created from local diff input.

## 3. Operator Contract

`review --url <PR-or-MR> --publish` runs the existing review and local Markdown
delivery first. If a fixed result snapshot exists, it then invokes remote
publication. The command's structured and human-readable result includes a
separate remote publication state and counts for published, skipped, failed,
and unknown items. Local report delivery state keeps its existing meaning.

`publish <task-id> --confirm` publishes the latest persisted immutable result
snapshot for an existing remote-input task. The confirmation flag is mandatory
because this command has an external write side effect. Re-running the command
reconciles stable markers against the platform before issuing any write.

The following requests fail before a remote write:

- a task created from stdin or a diff file;
- a task without a persisted result snapshot;
- a missing or invalid platform credential;
- a closed or merged PR/MR;
- a current platform head SHA different from the head SHA bound to the review;
- an unavailable security scan or content rejected by the scan.

## 4. Architecture

Remote publication is a separate application use case. It does not extend the
local `OutputAdapterPort`, whose delivery semantics remain limited to local,
side-effect-free artifacts. It also does not add write behavior to the input
providers.

The feature introduces:

- a `RemotePublisherPort` for platform metadata verification, existing-marker
  discovery, line-comment creation, and summary-comment creation;
- GitHub and GitLab publisher adapters with fixed origins, fixed API routes,
  redirects disabled, environment proxies disabled, bounded responses, and no
  automatic HTTP retries;
- a publication planner that deterministically maps a persisted snapshot into
  ordered publication items;
- a publication service that persists intent before I/O, reconciles remote
  state, performs one network attempt at a time, and records the outcome;
- publication repositories and SQLite migration additions that are independent
  of local report delivery attempts;
- CLI composition that chooses a publisher from the task's normalized source
  URL and obtains credentials only inside the trusted adapter boundary.

## 5. Publication Model

A `Publication` binds exactly one task, result snapshot, normalized source
target, platform, repository/project, change number, base SHA, and head SHA.
It has a stable `publication_id` derived from those values.

A `PublicationItem` has:

- a stable `publication_key`;
- kind `line` or `summary`;
- optional finding ID and platform position;
- the exact body digest;
- state `pending`, `succeeded`, `failed`, or `unknown`;
- remote comment identifier and URL when known;
- stable error code and timestamps.

Line item keys derive from platform, normalized target, snapshot ID, finding
fingerprint, exact position, and body digest. The summary key derives from the
same target and snapshot plus the summary digest. A changed body therefore
creates a new identity rather than silently mutating an old published comment.

Overall publication state is:

- `succeeded` when every planned item is confirmed remotely;
- `partial` when at least one item succeeded and at least one item failed;
- `failed` when no item succeeded and all attempted items have known failures;
- `unknown` when any item has an unresolved outcome;
- `pending` before all work reaches a terminal outcome.

## 6. Rendering

Every published body passes through the report-delivery security boundary
before it is persisted as a publishable intent or sent to a platform. Bodies
are deterministic UTF-8 Markdown and end with an HTML marker:

`<!-- cr-agent:publication:<publication-key> -->`

Line comments contain the title, confidence, problem, trigger, impact,
suggestion, trace ID, and limitations for one finding. The visible body does not
expose local filesystem paths, credentials, raw prompts, or raw model replies.

The summary contains task and snapshot identity, completion and coverage,
budget summary, limitations, and a compact list of all findings. Findings that
cannot be represented as line comments are fully described in the summary.
Line-published findings remain listed compactly so the summary is a complete
index without duplicating their full bodies.

## 7. Position Mapping

Only findings whose location kind is `new_line` or `old_line` become line
items. The file path and line number must still exist in the immutable parsed
change set bound to the snapshot.

For GitHub:

- `new_line` maps to `path`, `line`, `side=RIGHT`, and the reviewed head commit;
- `old_line` maps to `path`, `line`, `side=LEFT`, and the reviewed head commit.

For GitLab, a discussion position includes `position_type=text`, the reviewed
base/start/head SHAs, old and new paths, plus `new_line` or `old_line`. The
immutable input binding must retain the start SHA required by GitLab. When the
existing input shape lacks a distinct start SHA, it uses the MR diff refs'
`start_sha`; GitHub uses base SHA for this field because its API does not
require a start SHA.

File-level and multi-file findings are summary-only. A line that is no longer
accepted by the remote API is a known per-item failure; it is not silently
converted into a second full summary entry because the standard summary already
contains the finding.

## 8. Platform Operations

The GitHub adapter:

1. reads current PR metadata and verifies open state and head SHA;
2. lists issue comments and pull-request review comments to find CR-Agent
   markers, following only validated GitHub API pagination links;
3. creates missing line comments through the pull-request comments endpoint;
4. creates the missing summary through the issue comments endpoint.

The GitLab adapter:

1. reads current MR metadata and verifies opened state and head SHA;
2. lists MR notes and discussions to find CR-Agent markers, following only
   validated GitLab.com pagination links;
3. creates missing line discussions;
4. creates the missing summary note.

Both adapters accept only `https` URLs on their fixed public origin, reject
redirects, cap response sizes, map 401/403 to credential/permission failures,
map invalid positions and other 4xx responses to stable known failures, and
classify transport errors or 5xx responses after a write may have been sent as
unknown outcomes.

## 9. Idempotency and Recovery

Before each write, the service atomically stores a pending item containing the
exact target, body digest, and publication key. It then refreshes remote
markers. A matching marker marks the item succeeded without another write.

After a confirmed 2xx create response, the service persists the returned remote
identifier and URL. A definitive pre-send validation or 4xx error marks the
item failed. A transport error, malformed success response, or 5xx response
marks it unknown because the platform might have accepted the write.

On a later explicit `publish --confirm`, unknown and pending items are first
reconciled by listing remote comments. A found marker changes the item to
succeeded. If no marker is found, the service may retry only after a complete,
successful remote listing proves absence. Failed items may be retried by the
same explicit command after metadata and permissions are revalidated.

Items are processed line comments first and the summary last. Partial success
is preserved; no compensating deletes occur. Concurrent invocations use a
database lease/fencing token so only one process can publish a task snapshot at
a time.

## 10. Persistence

SQLite adds tables for publications, publication items, and publication leases.
Constraints enforce one publication identity per task/snapshot/target and one
item per stable publication key. State transitions use compare-and-swap version
checks. Persisted command data continues to supply the source URL; the immutable
input checkpoint/snapshot supplies the reviewed SHAs and parsed paths.

Cleanup removes detailed publication rows with the task's other detailed
runtime records but does not remove remote comments. The retained tombstone
states that remote side effects may remain.

Trace events record publication planning, target verification, reconciliation,
each attempt state, remote identifiers, and the final aggregate state. They do
not record credentials or unscanned comment bodies.

## 11. Error Semantics

Stable errors include:

- `publication_not_remote_input`;
- `publication_snapshot_not_ready`;
- `publication_target_changed`;
- `publication_target_closed`;
- `publication_credential_missing`;
- `publication_permission_denied`;
- `publication_position_invalid`;
- `publication_content_rejected`;
- `publication_provider_unavailable`;
- `publication_result_unknown`;
- `publication_lease_held`;
- `persistence_integrity_failed`.

A remote publication failure never rewrites a completed review result or local
Markdown delivery result. CLI exit status is non-zero for `failed`, `partial`,
or `unknown`, while still returning the detailed publication counts.

## 12. Testing and Acceptance

Unit tests cover deterministic keys, rendering, state transitions, aggregate
states, and new/old line mapping. Contract tests use mocked HTTP to assert exact
GitHub and GitLab routes, headers, request bodies, pagination validation, error
classification, and bounded responses.

Integration tests cover:

- review plus explicit immediate publication;
- later publication of a persisted review;
- no writes without `--publish` or `--confirm`;
- rejection of local-diff tasks, closed targets, and changed head SHAs;
- mixed line and summary-only findings;
- zero-finding summaries;
- partial success followed by safe continuation;
- a response-lost write reconciled through its marker without duplication;
- concurrent publication lease exclusion;
- security rejection before any write;
- cleanup behavior and trace persistence.

The full existing suite must continue to pass. No automated test sends a write
to a real GitHub or GitLab repository.
