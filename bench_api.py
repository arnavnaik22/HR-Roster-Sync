"""Measure end-to-end in-process HTTP latency with FastAPI TestClient."""
import math
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from roster_sync.api import create_app, issue_token
from roster_sync.core import RosterStore


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * q) - 1)]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        store = RosterStore(Path(tmp) / "api-bench.db")
        store.add_office("T1", "BLR")
        client = TestClient(create_app(store, "a" * 32, relay_enabled=False))
        headers = {"Authorization": "Bearer " + issue_token("a" * 32, "T1", "hr")}
        latencies = []
        begin = time.perf_counter()
        for i in range(1000):
            data = {"employee_id": f"E{i}", "name": "Example Employee", "email": f"e{i}@test.invalid",
                    "phone": "+919999999999", "manager": "M1", "office_code": "BLR",
                    "shift": "09:00", "source_version": 1}
            start = time.perf_counter()
            response = client.post("/v1/events", json=data, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(response.text)
            latencies.append((time.perf_counter() - start) * 1000)
        elapsed = time.perf_counter() - begin
        print(f"requests=1000 throughput_per_second={1000/elapsed:.1f} "
              f"p50_ms={percentile(latencies, .5):.2f} p99_ms={percentile(latencies, .99):.2f}")
