"""
Test to reproduce the 220GB memory leak bug.
This test should FAIL before the fix and PASS after the fix.
"""
import asyncio
import time
import psutil
import requests


async def test_timeout_kills_memory_hog():
    """Reproduce: Python process that allocates memory should be killed on timeout."""

    # Start session
    resp = requests.post("http://127.0.0.1:60860/session/create", json={})
    resp_json = resp.json()
    if resp_json.get("status") != "Success":
        raise RuntimeError(f"Failed to create session: {resp_json}")
    session_id = resp_json["session_id"]

    # Record process count before
    before_count = len(psutil.pids())

    # Run memory-hogging code with 5 second timeout
    start_time = time.time()
    resp = requests.post("http://127.0.0.1:60860/session/run", json={
        "session_id": session_id,
        "command": """
python3 -c "
import time
# Allocate memory continuously
data = []
while True:
    data.append('x' * 1000000)  # 1MB per iteration
    time.sleep(0.1)
"
        """,
        "timeout": 5
    })
    elapsed = time.time() - start_time

    result = resp.json()

    # Should timeout after ~5 seconds
    assert 4 < elapsed < 7, f"Timeout took {elapsed}s, expected ~5s"
    assert result["status"] == "Failed", f"Expected Failed, got {result['status']}"
    assert "timed out" in result["stderr"].lower(), "Expected timeout message"

    # Wait for processes to be killed
    await asyncio.sleep(2)

    # CRITICAL CHECK: Verify no python processes are still running
    python_procs = [p for p in psutil.process_iter(['pid', 'name', 'cmdline'])
                    if p.info['name'] == 'python3' and 'data.append' in str(p.info.get('cmdline', []))]

    # THIS WILL FAIL BEFORE THE FIX!
    assert len(python_procs) == 0, f"ERROR: {len(python_procs)} python processes still alive: {[p.info['pid'] for p in python_procs]}"

    # Verify process count returned to baseline (allow up to 5 for session overhead)
    after_count = len(psutil.pids())
    leaked = after_count - before_count
    assert leaked < 6, f"ERROR: {leaked} processes leaked (baseline: {before_count}, after: {after_count})!"

    # Cleanup session
    requests.post("http://127.0.0.1:60860/session/destroy", json={"session_id": session_id})

    print("✓ Test PASSED: All processes killed on timeout")


async def test_timeout_kills_child_processes():
    """Reproduce: Bash spawning child python processes - all should be killed."""

    resp = requests.post("http://127.0.0.1:60860/session/create", json={})
    resp_json = resp.json()
    if resp_json.get("status") != "Success":
        raise RuntimeError(f"Failed to create session: {resp_json}")
    session_id = resp_json["session_id"]

    # Spawn multiple child processes
    resp = requests.post("http://127.0.0.1:60860/session/run", json={
        "session_id": session_id,
        "command": """
python3 -c "
import subprocess
import time

# Spawn 3 child sleep processes
procs = []
for i in range(3):
    p = subprocess.Popen(['sleep', '300'])
    procs.append(p)
    print(f'Spawned child PID: {p.pid}')

time.sleep(100)  # Parent sleeps
"
        """,
        "timeout": 3
    })

    result = resp.json()
    assert result["status"] == "Failed", "Expected timeout"

    # Wait for kill to propagate
    await asyncio.sleep(2)

    # Check for surviving sleep 300 processes specifically (from our test)
    sleep_procs = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
        try:
            if p.info['name'] == 'sleep' and p.info['status'] != 'zombie':
                cmdline = p.info.get('cmdline', [])
                if cmdline and '300' in cmdline:
                    sleep_procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # THIS WILL FAIL BEFORE THE FIX!
    assert len(sleep_procs) == 0, f"ERROR: {len(sleep_procs)} sleep 300 processes survived: {[p.info['pid'] for p in sleep_procs]}"

    # Cleanup session
    requests.post("http://127.0.0.1:60860/session/destroy", json={"session_id": session_id})

    print("✓ Test PASSED: All child processes killed")


if __name__ == "__main__":
    asyncio.run(test_timeout_kills_memory_hog())
    asyncio.run(test_timeout_kills_child_processes())
