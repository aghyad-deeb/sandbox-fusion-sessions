#!/bin/bash
# Start SandboxFusion with Multiple Containers for High Concurrency
#
# Usage:
#   ./start.sh                    # Start with defaults (8 containers, ports 60808-60815)
#   ./start.sh 4                  # Start 4 containers
#   ./start.sh 8 60900            # 8 containers starting at port 60900
#
# Environment variables:
#   SANDBOX_NUM_CONTAINERS       - Number of containers (default: 8)
#   SANDBOX_BASE_PORT            - Starting port (default: 60808)
#   MAX_CONCURRENT_COMMANDS      - Server-side concurrency limit per container (default: 32)
#   MAX_BASH_SESSIONS            - Max concurrent sessions per container (default: 15000)
#   BASH_SESSION_TIMEOUT         - Session timeout in seconds (default: 3600)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration
NUM_CONTAINERS="${1:-${SANDBOX_NUM_CONTAINERS:-8}}"
BASE_PORT="${2:-${SANDBOX_BASE_PORT:-60808}}"
MAX_CONCURRENT="${MAX_CONCURRENT_COMMANDS:-32}"
MAX_SESSIONS="${MAX_BASH_SESSIONS:-15000}"
SESSION_TIMEOUT="${BASH_SESSION_TIMEOUT:-3600}"
CONTAINER_PREFIX="sandbox-fusion-sessions"
IMAGE_NAME="sandbox-fusion:sessions"

echo "============================================================"
echo "     SandboxFusion Multi-Container Deployment"
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Number of containers:  $NUM_CONTAINERS"
echo "  Port range:            $BASE_PORT - $((BASE_PORT + NUM_CONTAINERS - 1))"
echo "  Max concurrent cmds:   $MAX_CONCURRENT (per container)"
echo "  Max sessions:          $MAX_SESSIONS (per container)"
echo "  Total max sessions:    $((MAX_SESSIONS * NUM_CONTAINERS))"
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
# STEP 2: Stop existing containers (PARALLEL)
# =============================================================================

echo "Stopping any existing containers in parallel..."
STOP_PIDS=()
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    CONTAINER_NAME="${CONTAINER_PREFIX}-${i}"
    if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 &
        STOP_PIDS+=($!)
    fi
done

# Wait for all stop operations
if [ ${#STOP_PIDS[@]} -gt 0 ]; then
    wait "${STOP_PIDS[@]}"
    echo "Stopped ${#STOP_PIDS[@]} existing container(s)"
fi
sleep 1
echo ""

# =============================================================================
# STEP 3: Start all containers (PARALLEL)
# =============================================================================

echo "Starting $NUM_CONTAINERS containers in parallel..."
START_PIDS=()
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    PORT=$((BASE_PORT + i))
    CONTAINER_NAME="${CONTAINER_PREFIX}-${i}"

    docker run -d --rm \
        --name "$CONTAINER_NAME" \
        --ulimit nofile=65535:65535 \
        -p "$PORT":8080 \
        -e MAX_CONCURRENT_COMMANDS="$MAX_CONCURRENT" \
        -e MAX_BASH_SESSIONS="$MAX_SESSIONS" \
        -e BASH_SESSION_TIMEOUT="$SESSION_TIMEOUT" \
        "$IMAGE_NAME" >/dev/null 2>&1 &
    START_PIDS+=($!)
done

# Wait for all docker run commands to complete
wait "${START_PIDS[@]}"
echo "Launched $NUM_CONTAINERS containers"

# Build endpoints list
ENDPOINTS=""
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    PORT=$((BASE_PORT + i))
    if [ -z "$ENDPOINTS" ]; then
        ENDPOINTS="http://localhost:$PORT"
    else
        ENDPOINTS="$ENDPOINTS,http://localhost:$PORT"
    fi
done
echo ""

# =============================================================================
# STEP 4: Wait for all servers to be ready
# =============================================================================

echo "Waiting for all servers to start..."
ALL_READY=false
for attempt in {1..60}; do
    READY_COUNT=0
    for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
        PORT=$((BASE_PORT + i))
        if curl -s "http://localhost:$PORT/v1/ping" 2>/dev/null | grep -q "pong"; then
            READY_COUNT=$((READY_COUNT + 1))
        fi
    done
    
    if [ "$READY_COUNT" -eq "$NUM_CONTAINERS" ]; then
        ALL_READY=true
        break
    fi
    
    if [ $((attempt % 10)) -eq 0 ]; then
        echo "  $READY_COUNT/$NUM_CONTAINERS containers ready... (${attempt}s)"
    fi
    sleep 1
done

if [ "$ALL_READY" = true ]; then
    echo ""
    echo "============================================================"
    echo "          ALL CONTAINERS STARTED SUCCESSFULLY!"
    echo "============================================================"
    echo ""
    echo "  Containers:      $NUM_CONTAINERS"
    echo "  Port range:      $BASE_PORT - $((BASE_PORT + NUM_CONTAINERS - 1))"
    echo "  Total capacity:  $((MAX_SESSIONS * NUM_CONTAINERS)) concurrent sessions"
    echo ""
    echo "  Set this environment variable for the agent loop:"
    echo ""
    echo "    export SANDBOX_FUSION_ENDPOINTS=\"$ENDPOINTS\""
    echo ""
    echo "  API Endpoints (on each container):"
    echo "    POST /session/create   - Create a session"
    echo "    POST /session/run      - Run command in session"
    echo "    POST /session/destroy  - Destroy session"
    echo "    GET  /session/list     - List all sessions"
    echo ""
    echo "  To stop:  ./stop.sh"
    echo "  Logs:     docker logs -f ${CONTAINER_PREFIX}-0"
    echo ""
    echo "============================================================"
    exit 0
else
    echo ""
    echo "ERROR: Not all containers started within 60 seconds"
    echo "Check logs: docker logs ${CONTAINER_PREFIX}-0"
    exit 1
fi
