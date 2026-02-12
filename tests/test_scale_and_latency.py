#!/usr/bin/env python3
"""
Concurrency and latency benchmarks for BOTH session types.

Benchmarks /session/ (non-overlay) and /overlay-session/ (overlay) side by side.

Latency tests (sequential, per-operation):
- create_session, run_command, destroy_session latency (p50, p95, p99)
- Full round-trip: create + 3 commands + destroy

Concurrency tests (parallel, scaling):
- Ramp: 10, 100, 500, 1000, 2000, 5000, 10000 concurrent sessions
- Measures wall-clock time and error rate at each level

Usage:
    SANDBOX_TEST_ENDPOINT=http://localhost:60808 python tests/test_scale_and_latency.py

For 10k sessions the container needs MAX_BASH_SESSIONS=15000.
"""

import asyncio
import os
import sys
import time

import aiohttp

ENDPOINT = os.getenv("SANDBOX_TEST_ENDPOINT", "http://localhost:60808")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=120)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def api_create(http, prefix, session_id=None):
    payload = {"session_id": session_id or "", "files": {}, "extra_files": {},
               "startup_commands": [], "env": {}}
    if not session_id:
        payload.pop("session_id")
    async with http.post(f"{ENDPOINT}{prefix}/create", json=payload) as resp:
        data = await resp.json()
    return data


async def api_run(http, prefix, session_id, command="echo hello", timeout=10):
    payload = {"session_id": session_id, "command": command,
               "timeout": timeout, "fetch_files": []}
    async with http.post(f"{ENDPOINT}{prefix}/run", json=payload) as resp:
        data = await resp.json()
    return data


async def api_destroy(http, prefix, session_id):
    payload = {"session_id": session_id}
    async with http.post(f"{ENDPOINT}{prefix}/destroy", json=payload) as resp:
        data = await resp.json()
    return data


def percentile(data, p):
    """Return the p-th percentile of data."""
    if not data:
        return 0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


# ---------------------------------------------------------------------------
# Latency benchmarks (sequential)
# ---------------------------------------------------------------------------

async def bench_latency(http, prefix, label, iterations=100):
    """Measure per-operation latency for a given session type."""
    create_times = []
    run_times = []
    destroy_times = []
    roundtrip_times = []

    print(f"\n  [{label}] Latency benchmark ({iterations} iterations)...")

    # Per-operation latency
    for i in range(iterations):
        # Create
        t0 = time.perf_counter()
        result = await api_create(http, prefix)
        create_times.append((time.perf_counter() - t0) * 1000)
        assert result["status"] == "Success", f"Create failed iter {i}: {result}"
        sid = result["session_id"]

        # Run
        t0 = time.perf_counter()
        result = await api_run(http, prefix, sid)
        run_times.append((time.perf_counter() - t0) * 1000)

        # Destroy
        t0 = time.perf_counter()
        result = await api_destroy(http, prefix, sid)
        destroy_times.append((time.perf_counter() - t0) * 1000)

        if (i + 1) % 25 == 0:
            print(f"    {i + 1}/{iterations} done")

    # Full round-trip (create + 3 commands + destroy)
    rt_iters = min(iterations, 50)
    print(f"  [{label}] Round-trip benchmark ({rt_iters} iterations)...")
    for i in range(rt_iters):
        t0 = time.perf_counter()
        result = await api_create(http, prefix)
        sid = result["session_id"]
        await api_run(http, prefix, sid, "echo cmd1")
        await api_run(http, prefix, sid, "echo cmd2")
        await api_run(http, prefix, sid, "echo cmd3")
        await api_destroy(http, prefix, sid)
        roundtrip_times.append((time.perf_counter() - t0) * 1000)

    return {
        "create": create_times,
        "run": run_times,
        "destroy": destroy_times,
        "roundtrip": roundtrip_times,
    }


# ---------------------------------------------------------------------------
# Concurrency benchmarks (parallel)
# ---------------------------------------------------------------------------

async def bench_concurrency_level(http, prefix, n):
    """Create N sessions, run one command in each, destroy all. Returns stats."""
    errors = {"create": 0, "run": 0, "destroy": 0}
    sids = []

    # Create N sessions
    t_start = time.perf_counter()

    async def create_one(idx):
        try:
            result = await api_create(http, prefix)
            if result.get("status") == "Success":
                return result["session_id"]
            errors["create"] += 1
            return None
        except Exception:
            errors["create"] += 1
            return None

    create_results = await asyncio.gather(*[create_one(i) for i in range(n)])
    sids = [s for s in create_results if s is not None]
    t_created = time.perf_counter()

    # Run one command in each
    async def run_one(sid):
        try:
            result = await api_run(http, prefix, sid)
            if result.get("status") != "Success":
                errors["run"] += 1
        except Exception:
            errors["run"] += 1

    await asyncio.gather(*[run_one(s) for s in sids])
    t_ran = time.perf_counter()

    # Destroy all
    async def destroy_one(sid):
        try:
            await api_destroy(http, prefix, sid)
        except Exception:
            errors["destroy"] += 1

    await asyncio.gather(*[destroy_one(s) for s in sids])
    t_done = time.perf_counter()

    wall_clock = t_done - t_start
    total_errors = sum(errors.values())
    error_rate = total_errors / (n * 3) * 100 if n > 0 else 0

    return {
        "n": n,
        "wall_clock": wall_clock,
        "create_time": t_created - t_start,
        "run_time": t_ran - t_created,
        "destroy_time": t_done - t_ran,
        "errors": errors,
        "total_errors": total_errors,
        "error_rate_pct": error_rate,
        "sessions_created": len(sids),
    }


async def bench_concurrency(http, prefix, label, levels=None):
    """Run concurrency benchmarks at multiple levels."""
    if levels is None:
        levels = [10, 100, 500, 1000, 2000, 5000, 10000]

    results = []
    for n in levels:
        print(f"  [{label}] {n} concurrent sessions...", end=" ", flush=True)
        try:
            stats = await bench_concurrency_level(http, prefix, n)
            print(f"{stats['wall_clock']:.1f}s "
                  f"(created={stats['sessions_created']}, "
                  f"errors={stats['total_errors']})")
            results.append(stats)
        except Exception as e:
            print(f"FAILED: {e}")
            results.append({
                "n": n, "wall_clock": -1, "errors": {"create": n},
                "total_errors": n, "error_rate_pct": 100,
                "sessions_created": 0,
            })
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_latency_table(non_overlay, overlay):
    """Print side-by-side latency comparison."""
    print("\n" + "=" * 60)
    print("  LATENCY COMPARISON (ms)")
    print("=" * 60)
    print(f"{'Operation':<20} {'non-overlay':>12} {'overlay':>12}")
    print("-" * 60)

    for op in ["create", "run", "destroy", "roundtrip"]:
        no = non_overlay.get(op, [])
        ov = overlay.get(op, [])
        for pct, lbl in [(50, "p50"), (95, "p95"), (99, "p99")]:
            no_val = percentile(no, pct)
            ov_val = percentile(ov, pct)
            print(f"{op:<12} {lbl:<8} {no_val:>10.1f}ms {ov_val:>10.1f}ms")
    print("=" * 60)


def print_concurrency_table(no_results, ov_results):
    """Print side-by-side concurrency comparison."""
    print("\n" + "=" * 70)
    print("  CONCURRENCY COMPARISON (wall-clock seconds)")
    print("=" * 70)
    print(f"{'Sessions':<10} {'non-overlay':>12} {'overlay':>12} {'overhead':>10}")
    print("-" * 70)

    no_by_n = {r["n"]: r for r in no_results}
    ov_by_n = {r["n"]: r for r in ov_results}
    all_ns = sorted(set(list(no_by_n.keys()) + list(ov_by_n.keys())))

    for n in all_ns:
        no_r = no_by_n.get(n)
        ov_r = ov_by_n.get(n)
        no_t = f"{no_r['wall_clock']:.1f}s" if no_r and no_r["wall_clock"] > 0 else "N/A"
        ov_t = f"{ov_r['wall_clock']:.1f}s" if ov_r and ov_r["wall_clock"] > 0 else "N/A"

        if (no_r and no_r["wall_clock"] > 0 and
                ov_r and ov_r["wall_clock"] > 0):
            overhead = ((ov_r["wall_clock"] - no_r["wall_clock"])
                        / no_r["wall_clock"] * 100)
            overhead_s = f"{overhead:+.0f}%"
        else:
            overhead_s = "N/A"

        no_err = f" (err={no_r['total_errors']})" if no_r and no_r.get("total_errors") else ""
        ov_err = f" (err={ov_r['total_errors']})" if ov_r and ov_r.get("total_errors") else ""

        print(f"{n:<10} {no_t + no_err:>18} {ov_t + ov_err:>18} {overhead_s:>10}")

    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("=" * 60)
    print("  SandboxFusion Scale & Latency Benchmarks")
    print("=" * 60)
    print(f"  Endpoint: {ENDPOINT}")

    # Parse CLI args for concurrency levels
    levels = [10, 100, 500, 1000, 2000, 5000, 10000]
    latency_iters = 100
    skip_concurrency = False

    for arg in sys.argv[1:]:
        if arg == "--latency-only":
            skip_concurrency = True
        elif arg.startswith("--levels="):
            levels = [int(x) for x in arg.split("=")[1].split(",")]
        elif arg.startswith("--latency-iters="):
            latency_iters = int(arg.split("=")[1])

    # Use unlimited connections for concurrency tests
    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, connector=conn) as http:
        # Health check
        try:
            async with http.get(f"{ENDPOINT}/v1/ping") as resp:
                text = await resp.text()
                assert "pong" in text
            print("  Server: OK\n")
        except Exception as e:
            print(f"  ERROR: Server not reachable at {ENDPOINT}: {e}")
            sys.exit(1)

        # --- Latency benchmarks ---
        print("\n" + "-" * 60)
        print("  LATENCY BENCHMARKS (sequential)")
        print("-" * 60)

        no_latency = await bench_latency(http, "/session", "non-overlay",
                                         iterations=latency_iters)
        ov_latency = await bench_latency(http, "/overlay-session", "overlay",
                                         iterations=latency_iters)
        print_latency_table(no_latency, ov_latency)

        if skip_concurrency:
            print("\nSkipping concurrency benchmarks (--latency-only)")
            return

        # --- Concurrency benchmarks ---
        print("\n" + "-" * 60)
        print("  CONCURRENCY BENCHMARKS (parallel)")
        print("-" * 60)

        no_conc = await bench_concurrency(http, "/session", "non-overlay",
                                          levels=levels)
        ov_conc = await bench_concurrency(http, "/overlay-session", "overlay",
                                          levels=levels)
        print_concurrency_table(no_conc, ov_conc)


if __name__ == "__main__":
    asyncio.run(main())
