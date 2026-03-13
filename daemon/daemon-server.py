#!/usr/bin/env python3
"""
Daemon Server — Agentic Loop + Task Management
================================================
FastMCP server with Claude Opus agentic loop, heartbeat scheduler,
task/schedule/webhook management.

Run:  python3 daemon-server.py
Port: 8254
"""

import os, json, uuid, logging, asyncio, sqlite3, datetime, secrets, socket, subprocess
import threading, time, urllib.request
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
DB_PATH = os.environ.get("DAEMON_DB", os.path.expanduser("~/claude/opensquid/daemon/daemon.db"))
SOUL_PATH = os.path.expanduser("~/claude/opensquid/SOUL.md")
HEARTBEAT_PATH = os.path.expanduser("~/claude/opensquid/HEARTBEAT.md")
HEARTBEAT_LOG = os.path.expanduser("~/claude/opensquid/daemon/heartbeat.log")
AUTH_TOKEN = os.environ.get("DAEMON_AUTH_TOKEN", "emc2ymmv")
MAX_AGENT_STEPS = 20
CLAUDE_MODEL = "claude-opus-4-20250514"

SKILLS_DIR = os.path.expanduser("~/claude/opensquid/skills")

# Upstream MCP endpoints
CORTEX_URL = "https://autonomous.fahrenheitrequited.dev"
OPENMIND_URL = "https://openmind.fahrenheitrequited.dev"
RWX_URL = "https://rwx.fahrenheitrequited.dev"

log = logging.getLogger("daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ─── AgentCortexManager (inlined from agent_cortex.py) ───────────────────────

_CORTEX_DIR = Path.home() / "cortex"
_DUAL_SERVER = Path.home() / "claude" / "mcp-server" / "dual-server.py"
_PYTHON = Path.home() / "miniconda3" / "bin" / "python3"
_CORTEX_PORT_RANGE = range(8300, 8400)
_CORTEX_HEALTH_TIMEOUT = 180.0  # seconds; ML model loading can take 2+ minutes


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class AgentCortexManager:
    """
    Manages private Cortex instances for named agents.

    Thread-safe. One global instance is shared across all sessions.
    Ports are allocated from the range 8300-8399.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running: dict = {}  # agent_id -> {port, pid, process, db_path, url}

    def _find_free_port(self) -> int:
        """Find a free port in 8300-8399."""
        with self._lock:
            used = {info["port"] for info in self._running.values()}
        for port in _CORTEX_PORT_RANGE:
            if port not in used and _is_port_free(port):
                return port
        raise RuntimeError("No free port available in range 8300-8399")

    def _wait_healthy(self, port: int, timeout: float = _CORTEX_HEALTH_TIMEOUT) -> bool:
        """Poll /api/stats until 200 OK or timeout."""
        url = f"http://localhost:{port}/api/stats"
        deadline = time.time() + timeout
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
        """
        Provision a private Cortex for agent_id.
        Returns: {agent_id, db_path, port, url, pid}

        If an instance is already running for this agent_id, returns it.
        Otherwise: find free port, launch dual-server.py, wait for health check.
        """
        with self._lock:
            if agent_id in self._running:
                info = self._running[agent_id]
                proc = info["process"]
                if proc.poll() is None:
                    # Still alive — return existing info
                    return {k: v for k, v in info.items() if k != "process"}
                else:
                    # Process died; clean up and re-provision
                    del self._running[agent_id]

        # Find port and launch (outside lock to avoid blocking)
        _CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(_CORTEX_DIR / f"agent-{agent_id}.db")
        port = self._find_free_port()

        env = os.environ.copy()
        env["CORTEX_DB"] = db_path
        env["CORTEX_PORT"] = str(port)

        proc = subprocess.Popen(
            [str(_PYTHON), str(_DUAL_SERVER)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        log.info(f"[AGENT-CORTEX] Launched for '{agent_id}' on port {port} (pid={proc.pid})")

        healthy = self._wait_healthy(port)
        if not healthy:
            proc.kill()
            raise RuntimeError(
                f"Agent Cortex for '{agent_id}' failed to start on port {port} "
                f"within {_CORTEX_HEALTH_TIMEOUT}s"
            )

        log.info(f"[AGENT-CORTEX] '{agent_id}' is ready on port {port}")

        info = {
            "agent_id": agent_id,
            "db_path": db_path,
            "port": port,
            "url": f"http://localhost:{port}",
            "pid": proc.pid,
            "process": proc,
        }
        with self._lock:
            self._running[agent_id] = info

        return {k: v for k, v in info.items() if k != "process"}

    def release(self, agent_id: str):
        """
        Kill the Cortex process for agent_id.
        Does NOT delete the db — it persists for future sessions.
        """
        with self._lock:
            info = self._running.pop(agent_id, None)
        if not info:
            return
        proc = info["process"]
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.info(f"[AGENT-CORTEX] Released '{agent_id}' (port {info['port']})")

    def list_agents(self) -> list:
        """
        List all known agents (those with db files in ~/cortex/agent-*.db).
        Includes: agent_id, db_size_bytes, entry_count, is_running, port.
        """
        if not _CORTEX_DIR.exists():
            return []

        with self._lock:
            running_ports = {aid: info["port"] for aid, info in self._running.items()}

        agents = []
        for db_file in sorted(_CORTEX_DIR.glob("agent-*.db")):
            stem = db_file.stem  # e.g. "agent-overnight-builder"
            agent_id = stem[len("agent-"):]
            db_size = db_file.stat().st_size

            entry_count = 0
            try:
                conn = sqlite3.connect(str(db_file))
                entry_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                conn.close()
            except Exception:
                pass

            agents.append({
                "agent_id": agent_id,
                "db_size_bytes": db_size,
                "entry_count": entry_count,
                "is_running": agent_id in running_ports,
                "port": running_ports.get(agent_id),
            })

        return agents

    def get_stats(self, agent_id: str) -> dict:
        """
        Return stats for a specific agent's Cortex.
        Queries the SQLite db directly (no server required).
        """
        db_path = _CORTEX_DIR / f"agent-{agent_id}.db"
        if not db_path.exists():
            return {"error": f"No Cortex db found for agent '{agent_id}'"}

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            db_size = db_path.stat().st_size

            if total == 0:
                conn.close()
                return {
                    "agent_id": agent_id,
                    "entry_count": 0,
                    "db_size_bytes": db_size,
                    "oldest_entry": None,
                    "newest_entry": None,
                    "top_tags": [],
                }

            rows = conn.execute(
                "SELECT content, tags, timestamp FROM entries ORDER BY timestamp"
            ).fetchall()
            oldest = rows[0]["timestamp"]
            newest = rows[-1]["timestamp"]

            tag_counts: dict = {}
            for r in rows:
                for t in json.loads(r["tags"]):
                    tag_counts[t] = tag_counts.get(t, 0) + 1
            top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:10]
            conn.close()

            return {
                "agent_id": agent_id,
                "entry_count": total,
                "db_size_bytes": db_size,
                "oldest_entry": oldest,
                "newest_entry": newest,
                "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
            }
        except Exception as e:
            return {"error": str(e), "agent_id": agent_id}


# Global agent cortex manager — one instance shared across all tasks
agent_cortex_manager = AgentCortexManager()

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

# ─── Skill loading ───────────────────────────────────
def load_skill(skill_name: str) -> Optional[str]:
    """
    Load skill content from ~/claude/opensquid/skills/{skill}.md
    or ~/claude/opensquid/skills/{skill}/SKILL.md.
    Returns the file content, or None if not found.
    """
    skills_dir = Path(SKILLS_DIR)

    # Try flat file first: skills/{skill}.md
    flat_path = skills_dir / f"{skill_name}.md"
    if flat_path.exists():
        try:
            return flat_path.read_text()
        except Exception as e:
            log.warning(f"[SKILL] Failed to read {flat_path}: {e}")

    # Try subdirectory: skills/{skill}/SKILL.md
    dir_path = skills_dir / skill_name / "SKILL.md"
    if dir_path.exists():
        try:
            return dir_path.read_text()
        except Exception as e:
            log.warning(f"[SKILL] Failed to read {dir_path}: {e}")

    return None


def build_system_prompt_with_skill(skill: Optional[str], base_system_prompt: str) -> str:
    """
    If skill is provided, prepend the skill content to the system prompt.
    Returns the (possibly modified) system prompt.
    """
    if not skill:
        return base_system_prompt

    skill_content = load_skill(skill)
    if skill_content is None:
        log.warning(f"[SKILL] Skill '{skill}' not found in {SKILLS_DIR}")
        return base_system_prompt

    return f"# Skill: {skill}\n\n{skill_content}\n\n---\n\n{base_system_prompt}"


# ─── claude-code backend ─────────────────────────────
def run_session_claude_code(
    task_id: str,
    prompt: str,
    mcp_servers: Optional[dict] = None,
    system_prompt: Optional[str] = None,
) -> tuple:
    """
    Run a task using the claude CLI (claude-code backend).
    mcp_servers: dict of {name: {url: ..., type: "http"}}
    Returns: (stdout, stderr, returncode)
    """
    cli = "/home/jfischer/.npm-global/bin/claude"
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # allow nested sessions
    if "/usr/local/bin" not in env.get("PATH", ""):
        env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")

    # Ensure node is on PATH (claude CLI requires it)
    node_paths = ["/usr/local/bin", "/usr/bin", "/home/jfischer/.npm-global/bin"]
    existing_path = env.get("PATH", "")
    extra = ":".join(p for p in node_paths if p not in existing_path)
    if extra:
        env["PATH"] = extra + ":" + existing_path

    # Build combined prompt — prepend system_prompt if provided
    full_prompt = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt

    mcp_config_path = None
    try:
        cmd = [cli, "--dangerously-skip-permissions", "--max-budget-usd", "5.0"]

        if mcp_servers:
            # IMPORTANT: type must be "http" not "sse" (dual-server uses streamable HTTP)
            mcp_config_path = f"/tmp/daemon-mcp-{task_id}.json"
            mcp_config = {"mcpServers": {}}
            for name, cfg in mcp_servers.items():
                mcp_config["mcpServers"][name] = {
                    "type": "http",
                    "url": cfg["url"]
                }
            with open(mcp_config_path, "w") as f:
                json.dump(mcp_config, f)
            cmd += ["--mcp-config", mcp_config_path]

        cmd += ["-p", full_prompt]

        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
        return result.stdout, result.stderr, result.returncode
    finally:
        if mcp_config_path:
            try:
                os.unlink(mcp_config_path)
            except Exception:
                pass


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

async def call_mcp_tool(tool_name: str, arguments: dict, extra_endpoints: Optional[dict] = None) -> str:
    """
    Dispatch a tool call to the appropriate upstream MCP server.
    extra_endpoints: optional dict of {tool_prefix: base_url} for dynamically added tools.
    """
    client = await get_http_client()

    # Route to correct upstream
    url = None
    method = tool_name

    # Check extra (dynamic) endpoints first (e.g. isolated cortex tools)
    if extra_endpoints:
        for prefix, base_url in extra_endpoints.items():
            if tool_name.startswith(prefix):
                url = f"{base_url}/mcp"
                method = tool_name
                break

    if url is None:
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
async def run_agent_loop(
    task_id: str,
    prompt: str,
    backend: str = "api",
    skill: Optional[str] = None,
    isolated_cortex: bool = False,
) -> str:
    """
    Execute the agentic loop: Claude + tools until done or max steps.

    backend: 'api' (default Anthropic API loop) or 'claude-code' (CLI subprocess)
    skill: optional skill name to prepend to system prompt
    isolated_cortex: if True, provision a private per-task Cortex instance
    """
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (now, task_id))
    db.commit()

    # Load SOUL.md as base system prompt
    soul = ""
    try:
        soul = Path(SOUL_PATH).read_text()
    except Exception:
        soul = "You are J's personal AI infrastructure."

    # Apply skill overlay if requested
    system_prompt = build_system_prompt_with_skill(skill, soul)

    # Provision isolated cortex if requested
    cortex_info = None
    cortex_agent_id = f"task-{task_id}"
    if isolated_cortex:
        try:
            cortex_info = agent_cortex_manager.provision(cortex_agent_id)
            log.info(f"[AGENT] Isolated cortex provisioned for task {task_id} on port {cortex_info['port']}")
        except Exception as e:
            log.error(f"[AGENT] Failed to provision isolated cortex for task {task_id}: {e}")
            cortex_info = None

    final_result = ""

    try:
        # ── claude-code backend ───────────────────────────────────────────────
        if backend == "claude-code":
            mcp_servers = None
            if cortex_info:
                mcp_servers = {
                    "task-cortex": {
                        "type": "http",
                        "url": f"{cortex_info['url']}/mcp",
                    }
                }

            log.info(f"[AGENT] Task {task_id} using claude-code backend")
            try:
                stdout, stderr, returncode = run_session_claude_code(
                    task_id=task_id,
                    prompt=prompt,
                    mcp_servers=mcp_servers,
                    system_prompt=system_prompt if system_prompt != soul else None,
                )
                if stderr:
                    log.warning(f"[AGENT] Task {task_id} claude-code stderr: {stderr[:2000]}")

                final_result = stdout.strip() if stdout else f"[returncode={returncode}]"

                # Log a single step for the claude-code result
                db.execute(
                    "INSERT INTO task_steps (id, task_id, step_num, action, result, ts) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), task_id, 1, "claude-code", final_result[:2000], datetime.datetime.utcnow().isoformat())
                )
                db.commit()

                if returncode == 0:
                    now = datetime.datetime.utcnow().isoformat()
                    db.execute(
                        "UPDATE tasks SET status='completed', result=?, updated_at=? WHERE id=?",
                        (final_result[:10000], now, task_id)
                    )
                else:
                    now = datetime.datetime.utcnow().isoformat()
                    db.execute(
                        "UPDATE tasks SET status='failed', result=?, updated_at=? WHERE id=?",
                        (final_result[:10000], now, task_id)
                    )
                db.commit()

            except Exception as e:
                log.error(f"[AGENT] Task {task_id} claude-code backend failed: {e}")
                now = datetime.datetime.utcnow().isoformat()
                db.execute(
                    "UPDATE tasks SET status='failed', result=?, updated_at=? WHERE id=?",
                    (str(e)[:5000], now, task_id)
                )
                db.commit()
                final_result = f"Error: {e}"

        # ── API backend (default) ─────────────────────────────────────────────
        else:
            # Build tool list — add isolated cortex tools if provisioned
            tools = list(AGENT_TOOLS)
            extra_endpoints: Optional[dict] = None

            if cortex_info:
                # Add cortex tools pointing at the isolated instance
                isolated_cortex_tools = [
                    {
                        "name": "task_cortex_store",
                        "description": "Store an entry in this task's isolated Cortex memory.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Text content to store"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                                "source": {"type": "string", "description": "Source context"}
                            },
                            "required": ["content"]
                        }
                    },
                    {
                        "name": "task_cortex_search",
                        "description": "Full-text search in this task's isolated Cortex memory.",
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
                        "name": "task_cortex_semantic_search",
                        "description": "Semantic search in this task's isolated Cortex memory.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Natural language query"},
                                "limit": {"type": "integer", "description": "Max results", "default": 5}
                            },
                            "required": ["query"]
                        }
                    },
                ]
                tools = tools + isolated_cortex_tools
                extra_endpoints = {"task_cortex_": cortex_info["url"]}

            client = anthropic.Anthropic()
            messages = [{"role": "user", "content": prompt}]
            step_num = 0

            try:
                while step_num < MAX_AGENT_STEPS:
                    step_num += 1
                    log.info(f"[AGENT] Task {task_id} step {step_num}")

                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=4096,
                        system=system_prompt,
                        tools=tools,
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
                                result = await call_mcp_tool(tool_name, tool_input, extra_endpoints=extra_endpoints)
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
        # Release isolated cortex if it was provisioned
        if isolated_cortex and cortex_info:
            try:
                agent_cortex_manager.release(cortex_agent_id)
                log.info(f"[AGENT] Isolated cortex released for task {task_id}")
            except Exception as e:
                log.error(f"[AGENT] Failed to release isolated cortex for task {task_id}: {e}")

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
async def daemon_task_create(
    title: str,
    prompt: str,
    backend: str = "api",
    skill: Optional[str] = None,
    isolated_cortex: bool = False,
) -> str:
    """
    Create a new daemon task and start the agentic loop.

    Args:
        title: Short descriptive title for the task.
        prompt: The task prompt / goal to execute.
        backend: Execution backend — 'api' (default Anthropic API loop) or
                 'claude-code' (claude CLI subprocess).
        skill: Optional skill name. If provided, loads
               ~/claude/opensquid/skills/{skill}.md or
               ~/claude/opensquid/skills/{skill}/SKILL.md and prepends it
               to the system prompt.
        isolated_cortex: If True, provisions a private per-task Cortex
                         instance (agent-{task_id}.db on a port in 8300-8399)
                         and exposes it as an MCP tool. The Cortex is released
                         after the task completes.
    """
    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (task_id, title, prompt, "pending", now, now)
    )
    db.commit()
    db.close()
    log.info(f"[TASK] Created {task_id}: {title} (backend={backend}, skill={skill}, isolated_cortex={isolated_cortex})")

    # Fire and forget the agent loop
    asyncio.create_task(run_agent_loop(
        task_id,
        prompt,
        backend=backend,
        skill=skill,
        isolated_cortex=isolated_cortex,
    ))

    return json.dumps({
        "task_id": task_id,
        "status": "pending",
        "title": title,
        "backend": backend,
        "skill": skill,
        "isolated_cortex": isolated_cortex,
    })

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

# ─── HTTP endpoints ─────────────────────────────────
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse

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

# ─── SSE streaming endpoint ───────────────────────────
async def stream_task_handler(request: StarletteRequest):
    """SSE stream for task steps. GET /stream/{task_id}"""
    task_id = request.path_params.get("task_id", "")

    # Auth check (Bearer emc2ymmv)
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async def event_generator():
        last_step_id_val = None  # track by rowid since step ids are UUIDs

        # We track the rowid of the last seen task_step row
        last_rowid = 0
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            # Get new steps from DB using rowid for ordering
            db = get_db()
            rows = db.execute(
                "SELECT rowid, id, step_num, action, result, ts FROM task_steps "
                "WHERE task_id=? AND rowid>? ORDER BY rowid",
                (task_id, last_rowid)
            ).fetchall()
            db.close()

            for row in rows:
                last_rowid = row["rowid"]
                data = json.dumps({
                    "id": row["id"],
                    "step_num": row["step_num"],
                    "action": row["action"],
                    "content": row["result"],
                    "created_at": row["ts"],
                })
                yield f"data: {data}\n\n"

            # Check if task is done
            db = get_db()
            task = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            db.close()

            if task and task["status"] in ("completed", "failed", "cancelled"):
                yield f"data: {json.dumps({'event': 'done', 'status': task['status']})}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



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

# ─── REST task API endpoints ─────────────────────────
# POST /tasks  — create & enqueue a task
# POST /run    — alias for POST /tasks (OpenSquid compat)
# GET  /tasks  — list tasks
# GET  /sessions — alias for GET /tasks
# GET  /tasks/{id}  — get task + steps
# GET  /status/{id} — alias for GET /tasks/{id}
# DELETE /tasks/{id} — cancel a task

def _check_bearer(request: StarletteRequest):
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {AUTH_TOKEN}"

async def http_task_create(request: StarletteRequest):
    """POST /tasks or POST /run — create and enqueue a task."""
    if not _check_bearer(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Accept 'goal' (dispatcher compat) as alias for 'prompt'
    prompt = body.get("prompt") or body.get("goal", "")
    if not prompt:
        return JSONResponse({"error": "prompt or goal is required"}, status_code=400)
    title = body.get("title") or prompt[:80]

    # Map dispatcher 'arch' to daemon 'backend'
    arch = body.get("arch", "")
    backend = body.get("backend", "claude-code" if arch == "claude-code" else "api")

    # Map 'skills' list to first skill name
    skills_list = body.get("skills", [])
    skill = body.get("skill") or body.get("skill_name") or (skills_list[0] if skills_list else None)

    # Map 'agent_id' to isolated_cortex
    agent_id = body.get("agent_id")
    isolated_cortex = bool(body.get("isolated_cortex") or body.get("isolated_memory") or agent_id)

    result = await daemon_task_create(
        title=title, prompt=prompt,
        backend=backend, skill=skill, isolated_cortex=isolated_cortex
    )
    data = json.loads(result)
    data["session_id"] = data.get("task_id")  # OpenSquid compat alias
    return JSONResponse(data, status_code=201)

async def http_task_list(request: StarletteRequest):
    """GET /tasks or GET /sessions — list tasks."""
    if not _check_bearer(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = int(request.query_params.get("limit", 20))
    result = await daemon_task_list(limit=limit)
    return JSONResponse(json.loads(result))

async def http_task_get(request: StarletteRequest):
    """GET /tasks/{id} or GET /status/{id} — get task + steps."""
    if not _check_bearer(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    result = await daemon_task_status(task_id)
    data = json.loads(result)
    if "error" in data:
        return JSONResponse(data, status_code=404)
    return JSONResponse(data)

async def http_task_delete(request: StarletteRequest):
    """DELETE /tasks/{id} — cancel a task."""
    if not _check_bearer(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    result = await daemon_task_cancel(task_id)
    data = json.loads(result)
    if "error" in data:
        return JSONResponse(data, status_code=404)
    return JSONResponse(data)

# ─── App startup ─────────────────────────────────────
def create_app():
    """Create the ASGI app with MCP + HTTP REST + webhook + SSE routes."""
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount

    # Get the MCP ASGI app
    mcp_app = mcp.http_app(path="/mcp")

    routes = [
        # Task REST API
        Route("/tasks",           http_task_create,  methods=["POST"]),
        Route("/tasks",           http_task_list,    methods=["GET"]),
        Route("/tasks/{task_id}", http_task_get,     methods=["GET"]),
        Route("/tasks/{task_id}", http_task_delete,  methods=["DELETE"]),
        # OpenSquid compat aliases
        Route("/run",             http_task_create,  methods=["POST"]),
        Route("/sessions",        http_task_list,    methods=["GET"]),
        Route("/status/{task_id}",http_task_get,     methods=["GET"]),
        # Webhook + SSE
        Route("/webhook/{webhook_id}", webhook_handler,   methods=["POST"]),
        Route("/stream/{task_id}",     stream_task_handler, methods=["GET"]),
    ]

    app = Starlette(routes=routes)
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
