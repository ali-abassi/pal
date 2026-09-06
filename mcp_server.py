#!/usr/bin/env python3
"""Minimal MCP (stdio, newline-delimited JSON-RPC) server exposing pal sessions as tools.

Register once:
  claude mcp add --scope user pal -- python3 /absolute/path/to/pal/mcp_server.py
Every tool shells out to the `pal` CLI, so the CLI stays the single source of truth.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

PAL = [sys.executable, str(Path(__file__).resolve().with_name("pal.py"))]
SERVER_INFO = {"name": "pal", "version": "1.2.0"}
PROTOCOL_VERSION = "2024-11-05"
OUTPUT_LOCK = threading.Lock()
MAX_BLOCKING_TIMEOUT = 540

NAME = {"type": "string", "description": "pal session name"}
PROMPT = {"type": "string", "description": "message to send to the agent"}
WAIT = {"type": "boolean", "description": "block for the reply (default true)", "default": True}
TIMEOUT = {"type": "number", "description": "requested upper bound in seconds; blocking calls are capped at 540 (default 600)", "default": 600}

TOOLS = [
    {
        "name": "pal_start",
        "description": "Use only when the user explicitly requests agent delegation. Start a persistent, resumable session with another coding agent (codex, pi, or claude) and send the first message. Returns the agent's reply. Use pal_say to continue the same conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": {"type": "string", "enum": ["codex", "pi", "claude"]},
                "prompt": PROMPT,
                "name": {"type": "string", "description": "session name (default <backend>-xxxx)"},
                "cwd": {"type": "string", "description": "working directory the agent may edit (default: server cwd)"},
                "worktree": {"type": "string", "description": "create an isolated git worktree for this branch (requires cwd repo)"},
                "shared": {"type": "boolean", "description": "macOS: read-only worker with exclusive proposal paths", "default": False},
                "files": {"type": "array", "items": {"type": "string"}, "description": "exact repo-relative paths; required for shared"},
                "model": {"type": "string", "description": "codex: gpt-5.6-sol…; pi: provider/id; claude: sonnet|opus"},
                "effort": {"type": "string", "description": "codex low|medium|high|xhigh; pi thinking level; claude effort"},
                "agent": {"type": "string", "description": "pi: ~/.pi-x/agent/agents/<name>.md role; claude: custom agent"},
                "no_brief": {"type": "boolean", "description": "skip the orchestration preamble", "default": False},
                "wait": WAIT,
                "timeout": TIMEOUT,
            },
            "required": ["backend", "prompt"],
        },
    },
    {
        "name": "pal_say",
        "description": "Send the next message to an existing pal session; the agent keeps its full prior context. Returns its reply.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": NAME, "prompt": PROMPT, "wait": WAIT, "timeout": TIMEOUT},
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "pal_wait",
        "description": "Block until the named sessions finish their current turn, then return their latest replies.",
        "inputSchema": {
            "type": "object",
            "properties": {"names": {"type": "array", "items": NAME}, "timeout": TIMEOUT},
            "required": ["names"],
        },
    },
    {
        "name": "pal_read",
        "description": "Read a session's reply (latest by default, or a given turn), or the whole transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": NAME, "turn": {"type": "integer"}, "all": {"type": "boolean", "default": False}},
            "required": ["name"],
        },
    },
    {
        "name": "pal_log",
        "description": "Show what a session actually did: commands run, files touched, errors; optionally backend stderr.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": NAME, "turn": {"type": "integer"}, "stderr": {"type": "boolean", "default": False}},
            "required": ["name"],
        },
    },
    {
        "name": "pal_diff",
        "description": "git status and diff in the session's working directory (the real change, not the agent's claim).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": NAME, "full": {"type": "boolean", "description": "full diff instead of --stat", "default": False}},
            "required": ["name"],
        },
    },
    {
        "name": "pal_list",
        "description": "List recent pal sessions with backend, model, parent, tokens, cost, status, and cwd.",
        "inputSchema": {
            "type": "object",
            "properties": {"all": {"type": "boolean", "description": "include sessions older than 24 hours", "default": False}},
        },
    },
    {
        "name": "pal_status",
        "description": "Read session metadata and descendant usage totals.",
        "inputSchema": {"type": "object", "properties": {"name": NAME}, "required": ["name"]},
    },
    {
        "name": "pal_stop",
        "description": "Kill a session's running turn.",
        "inputSchema": {"type": "object", "properties": {"name": NAME}, "required": ["name"]},
    },
]


for action in ("board", "apply", "release", "recover"):
    properties = {"cwd": {"type": "string", "description": "Git checkout path"}}
    required = ["cwd"]
    if action in ("apply", "release"):
        properties["name"] = NAME
        required.append("name")
    if action == "apply":
        properties["proposal"] = {"type": "string", "description": "Reviewed JSON text replacement proposal; lead only"}
        required.append("proposal")
    TOOLS.append({
        "name": "pal_shared_" + action,
        "description": "Shared checkout " + action + ". Only board is read-only. Mutations belong to the lead; application is not acceptance.",
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    })


def run_pal(argv: list[str], stdin_text: str | None = None) -> str:
    proc = subprocess.run(PAL + argv, input=stdin_text, capture_output=True, text=True,
                          stdin=None if stdin_text is not None else subprocess.DEVNULL)
    out = proc.stdout
    if proc.returncode == 124:
        out += "\n[pal: still running; call pal_wait or pal_stop]"
    elif proc.returncode != 0:
        out += f"\n[pal exit {proc.returncode}] {proc.stderr.strip()}"
    return out.strip() or "(no output)"


def turn_args(a: dict) -> list[str]:
    timeout = effective_timeout(a) if a.get("wait", True) else a.get("timeout", 600)
    argv = ["--timeout", str(timeout)]
    if not a.get("wait", True):
        argv.append("--bg")
    return argv


def effective_timeout(a: dict) -> float | int:
    return min(a.get("timeout", 600), MAX_BLOCKING_TIMEOUT)


def tool_start(a: dict) -> str:
    argv = ["start", a["backend"]]
    for flag, key in (("-n", "name"), ("-C", "cwd"), ("-m", "model"), ("--effort", "effort"), ("--agent", "agent"), ("--worktree", "worktree")):
        if a.get(key):
            argv += [flag, a[key]]
    argv += shared_start_args(a)
    if a.get("no_brief"):
        argv.append("--no-brief")
    return run_pal(argv + turn_args(a) + ["-"], a["prompt"])


def shared_start_args(a: dict) -> list[str]:
    flags = ["--shared"] if a.get("shared") else []
    return flags + [value for path in a.get("files", []) for value in ("--files", path)]


def tool_say(a: dict) -> str:
    return run_pal(["say", a["name"]] + turn_args(a) + ["-"], a["prompt"])


def tool_wait(a: dict) -> str:
    return run_pal(["wait", *a["names"], "--timeout", str(effective_timeout(a))])


def tool_read(a: dict) -> str:
    argv = ["read", a["name"]]
    if a.get("turn"):
        argv += ["-t", str(a["turn"])]
    if a.get("all"):
        argv.append("--all")
    return run_pal(argv)


def tool_log(a: dict) -> str:
    argv = ["log", a["name"]]
    if a.get("turn"):
        argv += ["-t", str(a["turn"])]
    if a.get("stderr"):
        argv.append("--stderr")
    return run_pal(argv)


def tool_diff(a: dict) -> str:
    return run_pal(["diff", a["name"]] + (["--full"] if a.get("full") else []))


def tool_list(a: dict) -> str:
    return run_pal(["ls"] + (["--all"] if a.get("all") else []))


def tool_status(a: dict) -> str:
    return run_pal(["status", a["name"]])


def tool_stop(a: dict) -> str:
    return run_pal(["stop", a["name"]])


def tool_shared(action: str, a: dict) -> str:
    argv = ["shared", action, "-C", a["cwd"]]
    if action in ("apply", "release"):
        argv.append(a["name"])
    if action == "apply":
        return run_pal(argv + ["--file", "-"], a["proposal"])
    return run_pal(argv)


HANDLERS = {
    "pal_start": tool_start, "pal_say": tool_say, "pal_wait": tool_wait, "pal_read": tool_read,
    "pal_log": tool_log, "pal_diff": tool_diff, "pal_list": tool_list, "pal_stop": tool_stop,
    "pal_status": tool_status,
}


for shared_action in ("board", "apply", "release", "recover"):
    HANDLERS["pal_shared_" + shared_action] = lambda a, action=shared_action: tool_shared(action, a)


def handle(req: dict) -> dict | None:
    method = req.get("method")
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(req.get("params", {}))
    if method == "ping":
        return {}
    return None


def call_tool(params: dict) -> dict:
    handler = HANDLERS.get(params.get("name"))
    if handler is None:
        return {"content": [{"type": "text", "text": f"unknown tool {params.get('name')}"}], "isError": True}
    try:
        result = handler(params.get("arguments") or {})
    except (KeyError, TypeError, ValueError, OSError) as error:
        return {"content": [{"type": "text", "text": f"invalid tool request: {error}"}], "isError": True}
    return {"content": [{"type": "text", "text": result}]}


def response_for(req: dict) -> dict:
    result = handle(req)
    if result is None:
        return {"jsonrpc": "2.0", "id": req["id"],
                "error": {"code": -32601, "message": f"unknown method {req.get('method')}"}}
    return {"jsonrpc": "2.0", "id": req["id"], "result": result}


def write_response(req: dict) -> None:
    reply = response_for(req)
    with OUTPUT_LOCK:
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if "id" not in req:
            continue  # notification; nothing to answer
        if req.get("method") == "tools/call":
            threading.Thread(target=write_response, args=(req,)).start()
        else:
            write_response(req)


if __name__ == "__main__":
    main()
