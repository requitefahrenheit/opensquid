"""
dispatcher-server.py — FastAPI server on port 8255.

Accepts POST /run, POST /run-skill, GET /skills, GET /status/{id},
GET /sessions, GET /stream/{id}.

Auth: ?token=emc2ymmv (same as other servers)
"""

import os
import sys
import json
import uuid
import time
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from dispatcher import load_skills, run_session, make_session_id, store_in_cortex, SESSIONS_DIR, agent_cortex_manager

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("RWX_TOKEN", "emc2ymmv")
CORTEX_DB = os.path.expanduser("~/cortex/cortex.db")
PORT = int(os.environ.get("DISPATCHER_PORT", "8255"))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    goal: str
    skills: List[str] = []
    arch: str = "claude"
    max_turns: int = 10
    budget: float = 5.0
    stream: bool = False
    agent_id: Optional[str] = None


class RunSkillRequest(BaseModel):
    skill_name: str
    input: str
    context: str = ""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_token(token: str = Query(default="")):
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


# ---------------------------------------------------------------------------
# Background runner + Cortex
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()
_active: dict = {}  # session_id -> thread


# Cortex store: delegate to dispatcher.store_in_cortex()


def _run_bg(session_id: str, goal: str, skills: dict, arch: str, max_turns: int, budget: float,
            agent_id: str = None):
    """Background thread: run session then store to Cortex."""
    try:
        result = run_session(goal, skills, arch, max_turns, budget, session_id, agent_id=agent_id)
        store_in_cortex(result)
    except Exception as e:
        print(f"[BG] Session {session_id} failed: {e}")
        # Write error status
        status_file = SESSIONS_DIR / session_id / "status.json"
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                status["status"] = "error"
                status["error"] = str(e)
                status_file.write_text(json.dumps(status, indent=2))
            except Exception:
                pass
    finally:
        with _sessions_lock:
            _active.pop(session_id, None)


def _spawn(session_id: str, goal: str, skills: dict, arch: str, max_turns: int, budget: float,
           agent_id: str = None):
    t = threading.Thread(
        target=_run_bg,
        args=(session_id, goal, skills, arch, max_turns, budget),
        kwargs={"agent_id": agent_id},
        daemon=True,
    )
    with _sessions_lock:
        _active[session_id] = t
    t.start()
    return t


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Dispatcher", version="1.0")


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(), "port": PORT}


@app.get("/skills")
def list_skills(token: str = Depends(check_token)):
    skills = load_skills()
    return {name: {"description": s["description"]} for name, s in skills.items()}


@app.post("/run")
def run(req: RunRequest, token: str = Depends(check_token)):
    all_skills = load_skills()
    selected = {k: v for k, v in all_skills.items() if k in req.skills} if req.skills else {}

    session_id = make_session_id(req.arch)
    _spawn(session_id, req.goal, selected, req.arch, req.max_turns, req.budget,
           agent_id=req.agent_id)

    return {
        "session_id": session_id,
        "status": "started",
        "skills": list(selected.keys()),
        "agent_id": req.agent_id,
        "stream_url": f"/stream/{session_id}?token={TOKEN}",
        "status_url": f"/status/{session_id}?token={TOKEN}",
    }


@app.post("/run-skill")
def run_skill(req: RunSkillRequest, token: str = Depends(check_token)):
    all_skills = load_skills()
    if req.skill_name not in all_skills:
        raise HTTPException(status_code=404, detail=f"Skill '{req.skill_name}' not found")

    skill = {req.skill_name: all_skills[req.skill_name]}
    goal = req.input
    if req.context:
        goal = f"{req.input}\n\nContext:\n{req.context}"

    session_id = make_session_id("claude")
    _spawn(session_id, goal, skill, "claude", 10, 5.0)

    return {
        "session_id": session_id,
        "skill": req.skill_name,
        "status": "started",
        "stream_url": f"/stream/{session_id}?token={TOKEN}",
        "status_url": f"/status/{session_id}?token={TOKEN}",
    }


@app.get("/status/{session_id}")
def get_status(session_id: str, token: str = Depends(check_token)):
    status_file = SESSIONS_DIR / session_id / "status.json"
    if not status_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return json.loads(status_file.read_text())


@app.get("/sessions")
def list_sessions(token: str = Depends(check_token)):
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for d in sorted(SESSIONS_DIR.iterdir(), key=lambda p: p.name, reverse=True)[:20]:
        sf = d / "status.json"
        if sf.exists():
            try:
                sessions.append(json.loads(sf.read_text()))
            except Exception:
                pass
    return sessions


@app.get("/stream/{session_id}")
def stream_session(session_id: str, token: str = Depends(check_token)):
    """SSE stream: tail session output.log until session completes."""
    log_path = SESSIONS_DIR / session_id / "output.log"
    status_file = SESSIONS_DIR / session_id / "status.json"

    def event_gen():
        # Wait up to 8s for the log to appear
        waited = 0.0
        while not log_path.exists() and waited < 8.0:
            time.sleep(0.25)
            waited += 0.25

        if not log_path.exists():
            yield "data: [Session log not found]\n\n"
            return

        position = 0
        while True:
            with open(log_path, "r", errors="replace") as f:
                f.seek(position)
                chunk = f.read()
            if chunk:
                for line in chunk.splitlines():
                    yield f"data: {line}\n\n"
                position += len(chunk.encode("utf-8"))

            # Check if done
            if status_file.exists():
                try:
                    st = json.loads(status_file.read_text())
                    if st.get("status") not in ("running",):
                        # Drain remainder
                        with open(log_path, "r", errors="replace") as f:
                            f.seek(position)
                            for line in f.read().splitlines():
                                yield f"data: {line}\n\n"
                        yield "data: [STREAM_END]\n\n"
                        return
                except Exception:
                    pass

            time.sleep(0.5)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Agent Cortex endpoints
# ---------------------------------------------------------------------------

@app.get("/agents")
def list_agents(token: str = Depends(check_token)):
    """List all known agents and their Cortex stats."""
    return agent_cortex_manager.list_agents()


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str, token: str = Depends(check_token)):
    """Get detailed Cortex stats for a specific agent."""
    stats = agent_cortex_manager.get_stats(agent_id)
    if "error" in stats:
        raise HTTPException(status_code=404, detail=stats["error"])
    return stats


@app.delete("/agents/{agent_id}/memory")
def delete_agent_memory(
    agent_id: str,
    confirm: bool = Query(default=False),
    token: str = Depends(check_token),
):
    """
    Delete an agent's persistent Cortex db. IRREVERSIBLE.
    Requires ?confirm=true.
    """
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="This will permanently delete the agent's memory. Add ?confirm=true to proceed.",
        )

    # Kill any running process for this agent first
    agent_cortex_manager.release(agent_id)

    db_path = Path.home() / "cortex" / f"agent-{agent_id}.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"No memory db found for agent '{agent_id}'")

    db_path.unlink()
    return {"status": "deleted", "agent_id": agent_id, "db_path": str(db_path)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[DISPATCHER] Starting on port {PORT}")
    print(f"[DISPATCHER] Sessions dir: {SESSIONS_DIR}")
    print(f"[DISPATCHER] Skills dir: {Path.home() / 'claude' / 'skills'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
