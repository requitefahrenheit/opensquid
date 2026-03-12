"""
dispatcher.py — Core dispatcher logic.

Loads skills from ~/claude/skills/, builds system prompts, runs agent sessions.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path
from datetime import datetime

CLAUDE_BIN = Path.home() / ".npm-global" / "bin" / "claude"

from agent_cortex import AgentCortexManager

SKILLS_DIR = Path.home() / "claude" / "skills"
SESSIONS_DIR = Path.home() / "claude" / "dispatcher" / "sessions"
CORTEX_DB = Path.home() / "cortex" / "cortex.db"

# Global agent cortex manager — one instance shared across all sessions
agent_cortex_manager = AgentCortexManager()

WORKER_SYSTEM = """\
You are a general-purpose autonomous agent. You receive a goal and work toward completing it.

RULES:
- Never end a turn with a question. Always commit to your best action and execute it.
- Each turn, make meaningful progress toward the goal.
- Write your work out loud — internal reasoning is not recorded.
- When the goal is fully addressed, end your message with [DONE].
- Do NOT say [DONE] prematurely. Only say it when the goal is genuinely complete.
{cortex_block}{skills_block}"""

_CORTEX_MEMORY_TEMPLATE = """\

=== YOUR PRIVATE MEMORY (Cortex) ===
You have a persistent memory store that survives across sessions.
Agent ID: {agent_id}
Base URL: http://localhost:{port}

REST API (read-only, no auth required):
  GET /api/search?q=QUERY&limit=10          — full-text keyword search
  GET /api/semantic?q=QUERY&limit=5&threshold=0.3  — semantic/meaning search
  GET /api/list?limit=20&offset=0           — list recent entries
  GET /api/stats                            — storage statistics

MCP endpoint: http://localhost:{port}/mcp
MCP tools: cortex_store, cortex_search, cortex_semantic_search, cortex_list,
           cortex_get, cortex_update, cortex_delete, cortex_stats
NOTE: Writing/storing entries requires the MCP endpoint.

USE YOUR MEMORY:
- On start: search for what you've done before on this goal/topic
- During work: store key decisions, findings, and artifacts
- On completion: store a summary of what you accomplished

This memory persists. Future sessions with the same agent_id will
read what you store now.
=== END MEMORY ===
"""


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------

def load_skills(skills_dir: Path = SKILLS_DIR) -> dict:
    """Read all SKILL.md files, parse YAML frontmatter + body."""
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


def _parse_yaml_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter, falling back to line-by-line if pyyaml unavailable."""
    try:
        import yaml
        result = yaml.safe_load(text)
        return result if isinstance(result, dict) else {}
    except Exception:
        pass

    # Minimal fallback: key: value lines
    result = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt(skills: dict, cortex_info: dict = None) -> str:
    """Inject selected skills and optional cortex block into system prompt."""
    cortex_block = ""
    if cortex_info:
        cortex_block = _CORTEX_MEMORY_TEMPLATE.format(
            agent_id=cortex_info["agent_id"],
            port=cortex_info["port"],
        )

    if not skills:
        return (
            WORKER_SYSTEM
            .replace("{cortex_block}", cortex_block)
            .replace("{skills_block}", "")
        )

    lines = ["<available_skills>"]
    for name, skill in skills.items():
        desc = skill.get("description", "")
        instructions = skill.get("instructions", "")
        lines.append(f'  <skill name="{name}">')
        lines.append(f"    <description>{desc}</description>")
        lines.append(f"    <instructions>")
        for instruction_line in instructions.splitlines():
            lines.append(f"    {instruction_line}")
        lines.append(f"    </instructions>")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    skills_block = "\n\n" + "\n".join(lines) + "\n"
    return (
        WORKER_SYSTEM
        .replace("{cortex_block}", cortex_block)
        .replace("{skills_block}", skills_block)
    )


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------

def _write_status(session_dir: Path, status: dict):
    with open(session_dir / "status.json", "w") as f:
        json.dump(status, f, indent=2)


def make_session_id(arch: str = "claude") -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:6]
    return f"{arch}-{ts}-{uid}"


def _build_claude_code_prompt(goal: str, cortex_info: dict, skills: dict, agent_id: str) -> str:
    """Build the combined -p prompt for claude CLI sessions."""
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
            "USE YOUR MEMORY:",
            "- On start: call my-cortex__cortex_search or my-cortex__cortex_semantic_search",
            "  to recall what you've done before on this topic",
            "- During work: store key decisions, findings, and artifacts",
            "- On completion: store a summary of what you accomplished",
            "",
            "This memory persists. Future sessions with the same agent_id will read what you store now.",
            "=== END MEMORY ===",
        ]

    if skills:
        lines.append("")
        lines.append("<available_skills>")
        for name, skill in skills.items():
            desc = skill.get("description", "")
            instructions = skill.get("instructions", "")
            lines.append(f'  <skill name="{name}">')
            lines.append(f"    <description>{desc}</description>")
            lines.append("    <instructions>")
            for instruction_line in instructions.splitlines():
                lines.append(f"    {instruction_line}")
            lines.append("    </instructions>")
            lines.append("  </skill>")
        lines.append("</available_skills>")

    lines += ["", "=== GOAL ===", goal, "=== END GOAL ==="]
    return "\n".join(lines)


def run_session_claude_code(
    goal: str,
    cortex_info: dict,
    skills: dict,
    session_dir: Path,
    session_id: str,
    max_turns: int = 10,
    budget: float = 5.0,
    agent_id: str = None,
) -> dict:
    """Run a session using the claude CLI with optional MCP Cortex support."""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "goal.txt").write_text(goal)

    log_path = session_dir / "output.log"

    def log(msg: str):
        with open(log_path, "a") as f:
            f.write(msg + "\n")

    log(f"=== DISPATCHER SESSION {session_id} (claude-code) ===")
    log(f"Goal: {goal}")
    log(f"Budget: ${budget}")
    if agent_id:
        log(f"Agent: {agent_id} | Cortex port: {cortex_info['port'] if cortex_info else 'N/A'}")
    if skills:
        log(f"Skills: {', '.join(skills.keys())}")
    log("=" * 60)

    status = {
        "session_id": session_id,
        "goal": goal,
        "arch": "claude-code",
        "model": "claude-code",
        "status": "running",
        "turn": 0,
        "max_turns": max_turns,
        "started": datetime.now().isoformat(),
        "total_cost": 0.0,
        "skill_names": list(skills.keys()),
        "agent_id": agent_id,
    }
    _write_status(session_dir, status)

    mcp_config_path = None
    try:
        full_prompt = _build_claude_code_prompt(goal, cortex_info, skills, agent_id)

        cmd = [str(CLAUDE_BIN), "--dangerously-skip-permissions", "--max-budget-usd", str(budget)]

        if cortex_info:
            mcp_config_path = f"/tmp/agent-mcp-{session_id}.json"
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
        env.pop("CLAUDECODE", None)  # allow launching claude inside a claude-code session
        # Ensure node is on PATH (claude CLI requires it)
        node_paths = ["/usr/local/bin", "/usr/bin", str(Path.home() / ".npm-global" / "bin")]
        existing_path = env.get("PATH", "")
        extra = ":".join(p for p in node_paths if p not in existing_path)
        if extra:
            env["PATH"] = extra + ":" + existing_path
        with open(log_path, "a") as log_file:
            proc = subprocess.run(cmd, stdout=log_file, stderr=log_file, env=env)

        final_status = "done" if proc.returncode == 0 else "error"
        status["status"] = final_status
        status["completed"] = datetime.now().isoformat()
        _write_status(session_dir, status)

        result_text = log_path.read_text() if log_path.exists() else ""
        (session_dir / "result.md").write_text(result_text)

        return {
            "session_id": session_id,
            "goal": goal,
            "status": final_status,
            "turns": 0,
            "cost": 0.0,
            "result_snippet": result_text[-2000:],
            "skill_names": list(skills.keys()),
            "agent_id": agent_id,
        }
    except Exception as e:
        log(f"[ERROR] claude-code session failed: {e}")
        status["status"] = "error"
        status["error"] = str(e)
        status["completed"] = datetime.now().isoformat()
        _write_status(session_dir, status)
        return {
            "session_id": session_id,
            "goal": goal,
            "status": "error",
            "turns": 0,
            "cost": 0.0,
            "result_snippet": str(e),
            "skill_names": list(skills.keys()),
            "agent_id": agent_id,
        }
    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            try:
                os.unlink(mcp_config_path)
            except Exception:
                pass


def run_session(
    goal: str,
    skills: dict = None,
    arch: str = "claude",
    max_turns: int = 10,
    budget: float = 5.0,
    session_id: str = None,
    session_dir: Path = None,
    model: str = None,
    on_output=None,
    agent_id: str = None,
) -> dict:
    """Run a single-thread general-purpose agent session. Blocking."""
    from anthropic import Anthropic

    if skills is None:
        skills = {}
    if session_id is None:
        session_id = make_session_id(arch)
    if model is None:
        model = "claude-sonnet-4-6"

    if session_dir is None:
        session_dir = SESSIONS_DIR / session_id
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "goal.txt").write_text(goal)

    # Provision agent cortex if agent_id provided
    cortex_info = None
    if agent_id:
        try:
            cortex_info = agent_cortex_manager.provision(agent_id)
        except Exception as e:
            print(f"[DISPATCHER] Agent cortex provision failed for '{agent_id}': {e}")

    try:
        # Route to claude-code backend when explicitly requested or when agent_id is set
        if arch == "claude-code" or (agent_id and arch == "claude"):
            return run_session_claude_code(
                goal=goal,
                cortex_info=cortex_info,
                skills=skills,
                session_dir=session_dir,
                session_id=session_id,
                max_turns=max_turns,
                budget=budget,
                agent_id=agent_id,
            )

        status = {
            "session_id": session_id,
            "goal": goal,
            "arch": arch,
            "model": model,
            "status": "running",
            "turn": 0,
            "max_turns": max_turns,
            "started": datetime.now().isoformat(),
            "total_cost": 0.0,
            "skill_names": list(skills.keys()),
            "agent_id": agent_id,
        }
        _write_status(session_dir, status)

        system_prompt = build_system_prompt(skills, cortex_info)
        client = Anthropic()

        messages = [{"role": "user", "content": goal}]
        log_path = session_dir / "output.log"
        total_in = total_out = 0
        done = False

        def log(msg: str):
            with open(log_path, "a") as f:
                f.write(msg + "\n")
            if on_output:
                try:
                    on_output(msg + "\n")
                except Exception:
                    pass

        log(f"=== DISPATCHER SESSION {session_id} ===")
        log(f"Goal: {goal}")
        log(f"Model: {model} | Max turns: {max_turns} | Budget: ${budget}")
        if agent_id:
            log(f"Agent: {agent_id} | Cortex port: {cortex_info['port'] if cortex_info else 'N/A'}")
        if skills:
            log(f"Skills: {', '.join(skills.keys())}")
        log("=" * 60)

        for turn in range(1, max_turns + 1):
            status["turn"] = turn
            _write_status(session_dir, status)
            log(f"\n--- Turn {turn}/{max_turns} ---")

            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=8192,
                    system=system_prompt,
                    messages=messages,
                )
            except Exception as e:
                log(f"[ERROR] API call failed: {e}")
                status["status"] = "error"
                status["error"] = str(e)
                _write_status(session_dir, status)
                break

            total_in += response.usage.input_tokens
            total_out += response.usage.output_tokens

            text = "".join(
                b.text for b in response.content
                if hasattr(b, "type") and b.type == "text"
            )
            log(text)

            if "[DONE]" in text:
                done = True
                log(f"\n[SESSION COMPLETE] {turn} turns")
                break

            # Sonnet-4-6 pricing: $3/M in, $15/M out
            cost = (total_in / 1_000_000) * 3.0 + (total_out / 1_000_000) * 15.0
            status["total_cost"] = round(cost, 4)
            if cost >= budget:
                log(f"\n[BUDGET EXCEEDED] ${cost:.4f} >= ${budget}")
                break

            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "Continue working toward the goal."})

        cost = (total_in / 1_000_000) * 3.0 + (total_out / 1_000_000) * 15.0
        final_status = "done" if done else ("error" if status.get("status") == "error" else "stopped")
        status["status"] = final_status
        status["completed"] = datetime.now().isoformat()
        status["total_cost"] = round(cost, 4)
        _write_status(session_dir, status)

        result_text = log_path.read_text() if log_path.exists() else ""
        (session_dir / "result.md").write_text(result_text)

        return {
            "session_id": session_id,
            "goal": goal,
            "status": final_status,
            "turns": status["turn"],
            "cost": status["total_cost"],
            "result_snippet": result_text[-2000:],
            "skill_names": list(skills.keys()),
            "agent_id": agent_id,
        }

    finally:
        if agent_id and cortex_info:
            agent_cortex_manager.release(agent_id)


# ---------------------------------------------------------------------------
# Cortex integration
# ---------------------------------------------------------------------------

def store_in_cortex(session_result: dict):
    """Write session summary directly to Cortex SQLite DB."""
    try:
        import sqlite3
        db_path = str(CORTEX_DB)
        if not os.path.exists(db_path):
            return

        sid = session_result["session_id"]
        goal = session_result.get("goal", "")
        cost = session_result.get("cost", 0)
        status_str = session_result.get("status", "unknown")
        turns = session_result.get("turns", 0)
        skill_names = session_result.get("skill_names", [])
        snippet = session_result.get("result_snippet", "")

        tags = json.dumps(["dispatcher", sid] + list(skill_names))
        content = (
            f"**Dispatcher session:** {sid}\n"
            f"**Goal:** {goal}\n\n"
            f"**Output:**\n{snippet[:3000]}\n\n"
            f"**Status:** {status_str}  **Cost:** ${cost:.4f}  **Turns:** {turns}"
        )
        entry_id = uuid.uuid4().hex
        ts = datetime.utcnow().isoformat() + "Z"
        source = f"dispatcher/{sid}"

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO entries (id, timestamp, content, tags, source) VALUES (?,?,?,?,?)",
            (entry_id, ts, content, tags, source),
        )
        conn.execute(
            "INSERT INTO entries_fts (content, tags, source) VALUES (?,?,?)",
            (content, tags, source),
        )
        conn.commit()
        conn.close()
        return entry_id
    except Exception as e:
        print(f"[DISPATCHER] Cortex store failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def dispatch(
    goal: str,
    skill_names: list = None,
    arch: str = "claude",
    max_turns: int = 10,
    budget: float = 5.0,
    model: str = None,
    agent_id: str = None,
) -> dict:
    """Load skills, build prompt, run session, store in Cortex. Main entry point."""
    all_skills = load_skills()
    selected = {}
    if skill_names:
        selected = {k: v for k, v in all_skills.items() if k in skill_names}

    result = run_session(
        goal, selected, arch, max_turns, budget,
        model=model,
        agent_id=agent_id,
        on_output=lambda msg: print(msg, end="", flush=True),
    )
    store_in_cortex(result)
    return result
