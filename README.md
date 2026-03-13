# OpenSquid

Personal AI infrastructure — Parity daemon architecture merged with OpenSquid dispatcher.

Runs on **c-jfischer3** (Irvine, CA). All services bind `127.0.0.1`; external access via Cloudflare tunnel `5382c123`.

---

## Architecture

```
opensquid/
  daemon/
    daemon-server.py    # Primary service — agentic loop + HTTP API + MCP tools
    daemon.db           # Live SQLite task database
    SOUL.md             # System prompt / identity
    HEARTBEAT.md        # Directives read every 30 min by APScheduler
    kick-off.sh         # Start daemon
    setup.sh            # Create daemon venv
  channels/
    channels-server.py  # Telegram bot gateway
    kick-off.sh / setup.sh
  browser/
    browser-server.py   # httpx + BeautifulSoup4 web fetcher (MCP)
    kick-off.sh / setup.sh
  voice-wake/           # pvporcupine wake word daemon (stub)
  skills/               # Skill libraries (SKILL.md files)
  agent_cortex.py       # Per-task isolated Cortex manager (reference copy)
  kick-off.sh           # Kill old processes → start daemon + browser
  setup.sh              # Root setup (channels + browser deps)
  .env.example          # Env var template for channels-server
```

---

## Port Map

| Service       | Port  | File                  | Notes                         |
|---------------|-------|-----------------------|-------------------------------|
| daemon        | 8256  | daemon/daemon-server.py   | Primary service, always on    |
| channels      | 8257  | channels/channels-server.py | Requires TELEGRAM_BOT_TOKEN |
| browser       | 8258  | browser/browser-server.py | httpx+bs4, no Playwright    |
| agent-cortex  | 8300–8399 | (dynamic)             | Per-task isolated Cortex DBs  |

Upstream dependencies (not managed here):
- Cortex autonomous: port 8082 / `https://autonomous.fahrenheitrequited.dev`
- OpenMind: port 8250 / `https://openmind.fahrenheitrequited.dev`
- rwx-server: port 8251 / `https://rwx.fahrenheitrequited.dev`

---

## daemon/daemon-server.py

The primary service. Runs on port **8256**.

### HTTP API (auth: `Authorization: Bearer emc2ymmv`)

| Method | Path | Description |
|--------|------|-------------|
| POST   | /tasks | Create a task |
| GET    | /tasks | List recent tasks |
| GET    | /tasks/{id} | Task details + steps |
| DELETE | /tasks/{id} | Cancel a task |
| GET    | /stream/{task_id} | SSE live stream of task_steps |
| POST   | /run | Alias: create a task (dispatcher compat) |
| GET    | /sessions | Alias: list tasks |
| GET    | /status/{id} | Alias: task details |
| POST   | /webhook/{id} | Trigger a webhook |

### MCP tools (at `/mcp`)

| Tool | Description |
|------|-------------|
| `daemon_task_create` | Create a task, fire the agentic loop |
| `daemon_task_status` | Get task status + all steps |
| `daemon_task_list`   | List recent tasks |
| `daemon_task_cancel` | Cancel a task |
| `daemon_heartbeat`   | View heartbeat log + HEARTBEAT.md |
| `daemon_schedule_add` | Add a cron-scheduled prompt |
| `daemon_schedule_list` | List all schedules |
| `daemon_webhook_create` | Create a webhook endpoint |

### POST /tasks body

```json
{
  "title": "short label",
  "prompt": "the task prompt",
  "backend": "api",
  "skill": "overnight-builder",
  "isolated_cortex": false
}
```

- `backend`: `"api"` (raw Anthropic SDK loop) or `"claude-code"` (claude CLI)
- `skill`: skill name to inject into system prompt (`isolated_memory` also accepted)
- `isolated_cortex`: provision a private Cortex DB on port 8300–8399

---

## Agentic Loop

The daemon runs two backends:

**Raw API** (`backend: "api"`, default):
- Calls Claude Opus via Anthropic SDK
- Loop: call → parse `tool_use` → execute MCP tools → append results → repeat
- Continues until `stop_reason == "end_turn"` or 20 steps
- Writes each step to `task_steps` in real time
- SOUL.md injected as system prompt prefix
- Selected skills injected as XML block

**Claude Code** (`backend: "claude-code"`):
- Invokes `~/.npm-global/bin/claude` CLI with `--dangerously-skip-permissions`
- Full tool access via claude's built-in tools
- If `isolated_cortex=True`, injects per-task Cortex MCP config (type: `"http"`)

---

## Skills

Skills live in `skills/*/SKILL.md` with YAML frontmatter (`name`, `description`) and instruction body.

Skills are injected into the system prompt when specified via `skill` in the task body. Available skills:

- **overnight-builder** — autonomous overnight build agent
- **smooth-web-animation** — WebGPU/WGSL animation patterns
- **solver-launcher** — task decomposition and sub-agent launch
- **twix-cohere-translate** — MT quality estimation and translation
- **metricx** — MetricX scoring integration

---

## Heartbeat

APScheduler runs `heartbeat_check()` every 30 minutes:
- Reads `HEARTBEAT.md`
- If non-comment content found → creates a task from those directives
- Otherwise → logs `HEARTBEAT_OK` to `heartbeat.log`

To queue a directive: edit `HEARTBEAT.md` and add text below the header.

---

## Per-task Isolated Cortex

When `isolated_memory: true`:
1. `AgentCortexManager` finds a free port in 8300–8399
2. Launches a private `dual-server.py` instance with `agent-{agent_id}.db`
3. Injects Cortex URL into the agentic loop (API mode) or MCP config (claude-code mode)
4. Process is terminated after task completes; db file persists for future sessions

Agent DBs live at `~/cortex/agent-{agent_id}.db`.

---

## Quick Start

```bash
cd ~/claude/opensquid
bash daemon/setup.sh     # create daemon venv, install deps (first run only)
bash kick-off.sh         # kill old processes, start daemon + browser
```

For channels (Telegram):
```bash
cp .env.example .env
# Edit .env: add TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_IDS
bash channels/setup.sh
bash channels/kick-off.sh
```

---

## Auth

All endpoints require: `Authorization: Bearer emc2ymmv`

Never use `?token=` query params.

---

## Database

SQLite WAL mode at `~/claude/opensquid/daemon/daemon.db`.

Schema: `tasks`, `task_steps`, `schedules`, `webhooks`

Do not delete `daemon.db` — it contains live task history.
