#!/bin/bash
# Stop all SandboxFusion containers
#
# Usage:
#   ./stop.sh           # Stop all containers (default: 8)
#   ./stop.sh 4         # Stop 4 containers

NUM_CONTAINERS="${1:-${SANDBOX_NUM_CONTAINERS:-8}}"
CONTAINER_PREFIX="sandbox-fusion-sessions"

echo "Stopping SandboxFusion containers..."

STOPPED=0
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    CONTAINER_NAME="${CONTAINER_PREFIX}-${i}"
    if docker ps -a -q -f name="^${CONTAINER_NAME}$" | grep -q .; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
        echo "  Stopped: $CONTAINER_NAME"
        STOPPED=$((STOPPED + 1))
    fi
done

# Also stop any legacy single container
if docker ps -a -q -f name="^sandbox-fusion-sessions$" | grep -q .; then
    docker rm -f "sandbox-fusion-sessions" >/dev/null 2>&1
    echo "  Stopped: sandbox-fusion-sessions (legacy)"
    STOPPED=$((STOPPED + 1))
fi

if [ "$STOPPED" -eq 0 ]; then
    echo "  No containers were running"
else
    echo ""
    echo "Stopped $STOPPED container(s)"
fi
