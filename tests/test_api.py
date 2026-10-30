import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from roster_sync.api import create_app, issue_token
from roster_sync.core import RosterStore


def record(employee_id="E1", version=1):
    return {"employee_id": employee_id, "name": "Demo Employee", "email": "demo@test.invalid",
            "phone": "+919999999999", "manager": "M1", "office_code": "BLR",
            "shift": "09:00", "source_version": version}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RosterStore(Path(self.tmp.name) / "api.db")
        self.client = TestClient(create_app(self.store, "s" * 32, relay_enabled=False))

    def auth(self, tenant="T1", role="operator", subject="tester"):
        return {"Authorization": "Bearer " + issue_token("s" * 32, tenant, role, subject)}

    def test_auth_roles_and_tenant_boundary(self):
        self.assertEqual(self.client.post("/v1/offices", json={"office_code": "BLR"}, headers=self.auth()).status_code, 200)
        self.assertEqual(self.client.post("/v1/events", json=record(), headers=self.auth(role="hr")).json()["result"], "APPLIED")
        self.assertEqual(self.client.get("/v1/employees/E1", headers=self.auth()).json()["employee_id"], "E1")
        self.assertEqual(self.client.get("/v1/employees/E1", headers=self.auth(tenant="T2")).status_code, 404)
        self.assertEqual(self.client.post("/v1/events", json=record(), headers=self.auth(role="employee")).status_code, 403)
        self.assertEqual(self.client.get("/v1/metrics").status_code, 401)
        self.assertEqual(self.client.get("/v1/metrics", headers={"Authorization": "Bearer garbage"}).status_code, 401)
        self.assertNotIn("phone", self.client.get("/v1/employees/E1", headers=self.auth()).json())

    def test_csv_stream_hold_and_approval_audit(self):
        self.store.add_office("T1", "BLR")
        for i in range(10):
            self.store.apply_event("T1", record(f"E{i}"))
        header = "employee_id,name,email,phone,manager,office_code,shift,source_version\n"
        rows = "".join(f"E{i},Demo Employee,demo{i}@test.invalid,+919999999999,M1,BLR,09:00,1\n" for i in range(6))
        response = self.client.post("/v1/snapshots?version=10", content=(header + rows).encode(),
                                    headers={**self.auth(role="hr"), "Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 200)
        run = response.json()
        self.assertEqual((run["status"], run["missing"]), ("HELD", 4))
        self.assertEqual(self.client.post(f"/v1/runs/{run['run_id']}/apply", json={"approve_missing": False},
                                          headers=self.auth()).status_code, 409)
        self.assertEqual(self.store.employee("T1", "E9")["active"], 1)
        applied = self.client.post(f"/v1/runs/{run['run_id']}/apply", json={"approve_missing": True},
                                   headers=self.auth(subject="operator-123"))
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(self.client.get(f"/v1/runs/{run['run_id']}", headers=self.auth()).json()["approved_by"], "operator-123")
        self.assertEqual(self.store.employee("T1", "E9")["active"], 0)


if __name__ == "__main__":
    unittest.main()
