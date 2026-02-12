"""
Integration tests for OverlayFS-isolated sessions.

Tests the /overlay-session/ API endpoints and verifies:
- Full filesystem view (agent sees /usr/bin, /lib, etc.)
- Regular files in working directory
- Extra files at absolute paths inside the overlay
- pwd shows /home/agent_XXXX (not the real overlay mount path)
- Concurrent session isolation (same absolute path, different content)
- Write isolation (writes in one session don't appear in another)
- Agent writing files works within the session
- cd/env persistence inside the chroot
- Clean unmount on destroy (no stale mounts)

Usage:
    SANDBOX_TEST_ENDPOINT=http://localhost:60808 python -m pytest tests/test_overlay_sessions.py -v

Requires a running SandboxFusion server with --cap-add SYS_ADMIN.
"""

import asyncio
import base64
import json
import os
import time

import aiohttp
import pytest
import pytest_asyncio

ENDPOINT = os.getenv("SANDBOX_TEST_ENDPOINT", "http://localhost:60808")


def b64(text):
    return base64.b64encode(text.encode()).decode()


async def create_overlay_session(http, session_id=None, files=None, extra_files=None,
                                  startup_commands=None, env=None):
    payload = {
        "session_id": session_id,
        "files": files or {},
        "extra_files": extra_files or {},
        "startup_commands": startup_commands or [],
        "env": env or {},
    }
    async with http.post(f"{ENDPOINT}/overlay-session/create", json=payload) as resp:
        data = await resp.json()
    assert data["status"] == "Success", f"create_session failed: {data}"
    return data["session_id"]


async def run_overlay_command(http, session_id, command, timeout=10, fetch_files=None):
    payload = {
        "session_id": session_id,
        "command": command,
        "timeout": timeout,
        "fetch_files": fetch_files or [],
    }
    async with http.post(f"{ENDPOINT}/overlay-session/run", json=payload) as resp:
        data = await resp.json()
    return data


async def destroy_overlay_session(http, session_id):
    payload = {"session_id": session_id}
    async with http.post(f"{ENDPOINT}/overlay-session/destroy", json=payload) as resp:
        data = await resp.json()
    return data


@pytest_asyncio.fixture
async def http():
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(f"{ENDPOINT}/v1/ping") as resp:
                text = await resp.text()
                assert "pong" in text
        except Exception as e:
            pytest.skip(f"Server not reachable at {ENDPOINT}: {e}")
        yield session


@pytest_asyncio.fixture
async def session_id(http):
    sid = await create_overlay_session(http)
    yield sid
    await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_full_filesystem_view(http, session_id):
    """Agent sees a full Linux filesystem."""
    result = await run_overlay_command(http, session_id, "ls /usr/bin | head -5")
    assert result["status"] == "Success", f"ls /usr/bin failed: {result}"
    assert result["stdout"].strip() != ""

    result = await run_overlay_command(http, session_id, "which bash")
    assert result["status"] == "Success"
    assert "/bin/bash" in result["stdout"]


@pytest.mark.asyncio
async def test_regular_files_in_working_dir(http):
    """Files are placed in /home/agent_XXXX/."""
    files = {
        "hello.txt": b64("Hello, world!"),
        "subdir/nested.txt": b64("Nested content"),
    }
    sid = await create_overlay_session(http, files=files)
    try:
        result = await run_overlay_command(http, sid, "cat hello.txt")
        assert result["status"] == "Success"
        assert "Hello, world!" in result["stdout"]

        result = await run_overlay_command(http, sid, "cat subdir/nested.txt")
        assert result["status"] == "Success"
        assert "Nested content" in result["stdout"]
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_extra_files_at_absolute_paths(http):
    """Extra files appear at absolute paths inside the overlay."""
    extra_files = {
        "opt/grading/results.json": b64(json.dumps({"score": 42})),
        "tmp/eval/config.yaml": b64("key: value\n"),
    }
    sid = await create_overlay_session(http, extra_files=extra_files)
    try:
        result = await run_overlay_command(http, sid, "cat /opt/grading/results.json")
        assert result["status"] == "Success", f"cat extra file failed: {result}"
        data = json.loads(result["stdout"].strip())
        assert data["score"] == 42

        result = await run_overlay_command(http, sid, "cat /tmp/eval/config.yaml")
        assert result["status"] == "Success"
        assert "key: value" in result["stdout"]
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_pwd_shows_agent_home(http):
    """pwd shows /home/agent_XXXX, not the real overlay mount path."""
    sid = await create_overlay_session(http)
    try:
        result = await run_overlay_command(http, sid, "pwd")
        assert result["status"] == "Success"
        pwd_out = result["stdout"].strip()
        assert pwd_out.startswith("/home/agent_"), f"Got: {pwd_out}"
        assert "/tmp/ovl_" not in pwd_out, f"Leaked overlay path: {pwd_out}"
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_concurrent_session_isolation_same_path(http):
    """
    KEY TEST: Two sessions with extra_files at the SAME absolute path but
    different content -- each sees only its own version.
    """
    content_a = json.dumps({"session": "A", "value": 111})
    content_b = json.dumps({"session": "B", "value": 222})

    sid_a = await create_overlay_session(
        http, extra_files={"opt/grading/results.json": b64(content_a)},
    )
    sid_b = await create_overlay_session(
        http, extra_files={"opt/grading/results.json": b64(content_b)},
    )
    try:
        result_a = await run_overlay_command(http, sid_a, "cat /opt/grading/results.json")
        assert result_a["status"] == "Success"
        data_a = json.loads(result_a["stdout"].strip())
        assert data_a["session"] == "A"
        assert data_a["value"] == 111

        result_b = await run_overlay_command(http, sid_b, "cat /opt/grading/results.json")
        assert result_b["status"] == "Success"
        data_b = json.loads(result_b["stdout"].strip())
        assert data_b["session"] == "B"
        assert data_b["value"] == 222

        # Re-check A after B was accessed
        result_a2 = await run_overlay_command(http, sid_a, "cat /opt/grading/results.json")
        data_a2 = json.loads(result_a2["stdout"].strip())
        assert data_a2["session"] == "A"
    finally:
        await destroy_overlay_session(http, sid_a)
        await destroy_overlay_session(http, sid_b)


@pytest.mark.asyncio
async def test_write_isolation_between_sessions(http):
    """Writes in one session do NOT appear in another."""
    sid_a = await create_overlay_session(http)
    sid_b = await create_overlay_session(http)
    try:
        result = await run_overlay_command(http, sid_a, 'echo "secret" > /tmp/sa.txt')
        assert result["status"] == "Success"

        result = await run_overlay_command(http, sid_a, "cat /tmp/sa.txt")
        assert result["status"] == "Success"
        assert "secret" in result["stdout"]

        result = await run_overlay_command(http, sid_b, "cat /tmp/sa.txt")
        assert result["return_code"] != 0, "Session A file visible in B"
    finally:
        await destroy_overlay_session(http, sid_a)
        await destroy_overlay_session(http, sid_b)


@pytest.mark.asyncio
async def test_agent_writing_files(http, session_id):
    """Agent can create and read files within its session."""
    result = await run_overlay_command(http, session_id, 'echo "tc" > myfile.txt')
    assert result["status"] == "Success"

    result = await run_overlay_command(http, session_id, "cat myfile.txt")
    assert result["status"] == "Success"
    assert "tc" in result["stdout"]

    result = await run_overlay_command(http, session_id, "wc -l myfile.txt")
    assert result["status"] == "Success"
    assert "1" in result["stdout"]


@pytest.mark.asyncio
async def test_cd_persistence(http, session_id):
    """cd persists across commands."""
    await run_overlay_command(http, session_id, "mkdir -p subdir1/subdir2")
    await run_overlay_command(http, session_id, "cd subdir1/subdir2")
    result = await run_overlay_command(http, session_id, "pwd")
    assert result["status"] == "Success"
    assert "subdir1/subdir2" in result["stdout"]


@pytest.mark.asyncio
async def test_env_persistence(http, session_id):
    """Environment variables persist across commands."""
    await run_overlay_command(http, session_id, 'export MY_VAR="hello_overlay"')
    result = await run_overlay_command(http, session_id, "echo $MY_VAR")
    assert result["status"] == "Success"
    assert "hello_overlay" in result["stdout"]


@pytest.mark.asyncio
async def test_env_from_create(http):
    """Env vars passed at creation are available."""
    sid = await create_overlay_session(http, env={"GREETING": "howdy", "COUNT": "42"})
    try:
        result = await run_overlay_command(http, sid, "echo $GREETING $COUNT")
        assert result["status"] == "Success"
        assert "howdy" in result["stdout"]
        assert "42" in result["stdout"]
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_destroy_unmounts_cleanly(http):
    """After destroy, the session is gone and commands fail."""
    sid = await create_overlay_session(http)
    result = await run_overlay_command(http, sid, "echo active")
    assert result["status"] == "Success"

    d = await destroy_overlay_session(http, sid)
    assert d["status"] == "Success"

    result = await run_overlay_command(http, sid, "echo test")
    assert result["status"] == "Failed"
    combined = result.get("message", "") + result.get("stderr", "")
    assert "not found" in combined.lower()


@pytest.mark.asyncio
async def test_fetch_files(http):
    """fetch_files returns base64-encoded file content."""
    sid = await create_overlay_session(http)
    try:
        await run_overlay_command(http, sid, 'echo "fetchme" > output.txt')
        result = await run_overlay_command(http, sid, "echo done", fetch_files=["output.txt"])
        assert result["status"] == "Success"
        assert "output.txt" in result["files"]
        content = base64.b64decode(result["files"]["output.txt"]).decode()
        assert "fetchme" in content
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_startup_commands(http):
    """Startup commands run during session creation."""
    sid = await create_overlay_session(
        http, startup_commands=["mkdir -p /tmp/stest", "touch /tmp/stest/marker"],
    )
    try:
        result = await run_overlay_command(http, sid, "ls /tmp/stest/marker")
        assert result["status"] == "Success"
        assert "marker" in result["stdout"]
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_command_timeout(http, session_id):
    """Commands that exceed timeout are killed."""
    start = time.time()
    result = await run_overlay_command(http, session_id, "sleep 60", timeout=2)
    elapsed = time.time() - start
    assert result["status"] == "Failed"
    combined = result.get("stderr", "") + result.get("message", "")
    assert "timed out" in combined.lower()
    assert elapsed < 10


@pytest.mark.asyncio
async def test_files_and_extra_files_together(http):
    """Both files (working dir) and extra_files (absolute paths) work."""
    files = {"code.py": b64('print("hello from code")')}
    extra = {"opt/grading/expected.txt": b64("expected output")}
    sid = await create_overlay_session(http, files=files, extra_files=extra)
    try:
        result = await run_overlay_command(http, sid, "python3 code.py")
        assert result["status"] == "Success"
        assert "hello from code" in result["stdout"]

        result = await run_overlay_command(http, sid, "cat /opt/grading/expected.txt")
        assert result["status"] == "Success"
        assert "expected output" in result["stdout"]
    finally:
        await destroy_overlay_session(http, sid)


@pytest.mark.asyncio
async def test_many_concurrent_sessions(http):
    """Create and use 10 concurrent overlay sessions."""
    n = 10
    sids = await asyncio.gather(*[create_overlay_session(http) for _ in range(n)])
    assert len(sids) == n
    try:
        results = await asyncio.gather(*[
            run_overlay_command(http, sid, f"echo s{i}") for i, sid in enumerate(sids)
        ])
        for i, r in enumerate(results):
            assert r["status"] == "Success", f"Session {i} failed: {r}"
            assert f"s{i}" in r["stdout"]
    finally:
        await asyncio.gather(*[destroy_overlay_session(http, s) for s in sids])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
