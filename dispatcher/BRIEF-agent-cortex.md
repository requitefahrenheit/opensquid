# Agent Private Cortex — Build Brief

## Context

The dispatcher (dispatcher-server.py + dispatcher.py) is running on port 8255.
It accepts POST /run {goal, skills[], arch, max_turns, budget, agent_id}.

The Cortex server (dual-server.py) already supports multiple databases via
env vars: CORTEX_DB sets the SQLite path, CORTEX_PORT sets the port.
This pattern is already proven — the autonomous heartbeat agent uses it.

Key files:
- ~/claude/dispatcher/dispatcher.py       — core session logic
- ~/claude/dispatcher/dispatcher-server.py — FastAPI server
- ~/claude/mcp-server/dual-server.py      — Cortex MCP server
- ~/claude/env.sh                          — shared env vars
- ~/cortex/                               — cortex db directory (cortex.db, autonomous.db)
- ~/.cloudflared/config.yml               — Cloudflare tunnel config

## What to build

### 1. agent_cortex.py  (new file in ~/claude/dispatcher/)

A module that manages per-agent private Cortex instances:

```python
class AgentCortexManager:
    """
    Manages private Cortex instances for named agents.
    
    Each named agent (e.g. 'overnight-builder', 'market-scanner') gets:
    - A persistent SQLite db at ~/cortex/agent-{agent_id}.db
    - A Cortex MCP server process on a dynamically assigned port (8300-8399)
    - A public tunnel URL if Cloudflare is configured
    - The process is kept alive for the session duration, then killed
    - The db persists forever (agent memory across runs)
    """
    
    def provision(self, agent_id: str) -> dict:
        """
        Provision a private Cortex for agent_id.
        Returns: {agent_id, db_path, port, url, pid}
        
        If an instance for this agent_id is already running, return it.
        Otherwise: find free port in 8300-8399, launch dual-server.py,
        wait for health check to pass, return connection info.
        """
        
    def release(self, agent_id: str):
        """
        Kill the Cortex process for agent_id.
        Does NOT delete the db. Called when session ends.
        """
        
    def list_agents(self) -> list:
        """
        List all known agents (those with db files in ~/cortex/agent-*.db)
        Include: agent_id, db_size_bytes, entry_count (from db), is_running, port
        """
        
    def get_stats(self, agent_id: str) -> dict:
        """
        Return stats for a specific agent's Cortex:
        entry_count, db_size, oldest_entry, newest_entry, top_tags
        Query the SQLite db directly (don't need server running).
        """
```

Port allocation: scan 8300-8399 for free ports using socket.bind() test.
Process management: use subprocess.Popen, store pid in a dict keyed by agent_id.
Health check: poll http://localhost:{port}/health until 200 or 5s timeout.
Thread safety: use a threading.Lock() for the port/pid dicts.

### 2. Modify dispatcher.py

In run_session(), add agent_cortex support:

```python
def run_session(goal, skills, arch, max_turns, budget, agent_id=None, ...):
    cortex_info = None
    if agent_id:
        cortex_info = agent_cortex_manager.provision(agent_id)
        # inject cortex URL into agent system prompt:
        # "You have a private memory store at {url}. Use it to remember
        # what you've built, learned, and decided across sessions.
        # MCP endpoint: {url}/mcp — tools: store, search, semantic_search, list"
    
    try:
        # ... existing session logic ...
        # Pass cortex_info to system prompt builder
    finally:
        if agent_id and cortex_info:
            agent_cortex_manager.release(agent_id)
```

The cortex URL and tool instructions should be injected into the worker
system prompt as a new section BEFORE the skills block:

```
=== YOUR PRIVATE MEMORY (Cortex) ===
You have a persistent memory store that survives across sessions.
Agent ID: {agent_id}
MCP URL: http://localhost:{port}/mcp
Tools available: store, search, semantic_search, list, get, update, stats

USE YOUR MEMORY:
- On start: search for what you've done before on this goal/topic
- During work: store key decisions, findings, and artifacts
- On completion: store a summary of what you accomplished

This memory persists. Future sessions with the same agent_id will
read what you store now.
=== END MEMORY ===
```

### 3. Modify dispatcher-server.py

Add agent_id to RunRequest:
```python
class RunRequest(BaseModel):
    goal: str
    skills: List[str] = []
    arch: str = "claude"
    max_turns: int = 10
    budget: float = 5.0
    stream: bool = False
    agent_id: Optional[str] = None  # ADD THIS
```

Add new endpoints:
- GET /agents — list all known agents with their cortex stats
- GET /agents/{agent_id} — detailed stats for one agent
- DELETE /agents/{agent_id}/memory — dangerous: deletes the db (requires confirm=true query param)

### 4. Cloudflare tunnel entries (optional, add if easy)

For each running agent cortex, we could expose it at:
  agent-{agent_id}.fahrenheitrequited.dev → localhost:{port}

But this requires reloading cloudflared which is disruptive.
Skip this for now — agents access their Cortex via localhost.
The dispatcher knows the port and passes it to the agent.

### 5. Update watchdog.sh (DO NOT touch cloudflared config)

No changes needed — agent Cortex processes are managed by the dispatcher,
not the watchdog. They're ephemeral per-session.

## Implementation notes

- dual-server.py is already proven. Just launch it with:
  env CORTEX_DB=~/cortex/agent-{id}.db CORTEX_PORT={port} python3 dual-server.py
- The db is created automatically by dual-server.py on first run
- Import AgentCortexManager in dispatcher.py at the top
- Initialize ONE global instance: agent_cortex_manager = AgentCortexManager()
- The MCP injection means the agent can call Cortex tools directly IF the
  session is using Claude Code / MCP-capable runner. For direct API sessions
  (ClaudeBackend), inject the URL in the system prompt and let the agent
  use it via HTTP calls (the Cortex REST API at /api/store, /api/search, etc.)
- Check if dual-server.py exposes a REST API (not just MCP) — if so, document
  the REST endpoints in the injected prompt so the agent can call them with
  the web_search tool or a custom requests call.

## REST API check

Before implementing, read ~/claude/mcp-server/dual-server.py to understand:
1. What REST endpoints exist (likely /api/store, /api/search, /api/semantic_search etc.)
2. What auth is used (token in header or query param)
3. The health check endpoint path
Then document these accurately in the injected prompt.

## Test

After building, test with:
```bash
# Run a session with agent_id
curl -X POST 'http://localhost:8255/run?token=emc2ymmv' \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Remember that you like haiku. Write one.", "agent_id": "test-agent", "max_turns": 2}'

# Check agent list
curl 'http://localhost:8255/agents?token=emc2ymmv'

# Run again - agent should remember
curl -X POST 'http://localhost:8255/run?token=emc2ymmv' \
  -H 'Content-Type: application/json' \
  -d '{"goal": "What do you remember about yourself?", "agent_id": "test-agent", "max_turns": 2}'
```

## Files to create/modify

CREATE:
- ~/claude/dispatcher/agent_cortex.py

MODIFY:
- ~/claude/dispatcher/dispatcher.py     (add agent_id param + cortex injection)
- ~/claude/dispatcher/dispatcher-server.py (add agent_id to RunRequest + /agents endpoints)

Do NOT modify:
- ~/claude/mcp-server/dual-server.py   (use as-is)
- ~/.cloudflared/config.yml             (leave alone)
- ~/claude/watchdog.sh                  (leave alone)
