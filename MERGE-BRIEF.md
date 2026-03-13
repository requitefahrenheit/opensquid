# OpenSquid Merge Brief
## Parity + OpenSquid → ~/claude/opensquid/

You are merging two codebases into a single unified system.
Commit everything to the OpenSquid GitHub repo: https://github.com/requitefahrenheit/opensquid
PAT: [REDACTED]

---

## Source Codebases

### OpenSquid (~/claude/dispatcher/)
A multi-agent dispatcher. Key files:
- dispatcher-server.py — FastAPI server, port 8255
- dispatcher.py — session logic, multi-backend routing
- agent_cortex.py — AgentCortexManager (per-agent isolated Cortex instances on ports 8300-8399)

Key strengths to KEEP:
- Per-agent isolated Cortex instances (agent_cortex.py) — provision agent-{id}.db per task
- Multi-backend routing: arch='claude-code' → Claude Code backend; else raw API
- SSE streaming endpoint GET /stream/{id} — live tail of running session
- Skills library pattern (~/claude/skills/) — markdown files injected into agent context
- Claude Code invocation (run_session_claude_code in dispatcher.py) — already debugged

Claude Code quirks (preserve these exactly):
- CLI at /home/jfischer/.npm-global/bin/claude (v2.1.63, no --max-turns)
- MCP config type must be "http" not "sse"
- Must env.pop("CLAUDECODE", None) for nested sessions
- Must ensure /usr/local/bin in PATH for node

### Parity (~/claude/parity/)
A personal AI operating system. Key files:
- parity/daemon/daemon-server.py — agentic loop server, port 8256
- parity/channels/channels-server.py — Telegram webhook, port 8257
- parity/browser/browser-server.py — httpx+bs4 browser MCP, port 8258
- parity/voice-wake/ — pvporcupine wake word daemon
- parity/CLAUDE.md — canonical conventions (READ THIS)

Key strengths to KEEP:
- Persistent SQLite task queue: tasks, task_steps, schedules, webhooks tables
- Agentic loop: call → tool_use → execute → repeat (max 20 steps)
- APScheduler heartbeat (30 min) reading HEARTBEAT.md for directives
- SOUL.md as system prompt identity layer
- Channels: Telegram routing (slash commands → Cortex/OpenMind, agentic keywords → daemon)
- Browser MCP server (httpx+bs4, NO Playwright — GLIBC too old)
- Voice wake daemon (jarvis wake word → OpenMind/daemon)

---

## Target Architecture: ~/claude/opensquid/

The daemon REPLACES the dispatcher as the core. Everything else integrates around it.

```
opensquid/
  daemon/
    daemon-server.py     # Core — Parity daemon + OpenSquid multi-backend routing
    daemon.db            # SQLite task queue
    SOUL.md              # Identity/system prompt
    HEARTBEAT.md         # Directive file for autonomous heartbeat
    kick-off.sh
    setup.sh
  channels/
    channels-server.py   # Telegram (from Parity, unchanged)
    kick-off.sh
    setup.sh
    .env.example
  browser/
    browser-server.py    # httpx+bs4 (from Parity, unchanged)
    kick-off.sh
    setup.sh
  voice-wake/
    voice-wake.py        # pvporcupine (from Parity, unchanged)
    start.sh
    setup.sh
  skills/                # Skill markdown files (from OpenSquid)
    overnight-builder.md
    solver-launcher.md
    smooth-web-animation.md
    twix-cohere-translate.md
  agent_cortex.py        # Per-agent Cortex instances (from OpenSquid)
  CLAUDE.md              # Updated canonical conventions
  watchdog.sh            # Service supervisor
  kick-off-all.sh        # Start everything
  README.md
```

---

## Key Integration Points

### 1. Daemon gets multi-backend routing
In daemon-server.py agentic loop, add backend field to tasks table:
```sql
ALTER TABLE tasks ADD COLUMN backend TEXT DEFAULT 'api';
-- Values: 'api' (default) or 'claude-code'
```
When backend='claude-code', use OpenSquid's run_session_claude_code() logic.
When backend='api', use existing Parity agentic loop.

### 2. Daemon gets per-task Cortex isolation
Integrate agent_cortex.py into daemon. When task is created with isolated_memory=True:
- AgentCortexManager provisions agent-{task_id}.db on port 8300-8399
- Task's agentic loop uses that Cortex instance
- Release after task completes, keep db for history

### 3. Daemon gets SSE streaming
Add to daemon-server.py:
  GET /stream/{task_id} — SSE endpoint that tails task_steps as they're written
Borrow implementation from OpenSquid's dispatcher-server.py /stream/{id}

### 4. Daemon gets skills injection
Copy ~/claude/skills/ to ~/claude/opensquid/skills/
In daemon agentic loop: check skills/ for matching skill file based on task title/tags
If found, prepend skill markdown to task system prompt (after SOUL.md)

---

## Conventions (from parity/CLAUDE.md — follow exactly)

- Auth: Authorization: Bearer emc2ymmv header ONLY — never ?token= query param
- All servers bind 127.0.0.1 only
- SQLite WAL: PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
- Each server gets kick-off.sh (kill old → activate venv → nohup start)
- FastMCP for MCP servers, FastAPI for HTTP-only
- Python venv at ~/claude/opensquid/<service>/venv/
- Token everywhere: emc2ymmv

---

## Ports

| Service | Port |
|---|---|
| daemon | 8256 |
| channels | 8257 |
| browser | 8258 |
| voice-wake | local only |
| agent Cortex pool | 8300-8399 |

---

## Sub-Agent Strategy

Use sub-agents in parallel for speed:
- Agent A: Build daemon/daemon-server.py (core integration)
- Agent B: Copy + verify channels/, browser/, voice-wake/ from parity
- Agent C: Copy agent_cortex.py, skills/, write CLAUDE.md, README.md, watchdog.sh

Agent A is the critical path. Start it first.

---

## Git

Repo: https://github.com/requitefahrenheit/opensquid
PAT: [REDACTED]

```bash
cd ~/claude/opensquid
git init
git remote add origin https://[PAT]@github.com/requitefahrenheit/opensquid.git
git checkout -b merge/parity-opensquid
```

Commit each major component separately with clear messages.
Final commit: "feat(merge): Parity + OpenSquid unified architecture"

---

## AMENDMENT — Dual Memory: Cortex + Markdown

Added March 13, 2026. Implement alongside the core merge.

Every Cortex store operation in the daemon should ALSO append the same content to a daily Markdown log. Dual format: Cortex for semantic search, Markdown for human-readable history and git diffability.

Workspace layout to create at ~/claude/opensquid/:
```
memory/
  YYYY-MM-DD.md    # daily append-only log (auto-created by daemon)
MEMORY.md          # curated long-term facts (updated in place by agent)
SOUL.md            # identity (already exists)
HEARTBEAT.md      # directives (already exists)
```

Implementation:
1. In daemon agentic loop: after every cortex_store call, append same content to ~/claude/opensquid/memory/YYYY-MM-DD.md with a timestamp header
2. Pre-compaction flush: before context limit hit, fire silent turn writing to BOTH Cortex and today's markdown log
3. MEMORY.md = canonical long-term facts, updated in place (not appended). Agent can read/write this directly.
4. Daily logs are append-only. Never edit, only append.

Format for daily log entries:
```markdown
## HH:MM — <source or tag>
<content>
```

This mirrors OpenClaw's memory layout exactly, which means tools and patterns from that ecosystem will be compatible. Zero downside to having both.

---

## Done When

1. ~/claude/opensquid/ directory exists with full structure
2. daemon-server.py runs on port 8256 with task queue + agentic loop + multi-backend + SSE
3. channels, browser, voice-wake copied and functional
4. agent_cortex.py integrated
5. skills/ directory populated
6. All committed and pushed to GitHub merge branch
7. kick-off-all.sh tested
