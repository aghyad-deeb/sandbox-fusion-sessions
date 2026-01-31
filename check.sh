#!/bin/bash
# Comprehensive health check for SandboxFusion containers
# Tests run in PARALLEL for fast verification
#
# Usage:
#   ./check.sh                    # Check 64 containers (default)
#   ./check.sh 8                  # Check 8 containers
#   ./check.sh 8 60808            # Check 8 containers starting at port 60808
#
# Environment variables:
#   SANDBOX_NUM_CONTAINERS       - Number of containers (default: 64)
#   SANDBOX_BASE_PORT            - Starting port (default: 60808)

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
NUM_CONTAINERS="${1:-${SANDBOX_NUM_CONTAINERS:-64}}"
BASE_PORT="${2:-${SANDBOX_BASE_PORT:-60808}}"

# Create temp directory for results
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Counters (will be aggregated from parallel runs)
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
FAILED_CONTAINERS=()

echo "============================================================"
echo "     SandboxFusion Container Health Check (PARALLEL)"
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Number of containers:  $NUM_CONTAINERS"
echo "  Port range:            $BASE_PORT - $((BASE_PORT + NUM_CONTAINERS - 1))"
echo "  Execution mode:        Parallel"
echo ""

# Function to log test result (writes to temp file for parallel execution)
log_test() {
    local result_file=$1
    local status=$2
    local message=$3

    if [ "$status" = "PASS" ]; then
        echo "PASS|$message" >> "$result_file"
    else
        echo "FAIL|$message" >> "$result_file"
    fi
}

# Function to check a single container
check_container() {
    local port=$1
    local container_num=$2
    local result_file=$3
    local base_url="http://localhost:$port"

    local container_failed=0

    # Test 1: Ping endpoint
    if curl -sf "$base_url/v1/ping" 2>/dev/null | grep -q "pong"; then
        log_test "$result_file" "PASS" "Ping endpoint responds"
    else
        log_test "$result_file" "FAIL" "Ping endpoint not responding"
        container_failed=1
    fi

    # Test 2: Create session
    local session_response=$(curl -sf -X POST "$base_url/session/create" \
        -H "Content-Type: application/json" \
        -d '{}' 2>/dev/null)

    if echo "$session_response" | grep -q '"status":"Success"'; then
        local session_id=$(echo "$session_response" | grep -o '"session_id":"[^"]*"' | cut -d'"' -f4)
        log_test "$result_file" "PASS" "Session created: $session_id"
    else
        log_test "$result_file" "FAIL" "Failed to create session"
        echo "CONTAINER_FAILED" >> "$result_file"
        return 1
    fi

    # Test 3: Run simple command
    local cmd_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"echo hello\",\"timeout\":5}" 2>/dev/null)

    if echo "$cmd_response" | grep -q '"status":"Success"' && echo "$cmd_response" | grep -q '"stdout":"hello'; then
        log_test "$result_file" "PASS" "Simple command execution works"
    else
        log_test "$result_file" "FAIL" "Simple command execution failed"
        container_failed=1
    fi

    # Test 4: Test state persistence (cd command)
    local cd_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"cd /tmp && pwd\",\"timeout\":5}" 2>/dev/null)

    if echo "$cd_response" | grep -q '"/tmp'; then
        log_test "$result_file" "PASS" "State persistence (cd) works"
    else
        log_test "$result_file" "FAIL" "State persistence (cd) failed"
        container_failed=1
    fi

    # Test 5: Verify cd persisted in next command
    local pwd_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"pwd\",\"timeout\":5}" 2>/dev/null)

    if echo "$pwd_response" | grep -q '"/tmp'; then
        log_test "$result_file" "PASS" "Working directory persisted across commands"
    else
        log_test "$result_file" "FAIL" "Working directory did not persist"
        container_failed=1
    fi

    # Test 6: Test environment variable persistence
    local export_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"export TEST_VAR=hello123 && echo \$TEST_VAR\",\"timeout\":5}" 2>/dev/null)

    if echo "$export_response" | grep -q '"stdout":"hello123'; then
        log_test "$result_file" "PASS" "Environment variable export works"
    else
        log_test "$result_file" "FAIL" "Environment variable export failed"
        container_failed=1
    fi

    # Test 7: Verify env var persisted
    local env_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"echo \$TEST_VAR\",\"timeout\":5}" 2>/dev/null)

    if echo "$env_response" | grep -q '"stdout":"hello123'; then
        log_test "$result_file" "PASS" "Environment variable persisted across commands"
    else
        log_test "$result_file" "FAIL" "Environment variable did not persist"
        container_failed=1
    fi

    # Test 8: Test file operations
    local file_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"echo testdata > testfile.txt && cat testfile.txt\",\"timeout\":5}" 2>/dev/null)

    if echo "$file_response" | grep -q '"stdout":"testdata'; then
        log_test "$result_file" "PASS" "File operations work"
    else
        log_test "$result_file" "FAIL" "File operations failed"
        container_failed=1
    fi

    # Test 9: Test return codes
    local fail_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"false\",\"timeout\":5}" 2>/dev/null)

    if echo "$fail_response" | grep -q '"return_code":1' && echo "$fail_response" | grep -q '"status":"Failed"'; then
        log_test "$result_file" "PASS" "Return codes work correctly"
    else
        log_test "$result_file" "FAIL" "Return codes incorrect"
        container_failed=1
    fi

    # Test 10: Test timeout handling
    local timeout_response=$(curl -sf -X POST "$base_url/session/run" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"command\":\"sleep 10\",\"timeout\":2}" 2>/dev/null)

    if echo "$timeout_response" | grep -q "timed out"; then
        log_test "$result_file" "PASS" "Timeout handling works"
    else
        log_test "$result_file" "FAIL" "Timeout handling failed"
        container_failed=1
    fi

    # Test 11: List sessions
    local list_response=$(curl -sf -X GET "$base_url/session/list" 2>/dev/null)

    if echo "$list_response" | grep -q "$session_id"; then
        log_test "$result_file" "PASS" "Session listing works"
    else
        log_test "$result_file" "FAIL" "Session listing failed"
        container_failed=1
    fi

    # Test 12: Get session info
    local info_response=$(curl -sf -X GET "$base_url/session/info/$session_id" 2>/dev/null)

    if echo "$info_response" | grep -q "\"session_id\":\"$session_id\""; then
        log_test "$result_file" "PASS" "Session info endpoint works"
    else
        log_test "$result_file" "FAIL" "Session info endpoint failed"
        container_failed=1
    fi

    # Test 13: Destroy session
    local destroy_response=$(curl -sf -X POST "$base_url/session/destroy" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\"}" 2>/dev/null)

    if echo "$destroy_response" | grep -q '"status":"Success"'; then
        log_test "$result_file" "PASS" "Session destroyed successfully"
    else
        log_test "$result_file" "FAIL" "Session destruction failed"
        container_failed=1
    fi

    # Test 14: Verify session no longer exists
    # Note: Use -s instead of -sf because we expect a 500 error response
    local verify_response=$(curl -s -X GET "$base_url/session/info/$session_id" 2>/dev/null)

    if echo "$verify_response" | grep -qi "not found\|error"; then
        log_test "$result_file" "PASS" "Session cleanup verified"
    else
        log_test "$result_file" "FAIL" "Session still exists after destruction"
        container_failed=1
    fi

    if [ $container_failed -ne 0 ]; then
        echo "CONTAINER_FAILED" >> "$result_file"
    fi

    return $container_failed
}

# Main test loop - PARALLEL EXECUTION
echo "Starting parallel tests..."
echo ""
START_TIME=$(date +%s)

# Launch all container tests in parallel
PIDS=()
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    PORT=$((BASE_PORT + i))
    RESULT_FILE="$TEMP_DIR/container_${i}.result"

    # Run test in background
    (check_container "$PORT" "$i" "$RESULT_FILE" 2>/dev/null) &
    PIDS[$i]=$!
done

# Show progress while waiting
echo "Testing $NUM_CONTAINERS containers in parallel..."
COMPLETED=0
while [ $COMPLETED -lt $NUM_CONTAINERS ]; do
    sleep 1
    COMPLETED=0
    for pid in "${PIDS[@]}"; do
        if ! kill -0 $pid 2>/dev/null; then
            COMPLETED=$((COMPLETED + 1))
        fi
    done
    echo -ne "\rProgress: $COMPLETED/$NUM_CONTAINERS containers completed..."
done
echo ""
echo ""

# Wait for all background jobs to finish
wait

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

# Aggregate results from all temp files
echo "Aggregating results..."
for i in $(seq 0 $((NUM_CONTAINERS - 1))); do
    RESULT_FILE="$TEMP_DIR/container_${i}.result"
    PORT=$((BASE_PORT + i))

    if [ -f "$RESULT_FILE" ]; then
        # Count pass/fail for this container (use wc -l to avoid grep issues)
        container_passed=$(grep "^PASS|" "$RESULT_FILE" 2>/dev/null | wc -l)
        container_failed=$(grep "^FAIL|" "$RESULT_FILE" 2>/dev/null | wc -l)

        # Trim whitespace
        container_passed=$(echo $container_passed | tr -d ' \n')
        container_failed=$(echo $container_failed | tr -d ' \n')

        # Default to 0 if empty
        container_passed=${container_passed:-0}
        container_failed=${container_failed:-0}

        TOTAL_TESTS=$((TOTAL_TESTS + container_passed + container_failed))
        PASSED_TESTS=$((PASSED_TESTS + container_passed))
        FAILED_TESTS=$((FAILED_TESTS + container_failed))

        # Check if container failed overall
        if grep -q "CONTAINER_FAILED" "$RESULT_FILE" 2>/dev/null; then
            FAILED_CONTAINERS+=($i)
            echo -e "${RED}✗${NC} Container $i (port $PORT): FAILED - $container_failed/$((container_passed + container_failed)) tests failed"
        else
            echo -e "${GREEN}✓${NC} Container $i (port $PORT): PASSED - $container_passed/$((container_passed + container_failed)) tests passed"
        fi
    else
        echo -e "${RED}✗${NC} Container $i (port $PORT): NO RESULTS (test crashed or timed out)"
        FAILED_CONTAINERS+=($i)
    fi
done

# Summary
echo ""
echo "============================================================"
echo "                    TEST SUMMARY"
echo "============================================================"
echo ""
echo "  Containers tested:     $NUM_CONTAINERS"
echo "  Total tests run:       $TOTAL_TESTS"
echo -e "  Passed:                ${GREEN}$PASSED_TESTS${NC}"
echo -e "  Failed:                ${RED}$FAILED_TESTS${NC}"
echo "  Time elapsed:          ${ELAPSED}s"
if [ $ELAPSED -gt 0 ]; then
    echo "  Throughput:            $((NUM_CONTAINERS * 14 / ELAPSED)) tests/sec"
fi
echo ""

if [ ${#FAILED_CONTAINERS[@]} -eq 0 ]; then
    echo -e "${GREEN}============================================================${NC}"
    echo -e "${GREEN}        ALL CONTAINERS VERIFIED SUCCESSFULLY! ✓${NC}"
    echo -e "${GREEN}============================================================${NC}"
    exit 0
else
    echo -e "${RED}============================================================${NC}"
    echo -e "${RED}     ${#FAILED_CONTAINERS[@]} CONTAINERS FAILED${NC}"
    echo -e "${RED}============================================================${NC}"
    echo ""
    echo "Failed containers:"
    for container_num in "${FAILED_CONTAINERS[@]}"; do
        port=$((BASE_PORT + container_num))
        echo -e "  ${RED}✗${NC} Container $container_num: http://localhost:$port"

        # Show failed tests for this container
        result_file="$TEMP_DIR/container_${container_num}.result"
        if [ -f "$result_file" ]; then
            failed_tests=$(grep "^FAIL|" "$result_file" | cut -d'|' -f2)
            if [ -n "$failed_tests" ]; then
                echo "     Failed tests:"
                echo "$failed_tests" | while read test; do
                    echo "       - $test"
                done
            fi
        fi
        echo ""
    done
    echo "Check logs with:"
    echo "  docker logs sandbox-fusion-sessions-${FAILED_CONTAINERS[0]}"
    exit 1
fi
