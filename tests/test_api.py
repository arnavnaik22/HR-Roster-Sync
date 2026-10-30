import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from roster_sync.api import issue_token, make_handler
from roster_sync.core import RosterStore


class ApiTests(unittest.TestCase):
    def test_auth_roles_and_tenant_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RosterStore(Path(tmp) / "api.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "s" * 32))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def call(path, method="GET", body=None, tenant="T1", role="operator"):
                token = issue_token("s" * 32, tenant, role)
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(base + path, data=data, method=method,
                                             headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3) as response:
                    return json.load(response)
            try:
                call("/v1/offices", "POST", {"office_code": "BLR"})
                item = {"employee_id": "E1", "name": "Demo Employee", "email": "demo@test.invalid",
                        "phone": "+919999999999", "manager": "M1", "office_code": "BLR",
                        "shift": "09:00", "source_version": 1}
                self.assertEqual(call("/v1/events", "POST", item, role="hr")["result"], "APPLIED")
                self.assertEqual(call("/v1/employees/E1")["employee_id"], "E1")
                with self.assertRaises(urllib.error.HTTPError) as other_tenant:
                    call("/v1/employees/E1", tenant="T2")
                self.assertEqual(other_tenant.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as wrong_role:
                    call("/v1/events", "POST", item, role="employee")
                self.assertEqual(wrong_role.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
