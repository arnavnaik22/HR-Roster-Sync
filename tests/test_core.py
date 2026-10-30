import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from roster_sync.api import issue_token, verify_token
from roster_sync.core import RosterError, RosterStore, connect


def event(employee="E1", version=1, **changes):
    data = dict(employee_id=employee, name="Example Employee", email="example@test.invalid",
                phone="+919999999999", manager="M1", office_code="BLR", shift="09:00",
                source_version=version)
    data.update(changes)
    return data


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RosterStore(Path(self.tmp.name) / "test.db")
        self.store.add_office("T1", "BLR")
        self.store.add_office("T2", "BLR")

    def test_replay_stale_and_conflict(self):
        self.assertEqual(self.store.apply_event("T1", event(version=7, shift="10:00")), "APPLIED")
        before = self.store.pending_count("T1")
        self.assertEqual(self.store.apply_event("T1", event(version=7, shift="10:00")), "DUPLICATE")
        self.assertEqual(self.store.apply_event("T1", event(version=6)), "STALE")
        self.assertEqual(self.store.pending_count("T1"), before)
        self.assertEqual(self.store.employee("T1", "E1")["shift"], "10:00")
        with self.assertRaisesRegex(RosterError, "VERSION_CONFLICT"):
            self.store.apply_event("T1", event(version=7, shift="11:00"))

    def test_explicit_create_update_deactivate_contract(self):
        with self.assertRaisesRegex(RosterError, "UNKNOWN_EMPLOYEE"):
            self.store.apply_event("T1", event(version=1, type="UPDATE"))
        self.assertEqual(self.store.apply_event("T1", event(version=1, type="CREATE")), "APPLIED")
        self.assertEqual(self.store.apply_event("T1", event(version=1, type="CREATE")), "DUPLICATE")
        self.assertEqual(self.store.apply_event("T1", event(version=1, type="UPSERT")), "DUPLICATE")
        with self.assertRaisesRegex(RosterError, "EMPLOYEE_EXISTS"):
            self.store.apply_event("T1", event(version=2, type="CREATE"))
        self.assertEqual(self.store.apply_event("T1", event(version=2, type="UPDATE", shift="18:00")), "APPLIED")
        self.assertEqual(self.store.employee("T1", "E1")["shift"], "18:00")

    def test_transport_fields_survive_hr_update(self):
        self.store.apply_event("T1", event())
        self.store.set_transport_fields("T1", "E1", "PICKUP-A", "WHEELCHAIR")
        self.store.apply_event("T1", event(version=2, office_code="BLR", shift="18:00"))
        row = self.store.employee("T1", "E1")
        self.assertEqual((row["pickup_point"], row["special_assistance"]), ("PICKUP-A", "WHEELCHAIR"))
        self.assertEqual(row["shift"], "18:00")

    def test_invalid_record_does_not_fail_snapshot(self):
        rows = [event("E1"), event("E2", email="bad"), event("E3", office_code="UNKNOWN")]
        run = self.store.begin_snapshot("T1", 10, rows)
        self.assertEqual((run.total, run.invalid, run.status), (3, 2, "READY"))
        self.assertEqual({e["code"] for e in self.store.errors("T1", run.run_id)}, {"INVALID_EMAIL", "UNKNOWN_OFFICE"})
        self.store.apply_snapshot("T1", run.run_id)
        self.assertIsNotNone(self.store.employee("T1", "E1"))
        self.assertIsNone(self.store.employee("T1", "E2"))

    def test_safety_brake_and_approval(self):
        for i in range(10):
            self.store.apply_event("T1", event(f"E{i}"))
        run = self.store.begin_snapshot("T1", 10, [event(f"E{i}") for i in range(6)])
        self.assertEqual((run.status, run.missing), ("HELD", 4))
        with self.assertRaisesRegex(RosterError, "APPROVAL_REQUIRED"):
            self.store.apply_snapshot("T1", run.run_id)
        self.assertEqual(self.store.employee("T1", "E9")["active"], 1)
        self.assertEqual(self.store.apply_snapshot("T1", run.run_id, approved=True).status, "APPLIED")
        self.assertEqual(self.store.employee("T1", "E9")["active"], 0)
        self.assertEqual(self.store.apply_snapshot("T1", run.run_id, approved=True).applied, 4)

    def test_older_snapshot_cannot_undo_newer_source_data(self):
        self.store.apply_event("T1", event(version=1))
        new_run = self.store.begin_snapshot("T1", 10, [event(version=1)])
        self.store.apply_snapshot("T1", new_run.run_id)
        self.store.apply_event("T1", event(version=12, shift="18:00"))
        old_run = self.store.begin_snapshot("T1", 9, [])
        with self.assertRaisesRegex(RosterError, "STALE_SNAPSHOT"):
            self.store.apply_snapshot("T1", old_run.run_id, approved=True)
        self.assertEqual(self.store.employee("T1", "E1")["shift"], "18:00")

    def test_crash_rolls_back_employee_and_event(self):
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.store.apply_event("T1", event(), fail_after_write=True)
        self.assertIsNone(self.store.employee("T1", "E1"))
        self.assertEqual(self.store.pending_count("T1"), 0)
        self.store.apply_event("T1", event())
        seen = []
        def fail(event_id, payload):
            seen.append(event_id)
            raise RuntimeError("publisher down")
        with self.assertRaises(RuntimeError):
            self.store.publish_pending("T1", fail)
        self.assertEqual(self.store.pending_count("T1"), 1)
        self.store.publish_pending("T1", lambda event_id, payload: seen.append(event_id))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(self.store.pending_count("T1"), 0)

    def test_concurrent_versions_cannot_regress(self):
        self.store.apply_event("T1", event(version=1))
        barrier = threading.Barrier(3)
        results = []
        def apply(v):
            barrier.wait()
            results.append(self.store.apply_event("T1", event(version=v, shift=f"{v}:00")))
        a = threading.Thread(target=apply, args=(2,))
        b = threading.Thread(target=apply, args=(3,))
        a.start(); b.start(); barrier.wait(); a.join(); b.join()
        self.assertEqual(self.store.employee("T1", "E1")["source_version"], 3)
        self.assertIn("APPLIED", results)

    def test_tenant_isolation_and_token(self):
        self.store.apply_event("T1", event())
        self.assertIsNone(self.store.employee("T2", "E1"))
        self.assertEqual(self.store.pending_count("T2"), 0)
        token = issue_token("a" * 32, "T1", "hr")
        self.assertEqual(verify_token("a" * 32, token)["tenant"], "T1")
        with self.assertRaisesRegex(RosterError, "UNAUTHORIZED"):
            verify_token("a" * 32, token + "x")

    def test_deactivation_cancels_future_bookings(self):
        self.store.apply_event("T1", event())
        with connect(self.store.path) as db:
            db.execute("INSERT INTO bookings VALUES (?,?,?,?,?)", ("B1", "T1", "E1", "2099-01-01 10:00:00", "CONFIRMED"))
        self.store.apply_event("T1", {"employee_id": "E1", "type": "DEACTIVATE", "source_version": 2})
        with connect(self.store.path) as db:
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id='B1'").fetchone()[0], "CANCELLED")
        self.assertEqual(self.store.pending_count("T1"), 3)


if __name__ == "__main__":
    unittest.main()
