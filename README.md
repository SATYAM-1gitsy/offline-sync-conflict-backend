# GDG SRM Offline Sync Conflict

FastAPI backend extending the existing Student Management API with a generic, versioned JSON synchronization API. Student routes and the `students` table remain available. Sync records use separate tables and do not require Student fields.

## Stack and architecture

- Python, FastAPI, Pydantic 2, SQLAlchemy 2, PostgreSQL 17, Docker Compose.
- `sync_records` holds a JSONB document and its current version.
- `change_history` has one row for each accepted version, including the base version, device, idempotency key, changed top-level fields, resulting JSON snapshot, and timestamp. The initial create is version 1.
- `sync_idempotency` stores a request hash and the original JSON response, unique per `(device_id, idempotency_key)`. Entries are retained indefinitely.
- An update locks its record row (`SELECT ... FOR UPDATE`) while checking history and committing state plus history in one transaction. A PostgreSQL transaction advisory lock serializes the same client idempotency key, including concurrent retries.

Device IDs identify clients; they are not authentication credentials. Add real authentication before exposing the service to untrusted networks.

## Setup and run

The repository has no dependency lock file. Install the existing app dependencies in a virtual environment:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start PostgreSQL with `docker compose up -d db`. Compose mounts `./data/db` as its persistent data directory; keep that directory and do not use `down -v`. Configure `DATABASE_URL` if needed (default: `postgresql+psycopg://gdg:gdgpswd@localhost:5432/myDB`). The Compose credentials are for local development only.

Create new tables without dropping or replacing existing tables, then run the API:

```sh
python create_tables.py
uvicorn main:app --reload
```

`create_tables.py` runs SQLAlchemy `create_all`, which creates missing tables but does not migrate existing tables. Sync tables are additive; the existing Student schema is unchanged. If `change_history` was created by an earlier version of this implementation, add its optional snapshots column before deploying the updated code:

```sql
ALTER TABLE change_history ADD COLUMN IF NOT EXISTS resulting_data JSONB;
```

For this Compose database, run that additive compatibility change with:

```sh
docker compose exec -T db psql -U gdg -d myDB -c 'ALTER TABLE change_history ADD COLUMN IF NOT EXISTS resulting_data JSONB;'
```

Existing historical rows keep `NULL` snapshots; new rows include the full resulting document. There is no Alembic setup or versioned migration yet. Swagger UI is at `http://127.0.0.1:8000/docs`.

## API

All request models reject unknown fields. JSON document and patch values must be objects. A patch must contain at least one field. `base_version` is the version from which the client edited; it must not be ahead of the server. The API returns HTTP 200 with outcome `accepted`, `merged`, `conflict`, or `rejected` for valid synchronization decisions. Malformed bodies get 422, unknown records 404, and a changed-payload idempotency-key reuse 409.

### Create

`POST /sync/records` (201)

```json
{"device_id":"phone-a","data":{"name":"Alex","course":"CSE"}}
```

Returns `record_id`, version 1, data, and timestamps. The server records the creator in version 1 history.

### Read and history

- `GET /sync/records/{record_id}` returns current data, version, and timestamps (404 if absent).
- `GET /sync/records/{record_id}/history` returns ordered versions, base versions, device IDs, changed fields, resulting snapshots, and accepted timestamps. It does not expose idempotency keys.

### Submit changes

`POST /sync/records/{record_id}/changes`

```json
{"device_id":"phone-b","base_version":1,"changes":{"course":"AI"},"idempotency_key":"b-edit-001"}
```

An exact retry for a device and key returns the original response without applying again. Reusing a key with a different request body returns 409. Request results, state, and history commit together.

### Resolve a conflict

`POST /sync/records/{record_id}/resolve`

```json
{"device_id":"phone-b","expected_version":2,"changes":{"name":"Alexander"},"idempotency_key":"resolve-001"}
```

The client fetches and reviews current state, then submits the fields it explicitly chooses. If `expected_version` no longer matches, the outcome is `rejected` and no data changes. A successful resolution creates a new version. Resolution values are authoritative for the supplied fields only.

## Merge and conflict policy

Changes are shallow patches over top-level keys. Nested JSON values are atomic: concurrent edits anywhere inside the same top-level object conflict. A key omitted from `changes` is unchanged. To delete a key, submit `{ "$delete": true }` for that key; this exact object is reserved as the deletion marker.

- Current base: apply fields and increment version (`accepted`).
- Older base: collect every accepted changed field after that version. If none overlap the submitted top-level keys, apply to the latest document (`merged`).
- Any overlap returns `conflict`, lists the conflicting field names, and leaves server data/version unchanged.
- A future base or unavailable history returns `rejected`; no update is applied.
- Server timestamps do not determine precedence. There is no last-write-wins behavior.

History is currently retained indefinitely, so any extant version from 1 through current can be checked. If history is manually pruned or inconsistent, the engine rejects when it detects a missing version rather than guessing.

## Worked scenarios

**Independent edits:** Start at v1 `{ "name":"Alex", "course":"CSE" }`. Device A submits base 1 with `name=Alexander`, producing v2. Device B submits base 1 with `course=AI`; it merges to v3 `{ "name":"Alexander", "course":"AI" }`.

**Conflicting edits:** From v1, devices A and B both change `name`. A's update produces v2. B receives `conflict` with `conflicting_fields:["name"]`; v2 remains intact. B fetches v2 and chooses a value with `expected_version:2`; that explicit resolution creates v3 if the version is still 2.

## Tests

Tests require a separately provisioned PostgreSQL test database because the implementation uses JSONB and advisory locks. The suite refuses to run against the configured application `DATABASE_URL`; each test creates and drops only its own randomly named schema.

```sh
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/isolated_test_db'
pytest -q
```

Without `TEST_DATABASE_URL`, the PostgreSQL integration tests are skipped. They cover create/read, apply/merge/conflict, preserving the winning value, resolution/current-version checks, history, idempotent replay/key reuse, payload validation, future base, unknown record, and concurrent duplicate and overlapping requests. Test execution status must be checked from pytest output; writing tests alone does not verify them.

Verified during finalization on 2026-10-04 against a disposable PostgreSQL 17 container: **6 passed, 0 failed, 0 skipped**. The run also verified a deliberately failed history write rolls back the record update and version. Pytest emitted one Starlette deprecation warning about the installed httpx integration.

## Scope and limitations

- Top-level field-level conflict detection only; nested documents are atomic fields.
- No delete-record operation, history retention policy, authentication, or schema migrations are implemented.
- `create_all` is safe for additive initial setup but is not a migration system.
- `test_db.py` is a direct connectivity check and may print the database server version; it does not mutate data.
