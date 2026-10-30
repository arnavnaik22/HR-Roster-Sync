# HR Roster Sync — MoveInSync SDE Case Study 5

A runnable Python 3.12 service for idempotent HR events, bounded-memory nightly reconciliation, a missing-export safety brake, tenant-scoped access, and a transactional outbox. Python is explicitly allowed by the case brief; it matches the candidate's API and data pipeline experience.

## Run

```sh
docker compose up --build
```

The container serves `http://localhost:8000`. The Compose default secret is **for a local demo only**. Set `ROSTER_SECRET` to a random 32+ character value before using the service beyond your machine. SQLite is included in the container and persisted in the `roster-data` volume. Set `ROSTER_EVENT_URL` to an HTTP endpoint to deliver outbox events; without it, the demo relay writes event IDs and types to stdout.

No Docker on your machine? With Python 3.12 and `ROSTER_SECRET` set, run `python -m roster_sync` from this folder.

Generate a tenant-scoped demo token from the container:

```sh
docker compose exec roster-sync python -m roster_sync token acme operator
docker compose exec roster-sync python -m roster_sync token acme hr
```

Set the returned strings as `OP_TOKEN` and `HR_TOKEN` in your shell. Example API flow:

```sh
curl -X POST http://localhost:8000/v1/offices -H "Authorization: Bearer $OP_TOKEN" -H 'Content-Type: application/json' -d '{"office_code":"BLR"}'
curl -X POST http://localhost:8000/v1/events -H "Authorization: Bearer $HR_TOKEN" -H 'Content-Type: application/json' -d '{"employee_id":"E1","name":"Demo Employee","email":"demo@example.invalid","phone":"+919999999999","manager":"M1","office_code":"BLR","shift":"09:00","source_version":1}'
curl -X POST 'http://localhost:8000/v1/snapshots?version=10' -H "Authorization: Bearer $HR_TOKEN" -H 'Content-Type: text/csv' --data-binary @examples/snapshot.csv
curl -X POST http://localhost:8000/v1/runs/RUN_ID/apply -H "Authorization: Bearer $OP_TOKEN" -H 'Content-Type: application/json' -d '{"approve_missing":false}'
curl http://localhost:8000/v1/runs/RUN_ID/errors -H "Authorization: Bearer $OP_TOKEN"
curl http://localhost:8000/v1/metrics -H "Authorization: Bearer $OP_TOKEN"
```

`RUN_ID` comes from the snapshot upload response. Uploading is a **stage** operation: it validates and compares rows without changing employees. The operator applies a ready run. A run marked `HELD` needs `approve_missing: true`; this is an explicit operator action. The missing threshold defaults to 30% and is configurable in `RosterStore`.

## Guarantees and assumptions

- The idempotency identity is `(tenant_id, employee_id, source_version)`. A repeat of identical content is a no-op; conflicting content at the same version is rejected. Lower versions are stale. `BEGIN IMMEDIATE` serializes competing writers in this SQLite demo. The same rule should use a guarded upsert/row lock in PostgreSQL.
- HR owns name, email, phone, manager, office, shift, and active state. Transport admins own pickup point and special assistance; HR updates never write those columns.
- Each nightly export has a **source watermark** (`version` query parameter). Included rows carry versions at or below it. Applied snapshot watermarks only move forward. A live event with a higher version wins over an older export. A missing employee is deactivated at the export watermark only when its current version is lower. The exporter must provide a monotonically increasing watermark; otherwise it is unsafe to infer deletions.
- The safety brake compares missing active employees against active employees at or before the watermark. If the missing fraction is at least 30%, the run is held before any employee changes are applied. Invalid rows count as missing, which makes the brake conservative.
- Employee changes, booking cancellation, and outbox insertion happen in the same database transaction. The relay provides **at-least-once** delivery. Downstream consumers must deduplicate by the stable `Idempotency-Key` header/event ID. Delivery to an external system cannot be made atomic with the SQLite transaction.
- Snapshot staging uses a row iterator and a database staging table, so application memory does not grow with row count. One database transaction applies an approved snapshot; that favors consistency but creates a longer write lock. A production service should batch with a versioned activation/swap or use PostgreSQL staging tables.
- Email and phone are validated but never written to logs or per-record error reports. Error reports contain row number, employee ID if valid, and error code.

## Tests and benchmark

```sh
python -m unittest discover -s tests -v
python bench.py
python bench_events.py
```

The tests cover duplicate and stale events, same-version conflicts, HR/transport field ownership, invalid rows, the safety brake, transaction rollback after injected failure, outbox retries, concurrent writers, tenant isolation, and booking cancellation.

Measured on the development host (Windows, bundled Python; 30 October 2026): staging and diffing **200,000 rows took 13.12 s** (15,250 rows/s), with **23.9 MiB peak process working set**. A second run took 12.85 s. The 1,000-event write benchmark measured **100.4 events/s, p50 9.53 ms, p99 20.92 ms**. Re-run both scripts on the interview machine; timings vary with disk and CPU. Neither is a distributed load test.

## API

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/v1/offices` | operator | Register office code for tenant |
| POST | `/v1/events` | hr | Apply one versioned UPSERT/DEACTIVATE |
| POST | `/v1/snapshots?version=N` | hr | Stage CSV export and get run status |
| POST | `/v1/runs/{id}/apply` | operator | Apply run; approve held run explicitly |
| GET | `/v1/runs/{id}/errors` | hr/admin/operator | Per-record error codes |
| GET | `/v1/employees/{id}` | hr/admin/operator | View non-sensitive roster fields |
| POST | `/v1/employees/{id}/transport` | admin/operator | Set transport-owned fields |
| GET | `/v1/metrics` | admin/operator | Pending outbox count, tenant-scoped |

Every request authenticates a signed, expiring token with tenant and role claims. This demonstration token format is intentionally small; production deployment should integrate the organization's identity provider, key rotation, TLS, audit trail, and rate limiting.

## What remains for production

Use PostgreSQL for concurrent multi-instance writes and a Kafka-compatible outbox relay. Put the HTTP service behind an identity provider and reverse proxy. Add operator approval audit records, file checksums, outbox dead-letter handling, richer metrics/histograms, and a controlled snapshot activation design for very large tenants. This demo keeps its boundaries explicit so the guarantees can be defended honestly.

See [DESIGN.md](DESIGN.md) for architecture, hardest-flow sequence, failure table, and trade-offs.
