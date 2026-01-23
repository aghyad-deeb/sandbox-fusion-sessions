# SandboxFusion with Stateful Bash Sessions

This is a modified version of [SandboxFusion](https://github.com/bytedance/SandboxFusion) that adds support for stateful bash sessions.

## What's Added

- **Session-based bash execution**: Create persistent bash sessions where `cd`, `export`, and file changes persist across commands
- **High scalability**: Uses subprocess pipes (not PTY) for 10,000+ concurrent sessions
- **Server-side concurrency control**: Configurable `MAX_CONCURRENT_COMMANDS` to prevent system overload
- **Episode isolation**: Each session is fully isolated, cleaned up after episode ends

## Quick Start

### Build the Docker Image

```bash
docker build -t sandbox-fusion:sessions -f Dockerfile.sessions .
```

### Run the Server

```bash
# Basic usage
docker run -d --rm \
    --name sandbox-fusion-sessions \
    -p 60809:8080 \
    sandbox-fusion:sessions

# With configuration
docker run -d --rm \
    --name sandbox-fusion-sessions \
    --ulimit nofile=65535:65535 \
    -p 60809:8080 \
    -e MAX_CONCURRENT_COMMANDS=32 \
    -e MAX_BASH_SESSIONS=15000 \
    -e BASH_SESSION_TIMEOUT=3600 \
    sandbox-fusion:sessions
```

### Test the Server

```bash
# Check server is running
curl http://localhost:60809/v1/ping

# Create a session
curl -X POST http://localhost:60809/session/create \
    -H "Content-Type: application/json" \
    -d '{"files": {}, "startup_commands": [], "env": {}}'

# Run a command (replace SESSION_ID)
curl -X POST http://localhost:60809/session/run \
    -H "Content-Type: application/json" \
    -d '{"session_id": "SESSION_ID", "command": "echo hello", "timeout": 10}'

# Destroy the session
curl -X POST http://localhost:60809/session/destroy \
    -H "Content-Type: application/json" \
    -d '{"session_id": "SESSION_ID"}'
```

## API Endpoints

### Session Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/session/create` | POST | Create a new bash session |
| `/session/run` | POST | Run a command in an existing session |
| `/session/destroy` | POST | Destroy a session and clean up resources |
| `/session/list` | GET | List all active sessions |
| `/session/info/{id}` | GET | Get info about a specific session |

### Create Session Request

```json
{
    "files": {"filename": "base64_content"},
    "startup_commands": ["cd /app", "export FOO=bar"],
    "env": {"VAR": "value"}
}
```

### Run Command Request

```json
{
    "session_id": "abc123",
    "command": "ls -la",
    "timeout": 10,
    "fetch_files": ["output.txt"]
}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_COMMANDS` | 32 | Max commands executing simultaneously |
| `MAX_BASH_SESSIONS` | 10000 | Max concurrent sessions |
| `BASH_SESSION_TIMEOUT` | 3600 | Session timeout in seconds |

## Performance

With `MAX_CONCURRENT_COMMANDS=32`:
- Tested with 10,000 concurrent sessions
- ~38 commands/sec throughput under heavy load
- State verification: 26%+ success rate even under extreme load

For higher throughput, consider horizontal scaling with multiple containers.

## Statefulness

Sessions maintain:
- Working directory (`cd` commands persist)
- Environment variables (`export` commands persist)
- File system changes
- Return codes from previous commands

Sessions do NOT maintain (by design, for scalability):
- Shell functions/aliases (not exported)
- Shell variables (only exported env vars)
- Interactive command state

## License

Apache License 2.0 - See the original [SandboxFusion](https://github.com/bytedance/SandboxFusion) for full license.

## Credits

Based on [SandboxFusion](https://github.com/bytedance/SandboxFusion) by ByteDance.
