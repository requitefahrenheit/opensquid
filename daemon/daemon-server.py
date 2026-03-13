#!/usr/bin/env python3
"""
Daemon Server — Agentic Loop + Task Management
================================================
FastMCP server with Claude Opus agentic loop, heartbeat scheduler,
task/schedule/webhook management.

Run:  python3 daemon-server.py
Port: 8254
"""

import os, json, uuid, logging, asyncio, sqlite3, datetime, secrets
from pathlib import Path
from typing import Optional

import httpx
import anthropic
import uvicorn
from fastmcp import FastMCP
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# ─── Config ──────────────────────────────────────────
PORT = int(os.environ.get("DAEMON_PORT", 8256))
DB_PATH = os.environ.get("DAEMON_DB", os.path.expanduser("~/claude/parity/daemon/daemon.db"))
SOUL_PATH = os.path.expanduser("~/claude/parity/daemon/SOUL.md")
HEARTBEAT_PATH = os.path.expanduser("~/claude/parity/daemon/HEARTBEAT.md")
HEARTBEAT_LOG = os.path.expanduser("~/claude/parity/daemon/heartbeat.log")
AUTH_TOKEN = os.environ.get("DAEMON_AUTH_TOKEN", "emc2ymmv")
MAX_AGENT_STEPS = 20
CLAUDE_MODEL = "claude-opus-4-20250514"

# Upstream MCP endpoints
CORTEX_URL = "https://autonomous.fahrenheitrequited.dev"
OPENMIND_URL = "https://openmind.fahrenheitrequited.dev"
RWX_URL = "https://rwx.fahrenheitrequited.dev"

log = logging.getLogger("daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ─── Database ────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS task_steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_num INTEGER NOT NULL,
            action TEXT NOT NULL,
            result TEXT,
            ts TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            cron TEXT NOT NULL,
            prompt TEXT NOT NULL,
            last_run TEXT,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            secret TEXT NOT NULL,
            prompt_template TEXT NOT NULL,
            last_triggered TEXT
        );
    """)
    db.close()
    log.info(f"Database initialized at {DB_PATH}")

# ─── Auth middleware ─────────────────────────────────
def check_auth(headers: dict) -> bool:
    auth = headers.get("authorization", "")
    return auth == f"Bearer {AUTH_TOKEN}"

# ─── Agent tool definitions for Claude ───────────────
AGENT_TOOLS = [
    {
        "name": "cortex_store",
        "description": "Store an entry in Cortex. Content is required, tags and source are optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text content to store"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "source": {"type": "string", "description": "Source context"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "cortex_search",
        "description": "Full-text keyword search across Cortex entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "cortex_semantic_search",
        "description": "Semantic similarity search across Cortex entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "openmind_add_node",
        "description": "Add a new node to OpenMind knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Node content"},
                "label": {"type": "string", "description": "Node label"},
                "node_type": {"type": "string", "description": "Type: note, idea, url, paper, project, task"},
                "url": {"type": "string", "description": "Optional URL"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "openmind_search",
        "description": "Search OpenMind knowledge base by semantic similarity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "openmind_natural_language",
        "description": "Send natural language to OpenMind. Auto-detects intent: add, search, digest, link.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Natural language input"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "dev_run",
        "description": "Run a shell command via rwx-server. Returns stdout, stderr, exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "cwd": {"type": "string", "description": "Working directory"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120}
            },
            "required": ["command"]
        }
    },
    {
        "name": "dev_read_file",
        "description": "Read a file with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or ~-relative)"},
                "line_start": {"type": "integer", "description": "Start line, 1-indexed"},
                "line_end": {"type": "integer", "description": "End line, -1 for EOF"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "dev_write_file",
        "description": "Write content to a file. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "File content"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "dev_list",
        "description": "List files in a directory with sizes and types.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "depth": {"type": "integer", "description": "Directory depth (1-3)", "default": 1},
                "pattern": {"type": "string", "description": "Glob pattern"}
            },
            "required": []
        }
    }
]

# ─── MCP call dispatcher ────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
            timeout=60.0
        )
    return _http_client

async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Dispatch a tool call to the appropriate upstream MCP server."""
    client = await get_http_client()

    # Route to correct upstream
    if tool_name.startswith("cortex_"):
        url = f"{CORTEX_URL}/mcp"
        method = tool_name
    elif tool_name.startswith("openmind_"):
        url = f"{OPENMIND_URL}/mcp"
        method = tool_name.replace("openmind_", "")
    elif tool_name.startswith("dev_"):
        url = f"{RWX_URL}/mcp"
        method = tool_name
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # MCP JSON-RPC call
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": method,
            "arguments": arguments
        }
    }

    try:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data:
            content = data["result"].get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(texts) if texts else json.dumps(data["result"])
        elif "error" in data:
            return json.dumps(data["error"])
        return json.dumps(data)
    except Exception as e:
        log.error(f"MCP call failed [{tool_name}]: {e}")
        return json.dumps({"error": str(e)})

# ─── Agentic loop ────────────────────────────────────
async def run_agent_loop(task_id: str, prompt: str) -> str:
    """Execute the agentic loop: Claude + tools until done or max steps."""
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (now, task_id))
    db.commit()

    # Load SOUL.md as system prompt
    soul = ""
    try:
        soul = Path(SOUL_PATH).read_text()
    except Exception:
        soul = "You are J's personal AI infrastructure."

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    step_num = 0
    final_result = ""

    try:
        while step_num < MAX_AGENT_STEPS:
            step_num += 1
            log.info(f"[AGENT] Task {task_id} step {step_num}")

            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=soul,
                tools=AGENT_TOOLS,
                messages=messages
            )

            # Check for end of turn
            if response.stop_reason == "end_turn":
                # Extract final text
                texts = [b.text for b in response.content if b.type == "text"]
                final_result = "\n".join(texts)
                # Log final step
                db.execute(
                    "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), task_id, step_num, "end_turn", final_result, datetime.datetime.utcnow().isoformat())
                )
                db.commit()
                break

            # Process tool_use blocks
            if response.stop_reason == "tool_use":
                # Add assistant response to messages
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        log.info(f"[AGENT] Tool call: {tool_name}({json.dumps(tool_input)[:200]})")

                        # Log step
                        db.execute(
                            "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
                            (str(uuid.uuid4()), task_id, step_num, f"tool_use:{tool_name}", json.dumps(tool_input)[:2000], datetime.datetime.utcnow().isoformat())
                        )
                        db.commit()

                        # Execute tool
                        result = await call_mcp_tool(tool_name, tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result[:8000]
                        })

                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason — extract text and stop
                texts = [b.text for b in response.content if b.type == "text"]
                final_result = "\n".join(texts) if texts else f"Stopped: {response.stop_reason}"
                break

        # Update task as completed
        now = datetime.datetime.utcnow().isoformat()
        db.execute(
            "UPDATE tasks SET status='completed', result=?, updated_at=? WHERE id=?",
            (final_result[:10000], now, task_id)
        )
        db.commit()

    except Exception as e:
        log.error(f"[AGENT] Task {task_id} failed: {e}")
        now = datetime.datetime.utcnow().isoformat()
        db.execute(
            "UPDATE tasks SET status='failed', result=?, updated_at=? WHERE id=?",
            (str(e)[:5000], now, task_id)
        )
        db.commit()
        final_result = f"Error: {e}"

    finally:
        db.close()

    return final_result

# ─── Heartbeat ───────────────────────────────────────
async def heartbeat_check():
    """Read HEARTBEAT.md — if directives present, create a task. Otherwise log OK."""
    try:
        content = Path(HEARTBEAT_PATH).read_text().strip()
        # Strip header and comment lines
        lines = [l for l in content.splitlines()
                 if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("<!--")]

        now = datetime.datetime.utcnow().isoformat()

        if lines:
            directive = "\n".join(lines)
            log.info(f"[HEARTBEAT] Directive found: {directive[:100]}")
            # Create a task from the directive
            task_id = str(uuid.uuid4())
            db = get_db()
            db.execute(
                "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (task_id, "Heartbeat directive", directive, "pending", now, now)
            )
            db.commit()
            db.close()
            # Run it
            asyncio.create_task(run_agent_loop(task_id, directive))
            _log_heartbeat(f"HEARTBEAT_DIRECTIVE task={task_id}")
        else:
            _log_heartbeat("HEARTBEAT_OK")

    except Exception as e:
        log.error(f"[HEARTBEAT] Error: {e}")
        _log_heartbeat(f"HEARTBEAT_ERROR: {e}")

def _log_heartbeat(msg: str):
    ts = datetime.datetime.utcnow().isoformat()
    line = f"{ts} {msg}\n"
    with open(HEARTBEAT_LOG, "a") as f:
        f.write(line)
    log.info(f"[HEARTBEAT] {msg}")

# ─── Scheduled task runner ───────────────────────────
async def run_scheduled(schedule_id: str, prompt: str):
    """Run a scheduled prompt as a new task."""
    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, f"Scheduled: {schedule_id}", prompt, "pending", now, now)
    )
    db.execute("UPDATE schedules SET last_run=? WHERE id=?", (now, schedule_id))
    db.commit()
    db.close()
    await run_agent_loop(task_id, prompt)

# ─── FastMCP server ─────────────────────────────────
mcp = FastMCP("daemon-server")

@mcp.tool()
async def daemon_task_create(title: str, prompt: str) -> str:
    """Create a new daemon task and start the agentic loop."""
    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, title, prompt, "pending", now, now)
    )
    db.commit()
    db.close()
    log.info(f"[TASK] Created {task_id}: {title}")

    # Fire and forget the agent loop
    asyncio.create_task(run_agent_loop(task_id, prompt))

    return json.dumps({"task_id": task_id, "status": "pending", "title": title})

@mcp.tool()
async def daemon_task_status(task_id: str) -> str:
    """Get status and steps for a task."""
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        db.close()
        return json.dumps({"error": "Task not found"})

    steps = db.execute(
        "SELECT step_num, action, result, ts FROM task_steps WHERE task_id=? ORDER BY step_num",
        (task_id,)
    ).fetchall()
    db.close()

    return json.dumps({
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "result": task["result"],
        "steps": [dict(s) for s in steps]
    })

@mcp.tool()
async def daemon_task_list(limit: int = 20) -> str:
    """List recent tasks."""
    db = get_db()
    tasks = db.execute(
        "SELECT id, title, status, created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    db.close()
    return json.dumps([dict(t) for t in tasks])

@mcp.tool()
async def daemon_task_cancel(task_id: str) -> str:
    """Cancel a pending or running task."""
    db = get_db()
    task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        db.close()
        return json.dumps({"error": "Task not found"})

    now = datetime.datetime.utcnow().isoformat()
    db.execute(
        "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?",
        (now, task_id)
    )
    db.commit()
    db.close()
    log.info(f"[TASK] Cancelled {task_id}")
    return json.dumps({"task_id": task_id, "status": "cancelled"})

@mcp.tool()
async def daemon_heartbeat() -> str:
    """Get last heartbeat status and recent log entries."""
    # Read last 20 lines of heartbeat log
    tail = ""
    try:
        lines = Path(HEARTBEAT_LOG).read_text().splitlines()
        tail = "\n".join(lines[-20:])
    except FileNotFoundError:
        tail = "(no heartbeat log yet)"

    # Read current HEARTBEAT.md
    content = ""
    try:
        content = Path(HEARTBEAT_PATH).read_text()
    except FileNotFoundError:
        content = "(HEARTBEAT.md not found)"

    return json.dumps({
        "heartbeat_md": content,
        "recent_log": tail
    })

@mcp.tool()
async def daemon_schedule_add(cron: str, prompt: str) -> str:
    """Add a cron-scheduled prompt. Cron format: 'minute hour day month day_of_week'."""
    schedule_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO schedules (id, cron, prompt, enabled) VALUES (?,?,?,1)",
        (schedule_id, cron, prompt)
    )
    db.commit()
    db.close()

    # Register with APScheduler
    _register_cron_job(schedule_id, cron, prompt)

    log.info(f"[SCHEDULE] Added {schedule_id}: {cron}")
    return json.dumps({"schedule_id": schedule_id, "cron": cron, "prompt": prompt})

@mcp.tool()
async def daemon_schedule_list() -> str:
    """List all schedules."""
    db = get_db()
    schedules = db.execute("SELECT * FROM schedules ORDER BY rowid").fetchall()
    db.close()
    return json.dumps([dict(s) for s in schedules])

@mcp.tool()
async def daemon_webhook_create(name: str, prompt_template: str) -> str:
    """Create a webhook endpoint. Returns the webhook ID and secret."""
    webhook_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(24)
    db = get_db()
    db.execute(
        "INSERT INTO webhooks (id, name, secret, prompt_template) VALUES (?,?,?,?)",
        (webhook_id, name, secret, prompt_template)
    )
    db.commit()
    db.close()
    log.info(f"[WEBHOOK] Created {webhook_id}: {name}")
    return json.dumps({"webhook_id": webhook_id, "name": name, "secret": secret})

# ─── Webhook HTTP endpoint ──────────────────────────
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

async def webhook_handler(request: StarletteRequest):
    """Handle incoming webhook POST requests."""
    webhook_id = request.path_params.get("webhook_id", "")
    db = get_db()
    webhook = db.execute("SELECT * FROM webhooks WHERE id=?", (webhook_id,)).fetchone()
    if not webhook:
        db.close()
        return JSONResponse({"error": "Webhook not found"}, status_code=404)

    # Verify secret
    provided_secret = request.headers.get("x-webhook-secret", "")
    if provided_secret != webhook["secret"]:
        db.close()
        return JSONResponse({"error": "Invalid secret"}, status_code=403)

    # Parse body and fill template
    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt = webhook["prompt_template"]
    for key, val in body.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(val))

    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE webhooks SET last_triggered=? WHERE id=?", (now, webhook_id))
    db.commit()
    db.close()

    # Create and run task
    task_id = str(uuid.uuid4())
    db2 = get_db()
    db2.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, f"Webhook: {webhook['name']}", prompt, "pending", now, now)
    )
    db2.commit()
    db2.close()

    asyncio.create_task(run_agent_loop(task_id, prompt))

    log.info(f"[WEBHOOK] Triggered {webhook_id} → task {task_id}")
    return JSONResponse({"task_id": task_id, "status": "pending"})

# ─── Scheduler setup ────────────────────────────────
scheduler = AsyncIOScheduler()

def _register_cron_job(schedule_id: str, cron: str, prompt: str):
    """Parse cron string and register with APScheduler."""
    parts = cron.strip().split()
    if len(parts) != 5:
        log.error(f"[SCHEDULE] Invalid cron: {cron}")
        return
    minute, hour, day, month, dow = parts
    trigger = CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=dow
    )
    scheduler.add_job(
        run_scheduled, trigger,
        args=[schedule_id, prompt],
        id=f"schedule_{schedule_id}",
        replace_existing=True
    )

def load_schedules():
    """Load all enabled schedules from DB into APScheduler."""
    db = get_db()
    schedules = db.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
    db.close()
    for s in schedules:
        _register_cron_job(s["id"], s["cron"], s["prompt"])
    log.info(f"[SCHEDULE] Loaded {len(schedules)} schedules")

# ─── App startup ─────────────────────────────────────
def create_app():
    """Create the ASGI app with MCP + webhook routes."""
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount

    # Get the MCP ASGI app
    mcp_app = mcp.http_app(path="/mcp")

    # Add webhook route
    routes = [
        Route("/webhook/{webhook_id}", webhook_handler, methods=["POST"]),
    ]

    # Mount MCP app and add custom routes
    app = Starlette(routes=routes)

    # Mount MCP at root so /mcp works
    app.mount("/", mcp_app)

    return app

async def startup():
    init_db()
    load_schedules()

    # Heartbeat every 30 minutes
    scheduler.add_job(
        heartbeat_check,
        IntervalTrigger(minutes=30),
        id="heartbeat",
        replace_existing=True
    )
    scheduler.start()
    log.info("[STARTUP] Scheduler started with heartbeat every 30 min")

    # Run initial heartbeat
    await heartbeat_check()

if __name__ == "__main__":
    import asyncio

    init_db()

    app = create_app()

    @app.on_event("startup")
    async def on_startup():
        await startup()

    log.info(f"Starting daemon-server on port {PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
