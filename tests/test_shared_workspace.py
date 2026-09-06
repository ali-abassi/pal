from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shared_workspace as shared
import mcp_server
import pal
import test_pal

PAL = Path(__file__).resolve().parents[1] / "pal.py"


def claim_in_process(home: str, repo: str, name: str) -> bool:
    coordinator = shared.Coordinator(Path(home))
    try:
        with coordinator.locked():
            coordinator.reserve({"cwd": repo, "name": name}, ["a.txt"], "task")
        return True
    except shared.SharedError:
        return False


class SharedFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "state"
        self.coordinator = shared.Coordinator(self.home)
        self.repo_s = str(self.repo)
        (self.repo / "a.txt").write_text("a")
        (self.repo / "b.txt").write_text("b")
        self.pal_session = patch.dict(os.environ)
        self.pal_session.start()
        os.environ.pop("PAL_SESSION", None)

    def tearDown(self):
        self.pal_session.stop()
        self.temp.cleanup()

    def reserve(self, name="one", files=None):
        with self.coordinator.locked():
            self.coordinator.reserve({"cwd": self.repo_s, "name": name}, files or ["a.txt"], name)

    def proposal(self, **changes):
        return {"changes": [{"path": name, "before_sha256": shared.fingerprint(self.repo / name),
                             "content": content} for name, content in changes.items()]}

    def apply(self, proposal, name="one"):
        with self.coordinator.locked():
            return self.coordinator.apply(self.repo_s, name, proposal)


class CoordinatorTests(SharedFixture):
    def test_atomic_claims_across_processes(self):
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
            futures = [pool.submit(claim_in_process, str(self.home), self.repo_s, str(i)) for i in range(4)]
            self.assertEqual(sum(f.result() for f in futures), 1)
        self.assertEqual(len(self.coordinator.board(self.repo_s)["tasks"]), 1)

    def test_scope_aliases_and_paths(self):
        self.reserve()
        for path in ["a.txt", "A.TXT"]:
            with self.subTest(path=path), self.assertRaises(shared.SharedError):
                self.reserve("two", [path])
        (self.repo / "link").symlink_to(self.root)
        os.link(self.repo / "b.txt", self.repo / "hard")
        for path in ["../outside", "/absolute", ".git/config", "link/escape", "hard", "missing/new"]:
            with self.subTest(path=path), self.assertRaises(shared.SharedError):
                self.reserve("bad", [path])
        self.reserve("two", ["new.txt"])

    def test_whole_proposal_validated_before_any_mutation(self):
        self.reserve(files=["a.txt", "b.txt"])
        proposal = self.proposal(**{"a.txt": "new a", "b.txt": "new b"})
        (self.repo / "b.txt").write_text("user edit")
        with self.assertRaisesRegex(shared.SharedError, "stale"):
            self.apply(proposal)
        self.assertEqual((self.repo / "a.txt").read_text(), "a")
        self.assertIsNone(self.coordinator.board(self.repo_s)["pending"])

    def test_unowned_duplicate_wrong_baseline_are_rejected(self):
        self.reserve()
        proposals = [self.proposal(**{"b.txt": "bad"}), {"changes": []}, {"changes": [None]}]
        normal = self.proposal(**{"a.txt": "changed"})
        proposals.append({"changes": normal["changes"] * 2})
        proposals.append({"changes": [dict(normal["changes"][0], before_sha256="wrong")]})
        for proposal in proposals:
            with self.subTest(proposal=proposal), self.assertRaises(shared.SharedError):
                self.apply(proposal)
        self.assertEqual((self.repo / "a.txt").read_text(), "a")

    def test_create_update_delete_and_explicit_release(self):
        self.reserve(files=["a.txt", "b.txt", "new.txt"])
        (self.repo / "a.txt").chmod(0o755)
        result = self.apply(self.proposal(**{"a.txt": "new", "b.txt": None, "new.txt": "hello"}))
        self.assertEqual((self.repo / "a.txt").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.repo / "new.txt").read_text(), "hello")
        self.assertFalse((self.repo / "b.txt").exists())
        self.assertEqual(result["tasks"]["one"]["state"], "applied")
        with self.assertRaises(shared.SharedError):
            self.reserve("two")
        with self.coordinator.locked():
            self.coordinator.release(self.repo_s, "one")
        self.reserve("two")
        with self.assertRaisesRegex(shared.SharedError, "released"):
            self.coordinator.check_turn({"cwd": self.repo_s, "name": "one", "shared": True})

    def interrupt_apply(self):
        self.reserve(files=["a.txt", "b.txt"])
        original = self.coordinator.publish_change
        def fail_second(repo, change):
            if change["path"] == "b.txt":
                raise OSError("simulated interruption")
            original(repo, change)
        with patch.object(self.coordinator, "publish_change", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.apply(self.proposal(**{"a.txt": "new a", "b.txt": "new b"}))

    def test_interrupted_application_recovers_idempotently(self):
        self.interrupt_apply()
        self.assertEqual((self.repo / "a.txt").read_text(), "new a")
        self.assertEqual((self.repo / "b.txt").read_text(), "b")
        with self.coordinator.locked():
            result = self.coordinator.recover(self.repo_s)
        self.assertIsNone(result["pending"])
        self.assertEqual((self.repo / "b.txt").read_text(), "new b")

    def test_external_edit_blocks_recovery_and_keeps_journal(self):
        self.interrupt_apply()
        (self.repo / "a.txt").write_text("human edit")
        with self.coordinator.locked(), self.assertRaisesRegex(shared.SharedError, "external edit"):
            self.coordinator.recover(self.repo_s)
        self.assertIsNotNone(self.coordinator.board(self.repo_s)["pending"])
        self.assertEqual((self.repo / "a.txt").read_text(), "human edit")
        self.assertEqual((self.repo / "b.txt").read_text(), "b")

    def test_shared_and_classic_turns_cannot_mix(self):
        self.reserve()
        with self.assertRaisesRegex(shared.SharedError, "block classic"):
            self.coordinator.check_turn({"cwd": self.repo_s})
        directory = self.home / "sessions" / "classic"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"name": "classic", "cwd": self.repo_s, "status": "running"}))
        with self.assertRaisesRegex(shared.SharedError, "stop classic"):
            self.reserve("two", ["b.txt"])

    def test_running_session_cannot_apply_or_release(self):
        self.reserve()
        directory = self.home / "sessions" / "one"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text('{"status":"running"}')
        with self.assertRaisesRegex(shared.SharedError, "wait or stop"):
            self.apply(self.proposal(**{"a.txt": "new"}))
        with self.coordinator.locked(), self.assertRaises(shared.SharedError):
            self.coordinator.release(self.repo_s, "one")

    def test_worker_mutations_and_unsupported_platform_fail_closed(self):
        with patch.dict(os.environ, {"PAL_SESSION": "worker"}), self.assertRaisesRegex(shared.SharedError, "lead-only"):
            self.reserve()
        with patch.object(shared.sys, "platform", "linux"), self.assertRaisesRegex(shared.SharedError, "no advisory fallback"):
            shared.sandbox_command({}, self.home, ["true"])


@unittest.skipUnless(sys.platform == "darwin", "native macOS Seatbelt boundary")
class SeatbeltTests(SharedFixture):
    def test_all_backend_launches_inherit_the_guard(self):
        (self.repo / ".git").mkdir()
        directory = self.home / "sessions" / "one"
        directory.mkdir(parents=True)
        (self.home / "shared").mkdir()
        script = 'from pathlib import Path; Path("a.txt").write_text("bad")'
        def builder(meta, paths):
            return [sys.executable, "-c", script], None
        for backend in ("codex", "pi", "claude"):
            meta = {"name": "one", "status": "running", "backend": backend,
                    "cwd": self.repo_s, "shared": True, "shared_git": str(self.repo / ".git")}
            (directory / "meta.json").write_text(json.dumps(meta))
            paths = {"events": self.root / "events", "stderr": self.root / "stderr"}
            with patch.object(pal, "PAL_HOME", self.home), patch.object(pal, "SESSIONS", self.home / "sessions"), patch.dict(pal.BUILDERS, {backend: builder}):
                self.assertNotEqual(pal.launch_backend(meta, paths, "task"), 0, backend)
            self.assertIn("Operation not permitted", paths["stderr"].read_text())
        self.assertEqual((self.repo / "a.txt").read_text(), "a")

    def test_native_descendant_git_state_and_alias_write_denial(self):
        (self.repo / ".git").mkdir()
        (self.home / "shared").mkdir(parents=True)
        external_git = self.root / "git-common"
        external_git.mkdir()
        (external_git / "config").write_text("original")
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        meta = {"cwd": self.repo_s, "shared_git": str(external_git)}
        attempts = [self.repo / "a.txt", self.repo / "new", alias / "a.txt", external_git / "config", self.home / "shared" / "fake.json"]
        for target in attempts:
            code = 'from pathlib import Path; Path(' + repr(str(target)) + ').write_text("bad")'
            command = shared.sandbox_command(meta, self.home, ["/bin/sh", "-c", 'exec "$@"', "sh", sys.executable, "-c", code])
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, str(target))
            self.assertIn("Operation not permitted", result.stderr)
        rename = shared.sandbox_command(meta, self.home, ["/bin/mv", self.repo_s, str(self.root / "moved")])
        self.assertNotEqual(subprocess.run(rename, capture_output=True).returncode, 0)
        self.assertEqual((self.repo / "a.txt").read_text(), "a")
        self.assertEqual((external_git / "config").read_text(), "original")
        allowed = shared.sandbox_command(meta, self.home, [sys.executable, "-c", 'from pathlib import Path; Path("../scratch").write_text(Path("a.txt").read_text())'])
        self.assertEqual(subprocess.run(allowed, cwd=self.repo).returncode, 0)


class SharedCliTests(unittest.TestCase):
    setUp = test_pal.PalBehaviorTests.setUp
    tearDown = test_pal.PalBehaviorTests.tearDown
    run_pal = test_pal.PalBehaviorTests.run_pal
    meta = test_pal.PalBehaviorTests.meta

    def make_repo(self):
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "a.txt").write_text("a")

    @unittest.skipUnless(sys.platform == "darwin", "shared execution requires macOS")
    def test_shared_start_resume_mcp_apply_and_release(self):
        self.make_repo()
        started = self.run_pal("start", "codex", "-n", "one", "-C", str(self.repo), "--shared", "--files", "a.txt", "--no-brief", "task")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(self.meta("one")["backend_session_id"], "codex-test-thread")
        conflict = self.run_pal("start", "codex", "-n", "two", "-C", str(self.repo), "--shared", "--files", "a.txt", "task")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertFalse((self.home / "sessions" / "two").exists())
        proposal = {"changes": [{"path": "a.txt", "before_sha256": shared.digest("a"), "content": "new"}]}
        with patch.dict(os.environ, self.env):
            response = mcp_server.tool_shared("apply", {"cwd": str(self.repo), "name": "one", "proposal": json.dumps(proposal)})
        self.assertIn('"applied"', response)
        resumed = self.run_pal("say", "one", "check again")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        prompt = (self.home / "sessions" / "one" / "turns" / "002.prompt.md").read_text()
        self.assertIn(shared.digest("new"), prompt)
        self.assertIn("read-only worker", prompt)
        self.assertEqual(self.meta("one")["backend_session_id"], "codex-test-thread")
        self.assertEqual(self.run_pal("shared", "release", "one", "-C", str(self.repo)).returncode, 0)
        self.assertNotEqual(self.run_pal("say", "one", "again").returncode, 0)

    def test_invalid_modes_start_no_worker(self):
        self.make_repo()
        for flags in [("--shared",), ("--files", "a.txt"), ("--shared", "--files", "a.txt", "--worktree", "branch")]:
            result = self.run_pal("start", "codex", "-C", str(self.repo), *flags, "task")
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())

    def test_mcp_invalid_shared_arguments_return_error(self):
        result = mcp_server.call_tool({"name": "pal_shared_apply", "arguments": {}})
        self.assertTrue(result["isError"])

    def test_mcp_schema_and_start_flag_forwarding(self):
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertTrue({"pal_shared_board", "pal_shared_apply", "pal_shared_release", "pal_shared_recover"} <= names)
        with patch.object(mcp_server, "run_pal", return_value="ok") as run:
            mcp_server.tool_start({"backend": "pi", "prompt": "task", "shared": True, "files": ["a", "b"], "wait": False})
        argv = run.call_args.args[0]
        self.assertIn("--shared", argv)
        self.assertEqual(argv.count("--files"), 2)


if __name__ == "__main__":
    unittest.main()
