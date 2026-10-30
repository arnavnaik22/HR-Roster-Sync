"""Measure single-process event write latency and throughput."""
import math
import statistics
import tempfile
import time
from pathlib import Path
from roster_sync.core import RosterStore


def pct(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        store = RosterStore(Path(tmp) / "events.db")
        store.add_office("T1", "BLR")
        latencies = []
        start = time.perf_counter()
        for i in range(1000):
            event = {"employee_id": f"E{i}", "name": "Example Employee", "email": f"e{i}@test.invalid",
                     "phone": "+919999999999", "manager": "M1", "office_code": "BLR", "shift": "09:00", "source_version": 1}
            t = time.perf_counter()
            store.apply_event("T1", event)
            latencies.append((time.perf_counter() - t) * 1000)
        elapsed = time.perf_counter() - start
        print(f"events=1000 throughput_per_second={1000/elapsed:.1f} p50_ms={pct(latencies,.5):.2f} p99_ms={pct(latencies,.99):.2f}")
