"""FastAPI boundary around the deterministic roster core.

The signed demo tokens are deliberately small. An identity provider should issue
tenant and role claims in a real deployment; no personal data enters logs.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import RosterError, RosterStore, connect


def issue_token(secret: str, tenant: str, role: str, subject: str = "demo-user", ttl: int = 3600) -> str:
    if role not in {"employee", "admin", "operator", "hr"} or not tenant or not subject:
        raise ValueError("invalid token claims")
    payload = base64.urlsafe_b64encode(json.dumps(
        {"tenant": tenant, "role": role, "sub": subject, "exp": int(time.time()) + ttl},
        separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def verify_token(secret: str, token: str) -> dict:
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if (claims["exp"] < time.time() or not claims.get("tenant") or not claims.get("sub")
                or claims["role"] not in {"employee", "admin", "operator", "hr"}):
            raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error):
        raise RosterError("UNAUTHORIZED", 401)


def publisher_for(url: str):
    def publish(event_id: str, event: dict):
        if url:
            request = urllib.request.Request(
                url, data=json.dumps(event).encode(), method="POST",
                headers={"Content-Type": "application/json", "Idempotency-Key": event_id})
            with urllib.request.urlopen(request, timeout=3) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("downstream rejected event")
        else:
            print(json.dumps({"kind": "event", "id": event_id, "type": event["type"]}), flush=True)
    return publish


def create_app(store: RosterStore, secret: str, event_url: str = "", relay_enabled: bool = True,
               log_requests: bool = False) -> FastAPI:
    if len(secret) < 32:
        raise ValueError("ROSTER_SECRET must have at least 32 characters")
    stop = threading.Event()

    def relay_loop():
        publish = publisher_for(event_url)
        while not stop.is_set():
            try:
                with connect(store.path) as db:
                    tenants = [row[0] for row in db.execute(
                        "SELECT DISTINCT tenant_id FROM outbox WHERE published_at IS NULL")]
                for tenant in tenants:
                    store.publish_pending(tenant, publish)
            except Exception as exc:
                print(json.dumps({"kind": "outbox_error", "error": type(exc).__name__}), flush=True)
            stop.wait(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = None
        if relay_enabled:
            thread = threading.Thread(target=relay_loop, daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=4)

    app = FastAPI(title="MoveInSync HR Roster Sync", version="1.0.0", lifespan=lifespan)

    if log_requests:
        @app.middleware("http")
        async def structured_request_log(request: Request, call_next):
            start = time.perf_counter()
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                print(json.dumps({"kind": "http", "method": request.method,
                                  "status": locals().get("status", 500),
                                  "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}), flush=True)

    @app.exception_handler(RosterError)
    async def roster_error_handler(request: Request, exc: RosterError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code})

    def principal(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise RosterError("UNAUTHORIZED", 401)
        return verify_token(secret, authorization[7:])

    def require(*roles: str):
        def check(claims: dict = Depends(principal)) -> dict:
            if claims["role"] not in roles:
                raise RosterError("FORBIDDEN", 403)
            return claims
        return check

    @app.get("/v1/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/offices")
    def add_office(body: dict[str, Any], claims: dict = Depends(require("operator"))):
        office = body.get("office_code")
        if not isinstance(office, str) or not office or len(office) > 80:
            raise RosterError("INVALID_OFFICE_CODE")
        store.add_office(claims["tenant"], office)
        return {"result": "CREATED"}

    @app.post("/v1/events")
    def event(body: dict[str, Any], claims: dict = Depends(require("hr"))):
        return {"result": store.apply_event(claims["tenant"], body)}

    @app.post("/v1/snapshots")
    async def snapshot(request: Request, version: int, claims: dict = Depends(require("hr"))):
        if version < 0:
            raise RosterError("INVALID_VERSION")
        # SpooledTemporaryFile keeps at most 1 MiB in RAM and spills to disk.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as spool:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise RosterError("BODY_TOO_LARGE", 413)
                spool.write(chunk)
            spool.seek(0)
            text = io.TextIOWrapper(spool, encoding="utf-8-sig", newline="")
            try:
                result = await run_in_threadpool(store.snapshot_csv, claims["tenant"], version, text)
            finally:
                text.detach()
        return result.__dict__

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, claims: dict = Depends(require("hr", "admin", "operator"))):
        return store.run(claims["tenant"], run_id)

    @app.get("/v1/runs/{run_id}/errors")
    def get_errors(run_id: str, claims: dict = Depends(require("hr", "admin", "operator"))):
        return store.errors(claims["tenant"], run_id)

    @app.post("/v1/runs/{run_id}/apply")
    def apply_run(run_id: str, body: dict[str, Any], claims: dict = Depends(require("operator"))):
        approved = body.get("approve_missing") is True
        result = store.apply_snapshot(claims["tenant"], run_id, approved, claims["sub"] if approved else None)
        return result.__dict__

    @app.get("/v1/employees/{employee_id}")
    def get_employee(employee_id: str, claims: dict = Depends(require("hr", "admin", "operator"))):
        row = store.employee(claims["tenant"], employee_id)
        if row is None:
            raise RosterError("UNKNOWN_EMPLOYEE", 404)
        # Return only what an operator needs. Email, phone, and source hash stay private.
        return {key: row[key] for key in (
            "employee_id", "name", "office_code", "shift", "active", "source_version",
            "pickup_point", "special_assistance")}

    @app.post("/v1/employees/{employee_id}/transport")
    def transport_fields(employee_id: str, body: dict[str, Any], claims: dict = Depends(require("admin", "operator"))):
        pickup, assistance = body.get("pickup_point"), body.get("special_assistance")
        if any(value is not None and (not isinstance(value, str) or len(value) > 255) for value in (pickup, assistance)):
            raise RosterError("INVALID_TRANSPORT_FIELD")
        store.set_transport_fields(claims["tenant"], employee_id, pickup, assistance)
        return {"result": "UPDATED"}

    @app.get("/v1/metrics")
    def metrics(claims: dict = Depends(require("admin", "operator"))):
        return store.metrics(claims["tenant"])

    return app


def serve():
    secret = os.environ.get("ROSTER_SECRET")
    if not secret or len(secret) < 32:
        raise SystemExit("Set ROSTER_SECRET to at least 32 characters")
    store = RosterStore(os.environ.get("ROSTER_DB", "roster.db"),
                        float(os.environ.get("ROSTER_MISSING_THRESHOLD", "0.30")))
    app = create_app(store, secret, os.environ.get("ROSTER_EVENT_URL", ""), log_requests=True)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
