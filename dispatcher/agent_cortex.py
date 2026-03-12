"""
agent_cortex.py — Manages per-agent private Cortex instances.

Each named agent (e.g. 'overnight-builder') gets:
- A persistent SQLite db at ~/cortex/agent-{agent_id}.db
- A Cortex MCP server process on a dynamically assigned port (8300-8399)
- The process lives for the session duration, then is killed
- The db persists forever (agent memory across runs)
"""

import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

CORTEX_DIR = Path.home() / "cortex"
DUAL_SERVER = Path.home() / "claude" / "mcp-server" / "dual-server.py"
PYTHON = Path.home() / "miniconda3" / "bin" / "python3"
PORT_RANGE = range(8300, 8400)
HEALTH_TIMEOUT = 180.0  # seconds; ML model loading can take 2+ minutes


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
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running: dict = {}  # agent_id -> {port, pid, process, db_path, url}

    def _find_free_port(self) -> int:
        """Find a free port in 8300-8399."""
        # Collect ports already in use by our own processes
        with self._lock:
            used = {info["port"] for info in self._running.values()}
        for port in PORT_RANGE:
            if port not in used and _is_port_free(port):
                return port
        raise RuntimeError("No free port available in range 8300-8399")

    def _wait_healthy(self, port: int, timeout: float = HEALTH_TIMEOUT) -> bool:
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
        CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(CORTEX_DIR / f"agent-{agent_id}.db")
        port = self._find_free_port()

        env = os.environ.copy()
        env["CORTEX_DB"] = db_path
        env["CORTEX_PORT"] = str(port)

        proc = subprocess.Popen(
            [str(PYTHON), str(DUAL_SERVER)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        print(f"[AGENT-CORTEX] Launched for '{agent_id}' on port {port} (pid={proc.pid})")

        healthy = self._wait_healthy(port)
        if not healthy:
            proc.kill()
            raise RuntimeError(
                f"Agent Cortex for '{agent_id}' failed to start on port {port} "
                f"within {HEALTH_TIMEOUT}s"
            )

        print(f"[AGENT-CORTEX] '{agent_id}' is ready on port {port}")

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
        print(f"[AGENT-CORTEX] Released '{agent_id}' (port {info['port']})")

    def list_agents(self) -> list:
        """
        List all known agents (those with db files in ~/cortex/agent-*.db).
        Includes: agent_id, db_size_bytes, entry_count, is_running, port.
        """
        if not CORTEX_DIR.exists():
            return []

        with self._lock:
            running_ports = {aid: info["port"] for aid, info in self._running.items()}

        agents = []
        for db_file in sorted(CORTEX_DIR.glob("agent-*.db")):
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
        db_path = CORTEX_DIR / f"agent-{agent_id}.db"
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
