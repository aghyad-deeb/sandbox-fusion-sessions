#!/bin/bash
# Start SandboxFusion with Stateful Bash Sessions
#
# Usage:
#   ./start.sh                    # Start with defaults (port 60808)
#   ./start.sh 60809              # Start on custom port
#   ./start.sh 60808 32           # Port 60808, 32 concurrent commands
#   ./start.sh 60808 32 15000     # Port, concurrent commands, max sessions
#
# Environment variables:
#   MAX_CONCURRENT_COMMANDS  - Server-side concurrency limit (default: 32)
#   MAX_BASH_SESSIONS        - Max concurrent sessions (default: 15000)
#   BASH_SESSION_TIMEOUT     - Session timeout in seconds (default: 3600)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration
PORT="${1:-60808}"
MAX_CONCURRENT="${2:-${MAX_CONCURRENT_COMMANDS:-32}}"
MAX_SESSIONS="${3:-${MAX_BASH_SESSIONS:-15000}}"
SESSION_TIMEOUT="${BASH_SESSION_TIMEOUT:-3600}"
CONTAINER_NAME="sandbox-fusion-sessions"
IMAGE_NAME="sandbox-fusion:sessions"

echo "============================================================"
echo "     SandboxFusion with Stateful Bash Sessions"
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Port:                  $PORT"
echo "  Max concurrent cmds:   $MAX_CONCURRENT"
echo "  Max sessions:          $MAX_SESSIONS"
echo "  Session timeout:       ${SESSION_TIMEOUT}s"
echo ""

# =============================================================================
# STEP 1: Build image if needed
# =============================================================================

echo "Checking Docker image..."
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building Docker image: $IMAGE_NAME"
    docker build -t "$IMAGE_NAME" -f Dockerfile.sessions .
    echo "Image built successfully"
else
    echo "Image exists: $IMAGE_NAME"
fi
echo ""

# =============================================================================
# STEP 2: Stop existing container
# =============================================================================

if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping existing container..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    sleep 2
fi

# =============================================================================
# STEP 3: Start container
# =============================================================================

echo "Starting container..."
docker run -d --rm \
    --name "$CONTAINER_NAME" \
    --ulimit nofile=65535:65535 \
    -p "$PORT":8080 \
    -e MAX_CONCURRENT_COMMANDS="$MAX_CONCURRENT" \
    -e MAX_BASH_SESSIONS="$MAX_SESSIONS" \
    -e BASH_SESSION_TIMEOUT="$SESSION_TIMEOUT" \
    "$IMAGE_NAME" >/dev/null

# =============================================================================
# STEP 4: Wait for server to be ready
# =============================================================================

echo "Waiting for server to start..."
for i in {1..60}; do
    if curl -s "http://localhost:$PORT/v1/ping" 2>/dev/null | grep -q "pong"; then
        echo ""
        echo "============================================================"
        echo "          SERVER STARTED SUCCESSFULLY!"
        echo "============================================================"
        echo ""
        echo "  Server URL:      http://localhost:$PORT"
        echo "  Container:       $CONTAINER_NAME"
        echo ""
        echo "  API Endpoints:"
        echo "    POST /session/create   - Create a session"
        echo "    POST /session/run      - Run command in session"
        echo "    POST /session/destroy  - Destroy session"
        echo "    GET  /session/list     - List all sessions"
        echo ""
        echo "  To stop:  ./stop.sh"
        echo "  Logs:     docker logs -f $CONTAINER_NAME"
        echo ""
        echo "============================================================"
        exit 0
    fi
    if [ $((i % 10)) -eq 0 ]; then
        echo "  Still starting... (${i}s)"
    fi
    sleep 1
done

echo ""
echo "ERROR: Server did not start within 60 seconds"
echo "Check logs: docker logs $CONTAINER_NAME"
docker logs "$CONTAINER_NAME" 2>&1 | tail -30
exit 1
