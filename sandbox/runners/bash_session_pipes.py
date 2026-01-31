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
High-performance session-based bash execution using subprocess pipes.

This version uses subprocess pipes instead of PTY for much higher scalability.
Supports 10,000+ concurrent sessions.

Key difference from PTY version:
- Uses asyncio.subprocess with pipes instead of pty.fork()
- Each command runs in a subshell that sources saved state
- State (cwd, env) is saved after each command
- Scales to 10,000+ sessions (no PTY limit)

Trade-offs:
- Slightly higher overhead per command (~10-20ms for state save/restore)
- Some edge cases with interactive commands won't work
- Shell variables don't persist (only exported env vars and cwd)
"""

import asyncio
import base64
import os
import shutil
import signal
import tempfile
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
class BashSession:
    """A stateful bash session with persistent working directory and environment."""
    
    session_id: str
    working_dir: str
    cwd: str  # Current working directory within session
    env_file: str  # Path to saved environment file
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    command_count: int = 0
    
    def update_last_used(self):
        self.last_used = time.time()
        self.command_count += 1


class BashSessionManager:
    """
    High-performance session manager using subprocess pipes.
    
    Designed for 10,000+ concurrent sessions.
    
    Concurrency is limited via semaphore to prevent system overload.
    Configure via MAX_CONCURRENT_COMMANDS env var (default: 32).
    """
    
    def __init__(
        self,
        max_sessions: int = 15000,
        session_timeout: float = 3600,
        cleanup_interval: float = 60,
        max_concurrent_commands: int = 32,
    ):
        self.sessions: Dict[str, BashSession] = {}
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval
        self.max_concurrent_commands = max_concurrent_commands
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        # Semaphore to limit concurrent command executions
        self._cmd_semaphore: Optional[asyncio.Semaphore] = None
        # Metrics tracking
        self.timeout_count = 0
        self.command_count = 0
        self.process_kill_failures = 0
        
    async def start(self):
        """Start the session manager."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._cmd_semaphore = asyncio.Semaphore(self.max_concurrent_commands)
            logger.info(f"BashSessionManager (pipes) started with max_concurrent_commands={self.max_concurrent_commands}")
            
    async def stop(self):
        """Stop the session manager."""
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
        logger.info("BashSessionManager (pipes) stopped")
    
    async def _cleanup_loop(self):
        """Periodically clean up idle sessions."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                
    async def _cleanup_idle_sessions(self):
        """Remove idle sessions."""
        now = time.time()
        to_remove = []
        
        async with self._lock:
            for sid, session in self.sessions.items():
                if now - session.last_used > self.session_timeout:
                    to_remove.append(sid)
                    
        for sid in to_remove:
            logger.info(f"Cleaning up idle session: {sid}")
            await self.destroy_session(sid)
    
    async def create_session(
        self,
        session_id: Optional[str] = None,
        files: Optional[Dict[str, Optional[str]]] = None,
        startup_commands: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a new session.
        
        Args:
            session_id: Optional client-provided session ID for consistent hashing.
                        If None, a UUID is generated server-side.
            files: Dict of filename -> base64-encoded content to pre-populate.
            startup_commands: Commands to run on session initialization.
            env: Additional environment variables for the session.
            
        Returns:
            The session_id (either client-provided or server-generated).
        """
        # Use client-provided session_id or generate one
        if session_id is None:
            session_id = uuid4().hex
            
        async with self._lock:
            if len(self.sessions) >= self.max_sessions:
                raise RuntimeError(f"Max sessions ({self.max_sessions}) reached")
            # Check if session_id already exists (client error)
            if session_id in self.sessions:
                raise RuntimeError(f"Session {session_id} already exists")
        working_dir = tempfile.mkdtemp(dir=get_tmp_dir(), prefix=f"sess_{session_id[:8]}_")
        
        # Restore files
        if files:
            restore_files(working_dir, files)
        
        # Create env file with initial environment
        env_file = os.path.join(working_dir, ".session_env")
        with open(env_file, 'w') as f:
            f.write("# Session environment\n")
            if env:
                for k, v in env.items():
                    f.write(f"export {k}={shutil.quote(v)}\n")
        
        # Create cwd file
        cwd_file = os.path.join(working_dir, ".session_cwd")
        with open(cwd_file, 'w') as f:
            f.write(working_dir)
        
        session = BashSession(
            session_id=session_id,
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
        
        logger.info(f"Created session {session_id} in {working_dir}")
        return session_id
    
    async def run_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 10,
        fetch_files: Optional[List[str]] = None,
    ) -> Dict:
        """Run a command in a session with state persistence.
        
        Uses semaphore to limit concurrent command executions server-side.
        """
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

        # Acquire semaphore to limit concurrent commands
        if self._cmd_semaphore:
            await self._cmd_semaphore.acquire()
        
        try:
            return await self._run_command_impl(session, command, timeout, fetch_files)
        finally:
            if self._cmd_semaphore:
                self._cmd_semaphore.release()
    
    async def _run_command_impl(
        self,
        session: BashSession,
        command: str,
        timeout: float,
        fetch_files: Optional[List[str]],
    ) -> Dict:
        """Internal command execution (called with semaphore held)."""
        # Build command that:
        # 1. Sources saved environment
        # 2. Changes to saved cwd
        # 3. Runs the user command
        # 4. Saves new cwd and env
        env_file = session.env_file
        cwd_file = os.path.join(session.working_dir, ".session_cwd")
        
        # Wrapper script that handles state
        wrapped_cmd = f'''
set -o pipefail
source "{env_file}" 2>/dev/null || true
cd "$(cat "{cwd_file}")" 2>/dev/null || cd "{session.working_dir}"
{command}
__exit_code=$?
pwd > "{cwd_file}"
export -p > "{env_file}.new" && mv "{env_file}.new" "{env_file}"
exit $__exit_code
'''
        
        try:
            # Use start_new_session=True to create a new process group
            # This allows us to kill all child processes on timeout
            proc = await asyncio.create_subprocess_shell(
                wrapped_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=session.working_dir,
                executable='/bin/bash',
                start_new_session=True,  # Create new process group for clean killing
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout
                )
                return_code = proc.returncode
            except asyncio.TimeoutError:
                self.timeout_count += 1
                logger.warning(f"Command timed out ({self.timeout_count} total timeouts), killing process tree for PID {proc.pid}")

                # First attempt: killpg to terminate process group
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    logger.debug(f"Sent SIGTERM to process group {proc.pid}")
                except (ProcessLookupError, OSError) as e:
                    logger.debug(f"killpg failed: {e}, will use psutil fallback")

                await asyncio.sleep(0.5)  # Grace period

                # Second attempt: Force kill with killpg
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    logger.debug(f"Sent SIGKILL to process group {proc.pid}")
                except (ProcessLookupError, OSError):
                    pass

                # Third attempt: Use psutil to recursively kill descendants
                children_to_reap = []  # Track PIDs for reaping to prevent zombies
                try:
                    parent = psutil.Process(proc.pid)
                    children = parent.children(recursive=True)

                    if children:
                        child_pids = [c.pid for c in children]
                        logger.warning(f"Found {len(children)} surviving children: {child_pids}")

                        # Kill children first and track for reaping
                        for child in children:
                            try:
                                child.kill()  # SIGKILL
                                children_to_reap.append(child.pid)  # Track PID for reaping
                            except psutil.NoSuchProcess:
                                pass

                    # Kill parent
                    try:
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass

                except psutil.NoSuchProcess:
                    logger.debug(f"Process {proc.pid} already terminated")

                # Reap all killed children to prevent zombies
                reaped_count = 0
                for child_pid in children_to_reap:
                    try:
                        # Use waitpid with WNOHANG to reap without blocking
                        pid, status = os.waitpid(child_pid, os.WNOHANG)
                        if pid != 0:  # Successfully reaped
                            reaped_count += 1
                    except ChildProcessError:
                        # Already reaped by someone else or not our child
                        pass
                    except ProcessLookupError:
                        # Process doesn't exist
                        pass

                if reaped_count > 0:
                    logger.debug(f"Reaped {reaped_count} child processes")

                # Verify all processes are dead (not zombies)
                await asyncio.sleep(0.2)
                try:
                    parent = psutil.Process(proc.pid)
                    remaining_children = parent.children(recursive=True)
                    if remaining_children:
                        remaining_pids = [c.pid for c in remaining_children]
                        self.process_kill_failures += 1
                        logger.error(f"ERROR: {len(remaining_children)} processes STILL ALIVE: {remaining_pids}")
                except psutil.NoSuchProcess:
                    logger.debug("All processes successfully killed and reaped")

                # Wait for process to be reaped
                await proc.wait()
                return {
                    "status": "Failed",
                    "message": "Command timed out",
                    "stdout": "",
                    "stderr": "Command timed out",
                    "return_code": -1,
                    "files": {},
                }
            
            # Update session cwd from saved file
            try:
                with open(cwd_file, 'r') as f:
                    session.cwd = f.read().strip()
            except:
                pass
            
            # Fetch requested files
            files = {}
            if fetch_files:
                for filepath in fetch_files:
                    # Try relative to current cwd first, then working_dir
                    for base in [session.cwd, session.working_dir]:
                        full_path = os.path.join(base, filepath)
                        if os.path.isfile(full_path):
                            try:
                                with open(full_path, 'rb') as f:
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
        """Destroy a session."""
        async with self._lock:
            session = self.sessions.pop(session_id, None)
            
        if not session:
            return False
        
        try:
            if os.path.exists(session.working_dir):
                shutil.rmtree(session.working_dir, ignore_errors=True)
            logger.info(f"Destroyed session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error destroying session {session_id}: {e}")
            return False
    
    async def get_session_info(self, session_id: str) -> Optional[Dict]:
        """Get session info."""
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
        """List all sessions."""
        async with self._lock:
            session_ids = list(self.sessions.keys())
        
        sessions = []
        for sid in session_ids:
            info = await self.get_session_info(sid)
            if info:
                sessions.append(info)
        return sessions


# Global instance
_session_manager: Optional[BashSessionManager] = None


def get_session_manager() -> BashSessionManager:
    """Get the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = BashSessionManager()
    return _session_manager


async def init_session_manager(**kwargs):
    """Initialize the session manager.
    
    Reads MAX_CONCURRENT_COMMANDS from environment (default: 32).
    """
    global _session_manager
    # Read concurrency limit from environment if not explicitly provided
    if 'max_concurrent_commands' not in kwargs:
        kwargs['max_concurrent_commands'] = int(os.environ.get('MAX_CONCURRENT_COMMANDS', '32'))
    _session_manager = BashSessionManager(**kwargs)
    await _session_manager.start()
    return _session_manager


async def shutdown_session_manager():
    """Shutdown the session manager."""
    global _session_manager
    if _session_manager:
        await _session_manager.stop()
        _session_manager = None
