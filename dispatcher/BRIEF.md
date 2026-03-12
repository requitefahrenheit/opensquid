# Dispatcher — Build Brief

## What this is

A general-purpose agent dispatcher for Jeremy's infrastructure. It is the missing nervous system that connects triggers (Telegram, webhooks, cron, other agents) to agent sessions (multi-agent.py / ralph6.py style loops), with skills loaded from ~/claude/skills/.

## Context

Jeremy has already built:
- `~/claude/multi-agent/multi-agent.py` — 1862-line multi-backend agent (Claude/GPT/Gemini), Worker+Evaluator+Narrator+Coordinator loop, sandboxed Python compute, session save/resume
- `~/claude/ralph6.py` — Dual-thread autonomous research agent (Worker+Evaluator+Context)
- `~/claude/skills/` — Skills directory (solver-launcher, twix-cohere-translate) in SKILL.md format
- `~/claude/watchdog.sh` — Service supervisor (cron every 5 min)
- Cortex MCP at port 8080 — persistent memory
- RWX server at port 8251 — remote shell/file access
- OpenMind at port 8250 — knowledge graph

## What to build: dispatcher.py + dispatcher-server.py

### dispatcher-server.py
A FastAPI server (port 8255) that:
1. Accepts POST /run with JSON body: `{goal, skills[], arch, max_turns, budget, stream}`
2. Accepts POST /run-skill with JSON body: `{skill_name, input, context}`
3. Accepts GET /skills — lists available skills from ~/claude/skills/
4. Accepts GET /status/{session_id} — returns session status
5. Accepts GET /sessions — lists recent sessions
6. Loads SKILL.md files from ~/claude/skills/ and injects them into agent system prompt
7. Spawns agent sessions in background threads/processes
8. Writes session output to Cortex (tagged: dispatcher, session-id)
9. Exposes SSE stream endpoint GET /stream/{session_id} for live output

### dispatcher.py
The core dispatcher logic:
1. `load_skills(skills_dir)` — reads all SKILL.md files, parses YAML frontmatter + instructions
2. `build_system_prompt(base_prompt, skills)` — injects skills as XML block into system prompt
3. `run_session(goal, skills, arch, max_turns, budget)` — wraps multi-agent.py Session + Backend
4. `dispatch(goal, skill_names, arch)` — main entry point

### Skills injection format
When skills are loaded, inject into system prompt as:
```xml
<available_skills>
  <skill name="solver-launcher">
    <description>...</description>
    <instructions>...</instructions>
  </skill>
</available_skills>
```
The agent reads this and decides when to invoke skill behaviors.

### Key design decisions
- Keep it simple: ~200 lines for the server, ~150 lines for dispatcher core
- Reuse multi-agent.py's ClaudeBackend, Session, run() directly (import them)
- Sessions stored in ~/claude/dispatcher/sessions/{session_id}/
- Each session gets: goal.txt, output.log, result.md, status.json
- Cortex integration: on session complete, store summary with tags [dispatcher, session-id, skill-names]
- No auth needed (localhost only, behind Cloudflare tunnel like everything else)
- Token auth: ?token=emc2ymmv (same as other servers)

### watchdog integration
Add to ~/claude/watchdog.sh:
```bash
check_and_restart "dispatcher" 8255 \
  "/home/jfischer/miniconda3/bin/python3 -u /home/jfischer/claude/dispatcher/dispatcher-server.py >> /home/jfischer/claude/dispatcher/dispatcher.log 2>&1"
```

### Cloudflare tunnel
Add to ~/.cloudflared/config.yml:
```yaml
- hostname: dispatcher.fahrenheitrequited.dev
  service: http://localhost:8255
```

### Trigger examples (future)
- Telegram bot sends message → POST /run
- Cron job for overnight builder → POST /run with overnight-builder skill
- Another agent (autonomous heartbeat) → POST /run with sub-goal
- OpenMind webhook → POST /run-skill

## File structure to create
```
~/claude/dispatcher/
  dispatcher.py          # Core logic
  dispatcher-server.py   # FastAPI server
  kick-off.sh           # Start script
  sessions/             # Session output directory
```

## Implementation notes
- Import from multi-agent.py: `sys.path.insert(0, '../multi-agent'); from multi-agent import Session, ClaudeBackend, run`
- If import fails (path issues), inline the minimal Session+ClaudeBackend needed
- Use FastAPI + uvicorn (already installed on the box)
- Use asyncio background tasks for non-blocking session runs
- SSE streaming: yield session log lines as they appear
- Default arch: claude, default model: claude-sonnet-4-6 (cheaper for general tasks vs opus for math)
- ANTHROPIC_API_KEY already in environment (source ~/claude/env.sh)

## Test
After building, test with:
```bash
curl -X POST http://localhost:8255/run?token=emc2ymmv \
  -H 'Content-Type: application/json' \
  -d '{"goal": "Write a haiku about autonomous agents", "arch": "claude", "max_turns": 3}'
```

## Stretch: simple overnight-builder skill
Create ~/claude/skills/overnight-builder/SKILL.md:
- Use this skill when the user wants to build something while they sleep
- Reads goals from a goals.txt file
- Generates 4-5 concrete tasks (one must be a small build)
- Spawns a sub-session for the build task
- Writes results to Cortex tagged: overnight-build
