"""A reproducible API walkthrough using synthetic data and FastAPI TestClient."""
import csv
import io
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from roster_sync.api import create_app, issue_token
from roster_sync.core import RosterStore


def employee(employee_id, version=1, shift="09:00", typ="CREATE"):
    return {"employee_id": employee_id, "name": "Demo Employee", "email": f"{employee_id.lower()}@test.invalid",
            "phone": "+919999999999", "manager": "M1", "office_code": "BLR",
            "shift": shift, "source_version": version, "type": typ}


with tempfile.TemporaryDirectory() as tmp:
    store = RosterStore(Path(tmp) / "api-demo.db")
    client = TestClient(create_app(store, "s" * 32, relay_enabled=False))
    operator = {"Authorization": "Bearer " + issue_token("s" * 32, "acme", "operator", "operator-123")}
    hr = {"Authorization": "Bearer " + issue_token("s" * 32, "acme", "hr", "hr-sync")}

    response = client.post("/v1/offices", json={"office_code": "BLR"}, headers=operator)
    print("POST /v1/offices ->", response.status_code, response.json())
    for i in range(10):
        response = client.post("/v1/events", json=employee(f"E{i}"), headers=hr)
        assert response.status_code == 200
    print("POST /v1/events -> created 10 employees")
    updated = employee("E0", 2, "18:00", "UPDATE")
    print("UPDATE E0 v2 ->", client.post("/v1/events", json=updated, headers=hr).json())
    print("REPLAY E0 v2 ->", client.post("/v1/events", json=updated, headers=hr).json())
    print("LATE E0 v1 ->", client.post("/v1/events", json=employee("E0"), headers=hr).json())

    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=("employee_id", "name", "email", "phone", "manager", "office_code", "shift", "source_version"))
    writer.writeheader()
    for i in range(6):
        row = employee(f"E{i}", 2 if i == 0 else 1, "18:00" if i == 0 else "09:00")
        row.pop("type")
        writer.writerow(row)
    response = client.post("/v1/snapshots?version=10", content=stream.getvalue().encode(),
                           headers={**hr, "Content-Type": "text/csv"})
    run = response.json()
    print("POST /v1/snapshots ->", response.status_code, {k: run[k] for k in ("status", "total", "invalid", "missing")})
    path = f"/v1/runs/{run['run_id']}/apply"
    denied = client.post(path, json={"approve_missing": False}, headers=operator)
    print("APPLY without approval ->", denied.status_code, denied.json())
    print("E9 active before approval ->", bool(client.get("/v1/employees/E9", headers=operator).json()["active"]))
    applied = client.post(path, json={"approve_missing": True}, headers=operator)
    print("APPLY with approval ->", applied.status_code, {k: applied.json()[k] for k in ("status", "applied", "invalid")})
    print("E9 active after approval ->", bool(client.get("/v1/employees/E9", headers=operator).json()["active"]))
    print("Approval actor ->", client.get(f"/v1/runs/{run['run_id']}", headers=operator).json()["approved_by"])

    try:
        store.publish_pending("acme", lambda event_id, event: (_ for _ in ()).throw(RuntimeError("downstream down")))
    except RuntimeError:
        print("INJECTED downstream failure -> events remain pending:", store.pending_count("acme"))
    sent = store.publish_pending("acme", lambda event_id, event: None, limit=100)
    print("RETRY outbox -> delivered:", sent, "pending:", store.pending_count("acme"))
