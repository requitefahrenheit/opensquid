#!/usr/bin/env python3
"""
OpenSquid Daemon Server
========================
Parity daemon architecture + OpenSquid dispatcher endpoints merged.

MCP server:  /mcp  (FastMCP, streamable HTTP)
HTTP API:    /run  /stream/{id}  /sessions  /status/{id}  /health  /skills  /agents
Webhooks:    /webhook/{id}
Port:        8256
Auth:        Authorization: Bearer emc2ymmv
"""

import asyncio
import datetime
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
import anthropic
import uvicorn
from fastmcp import FastMCP
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, Mount

# ─── Config ──────────────────────────────────────────
BASE_DIR   = Path.home() / "claude" / "opensquid"
PORT       = int(os.environ.get("DAEMON_PORT", 8256))
DB_PATH    = os.environ.get("DAEMON_DB", str(BASE_DIR / "daemon.db"))
SOUL_PATH  = str(BASE_DIR / "SOUL.md")
HEARTBEAT_PATH = str(BASE_DIR / "HEARTBEAT.md")
HEARTBEAT_LOG  = str(BASE_DIR / "heartbeat.log")
SESSIONS_DIR   = BASE_DIR / "sessions"
SKILLS_DIR     = BASE_DIR / "skills"
AUTH_TOKEN     = os.environ.get("DAEMON_AUTH_TOKEN", "emc2ymmv")
MAX_AGENT_STEPS = 20
CLAUDE_MODEL    = "claude-opus-4-20250514"
CLAUDE_BIN      = Path.home() / ".npm-global" / "bin" / "claude"
PYTHON          = Path.home() / "miniconda3" / "bin" / "python3"
CORTEX_DIR      = Path.home() / "cortex"
DUAL_SERVER     = Path.home() / "claude" / "mcp-server" / "dual-server.py"
PORT_RANGE      = range(8300, 8400)

# Upstream MCP endpoints
CORTEX_URL   = "https://autonomous.fahrenheitrequited.dev"
OPENMIND_URL = "https://openmind.fahrenheitrequited.dev"
RWX_URL      = "https://rwx.fahrenheitrequited.dev"

log = logging.getLogger("opensquid")
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
            result TEXT,
            backend TEXT DEFAULT 'api',
            isolated_memory INTEGER DEFAULT 0,
            agent_id TEXT,
            skills TEXT DEFAULT '[]',
            budget REAL DEFAULT 5.0
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
    # Migrate existing tasks table: add new columns if missing
    for col_def in [
        ("backend", "TEXT DEFAULT 'api'"),
        ("isolated_memory", "INTEGER DEFAULT 0"),
        ("agent_id", "TEXT"),
        ("skills", "TEXT DEFAULT '[]'"),
        ("budget", "REAL DEFAULT 5.0"),
    ]:
        try:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {col_def[0]} {col_def[1]}")
            db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    db.close()
    log.info(f"Database ready at {DB_PATH}")

# ─── Auth ────────────────────────────────────────────
def check_auth(headers) -> bool:
    auth = headers.get("authorization", "")
    return auth == f"Bearer {AUTH_TOKEN}"

def auth_required(request: StarletteRequest):
    if not check_auth(dict(request.headers)):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None

# ─── Skill loading ───────────────────────────────────
def _parse_yaml_frontmatter(text: str) -> dict:
    try:
        import yaml
        result = yaml.safe_load(text)
        return result if isinstance(result, dict) else {}
    except Exception:
        pass
    result = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result

def load_skills(skills_dir: Path = None) -> dict:
    if skills_dir is None:
        skills_dir = SKILLS_DIR
    skills = {}
    if not skills_dir.exists():
        return skills
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue
        if not content.startswith("---"):
            continue
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue
        meta = _parse_yaml_frontmatter(parts[1])
        name = meta.get("name", child.name)
        desc = meta.get("description", "")
        if isinstance(desc, dict):
            desc = str(desc)
        skills[name] = {
            "name": name,
            "description": str(desc).strip().replace("\n", " "),
            "instructions": parts[2].strip(),
        }
    return skills

def _skills_block(skills: dict) -> str:
    if not skills:
        return ""
    lines = ["<available_skills>"]
    for name, skill in skills.items():
        lines.append(f'  <skill name="{name}">')
        lines.append(f"    <description>{skill.get('description','')}</description>")
        lines.append("    <instructions>")
        for l in skill.get("instructions", "").splitlines():
            lines.append(f"    {l}")
        lines.append("    </instructions>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n\n" + "\n".join(lines) + "\n"

# ─── Agent Cortex Manager ────────────────────────────
HEALTH_TIMEOUT = 180.0

def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

class AgentCortexManager:
    """Per-task isolated Cortex instances on ports 8300-8399."""
    def __init__(self):
        self._lock = threading.Lock()
        self._running: dict = {}

    def _find_free_port(self) -> int:
        with self._lock:
            used = {info["port"] for info in self._running.values()}
        for port in PORT_RANGE:
            if port not in used and _is_port_free(port):
                return port
        raise RuntimeError("No free port in range 8300-8399")

    def _wait_healthy(self, port: int) -> bool:
        import urllib.request
        url = f"http://localhost:{port}/api/stats"
        deadline = time.time() + HEALTH_TIMEOUT
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def provision(self, agent_id: str) -> dict:
        with self._lock:
            if agent_id in self._running:
                info = self._running[agent_id]
                if info["process"].poll() is None:
                    return {k: v for k, v in info.items() if k != "process"}
                del self._running[agent_id]

        CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(CORTEX_DIR / f"agent-{agent_id}.db")
        port = self._find_free_port()
        env = os.environ.copy()
        env["CORTEX_DB"] = db_path
        env["CORTEX_PORT"] = str(port)
        proc = subprocess.Popen(
            [str(PYTHON), str(DUAL_SERVER)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info(f"[CORTEX] Launched for '{agent_id}' on port {port} (pid={proc.pid})")
        if not self._wait_healthy(port):
            proc.kill()
            raise RuntimeError(f"Agent Cortex for '{agent_id}' failed to start on port {port}")
        log.info(f"[CORTEX] '{agent_id}' ready on port {port}")
        info = {
            "agent_id": agent_id, "db_path": db_path,
            "port": port, "url": f"http://localhost:{port}",
            "pid": proc.pid, "process": proc,
        }
        with self._lock:
            self._running[agent_id] = info
        return {k: v for k, v in info.items() if k != "process"}

    def release(self, agent_id: str):
        with self._lock:
            info = self._running.pop(agent_id, None)
        if not info:
            return
        try:
            info["process"].terminate()
            info["process"].wait(timeout=5)
        except Exception:
            try:
                info["process"].kill()
            except Exception:
                pass
        log.info(f"[CORTEX] Released '{agent_id}' (port {info['port']})")

    def list_agents(self) -> list:
        if not CORTEX_DIR.exists():
            return []
        with self._lock:
            running_ports = {aid: info["port"] for aid, info in self._running.items()}
        agents = []
        for db_file in sorted(CORTEX_DIR.glob("agent-*.db")):
            agent_id = db_file.stem[len("agent-"):]
            entry_count = 0
            try:
                conn = sqlite3.connect(str(db_file))
                entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                conn.close()
            except Exception:
                pass
            agents.append({
                "agent_id": agent_id,
                "db_size_bytes": db_file.stat().st_size,
                "entry_count": entry_count,
                "is_running": agent_id in running_ports,
                "port": running_ports.get(agent_id),
            })
        return agents

    def get_stats(self, agent_id: str) -> dict:
        db_path = CORTEX_DIR / f"agent-{agent_id}.db"
        if not db_path.exists():
            return {"error": f"No Cortex db for '{agent_id}'"}
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            if total == 0:
                conn.close()
                return {"agent_id": agent_id, "entry_count": 0, "db_size_bytes": db_path.stat().st_size}
            rows = conn.execute("SELECT timestamp FROM entries ORDER BY timestamp").fetchall()
            conn.close()
            return {
                "agent_id": agent_id, "entry_count": total,
                "db_size_bytes": db_path.stat().st_size,
                "oldest_entry": rows[0]["timestamp"],
                "newest_entry": rows[-1]["timestamp"],
            }
        except Exception as e:
            return {"error": str(e), "agent_id": agent_id}

agent_cortex_manager = AgentCortexManager()

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

async def call_mcp_tool(tool_name: str, arguments: dict, cortex_url: str = None) -> str:
    """Dispatch a tool call to the appropriate upstream MCP server."""
    client = await get_http_client()
    if tool_name.startswith("cortex_"):
        url = f"{cortex_url or CORTEX_URL}/mcp"
        method = tool_name
    elif tool_name.startswith("openmind_"):
        url = f"{OPENMIND_URL}/mcp"
        method = tool_name.replace("openmind_", "")
    elif tool_name.startswith("dev_"):
        url = f"{RWX_URL}/mcp"
        method = tool_name
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": method, "arguments": arguments}
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

# ─── Agent tool definitions ──────────────────────────
AGENT_TOOLS = [
    {
        "name": "cortex_store",
        "description": "Store an entry in Cortex. Content is required, tags and source are optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"}
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
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
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
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5}
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
                "content": {"type": "string"},
                "label": {"type": "string"},
                "node_type": {"type": "string"},
                "url": {"type": "string"}
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
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "openmind_natural_language",
        "description": "Send natural language to OpenMind. Auto-detects intent.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"]
        }
    },
    {
        "name": "dev_run",
        "description": "Run a shell command via rwx-server.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "default": 120}
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
                "path": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "dev_write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "dev_list",
        "description": "List files in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer", "default": 1},
                "pattern": {"type": "string"}
            },
            "required": []
        }
    }
]

# ─── Claude Code backend ─────────────────────────────
def _build_claude_code_prompt(goal: str, cortex_info: dict, skills: dict, agent_id: str) -> str:
    lines = [
        "You are a general-purpose autonomous agent. You receive a goal and work toward completing it.",
        "",
        "RULES:",
        "- Never end a turn with a question. Always commit to your best action and execute it.",
        "- Each turn, make meaningful progress toward the goal.",
        "- Write your work out loud — internal reasoning is not recorded.",
        "- When the goal is fully addressed, end your final message with [DONE].",
        "- Do NOT say [DONE] prematurely. Only say it when the goal is genuinely complete.",
    ]
    if cortex_info:
        lines += [
            "",
            "=== YOUR PRIVATE MEMORY (Cortex) ===",
            f"Agent ID: {agent_id}",
            "You have persistent MCP memory tools available:",
            "  my-cortex__cortex_store           — save entries to memory",
            "  my-cortex__cortex_search          — full-text keyword search",
            "  my-cortex__cortex_semantic_search — meaning-based search",
            "  my-cortex__cortex_list            — list recent entries",
            "  my-cortex__cortex_get             — get a specific entry by id",
            "  my-cortex__cortex_stats           — storage statistics",
            "",
            "USE YOUR MEMORY: search at start, store decisions and findings, summarize at end.",
            "=== END MEMORY ===",
        ]
    if skills:
        lines.append("")
        lines.append("<available_skills>")
        for name, skill in skills.items():
            lines.append(f'  <skill name="{name}">')
            lines.append(f"    <description>{skill.get('description','')}</description>")
            lines.append("    <instructions>")
            for il in skill.get("instructions", "").splitlines():
                lines.append(f"    {il}")
            lines.append("    </instructions>")
            lines.append("  </skill>")
        lines.append("</available_skills>")
    lines += ["", "=== GOAL ===", goal, "=== END GOAL ==="]
    return "\n".join(lines)

def _write_step(task_id: str, step_num: int, action: str, result: str):
    """Write a task step to DB (thread-safe, opens its own connection)."""
    db = get_db()
    db.execute(
        "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), task_id, step_num,
         action, (result or "")[:4000], datetime.datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()

def run_claude_code_task(task_id: str, goal: str, cortex_info: dict, skills: dict,
                          agent_id: str, budget: float):
    """Run claude CLI in a thread, streaming output lines to task_steps."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = SESSIONS_DIR / task_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "goal.txt").write_text(goal)
    log_path = session_dir / "output.log"

    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (now, task_id))
    db.commit()
    db.close()

    full_prompt = _build_claude_code_prompt(goal, cortex_info, skills, agent_id)
    cmd = [str(CLAUDE_BIN), "--dangerously-skip-permissions", "--max-budget-usd", str(budget)]
    mcp_config_path = None

    try:
        if cortex_info:
            mcp_config_path = f"/tmp/agent-mcp-{task_id}.json"
            mcp_config = {
                "mcpServers": {
                    "my-cortex": {
                        "type": "http",
                        "url": f"http://localhost:{cortex_info['port']}/mcp",
                    }
                }
            }
            with open(mcp_config_path, "w") as f:
                json.dump(mcp_config, f)
            cmd += ["--mcp-config", mcp_config_path]

        cmd += ["-p", full_prompt]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        node_paths = ["/usr/local/bin", "/usr/bin", str(Path.home() / ".npm-global" / "bin")]
        existing_path = env.get("PATH", "")
        extra = ":".join(p for p in node_paths if p not in existing_path)
        if extra:
            env["PATH"] = extra + ":" + existing_path

        _write_step(task_id, 1, "claude-code:start", f"budget=${budget}")

        # Run subprocess, capture output
        with open(log_path, "w") as log_file:
            proc = subprocess.run(cmd, stdout=log_file, stderr=log_file, env=env)

        result_text = log_path.read_text() if log_path.exists() else ""
        final_status = "completed" if proc.returncode == 0 else "failed"

        # Write output as a step (truncated)
        _write_step(task_id, 2, "claude-code:output", result_text[-4000:])

        db = get_db()
        now = datetime.datetime.utcnow().isoformat()
        db.execute(
            "UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?",
            (final_status, result_text[-10000:], now, task_id)
        )
        db.commit()
        db.close()
        log.info(f"[CLAUDE-CODE] Task {task_id} {final_status}")

    except Exception as e:
        log.error(f"[CLAUDE-CODE] Task {task_id} failed: {e}")
        _write_step(task_id, 99, "error", str(e))
        db = get_db()
        now = datetime.datetime.utcnow().isoformat()
        db.execute(
            "UPDATE tasks SET status='failed', result=?, updated_at=? WHERE id=?",
            (str(e)[:5000], now, task_id)
        )
        db.commit()
        db.close()
    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            try:
                os.unlink(mcp_config_path)
            except Exception:
                pass
        if cortex_info:
            agent_cortex_manager.release(agent_id or task_id)

# ─── Raw API agentic loop ─────────────────────────────
async def run_agent_loop(task_id: str, prompt: str,
                          skills: dict = None,
                          cortex_url: str = None) -> str:
    """Execute the agentic loop: Claude + tools until done or max steps."""
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (now, task_id))
    db.commit()
    db.close()

    # Load SOUL.md as system prompt
    soul = ""
    try:
        soul = Path(SOUL_PATH).read_text()
    except Exception:
        soul = "You are J's personal AI infrastructure."

    if skills:
        soul += _skills_block(skills)

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    step_num = 0
    final_result = ""
    db = get_db()

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

            if response.stop_reason == "end_turn":
                texts = [b.text for b in response.content if b.type == "text"]
                final_result = "\n".join(texts)
                db.execute(
                    "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), task_id, step_num, "end_turn",
                     final_result[:4000], datetime.datetime.utcnow().isoformat())
                )
                db.commit()
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        db.execute(
                            "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
                            (str(uuid.uuid4()), task_id, step_num,
                             f"tool_use:{block.name}",
                             json.dumps(block.input)[:2000],
                             datetime.datetime.utcnow().isoformat())
                        )
                        db.commit()
                        result = await call_mcp_tool(block.name, block.input,
                                                      cortex_url=cortex_url)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result[:8000]
                        })
                        log.info(f"[AGENT] {block.name} → {result[:100]}")
                messages.append({"role": "user", "content": tool_results})
            else:
                texts = [b.text for b in response.content if b.type == "text"]
                final_result = "\n".join(texts) if texts else f"Stopped: {response.stop_reason}"
                break

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

# ─── Task dispatcher ─────────────────────────────────
async def run_task(task_id: str):
    """Dispatch a task to the appropriate backend."""
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    db.close()
    if not row:
        log.error(f"[TASK] Task {task_id} not found")
        return

    backend  = row["backend"] or "api"
    prompt   = row["prompt"]
    agent_id = row["agent_id"] or task_id
    budget   = row["budget"] or 5.0
    isolated = bool(row["isolated_memory"])
    skill_names = json.loads(row["skills"] or "[]")

    # Load selected skills
    all_skills = load_skills()
    skills = {k: v for k, v in all_skills.items() if k in skill_names} if skill_names else {}

    cortex_info = None
    cortex_url = None
    if isolated:
        try:
            cortex_info = agent_cortex_manager.provision(agent_id)
            cortex_url = f"http://localhost:{cortex_info['port']}"
            log.info(f"[TASK] Isolated Cortex for '{agent_id}' on port {cortex_info['port']}")
        except Exception as e:
            log.error(f"[TASK] Cortex provision failed: {e}")

    if backend == "claude-code":
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            run_claude_code_task,
            task_id, prompt, cortex_info, skills, agent_id, budget
        )
    else:
        await run_agent_loop(task_id, prompt, skills=skills, cortex_url=cortex_url)
        if cortex_info:
            agent_cortex_manager.release(agent_id)

# ─── Heartbeat ───────────────────────────────────────
async def heartbeat_check():
    try:
        content = Path(HEARTBEAT_PATH).read_text().strip()
        lines = [l for l in content.splitlines()
                 if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("<!--")]
        now = datetime.datetime.utcnow().isoformat()
        if lines:
            directive = "\n".join(lines)
            log.info(f"[HEARTBEAT] Directive: {directive[:100]}")
            task_id = str(uuid.uuid4())
            db = get_db()
            db.execute(
                "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (task_id, "Heartbeat directive", directive, "pending", now, now)
            )
            db.commit()
            db.close()
            asyncio.create_task(run_task(task_id))
            _log_heartbeat(f"HEARTBEAT_DIRECTIVE task={task_id}")
        else:
            _log_heartbeat("HEARTBEAT_OK")
    except Exception as e:
        log.error(f"[HEARTBEAT] Error: {e}")
        _log_heartbeat(f"HEARTBEAT_ERROR: {e}")

def _log_heartbeat(msg: str):
    ts = datetime.datetime.utcnow().isoformat()
    with open(HEARTBEAT_LOG, "a") as f:
        f.write(f"{ts} {msg}\n")
    log.info(f"[HEARTBEAT] {msg}")

# ─── Scheduled task runner ───────────────────────────
async def run_scheduled(schedule_id: str, prompt: str):
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
    await run_task(task_id)

# ─── FastMCP tools ──────────────────────────────────
mcp = FastMCP("opensquid-daemon")

@mcp.tool()
async def daemon_task_create(title: str, prompt: str,
                              backend: str = "api",
                              skills: list = None,
                              isolated_memory: bool = False,
                              agent_id: str = None,
                              budget: float = 5.0) -> str:
    """Create a new daemon task and start the agentic loop."""
    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at, backend, isolated_memory, agent_id, skills, budget) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, title, prompt, "pending", now, now,
         backend, int(isolated_memory), agent_id,
         json.dumps(skills or []), budget)
    )
    db.commit()
    db.close()
    log.info(f"[TASK] Created {task_id}: {title} (backend={backend})")
    asyncio.create_task(run_task(task_id))
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
        "id": task["id"], "title": task["title"],
        "status": task["status"], "backend": task["backend"],
        "created_at": task["created_at"], "updated_at": task["updated_at"],
        "result": task["result"],
        "steps": [dict(s) for s in steps]
    })

@mcp.tool()
async def daemon_task_list(limit: int = 20) -> str:
    """List recent tasks."""
    db = get_db()
    tasks = db.execute(
        "SELECT id, title, status, backend, created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
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
    db.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?", (now, task_id))
    db.commit()
    db.close()
    return json.dumps({"task_id": task_id, "status": "cancelled"})

@mcp.tool()
async def daemon_heartbeat() -> str:
    """Get last heartbeat status and recent log entries."""
    tail = ""
    try:
        lines = Path(HEARTBEAT_LOG).read_text().splitlines()
        tail = "\n".join(lines[-20:])
    except FileNotFoundError:
        tail = "(no heartbeat log yet)"
    content = ""
    try:
        content = Path(HEARTBEAT_PATH).read_text()
    except FileNotFoundError:
        content = "(HEARTBEAT.md not found)"
    return json.dumps({"heartbeat_md": content, "recent_log": tail})

@mcp.tool()
async def daemon_schedule_add(cron: str, prompt: str) -> str:
    """Add a cron-scheduled prompt."""
    schedule_id = str(uuid.uuid4())
    db = get_db()
    db.execute("INSERT INTO schedules (id, cron, prompt, enabled) VALUES (?,?,?,1)",
               (schedule_id, cron, prompt))
    db.commit()
    db.close()
    _register_cron_job(schedule_id, cron, prompt)
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
    """Create a webhook endpoint."""
    webhook_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(24)
    db = get_db()
    db.execute("INSERT INTO webhooks (id, name, secret, prompt_template) VALUES (?,?,?,?)",
               (webhook_id, name, secret, prompt_template))
    db.commit()
    db.close()
    return json.dumps({"webhook_id": webhook_id, "name": name, "secret": secret})

# ─── HTTP API handlers ────────────────────────────────
async def http_health(request: StarletteRequest):
    return JSONResponse({"status": "ok", "service": "opensquid-daemon", "port": PORT})

async def http_run(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    title  = body.get("title") or body.get("goal", "")[:80] or "Untitled"
    prompt = body.get("prompt") or body.get("goal", "")
    if not prompt:
        return JSONResponse({"error": "prompt or goal required"}, status_code=400)

    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at, backend, isolated_memory, agent_id, skills, budget) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, title, prompt, "pending", now, now,
            body.get("backend", "api"),
            int(body.get("isolated_memory", False)),
            body.get("agent_id"),
            json.dumps(body.get("skills", [])),
            float(body.get("budget", 5.0))
        )
    )
    db.commit()
    db.close()
    log.info(f"[HTTP /run] Created task {task_id}: {title}")
    asyncio.create_task(run_task(task_id))
    return JSONResponse({
        "task_id": task_id,
        "status": "pending",
        "title": title,
        "stream_url": f"/stream/{task_id}",
        "status_url": f"/status/{task_id}",
    })

async def http_status(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    task_id = request.path_params.get("task_id", "")
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        db.close()
        return JSONResponse({"error": "Task not found"}, status_code=404)
    steps = db.execute(
        "SELECT step_num, action, result, ts FROM task_steps WHERE task_id=? ORDER BY step_num",
        (task_id,)
    ).fetchall()
    db.close()
    return JSONResponse({
        "id": task["id"], "title": task["title"],
        "status": task["status"], "backend": task["backend"],
        "created_at": task["created_at"], "updated_at": task["updated_at"],
        "result": task["result"],
        "steps": [dict(s) for s in steps]
    })

async def http_sessions(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    limit = int(request.query_params.get("limit", 20))
    db = get_db()
    tasks = db.execute(
        "SELECT id, title, status, backend, created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    db.close()
    return JSONResponse([dict(t) for t in tasks])

async def http_stream(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    task_id = request.path_params.get("task_id", "")

    async def event_gen():
        seen_step_ids = set()
        timeout = 300  # 5 min max
        elapsed = 0.0

        while elapsed < timeout:
            db = get_db()
            task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                db.close()
                yield "data: [Task not found]\n\n"
                return

            steps = db.execute(
                "SELECT id, step_num, action, result, ts FROM task_steps WHERE task_id=? ORDER BY step_num",
                (task_id,)
            ).fetchall()
            db.close()

            for step in steps:
                if step["id"] not in seen_step_ids:
                    seen_step_ids.add(step["id"])
                    data = json.dumps({
                        "step": step["step_num"],
                        "action": step["action"],
                        "result": step["result"],
                        "ts": step["ts"],
                    })
                    yield f"data: {data}\n\n"

            if task["status"] in ("completed", "failed", "cancelled"):
                yield 'data: {"event":"stream_end","status":"' + task["status"] + '"}\n\n'
                return

            await asyncio.sleep(0.5)
            elapsed += 0.5

        yield 'data: {"event":"timeout"}\n\n'

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

async def http_skills(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    skills = load_skills()
    return JSONResponse({name: {"description": s["description"]} for name, s in skills.items()})

async def http_agents(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    return JSONResponse(agent_cortex_manager.list_agents())

async def http_agent_detail(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    agent_id = request.path_params.get("agent_id", "")
    stats = agent_cortex_manager.get_stats(agent_id)
    if "error" in stats:
        return JSONResponse(stats, status_code=404)
    return JSONResponse(stats)

async def http_agent_delete_memory(request: StarletteRequest):
    err = auth_required(request)
    if err:
        return err
    agent_id = request.path_params.get("agent_id", "")
    confirm = request.query_params.get("confirm", "false").lower() == "true"
    if not confirm:
        return JSONResponse({"error": "Add ?confirm=true to delete agent memory"}, status_code=400)
    agent_cortex_manager.release(agent_id)
    db_path = CORTEX_DIR / f"agent-{agent_id}.db"
    if not db_path.exists():
        return JSONResponse({"error": f"No memory db for '{agent_id}'"}, status_code=404)
    db_path.unlink()
    return JSONResponse({"status": "deleted", "agent_id": agent_id})

# ─── Webhook endpoint ─────────────────────────────────
async def webhook_handler(request: StarletteRequest):
    webhook_id = request.path_params.get("webhook_id", "")
    db = get_db()
    webhook = db.execute("SELECT * FROM webhooks WHERE id=?", (webhook_id,)).fetchone()
    if not webhook:
        db.close()
        return JSONResponse({"error": "Webhook not found"}, status_code=404)
    if request.headers.get("x-webhook-secret", "") != webhook["secret"]:
        db.close()
        return JSONResponse({"error": "Invalid secret"}, status_code=403)
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
    task_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, f"Webhook: {webhook['name']}", prompt, "pending", now, now)
    )
    db.commit()
    db.close()
    asyncio.create_task(run_task(task_id))
    return JSONResponse({"task_id": task_id, "status": "pending"})

# ─── Scheduler ───────────────────────────────────────
scheduler = AsyncIOScheduler()

def _register_cron_job(schedule_id: str, cron: str, prompt: str):
    parts = cron.strip().split()
    if len(parts) != 5:
        log.error(f"[SCHEDULE] Invalid cron: {cron}")
        return
    minute, hour, day, month, dow = parts
    scheduler.add_job(
        run_scheduled, CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
        args=[schedule_id, prompt],
        id=f"schedule_{schedule_id}", replace_existing=True
    )

def load_schedules():
    db = get_db()
    schedules = db.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
    db.close()
    for s in schedules:
        _register_cron_job(s["id"], s["cron"], s["prompt"])
    log.info(f"[SCHEDULE] Loaded {len(schedules)} schedules")

# ─── App factory ─────────────────────────────────────
def create_app():
    from starlette.routing import Route as R

    mcp_app = mcp.http_app(path="/mcp")

    routes = [
        R("/health",                     http_health,             methods=["GET"]),
        R("/run",                        http_run,                methods=["POST"]),
        R("/sessions",                   http_sessions,           methods=["GET"]),
        R("/status/{task_id}",           http_status,             methods=["GET"]),
        R("/stream/{task_id}",           http_stream,             methods=["GET"]),
        R("/skills",                     http_skills,             methods=["GET"]),
        R("/agents",                     http_agents,             methods=["GET"]),
        R("/agents/{agent_id}",          http_agent_detail,       methods=["GET"]),
        R("/agents/{agent_id}/memory",   http_agent_delete_memory, methods=["DELETE"]),
        R("/webhook/{webhook_id}",       webhook_handler,         methods=["POST"]),
    ]

    app = Starlette(routes=routes)
    app.mount("/", mcp_app)
    return app

async def startup():
    init_db()
    load_schedules()
    scheduler.add_job(
        heartbeat_check, IntervalTrigger(minutes=30),
        id="heartbeat", replace_existing=True
    )
    scheduler.start()
    log.info("[STARTUP] Scheduler started (heartbeat every 30 min)")
    await heartbeat_check()

if __name__ == "__main__":
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    app = create_app()

    @app.on_event("startup")
    async def on_startup():
        await startup()

    log.info(f"[STARTUP] OpenSquid daemon starting on port {PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
