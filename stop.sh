#!/bin/bash
# Stop SandboxFusion container

CONTAINER_NAME="sandbox-fusion-sessions"

echo "Stopping SandboxFusion..."

if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
    echo "Container stopped: $CONTAINER_NAME"
else
    echo "Container not running: $CONTAINER_NAME"
fi
