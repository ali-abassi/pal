"""Shared checkout coordination. Locks cover this PAL_HOME, not external editors."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path


class SharedError(Exception):
    """A coordination boundary failed; do not start or apply work."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SharedError(message)


def lead_only() -> None:
    require(not os.environ.get("PAL_SESSION"), "shared mutations are lead-only")


def digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def git_path(cwd: str, flag: str) -> Path:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", cwd, "rev-parse", flag],
        capture_output=True, text=True,
    )
    require(result.returncode == 0, f"shared mode requires a Git checkout: {result.stderr.strip()}")
    return (Path(cwd) / result.stdout.strip()).resolve()


def path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def overlaps(left: str, right: str) -> bool:
    a, b = path_key(left).rstrip("/"), path_key(right).rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def checked_path(repo: str, relative: str) -> Path:
    require(isinstance(relative, str) and bool(relative), "file path must be a nonempty string")
    parts = relative.split("/")
    require(all(p not in ("", ".", "..") for p in parts), "use exact relative paths without . or ..")
    require(all(path_key(p) != ".git" for p in parts), "Git metadata cannot be reserved")
    require("\x00" not in relative, "invalid path")
    target = Path(repo)
    for part in parts:
        target = target / part
        require(not target.is_symlink(), f"symlink path is unsupported: {relative}")
    require(target.parent.is_dir(), f"create parent directories before reserving: {relative}")
    return target


def fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f"requires a regular, unlinked file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(repo: str, paths: list[str]) -> dict:
    require(bool(paths), "--shared requires at least one --files PATH")
    result = {}
    for relative in paths:
        target = checked_path(repo, relative)
        require(not any(overlaps(relative, other) for other in result), f"overlapping file: {relative}")
        result[relative] = fingerprint(target)
    return result


def sandbox_command(meta: dict, home: Path, command: list[str]) -> list[str]:
    require(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
            "shared mode requires macOS /usr/bin/sandbox-exec; no advisory fallback")
    roots = [Path(meta["cwd"]), Path(meta["shared_git"]), home.resolve() / "shared"]
    denies = [f"(deny file-write* (subpath {json.dumps(str(root))}))" for root in roots]
    ancestors = protected_ancestors(roots)
    denies += [f"(deny file-write-unlink (literal {json.dumps(str(p))}))" for p in sorted(ancestors)]
    profile = "(version 1)(allow default)" + "".join(denies)
    return ["/usr/bin/sandbox-exec", "-p", profile, *command]


def protected_ancestors(roots: list[Path]) -> set[Path]:
    return {parent for root in roots for parent in root.parents}


def first_conflict(files: dict, existing: dict) -> str | None:
    return next((p for p in files for other in existing if overlaps(p, other)), None)


def context_board(board: dict, owner: str) -> dict:
    tasks = {name: task_context(name, task, owner) for name, task in board["tasks"].items()
             if task["state"] != "released"}
    return {"repo": board["repo"], "revision": board["revision"], "tasks": tasks}


def task_context(name: str, task: dict, owner: str) -> dict:
    if name == owner:
        return task
    return {"task": task["task"][:500], "files": list(task["files"]), "state": task["state"]}


def preflight(meta: dict, home: Path) -> None:
    result = subprocess.run(sandbox_command(meta, home, ["/usr/bin/true"]), capture_output=True, text=True)
    require(result.returncode == 0, f"cannot enforce shared write guard: {result.stderr.strip()}")


class Coordinator:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.root = self.home / "shared"

    @contextlib.contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def board_path(self, repo: str) -> Path:
        return self.root / (hashlib.sha256(repo.encode()).hexdigest() + ".json")

    def board(self, repo: str) -> dict:
        path = self.board_path(repo)
        if path.exists():
            return json.loads(path.read_text())
        return {"repo": repo, "revision": 0, "tasks": {}, "pending": None}

    def save(self, board: dict) -> None:
        board["revision"] += 1
        atomic_write(self.board_path(board["repo"]), json.dumps(board, indent=2).encode())

    def reserve(self, meta: dict, files: list[str], task: str) -> None:
        lead_only()
        repo = meta["cwd"]
        board = self.board(repo)
        require(board["pending"] is None, "recover pending application before reserving")
        require(meta["name"] not in board["tasks"], "shared session name was already used; choose a new name")
        claims = snapshot(repo, files)
        self.reject_conflicts(board, claims)
        self.reject_classic(repo)
        board["tasks"][meta["name"]] = {"task": task[:4000], "files": claims, "state": "reserved"}
        self.save(board)

    def reject_conflicts(self, board: dict, files: dict) -> None:
        for owner, task in board["tasks"].items():
            if task["state"] == "released":
                continue
            conflict = first_conflict(files, task["files"])
            require(conflict is None, f"file reserved by {owner}: {conflict}")

    def reject_classic(self, repo: str) -> None:
        for path in (self.home / "sessions").glob("*/meta.json"):
            meta = json.loads(path.read_text())
            if meta.get("shared") or meta.get("status") != "running":
                continue
            require(not overlaps(repo, str(Path(meta["cwd"]).resolve())),
                    f"stop classic session {meta['name']} before shared work")

    def check_turn(self, meta: dict) -> None:
        if meta.get("shared"):
            self.active_task(self.board(meta["cwd"]), meta["name"])
            self.reject_classic(meta["cwd"])
            return
        for path in self.root.glob("*.json"):
            board = json.loads(path.read_text())
            active = any(t["state"] != "released" for t in board["tasks"].values())
            require(not (active and overlaps(meta["cwd"], board["repo"])),
                    f"shared reservations block classic turns in {board['repo']}")

    def active_task(self, board: dict, name: str) -> dict:
        require(name in board["tasks"], f"no shared reservation for {name}")
        task = board["tasks"][name]
        require(task["state"] != "released", "reservation released; start a new shared session")
        require(board["pending"] is None, "pending application; lead must run shared recover")
        return task

    def prompt(self, meta: dict, prompt: str) -> str:
        board = self.board(meta["cwd"])
        self.active_task(board, meta["name"])
        return (
            "SHARED CHECKOUT CONTRACT (applies on every turn):\n"
            "You are a read-only worker. Do not write repository files, run Git mutations, "
            "install dependencies, delegate, or invoke external services to edit files. "
            "The lead owns integration, tests with build outputs, Git and acceptance. "
            "Other workers may be active. Read relevant files; return your proposal as a JSON "
            "object only: {\"summary\":\"reason\",\"changes\":[{\"path\":\"owned/path\","
            "\"before_sha256\":\"hash from board\",\"content\":\"complete new UTF-8 text\"}]}. "
            "Use null before_sha256 for a new file; null content deletes a file. "
            "Only propose your exact reserved paths. If dependencies changed or you need "
            "another path, report the blocker instead of guessing or editing around it. "
            "The lead reviews all proposals; completion is not acceptance. "
            "Board snapshots refresh each turn. During a long turn you can read updates using "
            "pal shared board -C followed by the checkout path.\n"
            f"Your session: {meta['name']}\nShared board:\n{json.dumps(context_board(board, meta['name']))}\n\n{prompt}"
        )

    def ensure_idle(self, name: str) -> None:
        path = self.home / "sessions" / name / "meta.json"
        if path.exists():
            meta = json.loads(path.read_text())
            require(meta["status"] != "running", f"wait or stop {name} first")

    def release(self, repo: str, name: str) -> dict:
        lead_only()
        board = self.board(repo)
        task = self.active_task(board, name)
        self.ensure_idle(name)
        task["state"] = "released"
        self.save(board)
        return board

    def prepare(self, board: dict, name: str, proposal: dict) -> list[dict]:
        task = self.active_task(board, name)
        require(isinstance(proposal, dict), "proposal must be a JSON object")
        changes = proposal.get("changes")
        require(isinstance(changes, list) and 0 < len(changes) <= 100, "proposal requires 1–100 changes")
        prepared = [self.prepare_change(board["repo"], task, change) for change in changes]
        require(len({c["path"] for c in prepared}) == len(prepared), "duplicate proposal path")
        return prepared

    def prepare_change(self, repo: str, task: dict, change: dict) -> dict:
        require(isinstance(change, dict), "each change must be an object")
        require(set(change) == {"path", "before_sha256", "content"}, "unexpected or missing change fields")
        relative, content = change["path"], change["content"]
        require(isinstance(relative, str) and relative in task["files"], f"unowned path: {relative}")
        require(content is None or isinstance(content, str), "content must be UTF-8 text or null")
        before = task["files"][relative]
        require(change["before_sha256"] == before, f"proposal baseline mismatch: {relative}")
        path = checked_path(repo, relative)
        require(fingerprint(path) == before, f"stale file; release and reserve again: {relative}")
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        return dict(change, after_sha256=digest(content), mode=mode)

    def apply(self, repo: str, name: str, proposal: dict) -> dict:
        lead_only()
        self.ensure_idle(name)
        board = self.board(repo)
        changes = self.prepare(board, name, proposal)
        board["pending"] = {"name": name, "changes": changes}
        self.save(board)
        return self.recover(repo)

    def recover(self, repo: str) -> dict:
        lead_only()
        board = self.board(repo)
        pending = board["pending"]
        require(pending is not None, "no pending application")
        self.ensure_idle(pending["name"])
        for change in pending["changes"]:
            self.recovery_path(repo, change)
        for change in pending["changes"]:
            self.publish_change(repo, change)
        self.finish_application(board)
        return board

    def recovery_path(self, repo: str, change: dict) -> Path:
        path = checked_path(repo, change["path"])
        require(fingerprint(path) in (change["before_sha256"], change["after_sha256"]),
                f"external edit blocks recovery: {change['path']}; preserve journal and reconcile manually")
        return path

    def publish_change(self, repo: str, change: dict) -> None:
        path = self.recovery_path(repo, change)
        if fingerprint(path) == change["after_sha256"]:
            return
        if change["content"] is None:
            path.unlink()
            sync_directory(path.parent)
            return
        atomic_write(path, change["content"].encode("utf-8"), change["mode"])

    def finish_application(self, board: dict) -> None:
        pending = board["pending"]
        task = board["tasks"][pending["name"]]
        task["files"].update({c["path"]: c["after_sha256"] for c in pending["changes"]})
        task["state"] = "applied"
        task["last_application"] = {c["path"]: c["after_sha256"] for c in pending["changes"]}
        board["pending"] = None
        self.save(board)
