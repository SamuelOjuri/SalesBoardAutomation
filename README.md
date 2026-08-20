# Sales Board Automation

The backend implements the frozen Phase 0 safety contract, the durable Phase 1
service boundary, and authenticated Phase 2 Monday intake. It does not yet
extract email content, match Accounts, or write to Monday; those behaviors
belong to later phases.

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

The application remains available when schema loading fails, but `/health`
reports `publication_enabled: false` and the gate's issue codes. Alembic is the
only schema creation path; application startup never creates tables.

## Local setup

Create a PostgreSQL database, install dependencies, and create a local `.env`
from `.env.example` with real credentials. At least one of
`MONDAY_SIGNING_SECRET` or `MONDAY_WEBHOOK_SHARED_SECRET` is required.

From the repository root:

```powershell
python -m pip install -r requirements.txt
python -m alembic -c alembic.ini upgrade head
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Check service and publication-gate status at `http://127.0.0.1:8000/health`.

## Tests

Run the complete Phase 0 through Phase 2 suite from the repository root:

```powershell
python -m pytest -q
```

The model tests use SQLite only as a fast local check. Release migrations and
runtime storage target PostgreSQL, including its partial unique index and
immutability triggers.
