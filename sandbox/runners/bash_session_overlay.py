# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
OverlayFS-isolated session-based bash execution.

Each session gets its own filesystem view via OverlayFS:
- lowerdir = container root / (read-only, shared)
- upperdir = session-specific writable layer (on tmpfs)
- merged   = combined view, used as chroot for commands

This provides full filesystem isolation between sessions:
- extra_files at absolute paths are only visible to their session
- writes in one session don't affect other sessions
- destroy unmounts everything, leaving no traces

Requires: CAP_SYS_ADMIN (for mount/umount syscalls inside the container).
Add --cap-add SYS_ADMIN to docker run.
"""

import asyncio
import base64
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

import psutil
import structlog

from sandbox.runners.base import restore_files
from sandbox.utils.execution import get_tmp_dir

logger = structlog.stdlib.get_logger()


@dataclass
class OverlaySession:
    """A session with its own OverlayFS filesystem view."""

    session_id: str
    overlay_base: str     # /tmp/ovl_{id} — tmpfs mount point for upper/work
    merged_root: str      # /tmp/ovl_{id}/merged — chroot target
    working_dir: str      # /home/agent_{id} inside the chroot (what the agent sees)
    cwd: str              # Current working directory (inside chroot)
    env_file: str         # Path to env file (inside chroot)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    command_count: int = 0

    def update_last_used(self):
        self.last_used = time.time()
        self.command_count += 1


class OverlaySessionManager:
    """
    Session manager with per-session OverlayFS isolation.

    Each session gets a private overlay filesystem. Commands run inside
    a chroot so the agent sees a normal Linux filesystem at / but all
    writes go to a session-specific tmpfs-backed upper layer.

    Requires CAP_SYS_ADMIN in the Docker container.
    """

    def __init__(
        self,
        max_sessions: int = 15000,
        session_timeout: float = 3600,
        cleanup_interval: float = 60,
        max_concurrent_commands: int = 32,
    ):
        self.sessions: Dict[str, OverlaySession] = {}
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval
        self.max_concurrent_commands = max_concurrent_commands
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cmd_semaphore: Optional[asyncio.Semaphore] = None
        self.timeout_count = 0
        self.command_count = 0
        self.process_kill_failures = 0

    async def start(self):
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._cmd_semaphore = asyncio.Semaphore(self.max_concurrent_commands)
            logger.info(f"OverlaySessionManager started with max_concurrent_commands={self.max_concurrent_commands}")

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        async with self._lock:
            session_ids = list(self.sessions.keys())
        for sid in session_ids:
            await self.destroy_session(sid)
        logger.info("OverlaySessionManager stopped")

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_idle_sessions(self):
        now = time.time()
        to_remove = []
        async with self._lock:
            for sid, session in self.sessions.items():
                if now - session.last_used > self.session_timeout:
                    to_remove.append(sid)
        for sid in to_remove:
            logger.info(f"Cleaning up idle overlay session: {sid}")
            await self.destroy_session(sid)

    def _setup_overlay(self, session_id: str) -> tuple:
        """Set up OverlayFS mounts for a session.

        Returns (overlay_base, merged_root).
        """
        overlay_base = os.path.join(get_tmp_dir(), f"ovl_{session_id[:8]}")
        os.makedirs(overlay_base, exist_ok=True)

        # Mount tmpfs for upper/work dirs (avoids overlay-on-overlay issue)
        subprocess.run(
            ["mount", "-t", "tmpfs", "tmpfs", overlay_base],
            check=True, capture_output=True,
        )

        upper = os.path.join(overlay_base, "upper")
        work = os.path.join(overlay_base, "work")
        merged = os.path.join(overlay_base, "merged")
        os.makedirs(upper)
        os.makedirs(work)
        os.makedirs(merged)

        # Mount overlayfs: lower=container root, upper=session-private tmpfs
        subprocess.run(
            [
                "mount", "-t", "overlay", "overlay",
                "-o", f"lowerdir=/,upperdir={upper},workdir={work}",
                merged,
            ],
            check=True, capture_output=True,
        )

        return overlay_base, merged

    def _teardown_overlay(self, session: OverlaySession):
        """Unmount and remove overlay mounts."""
        # Unmount overlay first, then tmpfs
        subprocess.run(["umount", "-l", session.merged_root], capture_output=True)
        subprocess.run(["umount", "-l", session.overlay_base], capture_output=True)
        shutil.rmtree(session.overlay_base, ignore_errors=True)

    async def create_session(
        self,
        session_id: Optional[str] = None,
        files: Optional[Dict[str, Optional[str]]] = None,
        extra_files: Optional[Dict[str, Optional[str]]] = None,
        startup_commands: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a new overlay-isolated session.

        Args:
            session_id: Optional client-provided session ID.
            files: Dict of filename -> base64 content (placed in working dir).
            extra_files: Dict of absolute_path -> base64 content (placed at
                         absolute paths inside the session's overlay — invisible
                         to other sessions).
            startup_commands: Commands to run on init.
            env: Additional environment variables.

        Returns:
            session_id
        """
        if session_id is None:
            session_id = uuid4().hex

        async with self._lock:
            if len(self.sessions) >= self.max_sessions:
                raise RuntimeError(f"Max sessions ({self.max_sessions}) reached")
            if session_id in self.sessions:
                raise RuntimeError(f"Session {session_id} already exists")

        # Set up overlay filesystem
        overlay_base, merged_root = self._setup_overlay(session_id)

        # Create working directory inside the merged view
        # The agent sees this as /home/agent_XXXX
        agent_dir_name = f"agent_{session_id[:8]}"
        working_dir_on_host = os.path.join(merged_root, "home", agent_dir_name)
        os.makedirs(working_dir_on_host, exist_ok=True)

        # Path as seen from inside the chroot
        working_dir = f"/home/{agent_dir_name}"

        # Restore regular files into the working dir (inside merged view)
        if files:
            restore_files(working_dir_on_host, files)

        # Restore extra files at absolute paths inside the merged view
        if extra_files:
            restore_files(merged_root, extra_files)

        # Create env file (inside the chroot-visible working dir)
        env_file_on_host = os.path.join(working_dir_on_host, ".session_env")
        with open(env_file_on_host, 'w') as f:
            f.write("# Session environment\n")
            if env:
                for k, v in env.items():
                    f.write(f"export {k}={shlex.quote(v)}\n")

        # Create cwd file
        cwd_file_on_host = os.path.join(working_dir_on_host, ".session_cwd")
        with open(cwd_file_on_host, 'w') as f:
            f.write(working_dir)

        # Paths as seen inside the chroot
        env_file = os.path.join(working_dir, ".session_env")

        session = OverlaySession(
            session_id=session_id,
            overlay_base=overlay_base,
            merged_root=merged_root,
            working_dir=working_dir,
            cwd=working_dir,
            env_file=env_file,
        )

        async with self._lock:
            self.sessions[session_id] = session

        # Run startup commands
        if startup_commands:
            for cmd in startup_commands:
                await self.run_command(session_id, cmd, timeout=5)

        logger.info(f"Created overlay session {session_id} (merged={merged_root}, working_dir={working_dir})")
        return session_id

    async def run_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 10,
        fetch_files: Optional[List[str]] = None,
    ) -> Dict:
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return {
                    "status": "Failed",
                    "message": f"Session {session_id} not found",
                    "stdout": "",
                    "stderr": f"Session {session_id} not found",
                    "return_code": -1,
                    "files": {},
                }

        session.update_last_used()
        self.command_count += 1

        if self._cmd_semaphore:
            await self._cmd_semaphore.acquire()

        try:
            return await self._run_command_impl(session, command, timeout, fetch_files)
        finally:
            if self._cmd_semaphore:
                self._cmd_semaphore.release()

    async def _run_command_impl(
        self,
        session: OverlaySession,
        command: str,
        timeout: float,
        fetch_files: Optional[List[str]],
    ) -> Dict:
        """Run a command inside the session's chrooted overlay filesystem."""
        # Paths as seen inside the chroot
        env_file = session.env_file
        cwd_file = os.path.join(session.working_dir, ".session_cwd")

        # Build the command that runs inside chroot
        # We use chroot to enter the merged overlay filesystem
        inner_cmd = (
            f'set -o pipefail\n'
            f'source "{env_file}" 2>/dev/null || true\n'
            f'cd "$(cat "{cwd_file}")" 2>/dev/null || cd "{session.working_dir}"\n'
            f'{command}\n'
            f'__exit_code=$?\n'
            f'pwd > "{cwd_file}"\n'
            f'export -p > "{env_file}.new" && mv "{env_file}.new" "{env_file}"\n'
            f'exit $__exit_code\n'
        )

        wrapped_cmd = f'chroot {session.merged_root} /bin/bash -c {shlex.quote(inner_cmd)}'

        try:
            proc = await asyncio.create_subprocess_shell(
                wrapped_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                executable='/bin/bash',
                start_new_session=True,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
                return_code = proc.returncode
            except asyncio.TimeoutError:
                self.timeout_count += 1
                logger.warning(f"Command timed out ({self.timeout_count} total), killing PID {proc.pid}")

                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

                await asyncio.sleep(0.5)

                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

                try:
                    parent = psutil.Process(proc.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                except psutil.NoSuchProcess:
                    pass

                await asyncio.sleep(0.2)
                await proc.wait()
                return {
                    "status": "Failed",
                    "message": "Command timed out",
                    "stdout": "",
                    "stderr": "Command timed out",
                    "return_code": -1,
                    "files": {},
                }

            # Update cwd from file on host side
            cwd_file_on_host = os.path.join(
                session.merged_root,
                session.working_dir.lstrip("/"),
                ".session_cwd",
            )
            try:
                with open(cwd_file_on_host, 'r') as f:
                    session.cwd = f.read().strip()
            except Exception:
                pass

            # Fetch files (resolve paths relative to chroot)
            files = {}
            if fetch_files:
                for filepath in fetch_files:
                    for base_in_chroot in [session.cwd, session.working_dir]:
                        host_path = os.path.join(
                            session.merged_root,
                            base_in_chroot.lstrip("/"),
                            filepath,
                        )
                        if os.path.isfile(host_path):
                            try:
                                with open(host_path, 'rb') as f:
                                    files[filepath] = base64.b64encode(f.read()).decode()
                                break
                            except Exception as e:
                                logger.debug(f"Failed to read {filepath}: {e}")

            status = "Success" if return_code == 0 else "Failed"

            return {
                "status": status,
                "message": "",
                "stdout": stdout.decode('utf-8', errors='replace'),
                "stderr": stderr.decode('utf-8', errors='replace'),
                "return_code": return_code,
                "files": files,
            }

        except Exception as e:
            logger.error(f"Error running command: {e}")
            return {
                "status": "Failed",
                "message": str(e),
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
                "files": {},
            }

    async def destroy_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self.sessions.pop(session_id, None)

        if not session:
            return False

        try:
            self._teardown_overlay(session)
            logger.info(f"Destroyed overlay session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error destroying overlay session {session_id}: {e}")
            return False

    async def get_session_info(self, session_id: str) -> Optional[Dict]:
        async with self._lock:
            session = self.sessions.get(session_id)

        if not session:
            return None

        return {
            "session_id": session.session_id,
            "working_dir": session.working_dir,
            "cwd": session.cwd,
            "created_at": session.created_at,
            "last_used": session.last_used,
            "command_count": session.command_count,
            "age_seconds": time.time() - session.created_at,
            "idle_seconds": time.time() - session.last_used,
        }

    async def list_sessions(self) -> List[Dict]:
        async with self._lock:
            session_ids = list(self.sessions.keys())

        sessions = []
        for sid in session_ids:
            info = await self.get_session_info(sid)
            if info:
                sessions.append(info)
        return sessions


# Global instance
_overlay_session_manager: Optional[OverlaySessionManager] = None


def get_overlay_session_manager() -> OverlaySessionManager:
    global _overlay_session_manager
    if _overlay_session_manager is None:
        _overlay_session_manager = OverlaySessionManager()
    return _overlay_session_manager


async def init_overlay_session_manager(**kwargs):
    global _overlay_session_manager
    if 'max_concurrent_commands' not in kwargs:
        kwargs['max_concurrent_commands'] = int(os.environ.get('MAX_CONCURRENT_COMMANDS', '32'))
    _overlay_session_manager = OverlaySessionManager(**kwargs)
    await _overlay_session_manager.start()
    return _overlay_session_manager


async def shutdown_overlay_session_manager():
    global _overlay_session_manager
    if _overlay_session_manager:
        await _overlay_session_manager.stop()
        _overlay_session_manager = None
