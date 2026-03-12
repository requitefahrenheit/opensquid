#!/usr/bin/env python3
"""
rwx-server — Generic MCP server for remote Linux dev access from Claude.ai

Gives Claude.ai read/write/exec access to your Linux machine.
Not project-specific — works with any directory you whitelist.

Usage:
  pip install "mcp[cli]" uvicorn --break-system-packages
  python rwx-server.py

Environment variables:
  DEV_PORT          Port to listen on (default: 8251)
  DEV_ALLOWED_ROOTS Colon-separated allowed directories (default: ~)
  DEV_MAX_FILE_SIZE Max file size in bytes (default: 10MB)
  DEV_CMD_TIMEOUT   Command timeout in seconds (default: 120)
"""

import os
import json
import subprocess
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# === Audit log ===

AUDIT_LOG = Path(os.path.expanduser("~/claude/rwx/audit.log"))
AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

def audit(tool: str, **kwargs):
    """Append one JSON line per tool call: timestamp, tool, params."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool, **kwargs}
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")

# === Config from environment ===

PORT = int(os.environ.get("DEV_PORT", "8251"))
ALLOWED_ROOTS = [
    os.path.expanduser(p.strip()) 
    for p in os.environ.get("DEV_ALLOWED_ROOTS", "~").split(":")
    if p.strip()
]
MAX_FILE_SIZE = int(os.environ.get("DEV_MAX_FILE_SIZE", str(10 * 1024 * 1024)))
CMD_TIMEOUT = int(os.environ.get("DEV_CMD_TIMEOUT", "120"))
DEFAULT_CWD = ALLOWED_ROOTS[0] if ALLOWED_ROOTS else os.path.expanduser("~")

BLOCKED_COMMANDS = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "> /dev/sd",
    "shutdown", "reboot", "init 0", "init 6",
    "chmod -R 777 /", "chown -R", ":(){ :|:",
]

mcp = FastMCP("dev_mcp")


# === Helpers ===

def validate_path(path_str: str) -> Path:
    """Resolve path and verify it's within allowed roots."""
    resolved = Path(os.path.expanduser(path_str)).resolve()
    for root in ALLOWED_ROOTS:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"Path '{path_str}' is outside allowed directories: {', '.join(ALLOWED_ROOTS)}"
    )


def check_command(cmd: str) -> None:
    """Screen command against blocklist."""
    cmd_lower = cmd.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            raise ValueError(f"Blocked command pattern: '{blocked}'")


def format_size(size: int) -> str:
    if size < 1024: return f"{size}B"
    if size < 1024 * 1024: return f"{size // 1024}K"
    return f"{size // (1024 * 1024)}M"


# === Input Models ===

class ReadFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    path: str = Field(..., description="File path (absolute or ~-relative)", min_length=1)
    line_start: Optional[int] = Field(default=None, description="Start line, 1-indexed", ge=1)
    line_end: Optional[int] = Field(default=None, description="End line (inclusive), -1 for EOF")

class WriteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    path: str = Field(..., description="File path to write/create", min_length=1)
    content: str = Field(..., description="File content", max_length=MAX_FILE_SIZE)

class PatchFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    path: str = Field(..., description="File path to patch", min_length=1)
    old_str: str = Field(..., description="String to find (must be unique in file)", min_length=1)
    new_str: str = Field(default="", description="Replacement string (empty to delete)")

class RunCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    command: str = Field(..., description="Shell command to run", min_length=1)
    cwd: Optional[str] = Field(default=None, description=f"Working directory (default: {DEFAULT_CWD})")
    timeout: Optional[int] = Field(default=CMD_TIMEOUT, description="Timeout seconds", ge=1, le=600)

class ListFilesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    path: Optional[str] = Field(default=None, description=f"Directory (default: {DEFAULT_CWD})")
    pattern: Optional[str] = Field(default=None, description="Glob pattern, e.g. '*.py', '**/*.html'")
    depth: Optional[int] = Field(default=1, description="Directory depth (1-3)", ge=1, le=3)

class SearchFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    pattern: str = Field(..., description="Grep pattern (regex)", min_length=1)
    path: Optional[str] = Field(default=None, description=f"File or directory to search (default: {DEFAULT_CWD})")
    include: Optional[str] = Field(default=None, description="File glob to include, e.g. '*.py'")
    max_results: Optional[int] = Field(default=50, description="Max matches to return", ge=1, le=200)


# === Tools ===

@mcp.tool(
    name="dev_read_file",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def dev_read_file(params: ReadFileInput) -> str:
    """Read a file with line numbers. Supports line ranges for large files."""
    audit("read_file", path=params.path, line_start=params.line_start, line_end=params.line_end)
    try:
        resolved = validate_path(params.path)
        if not resolved.exists(): return f"Error: Not found: {resolved}"
        if not resolved.is_file(): return f"Error: Not a file: {resolved}"
        if resolved.stat().st_size > MAX_FILE_SIZE:
            return f"Error: File too large ({format_size(resolved.stat().st_size)}, max {format_size(MAX_FILE_SIZE)})"

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)

        start = (params.line_start or 1) - 1
        end = total if (params.line_end is None or params.line_end == -1) else min(params.line_end, total)

        if start >= total:
            return f"Error: Line {params.line_start} exceeds file length ({total})"

        numbered = "".join(f"{i + start + 1:6d} | {line}" for i, line in enumerate(lines[start:end]))
        header = f"{resolved} ({total} lines, {format_size(resolved.stat().st_size)})"
        if params.line_start or params.line_end:
            header += f" [lines {start + 1}–{end}]"
        return f"{header}\n{'─' * 60}\n{numbered}"

    except ValueError as e: return f"Error: {e}"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="dev_write_file",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
)
async def dev_write_file(params: WriteFileInput) -> str:
    """Write content to a file. Creates parent directories if needed."""
    audit("write_file", path=params.path, size=len(params.content))
    try:
        resolved = validate_path(params.path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(params.content, encoding="utf-8")
        lines = params.content.count('\n') + (1 if params.content and not params.content.endswith('\n') else 0)
        return f"Written: {resolved} ({format_size(len(params.content))}, {lines} lines)"
    except ValueError as e: return f"Error: {e}"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="dev_patch_file",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def dev_patch_file(params: PatchFileInput) -> str:
    """Find a unique string in a file and replace it. Like str_replace over the network."""
    audit("patch_file", path=params.path, old_len=len(params.old_str), new_len=len(params.new_str), preview=params.old_str[:120])
    try:
        resolved = validate_path(params.path)
        if not resolved.exists(): return f"Error: Not found: {resolved}"

        content = resolved.read_text(encoding="utf-8")
        count = content.count(params.old_str)
        if count == 0: return f"Error: String not found in {resolved.name} ({len(content)} chars, {content.count(chr(10))} lines)"
        if count > 1: return f"Error: String appears {count} times. Must be unique — add surrounding context."

        resolved.write_text(content.replace(params.old_str, params.new_str, 1), encoding="utf-8")
        return f"Patched {resolved.name}: replaced {len(params.old_str)} chars with {len(params.new_str)} chars"

    except ValueError as e: return f"Error: {e}"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="dev_run",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
)
async def dev_run(params: RunCommandInput) -> str:
    """Run a shell command. Returns stdout, stderr, and exit code."""
    audit("run", command=params.command, cwd=params.cwd, timeout=params.timeout)
    try:
        check_command(params.command)
        cwd = str(Path(os.path.expanduser(params.cwd or DEFAULT_CWD)).resolve())

        proc = await asyncio.create_subprocess_shell(
            params.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=params.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: Timed out after {params.timeout}s"

        parts = []
        if stdout:
            text = stdout.decode("utf-8", errors="replace")
            if len(text) > 50000:
                text = text[:25000] + "\n\n...(truncated)...\n\n" + text[-25000:]
            parts.append(text)
        if stderr:
            err = stderr.decode("utf-8", errors="replace").strip()
            if err: parts.append(f"[stderr] {err}")

        return (("\n".join(parts) if parts else "(no output)") + f"\n[exit {proc.returncode}]")

    except ValueError as e: return f"Error: {e}"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="dev_list",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def dev_list(params: ListFilesInput) -> str:
    """List files in a directory with sizes and types."""
    audit("list", path=params.path, pattern=params.pattern)
    try:
        resolved = validate_path(params.path or DEFAULT_CWD)
        if not resolved.exists(): return f"Error: Not found: {resolved}"
        if not resolved.is_dir(): return f"Error: Not a directory: {resolved}"

        entries = []
        if params.pattern:
            items = sorted(resolved.glob(params.pattern))
        else:
            items = sorted(resolved.iterdir())

        for item in items:
            if item.name.startswith('.'):
                continue
            try:
                if item.is_dir():
                    child_count = sum(1 for _ in item.iterdir() if not _.name.startswith('.'))
                    entries.append(f"  📁 {item.name}/  ({child_count} items)")
                else:
                    entries.append(f"  📄 {item.name}  ({format_size(item.stat().st_size)})")
            except PermissionError:
                entries.append(f"  🔒 {item.name}  (permission denied)")

        return f"{resolved}/ ({len(entries)} items)\n{'─' * 40}\n" + "\n".join(entries)

    except ValueError as e: return f"Error: {e}"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


@mcp.tool(
    name="dev_search",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def dev_search(params: SearchFileInput) -> str:
    """Search files for a pattern using grep. Returns matching lines with file and line number."""
    audit("search", pattern=params.pattern, path=params.path, include=params.include)
    try:
        target = validate_path(params.path or DEFAULT_CWD)

        cmd_parts = ["grep", "-rn", "--color=never"]
        if params.include:
            cmd_parts.extend(["--include", params.include])
        cmd_parts.extend(["-m", str(params.max_results), params.pattern, str(target)])

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return f"No matches for '{params.pattern}' in {target}"

        lines = output.splitlines()
        header = f"Found {len(lines)} match{'es' if len(lines) != 1 else ''} for '{params.pattern}':"
        return f"{header}\n{'─' * 40}\n{output}"

    except ValueError as e: return f"Error: {e}"
    except asyncio.TimeoutError: return "Error: Search timed out after 30s"
    except Exception as e: return f"Error: {type(e).__name__}: {e}"


# === Entry point ===

if __name__ == "__main__":
    import uvicorn

    # Load bearer token
    # Token auth
    MCP_TOKEN = os.environ.get("RWX_TOKEN", "emc2ymmv")
    if MCP_TOKEN:
        print(f"🔒 Token auth enabled ({len(MCP_TOKEN)} chars)")
    else:
        print("⚠️  No token file found — running WITHOUT auth")

    class HostRewriteMiddleware:
        """Rewrite Host header + check bearer token."""
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                # --- Token check ---
                if MCP_TOKEN:
                    from urllib.parse import parse_qs
                    qs = parse_qs(scope.get("query_string", b"").decode())
                    token_param = qs.get("token", [None])[0]
                    # Also check Authorization header
                    auth_header = None
                    for k, v in scope.get("headers", []):
                        if k == b"authorization":
                            auth_header = v.decode()
                            break
                    bearer = auth_header.replace("Bearer ", "") if auth_header and auth_header.startswith("Bearer ") else None
                    if token_param != MCP_TOKEN and bearer != MCP_TOKEN:
                        if scope["type"] == "http":
                            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                            await send({"type": "http.response.body", "body": b"Unauthorized"})
                            return
                        else:
                            await send({"type": "websocket.close", "code": 4001})
                            return
                # --- Host rewrite ---
                new_headers = []
                for k, v in scope.get("headers", []):
                    if k == b"host":
                        new_headers.append((k, f"localhost:{PORT}".encode()))
                    else:
                        new_headers.append((k, v))
                scope["headers"] = new_headers
            await self.app(scope, receive, send)

    print(f"🔧 rwx-server starting on port {PORT}")
    print(f"📁 Allowed roots: {ALLOWED_ROOTS}")

    app = HostRewriteMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="127.0.0.1", port=PORT)
