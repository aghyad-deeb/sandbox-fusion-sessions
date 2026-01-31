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
API endpoints for session-based bash execution.

These endpoints provide stateful bash sessions for agent loops where
state (cwd, env vars, files) needs to persist across multiple commands
within an episode but be isolated between episodes.

Typical usage flow:
    1. Create session at episode start: POST /session/create
    2. Run commands during episode: POST /session/run  
    3. Destroy session at episode end: POST /session/destroy
"""

import traceback
from typing import Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sandbox.runners.bash_session_pipes import get_session_manager

session_router = APIRouter(prefix="/session", tags=["session"])
logger = structlog.stdlib.get_logger()


class CreateSessionRequest(BaseModel):
    """Request to create a new bash session."""
    session_id: Optional[str] = Field(
        default=None,
        description="Client-provided session ID for consistent hashing across multiple containers. If not provided, server generates one."
    )
    files: Dict[str, Optional[str]] = Field(
        default={},
        description="Dict of filename -> base64-encoded content to pre-populate in session"
    )
    startup_commands: List[str] = Field(
        default=[],
        description="Commands to run on session initialization (e.g., 'cd /app', 'export PATH=...')"
    )
    env: Dict[str, str] = Field(
        default={},
        description="Additional environment variables for the session"
    )


class CreateSessionResponse(BaseModel):
    """Response from creating a session."""
    status: str
    session_id: Optional[str] = None
    message: str = ""


class RunSessionCommandRequest(BaseModel):
    """Request to run a command in an existing session."""
    session_id: str = Field(..., description="Session identifier from create response")
    command: str = Field(..., description="Bash command to execute")
    timeout: float = Field(default=10, description="Command timeout in seconds")
    fetch_files: List[str] = Field(
        default=[],
        description="Files to retrieve after command execution (base64 encoded)"
    )


class RunSessionCommandResponse(BaseModel):
    """Response from running a command in a session."""
    status: str  # "Success", "Failed"
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    files: Dict[str, str] = Field(
        default={},
        description="Dict of filename -> base64-encoded content"
    )


class DestroySessionRequest(BaseModel):
    """Request to destroy a session."""
    session_id: str = Field(..., description="Session identifier to destroy")


class DestroySessionResponse(BaseModel):
    """Response from destroying a session."""
    status: str
    message: str = ""


class SessionInfoResponse(BaseModel):
    """Information about a session."""
    session_id: str
    working_dir: str
    created_at: float
    last_used: float
    command_count: int
    age_seconds: float
    idle_seconds: float


class ListSessionsResponse(BaseModel):
    """Response listing all sessions."""
    sessions: List[SessionInfoResponse]


@session_router.post("/create", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
    """
    Create a new stateful bash session.
    
    The session maintains:
    - Working directory state (cd commands persist)
    - Environment variables (export commands persist)
    - File system changes
    - Shell variables and history
    
    Sessions are isolated from each other via separate working directories
    and bash processes.
    
    If session_id is provided, it will be used (for consistent hashing across
    multiple containers). Otherwise, the server generates one.
    """
    try:
        manager = get_session_manager()
        session_id = await manager.create_session(
            session_id=request.session_id,
            files=request.files,
            startup_commands=request.startup_commands,
            env=request.env,
        )
        return CreateSessionResponse(
            status="Success",
            session_id=session_id,
        )
    except Exception as e:
        logger.error(f"Failed to create session: {e}\n{traceback.format_exc()}")
        return CreateSessionResponse(
            status="Failed",
            message=str(e),
        )


@session_router.post("/run", response_model=RunSessionCommandResponse)
async def run_session_command(request: RunSessionCommandRequest) -> RunSessionCommandResponse:
    """
    Run a command in an existing bash session.
    
    The command executes in the session's current working directory with
    its current environment. State changes (cd, export, file writes) persist
    for subsequent commands in the same session.
    """
    try:
        manager = get_session_manager()
        result = await manager.run_command(
            session_id=request.session_id,
            command=request.command,
            timeout=request.timeout,
            fetch_files=request.fetch_files,
        )
        return RunSessionCommandResponse(**result)
    except Exception as e:
        logger.error(f"Failed to run command: {e}\n{traceback.format_exc()}")
        return RunSessionCommandResponse(
            status="Failed",
            message=str(e),
            stderr=str(e),
        )


@session_router.post("/destroy", response_model=DestroySessionResponse)
async def destroy_session(request: DestroySessionRequest) -> DestroySessionResponse:
    """
    Destroy a session and clean up all resources.
    
    This should be called at the end of an episode to free resources.
    The session's working directory and all files are deleted.
    """
    try:
        manager = get_session_manager()
        success = await manager.destroy_session(request.session_id)
        if success:
            return DestroySessionResponse(status="Success")
        else:
            return DestroySessionResponse(
                status="Failed",
                message=f"Session {request.session_id} not found",
            )
    except Exception as e:
        logger.error(f"Failed to destroy session: {e}\n{traceback.format_exc()}")
        return DestroySessionResponse(
            status="Failed",
            message=str(e),
        )


@session_router.get("/info/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str):
    """Get information about a specific session."""
    manager = get_session_manager()
    info = await manager.get_session_info(session_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return SessionInfoResponse(**info)


@session_router.get("/list", response_model=ListSessionsResponse)
async def list_sessions():
    """List all active sessions."""
    manager = get_session_manager()
    sessions = await manager.list_sessions()
    return ListSessionsResponse(
        sessions=[SessionInfoResponse(**s) for s in sessions]
    )
