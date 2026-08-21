# Sales Board Automation

The backend implements the frozen Phase 0 safety contract through the Phase 7
durable worker and operator controls. The FastAPI webhook and background worker
run as separate processes against the same PostgreSQL queue.

## Backend foundation

The FastAPI service uses Pydantic Settings, SQLAlchemy 2, Alembic, PostgreSQL,
`requests`, and `tenacity`. The installed dependency set also includes
`extract-msg` and `google-genai` for the extraction phases that follow.

Phase 1 persists four records:

- `webhook_events`: authenticated event intake with a unique idempotency key;
- `processing_items`: one durable state record per Sales item;
- `processing_jobs`: leased, retryable work with one active job per Sales item;
- `processing_audits`: append-only processing decisions and outcomes.

The database enforces one active `scheduled`, `running`, or `retry_wait` job for
each board/item pair. A job stores its canonical input asset manifest and a
SHA-256 revision derived from each asset's ID, filename, size, and creation
timestamp. PostgreSQL triggers prevent that identity from changing and prevent
audit rows from being updated or deleted.

## Behavioral contract

The immutable contract in `backend/app/behavioral_contract.py` requires the
pipeline to:

- process only current `.msg` and `.eml` email files;
- publish the alphabetic project postcode area;
- exclude Accounts carrying Duplicate label ID `1`;
- leave ambiguous Account matches unresolved;
- fill only empty Sales values and preserve different human values;
- process Postcode and Accounts independently; and
- treat email content and model output as untrusted data.

The reference implementation is pinned to commit
`ef321095ed96a7dde6543b89da58b2689e76a53d`.

## Startup schema gate

`backend/app/publication_gate.py` starts with publication disabled. During
FastAPI startup, the Monday client requests typed column `settings` for the
three configured contract columns and passes the authoritative board to
`validate_schema_at_startup`. `settings_str` is accepted only by the validator
to support the supplied legacy schema snapshot.

```python
publication_gate = validate_schema_at_startup(monday_client.load_sales_board_schema)
```

Publication is enabled only when the live schema matches all configured column
IDs and types, the Accounts relation targets only board `1654217230`, and all
123 configured Postcode labels retain their expected IDs. A schema fetch error
or any drift leaves intake and analysis available but keeps publication
disabled.

Every Monday mutation boundary must enforce the gate immediately before a
write:

```python
publication_gate.require_publication_enabled()
```

## Monday intake

Monday sends Email File changes to `POST /api/monday/webhooks`. Challenge
requests are echoed as required by Monday. Event requests require HTTPS and
either an expiring HS256 bearer token signed with `MONDAY_SIGNING_SECRET` or a
shared secret supplied as `X-Monday-Webhook-Secret` (the reference-compatible
`token` query parameter is also accepted).

The endpoint persists the authenticated event and its deterministic
idempotency key before fetching any item data. It then re-fetches the Sales item
from Monday and queues work only when the event and authoritative snapshot meet
all of these conditions:

- the board is the configured Sales board;
- the changed column is `file_mm5erpbb`;
- the item is active; and
- the current Email File membership includes at least one `.msg` or `.eml`.

Only assets listed in the Email File column are inputs. The queue manifest is
sorted by numeric asset ID and its revision covers asset ID, filename, size,
and creation timestamp. Duplicate webhook delivery returns the existing event
without another Monday read or job. A changed snapshot coalesces with an active
job and records a supersession request without mutating that job's immutable
input identity.

`download_email_assets` provides the worker-facing download boundary. It uses
authorized streamed downloads, checks the metadata size, computes SHA-256, and
deletes the temporary directory on every exit path.

## Email and postcode extraction

`backend/app/services/email_parser.py` parses verified `.eml` and `.msg` files.
It prefers plain-text message bodies and falls back to visible HTML text, then
extracts text from PDF and supported image attachments through an injected
multimodal client. Files are processed in numeric asset-ID order, and their
size and SHA-256 are revalidated immediately before parsing.

`backend/app/services/postcode.py` sends the combined untrusted content to the
configured Gemini model using a strict Pydantic response schema that can
return only the project-location postcode and an explicitly stated requester
company. The prompt and schema explicitly exclude sender, recipient,
signature, and correspondence addresses from postcode selection. Company is
secondary account-matching evidence only. Malformed or augmented model output
is rejected.

The extracted postcode is reduced to its alphabetic area using the pinned
reference behavior. `MondayClient.load_postcode_dropdown_column` obtains the
live typed dropdown settings, and the resolver returns a value only for one
unambiguous existing label on `dropdown_mm60y5x8`; it never creates a label.
For example, `WA4 6NL`, `wa4 6nl`, and `wa46nl` all resolve to `WA`, then to
label ID `115` when that label is present in the live settings. Missing and
unmapped postcodes produce no Monday value.

Extraction results contain only the area, resolved label ID/value, normalized
company evidence, input asset IDs, and an extracted-text SHA-256. Raw email and
attachment content is not returned or logged. The default pipeline identity is
`sales-requester-v1`.

## Requester identity and Accounts index

`backend/app/services/requester_identity.py` prefers an external top-level
sender, then inspects forwarded header blocks in newest-first order when the
top-level sender is internal. Addresses are lower-cased and IDNA-normalized;
registrable domains are derived from an offline public-suffix snapshot. Generic
email providers are retained as requester addresses but are never automatic
domain evidence.

`backend/app/services/accounts.py` loads every Accounts page through typed
Monday column values, including the final page whose cursor is null. Only
Duplicate label ID `1` excludes an item, while null and empty values remain
eligible. Complete indexes are cached for five minutes. A selected Account is
always re-fetched, must belong to the configured Accounts board, and must still
be active and unflagged before publication.

## Safe Monday publication

`backend/app/services/publication.py` is the Phase 6 write boundary. It
re-fetches the Sales item with the current Email File asset metadata and typed
`DropdownValue.values` and `BoardRelationValue.linked_item_ids`. The service
recomputes the immutable input revision and rejects inactive, moved, or changed
items before any mutation.

Postcode and Accounts are evaluated independently. Empty columns with valid
candidates are included in one selective `change_multiple_column_values`
mutation. Intended values already present are reported without a write, while
different existing values are preserved and returned as conflicts. An Account
candidate is omitted when its immediate revalidation finds it inactive,
duplicate-flagged, missing, or otherwise ineligible; a valid Postcode can still
be published in that case.

`MondayClient.change_sales_item_column_values` permits only
`dropdown_mm60y5x8` and `board_relation_mm64107r`, validates their complete
value shapes, and performs one HTTP attempt. The publication service enforces
the startup schema gate immediately before that attempt and post-reads the
Sales item after both successful and network-ambiguous mutations. An ambiguous
result is accepted only when the authoritative post-read confirms it;
otherwise the transient error is raised for a later worker retry.

The application remains available when schema loading fails, but `/health`
reports `publication_enabled: false` and the gate's issue codes. Alembic is the
only schema creation path; application startup never creates tables.

## Durable worker and operations

`backend/app/worker.py` is the separate background process. It claims due jobs
with PostgreSQL row locks and `SKIP LOCKED`, records lease ownership, sends
heartbeats from short independent transactions, and recovers expired leases.
Failures retain the last completed stage and use capped exponential retry.
Only exception types, job IDs, item IDs, and structured outcomes are logged;
email and model content are not persisted in logs or error fields.

`backend/app/services/pipeline.py` commits checkpoints after `extracting`,
`matching_account`, `validating`, and `publishing`. A retry resumes from the
saved stage. Every checkpoint verifies lease ownership and the immutable input
identity. If Monday or a newer webhook exposes changed Email input, the worker
cancels the leased job and creates an immutable successor for the authoritative
snapshot. A retry after an ambiguous publication remains safe because Phase 6
post-reads Monday and complete-value writes are idempotent.

Processing modes are enforced by the worker:

- `off` recovers expired leases but claims no work;
- `shadow` analyzes and validates all queued items but performs no mutations;
- `allowlist` analyzes and publishes only `PROCESSING_ALLOWLIST_ITEM_IDS`; and
- `enabled` analyzes and publishes all eligible queued items.

Run a background worker from the repository root:

```powershell
python -m app.worker
```

Set `PYTHONPATH=backend` for that command, or use Render's worker start command:

```text
cd backend && python -m app.worker
```

Phase 1 already created all stage, lease, heartbeat, retry, result, and audit
columns used by the worker, so Phase 7 does not require another migration.

Operator commands use the same queue and identity checks:

```powershell
$env:PYTHONPATH = "backend"
python -m app.operator enqueue 1234567890
python -m app.operator retry 00000000-0000-0000-0000-000000000000
python -m app.operator reconcile 1234567890
python -m app.operator metrics
```

Worker timing can be tuned with `WORKER_POLL_INTERVAL_SECONDS`,
`WORKER_HEARTBEAT_INTERVAL_SECONDS`, `WORKER_LEASE_TIMEOUT_SECONDS`,
`WORKER_RETRY_BASE_SECONDS`, and `WORKER_RETRY_MAX_SECONDS`. The heartbeat
interval must remain shorter than the lease timeout.

## Local setup

Create a PostgreSQL database, install dependencies, and create a local `.env`
from `.env.example` with real credentials. At least one of
`MONDAY_SIGNING_SECRET` or `MONDAY_WEBHOOK_SHARED_SECRET` is required.

From the repository root:

```powershell
python -m pip install -r requirements.txt
python -m alembic -c alembic.ini upgrade head
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
$env:PYTHONPATH = "backend"
python -m app.worker
```

Check service and publication-gate status at `http://127.0.0.1:8000/health`.

## Tests

Run the complete Phase 0 through Phase 7 suite from the repository root:

```powershell
python -m pytest -q
```

The model tests use SQLite only as a fast local check. Release migrations and
runtime storage target PostgreSQL, including its partial unique index and
immutability triggers.
