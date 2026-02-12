"""
End-to-end tests for FusionAgentLoop and FusionAgentLoopOverlay.

Tests both session types through their actual class interfaces -- SessionClient,
execute_agent_command, helper methods -- verifying session lifecycle, command
execution, state persistence, and overlay isolation.

Does NOT issue raw HTTP calls; everything goes through the classes under test.

Usage:
    SANDBOX_TEST_ENDPOINT=http://localhost:60808 \
        python -m pytest sandbox/tests/test_fusion_agent_loops.py -v

Requires a running SandboxFusion container with --cap-add SYS_ADMIN
and --security-opt apparmor=unconfined.
"""

import asyncio
import base64
import json
import os

import numpy as np
import pytest
import pytest_asyncio
from omegaconf import OmegaConf
from unittest.mock import MagicMock

from verl.experimental.agent_loop.agent_loop import DictConfigWrap
from verl.experimental.agent_loop.fusion_agent_loop import (
    FusionAgentLoop,
    FusionAgentLoopOverlay,
    SessionClient,
    SANDBOX_ENDPOINTS,
    SANDBOX_CLIENT_TIMEOUT,
    SANDBOX_RUN_TIMEOUT,
)

ENDPOINT = os.getenv("SANDBOX_TEST_ENDPOINT", "http://localhost:60808")


# ---------------------------------------------------------------------------
# Helpers — build minimal fakes for the verl framework dependencies
# ---------------------------------------------------------------------------

def _make_fake_tokenizer():
    """A tokenizer-like object that satisfies initialize_system_prompt."""
    tok = MagicMock()
    # initialize_system_prompt calls apply_chat_template twice.
    # Return lists of ints so the subtraction logic works.
    tok.apply_chat_template.side_effect = lambda msgs, **kw: list(range(len(msgs) * 3))
    return tok


def _make_config():
    """Minimal OmegaConf config with the fields FusionAgentLoop reads."""
    return OmegaConf.create({
        "actor_rollout_ref": {
            "rollout": {
                "prompt_length": 512,
                "response_length": 1024,
            },
        },
        "data": {
            "apply_chat_template_kwargs": {},
        },
    })


def _make_loop(cls):
    """Instantiate *cls* (FusionAgentLoop or subclass) with faked deps."""
    config = _make_config()
    return cls(
        trainer_config=DictConfigWrap(config),
        server_manager=MagicMock(),
        tokenizer=_make_fake_tokenizer(),
        processor=None,
        dataset_cls=MagicMock(),
        dataset_config=DictConfigWrap(OmegaConf.create({"apply_chat_template_kwargs": {}})),
    )


def _b64(text):
    return base64.b64encode(text.encode()).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def non_overlay_loop():
    return _make_loop(FusionAgentLoop)


@pytest.fixture
def overlay_loop():
    return _make_loop(FusionAgentLoopOverlay)


# ---------------------------------------------------------------------------
# 1. Class-level attribute tests
# ---------------------------------------------------------------------------

class TestClassAttributes:
    def test_use_overlay_flag(self):
        assert FusionAgentLoop._use_overlay is False
        assert FusionAgentLoopOverlay._use_overlay is True

    def test_session_prefix(self, non_overlay_loop, overlay_loop):
        assert non_overlay_loop.session_client._session_prefix == "/session"
        assert overlay_loop.session_client._session_prefix == "/overlay-session"

    def test_overlay_inherits_fusion(self):
        assert issubclass(FusionAgentLoopOverlay, FusionAgentLoop)


# ---------------------------------------------------------------------------
# 2. Helper method tests (no server needed)
# ---------------------------------------------------------------------------

class TestHelperMethods:
    def test_extract_bash_command_basic(self, non_overlay_loop):
        loop = non_overlay_loop
        assert loop.extract_bash_command("<bash>ls -la</bash>") == "ls -la"
        assert loop.extract_bash_command("no command here") is None

    def test_extract_bash_command_with_think(self, non_overlay_loop):
        loop = non_overlay_loop
        text = "<think>planning</think><bash>echo hello</bash>"
        assert loop.extract_bash_command(text) == "echo hello"

    def test_extract_bash_command_last_match(self, non_overlay_loop):
        loop = non_overlay_loop
        text = "<bash>first</bash> middle <bash>second</bash>"
        assert loop.extract_bash_command(text) == "second"

    def test_extract_bash_command_no_closing_tag(self, non_overlay_loop):
        loop = non_overlay_loop
        assert loop.extract_bash_command("<bash>incomplete") is None

    def test_extract_bash_command_strips_leading_newline(self, non_overlay_loop):
        loop = non_overlay_loop
        assert loop.extract_bash_command("<bash>\nls</bash>") == "ls"

    def test_create_command_output_success(self, non_overlay_loop):
        loop = non_overlay_loop
        result = {"status": "Success", "stdout": "hello\n", "stderr": ""}
        assert loop.create_command_output(result) == "hello\n"

    def test_create_command_output_failure_with_stderr(self, non_overlay_loop):
        loop = non_overlay_loop
        result = {"status": "Failed", "stdout": "", "stderr": "not found", "message": ""}
        output = loop.create_command_output(result)
        assert "Execution Failed" in output
        assert "not found" in output

    def test_create_command_output_failure_with_partial_stdout(self, non_overlay_loop):
        loop = non_overlay_loop
        result = {"status": "Failed", "stdout": "partial output\n", "stderr": "error", "message": ""}
        output = loop.create_command_output(result)
        assert "partial output" in output
        assert "Execution Failed" in output

    def test_flatten_structure(self, non_overlay_loop):
        loop = non_overlay_loop
        fs = [
            {"name": "hello.txt", "type": "file", "content": "hello"},
            {"name": "sub", "type": "directory", "content": [
                {"name": "nested.txt", "type": "file", "content": "nested"},
            ]},
        ]
        files = loop.flatten_structure(fs)
        assert "hello.txt" in files
        assert "sub/nested.txt" in files
        # Values should be base64-encoded
        assert base64.b64decode(files["hello.txt"]).decode() == "hello"
        assert base64.b64decode(files["sub/nested.txt"]).decode() == "nested"

    def test_decode_fetched_files(self, non_overlay_loop):
        loop = non_overlay_loop
        files = {"out.txt": _b64("content")}
        decoded = loop.decode_fetched_files(files)
        assert isinstance(decoded, np.ndarray)
        assert decoded.item()["out.txt"] == "content"

    def test_has_junk_artifacts(self, non_overlay_loop):
        loop = non_overlay_loop
        assert loop.has_junk_artifacts("```python\nprint(1)\n```") is True
        assert loop.has_junk_artifacts("echo hello") is False
        assert loop.has_junk_artifacts("<output>stuff</output>") is True

    def test_helpers_same_on_overlay(self, overlay_loop):
        """Overlay variant inherits all helpers unchanged."""
        loop = overlay_loop
        assert loop.extract_bash_command("<bash>pwd</bash>") == "pwd"
        assert loop.create_command_output({"status": "Success", "stdout": "ok"}) == "ok"


# ---------------------------------------------------------------------------
# 3. SessionClient integration tests (requires running server)
# ---------------------------------------------------------------------------

def _skip_if_no_server():
    """Pytest skip if sandbox server is unreachable."""
    import aiohttp

    async def _check():
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(f"{ENDPOINT}/v1/ping") as r:
                    return "pong" in await r.text()
        except Exception:
            return False

    if not asyncio.get_event_loop().run_until_complete(_check()):
        pytest.skip(f"Sandbox server not reachable at {ENDPOINT}")


@pytest.fixture(autouse=True, scope="class")
def require_server(request):
    """Auto-skip server-dependent test classes if server is down."""
    if request.cls and getattr(request.cls, "_needs_server", False):
        _skip_if_no_server()


class TestSessionClientNonOverlay:
    """Test SessionClient through the non-overlay FusionAgentLoop."""

    _needs_server = True

    @pytest.mark.asyncio
    async def test_create_run_destroy(self, non_overlay_loop):
        client = non_overlay_loop.session_client
        sid = await client.create_session(session_id="test_no_ovl_01")
        assert sid == "test_no_ovl_01"
        try:
            result = await client.run_command(sid, "echo hello_non_overlay")
            assert result["status"] == "Success"
            assert "hello_non_overlay" in result["stdout"]
        finally:
            assert await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_cd_and_env_persistence(self, non_overlay_loop):
        client = non_overlay_loop.session_client
        sid = await client.create_session(session_id="test_no_ovl_02")
        try:
            await client.run_command(sid, "mkdir -p /tmp/test_cd_no && cd /tmp/test_cd_no")
            result = await client.run_command(sid, "pwd")
            assert "/tmp/test_cd_no" in result["stdout"]

            await client.run_command(sid, 'export TEST_VAR="persist_no_ovl"')
            result = await client.run_command(sid, "echo $TEST_VAR")
            assert "persist_no_ovl" in result["stdout"]
        finally:
            await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_files_at_create(self, non_overlay_loop):
        client = non_overlay_loop.session_client
        sid = await client.create_session(
            session_id="test_no_ovl_03",
            files={"greet.txt": _b64("hello from file")},
        )
        try:
            result = await client.run_command(sid, "cat greet.txt")
            assert result["status"] == "Success"
            assert "hello from file" in result["stdout"]
        finally:
            await client.destroy_session(sid)


class TestSessionClientOverlay:
    """Test SessionClient through the overlay FusionAgentLoopOverlay."""

    _needs_server = True

    @pytest.mark.asyncio
    async def test_create_run_destroy(self, overlay_loop):
        client = overlay_loop.session_client
        sid = await client.create_session(session_id="test_ovl_01")
        assert sid == "test_ovl_01"
        try:
            result = await client.run_command(sid, "echo hello_overlay")
            assert result["status"] == "Success"
            assert "hello_overlay" in result["stdout"]
        finally:
            assert await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_full_filesystem_visible(self, overlay_loop):
        client = overlay_loop.session_client
        sid = await client.create_session(session_id="test_ovl_02")
        try:
            result = await client.run_command(sid, "which bash")
            assert result["status"] == "Success"
            assert "/bin/bash" in result["stdout"]
        finally:
            await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_extra_files_at_absolute_paths(self, overlay_loop):
        client = overlay_loop.session_client
        sid = await client.create_session(
            session_id="test_ovl_03",
            extra_files={"opt/grading/data.json": _b64(json.dumps({"key": "val"}))},
        )
        try:
            result = await client.run_command(sid, "cat /opt/grading/data.json")
            assert result["status"] == "Success"
            data = json.loads(result["stdout"].strip())
            assert data["key"] == "val"
        finally:
            await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_cd_and_env_persistence(self, overlay_loop):
        client = overlay_loop.session_client
        sid = await client.create_session(session_id="test_ovl_04")
        try:
            await client.run_command(sid, "mkdir -p /tmp/test_cd_ovl && cd /tmp/test_cd_ovl")
            result = await client.run_command(sid, "pwd")
            assert "/tmp/test_cd_ovl" in result["stdout"]

            await client.run_command(sid, 'export MY_VAR="persist_ovl"')
            result = await client.run_command(sid, "echo $MY_VAR")
            assert "persist_ovl" in result["stdout"]
        finally:
            await client.destroy_session(sid)

    @pytest.mark.asyncio
    async def test_pwd_shows_agent_home(self, overlay_loop):
        client = overlay_loop.session_client
        sid = await client.create_session(session_id="test_ovl_05")
        try:
            result = await client.run_command(sid, "pwd")
            assert result["status"] == "Success"
            pwd = result["stdout"].strip()
            assert pwd.startswith("/home/agent_")
            assert "/tmp/ovl_" not in pwd
        finally:
            await client.destroy_session(sid)


class TestOverlayIsolation:
    """Verify that overlay sessions are isolated from each other."""

    _needs_server = True

    @pytest.mark.asyncio
    async def test_same_path_different_content(self, overlay_loop):
        """Two overlay sessions with the same extra_file path see their own version."""
        client = overlay_loop.session_client
        # Session IDs must differ in the first 8 chars (overlay uses session_id[:8] for mount)
        sid_a = await client.create_session(
            session_id="isopathA_aaaaaa",
            extra_files={"opt/grading/result.json": _b64(json.dumps({"who": "A"}))},
        )
        sid_b = await client.create_session(
            session_id="isopathB_bbbbbb",
            extra_files={"opt/grading/result.json": _b64(json.dumps({"who": "B"}))},
        )
        try:
            ra = await client.run_command(sid_a, "cat /opt/grading/result.json")
            rb = await client.run_command(sid_b, "cat /opt/grading/result.json")
            assert json.loads(ra["stdout"].strip())["who"] == "A"
            assert json.loads(rb["stdout"].strip())["who"] == "B"
        finally:
            await client.destroy_session(sid_a)
            await client.destroy_session(sid_b)

    @pytest.mark.asyncio
    async def test_write_isolation(self, overlay_loop):
        """A file written in one overlay session is invisible to another."""
        client = overlay_loop.session_client
        # Session IDs must differ in the first 8 chars (overlay uses session_id[:8] for mount)
        sid_a = await client.create_session(session_id="isowritA_aaaaaa")
        sid_b = await client.create_session(session_id="isowritB_bbbbbb")
        try:
            await client.run_command(sid_a, 'echo "secret" > /tmp/iso_test.txt')
            result = await client.run_command(sid_a, "cat /tmp/iso_test.txt")
            assert "secret" in result["stdout"]

            result = await client.run_command(sid_b, "cat /tmp/iso_test.txt")
            assert result["return_code"] != 0
        finally:
            await client.destroy_session(sid_a)
            await client.destroy_session(sid_b)


# ---------------------------------------------------------------------------
# 4. execute_agent_command integration (requires running server)
# ---------------------------------------------------------------------------

class TestExecuteAgentCommand:
    """Test FusionAgentLoop.execute_agent_command end-to-end."""

    _needs_server = True

    @pytest.mark.asyncio
    async def test_non_overlay_execute(self, non_overlay_loop):
        loop = non_overlay_loop
        loop.files_to_fetch = []
        sid = await loop.session_client.create_session(session_id="test_exec_no")
        loop.current_session_id = sid
        try:
            output, files = await loop.execute_agent_command("echo from_execute")
            assert "from_execute" in output
            assert isinstance(files, np.ndarray)
        finally:
            await loop.session_client.destroy_session(sid)
            loop.current_session_id = None

    @pytest.mark.asyncio
    async def test_overlay_execute(self, overlay_loop):
        loop = overlay_loop
        loop.files_to_fetch = []
        sid = await loop.session_client.create_session(session_id="test_exec_ovl")
        loop.current_session_id = sid
        try:
            output, files = await loop.execute_agent_command("echo from_overlay_execute")
            assert "from_overlay_execute" in output
            assert isinstance(files, np.ndarray)
        finally:
            await loop.session_client.destroy_session(sid)
            loop.current_session_id = None

    @pytest.mark.asyncio
    async def test_execute_with_fetch_files(self, overlay_loop):
        loop = overlay_loop
        loop.files_to_fetch = ["output.txt"]
        sid = await loop.session_client.create_session(session_id="test_exec_fetch")
        loop.current_session_id = sid
        try:
            await loop.execute_agent_command('echo "fetched" > output.txt')
            output, files = await loop.execute_agent_command("echo done")
            assert "done" in output
            fetched = files.item()
            assert "output.txt" in fetched
            assert "fetched" in fetched["output.txt"]
        finally:
            await loop.session_client.destroy_session(sid)
            loop.current_session_id = None

    @pytest.mark.asyncio
    async def test_execute_failed_command(self, non_overlay_loop):
        loop = non_overlay_loop
        loop.files_to_fetch = []
        sid = await loop.session_client.create_session(session_id="test_exec_fail")
        loop.current_session_id = sid
        try:
            output, _ = await loop.execute_agent_command("cat /nonexistent/file")
            assert "Execution Failed" in output or "No such file" in output
        finally:
            await loop.session_client.destroy_session(sid)
            loop.current_session_id = None

    @pytest.mark.asyncio
    async def test_execute_state_persists(self, overlay_loop):
        """cd and export in one execute_agent_command persist to the next."""
        loop = overlay_loop
        loop.files_to_fetch = []
        sid = await loop.session_client.create_session(session_id="test_exec_state")
        loop.current_session_id = sid
        try:
            await loop.execute_agent_command('export MY_EXEC_VAR="yes"')
            await loop.execute_agent_command("mkdir -p /tmp/exec_cd && cd /tmp/exec_cd")

            out_var, _ = await loop.execute_agent_command("echo $MY_EXEC_VAR")
            assert "yes" in out_var

            out_pwd, _ = await loop.execute_agent_command("pwd")
            assert "/tmp/exec_cd" in out_pwd
        finally:
            await loop.session_client.destroy_session(sid)
            loop.current_session_id = None


# ---------------------------------------------------------------------------
# 5. Session cleanup
# ---------------------------------------------------------------------------

class TestSessionCleanup:
    _needs_server = True

    @pytest.mark.asyncio
    async def test_close_cleans_http_sessions(self, non_overlay_loop):
        loop = non_overlay_loop
        sid = await loop.session_client.create_session(session_id="test_close_01")
        await loop.session_client.run_command(sid, "echo hi")
        await loop.session_client.destroy_session(sid)
        await loop.session_client.close()
        assert len(loop.session_client._http_sessions) == 0
        assert len(loop.session_client._session_endpoints) == 0

    @pytest.mark.asyncio
    async def test_overlay_close_cleans_http_sessions(self, overlay_loop):
        loop = overlay_loop
        sid = await loop.session_client.create_session(session_id="test_close_02")
        await loop.session_client.run_command(sid, "echo hi")
        await loop.session_client.destroy_session(sid)
        await loop.session_client.close()
        assert len(loop.session_client._http_sessions) == 0
        assert len(loop.session_client._session_endpoints) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
