from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PAL = Path(__file__).parents[1] / "pal.py"
MCP_SERVER = Path(__file__).parents[1] / "mcp_server.py"
FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
sys.stdin.read()
session_id = args[args.index("resume") + 1] if "resume" in args else "codex-test-thread"
with open(os.environ["PAL_FAKE_TRACE"], "a") as stream:
    stream.write(json.dumps({
        "cwd": os.getcwd(), "pal_session": os.environ.get("PAL_SESSION"),
        "pal_depth": os.environ.get("PAL_DEPTH"),
    }) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": session_id}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300
}}))
'''


class PalBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "pal-home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace.jsonl"
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX)
        codex.chmod(0o755)
        self.env = os.environ | {
            "PAL_HOME": str(self.home),
            "PAL_WORKTREE_ROOT": str(self.root / "worktrees"),
            "PAL_TMP_ROOT": str(self.root / "private-tmp"),
            "PAL_FAKE_TRACE": str(self.trace),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        }
        self.env.pop("PAL_SESSION", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_pal(
        self, *args: str, env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PAL), *args], env=env or self.env, text=True,
            capture_output=True, timeout=30,
        )

    def meta(self, name: str) -> dict:
        return json.loads((self.home / "sessions" / name / "meta.json").read_text())

    def fixture(self, name: str, status: str, updated: str) -> None:
        directory = self.home / "sessions" / name
        directory.mkdir(parents=True)
        meta = {
            "name": name, "backend": "codex", "model": "gpt-test",
            "cwd": str(self.root), "status": status, "updated": updated,
            "agent": None, "turns": [],
        }
        (directory / "meta.json").write_text(json.dumps(meta))

    def init_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "pal@example.com"],
            ["git", "config", "user.name", "Pal Test"],
        ):
            subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)
        (repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        return repo

    def test_worktree_path_and_branch_creation(self) -> None:
        repo = self.init_repo()
        self.env.pop("PAL_WORKTREE_ROOT")
        result = self.run_pal(
            "start", "codex", "-n", "isolated", "-C", str(repo),
            "--worktree", "feature/pal-test", "--no-brief", "work",
        )

        worktree = (self.home / "worktrees" / "isolated").resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = self.meta("isolated")
        branch = subprocess.run(
            ["git", "-C", str(worktree), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(branch, "feature/pal-test")
        self.assertEqual(meta["cwd"], str(worktree))
        self.assertEqual(meta["repo"], str(repo.resolve()))
        self.assertEqual(meta["worktree"], "feature/pal-test")
        self.assertEqual(meta["depth"], 0)
        self.assertEqual(json.loads(self.trace.read_text().splitlines()[0])["pal_depth"], "0")

    def test_parent_recorded_from_pal_session(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fixture("parent-session", "idle", now)
        child_env = self.env | {"PAL_SESSION": "parent-session", "PAL_DEPTH": "0"}
        child = self.run_pal(
            "start", "codex", "-n", "child-session", "--no-brief", "work", env=child_env,
        )
        grandchild_env = self.env | {"PAL_SESSION": "child-session", "PAL_DEPTH": "1"}
        grandchild = self.run_pal(
            "start", "codex", "-n", "grandchild-session", "--no-brief", "work",
            env=grandchild_env,
        )
        refused = self.run_pal(
            "start", "codex", "-n", "too-deep", "--no-brief", "work",
            env=self.env | {"PAL_SESSION": "grandchild-session", "PAL_DEPTH": "2"},
        )
        tree = self.run_pal("ls", "--tree")
        status = json.loads(self.run_pal("status", "parent-session").stdout)

        traces = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(grandchild.returncode, 0, grandchild.stderr)
        self.assertEqual(
            (self.meta("child-session")["parent"], self.meta("child-session")["depth"]),
            ("parent-session", 1),
        )
        self.assertEqual(self.meta("grandchild-session")["depth"], 2)
        self.assertEqual(traces[0]["pal_depth"], "1")
        self.assertEqual(traces[1]["pal_depth"], "2")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("depth would exceed 2", refused.stderr)
        self.assertFalse((self.home / "sessions" / "too-deep").exists())
        self.assertIn("\n  child-session", tree.stdout)
        self.assertEqual(status["children"], ["child-session"])
        self.assertEqual(status["rollup"]["input_tokens"], 2400)

    def test_codex_usage_tokens_and_unknown_cost(self) -> None:
        self.home.mkdir()
        (self.home / "prices.json").write_text(json.dumps({
            "some-other-model": {
                "placeholder": False,
                "input_usd_per_million": 1,
                "cached_input_usd_per_million": 1,
                "output_usd_per_million": 1,
            },
        }))
        started = self.run_pal(
            "start", "codex", "-n", "usage", "-m", "missing-model",
            "--no-brief", "work",
        )
        read = self.run_pal("read", "usage")
        listed = self.run_pal("ls")

        turn = self.meta("usage")["turns"][0]
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(
            (turn["input_tokens"], turn["cached_input_tokens"], turn["output_tokens"]),
            (1200, 200, 300),
        )
        self.assertIsNone(turn["cost"])
        self.assertIn("tokens 1,200 in/200 cached/300 out", read.stdout)
        self.assertIn("cost unknown", read.stdout)
        self.assertIn("cost unknown", listed.stdout)

    def test_session_lifecycle_listing_archive_and_gc(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fixture("recent", "idle", now)
        self.fixture("idle-old", "idle", "2020-01-01T00:00:00Z")
        self.fixture("stopped-old", "stopped", "2020-01-01T00:00:00Z")
        self.fixture("running-old", "running", "2020-01-01T00:00:00Z")

        recent = self.run_pal("ls")
        all_sessions = self.run_pal("ls", "--all")
        archived = self.run_pal("archive", "--older-than", "2")
        tmp = self.root / "private-tmp"
        tmp.mkdir()
        stale_data = tmp / "stale-simulator-data"
        running_data = tmp / "running-simulator-data"
        for path in (stale_data, running_data, tmp / "idle-old-dd", tmp / "running-old-dd",
                     tmp / "orphan-dd", tmp / "scan-prior-art-old", tmp / "recent-prior-art"):
            path.mkdir()
            (path / "data").write_bytes(b"x" * 1024)
        old_timestamp = 1_600_000_000
        for path in (tmp / "orphan-dd", tmp / "scan-prior-art-old"):
            os.utime(path, (old_timestamp, old_timestamp))
        git_prior_art = tmp / "repo-prior-art"
        git_prior_art.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=git_prior_art, check=True)
        os.utime(git_prior_art, (old_timestamp, old_timestamp))
        devices = {"devices": {"runtime": [
            {"name": "pal-idle-old-iPhone", "udid": "STALE", "state": "Booted", "dataPath": str(stale_data)},
            {"name": "pal-running-old-iPhone", "udid": "RUNNING", "state": "Booted", "dataPath": str(running_data)},
        ]}}
        xcrun = self.bin / "xcrun"
        xcrun.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ["simctl", "list", "devices", "-j"]:
    print(os.environ["PAL_SIM_DEVICES"])
else:
    with open(os.environ["PAL_SIM_TRACE"], "a") as stream:
        stream.write(json.dumps(args) + "\\n")
''')
        xcrun.chmod(0o755)
        sim_trace = self.root / "sim-trace.jsonl"
        gc_env = self.env | {
            "PAL_SIM_DEVICES": json.dumps(devices), "PAL_SIM_TRACE": str(sim_trace),
        }
        preview = self.run_pal("gc", "--older-than", "6", "--dry-run", env=gc_env)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue((tmp / "idle-old-dd").is_dir())
        self.assertFalse(sim_trace.exists())
        self.assertIn("pal gc would free", preview.stdout)
        collected = self.run_pal("gc", "--older-than", "6", env=gc_env)

        self.assertIn("name=recent", recent.stdout)
        self.assertIn("name=running-old", recent.stdout)
        self.assertNotIn("name=idle-old", recent.stdout)
        self.assertIn("2 older sessions hidden (pal ls --all)", recent.stdout)
        self.assertIn("name=idle-old", all_sessions.stdout)
        self.assertEqual(archived.returncode, 0, archived.stderr)
        self.assertTrue((self.home / "archive" / "idle-old").is_dir())
        self.assertTrue((self.home / "archive" / "stopped-old").is_dir())
        self.assertTrue((self.home / "sessions" / "running-old").is_dir())
        self.assertNotIn("running-old", archived.stdout)
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertFalse((tmp / "idle-old-dd").exists())
        self.assertFalse((tmp / "orphan-dd").exists())
        self.assertFalse((tmp / "scan-prior-art-old").exists())
        self.assertTrue((tmp / "running-old-dd").is_dir())
        self.assertTrue((tmp / "recent-prior-art").is_dir())
        self.assertTrue(git_prior_art.is_dir())
        sim_actions = [json.loads(line) for line in sim_trace.read_text().splitlines()]
        self.assertEqual(sim_actions, [["simctl", "shutdown", "STALE"], ["simctl", "delete", "STALE"]])
        self.assertIn("removed simulator pal-idle-old-iPhone", collected.stdout)
        self.assertIn("pal gc freed", collected.stdout)

    def test_native_session_continuation(self) -> None:
        started = self.run_pal("start", "codex", "-n", "resume", "--no-brief", "first")
        session_id = self.meta("resume")["backend_session_id"]
        continued = self.run_pal("say", "resume", "second")
        meta = self.meta("resume")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(meta["backend_session_id"], session_id)
        self.assertEqual(len(meta["turns"]), 2)
        self.assertEqual(meta["status"], "idle")

    def test_mcp_discovery_and_listing(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "pal_list", "arguments": {}}},
        ]
        server = subprocess.run(
            [sys.executable, str(MCP_SERVER)],
            input="\n".join(map(json.dumps, requests)) + "\n",
            env=self.env, text=True, capture_output=True, timeout=30,
        )
        responses = {r["id"]: r for r in map(json.loads, server.stdout.splitlines())}
        names = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertEqual(server.returncode, 0, server.stderr)
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "pal")
        self.assertIn("pal_start", names)
        self.assertIn("pal_status", names)
        self.assertEqual(responses[3]["result"]["content"][0]["text"], "no sessions")


if __name__ == "__main__":
    unittest.main()
