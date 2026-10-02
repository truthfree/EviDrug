"""Linux-only, DB/LLM-free smoke and resource measurement for the pinned runtime."""

from __future__ import annotations

import argparse
import json
import os
import select
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SMILES = "CC/C(=C(\\c1ccccc1)c1ccc(OCCN(C)C)cc1)c1ccccc1"
COMMIT = "2a31aa119e27b6b69a5588d18a01f2a27fef4524"
CHANNELS = {"hERG", "Nav1.5", "Cav1.2"}
CGROUP_MEMORY = Path("/sys/fs/cgroup/memory.current")


def _rss_mib(pid: int) -> float:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return 0.0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


class Sampler:
    def __init__(self) -> None:
        self.pids: set[int] = set()
        self.peak_by_pid: dict[int, float] = {}
        self.total_peak_mib = 0.0
        self.cgroup_peak_mib: float | None = None
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(0.01):
            with self.lock:
                pids = tuple(self.pids)
            values = {pid: _rss_mib(pid) for pid in pids}
            try:
                cgroup_mib = int(CGROUP_MEMORY.read_text().strip()) / (1024 * 1024)
            except (FileNotFoundError, ValueError):
                cgroup_mib = None
            with self.lock:
                for pid, value in values.items():
                    self.peak_by_pid[pid] = max(self.peak_by_pid.get(pid, 0.0), value)
                self.total_peak_mib = max(self.total_peak_mib, sum(values.values()))
                if cgroup_mib is not None:
                    self.cgroup_peak_mib = max(self.cgroup_peak_mib or 0.0, cgroup_mib)


def _read_response(process: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    assert process.stdout is not None
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise TimeoutError("CToxPred2 smoke response timed out")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"CToxPred2 runtime exited before response: {process.poll()}")
    reply = json.loads(line)
    report = reply.get("report", {})
    if (
        reply.get("protocol") != 1
        or report.get("status") != "passed"
        or report.get("upstream_commit") != COMMIT
        or report.get("canonical_smiles") != SMILES
        or set(report.get("predictions", {})) != CHANNELS
    ):
        raise RuntimeError("CToxPred2 smoke returned unexpected provenance or channels")
    return reply


def _one_worker(index: int, iterations: int, sampler: Sampler) -> dict[str, Any]:
    process = subprocess.Popen(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "runtime.py"),
            "--artifact-root",
            str(ROOT / "artifacts"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PYTHONUNBUFFERED": "1",
        },
    )
    with sampler.lock:
        sampler.pids.add(process.pid)
    latencies: list[float] = []
    try:
        assert process.stdin is not None
        for attempt in range(iterations):
            request = {
                "protocol": 1,
                "request_id": f"smoke-{index}-{attempt}",
                "arguments": {"schema_version": "1", "canonical_smiles": SMILES},
            }
            start = time.monotonic()
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
            reply = _read_response(process, timeout=120)
            if reply.get("request_id") != request["request_id"]:
                raise RuntimeError("CToxPred2 smoke response ID mismatch")
            latencies.append(time.monotonic() - start)
        return {
            "worker_index": index,
            "pid": process.pid,
            "requests": len(latencies),
            "cold_seconds": round(latencies[0], 3),
            "warm_median_seconds": (
                round(statistics.median(latencies[1:]), 3) if len(latencies) > 1 else None
            ),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        with sampler.lock:
            sampler.pids.discard(process.pid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if os.name != "posix" or not Path("/proc/self/status").is_file():
        parser.error("Linux /proc is required for RSS measurement")
    if args.iterations < 2 or not 1 <= args.concurrency <= 4:
        parser.error("iterations must be >=2 and concurrency must be 1..4")
    sampler = Sampler()
    thread = threading.Thread(target=sampler.run, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(_one_worker, index, args.iterations, sampler)
                for index in range(args.concurrency)
            ]
            results = [future.result() for future in futures]
    finally:
        sampler.stop.set()
        thread.join(timeout=2)
    for result in results:
        result["peak_rss_mib"] = round(sampler.peak_by_pid[result["pid"]], 1)
        del result["pid"]
    print(
        json.dumps(
            {
                "schema_version": "1",
                "platform": "linux/amd64",
                "upstream_commit": COMMIT,
                "channels": sorted(CHANNELS),
                "iterations_per_worker": args.iterations,
                "concurrency": args.concurrency,
                "wall_seconds": round(time.monotonic() - started, 3),
                "peak_total_runtime_rss_mib": round(sampler.total_peak_mib, 1),
                "peak_cgroup_memory_mib": (
                    round(sampler.cgroup_peak_mib, 1)
                    if sampler.cgroup_peak_mib is not None
                    else None
                ),
                "workers": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
