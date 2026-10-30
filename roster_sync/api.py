from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import RosterError, RosterStore


def issue_token(secret: str, tenant: str, role: str, ttl: int = 3600) -> str:
    if role not in {"employee", "admin", "operator", "hr"}:
        raise ValueError("invalid role")
    payload = base64.urlsafe_b64encode(json.dumps({"tenant": tenant, "role": role, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def verify_token(secret: str, token: str) -> dict:
    try:
        payload, sig = token.split(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] < time.time() or not data.get("tenant") or data["role"] not in {"employee", "admin", "operator", "hr"}:
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error):
        raise RosterError("UNAUTHORIZED", 401)


def make_handler(store: RosterStore, secret: str):
    class LimitedReader(io.RawIOBase):
        def __init__(self, source, length):
            self.source, self.remaining = source, length

        def readable(self):
            return True

        def readinto(self, buffer):
            if self.remaining <= 0:
                return 0
            data = self.source.read(min(len(buffer), self.remaining))
            buffer[:len(data)] = data
            self.remaining -= len(data)
            return len(data)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log URL paths, request bodies, email, phone, or home locations.
            print(json.dumps({"kind": "http", "method": self.command, "status": args[1] if len(args) > 1 else "?"}))

        def send_json(self, status: int, obj: dict | list):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def auth(self, roles: set[str]) -> dict:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise RosterError("UNAUTHORIZED", 401)
            claim = verify_token(secret, header[7:])
            if claim["role"] not in roles:
                raise RosterError("FORBIDDEN", 403)
            return claim

        def json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise RosterError("BODY_TOO_LARGE", 413)
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (json.JSONDecodeError, ValueError):
                raise RosterError("INVALID_JSON")

        def do_GET(self):
            try:
                path = urlparse(self.path).path.split("/")
                if path[:3] == ["", "v1", "employees"] and len(path) == 4:
                    claim = self.auth({"admin", "operator", "hr"})
                    row = store.employee(claim["tenant"], path[3])
                    if row is None:
                        raise RosterError("UNKNOWN_EMPLOYEE", 404)
                    # API responses avoid exposing source hashes and sensitive fields by default.
                    self.send_json(200, {k: row[k] for k in ("employee_id", "name", "office_code", "shift", "active", "source_version", "pickup_point", "special_assistance")})
                elif path[:3] == ["", "v1", "runs"] and len(path) == 5 and path[4] == "errors":
                    claim = self.auth({"admin", "operator", "hr"})
                    self.send_json(200, store.errors(claim["tenant"], path[3]))
                elif path == ["", "v1", "metrics"]:
                    claim = self.auth({"admin", "operator"})
                    self.send_json(200, {"outbox_pending": store.pending_count(claim["tenant"])})
                else:
                    raise RosterError("NOT_FOUND", 404)
            except RosterError as e:
                self.send_json(e.status, {"error": e.code})

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path.split("/")
                if path == ["", "v1", "events"]:
                    claim = self.auth({"hr"})
                    result = store.apply_event(claim["tenant"], self.json_body())
                    self.send_json(200, {"result": result})
                elif path == ["", "v1", "offices"]:
                    claim = self.auth({"operator"})
                    office = self.json_body().get("office_code", "")
                    if not isinstance(office, str) or not office or len(office) > 80:
                        raise RosterError("INVALID_OFFICE_CODE")
                    store.add_office(claim["tenant"], office)
                    self.send_json(200, {"result": "CREATED"})
                elif path == ["", "v1", "snapshots"]:
                    claim = self.auth({"hr"})
                    version = int(parse_qs(parsed.query).get("version", [""])[0])
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 50 * 1024 * 1024:
                        raise RosterError("BODY_TOO_LARGE", 413)
                    # The reader never buffers the whole CSV in application memory.
                    stream = io.TextIOWrapper(io.BufferedReader(LimitedReader(self.rfile, length)), encoding="utf-8-sig", newline="")
                    result = store.snapshot_csv(claim["tenant"], version, stream)
                    self.send_json(200, result.__dict__)
                elif path[:3] == ["", "v1", "runs"] and len(path) == 5 and path[4] == "apply":
                    claim = self.auth({"operator"})
                    approved = bool(self.json_body().get("approve_missing", False))
                    result = store.apply_snapshot(claim["tenant"], path[3], approved)
                    self.send_json(200, result.__dict__)
                elif path[:3] == ["", "v1", "employees"] and len(path) == 5 and path[4] == "transport":
                    claim = self.auth({"admin", "operator"})
                    data = self.json_body()
                    store.set_transport_fields(claim["tenant"], path[3], data.get("pickup_point"), data.get("special_assistance"))
                    self.send_json(200, {"result": "UPDATED"})
                elif path == ["", "v1", "outbox", "drain"]:
                    claim = self.auth({"operator"})
                    # Demo publisher; a production adapter publishes to Kafka and retries failures.
                    n = store.publish_pending(claim["tenant"], lambda event_id, event: print(json.dumps({"kind": "event", "id": event_id, "type": event["type"]})))
                    self.send_json(200, {"published": n})
                else:
                    raise RosterError("NOT_FOUND", 404)
            except (ValueError, UnicodeDecodeError):
                self.send_json(400, {"error": "INVALID_REQUEST"})
            except RosterError as e:
                self.send_json(e.status, {"error": e.code})

    return Handler


def serve():
    secret = os.environ.get("ROSTER_SECRET")
    if not secret or len(secret) < 32:
        raise SystemExit("Set ROSTER_SECRET to at least 32 characters")
    store = RosterStore(os.environ.get("ROSTER_DB", "roster.db"))
    event_url = os.environ.get("ROSTER_EVENT_URL", "")

    def publish(event_id, event):
        if event_url:
            req = urllib.request.Request(event_url, data=json.dumps(event).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Idempotency-Key": event_id})
            with urllib.request.urlopen(req, timeout=3) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("downstream rejected event")
        else:
            print(json.dumps({"kind": "event", "id": event_id, "type": event["type"]}), flush=True)

    def relay():
        while True:
            try:
                with connect(store.path) as db:
                    tenants = [r[0] for r in db.execute("SELECT DISTINCT tenant_id FROM outbox WHERE published_at IS NULL")]
                for tenant in tenants:
                    store.publish_pending(tenant, publish)
            except Exception as exc:
                print(json.dumps({"kind": "outbox_error", "error": type(exc).__name__}), flush=True)
            time.sleep(1)

    from .core import connect
    threading.Thread(target=relay, daemon=True).start()
    print("Roster Sync listening on 0.0.0.0:8000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), make_handler(store, secret)).serve_forever()
