from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
import uuid
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS offices (
 tenant_id TEXT NOT NULL, office_code TEXT NOT NULL,
 PRIMARY KEY (tenant_id, office_code));
CREATE TABLE IF NOT EXISTS employees (
 tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL,
 name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
 manager TEXT NOT NULL, office_code TEXT NOT NULL, shift TEXT NOT NULL,
 pickup_point TEXT, special_assistance TEXT,
 active INTEGER NOT NULL, source_version INTEGER NOT NULL, source_hash TEXT NOT NULL,
 PRIMARY KEY (tenant_id, employee_id));
CREATE INDEX IF NOT EXISTS employee_active ON employees(tenant_id, active);
CREATE TABLE IF NOT EXISTS bookings (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL,
 starts_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS booking_employee ON bookings(tenant_id, employee_id, starts_at);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, employee_id TEXT NOT NULL,
 event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
 published_at REAL);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(published_at, created_at);
CREATE TABLE IF NOT EXISTS sync_runs (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, snapshot_version INTEGER NOT NULL,
 status TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
 invalid INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0,
 applied INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL,
 approved_by TEXT, applied_at REAL);
CREATE TABLE IF NOT EXISTS tenant_watermarks (
 tenant_id TEXT PRIMARY KEY, snapshot_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot_rows (
 run_id TEXT NOT NULL, employee_id TEXT NOT NULL, row_number INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(run_id, employee_id));
CREATE INDEX IF NOT EXISTS snapshot_run ON snapshot_rows(run_id);
CREATE TABLE IF NOT EXISTS sync_errors (
 run_id TEXT NOT NULL, row_number INTEGER NOT NULL,
 employee_id TEXT, code TEXT NOT NULL);
"""

FIELDS = ("employee_id", "name", "email", "phone", "manager", "office_code", "shift", "source_version")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE = re.compile(r"^\+?[0-9]{10,15}$")
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class RosterError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


@dataclass
class SyncResult:
    run_id: str
    status: str
    total: int
    invalid: int
    missing: int
    applied: int


class RosterStore:
    def __init__(self, path: str | Path = "roster.db", missing_threshold: float = 0.30):
        if not 0 < missing_threshold <= 1:
            raise ValueError("missing_threshold must be in (0, 1]")
        self.path = str(path)
        self.missing_threshold = missing_threshold
        self._office_cache: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._office_cache_lock = threading.Lock()
        with connect(self.path) as db:
            db.executescript(SCHEMA)

    def add_office(self, tenant: str, office: str) -> None:
        with connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO offices VALUES (?,?)", (tenant, office))
        with self._office_cache_lock:
            self._office_cache[(tenant, office)] = time.monotonic() + 5
            self._office_cache.move_to_end((tenant, office))
            if len(self._office_cache) > 4096:
                self._office_cache.popitem(last=False)

    def _office_known(self, db: sqlite3.Connection, tenant: str, office: str) -> bool:
        key, now = (tenant, office), time.monotonic()
        with self._office_cache_lock:
            expiry = self._office_cache.get(key, 0)
            if expiry > now:
                self._office_cache.move_to_end(key)
                return True
            self._office_cache.pop(key, None)
        exists = db.execute("SELECT 1 FROM offices WHERE tenant_id=? AND office_code=?", key).fetchone() is not None
        if exists:
            with self._office_cache_lock:
                self._office_cache[key] = now + 5
                self._office_cache.move_to_end(key)
                if len(self._office_cache) > 4096:
                    self._office_cache.popitem(last=False)
        return exists

    def _validate(self, db: sqlite3.Connection, tenant: str, event: dict) -> dict:
        if not ID.fullmatch(str(event.get("employee_id", ""))):
            raise RosterError("INVALID_EMPLOYEE_ID")
        typ = event.get("type", "UPSERT")
        if typ not in ("CREATE", "UPDATE", "UPSERT", "DEACTIVATE"):
            raise RosterError("INVALID_EVENT_TYPE")
        try:
            version = int(event["source_version"])
        except (KeyError, ValueError, TypeError):
            raise RosterError("INVALID_VERSION")
        if version < 0:
            raise RosterError("INVALID_VERSION")
        clean = {"employee_id": str(event["employee_id"]), "type": typ, "source_version": version}
        if typ != "DEACTIVATE":
            for field in FIELDS[1:-1]:
                value = event.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > 255:
                    raise RosterError("INVALID_" + field.upper())
                clean[field] = value.strip()
            if not EMAIL.fullmatch(clean["email"]):
                raise RosterError("INVALID_EMAIL")
            if not PHONE.fullmatch(clean["phone"]):
                raise RosterError("INVALID_PHONE")
            if not self._office_known(db, tenant, clean["office_code"]):
                raise RosterError("UNKNOWN_OFFICE")
        return clean

    def _outbox(self, db: sqlite3.Connection, tenant: str, employee: str, typ: str, version: int, extra: dict | None = None) -> None:
        # Stable event ID lets downstream consumers deduplicate a relay retry.
        event_id = digest([tenant, employee, typ, version])
        payload = {"tenant_id": tenant, "employee_id": employee, "source_version": version, "type": typ}
        if extra:
            payload.update(extra)
        db.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,NULL)",
                   (event_id, tenant, employee, typ, canonical(payload), time.time()))

    def _apply(self, db: sqlite3.Connection, tenant: str, event: dict) -> str:
        clean = self._validate(db, tenant, event)
        employee, version, typ = clean["employee_id"], clean["source_version"], clean["type"]
        row = db.execute("SELECT source_version,source_hash,active FROM employees WHERE tenant_id=? AND employee_id=?", (tenant, employee)).fetchone()
        # CREATE, UPDATE and snapshot UPSERT with the same version and HR fields
        # represent the same source state, regardless of transport envelope.
        event_hash = digest({key: clean[key] for key in FIELDS}) if typ != "DEACTIVATE" else digest(clean)
        if row and version < row["source_version"]:
            return "STALE"
        if row and version == row["source_version"]:
            if event_hash != row["source_hash"]:
                raise RosterError("VERSION_CONFLICT", 409)
            return "DUPLICATE"
        if typ == "CREATE" and row:
            raise RosterError("EMPLOYEE_EXISTS", 409)
        if typ == "UPDATE" and not row:
            raise RosterError("UNKNOWN_EMPLOYEE", 404)
        if typ == "DEACTIVATE":
            if not row:
                raise RosterError("UNKNOWN_EMPLOYEE", 404)
            db.execute("UPDATE employees SET active=0,source_version=?,source_hash=? WHERE tenant_id=? AND employee_id=?",
                       (version, event_hash, tenant, employee))
            count = db.execute("UPDATE bookings SET status='CANCELLED' WHERE tenant_id=? AND employee_id=? AND starts_at>datetime('now') AND status='CONFIRMED'",
                               (tenant, employee)).rowcount
            self._outbox(db, tenant, employee, "EMPLOYEE_DEACTIVATED", version)
            if count:
                self._outbox(db, tenant, employee, "BOOKINGS_CANCELLED", version, {"count": count})
        else:
            db.execute("""INSERT INTO employees
                (tenant_id,employee_id,name,email,phone,manager,office_code,shift,active,source_version,source_hash)
                VALUES (?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(tenant_id,employee_id) DO UPDATE SET
                name=excluded.name,email=excluded.email,phone=excluded.phone,
                manager=excluded.manager,office_code=excluded.office_code,shift=excluded.shift,
                active=1,source_version=excluded.source_version,source_hash=excluded.source_hash""",
                (tenant, employee, clean["name"], clean["email"], clean["phone"], clean["manager"], clean["office_code"], clean["shift"], version, event_hash))
            self._outbox(db, tenant, employee, "EMPLOYEE_CREATED" if not row else "EMPLOYEE_UPDATED", version)
        return "APPLIED"

    def apply_event(self, tenant: str, event: dict, fail_after_write: bool = False) -> str:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply(db, tenant, event)
                if fail_after_write:
                    raise RuntimeError("injected crash before commit")
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def set_transport_fields(self, tenant: str, employee: str, pickup: str | None, assistance: str | None) -> None:
        with connect(self.path) as db:
            changed = db.execute("UPDATE employees SET pickup_point=?,special_assistance=? WHERE tenant_id=? AND employee_id=?",
                                 (pickup, assistance, tenant, employee)).rowcount
            if not changed:
                raise RosterError("UNKNOWN_EMPLOYEE", 404)

    def employee(self, tenant: str, employee: str) -> dict | None:
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM employees WHERE tenant_id=? AND employee_id=?", (tenant, employee)).fetchone()
            return dict(row) if row else None

    def begin_snapshot(self, tenant: str, snapshot_version: int, rows: Iterable[dict]) -> SyncResult:
        run_id = str(uuid.uuid4())
        total = invalid = 0
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO sync_runs(id,tenant_id,snapshot_version,status,started_at) VALUES (?,?,?,?,?)",
                       (run_id, tenant, snapshot_version, "STAGING", time.time()))
            for number, raw in enumerate(rows, 2):
                total += 1
                try:
                    event = dict(raw)
                    event["type"] = "UPSERT"
                    event["source_version"] = int(event.get("source_version") or snapshot_version)
                    clean = self._validate(db, tenant, event)
                    if clean["source_version"] > snapshot_version:
                        raise RosterError("FUTURE_ROW_VERSION")
                    db.execute("INSERT INTO snapshot_rows VALUES (?,?,?,?)", (run_id, clean["employee_id"], number, canonical(clean)))
                except sqlite3.IntegrityError:
                    invalid += 1
                    db.execute("INSERT INTO sync_errors VALUES (?,?,?,?)", (run_id, number, str(raw.get("employee_id", "")), "DUPLICATE_ROW"))
                except (RosterError, ValueError) as exc:
                    invalid += 1
                    code = exc.code if isinstance(exc, RosterError) else "INVALID_VERSION"
                    employee_id = str(raw.get("employee_id", ""))
                    db.execute("INSERT INTO sync_errors VALUES (?,?,?,?)", (run_id, number, employee_id if ID.fullmatch(employee_id) else None, code))
            missing = db.execute("""SELECT COUNT(*) FROM employees e
                WHERE e.tenant_id=? AND e.active=1 AND e.source_version<=?
                AND NOT EXISTS(SELECT 1 FROM snapshot_rows s WHERE s.run_id=? AND s.employee_id=e.employee_id)""",
                (tenant, snapshot_version, run_id)).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM employees WHERE tenant_id=? AND active=1 AND source_version<=?", (tenant, snapshot_version)).fetchone()[0]
            status = "HELD" if active and missing / active >= self.missing_threshold else "READY"
            db.execute("UPDATE sync_runs SET status=?,total=?,invalid=?,missing=? WHERE id=?", (status, total, invalid, missing, run_id))
            db.commit()
        return SyncResult(run_id, status, total, invalid, missing, 0)

    def apply_snapshot(self, tenant: str, run_id: str, approved: bool = False, approved_by: str | None = None) -> SyncResult:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                run = db.execute("SELECT * FROM sync_runs WHERE id=? AND tenant_id=?", (run_id, tenant)).fetchone()
                if not run:
                    raise RosterError("UNKNOWN_RUN", 404)
                if run["status"] == "APPLIED":
                    return SyncResult(run_id, "APPLIED", run["total"], run["invalid"], run["missing"], run["applied"])
                watermark = db.execute("SELECT snapshot_version FROM tenant_watermarks WHERE tenant_id=?", (tenant,)).fetchone()
                if watermark and run["snapshot_version"] <= watermark[0]:
                    raise RosterError("STALE_SNAPSHOT", 409)
                if run["status"] == "HELD" and not approved:
                    raise RosterError("APPROVAL_REQUIRED", 409)
                applied = 0
                apply_invalid = 0
                for row in db.execute("SELECT employee_id,row_number,payload FROM snapshot_rows WHERE run_id=? ORDER BY employee_id", (run_id,)):
                    event = json.loads(row["payload"])
                    try:
                        if self._apply(db, tenant, event) == "APPLIED":
                            applied += 1
                    except RosterError as exc:
                        apply_invalid += 1
                        db.execute("INSERT INTO sync_errors VALUES (?,?,?,?)",
                                   (run_id, row["row_number"], row["employee_id"], exc.code))
                # Recheck against current state in this write transaction; newer events win.
                missing_cursor = db.execute("""SELECT e.employee_id FROM employees e
                    WHERE e.tenant_id=? AND e.active=1 AND e.source_version<=?
                    AND NOT EXISTS(SELECT 1 FROM snapshot_rows s WHERE s.run_id=? AND s.employee_id=e.employee_id)""",
                    (tenant, run["snapshot_version"], run_id))
                while batch := missing_cursor.fetchmany(1000):
                    for row in batch:
                        # A row already at the watermark may represent a newer live update.
                        current = db.execute("SELECT source_version FROM employees WHERE tenant_id=? AND employee_id=?", (tenant, row[0])).fetchone()[0]
                        if current >= run["snapshot_version"]:
                            continue
                        if self._apply(db, tenant, {"employee_id": row[0], "type": "DEACTIVATE", "source_version": run["snapshot_version"]}) == "APPLIED":
                            applied += 1
                invalid_total = run["invalid"] + apply_invalid
                db.execute("UPDATE sync_runs SET status='APPLIED',applied=?,invalid=?,approved_by=?,applied_at=? WHERE id=?",
                           (applied, invalid_total, approved_by if approved else None, time.time(), run_id))
                db.execute("INSERT INTO tenant_watermarks VALUES (?,?) ON CONFLICT(tenant_id) DO UPDATE SET snapshot_version=excluded.snapshot_version",
                           (tenant, run["snapshot_version"]))
                db.execute("DELETE FROM snapshot_rows WHERE run_id=?", (run_id,))
                db.commit()
                return SyncResult(run_id, "APPLIED", run["total"], invalid_total, run["missing"], applied)
            except BaseException:
                db.rollback()
                raise

    def snapshot_csv(self, tenant: str, snapshot_version: int, stream: io.TextIOBase) -> SyncResult:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or any(k not in reader.fieldnames for k in FIELDS[:-1]):
            raise RosterError("INVALID_CSV_HEADER")
        return self.begin_snapshot(tenant, snapshot_version, reader)

    def errors(self, tenant: str, run_id: str) -> list[dict]:
        with connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sync_runs WHERE id=? AND tenant_id=?", (run_id, tenant)).fetchone():
                raise RosterError("UNKNOWN_RUN", 404)
            return [dict(r) for r in db.execute("SELECT row_number,employee_id,code FROM sync_errors WHERE run_id=?", (run_id,))]

    def run(self, tenant: str, run_id: str) -> dict:
        with connect(self.path) as db:
            row = db.execute("SELECT id,snapshot_version,status,total,invalid,missing,applied,approved_by,applied_at FROM sync_runs WHERE id=? AND tenant_id=?",
                             (run_id, tenant)).fetchone()
            if not row:
                raise RosterError("UNKNOWN_RUN", 404)
            return dict(row)

    def metrics(self, tenant: str) -> dict:
        with connect(self.path) as db:
            pending = db.execute("SELECT COUNT(*),MIN(created_at) FROM outbox WHERE tenant_id=? AND published_at IS NULL", (tenant,)).fetchone()
            held = db.execute("SELECT COUNT(*) FROM sync_runs WHERE tenant_id=? AND status='HELD'", (tenant,)).fetchone()[0]
            invalid = db.execute("SELECT COALESCE(SUM(invalid),0) FROM sync_runs WHERE tenant_id=?", (tenant,)).fetchone()[0]
            return {"outbox_pending": pending[0], "outbox_oldest_age_seconds": round(time.time() - pending[1], 3) if pending[1] else 0,
                    "held_runs": held, "invalid_rows_total": invalid}

    def pending_count(self, tenant: str) -> int:
        with connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE tenant_id=? AND published_at IS NULL", (tenant,)).fetchone()[0]

    def publish_pending(self, tenant: str, publisher: Callable[[str, dict], None], limit: int = 100) -> int:
        sent = 0
        with connect(self.path) as db:
            rows = db.execute("SELECT id,payload FROM outbox WHERE tenant_id=? AND published_at IS NULL ORDER BY created_at,id LIMIT ?", (tenant, limit)).fetchall()
            for row in rows:
                # A crash after publish but before marking sent can redeliver. Consumers use event ID.
                publisher(row["id"], json.loads(row["payload"]))
                db.execute("UPDATE outbox SET published_at=? WHERE id=? AND published_at IS NULL", (time.time(), row["id"]))
                sent += 1
        return sent
