#!/usr/bin/env python3
"""
Channels Server — Telegram Bot Gateway
=======================================
Routes Telegram messages to OpenMind, Cortex, and Daemon.

Run:  python3 channels-server.py
Port: 8257
"""

import os, json, logging, re
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from telegram import Update, Bot
from telegram.constants import ParseMode

# ─── Config ──────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

PORT = int(os.environ.get("CHANNELS_PORT", 8257))
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_ALLOWED_IDS = {
    int(x.strip()) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()
}
OPENMIND_URL = os.environ.get("OPENMIND_URL", "http://127.0.0.1:8250")
CORTEX_URL = os.environ.get("CORTEX_URL", "http://127.0.0.1:8080")
DAEMON_URL = os.environ.get("DAEMON_URL", "http://127.0.0.1:8256")
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "emc2ymmv")

log = logging.getLogger("channels")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ─── HTTP client ─────────────────────────────────────
_http: httpx.AsyncClient | None = None

async def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            timeout=30.0,
        )
    return _http

# ─── Upstream helpers ────────────────────────────────
async def call_mcp(base_url: str, tool_name: str, arguments: dict) -> dict:
    """Call an upstream MCP server via JSON-RPC."""
    import uuid
    client = await get_http()
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = await client.post(f"{base_url}/mcp", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "result" in data:
            content = data["result"].get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return {"ok": True, "text": "\n".join(texts) if texts else json.dumps(data["result"])}
        elif "error" in data:
            return {"ok": False, "text": json.dumps(data["error"])}
        return {"ok": True, "text": json.dumps(data)}
    except Exception as e:
        log.error(f"MCP call failed [{tool_name}]: {e}")
        return {"ok": False, "text": str(e)}


async def call_openmind_nl(text: str) -> dict:
    """POST to OpenMind /api/nl endpoint."""
    client = await get_http()
    try:
        resp = await client.post(f"{OPENMIND_URL}/api/nl", json={"text": text})
        resp.raise_for_status()
        return {"ok": True, "text": json.dumps(resp.json(), indent=2)}
    except Exception as e:
        log.error(f"OpenMind NL failed: {e}")
        return {"ok": False, "text": str(e)}

# ─── Routing logic ───────────────────────────────────
AGENTIC_KEYWORDS = re.compile(r"\b(do|run|build|schedule|deploy|create task|execute)\b", re.IGNORECASE)

# Slash-command mapping
SLASH_COMMANDS = {
    "/add": ("openmind", "add_node"),
    "/search": ("openmind", "search"),
    "/om": ("openmind", "natural_language"),
    "/store": ("cortex", "cortex_store"),
    "/find": ("cortex", "cortex_search"),
    "/ask": ("cortex", "cortex_semantic_search"),
    "/status": ("daemon", "daemon_task_list"),
    "/task": ("daemon", "daemon_task_status"),
}


async def route_message(text: str) -> str:
    """Decide where to send the message and return the reply text."""
    text = text.strip()

    # ── Slash commands ──
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/start":
            return "Hey — I'm your personal AI gateway. Send me anything."

        if cmd == "/help":
            lines = [
                "/add <text> — add node to OpenMind",
                "/search <query> — search OpenMind",
                "/om <text> — natural language to OpenMind",
                "/store <text> — store in Cortex",
                "/find <query> — keyword search Cortex",
                "/ask <query> — semantic search Cortex",
                "/status — list recent daemon tasks",
                "/task <id> — get task details",
                "/do <prompt> — create a daemon task",
                "Or just send freeform text → OpenMind NL",
            ]
            return "\n".join(lines)

        if cmd == "/do":
            if not arg:
                return "Usage: /do <prompt>"
            result = await call_mcp(DAEMON_URL, "daemon_task_create", {"title": arg[:80], "prompt": arg})
            return result["text"]

        if cmd in SLASH_COMMANDS:
            service, tool = SLASH_COMMANDS[cmd]
            if not arg and cmd not in ("/status",):
                return f"Usage: {cmd} <text>"

            if service == "openmind":
                url = OPENMIND_URL
            elif service == "cortex":
                url = CORTEX_URL
            else:
                url = DAEMON_URL

            # Build arguments
            if tool in ("add_node", "cortex_store"):
                args = {"content": arg}
            elif tool in ("search", "cortex_search", "cortex_semantic_search"):
                args = {"query": arg}
            elif tool == "natural_language":
                args = {"text": arg}
            elif tool == "daemon_task_list":
                args = {"limit": 10}
            elif tool == "daemon_task_status":
                args = {"task_id": arg.strip()}
            else:
                args = {"text": arg}

            result = await call_mcp(url, tool, args)
            return result["text"]

        return f"Unknown command: {cmd}. Try /help"

    # ── Agentic requests → daemon ──
    if AGENTIC_KEYWORDS.search(text) and len(text) > 20:
        result = await call_mcp(DAEMON_URL, "daemon_task_create", {"title": text[:80], "prompt": text})
        return f"Task created:\n{result['text']}"

    # ── Default: freeform → OpenMind NL ──
    result = await call_openmind_nl(text)
    return result["text"]

# ─── Telegram webhook handler ────────────────────────
async def telegram_webhook(request: Request):
    """Handle incoming Telegram webhook updates."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request"}, status_code=400)

    update = Update.de_json(data, bot)

    if not update.message or not update.message.text:
        return JSONResponse({"ok": True})

    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    text = update.message.text

    # Allowlist check — reject silently
    if TELEGRAM_ALLOWED_IDS and user_id not in TELEGRAM_ALLOWED_IDS:
        log.warning(f"[TELEGRAM] Rejected user {user_id}")
        return JSONResponse({"ok": True})

    log.info(f"[TELEGRAM] user={user_id} text={text[:100]}")

    try:
        reply = await route_message(text)
    except Exception as e:
        log.error(f"[TELEGRAM] Routing error: {e}")
        reply = f"Error: {e}"

    # Truncate to Telegram's 4096-char limit
    if len(reply) > 4000:
        reply = reply[:4000] + "\n… (truncated)"

    try:
        await bot.send_message(chat_id=chat_id, text=reply)
    except Exception as e:
        log.error(f"[TELEGRAM] Send failed: {e}")

    return JSONResponse({"ok": True})

# ─── Health endpoint ─────────────────────────────────
async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "channels-server"})

# ─── App ─────────────────────────────────────────────
routes = [
    Route("/telegram/webhook", telegram_webhook, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
]

app = Starlette(routes=routes)

# Phase 2b — Discord: TODO
# Phase 2c — WhatsApp/Baileys: TODO

if __name__ == "__main__":
    log.info(f"Starting channels-server on port {PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
