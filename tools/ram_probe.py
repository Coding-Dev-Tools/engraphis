"""Windows RAM integrity and stress probe for local capacity work.

Allocates a target amount of memory across worker processes, writes and verifies
solid, checker, walking, strided, and seeded-random bit patterns, and reports any
mismatch with its worker, pass, offset, expected value, and XOR difference.
Verification is chunked, so the checker never reserves a second full-size buffer
for comparison.

Usage:
    python tools/ram_probe.py [target_gb] [passes] [workers]

    target_gb  Total GiB to allocate across workers. Defaults to available
               physical memory minus 3 GiB, clamped to 2..12.
    passes     Full pattern passes. 0 loops until interrupted. Default: 3.
    workers    Parallel worker processes. Default: min(cpu_count, 12).

This probe is Windows-only because it reads available physical memory through
GlobalMemoryStatusEx. It is a diagnostic aid, not a replacement for a hardware
memtest: stop it if the machine becomes unresponsive.
"""
from __future__ import annotations

import argparse
import ctypes
import multiprocessing as mp
import os
import time
from typing import Optional

import numpy as np

# Verify in bounded windows so a multi-gigabyte allocation never needs a second
# full-size boolean buffer (the previous whole-array comparison doubled residency).
VERIFY_CHUNK_ELEMENTS = 8 * 1024 * 1024  # 64 MiB of uint64 values
WALKING_CHUNK_ELEMENTS = 64 * 1024  # 512 KiB of uint64 values
RANDOM_CHUNK_ELEMENTS = 4 * 1024 * 1024  # 32 MiB of uint64 values
STRIDE = 512  # one uint64 per 4 KiB cache line
CONSTANT_PATTERNS = (
    ("Solid 0x00", 0x0000000000000000),
    ("Solid 0xFF", 0xFFFFFFFFFFFFFFFF),
    ("Checker 0x55", 0x5555555555555555),
    ("Checker 0xAA", 0xAAAAAAAAAAAAAAAA),
    ("Alt 0x33", 0x3333333333333333),
    ("Alt 0xCC", 0xCCCCCCCCCCCCCCCC),
)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def available_physical_bytes() -> int:
    """Return available physical memory reported by the Windows kernel."""
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullAvailPhys)


def _report_bitflip(
    queue: "mp.Queue",
    worker_id: int,
    pass_number: int,
    pattern: str,
    index: int,
    expected: int,
    actual: int,
    mismatches: Optional[int] = None,
) -> None:
    detail = (
        f"Pass {pass_number} [{pattern}] at idx {index}: expected 0x{expected:016X}, "
        f"got 0x{actual:016X}, XOR diff 0x{actual ^ expected:016X}"
    )
    if mismatches is not None:
        detail += f" ({mismatches} errors total)"
    queue.put((worker_id, "BitFlip", detail))


def _verify_constant(
    queue: "mp.Queue",
    arr: "np.ndarray",
    value: int,
    pattern: str,
    pass_number: int,
    worker_id: int,
) -> None:
    """Fill and verify a constant pattern in bounded windows."""
    expected = np.uint64(value)
    for start in range(0, arr.size, VERIFY_CHUNK_ELEMENTS):
        end = min(start + VERIFY_CHUNK_ELEMENTS, arr.size)
        window = arr[start:end]
        window.fill(expected)
        mismatches = np.flatnonzero(window != expected)
        if mismatches.size:
            first = int(mismatches[0])
            _report_bitflip(
                queue, worker_id, pass_number, pattern,
                start + first, int(expected), int(window[first]), int(mismatches.size),
            )
            return


def _verify_walking(
    queue: "mp.Queue",
    arr: "np.ndarray",
    pattern: str,
    pass_number: int,
    worker_id: int,
    invert: bool,
) -> None:
    """Fill and verify walking-ones (or inverted walking-zeros) windows."""
    base = np.left_shift(np.uint64(1), np.arange(64, dtype=np.uint64))
    if invert:
        base = np.bitwise_not(base)
    tiled = np.tile(base, WALKING_CHUNK_ELEMENTS // 64)
    for start in range(0, arr.size, WALKING_CHUNK_ELEMENTS):
        end = min(start + WALKING_CHUNK_ELEMENTS, arr.size)
        expected = tiled[: end - start]
        arr[start:end] = expected
        mismatches = np.flatnonzero(arr[start:end] != expected)
        if mismatches.size:
            first = int(mismatches[0])
            _report_bitflip(
                queue, worker_id, pass_number, pattern,
                start + first, int(expected[first]), int(arr[start + first]),
            )
            return


def _verify_stride(queue: "mp.Queue", arr: "np.ndarray", pass_number: int, worker_id: int) -> None:
    """Fill with one value, disturb every cache line, then verify the stride."""
    value = np.uint64(0x5A5A5A5A5A5A5A5A)
    disturbance = np.uint64(0xA5A5A5A5A5A5A5A5)
    arr.fill(value)
    for offset in range(0, min(arr.size, STRIDE * 1024), STRIDE):
        arr[offset::STRIDE] = disturbance
    expected = np.full(arr[0::STRIDE].shape, disturbance, dtype=np.uint64)
    mismatches = np.flatnonzero(arr[0::STRIDE] != expected)
    if mismatches.size:
        first = int(mismatches[0]) * STRIDE
        _report_bitflip(
            queue, worker_id, pass_number, "Row Disturbance (Stride)",
            first, int(disturbance), int(arr[first]),
        )


def _verify_random(queue: "mp.Queue", arr: "np.ndarray", pass_number: int, worker_id: int) -> None:
    """Write and re-verify a deterministic seeded random stream in windows."""
    seed = (pass_number * 1000 + worker_id * 37 + 42) & 0xFFFFFFFF
    writer = np.random.default_rng(seed)
    for start in range(0, arr.size, RANDOM_CHUNK_ELEMENTS):
        end = min(start + RANDOM_CHUNK_ELEMENTS, arr.size)
        arr[start:end] = writer.integers(0, 0xFFFFFFFFFFFFFFFF, size=end - start, dtype=np.uint64)
    verifier = np.random.default_rng(seed)
    for start in range(0, arr.size, RANDOM_CHUNK_ELEMENTS):
        end = min(start + RANDOM_CHUNK_ELEMENTS, arr.size)
        expected = verifier.integers(0, 0xFFFFFFFFFFFFFFFF, size=end - start, dtype=np.uint64)
        mismatches = np.flatnonzero(arr[start:end] != expected)
        if mismatches.size:
            first = int(mismatches[0])
            _report_bitflip(
                queue, worker_id, pass_number, "Random (Seed)",
                start + first, int(expected[first]), int(arr[start + first]), int(mismatches.size),
            )
            return


def worker_test(
    worker_id: int, num_elements: int, num_passes: int, error_queue: "mp.Queue"
) -> None:
    """Run the full pattern suite over one worker's allocation."""
    total_mb = num_elements * 8 / (1024 * 1024)
    try:
        arr = np.empty(num_elements, dtype=np.uint64)
    except MemoryError:
        error_queue.put((worker_id, "MemoryError", f"Failed to allocate {total_mb:.1f} MB"))
        return

    for pass_number in range(1, num_passes + 1):
        for pattern, value in CONSTANT_PATTERNS:
            _verify_constant(error_queue, arr, value, pattern, pass_number, worker_id)
        _verify_walking(error_queue, arr, "Walking 1s", pass_number, worker_id, invert=False)
        _verify_walking(error_queue, arr, "Walking 0s", pass_number, worker_id, invert=True)
        _verify_stride(error_queue, arr, pass_number, worker_id)
        _verify_random(error_queue, arr, pass_number, worker_id)
        error_queue.put(
            (worker_id, "PASS_DONE", f"Worker {worker_id} completed Pass {pass_number}/{num_passes}")
        )

    error_queue.put(
        (worker_id, "DONE", f"Completed {num_passes} passes on {total_mb:.1f} MB successfully")
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Windows RAM integrity and stress probe.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("target_gb", type=float, nargs="?", default=None,
                        help="total GiB to allocate across workers")
    parser.add_argument("passes", type=int, nargs="?", default=3,
                        help="full pattern passes; 0 loops until interrupted")
    parser.add_argument("workers", type=int, nargs="?", default=None,
                        help="parallel worker processes")
    args = parser.parse_args(argv)

    if os.name != "nt":
        parser.error("this probe reads Windows memory status and runs on Windows only")

    available_gb = available_physical_bytes() / (1024 ** 3)
    target_gb = args.target_gb
    if target_gb is None:
        target_gb = max(2.0, min(12.0, available_gb - 3.0))
    passes = args.passes
    num_workers = args.workers or min(mp.cpu_count(), 12)
    if target_gb <= 0 or num_workers <= 0 or passes < 0:
        parser.error("target_gb and workers must be positive; passes must be zero or greater")

    continuous = passes == 0
    passes_to_run = passes if not continuous else 999999
    elements_per_worker = int((target_gb * (1024 ** 3)) / num_workers) // 8

    print("=" * 65)
    print(" IN-WINDOWS PARALLEL RAM STRESS TEST")
    print("=" * 65)
    print(f"Physical RAM Available : {available_gb:.2f} GB")
    print(f"Target RAM to Test     : {target_gb:.2f} GB ({target_gb / num_workers:.2f} GB per worker)")
    print(f"Worker Processes       : {num_workers} parallel workers")
    print(f"Mode                   : {'Continuous (Ctrl+C to stop)' if continuous else f'{passes} passes'}")
    print("Patterns per Pass      : Solid, checker, alternating, walking, stride, random")
    print(f"Started at             : {time.strftime('%X')}")
    print("=" * 65 + "\n")

    error_queue: "mp.Queue" = mp.Queue()
    workers = [
        mp.Process(target=worker_test, args=(index, elements_per_worker, passes_to_run, error_queue))
        for index in range(num_workers)
    ]
    for worker in workers:
        worker.start()

    started = time.time()
    errors = []
    done_count = 0
    try:
        while any(worker.is_alive() for worker in workers) or not error_queue.empty():
            try:
                worker_id, message_type, text = error_queue.get(timeout=1.0)
            except Exception:  # queue.Empty, and a closed queue during shutdown
                continue
            if message_type == "DONE":
                print(f"  [+] Worker {worker_id}: {text}")
                done_count += 1
            elif message_type == "PASS_DONE":
                print(f"  [~] {text}")
            elif message_type == "BitFlip":
                print(f"  [!] HARDWARE BITFLIP DETECTED [Worker {worker_id}]: {text}")
                errors.append((worker_id, message_type, text))
            else:
                print(f"  [x] MEMORY ALLOCATION FAILED [Worker {worker_id}]: {text}")
                errors.append((worker_id, message_type, text))
    except KeyboardInterrupt:
        print("\n[!] User interrupted the test. Terminating workers...")
        for worker in workers:
            worker.terminate()
    finally:
        for worker in workers:
            worker.join()

    duration = time.time() - started
    tested_gb = target_gb * (passes if not continuous else max(1, done_count) / max(1, num_workers))
    throughput = tested_gb / duration if duration > 0 else 0.0

    print("\n" + "=" * 65)
    print(" SUMMARY")
    print("=" * 65)
    print(f"Total Duration     : {duration:.1f} seconds")
    print(f"Average Throughput : {throughput:.2f} GB/s aggregate")
    print(f"Workers Finished   : {done_count} / {num_workers}")
    if errors:
        print(f"\n[FAIL] DETECTED {len(errors)} MEMORY ERROR(S):")
        for worker_id, message_type, text in errors:
            print(f"  - Worker {worker_id} [{message_type}]: {text}")
        return 1
    print("\n[PASS] No bitflips detected in allocated memory.")
    print("=" * 65)
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
