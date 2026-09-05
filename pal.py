#!/usr/bin/env python3
"""pal — persistent, resumable back-and-forth sessions with other coding agents.

Backends: codex (OpenAI Codex CLI), pi (pi coding agent), claude (Claude Code).
Every backend runs in full-autonomy ("yolo") mode: no approval prompts, no sandbox.

State lives in $PAL_HOME (default ~/.pal):
  sessions/<name>/meta.json          session record + turn index
  sessions/<name>/turns/NNN.prompt.md / NNN.reply.md / NNN.events.jsonl / NNN.stderr
  sessions/<name>/transcript.md      human-readable running log
  sessions/<name>/pi/                pi session files (pi backend only)
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAL_HOME = Path(os.environ.get("PAL_HOME", Path.home() / ".pal"))
SESSIONS = PAL_HOME / "sessions"
ARCHIVE = PAL_HOME / "archive"
PRICES = PAL_HOME / "prices.json"
WORKTREE_ROOT = Path(os.environ.get(
    "PAL_WORKTREE_ROOT", PAL_HOME / "worktrees",
))
TMP_ROOT = Path(os.environ.get("PAL_TMP_ROOT", PAL_HOME / "tmp"))
PI_AGENTS_DIR = Path(os.environ.get("PAL_PI_AGENTS_DIR", Path.home() / ".pi-x" / "agent" / "agents"))
BACKENDS = ("codex", "pi", "claude")
DEFAULT_MODELS = {"claude": "sonnet"}
POLL_SECONDS = 1.0
SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

BRIEF = """[Orchestration note] You are being driven by an orchestrating agent through a non-interactive session. It reads your reply and may follow up with more instructions in this same session, so keep your working state and be ready to continue.
Rules:
- You have full autonomy in {cwd}. Never ask for permission or wait for confirmation; there is nobody to answer mid-turn.
- If something is ambiguous, choose the most reasonable interpretation, state the assumption, and proceed.
- Do the work completely and verify it (build, tests, or a real run where they exist).
- End every reply with three short sections: (1) What changed, with file paths. (2) How you verified it and the results. (3) Open items: anything undone, uncertain, or needing a decision.

Task:
"""


# ---------- small helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg: str, code: int = 2) -> None:
    print(f"pal: {msg}", file=sys.stderr)
    sys.exit(code)


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def validate_session_name(name: str) -> None:
    if not SESSION_NAME.fullmatch(name):
        die("session names must be 1-64 letters, numbers, dots, dashes, or underscores")


def session_dir(name: str) -> Path:
    return SESSIONS / name


def load_meta(name: str, repair_stale: bool = True) -> dict:
    validate_session_name(name)
    path = session_dir(name) / "meta.json"
    if not path.exists():
        die(f"no session named '{name}' (see: pal ls)")
    meta = read_json(path)
    if repair_stale and stale_runner(meta):
        meta = repair_stale_runner(name)
    return meta


def stale_runner(meta: dict) -> bool:
    runner_pid = meta.get("runner_pid")
    return meta.get("status") == "running" and bool(runner_pid) and not pid_alive(runner_pid)


def repair_stale_runner(name: str) -> dict:
    with edit_meta(name) as meta:
        if stale_runner(meta):
            meta["status"] = "error"
            mark_stale_turn(meta.get("turns", []))
    return meta


def mark_stale_turn(turns: list[dict]) -> None:
    if not turns or turns[-1].get("status") != "running":
        return
    turns[-1].update({"status": "error", "rc": 1, "errors": ["runner died"]})


def save_meta(meta: dict) -> None:
    meta["updated"] = now_iso()
    write_json(session_dir(meta["name"]) / "meta.json", meta)


@contextmanager
def edit_meta(name: str):
    validate_session_name(name)
    with (session_dir(name) / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = read_json(session_dir(name) / "meta.json")
        try:
            yield meta
        except BaseException:
            raise
        else:
            save_meta(meta)


def all_sessions() -> list[dict]:
    if not SESSIONS.exists():
        return []
    metas = []
    for d in sorted(SESSIONS.iterdir()):
        if (d / "meta.json").exists():
            metas.append(read_json(d / "meta.json"))
    return metas


def unique_name(backend: str) -> str:
    while True:
        name = f"{backend}-{secrets.token_hex(2)}"
        if not session_dir(name).exists():
            return name


def read_prompt(arg: str | None, files: list[str]) -> str:
    parts = [Path(file).read_text() for file in files]
    primary = read_primary_prompt(arg, bool(files))
    if primary is not None:
        parts.append(primary)
    prompt = "\n\n".join(filter(None, (part.strip("\n") for part in parts)))
    if not prompt.strip():
        die("empty prompt")
    return prompt


def read_primary_prompt(arg: str | None, has_files: bool) -> str | None:
    if arg not in (None, "-"):
        return arg
    if arg is None and has_files:
        return None
    if sys.stdin.isatty():
        die("prompt required (argument, -f FILE, or '-' for stdin)")
    return sys.stdin.read()


def turn_paths(name: str, n: int) -> dict[str, Path]:
    base = session_dir(name) / "turns"
    stem = f"{n:03d}"
    return {
        "prompt": base / f"{stem}.prompt.md",
        "reply": base / f"{stem}.reply.md",
        "events": base / f"{stem}.events.jsonl",
        "stderr": base / f"{stem}.stderr",
        "last": base / f"{stem}.last.md",
    }


def load_pi_agent(agent: str) -> dict:
    path = pi_agent_path(agent)
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {"body": text}
    front = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            front[k.strip()] = v.strip()
    front["body"] = m.group(2).strip()
    return front


def pi_agent_path(agent: str) -> Path:
    if not SESSION_NAME.fullmatch(agent):
        die("pi agent names must be 1-64 letters, numbers, dots, dashes, or underscores")
    path = PI_AGENTS_DIR / f"{agent}.md"
    if not path.exists():
        die(f"pi agent '{agent}' not found at {path}")
    return path


# ---------- backend command construction ----------

def codex_cmd(meta: dict, paths: dict[str, Path]) -> tuple[list[str], str | None]:
    yolo = ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
    tuning = []
    if meta.get("model"):
        tuning += ["-m", meta["model"]]
    if meta.get("effort"):
        tuning += ["-c", f'model_reasoning_effort="{meta["effort"]}"']
    head = ["codex", "exec", "--json", "-o", str(paths["last"])] + yolo
    sid = meta.get("backend_session_id")
    if sid:
        cmd = head + ["resume", sid] + tuning + ["-"]
    else:
        cmd = head + ["-C", meta["cwd"]] + tuning + ["-"]
    return cmd, "stdin"


def claude_cmd(meta: dict, paths: dict[str, Path]) -> tuple[list[str], str | None]:
    sid = meta.get("backend_session_id")
    cmd = ["claude", "-p", "--dangerously-skip-permissions", "--verbose",
           "--output-format", "stream-json"]
    cmd += ["--resume", sid] if meta.get("backend_initialized") else ["--session-id", sid]
    if meta.get("model"):
        cmd += ["--model", meta["model"]]
    if meta.get("effort"):
        cmd += ["--effort", meta["effort"]]
    if meta.get("agent"):
        cmd += ["--agent", meta["agent"]]
    return cmd, "stdin"


def pi_cmd(meta: dict, paths: dict[str, Path]) -> tuple[list[str], str | None]:
    sdir = session_dir(meta["name"]) / "pi"
    sdir.mkdir(exist_ok=True)
    cmd = ["pi", "-p", "--mode", "json", "--session-dir", str(sdir),
           "--session-id", meta["backend_session_id"]]
    agent = load_pi_agent(meta["agent"]) if meta.get("agent") else {}
    append_option(cmd, "--model", meta.get("model") or agent.get("model"))
    append_option(cmd, "--thinking", meta.get("effort") or agent.get("thinking"))
    append_option(cmd, "--tools", agent.get("tools"))
    append_pi_role(cmd, meta["name"], agent.get("body"))
    return cmd, "argv"


def append_option(command: list[str], flag: str, value: str | None) -> None:
    if value:
        command.extend((flag, value))


def append_pi_role(command: list[str], name: str, body: str | None) -> None:
    if not body:
        return
    role = session_dir(name) / "agent-role.md"
    role.write_text(body)
    command.extend(("--append-system-prompt", str(role)))


BUILDERS = {"codex": codex_cmd, "claude": claude_cmd, "pi": pi_cmd}


# ---------- backend output parsing ----------

def parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = (parse_json_line(line) for line in path.read_text().splitlines())
    return [event for event in events if event is not None]


def parse_json_line(line: str) -> dict | None:
    try:
        return json.loads(line) if line.lstrip().startswith("{") else None
    except json.JSONDecodeError:
        return None


def parse_codex(events: list[dict]) -> dict:
    out = {
        "texts": [], "activity": [], "errors": [], "session_id": None,
        "cost": None, "tokens": None,
    }
    for ev in events:
        parse_codex_event(out, ev)
    return out


def parse_codex_event(out: dict, event: dict) -> None:
    handler = CODEX_EVENT_HANDLERS.get(event.get("type"))
    if handler:
        handler(out, event)


def parse_codex_item(out: dict, item: dict) -> None:
    handler = CODEX_ITEM_HANDLERS.get(item.get("type"))
    if handler:
        handler(out, item)


def codex_session(out: dict, event: dict) -> None:
    out["session_id"] = event.get("thread_id")


def codex_error(out: dict, event: dict) -> None:
    error = event.get("error", {})
    out["errors"].append(str(error.get("message") or event.get("message") or "codex turn failed"))


def codex_item_event(out: dict, event: dict) -> None:
    parse_codex_item(out, event.get("item", {}))


def codex_text(out: dict, item: dict) -> None:
    out["texts"].append(item.get("text", ""))


def codex_command(out: dict, item: dict) -> None:
    out["activity"].append(f"$ {item.get('command', '')}  (exit {item.get('exit_code')})")


def codex_changes(out: dict, item: dict) -> None:
    out["activity"].extend(
        f"{change.get('kind', 'edit')} {change.get('path', '')}"
        for change in item.get("changes", [])
    )


def codex_item_error(out: dict, item: dict) -> None:
    out["errors"].append(item.get("message", ""))


def codex_usage(out: dict, event: dict) -> None:
    usage = event.get("usage", {})
    out["tokens"] = {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
    }


CODEX_EVENT_HANDLERS = {
    "thread.started": codex_session,
    "turn.failed": codex_error,
    "turn.completed": codex_usage,
    "error": codex_error,
    "item.completed": codex_item_event,
}
CODEX_ITEM_HANDLERS = {
    "agent_message": codex_text,
    "command_execution": codex_command,
    "file_change": codex_changes,
    "error": codex_item_error,
}


def parse_claude(events: list[dict]) -> dict:
    out = {"texts": [], "activity": [], "errors": [], "session_id": None, "cost": None}
    for ev in events:
        parse_claude_event(out, ev)
    return out


def parse_claude_event(out: dict, event: dict) -> None:
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            parse_claude_block(out, block)
    elif event.get("type") == "result":
        parse_claude_result(out, event)


def parse_claude_block(out: dict, block: dict) -> None:
    if block.get("type") == "text" and block.get("text"):
        out["texts"].append(block["text"])
    elif block.get("type") == "tool_use":
        out["activity"].append(describe_claude_tool(block))


def parse_claude_result(out: dict, event: dict) -> None:
    out["session_id"] = event.get("session_id")
    out["cost"] = event.get("total_cost_usd")
    if event.get("is_error") or event.get("subtype", "").startswith("error"):
        out["errors"].append(str(event.get("result", "error")))
    elif event.get("result") and event["result"] not in out["texts"]:
        out["texts"].append(event["result"])


def describe_claude_tool(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input", {})
    if name == "Bash":
        return f"$ {inp.get('command', '')}"
    target = inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
    return f"{name} {target}".strip()


def parse_pi(events: list[dict]) -> dict:
    out = {"texts": [], "activity": [], "errors": [], "session_id": None, "cost": 0.0}
    for ev in events:
        parse_pi_event(out, ev)
    return out


def parse_pi_event(out: dict, event: dict) -> None:
    handler = PI_EVENT_HANDLERS.get(event.get("type"))
    if handler:
        handler(out, event)
    if pi_agent_failed(event):
        out["errors"].append("pi turn failed")


def parse_pi_message(out: dict, message: dict) -> None:
    if message.get("role") != "assistant":
        return
    for block in message.get("content", []):
        parse_pi_block(out, block)
    out["cost"] += message.get("usage", {}).get("cost", {}).get("total", 0) or 0


def parse_pi_block(out: dict, block: dict) -> None:
    if block.get("type") == "text" and block.get("text"):
        out["texts"].append(block["text"])
    elif block.get("type") == "toolCall":
        out["activity"].append(describe_pi_tool(block))


def pi_session(out: dict, event: dict) -> None:
    out["session_id"] = event.get("id")


def pi_message(out: dict, event: dict) -> None:
    parse_pi_message(out, event.get("message", {}))


def pi_error(out: dict, event: dict) -> None:
    out["errors"].append(str(event.get("error") or event.get("message") or event))


def pi_agent_failed(event: dict) -> bool:
    messages = event.get("messages", [])
    return (
        event.get("type") == "agent_end"
        and event.get("willRetry") is False
        and bool(messages)
        and messages[-1].get("stopReason") == "error"
    )


PI_EVENT_HANDLERS = {
    "session": pi_session,
    "message_end": pi_message,
    "error": pi_error,
}


def describe_pi_tool(block: dict) -> str:
    name = block.get("name", "tool")
    args = block.get("arguments", {})
    if name == "bash":
        return f"$ {args.get('command', '')}"
    target = args.get("path") or args.get("pattern") or ""
    return f"{name} {target}".strip()


PARSERS = {"codex": parse_codex, "claude": parse_claude, "pi": parse_pi}

PRICE_KEYS = (
    "input_usd_per_million",
    "cached_input_usd_per_million",
    "output_usd_per_million",
)


def read_price_table() -> tuple[dict, str | None]:
    if not PRICES.exists():
        return {}, None
    try:
        return read_json(PRICES), None
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"invalid price table {PRICES}: {error}"


def valid_rates(rates: dict) -> bool:
    return all(
        type(rates.get(key)) in (int, float) and rates[key] >= 0
        for key in PRICE_KEYS
    )


def model_rates(model: str | None) -> tuple[dict | None, str | None]:
    table, error = read_price_table()
    if error:
        return None, error
    rates = table.get(model) if model else None
    if not rates:
        return None, None
    if not isinstance(rates, dict):
        return None, f"invalid rates for model {model} in {PRICES}"
    return checked_rates(model, rates)


def checked_rates(model: str | None, rates: dict) -> tuple[dict | None, str | None]:
    if rates.get("placeholder") is True:
        return None, None
    if not valid_rates(rates):
        return None, f"invalid rates for model {model} in {PRICES}"
    return rates, None


def estimated_cost(tokens: dict, rates: dict) -> float:
    uncached = max(tokens["input_tokens"] - tokens["cached_input_tokens"], 0)
    total = uncached * rates["input_usd_per_million"]
    total += tokens["cached_input_tokens"] * rates["cached_input_usd_per_million"]
    total += tokens["output_tokens"] * rates["output_usd_per_million"]
    return total / 1_000_000


def apply_codex_pricing(meta: dict, parsed: dict) -> None:
    if meta["backend"] != "codex" or not parsed.get("tokens"):
        return
    rates, warning = model_rates(meta.get("model"))
    if warning:
        parsed["errors"].append(warning)
    if rates:
        parsed["cost"] = estimated_cost(parsed["tokens"], rates)


# ---------- turn execution ----------

def run_backend(meta: dict, paths: dict[str, Path], prompt: str) -> tuple[int, float]:
    started = time.time()
    try:
        rc = launch_backend(meta, paths, prompt)
        return rc, time.time() - started
    except OSError as error:
        paths["events"].touch()
        paths["stderr"].write_text(f"{error}\n")
        return 127, time.time() - started


def launch_backend(meta: dict, paths: dict[str, Path], prompt: str) -> int:
    command, prompt_via = BUILDERS[meta["backend"]](meta, paths)
    if prompt_via == "argv":
        command.append(prompt)
    with paths["events"].open("w") as out, paths["stderr"].open("w") as err:
        proc = launch_registered_backend(meta, command, prompt_via, out, err)
        communicate_prompt(proc, prompt if prompt_via == "stdin" else None)
        return proc.returncode


def launch_registered_backend(
    meta: dict, command: list[str], prompt_via: str | None, out, err,
) -> subprocess.Popen:
    with edit_meta(meta["name"]) as current:
        if current["status"] != "running":
            raise ChildProcessError(f"session {meta['name']} was stopped before backend launch")
        proc = subprocess.Popen(
            command, cwd=meta["cwd"], env=backend_env(meta),
            stdin=subprocess.PIPE if prompt_via == "stdin" else subprocess.DEVNULL,
            stdout=out, stderr=err, start_new_session=True,
        )
        current["pid"] = proc.pid
    return proc


def backend_env(meta: dict) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    env["PAL_SESSION"] = meta["name"]
    env["PAL_DEPTH"] = str(meta.get("depth", 0))
    return env


def communicate_prompt(proc: subprocess.Popen, prompt: str | None) -> None:
    if prompt is None:
        proc.wait()
        return
    proc.communicate(prompt.encode())


def execute_turn(name: str, n: int) -> None:
    """Run one turn to completion in a detached runner process."""
    meta = wait_for_runner_registration(name)
    paths = turn_paths(name, n)
    prompt = paths["prompt"].read_text()
    rc, elapsed = run_backend(meta, paths, prompt)
    finish_turn(name, n, rc, elapsed)


def wait_for_runner_registration(name: str) -> dict:
    for _ in range(200):
        meta = load_meta(name, repair_stale=False)
        if meta.get("runner_pid") == os.getpid():
            return meta
        if meta.get("status") in ("stopped", "error"):
            raise RuntimeError(f"runner cancelled before registration for {name}")
        time.sleep(0.01)
    raise RuntimeError(f"runner registration timed out for {name}")


def result_errors(meta: dict, parsed: dict, rc: int, reply: str) -> list[str]:
    errors = []
    if rc != 0:
        errors.append(f"backend exited {rc}")
    if not reply:
        errors.append("backend returned no assistant reply")
    if errors:
        # Backend error events explain a failed turn; on a successful turn they are only warnings
        # (codex emits e.g. "Skill descriptions were shortened" as an error item and still exits 0).
        errors = list(parsed["errors"]) + errors
    session_id = parsed.get("session_id")
    if not session_id:
        errors.append("backend did not report a session ID")
    mismatch = session_mismatch(meta.get("backend_session_id"), session_id)
    if mismatch:
        errors.append(mismatch)
    return list(dict.fromkeys(errors))


def session_mismatch(expected: str | None, actual: str | None) -> str | None:
    if not expected or not actual or expected == actual:
        return None
    return f"backend resumed unexpected session {actual}; expected {expected}"


def finish_turn(name: str, n: int, rc: int, elapsed: float) -> None:
    paths = turn_paths(name, n)
    with edit_meta(name) as meta:
        if meta["turns"][n - 1].get("status") == "stopped":
            return
        parsed = PARSERS[meta["backend"]](parse_jsonl(paths["events"]))
        apply_codex_pricing(meta, parsed)
        reply = projected_reply(parsed, paths)
        errors = result_errors(meta, parsed, rc, reply)
        if errors and not reply:
            reply = tail_text(paths["stderr"], 1200).strip() or errors[0]
        paths["reply"].write_text(reply + "\n")
        turn = update_turn(meta["turns"][n - 1], parsed, errors, rc, elapsed)
        update_session(meta, parsed, errors)
        append_transcript(meta, n, paths["prompt"].read_text(), reply, turn)


def projected_reply(parsed: dict, paths: dict[str, Path]) -> str:
    reply = "\n\n".join(text for text in parsed["texts"] if text.strip()).strip()
    if reply or not paths["last"].exists():
        return reply
    return paths["last"].read_text().strip()


def update_turn(turn: dict, parsed: dict, errors: list[str], rc: int, elapsed: float) -> dict:
    turn.update({
        "finished": now_iso(), "elapsed": round(elapsed, 1), "rc": rc,
        "status": "error" if errors else "done",
        "activity": len(parsed["activity"]), "cost": parsed.get("cost"),
        "errors": errors[:5],
        "warnings": [] if errors else parsed["errors"][:5],
    })
    if parsed.get("tokens"):
        turn.update(parsed["tokens"])
    return turn


def update_session(meta: dict, parsed: dict, errors: list[str]) -> None:
    if not errors:
        meta["backend_session_id"] = parsed["session_id"]
        meta["backend_initialized"] = True
    meta["status"] = "error" if errors else "idle"
    meta["pid"] = None
    meta["runner_pid"] = None


def tail_text(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    text = path.read_text()
    return text[-limit:]


def append_transcript(meta: dict, n: int, prompt: str, reply: str, turn: dict) -> None:
    cost = f", ${turn['cost']:.3f}" if isinstance(turn.get("cost"), (int, float)) and turn["cost"] else ""
    block = (
        f"\n## Turn {n} | orchestrator | {turn.get('started', '')}\n\n{prompt.strip()}\n"
        f"\n## Turn {n} | {meta['backend']} | {turn.get('elapsed', 0)}s{cost} | {turn['status']}\n\n{reply}\n"
    )
    with (session_dir(meta["name"]) / "transcript.md").open("a") as f:
        f.write(block)


def spawn_runner(name: str, n: int) -> int:
    log = session_dir(name) / "runner.log"
    with log.open("a") as lf:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_run", name, str(n)],
            stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True,
        )
    return proc.pid


def begin_turn(name: str, prompt: str) -> int:
    with edit_meta(name) as meta:
        reject_running_session(meta)
        n = len(meta["turns"]) + 1
        paths = turn_paths(name, n)
        paths["prompt"].parent.mkdir(exist_ok=True)
        paths["prompt"].write_text(prompt)
        meta["turns"].append({"n": n, "started": now_iso(), "status": "running"})
        meta["status"] = "running"
        meta["runner_pid"] = spawn_runner(name, n)
    return n


def reject_running_session(meta: dict) -> None:
    if meta.get("status") == "running":
        die(f"session '{meta['name']}' is still running turn {len(meta['turns'])}; "
            f"use 'pal wait {meta['name']}' or 'pal stop {meta['name']}'")


def wait_for(name: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while True:
        meta = load_meta(name)
        if meta["status"] != "running":
            return meta
        if time.time() >= deadline:
            return meta
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.time())))


# ---------- output ----------

def turn_header(meta: dict, turn: dict) -> str:
    model = meta.get("model") or "default-model"
    bits = [meta["name"], f"{meta['backend']} {model}", f"turn {turn['n']}", turn.get("status", "")]
    append_turn_metrics(bits, turn)
    return "[pal " + " | ".join(filter(None, bits)) + "]"


def append_turn_metrics(bits: list[str], turn: dict) -> None:
    if turn.get("elapsed") is not None:
        bits.append(f"{turn['elapsed']}s")
    if turn.get("activity"):
        bits.append(f"{turn['activity']} tool calls")
    if append_token_metrics(bits, turn):
        return
    if isinstance(turn.get("cost"), (int, float)):
        bits.append(format_cost(turn["cost"]))


def append_token_metrics(bits: list[str], turn: dict) -> bool:
    tokens = turn_tokens(turn)
    if not (tokens["input_tokens"] or tokens["output_tokens"]):
        return False
    bits.append(format_tokens(tokens))
    bits.append(format_cost(turn.get("cost"), unknown=True))
    return True


def turn_tokens(turn: dict) -> dict[str, int]:
    return {
        key: int(turn.get(key, 0) or 0)
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }


def format_tokens(tokens: dict[str, int]) -> str:
    return (
        f"tokens {tokens['input_tokens']:,} in/"
        f"{tokens['cached_input_tokens']:,} cached/{tokens['output_tokens']:,} out"
    )


def format_cost(cost: float | None, unknown: bool = False) -> str:
    if isinstance(cost, (int, float)):
        return f"cost ${cost:.4f}"
    return "cost unknown" if unknown else "cost -"


def sum_session_tokens(turns: list[dict]) -> dict[str, int]:
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for turn in turns:
        tokens = turn_tokens(turn)
        for key in total:
            total[key] += tokens[key]
    return total


def session_cost_metrics(meta: dict) -> tuple[float | None, bool]:
    total = 0.0
    observed = False
    unknown = False
    for turn in meta.get("turns", []):
        if isinstance(turn.get("cost"), (int, float)):
            total += turn["cost"]
            observed = True
        elif missing_codex_cost(meta, turn):
            unknown = True
    return (total if observed else None), unknown


def missing_codex_cost(meta: dict, turn: dict) -> bool:
    return meta.get("backend") == "codex" and turn.get("status") == "done"


def session_metrics(meta: dict) -> dict:
    metrics = sum_session_tokens(meta.get("turns", []))
    metrics["cost"], metrics["cost_unknown"] = session_cost_metrics(meta)
    return metrics


def add_metrics(total: dict, item: dict) -> None:
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        total[key] += item[key]
    if isinstance(item["cost"], (int, float)):
        total["cost"] += item["cost"]
    total["cost_unknown"] = total["cost_unknown"] or item["cost_unknown"]


def print_turn(meta: dict, n: int, as_json: bool) -> None:
    turn = meta["turns"][n - 1]
    paths = turn_paths(meta["name"], n)
    reply = paths["reply"].read_text() if paths["reply"].exists() else ""
    if as_json:
        print_turn_json(meta, turn, reply)
        return
    stream = output_stream(turn)
    print(turn_header(meta, turn), file=stream)
    if turn.get("status") == "running":
        print(f"still running; use `pal wait {meta['name']}` or `pal log {meta['name']}`", file=stream)
        return
    print(reply.rstrip(), file=stream)
    if turn.get("errors"):
        print("\n[pal errors]\n" + "\n".join(turn["errors"]), file=sys.stderr)
    if turn.get("warnings"):
        print("\n[pal warnings]\n" + "\n".join(turn["warnings"]), file=sys.stderr)


def print_turn_json(meta: dict, turn: dict, reply: str) -> None:
    print(json.dumps({
        "session": meta["name"], "backend": meta["backend"],
        "status": meta["status"], "turn": turn, "reply": reply,
    }, indent=2))


def output_stream(turn: dict):
    return sys.stderr if turn.get("status") == "error" else sys.stdout


def report_after_wait(meta: dict, n: int, as_json: bool) -> None:
    print_turn(meta, n, as_json)
    if meta["status"] == "running":
        sys.exit(124)
    if meta["status"] == "error":
        sys.exit(meta["turns"][n - 1].get("rc") or 1)
    if meta["status"] == "stopped":
        sys.exit(130)


# ---------- commands ----------

def cmd_start(args: argparse.Namespace) -> None:
    cwd = resolve_cwd(args.cwd)
    name = resolve_new_session_name(args.name, args.backend)
    validate_start_agent(args.backend, args.agent)
    parent, depth = new_session_parentage()
    prompt = read_prompt(args.prompt, args.file)
    repo = None
    if args.worktree:
        if not args.cwd:
            die("--worktree requires -C REPO")
        repo = cwd
        cwd = create_worktree(repo, name, args.worktree)
    if not args.no_brief:
        prompt = BRIEF.format(cwd=cwd) + prompt
    meta = new_session_meta(args, name, cwd, repo, parent, depth)
    session_dir(name).mkdir(parents=True)
    save_meta(meta)
    n = begin_turn(name, prompt)
    report_or_background(meta, n, args)


def resolve_cwd(raw: str | None) -> str:
    cwd = str(Path(raw or os.getcwd()).resolve())
    if not Path(cwd).is_dir():
        die(f"cwd does not exist: {cwd}")
    return cwd


def resolve_new_session_name(raw: str | None, backend: str) -> str:
    name = raw or unique_name(backend)
    validate_session_name(name)
    if session_dir(name).exists():
        die(f"session '{name}' already exists")
    return name


def validate_start_agent(backend: str, agent: str | None) -> None:
    if agent and backend == "codex":
        die("--agent is only supported for pi (agents/*.md) and claude (custom agents)")
    if agent and backend == "pi":
        load_pi_agent(agent)


def git_result(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", repo, *args],
        capture_output=True, text=True,
    )


def require_git(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        die(f"{action}: {detail}")


def fetch_origin(repo: str) -> None:
    result = git_result(repo, "fetch", "-q", "origin")
    if result.returncode == 0:
        return
    if git_result(repo, "remote", "get-url", "origin").returncode != 0:
        return
    require_git(result, "git fetch origin failed")


def worktree_base(repo: str) -> str:
    result = git_result(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main")
    return "origin/main" if result.returncode == 0 else "HEAD"


def branch_exists(repo: str, branch: str) -> bool:
    ref = f"refs/heads/{branch}"
    return git_result(repo, "show-ref", "--verify", "--quiet", ref).returncode == 0


def create_worktree(repo: str, name: str, branch: str) -> str:
    require_git(git_result(repo, "check-ref-format", "--branch", branch), "invalid worktree branch")
    require_git(git_result(repo, "rev-parse", "--show-toplevel"), "worktree repository is not valid")
    fetch_origin(repo)
    root = WORKTREE_ROOT.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.exists():
        die(f"worktree path already exists: {target}")
    exists = branch_exists(repo, branch)
    argv = ["worktree", "add"]
    if not exists:
        argv += ["-b", branch]
    argv += [str(target), branch if exists else worktree_base(repo)]
    require_git(git_result(repo, *argv), "git worktree add failed")
    return str(target)


def new_session_parentage() -> tuple[str | None, int]:
    parent = os.environ.get("PAL_SESSION")
    if not parent:
        return None, 0
    validate_session_name(parent)
    try:
        depth = int(os.environ.get("PAL_DEPTH", "0")) + 1
    except ValueError:
        die("PAL_DEPTH must be a non-negative integer")
    if depth < 1:
        die("PAL_DEPTH must be a non-negative integer")
    if depth > 2:
        die("delegation depth would exceed 2; a depth-2 session cannot start a child")
    return parent, depth


def new_session_meta(
    args: argparse.Namespace, name: str, cwd: str, repo: str | None,
    parent: str | None, depth: int,
) -> dict:
    meta = {
        "name": name, "backend": args.backend, "cwd": cwd,
        "model": args.model or DEFAULT_MODELS.get(args.backend),
        "effort": args.effort, "agent": args.agent,
        "backend_session_id": None if args.backend == "codex" else str(uuid.uuid4()),
        "backend_initialized": False,
        "created": now_iso(), "status": "idle", "turns": [], "depth": depth,
    }
    if parent:
        meta["parent"] = parent
    if args.worktree:
        meta.update({"worktree": args.worktree, "repo": repo})
    return meta


def report_or_background(meta: dict, n: int, args: argparse.Namespace) -> None:
    if args.bg:
        print_background_receipt(meta, n, args.json)
        return
    report_after_wait(wait_for(meta["name"], args.timeout), n, args.json)


def print_background_receipt(meta: dict, n: int, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"session": meta["name"], "turn": n, "status": "running"}))
        return
    print(f"[pal {meta['name']} | {meta['backend']} | turn {n} | running in background]")


def cmd_say(args: argparse.Namespace) -> None:
    meta = load_meta(args.name)
    prompt = read_prompt(args.prompt, args.file)
    n = begin_turn(meta["name"], prompt)
    report_or_background(meta, n, args)


def cmd_wait(args: argparse.Namespace) -> None:
    deadline = time.time() + args.timeout
    statuses = set()
    for name in args.names:
        meta = wait_for(name, max(0.0, deadline - time.time()))
        if not meta["turns"]:
            continue
        print_turn(meta, len(meta["turns"]), args.json)
        statuses.add(meta["status"])
    exit_for_wait_statuses(statuses)


def exit_for_wait_statuses(statuses: set[str]) -> None:
    if "running" in statuses:
        sys.exit(124)
    if "error" in statuses:
        sys.exit(1)
    if "stopped" in statuses:
        sys.exit(130)


def cmd_read(args: argparse.Namespace) -> None:
    meta = load_meta(args.name)
    if not meta["turns"]:
        die("no turns yet")
    if args.all:
        print_full_transcript(meta)
        return
    n = selected_turn(meta, args.turn)
    print_turn(meta, n, args.json)
    exit_if_turn_failed(meta["turns"][n - 1])


def print_full_transcript(meta: dict) -> None:
    path = session_dir(meta["name"]) / "transcript.md"
    if not path.exists():
        die("transcript is not available until the first turn finishes")
    print(path.read_text())


def selected_turn(meta: dict, requested: int | None) -> int:
    n = requested or len(meta["turns"])
    if n < 1 or n > len(meta["turns"]):
        die(f"turn {n} out of range 1..{len(meta['turns'])}")
    return n


def exit_if_turn_failed(turn: dict) -> None:
    if turn.get("status") == "error":
        sys.exit(turn.get("rc") or 1)


def cmd_log(args: argparse.Namespace) -> None:
    meta = load_meta(args.name)
    turns = [args.turn] if args.turn else range(1, len(meta["turns"]) + 1)
    for n in turns:
        print_turn_log(meta, n)
    if args.stderr:
        print("--- stderr (last turn) ---")
        print(tail_text(turn_paths(meta["name"], len(meta["turns"]))["stderr"], 4000))


def print_turn_log(meta: dict, n: int) -> None:
    parsed = PARSERS[meta["backend"]](parse_jsonl(turn_paths(meta["name"], n)["events"]))
    print(f"--- turn {n} ({meta['turns'][n - 1].get('status')}) ---")
    for line in parsed["activity"]:
        print(line[:300])
    for error in parsed["errors"]:
        print(f"! {error[:300]}")


def cmd_ls(args: argparse.Namespace) -> None:
    all_metas = all_sessions()
    metas = visible_sessions(all_metas, args.all)
    hidden = len(all_metas) - len(metas)
    if args.json:
        print_sessions_json(metas)
        print_hidden_sessions(hidden, stream=sys.stderr)
        return
    if not metas:
        print("no sessions")
        print_hidden_sessions(hidden)
        return
    if args.tree:
        print_tree_sessions(metas)
    elif shutil.get_terminal_size().columns < 100:
        print_narrow_sessions(metas)
    else:
        print_wide_sessions(metas)
    print_hidden_sessions(hidden)


def updated_datetime(meta: dict) -> datetime:
    return datetime.fromisoformat(meta["updated"].replace("Z", "+00:00"))


def visible_sessions(metas: list[dict], show_all: bool) -> list[dict]:
    if show_all:
        return metas
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return [meta for meta in metas if meta.get("status") == "running" or updated_datetime(meta) >= cutoff]


def print_hidden_sessions(hidden: int, stream=None) -> None:
    if hidden:
        print(f"{hidden} older sessions hidden (pal ls --all)", file=stream or sys.stdout)


def print_sessions_json(metas: list[dict]) -> None:
    fields = ("name", "backend", "model", "cwd", "status", "updated", "agent", "parent", "worktree", "repo")
    sessions = [
        {key: meta.get(key) for key in fields}
        | {"turns": len(meta["turns"]), "metrics": session_metrics(meta)}
        for meta in metas
    ]
    print(json.dumps(sessions, indent=2))


def print_wide_sessions(metas: list[dict]) -> None:
    print(
        f"{'NAME':<22} {'BACKEND':<7} {'MODEL':<18} {'PARENT':<22} {'TURNS':>5} "
        f"{'STATUS':<8} {'TOKENS':>12} {'COST':>13} {'UPDATED':<20} CWD"
    )
    for m in metas:
        metrics = session_metrics(m)
        token_total = metrics["input_tokens"] + metrics["output_tokens"]
        cost = format_cost(metrics["cost"], metrics["cost_unknown"])
        print(
            f"{m['name'][:22]:<22} {m['backend']:<7} {(m.get('model') or 'default')[:18]:<18} "
            f"{(m.get('parent') or '-')[:22]:<22} {len(m['turns']):>5} {m['status']:<8} "
            f"{token_total:>12,} {cost:>13} {m.get('updated', ''):<20} {m['cwd']}"
        )


def print_narrow_sessions(metas: list[dict]) -> None:
    for meta in metas:
        metrics = session_metrics(meta)
        print(
            f"name={meta['name']} backend={meta['backend']} status={meta['status']} "
            f"parent={meta.get('parent') or '-'} turns={len(meta['turns'])} "
            f"{format_tokens(metrics)} {format_cost(metrics['cost'], metrics['cost_unknown'])} "
            f"cwd={meta['cwd']}"
        )


def child_map(metas: list[dict]) -> dict[str, list[dict]]:
    children: dict[str, list[dict]] = {}
    for meta in metas:
        children.setdefault(meta.get("parent") or "", []).append(meta)
    return children


def print_tree_branch(meta: dict, children: dict[str, list[dict]], depth: int, seen: set[str]) -> None:
    if meta["name"] in seen:
        return
    seen.add(meta["name"])
    metrics = session_metrics(meta)
    print(
        f"{'  ' * depth}{meta['name']} [{meta['status']}, {meta['backend']}, "
        f"{format_tokens(metrics)}, {format_cost(metrics['cost'], metrics['cost_unknown'])}]"
    )
    for child in children.get(meta["name"], []):
        print_tree_branch(child, children, depth + 1, seen)


def print_tree_sessions(metas: list[dict]) -> None:
    names = {meta["name"] for meta in metas}
    children = child_map(metas)
    roots = [meta for meta in metas if meta.get("parent") not in names]
    seen: set[str] = set()
    for meta in roots + metas:
        print_tree_branch(meta, children, 0, seen)


def cmd_status(args: argparse.Namespace) -> None:
    meta = load_meta(args.name)
    metas = all_sessions()
    children = [item["name"] for item in metas if item.get("parent") == args.name]
    meta["children"] = children
    meta["rollup"] = rolled_up_metrics(args.name, metas)
    print(json.dumps(meta, indent=2))


def rolled_up_metrics(name: str, metas: list[dict]) -> dict:
    children = child_map(metas)
    by_name = {meta["name"]: meta for meta in metas}
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
             "cost": 0.0, "cost_unknown": False}
    stack = [by_name[name]]
    seen: set[str] = set()
    while stack:
        meta = stack.pop()
        if meta["name"] in seen:
            continue
        seen.add(meta["name"])
        add_metrics(total, session_metrics(meta))
        stack.extend(children.get(meta["name"], []))
    return total


def cmd_stop(args: argparse.Namespace) -> None:
    with edit_meta(args.name) as current:
        killed = [pid for pid in (stop_pid(current.get("pid")), stop_pid(current.get("runner_pid"))) if pid]
        if current["status"] == "running":
            mark_session_stopped(current)
        state = current["status"]
    print(f"[pal {args.name} | {state} | killed {killed or 'nothing'}]")


def stop_pid(pid: int | None) -> int | None:
    if not pid_alive(pid):
        return None
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        os.kill(pid, signal.SIGTERM)
    return pid


def mark_session_stopped(meta: dict) -> None:
    meta["status"] = "stopped"
    meta["turns"][-1].update({"status": "stopped", "finished": now_iso()})
    meta["pid"] = meta["runner_pid"] = None


def cmd_rm(args: argparse.Namespace) -> None:
    validate_rm_options(args)
    for name in args.names:
        meta = load_meta(name)
        if meta["status"] == "running":
            die(f"'{name}' is running; stop it first")
        if args.worktree:
            remove_worktree(meta, args.force)
        elif meta.get("worktree"):
            print(f"[pal kept worktree {meta['cwd']}]")
        shutil.rmtree(session_dir(name))
        print(f"[pal removed {name}]")


def validate_rm_options(args: argparse.Namespace) -> None:
    if args.force and not args.worktree:
        die("--force requires --worktree")


def remove_worktree(meta: dict, force: bool) -> None:
    require_recorded_worktree(meta)
    status = git_result(meta["cwd"], "status", "--porcelain")
    require_git(status, "cannot inspect worktree")
    if status.stdout.strip() and not force:
        die(f"worktree for '{meta['name']}' is dirty; use --force to remove it")
    argv = ["worktree", "remove"]
    if force:
        argv.append("--force")
    require_git(git_result(meta["repo"], *argv, meta["cwd"]), "git worktree remove failed")


def require_recorded_worktree(meta: dict) -> None:
    if not meta.get("worktree"):
        die(f"'{meta['name']}' has no recorded worktree")
    if not meta.get("repo"):
        die(f"'{meta['name']}' has no recorded source repo")


def archive_candidates(days: float) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        meta for meta in all_sessions()
        if meta.get("status") in ("idle", "stopped") and updated_datetime(meta) < cutoff
    ]


def ensure_archive_targets(metas: list[dict]) -> None:
    for meta in metas:
        target = ARCHIVE / meta["name"]
        if target.exists():
            die(f"archive target already exists: {target}")


def cmd_archive(args: argparse.Namespace) -> None:
    metas = archive_candidates(args.older_than)
    if not metas:
        print("no sessions to archive")
        return
    if args.dry_run:
        for meta in metas:
            print(f"[pal would archive {meta['name']}]")
        return
    ensure_archive_targets(metas)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for meta in metas:
        session_dir(meta["name"]).rename(ARCHIVE / meta["name"])
        print(f"[pal archived {meta['name']}]")


def session_metas_under(root: Path) -> list[dict]:
    if not root.exists():
        return []
    paths = sorted(root.glob("*/meta.json"))
    return [read_json(path) for path in paths]


def known_session_metas() -> list[dict]:
    return all_sessions() + session_metas_under(ARCHIVE)


def named_for_running(value: str, running: set[str]) -> bool:
    return any(value == name or value.startswith(f"{name}-") for name in running)


def run_simctl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["xcrun", "simctl", *args], capture_output=True, text=True,
        )
    except OSError as error:
        die(f"xcrun simctl failed: {error}")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        die(f"xcrun simctl {' '.join(args)} failed: {detail}")
    return result


def simulator_groups(raw: str) -> list[list[dict]]:
    try:
        devices = json.loads(raw).get("devices")
    except (json.JSONDecodeError, AttributeError) as error:
        die(f"invalid simctl device list: {error}")
    if not isinstance(devices, dict) or not all(isinstance(group, list) for group in devices.values()):
        die("invalid simctl device list: expected devices by runtime")
    return list(devices.values())


def pal_simulators(raw: str, running: set[str]) -> list[dict]:
    candidates = []
    for device in sum(simulator_groups(raw), []):
        name = pal_simulator_name(device)
        if not name:
            continue
        checked = checked_simulator(device, name, running)
        if checked:
            candidates.append(checked)
    return candidates


def pal_simulator_name(device: object) -> str | None:
    if not isinstance(device, dict):
        return None
    name = device.get("name")
    if not isinstance(name, str):
        return None
    return name if name.startswith("pal-") else None


def checked_simulator(device: dict, name: str, running: set[str]) -> dict | None:
    if named_for_running(name.removeprefix("pal-"), running):
        return None
    if not isinstance(device.get("udid"), str):
        die(f"invalid simctl device entry for {name}: missing UDID")
    if simulator_data_in_worktree(device):
        return None
    return device


def simulator_data_in_worktree(device: dict) -> bool:
    data_path = device.get("dataPath")
    if not isinstance(data_path, str):
        return False
    path = Path(data_path)
    return path.is_dir() and inside_git_worktree(path)


def inside_git_worktree(path: Path) -> bool:
    result = git_result(str(path), "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        die(f"cannot measure {path}: {detail}")
    try:
        return int(result.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        die(f"cannot measure {path}: unexpected du output")


def simulator_size(device: dict) -> int:
    data_path = device.get("dataPath")
    return path_size(Path(data_path)) if isinstance(data_path, str) else 0


def cleanup_directories(metas: list[dict], running: set[str], hours: float) -> list[Path]:
    cutoff = time.time() - hours * 3600
    exact = {TMP_ROOT / f"{meta['name']}-dd" for meta in metas if meta["name"] not in running}
    generic = set(generic_cleanup_directories(cutoff))
    return sorted(path for path in exact | generic if safe_cleanup_directory(path, running))


def generic_cleanup_directories(cutoff: float) -> list[Path]:
    if not TMP_ROOT.exists():
        return []
    return [
        path for path in TMP_ROOT.iterdir()
        if (path.name.endswith("-dd") or "prior-art" in path.name)
        and path.stat().st_mtime < cutoff
    ]


def safe_cleanup_directory(path: Path, running: set[str]) -> bool:
    if not path.is_dir() or path.is_symlink() or named_for_running(path.name, running):
        return False
    return not inside_git_worktree(path)


def remove_simulator(device: dict, dry_run: bool) -> int:
    size = simulator_size(device)
    verb = "would remove" if dry_run else "removed"
    if not dry_run:
        if device.get("state") == "Booted":
            run_simctl("shutdown", device["udid"])
        run_simctl("delete", device["udid"])
    print(f"[pal {verb} simulator {device['name']} ({size} bytes)]")
    return size


def remove_cleanup_directory(path: Path, dry_run: bool) -> int:
    size = path_size(path)
    verb = "would remove" if dry_run else "removed"
    if not dry_run:
        shutil.rmtree(path)
    print(f"[pal {verb} {path} ({size} bytes)]")
    return size


def cmd_gc(args: argparse.Namespace) -> None:
    metas = known_session_metas()
    running = {meta["name"] for meta in all_sessions() if meta.get("status") == "running"}
    simulators = pal_simulators(run_simctl("list", "devices", "-j").stdout, running)
    directories = cleanup_directories(metas, running, args.older_than)
    freed = sum(remove_simulator(device, args.dry_run) for device in simulators)
    freed += sum(remove_cleanup_directory(path, args.dry_run) for path in directories)
    verb = "would free" if args.dry_run else "freed"
    print(f"[pal gc {verb} {freed} bytes]")


def cmd_diff(args: argparse.Namespace) -> None:
    meta = load_meta(args.name)
    git = ["git", "-c", "core.fsmonitor=false", "-C", meta["cwd"]]
    subprocess.run(git + ["status", "--short"])
    subprocess.run(git + (["diff"] if args.full else ["diff", "--stat"]))


# ---------- argparse ----------

def add_turn_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
    p.add_argument("-f", "--file", action="append", default=[], help="read prompt from file (repeatable)")
    p.add_argument("--bg", action="store_true", help="return immediately; poll with 'pal wait'")
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the reply (default 1800)")
    p.add_argument("--json", action="store_true")


def nonnegative_days(raw: str) -> float:
    days = float(raw)
    if days < 0:
        raise argparse.ArgumentTypeError("days must be non-negative")
    return days


def nonnegative_hours(raw: str) -> float:
    hours = float(raw)
    if hours < 0:
        raise argparse.ArgumentTypeError("hours must be non-negative")
    return hours


def build_parser() -> argparse.ArgumentParser:
    examples = """examples:
  pal start codex -n implement -C /path/to/repo "Implement and verify the task"
  pal start pi --agent implementer -n worker -C /path/to/repo "Implement the task"
  pal wait implement worker
  pal log implement
  pal diff implement --full
  pal say implement "Review the failure and fix it"
  pal read implement --all

The session name is the durable orchestrator handle. Use `say` for every
follow-up so the backend continues its native thread; inspect `log`, `diff`,
and the target files before accepting an agent's completion claim.
"""
    ap = argparse.ArgumentParser(
        prog="pal", description=__doc__, epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="create a session and send the first message")
    p.add_argument("backend", choices=BACKENDS)
    p.add_argument("-n", "--name", help="session name (default: <backend>-xxxx)")
    p.add_argument("-C", "--cwd", help="working directory for the agent (default: current)")
    p.add_argument("-m", "--model", help="model (codex: e.g. gpt-5.6-sol; pi: provider/id; claude: sonnet|opus|fable)")
    p.add_argument("--effort", help="reasoning: codex low|medium|high|xhigh; pi thinking level; claude effort")
    p.add_argument("--agent", help="pi: name of ~/.pi-x/agent/agents/<name>.md; claude: custom agent name")
    p.add_argument("--worktree", metavar="BRANCH", help="create an isolated git worktree for BRANCH")
    p.add_argument("--no-brief", action="store_true", help="do not prepend the orchestration brief")
    add_turn_opts(p)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("say", help="send the next message to a session")
    p.add_argument("name")
    add_turn_opts(p)
    p.set_defaults(fn=cmd_say)

    p = sub.add_parser("wait", help="block until sessions finish, then print their latest replies")
    p.add_argument("names", nargs="+")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("read", help="print a reply (default: latest)")
    p.add_argument("name")
    p.add_argument("-t", "--turn", type=int)
    p.add_argument("--all", action="store_true", help="print the whole transcript")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("log", help="show tool activity (commands run, files touched)")
    p.add_argument("name")
    p.add_argument("-t", "--turn", type=int)
    p.add_argument("--stderr", action="store_true", help="also show backend stderr for the last turn")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("ls", help="list sessions")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include sessions older than 24 hours")
    p.add_argument("--tree", action="store_true", help="show parent/child sessions as an indented tree")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("status", help="dump session metadata")
    p.add_argument("name")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop", help="kill a running turn")
    p.add_argument("name")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("rm", help="delete sessions")
    p.add_argument("names", nargs="+")
    p.add_argument("--worktree", action="store_true", help="also remove the recorded git worktree")
    p.add_argument("--force", action="store_true", help="allow removal of a dirty recorded worktree")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("archive", help="move old idle or stopped sessions into ~/.pal/archive")
    p.add_argument("--older-than", type=nonnegative_days, default=2.0, metavar="DAYS")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser("gc", help="remove stale PAL simulators and temporary build data")
    p.add_argument("--older-than", type=nonnegative_hours, default=6.0, metavar="HOURS")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_gc)

    p = sub.add_parser("diff", help="git status + diff --stat in the session's cwd")
    p.add_argument("name")
    p.add_argument("--full", action="store_true", help="full diff instead of --stat")
    p.set_defaults(fn=cmd_diff)

    return ap


def main() -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 4 and sys.argv[1] == "_run":
        execute_turn(sys.argv[2], int(sys.argv[3]))
        return
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
