#!/bin/bash
# Stop all SandboxFusion containers (PARALLEL)
#
# Usage:
#   ./stop.sh           # Stop all containers (default: 64)
#   ./stop.sh 4         # Stop 4 containers

NUM_CONTAINERS="${1:-${SANDBOX_NUM_CONTAINERS:-64}}"
CONTAINER_PREFIX="sandbox-fusion-sessions"

echo "Stopping SandboxFusion containers in parallel..."

# Stop all containers in parallel
PIDS=()
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    CONTAINER_NAME="${CONTAINER_PREFIX}-${i}"
    if docker ps -a -q -f name="^${CONTAINER_NAME}$" | grep -q .; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 &
        PIDS+=($!)
    fi
done

# Also stop any legacy single container
if docker ps -a -q -f name="^sandbox-fusion-sessions$" | grep -q .; then
    docker rm -f "sandbox-fusion-sessions" >/dev/null 2>&1 &
    PIDS+=($!)
fi

# Wait for all stop operations to complete
if [ ${#PIDS[@]} -gt 0 ]; then
    wait "${PIDS[@]}"
    echo "Stopped ${#PIDS[@]} container(s)"
else
    echo "  No containers were running"
fi
