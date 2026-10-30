"""Run the main case-study flows without any external setup."""
import json
import tempfile
from pathlib import Path

from roster_sync.core import RosterError, RosterStore


def employee(employee_id, version=1, shift="09:00"):
    return {"employee_id": employee_id, "name": "Demo Employee", "email": "demo@test.invalid",
            "phone": "+919999999999", "manager": "M1", "office_code": "BLR",
            "shift": shift, "source_version": version}


with tempfile.TemporaryDirectory() as tmp:
    store = RosterStore(Path(tmp) / "demo.db")
    store.add_office("acme", "BLR")
    print("1. New event:", store.apply_event("acme", employee("E1", 7, "18:00")))
    print("2. Duplicate:", store.apply_event("acme", employee("E1", 7, "18:00")))
    print("3. Out-of-order older event:", store.apply_event("acme", employee("E1", 6)))
    print("   Final shift:", store.employee("acme", "E1")["shift"])
    for i in range(2, 11):
        store.apply_event("acme", employee(f"E{i}"))
    truncated = store.begin_snapshot("acme", 10, [employee(f"E{i}") for i in range(1, 7)])
    print("4. Truncated export:", truncated.status, "missing:", truncated.missing)
    try:
        store.apply_snapshot("acme", truncated.run_id)
    except RosterError as exc:
        print("   Without approval:", exc.code)
    print("   Employee E10 still active:", bool(store.employee("acme", "E10")["active"]))
    print("5. Outbox pending before relay:", store.pending_count("acme"))
    published = store.publish_pending("acme", lambda event_id, payload: None)
    print("   Published:", published, "pending:", store.pending_count("acme"))
