"""Reproducible 200,000-row staging/diff benchmark. No external data needed."""
import os
import tempfile
import time
import tracemalloc
import sys


def peak_rss_mib():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.PeakWorkingSetSize / 1024 / 1024
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 if sys.platform != "darwin" else 1024 * 1024)
from pathlib import Path

from roster_sync.core import RosterStore, connect, digest


def rows(n, start=0):
    for i in range(start, start + n):
        yield {"employee_id": f"E{i:06}", "name": "Example Employee", "email": f"e{i}@test.invalid",
               "phone": "+919999999999", "manager": "M1", "office_code": "BLR", "shift": "09:00",
               "source_version": 1}


def existing_rows(n):
    for item in rows(n):
        yield ("T1", item["employee_id"], item["name"], item["email"], item["phone"],
               item["manager"], item["office_code"], item["shift"], None, None,
               1, item["source_version"], digest(item))


if __name__ == "__main__":
    n = int(os.environ.get("BENCH_ROWS", "200000"))
    with tempfile.TemporaryDirectory() as tmp:
        store = RosterStore(Path(tmp) / "bench.db")
        store.add_office("T1", "BLR")
        # Setup is outside the measured interval. This produces a real 200k-row diff.
        with connect(store.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", existing_rows(n))
            db.commit()
        tracemalloc.start()
        start = time.perf_counter()
        shifted = max(1, n // 200)
        run = store.begin_snapshot("T1", 2, rows(n, shifted))
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1]
        print(f"existing={n} export={n} missing={run.missing} new={shifted} "
              f"stage_and_diff_seconds={elapsed:.3f} rows_per_second={n/elapsed:.0f} "
              f"peak_python_mib={peak/1024/1024:.3f} peak_process_mib={peak_rss_mib():.1f} "
              f"status={run.status} invalid={run.invalid}")
