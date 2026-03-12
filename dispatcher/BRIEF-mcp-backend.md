# MCP Backend for Dispatcher — Build Brief

## Context

The dispatcher at ~/claude/dispatcher/ has:
- dispatcher.py — core session logic, currently only has 'claude' (raw API) backend
- dispatcher-server.py — FastAPI server on port 8255
- agent_cortex.py — AgentCortexManager provisions per-agent SQLite dbs on ports 8300-8399
- ~/claude/mcp-server/dual-server.py — Cortex MCP server
- ~/claude/env.sh — exports all API keys

The problem: when an agent_id is provided, we provision a private Cortex and tell the
agent about it via system prompt — but the raw API backend can't actually call MCP tools.
We need a Claude Code backend that runs the session with the private Cortex wired in as
an actual MCP server the agent can use via tool_use.

## What to build

### 1. Add 'claude-code' backend to dispatcher.py

New function: `run_session_claude_code(goal, cortex_info, skills, session_dir, max_turns, ...)`

This runs a Claude Code session (using the `claude` CLI) with:
- `--mcp-config` pointing to a temp JSON file that configures the agent's private Cortex
- `--dangerously-skip-permissions` so it runs unattended
- `--system-prompt` or `-p` for the goal
- Output captured to session_dir/output.log

The MCP config JSON for the session should look like:
```json
{
  "mcpServers": {
    "my-cortex": {
      "type": "sse",
      "url": "http://localhost:{cortex_port}/mcp"
    }
  }
}
```
This gives the agent tools: cortex_store, cortex_search, cortex_semantic_search, cortex_list, cortex_get, cortex_stats — all scoped to its private db.

The system prompt injected into the claude session should:
1. Tell it about its role and goal
2. Tell it about its private memory tools (MCP tools named my-cortex__cortex_store etc.)
3. Inject any skills from the skills block
4. Instruct it to use memory on start (search) and finish (store summary)

The `claude` CLI binary is at: ~/.npm-global/bin/claude
To run a session:
```bash
claude --dangerously-skip-permissions \
  --mcp-config /tmp/agent-mcp-{session_id}.json \
  -p "{goal_with_system_instructions}" \
  >> {session_dir}/output.log 2>&1
```

Capture stdout/stderr to output.log. Run as subprocess.run() (blocking) since it's
already in a background thread from the dispatcher.

### 2. Modify run_session() in dispatcher.py

Add arch='claude-code' branch:
```python
if arch == 'claude-code' or (arch == 'claude' and agent_id):
    return run_session_claude_code(...)
```

Actually: when agent_id is provided AND arch is 'claude', automatically upgrade to
'claude-code' backend so the agent actually has MCP tools. If arch is explicitly
'claude' and no agent_id, keep the raw API backend.

So the logic is:
- arch='claude-code' OR (agent_id is set AND arch='claude') → use claude-code backend
- arch='claude' with no agent_id → use existing raw API backend

### 3. Modify RunRequest in dispatcher-server.py

Add 'claude-code' as a valid arch option (it may already be there as a string, just
make sure it's documented in the OpenAPI description).

### 4. MCP config file management

- Write temp MCP config to /tmp/agent-mcp-{session_id}.json before session
- Clean it up after session completes
- If no cortex_info (provision failed), still run claude-code but without the MCP config
  (just use -p with the goal directly, no --mcp-config)

### 5. Cost tracking for claude-code sessions

The claude CLI doesn't expose cost via stdout easily. For now:
- Set cost = 0.0 (unknown) for claude-code sessions
- Parse output.log for any cost lines if claude CLI outputs them
- Store session metadata as usual

### 6. Test

After implementing, test:
```bash
# Run with agent_id (triggers claude-code backend + private Cortex)
curl -X POST 'http://localhost:8255/run?token=emc2ymmv' \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Search your memory for anything about haiku, then write a new haiku about agents and store it in your memory.", "agent_id": "test-agent", "max_turns": 3}'

# Wait ~30s, then check the session output
SESSION_ID=$(curl -s 'http://localhost:8255/sessions?token=emc2ymmv' | python3 -c 'import sys,json; sessions=json.load(sys.stdin); print(sorted(sessions, key=lambda s: s["started"])[-1]["session_id"])')
curl "http://localhost:8255/status/$SESSION_ID?token=emc2ymmv"

# Check if agent's cortex now has entries
curl 'http://localhost:8255/agents?token=emc2ymmv'
```

## Files to create/modify

MODIFY:
- ~/claude/dispatcher/dispatcher.py
  - Add run_session_claude_code() function
  - Modify run_session() to route to it based on arch + agent_id

MODIFY (minimally):
- ~/claude/dispatcher/dispatcher-server.py
  - No changes needed unless RunRequest needs 'claude-code' arch validation

Do NOT modify:
- agent_cortex.py (already working)
- dual-server.py (already working)
- watchdog.sh
- cloudflared config

## Notes

- The claude CLI path: /home/jfischer/.npm-global/bin/claude
- The env.sh must be sourced before running claude (for ANTHROPIC_API_KEY)
- subprocess.run() with env=os.environ.copy() should have the key already
  if the dispatcher was started via start.sh which sources env.sh
- The -p flag passes a prompt to claude CLI non-interactively
- Skills should be embedded directly in the -p prompt text (not separate system)
- max_turns maps to --max-turns if claude CLI supports it, else just run and let it finish
- Check: does `claude --help` show a --max-turns flag? If not, omit it.
- The goal prompt passed to -p should include: the agent role, memory instructions,
  skills block, and the actual user goal at the end
