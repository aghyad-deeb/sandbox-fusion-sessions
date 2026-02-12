"""
API endpoints for OverlayFS-isolated session-based bash execution.

Same API surface as session_api.py but each session gets its own
OverlayFS filesystem view for full isolation between sessions.

Typical usage flow:
    1. Create session: POST /overlay-session/create
    2. Run commands:   POST /overlay-session/run
    3. Destroy:        POST /overlay-session/destroy
"""

import traceback
from typing import Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sandbox.runners.bash_session_overlay import get_overlay_session_manager

overlay_session_router = APIRouter(prefix="/overlay-session", tags=["overlay-session"])
logger = structlog.stdlib.get_logger()


# Reuse the same request/response models as session_api.py
class CreateSessionRequest(BaseModel):
    session_id: Optional[str] = Field(
        default=None,
        description="Client-provided session ID for consistent hashing."
    )
    files: Dict[str, Optional[str]] = Field(
        default={},
        description="Dict of filename -> base64-encoded content to pre-populate in session"
    )
    extra_files: Dict[str, Optional[str]] = Field(
        default={},
        description="Dict of absolute_path -> base64-encoded content placed at absolute paths inside the session's isolated overlay (invisible to other sessions)"
    )
    startup_commands: List[str] = Field(
        default=[],
        description="Commands to run on session initialization"
    )
    env: Dict[str, str] = Field(
        default={},
        description="Additional environment variables for the session"
    )


class CreateSessionResponse(BaseModel):
    status: str
    session_id: Optional[str] = None
    message: str = ""


class RunSessionCommandRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    command: str = Field(..., description="Bash command to execute")
    timeout: float = Field(default=10, description="Command timeout in seconds")
    fetch_files: List[str] = Field(
        default=[],
        description="Files to retrieve after command execution (base64 encoded)"
    )


class RunSessionCommandResponse(BaseModel):
    status: str
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    files: Dict[str, str] = Field(default={})


class DestroySessionRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier to destroy")


class DestroySessionResponse(BaseModel):
    status: str
    message: str = ""


class SessionInfoResponse(BaseModel):
    session_id: str
    working_dir: str
    created_at: float
    last_used: float
    command_count: int
    age_seconds: float
    idle_seconds: float


class ListSessionsResponse(BaseModel):
    sessions: List[SessionInfoResponse]


@overlay_session_router.post("/create", response_model=CreateSessionResponse)
async def create_overlay_session(request: CreateSessionRequest) -> CreateSessionResponse:
    """Create a new OverlayFS-isolated bash session."""
    try:
        manager = get_overlay_session_manager()
        session_id = await manager.create_session(
            session_id=request.session_id,
            files=request.files,
            extra_files=request.extra_files,
            startup_commands=request.startup_commands,
            env=request.env,
        )
        return CreateSessionResponse(status="Success", session_id=session_id)
    except Exception as e:
        logger.error(f"Failed to create overlay session: {e}\n{traceback.format_exc()}")
        return CreateSessionResponse(status="Failed", message=str(e))


@overlay_session_router.post("/run", response_model=RunSessionCommandResponse)
async def run_overlay_session_command(request: RunSessionCommandRequest) -> RunSessionCommandResponse:
    """Run a command in an existing overlay session."""
    try:
        manager = get_overlay_session_manager()
        result = await manager.run_command(
            session_id=request.session_id,
            command=request.command,
            timeout=request.timeout,
            fetch_files=request.fetch_files,
        )
        return RunSessionCommandResponse(**result)
    except Exception as e:
        logger.error(f"Failed to run command: {e}\n{traceback.format_exc()}")
        return RunSessionCommandResponse(status="Failed", message=str(e), stderr=str(e))


@overlay_session_router.post("/destroy", response_model=DestroySessionResponse)
async def destroy_overlay_session(request: DestroySessionRequest) -> DestroySessionResponse:
    """Destroy an overlay session and clean up all mounts."""
    try:
        manager = get_overlay_session_manager()
        success = await manager.destroy_session(request.session_id)
        if success:
            return DestroySessionResponse(status="Success")
        else:
            return DestroySessionResponse(status="Failed", message=f"Session {request.session_id} not found")
    except Exception as e:
        logger.error(f"Failed to destroy overlay session: {e}\n{traceback.format_exc()}")
        return DestroySessionResponse(status="Failed", message=str(e))


@overlay_session_router.get("/info/{session_id}", response_model=SessionInfoResponse)
async def get_overlay_session_info(session_id: str):
    manager = get_overlay_session_manager()
    info = await manager.get_session_info(session_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return SessionInfoResponse(**info)


@overlay_session_router.get("/list", response_model=ListSessionsResponse)
async def list_overlay_sessions():
    manager = get_overlay_session_manager()
    sessions = await manager.list_sessions()
    return ListSessionsResponse(sessions=[SessionInfoResponse(**s) for s in sessions])
