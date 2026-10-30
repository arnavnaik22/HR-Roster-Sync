# Roster Desk · HR Roster Sync

An HR export arrives with six names where there should be ten. Applying it blindly could cancel four people's future rides. Roster Desk shows the decision an operator should see: **pause the file, show who is missing, and record a reason before applying it**.

This is a working implementation of MoveInSync SDE case study 5, built with **Python 3.12, FastAPI, and SQLite**. The backend also handles duplicate and late events, field ownership, tenant isolation, booking cancellation, and a transactional outbox. The web screen makes those rules easier to inspect; the core remains testable without a browser.

## Start here

The one-command application run is:

```sh
docker compose up --build
```

Open **`http://localhost:8000/`** and click **Open sample workspace**. Check the sample file, read the hold decision, enter an approval reason, and try the late update. The screen uses the same API as an external HR system; it creates a fresh tenant with synthetic records for each walkthrough. Generated API documentation is at `http://localhost:8000/docs`.

Compose binds the app to **localhost** and enables sample mode. It supplies a **local-demo-only** signing secret and persists SQLite in a Docker volume. Sample mode issues short-lived tokens for its synthetic tenant; leave `ROSTER_DEMO_MODE=0` outside this local walkthrough. Set `ROSTER_SECRET` to a random value of at least 32 characters outside a local demo. Docker is not required for the unit tests if Python dependencies are installed.

For a complete synthetic walkthrough:

```sh
python -m pip install -r requirements-dev.txt
python api_demo.py
python -m unittest discover -s tests -v
python bench.py
python bench_api.py
```

`api_demo.py` runs real FastAPI requests in process and shows duplicate and late events, a truncated export, explicit approval, and outbox retry. It uses synthetic identities only.

The [one-minute illustrated walkthrough](assets/demo.mp4) tells the same story in plain language; the [quick tour](assets/quick-tour.mp4) is shorter. They use synthetic records and figures verified against real local API, test, and benchmark runs. The reproducible commands and live presentation order are in [DEMO_SCRIPT.md](DEMO_SCRIPT.md), and [INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md) has short, honest answers to likely follow-up questions.

## Why this design fits the problem

| Case-study requirement | Implementation | How to prove it |
|---|---|---|
| Create, update, deactivate events | `POST /v1/events`; explicit `CREATE`, `UPDATE`, `DEACTIVATE` and snapshot `UPSERT` | `test_explicit_create_update_deactivate_contract` |
| Invalid rows do not fail a file | Stream CSV to a staging table; save row number, employee ID, and error code | `test_invalid_record_does_not_fail_snapshot` |
| Duplicate and out-of-order events | Per-employee source version and canonical source-state hash | `test_replay_stale_and_conflict`, concurrency test |
| Field ownership | HR writes HR columns; transport-admin API writes pickup and assistance | `test_transport_fields_survive_hr_update` |
| 200,000-row nightly diff | Indexed staging table and SQL anti-join; no full-file Python list | `bench.py` with 200,000 stored and 200,000 exported rows |
| Suspicious export safety brake | Hold if at least 30% of eligible active employees are missing; operator ID and reason recorded | `test_safety_brake_and_approval`, guided web walkthrough |
| Reliable downstream change events | Employee/bookings/outbox share one database transaction; retrying relay | `test_crash_rolls_back_employee_and_event`, API walkthrough |
| Deactivation side effects | Cancel future confirmed bookings in the same transaction; emit change events | `test_deactivation_cancels_future_bookings` |
| Authentication and tenant isolation | Signed expiring bearer tokens with tenant and role; all reads and writes tenant-scoped | `test_auth_roles_and_tenant_boundary` |
| Caching, metrics, safe errors | 5-second positive office-code LRU; pending/lag/held/invalid metrics; code-only errors | Tests and `GET /v1/metrics` |

## API flow

1. Operator registers an office with `POST /v1/offices`. The tenant comes from the signed token, never from the request body.
2. HR sends a versioned `CREATE`, `UPDATE`, or `DEACTIVATE` event to `POST /v1/events`.
3. HR uploads a raw UTF-8 CSV to `POST /v1/snapshots?version=N`. The file is spooled to disk after 1 MiB; the maximum accepted upload is 50 MiB. The response contains a run ID, row counts, missing count, and `READY` or `HELD`.
4. Operator reads `GET /v1/runs/{id}` and `GET /v1/runs/{id}/errors`.
5. Operator calls `POST /v1/runs/{id}/apply` with `{"approve_missing":false}`. A held run returns `APPROVAL_REQUIRED`. To proceed after checking the source file, the operator sends `{"approve_missing":true,"reason":"HR confirmed planned export reduction"}`. The operator ID and reason are saved with the run.
6. The relay publishes outbox events to `ROSTER_EVENT_URL` using `Idempotency-Key`. Without a URL, demo events are printed without personal data.

Generate demo tokens from the running container:

```sh
docker compose exec roster-sync python -m roster_sync token acme operator operator-123
docker compose exec roster-sync python -m roster_sync token acme hr hr-sync
```

Supply the returned value as `Authorization: Bearer TOKEN`. A minimal event body is:

```json
{
  "type": "CREATE",
  "employee_id": "E1",
  "name": "Demo Employee",
  "email": "demo@example.invalid",
  "phone": "+919999999999",
  "manager": "M1",
  "office_code": "BLR",
  "shift": "09:00",
  "source_version": 1
}
```

See [api_demo.py](api_demo.py) for exact requests and expected outcomes. The sample upload is [examples/snapshot.csv](examples/snapshot.csv).

## Correctness contract

**Ordering.** `(tenant_id, employee_id)` is the aggregate key. Versions must increase for that employee. A lower version is `STALE`; an equal version with identical HR-owned fields is `DUPLICATE`; an equal version with different HR-owned fields is `VERSION_CONFLICT`. `CREATE`, `UPDATE`, and snapshot `UPSERT` with identical source state have the same hash. Source timestamps are not used as versions because clock skew and equal timestamps make ordering ambiguous.

**Atomicity.** An event update and its outbox row are written in one SQLite transaction. Deactivation also cancels future confirmed bookings in that transaction. If the process crashes before commit, none of these writes survive. If it crashes after commit and before publish, the outbox row remains for retry.

**Delivery.** The relay is **at least once**. A crash after the consumer accepts an event but before `published_at` is recorded can resend it. Consumers must deduplicate using the stable event ID in the `Idempotency-Key` header. The application does not claim exactly-once network delivery.

**Snapshot source contract.** Each export has a monotonically increasing source watermark, supplied as `version=N`. Every included row has a source version at or below the watermark. The export must represent the complete employee set as of that watermark. An older export cannot apply after a newer one. An employee updated past the watermark cannot be changed by that export. Missing employees are deactivated at the watermark only if their stored version is lower. Without a trustworthy watermark and full-export contract, no system can safely infer exits from absence.

**Safety brake.** The denominator is eligible active employees with stored version at or below the watermark. Invalid export rows remain absent from staging, so they count toward the missing ratio; this intentionally errs toward holding a bad file. At or above `ROSTER_MISSING_THRESHOLD` (default `0.30`), nothing is applied before operator approval. The stage and apply phases are separate. Applied runs are idempotent.

**Field ownership.** HR owns name, email, phone, manager, office, shift, and active status. The transport administrator owns pickup point and special assistance. HR event SQL never updates transport-owned columns. Deactivation does not erase them, so reactivation can preserve the operational preference.

## Performance evidence

Measured on **Windows 11, Python 3.12.14, Intel64 Family 6 Model 186, 12 logical CPUs**, using the bundled Python interpreter on 30–31 October 2026:

| Measurement | Result | What it covers |
|---|---:|---|
| 200,000-row stage and diff | **14.946 s**; 13,381 rows/s | 200,000 existing employees versus a 200,000-row export with 1,000 missing and 1,000 new |
| Peak process working set | **24.0 MiB** | `bench.py` process; database file and OS page cache excluded |
| 1,000 FastAPI event requests | **59.1 requests/s** | In-process `TestClient`, SQLite commits, no network or relay |
| FastAPI write latency | **p50 15.88 ms; p99 39.73 ms** | Same single-process run; not a distributed production SLO |

The setup of the 200,000 existing employees happens **before** the timed interval. `bench.py` stages the export and runs the indexed missing-row comparison inside the interval. It does not time applying all 200,000 changes. Re-run these scripts on the target machine before quoting numbers in an interview.

Complexity: event processing performs O(1) indexed lookups/writes per employee. Streaming CSV staging is O(n) time and O(1) application memory, excluding the 1 MiB HTTP spool, SQLite cache, and database file. The anti-join touches O(n + m) indexed rows in the worst case. The main limits are SQLite's single-writer contention, the duration of applying a large snapshot in one transaction, and growth of the outbox if the consumer is unavailable.

## Run and test details

```sh
python -m unittest discover -s tests -v
python api_demo.py
python demo.py
python bench.py
python bench_api.py
python bench_events.py
```

All **14 tests pass** locally. They cover a concurrent version race, process failure before transaction commit, relay failure and retry, invalid rows, same-version conflicts, tenant isolation, role checks, snapshot approval audit, field ownership, booking cancellation, stale snapshot rejection, and the guided web demo. The actual Uvicorn process also passed a `/v1/health` request locally. `DESIGN.md` contains the architecture diagram, sequence diagram, failure-mode table, and trade-offs.

GitHub Actions runs the tests, the API walkthrough, bytecode compilation, and a Docker Compose health check on each push. The workflow has not run until the first GitHub push.

Configuration:

| Variable | Default | Meaning |
|---|---|---|
| `ROSTER_DB` | `roster.db` | SQLite file path |
| `ROSTER_SECRET` | Compose demo value only | HMAC signing key; minimum 32 characters |
| `ROSTER_EVENT_URL` | empty | HTTP event consumer; empty means safe demo logging |
| `ROSTER_MISSING_THRESHOLD` | `0.30` | Missing active fraction that holds an export |
| `ROSTER_DEMO_MODE` | `0` | Enable local sample sessions; Compose sets `1` |
| `ROSTER_HOST` | `127.0.0.1` in demo mode | Listen address; Compose binds the published port to localhost |

The `/v1/metrics` response is tenant-scoped: pending outbox count, oldest pending age, held run count, and total invalid rows. Production alert suggestions: outbox oldest age above 60 seconds, any held run awaiting review, and sustained invalid-row growth. Structured request logs contain method, status, and elapsed time, but not URL, request body, phone, email, or pickup location. The office-code cache stores only positive `(tenant, office)` keys, holds at most 4,096 entries, and expires after five seconds. There is no office-delete API; an out-of-band delete could be accepted from cache for at most five seconds.

## Trade-offs and production path

SQLite makes the assessment runnable with one command and gives a clear, testable transaction boundary. It is **not** the database choice for a multi-node production rollout. For MoveInSync scale, move the schema to PostgreSQL, use a guarded upsert or row lock for per-employee ordering, and use a reliable broker/CDC relay for the outbox. Partition or stage large imports, then activate a validated snapshot without holding one long write transaction. Retain the same version, ownership, safety, and idempotency rules.

The HMAC token issuer is a local assessment tool, not an enterprise identity provider. A production boundary needs the company's IdP/JWKS, key rotation, TLS, rate limits, and employee-level permissions. This implementation intentionally exposes only operator-facing employee fields and never logs personal data. It uses a simple one-second relay retry; production should add backoff, dead-letter handling, and tracing.

## GitHub submission

This folder is already a Git repository with separate commits for the core, API, benchmarks, and documentation. `origin` is configured as [arnavnaik22/HR-Roster-Sync](https://github.com/arnavnaik22/HR-Roster-Sync). The repository is currently empty. From this folder, the final publication step is:

```sh
git push -u origin main
```

Do not add real employee records or a `.env` file. The included CSV and demo use synthetic data. The source archive in the parent `outputs` folder is convenient for review, but **the local Git folder retains the commit history**; an archive alone does not.

## References

- [MoveInSync data sync help](https://helpcenter.moveinsync.com/support/solutions/articles/1070000136106-data-sync-schedule-sync) and [HRMS integration guide](https://moveinsync.com/support-article/how-to-integrate-transport-management-software-with-hr-payroll-and-erp-tools) establish the product relevance.
- [MoveInSync backend role description](https://moveinsync.com/join-us) emphasizes reliable event-driven services, database correctness, and observability.
- [FastAPI deployment guide](https://fastapi.tiangolo.com/deployment/docker/) supports the container layout.
- [Python SQLite transaction documentation](https://docs.python.org/3.12/library/sqlite3.html#transaction-control) explains the explicit transaction mode used here.
- [Debezium outbox event router](https://debezium.io/documentation/reference/transformations/outbox-event-router.html) describes the production outbox pattern.
- [PostgreSQL `INSERT ... ON CONFLICT`](https://www.postgresql.org/docs/current/sql-insert.html) is the basis for a production guarded upsert.
