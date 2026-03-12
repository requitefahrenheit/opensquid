# OpenSquid

A local-first autonomous agent platform running on a single Linux machine behind a Cloudflare tunnel.

OpenSquid is a personal AI infrastructure stack: a dispatcher that accepts goals, spawns Claude agents with private persistent memory, and exposes everything via authenticated HTTP APIs. Agents can read and write their own memory across sessions, load skills, and operate autonomously without human intervention.

---

## Architecture

```
                        ┌─────────────────────────────────────┐
                        │         Cloudflare Tunnel           │
                        │  dispatcher.fahrenheitrequited.dev  │
                        └──────────────┬──────────────────────┘
                                       │
                        ┌──────────────▼──────────────────────┐
                        │     Dispatcher  (port 8255)         │
                        │  POST /run  GET /agents  GET /status │
                        └──────┬───────────────────┬──────────┘
                               │                   │
               ┌───────────────▼──┐   ┌────────────▼────────────────┐
               │  Raw API backend  │   │   Claude Code backend       │
               │  (no agent_id)    │   │   (agent_id set)            │
               └───────────────────┘   └──────────┬──────────────────┘
                                                  │
                                   ┌──────────────▼──────────────────┐
                                   │   AgentCortexManager            │
                                   │   Provisions per-agent SQLite   │
                                   │   on ports 8300-8399            │
                                   │   ~/cortex/agent-{id}.db        │
                                   └─────────────────────────────────┘
```

### Components

| Component | Path | Port | Description |
|---|---|---|---|
| **Dispatcher** | `dispatcher/` | 8255 | FastAPI server. Accepts goals, spawns agent sessions. |
| **Cortex** | `mcp-server/dual-server.py` | 8080 | Personal knowledge base. FastMCP + SQLite + semantic search. |
| **Cortex (autonomous)** | `cortex/dual-server.py` | 8082 | Separate Cortex for the heartbeat agent. |
| **Agent Cortex** | `dispatcher/agent_cortex.py` | 8300-8399 | Per-agent private Cortex instances, provisioned on demand. |
| **Heartbeat Agent** | `cortex/wrap_v2.sh` | — | Cron-driven autonomous Claude Opus agent. Runs every 10 min. |
| **OpenMind** | `_open-mind/` | 8250 | Hyperbolic force-directed PKM graph. |
| **Watchdog** | `watchdog.sh` | — | Cron every minute. Restarts any service that goes down. |
| **RWX** | `rwx/` | 8251 | Remote shell and file access over MCP. |

---

## Dispatcher

The core of OpenSquid. A FastAPI server that accepts goals and runs them as agent sessions.

### API

```
POST /run              Launch a session
GET  /status/{id}     Session status + output
GET  /sessions         List all sessions
GET  /stream/{id}      SSE live tail of session output
GET  /skills           List loaded skills
GET  /agents           List all agents with Cortex stats
GET  /agents/{id}      One agent's Cortex stats
DEL  /agents/{id}/memory  Wipe an agent's memory (requires ?confirm=true)
GET  /health           Health check
```

All endpoints require `?token=<TOKEN>`.

### Run request

```json
{
  "goal": "Research the latest developments in fusion energy and write a summary.",
  "agent_id": "research-agent",
  "skills": ["solver-launcher"],
  "arch": "claude",
  "max_turns": 10,
  "budget": 5.0
}
```

- `agent_id` — if set, provisions a private Cortex for this agent and upgrades to the Claude Code backend so the agent has native MCP tool access to its memory.
- `skills` — list of skill names from `skills/`. Each skill's `SKILL.md` is injected into the agent's system prompt.
- `arch` — `claude` (raw API, default) or `claude-code` (Claude CLI with MCP).

### Backends

**Raw API backend** (`arch=claude`, no `agent_id`)  
Direct Anthropic API loop. Fast, cheap, no tool use. Good for simple generation tasks.

**Claude Code backend** (`arch=claude-code` or any session with `agent_id`)  
Runs the `claude` CLI with `--mcp-config` pointing at the agent's private Cortex server. The agent gets native MCP tools: `cortex_store`, `cortex_search`, `cortex_semantic_search`, `cortex_list`, `cortex_get`, `cortex_stats`. Memory persists across sessions in `~/cortex/agent-{id}.db`.

---

## Agent Memory

Each named agent gets a private SQLite database that persists across sessions:

```
~/cortex/agent-research-agent.db
~/cortex/agent-overnight-builder.db
~/cortex/agent-market-scanner.db
```

The `AgentCortexManager` (`dispatcher/agent_cortex.py`) handles lifecycle:
- `provision(agent_id)` — finds a free port in 8300-8399, launches `dual-server.py` with the agent's db, waits for health check
- `release(agent_id)` — kills the process (db persists)
- `list_agents()` — scans `~/cortex/agent-*.db`, returns stats
- `get_stats(agent_id)` — entry count, db size, oldest/newest entry

Agents are instructed to search memory on start and store summaries on completion.

---

## Skills

Skills live in `skills/{name}/SKILL.md`. Each file has YAML frontmatter + instructions that are injected into the agent's system prompt when the skill is requested.

Current skills:
- `overnight-builder` — dumps goals, generates tasks, spawns build sessions
- `solver-launcher` — structured problem solving with decomposition
- `smooth-web-animation` — frontend animation patterns
- `twix-cohere-translate` — translation pipeline

---

## Cortex

The knowledge base powering both personal memory and agent memory.

- **Storage**: SQLite with full-text search
- **Embeddings**: `sentence-transformers` (all-MiniLM-L6-v2) for semantic search
- **Transport**: FastMCP (SSE + streamable HTTP)
- **REST API**: `/api/store`, `/api/search`, `/api/semantic`, `/api/list`, `/api/stats`, `/api/export-viz`
- **Auth**: token in query param

Two persistent instances:
- `dual-server.py` on port 8080 — Jeremy's personal Cortex (~9,000+ entries)
- `cortex/dual-server.py` on port 8082 — autonomous heartbeat agent's Cortex

Agent Cortex instances are ephemeral per-session (process), persistent on disk (db).

---

## Heartbeat Agent

A cron-driven autonomous agent that runs every 10 minutes.

- Script: `cortex/wrap_v2.sh`
- Model: `claude-opus-4-6`
- Has read access to Jeremy's Cortex, write access to its own autonomous Cortex
- Currently: generating "Visit One" screenplay, doing ongoing research
- Logs: `cortex/cron.log`

---

## Deployment

Runs on a single headless Linux machine (`c-jfischer3`, Irvine CA) behind a Cloudflare tunnel.

### Services and URLs

| Service | URL |
|---|---|
| Dispatcher | `https://dispatcher.fahrenheitrequited.dev` |
| Cortex | `https://cortex.fahrenheitrequited.dev` |
| OpenMind | `https://openmind.fahrenheitrequited.dev` |
| RWX | `https://rwx.fahrenheitrequited.dev` |

### Watchdog

`watchdog.sh` runs every minute via cron. Checks each service port with `netstat` + TCP connect, restarts via `start.sh` if down.

```bash
# Manual restart of dispatcher
bash ~/claude/dispatcher/start.sh >> ~/claude/dispatcher/dispatcher.log 2>&1 &
```

### Environment

All API keys in `env.sh` (not committed). Copy `env.sh.example` and fill in:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `CORTEX_TOKEN`

---

## Setup

```bash
# Clone
git clone https://github.com/requitefahrenheit/opensquid
cd opensquid

# Environment
cp env.sh.example env.sh
# edit env.sh with your keys

# Install dispatcher deps
pip install fastapi uvicorn anthropic

# Install Cortex deps
pip install fastmcp sentence-transformers sqlite-utils

# Start dispatcher
bash dispatcher/start.sh >> dispatcher/dispatcher.log 2>&1 &

# Start Cortex
CORTEX_DB=~/cortex/cortex.db CORTEX_PORT=8080 python3 mcp-server/dual-server.py &

# Enable watchdog
(crontab -l; echo '* * * * * bash ~/claude/watchdog.sh >> ~/claude/watchdog.log 2>&1') | crontab -
```

---

## What's not in this repo

- `cortex/*.db` — SQLite databases (personal data)
- `env.sh` — API keys
- `dispatcher/sessions/` — session outputs
- `open-photo/` — separate repo
- `watchdog.log`, `*.log` — runtime logs
