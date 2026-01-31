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
Session-based bash execution for stateful command execution within episodes.

This module provides stateful bash sessions that maintain:
- Working directory (cd commands persist)
- Environment variables (export commands persist)
- Shell variables
- File system state

Sessions are isolated from each other and cleaned up on destruction.

Usage:
    1. Create a session: POST /session/create
    2. Run commands: POST /session/run  
    3. Destroy session: POST /session/destroy
"""

import asyncio
import base64
import os
import pty
import select
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

import psutil
import structlog

from sandbox.runners.base import restore_files, get_tmp_dir

logger = structlog.stdlib.get_logger()


def kill_process_tree(pid: int, sig=signal.SIGTERM):
    """Kill a process and all its children."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        child_pids = [c.pid for c in children]
        if child_pids:
            logger.debug(f"Killing {len(children)} child processes: {child_pids}")

        # Kill children first
        for child in children:
            try:
                child.send_signal(sig)
            except psutil.NoSuchProcess:
                pass

        # Kill parent
        try:
            parent.send_signal(sig)
            logger.debug(f"Sent signal {sig} to parent process {pid}")
        except psutil.NoSuchProcess:
            logger.debug(f"Process {pid} already gone")

    except psutil.NoSuchProcess:
        logger.debug(f"Process tree {pid} does not exist")


def _count_process_tree(pid: int) -> int:
    """Count processes in tree (for verification)."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        return 1 + len(children)  # parent + children
    except psutil.NoSuchProcess:
        return 0


# Unique markers for output parsing - unlikely to appear in normal output
OUTPUT_START_MARKER = "___SANDBOX_OUTPUT_START_8f3a2b1c___"
OUTPUT_END_MARKER = "___SANDBOX_OUTPUT_END_8f3a2b1c___"
RETURN_CODE_MARKER = "___SANDBOX_RC_8f3a2b1c___"


@dataclass
class BashSession:
    """A stateful bash session with a persistent process and working directory."""
    
    session_id: str
    working_dir: str
    master_fd: int  # PTY master file descriptor
    pid: int  # Bash process PID
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    command_count: int = 0
    
    def update_last_used(self):
        self.last_used = time.time()
        self.command_count += 1


class BashSessionManager:
    """
    Manages multiple concurrent bash sessions.
    
    Thread-safe and designed for high concurrency:
    - Each session has its own bash process
    - Sessions are isolated via separate working directories
    - Automatic cleanup of idle sessions
    """
    
    def __init__(
        self, 
        max_sessions: int = 10000,
        session_timeout: float = 3600,  # 1 hour default
        cleanup_interval: float = 60,   # Check every minute
    ):
        self.sessions: Dict[str, BashSession] = {}
        self.max_sessions = max_sessions
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Start the session manager and cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("BashSessionManager started")
            
    async def stop(self):
        """Stop the session manager and destroy all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            
        # Destroy all sessions
        async with self._lock:
            session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            await self.destroy_session(session_id)
        logger.info("BashSessionManager stopped")
    
    async def _cleanup_loop(self):
        """Periodically clean up idle sessions."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
                
    async def _cleanup_idle_sessions(self):
        """Remove sessions that have been idle for too long."""
        now = time.time()
        to_remove = []
        
        async with self._lock:
            for session_id, session in self.sessions.items():
                if now - session.last_used > self.session_timeout:
                    to_remove.append(session_id)
                    
        for session_id in to_remove:
            logger.info(f"Cleaning up idle session: {session_id}")
            await self.destroy_session(session_id)
    
    async def create_session(
        self,
        files: Optional[Dict[str, Optional[str]]] = None,
        startup_commands: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Create a new bash session.
        
        Args:
            files: Dict of filename -> base64-encoded content to pre-populate
            startup_commands: Commands to run on session start (e.g., cd, export)
            env: Additional environment variables
            
        Returns:
            session_id: Unique identifier for this session
        """
        async with self._lock:
            if len(self.sessions) >= self.max_sessions:
                raise RuntimeError(f"Maximum sessions ({self.max_sessions}) reached")
        
        session_id = uuid4().hex
        working_dir = tempfile.mkdtemp(dir=get_tmp_dir(), prefix=f"session_{session_id[:8]}_")
        
        # Restore any provided files
        if files:
            restore_files(working_dir, files)
        
        # Set up environment
        session_env = os.environ.copy()
        session_env['HOME'] = working_dir
        session_env['PS1'] = ''  # Disable prompt for cleaner output
        session_env['TERM'] = 'dumb'  # Simple terminal
        if env:
            session_env.update(env)
        
        # Create PTY and spawn bash
        master_fd, slave_fd = pty.openpty()
        
        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)  # stdin
            os.dup2(slave_fd, 1)  # stdout
            os.dup2(slave_fd, 2)  # stderr
            os.close(slave_fd)
            os.chdir(working_dir)
            os.execve('/bin/bash', ['bash', '--norc', '--noprofile', '-i'], session_env)
        
        # Parent process
        os.close(slave_fd)
        
        session = BashSession(
            session_id=session_id,
            working_dir=working_dir,
            master_fd=master_fd,
            pid=pid,
        )
        
        # Store session
        async with self._lock:
            self.sessions[session_id] = session
        
        # Wait for bash to be ready and clear initial output
        await asyncio.sleep(0.05)
        self._read_available(master_fd, timeout=0.1)
        
        # Run startup commands if provided
        if startup_commands:
            for cmd in startup_commands:
                await self.run_command(session_id, cmd, timeout=5)
        
        logger.info(f"Created session {session_id} in {working_dir}")
        return session_id
    
    def _read_available(self, fd: int, timeout: float = 0.1) -> str:
        """Read all available data from fd with timeout."""
        output = []
        end_time = time.time() + timeout
        
        while time.time() < end_time:
            r, _, _ = select.select([fd], [], [], 0.01)
            if r:
                try:
                    data = os.read(fd, 4096)
                    if data:
                        output.append(data.decode('utf-8', errors='replace'))
                    else:
                        break
                except OSError:
                    break
            else:
                # No data available
                if output:
                    break
        
        return ''.join(output)
    
    async def run_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 10,
        fetch_files: Optional[List[str]] = None,
    ) -> Dict:
        """
        Run a command in an existing session.
        
        Args:
            session_id: Session identifier
            command: Bash command to execute
            timeout: Maximum execution time
            fetch_files: Files to retrieve after execution
            
        Returns:
            Dict with status, stdout, stderr, return_code, files
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
        
        # Build wrapped command that captures output cleanly
        # Use markers to delimit output and capture return code
        wrapped_cmd = (
            f'echo "{OUTPUT_START_MARKER}"\n'
            f'{command}\n'
            f'__rc=$?\n'
            f'echo "{OUTPUT_END_MARKER}"\n'
            f'echo "{RETURN_CODE_MARKER}$__rc"\n'
        )
        
        try:
            # Send command
            os.write(session.master_fd, wrapped_cmd.encode())
            
            # Read output with timeout
            output = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: self._read_until_marker(session.master_fd, timeout)
                ),
                timeout=timeout + 1
            )
            
            # Parse output
            stdout, return_code = self._parse_output(output)
            
            # Fetch requested files
            files = {}
            if fetch_files:
                for filepath in fetch_files:
                    full_path = os.path.join(session.working_dir, filepath)
                    if os.path.isfile(full_path):
                        try:
                            with open(full_path, 'rb') as f:
                                files[filepath] = base64.b64encode(f.read()).decode()
                        except Exception as e:
                            logger.debug(f"Failed to read {filepath}: {e}")
            
            status = "Success" if return_code == 0 else "Failed"
            
            return {
                "status": status,
                "message": "",
                "stdout": stdout,
                "stderr": "",  # PTY combines stdout/stderr
                "return_code": return_code,
                "files": files,
            }
            
        except asyncio.TimeoutError:
            # Kill all child processes of the bash session when timeout occurs
            logger.warning(f"Command timed out in session {session_id}, killing process group")

            # Kill entire process group atomically
            try:
                # Bash process created with setsid(), so it's a session leader
                # Kill the entire session group
                os.killpg(session.pid, signal.SIGTERM)
                await asyncio.sleep(0.5)
                os.killpg(session.pid, signal.SIGKILL)
                logger.info(f"Successfully killed process group {session.pid}")
            except ProcessLookupError:
                logger.debug(f"Process group {session.pid} already terminated")
            except OSError as e:
                logger.warning(f"Failed to kill process group {session.pid}: {e}, falling back to psutil")
                # Fallback to psutil method
                kill_process_tree(session.pid, signal.SIGTERM)
                await asyncio.sleep(0.5)
                kill_process_tree(session.pid, signal.SIGKILL)

            # Verify processes are gone
            remaining = _count_process_tree(session.pid)
            if remaining > 0:
                logger.error(f"Warning: {remaining} processes still alive after kill in session {session_id}")

            return {
                "status": "Failed",
                "message": "Command timed out",
                "stdout": "",
                "stderr": "Command execution timed out and processes were terminated",
                "return_code": -1,
                "files": {},
            }
        except Exception as e:
            logger.error(f"Error running command in session {session_id}: {e}")
            return {
                "status": "Failed", 
                "message": str(e),
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
                "files": {},
            }
    
    def _read_until_marker(self, fd: int, timeout: float) -> str:
        """Read from fd until we see the end marker or timeout."""
        output = []
        end_time = time.time() + timeout
        
        while time.time() < end_time:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
                
            r, _, _ = select.select([fd], [], [], min(0.1, remaining))
            if r:
                try:
                    data = os.read(fd, 4096)
                    if data:
                        output.append(data.decode('utf-8', errors='replace'))
                        # Check if we've seen the return code marker
                        full_output = ''.join(output)
                        if RETURN_CODE_MARKER in full_output:
                            # Wait a tiny bit for any trailing output
                            time.sleep(0.01)
                            r2, _, _ = select.select([fd], [], [], 0.01)
                            if r2:
                                try:
                                    extra = os.read(fd, 4096)
                                    if extra:
                                        output.append(extra.decode('utf-8', errors='replace'))
                                except OSError:
                                    pass
                            break
                except OSError:
                    break
        
        return ''.join(output)
    
    def _parse_output(self, raw_output: str) -> tuple:
        """Parse the wrapped command output to extract stdout and return code."""
        stdout = ""
        return_code = 0
        
        # Find content between markers
        if OUTPUT_START_MARKER in raw_output and OUTPUT_END_MARKER in raw_output:
            start_idx = raw_output.find(OUTPUT_START_MARKER) + len(OUTPUT_START_MARKER)
            end_idx = raw_output.find(OUTPUT_END_MARKER)
            stdout = raw_output[start_idx:end_idx].strip()
            # Remove the echo command itself if it appears
            if stdout.startswith('\n'):
                stdout = stdout[1:]
        
        # Extract return code
        if RETURN_CODE_MARKER in raw_output:
            rc_start = raw_output.find(RETURN_CODE_MARKER) + len(RETURN_CODE_MARKER)
            rc_end = raw_output.find('\n', rc_start)
            if rc_end == -1:
                rc_end = len(raw_output)
            try:
                return_code = int(raw_output[rc_start:rc_end].strip())
            except ValueError:
                pass
        
        return stdout, return_code
    
    async def destroy_session(self, session_id: str) -> bool:
        """
        Destroy a session and clean up resources.
        
        Args:
            session_id: Session to destroy
            
        Returns:
            True if session was found and destroyed
        """
        async with self._lock:
            session = self.sessions.pop(session_id, None)
            
        if not session:
            return False
        
        try:
            # Close PTY
            try:
                os.close(session.master_fd)
            except OSError:
                pass
            
            # Kill process tree
            kill_process_tree(session.pid)
            
            # Clean up working directory
            if os.path.exists(session.working_dir):
                shutil.rmtree(session.working_dir, ignore_errors=True)
            
            logger.info(f"Destroyed session {session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error destroying session {session_id}: {e}")
            return False
    
    async def get_session_info(self, session_id: str) -> Optional[Dict]:
        """Get information about a session."""
        async with self._lock:
            session = self.sessions.get(session_id)
            
        if not session:
            return None
            
        return {
            "session_id": session.session_id,
            "working_dir": session.working_dir,
            "created_at": session.created_at,
            "last_used": session.last_used,
            "command_count": session.command_count,
            "age_seconds": time.time() - session.created_at,
            "idle_seconds": time.time() - session.last_used,
        }
    
    async def list_sessions(self) -> List[Dict]:
        """List all active sessions."""
        async with self._lock:
            session_ids = list(self.sessions.keys())
        
        sessions = []
        for session_id in session_ids:
            info = await self.get_session_info(session_id)
            if info:
                sessions.append(info)
        return sessions


# Global session manager instance
_session_manager: Optional[BashSessionManager] = None


def get_session_manager() -> BashSessionManager:
    """Get the global session manager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = BashSessionManager()
    return _session_manager


async def init_session_manager(**kwargs):
    """Initialize and start the global session manager."""
    global _session_manager
    _session_manager = BashSessionManager(**kwargs)
    await _session_manager.start()
    return _session_manager


async def shutdown_session_manager():
    """Shutdown the global session manager."""
    global _session_manager
    if _session_manager:
        await _session_manager.stop()
        _session_manager = None
