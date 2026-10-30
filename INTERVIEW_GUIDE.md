# Interview guide

Read the code and run the demo before using these answers. They are talking points, not a script to memorize.

## A 45-second opening

“The failure I focused on is a truncated nightly HR export. If 40% of employees disappear from one file, applying it blindly could cancel rides for people who still work there. I stage the file first, compare it with the current roster, and hold it when the missing fraction crosses a threshold. An operator can inspect the count and approve with a reason. Live events use source versions, so duplicate or late updates cannot move an employee backward. The employee change and its outbox event commit together, and the relay retries delivery.”

Then show the web demo: **10 active → 6 in file → held → approval reason → 6 active**.

## Questions worth being ready for

**Why did you choose this case study?**
It connects directly to employee transport operations: accurate employee and shift data decide who is eligible for a cab. It also let me demonstrate API design, data reconciliation, SQL transactions, and failure handling in one small system.

**What is your idempotency key?**
Tenant ID, employee ID, and source version identify a logical source change. An equal version with the same HR-owned values is a duplicate. An equal version with different values is a conflict; I do not guess which one is right.

**Why not order by event timestamp?**
Two changes can share a timestamp, and clocks can differ between systems. A source-assigned monotonic version gives an unambiguous order per employee. If a source only has timestamps, I would require a tie-breaker or source sequence before claiming the same guarantee.

**What if a newer live event arrives before an older nightly file?**
The file carries a source watermark. Applying it rechecks each employee's stored version. A record newer than the watermark is left alone. Applied snapshot watermarks only move forward.

**Why is the safety brake before applying any rows?**
Because a partial export is a file-level problem. Letting valid-looking rows apply while thousands of other employees are absent would make the roster harder to reason about. Staging gives the operator a complete preview before the decision.

**What if one row is bad?**
It gets a row number, employee ID when valid, and an error code. Other rows continue. The error report contains no phone, email, or pickup address. Invalid rows also increase the missing count, making the safety brake conservative.

**Does the outbox give exactly-once delivery?**
No. It guarantees the database change and event record commit together. The relay can deliver twice if it crashes after the consumer accepts an event but before marking it sent. Consumers must deduplicate using the stable event ID.

**Why do HR updates leave pickup points alone?**
The transport team owns that field. HR has authority over identity, office, shift, and employment state. The SQL update names only HR-owned columns, so an HR feed cannot accidentally erase a transport preference.

**Why Python and SQLite if the role prefers Java?**
The brief allows Python. It let me keep the core rules plain, test them quickly, and demonstrate the 200,000-row diff on the available machine. SQLite keeps the demo one-command and its transaction boundary visible. I would use PostgreSQL for multi-instance production writes. I would not claim this SQLite setup is a production cluster.

**What did you actually measure?**
`bench.py` preloads 200,000 employees, then times staging and comparing a 200,000-row export with 1,000 missing and 1,000 new. It does not time applying every row or a distributed deployment. `bench_api.py` measures 1,000 in-process FastAPI requests and reports p50/p99 latency. Hardware and measurement boundaries are in the README.

**What would you change for production?**
PostgreSQL for concurrent writers, an enterprise identity provider, a broker or CDC outbox relay, explicit retry and dead-letter handling, and a chunked snapshot activation design. The ordering, ownership, and safety rules would stay the same.

## During the demo

1. Say what would go wrong if a short file were trusted.
2. Show that the four absent people remain active while the run is held.
3. Enter a reason and approve. Point to the recorded operator and changed roster.
4. Trigger the late event; show that version 10 cannot undo version 11.
5. If asked about reliability, run `python api_demo.py` to show an injected downstream failure and the outbox retry.

The strongest answer is the one you can trace back to a line of code or a test. If you have not tested a production scenario, call it a design direction rather than a proven result.
