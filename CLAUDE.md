# CLAUDE.md — OpenSquid

Authoritative reference for working in this codebase.

---

## Project Identity

**OpenSquid** is a unified personal AI OS — a merger of OpenSquid (multi-agent dispatcher) and Parity (personal AI daemon). It runs as a persistent background service that accepts tasks, executes them using Claude AI, and manages state in SQLite. It is J's personal AI infrastructure.

Identity document: `SOUL.md`. Active directives: `HEARTBEAT.md`.

---

## Repository Layout

```
~/claude/opensquid/
  daemon/           # Core agentic loop, task queue, heartbeat (port 8256)
  channels/         # Telegram + future messaging integrations (port 8257)
  browser/          # httpx + bs4 web browsing (port 8258)
  voice-wake/       # pvporcupine wake word detection (local only)
  skills/           # Skill markdown files injected per task
  SOUL.md           # Identity document
  HEARTBEAT.md      # Directive file
  CLAUDE.md         # This file
  watchdog.sh       # Service supervisor
```

---

## Port Assignments

| Service           | Port        |
|-------------------|-------------|
| daemon            | 8256        |
| channels          | 8257        |
| browser           | 8258        |
| voice-wake        | local only  |
| agent Cortex pool | 8300–8399   |

---

## Auth Convention

ALL endpoints use the `Authorization: Bearer emc2ymmv` header.

**Never** use `?token=` query parameters. Header-only.

```python
headers = {"Authorization": "Bearer emc2ymmv"}
```

---

## Database Conventions

- SQLite WAL mode on every connection:
  ```python
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA synchronous=NORMAL")
  ```
- Each service manages its own `.db` file — no shared databases across services.
- `daemon` uses `daemon/daemon.db` with tables:
  - `tasks` — `id, title, prompt, status, created_at, updated_at, result`
  - `task_steps` — `id, task_id, step_num, action, result, ts`

---

## Daemon API (port 8256)

Base URL: `http://localhost:8256`

All requests require `Authorization: Bearer emc2ymmv`.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Create and enqueue a task |
| `GET` | `/tasks` | List all tasks |
| `GET` | `/tasks/{id}` | Get task with its steps |
| `GET` | `/stream/{task_id}` | SSE stream of task_steps as written |
| `DELETE` | `/tasks/{id}` | Cancel a task |

### POST /tasks — Parameters

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `title` | string | required | Short label for the task |
| `prompt` | string | required | Full task instructions for the agent |
| `backend` | string | `'api'` | Execution backend: `'api'` or `'claude-code'` |
| `skill` | string | optional | Skill name to inject into system prompt |
| `isolated_cortex` | bool | `false` | Provision an isolated agent DB on 8300–8399 |

---

## Task Flags

### `backend`

- **`'api'`** (default) — raw Anthropic API agentic loop. Standard path.
- **`'claude-code'`** — invokes Claude Code CLI as a subprocess. See Claude Code Backend Notes below.

### `skill`

Optional skill name. The daemon reads the corresponding markdown file and prepends it to the system prompt before executing the task.

Resolution order:
1. `skills/{skill}.md`
2. `skills/{skill}/SKILL.md`

### `isolated_cortex`

When `true`:
1. Provisions an isolated `agent-{task_id}.db` on a free port in the 8300–8399 range.
2. Adds it as an MCP tool for the duration of the task.
3. Releases the port and cleans up after task completion.

---

## Skills System

Skills are markdown files under `~/claude/opensquid/skills/`. They contain specialized instructions that are injected at the top of the system prompt for a given task.

Pass a skill name when creating a task:
```json
{ "title": "Overnight build", "prompt": "...", "skill": "overnight-builder" }
```

The daemon reads `skills/overnight-builder/SKILL.md` and prepends it to the system prompt.

To create a new skill, add a markdown file at `skills/<skill-name>/SKILL.md` (or `skills/<skill-name>.md` for single-file skills).

---

## Service Startup

Each service has a `kick-off.sh` that follows this pattern:

1. Kill any existing process on the service's port.
2. Activate the local venv: `source venv/bin/activate`
3. Start the server with `nohup ... &`

Example for daemon:
```bash
fuser -k 8256/tcp 2>/dev/null || true
cd ~/claude/opensquid/daemon
source venv/bin/activate
nohup python3 daemon-server.py >> daemon.log 2>&1 &
```

---

## Python Venvs

Each service has its own venv at `~/claude/opensquid/<service>/venv/`.

To set up a service:
```bash
cd ~/claude/opensquid/<service>
bash setup.sh
```

`setup.sh` creates the venv and installs dependencies. Never share venvs across services.

---

## Reference Implementations

When writing new code, follow patterns from:

| Pattern | Reference file |
|---------|---------------|
| Auth (Bearer token middleware) | `~/claude/rwx/rwx-server.py` |
| SQLite + MCP server | `~/claude/mcp-server/dual-server.py` |

---

## Watchdog

`~/claude/watchdog.sh` supervises all services and restarts them if they go down.

OpenSquid daemon entry:
```bash
check_and_restart "opensquid-daemon" 8256 \
  "/home/jfischer/claude/opensquid/daemon/kick-off.sh >> /home/jfischer/claude/opensquid/daemon/daemon.log 2>&1"
```

The watchdog checks each service's port and calls `kick-off.sh` if the port is not responding.

---

## Claude Code Backend Notes

When `backend='claude-code'`, the daemon invokes the Claude Code CLI as a subprocess.

Key requirements:

- **CLI path**: `/home/jfischer/.npm-global/bin/claude`
- **MCP config type**: must be `"http"`, not `"sse"`
- **Nested session isolation**: pop `CLAUDECODE` from the environment before invoking:
  ```python
  env = os.environ.copy()
  env.pop("CLAUDECODE", None)
  ```
- **Node in PATH**: ensure `/usr/local/bin` is present in PATH so node is found:
  ```python
  env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")
  ```
