# CLAUDE.md — rwx-server

> Last updated: 2026-03-07. Server: 371 lines.

## What is this?

rwx-server is a generic MCP server that gives Claude.ai read/write/execute access to a Linux machine over HTTP. It is **not project-specific** — it works across any directory you whitelist. Named for the Unix permission bits: read, write, execute.

Live at: **https://rwx.fahrenheitrequited.dev/mcp**

---

## Architecture

```
Claude.ai (MCP client)
       ↓  HTTPS
Cloudflare Tunnel
       ↓  HTTP
rwx-server (FastMCP, port 8251)
  HostRewriteMiddleware  ← token auth + Host header fix
  6 MCP tools
  Audit log → ~/claude/rwx/audit.log
       ↓
  Linux filesystem / shell
```

## Tech Stack

- **Framework:** Python 3, FastMCP (`mcp[cli]`), uvicorn
- **Auth:** Bearer token or `?token=` query param — hardcoded `emc2ymmv`
- **Transport:** Streamable HTTP (`mcp.streamable_http_app()`)
- **Audit:** One JSON line per tool call → `~/claude/rwx/audit.log`
- **Tunnel:** Cloudflare (shared tunnel `5382c123-1ceb-4b5f-9200-94974a8f6ee9`)

---

## File Structure

```
~/claude/rwx/
├── CLAUDE.md          ← this file
├── rwx-server.py      ← full server (371 lines)
├── kick-off.sh        ← kill + restart script
├── audit.log          ← append-only tool call log (JSON lines)
└── logs/
    └── server.log     ← uvicorn stdout/stderr
```

---

## Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|--------|
| `DEV_PORT` | `8251` | Listen port |
| `DEV_ALLOWED_ROOTS` | `~` | Colon-separated allowed directories |
| `DEV_MAX_FILE_SIZE` | `10485760` (10MB) | Max file read/write size |
| `DEV_CMD_TIMEOUT` | `120` | Shell command timeout (seconds) |

All paths are validated against `DEV_ALLOWED_ROOTS` before any operation. Attempts to escape via `../` traversal are blocked.

---

## MCP Tools (6)

| Tool | Read-only | Description |
|------|-----------|-------------|
| `dev_read_file` | ✓ | Read file with line numbers; optional line range |
| `dev_write_file` | ✗ | Write/create file (creates parent dirs) |
| `dev_patch_file` | ✗ | Find unique string in file and replace it |
| `dev_run` | ✗ | Run shell command; returns stdout, stderr, exit code |
| `dev_list` | ✓ | List directory contents with sizes; optional glob pattern, depth 1–3 |
| `dev_search` | ✓ | Grep for regex pattern across files; optional file glob, max results |

### dev_read_file
```
params:
  path: str          # file path (absolute or ~-relative)
  line_start: int?   # 1-indexed start line
  line_end: int?     # inclusive end line (-1 = EOF)
```
Returns numbered lines with header showing total lines and file size. Errors if file > MAX_FILE_SIZE.

### dev_write_file
```
params:
  path: str      # destination path
  content: str   # full file content (max MAX_FILE_SIZE chars)
```
Creates parent directories automatically. Overwrites existing files.

### dev_patch_file
```
params:
  path: str      # file to patch
  old_str: str   # string to find (must appear exactly once)
  new_str: str   # replacement (empty string = delete)
```
Fails with a clear error if `old_str` appears 0 or 2+ times. Equivalent to `str_replace` over the network.

### dev_run
```
params:
  command: str     # shell command
  cwd: str?        # working directory (default: first allowed root)
  timeout: int?    # seconds (1–600, default: CMD_TIMEOUT)
```
Runs via `asyncio.create_subprocess_shell`. Output capped at 50,000 chars (25K head + 25K tail with truncation notice). Timeout kills the process.

### dev_list
```
params:
  path: str?     # directory (default: first allowed root)
  pattern: str?  # glob pattern e.g. '*.py', '**/*.html'
  depth: int?    # 1–3 (default: 1)
```
Hidden files (dot-files) are excluded. Shows 📁 for dirs (with child count) and 📄 for files (with size).

### dev_search
```
params:
  pattern: str       # grep regex
  path: str?         # file or directory (default: first allowed root)
  include: str?      # file glob filter e.g. '*.py'
  max_results: int?  # 1–200 (default: 50)
```
Wraps `grep -rn`. Times out after 30s.

---

## Security

### Path Validation
Every path is resolved (symlinks expanded) and checked against `DEV_ALLOWED_ROOTS` via `Path.relative_to()`. Any path outside is rejected before the filesystem is touched.

### Command Blocklist
The following patterns are blocked in `dev_run` regardless of context:
```
rm -rf /    rm -rf /*    mkfs    dd if=    > /dev/sd
shutdown    reboot       init 0  init 6
chmod -R 777 /    chown -R    :(){ :|:
```

### Token Auth
All requests require token `emc2ymmv` either as:
- `?token=emc2ymmv` query param
- `Authorization: Bearer emc2ymmv` header

401 returned otherwise. Token is hardcoded in `rwx-server.py` line 325.

### Audit Log
Every tool call appends one JSON line to `~/claude/rwx/audit.log`:
```json
{"ts": "2026-03-07T19:09:57Z", "tool": "run", "command": "ls -la", "cwd": null, "timeout": 120}
```
Log is append-only. Never rotated automatically — prune manually if it grows large.

---

## Running

```bash
# Start (or restart)
cd ~/claude/rwx && sh kick-off.sh

# Check running
ps aux | grep rwx-server | grep -v grep

# View logs
tail -f ~/claude/rwx/logs/server.log

# View audit trail
tail -f ~/claude/rwx/audit.log | python3 -m json.tool

# Manual start
cd ~/claude/rwx && nohup python3 rwx-server.py > logs/server.log 2>&1 &
```

**IMPORTANT:** Do NOT `pkill -f cloudflared` — it kills the shared tunnel for ALL services (OpenMind, Cortex, Autonomous, Therapy). Kill rwx-server specifically:
```bash
pkill -f 'python3.*rwx-server'
```

---

## Deployment

- **Server:** c-jfischer3 (Linux)
- **Port:** 8251
- **Public URL:** `https://rwx.fahrenheitrequited.dev`
- **MCP URL:** `https://rwx.fahrenheitrequited.dev/mcp?token=emc2ymmv`
- **Tunnel:** Shared Cloudflare tunnel `5382c123-1ceb-4b5f-9200-94974a8f6ee9`
- **Tunnel config:** `~/.cloudflared/config.yml` — ingress entry:
  ```yaml
  - hostname: rwx.fahrenheitrequited.dev
    service: http://localhost:8251
  ```
- **Python:** `~/miniconda3/bin/python3` or system python3 (whichever has `mcp[cli]` installed)

---

## Host Rewrite Middleware

Cloudflare forwards requests with `Host: rwx.fahrenheitrequited.dev` but uvicorn binds to `localhost:8251`. The `HostRewriteMiddleware` ASGI wrapper:
1. Checks bearer token / query param — returns 401 if missing/wrong
2. Rewrites `Host` header to `localhost:8251` before passing to FastMCP

This is required because FastMCP validates the Host header for streamable HTTP transport.

---

## Installing Dependencies

```bash
pip install 'mcp[cli]' uvicorn --break-system-packages
```

---

## Relationship to Other Services

rwx-server is the **general-purpose** filesystem/shell layer. Other services use it (or the same `dev` MCP tools in Claude.ai) to operate on the machine:

| Service | Purpose | Uses rwx? |
|---------|---------|----------|
| OpenMind | Knowledge graph | No — direct SSH via Claude.ai dev tool |
| Cortex | AI memory store | No — direct SSH via Claude.ai dev tool |
| rwx-server | **This** — generic file/shell access | Is the tool |
| voice-server | ElevenLabs voice UI | No |
| autonomous | Autonomous agent runner | No |

The `dev` tools visible in Claude.ai conversations (e.g. `dev:dev_run`) **are** this server — they connect via the MCP URL above.
