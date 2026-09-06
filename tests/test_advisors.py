from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import advisors
import mcp_server
import pal


PAL = Path(__file__).resolve().parents[1] / "pal.py"
FAKE_CODEX = """#!/usr/bin/env python3
import json
import sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "advisor-test"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "advice"}}))
"""


class AdvisorTests(unittest.TestCase):
    def test_advisor_targets_are_explicit_and_fable_fails_closed(self) -> None:
        astra = advisors.resolve("astra-high")
        self.assertEqual((astra.backend, astra.model, astra.effort), ("codex", "gpt-6-astra", "high"))
        with self.assertRaisesRegex(ValueError, "verified provider model id"):
            advisors.resolve("fable-5.1")
        fable = advisors.resolve("fable-5.1", "verified/fable-5.1")
        self.assertEqual((fable.backend, fable.model, fable.effort), ("claude", "verified/fable-5.1", "high"))
        self.assertTrue(advisors.can_seek("openai-codex/gpt-5.6-sol", "xhigh"))
        self.assertFalse(advisors.can_seek("gpt-5.6-luna", "xhigh"))

    def test_advisor_metadata_and_environment_are_non_delegating(self) -> None:
        args = argparse.Namespace(
            backend="codex", route=None, model="gpt-6-astra", effort="high",
            agent=None, worktree=None,
        )
        meta = pal.new_session_meta(args, "advisor", "/tmp/repo", None, "sol", 1,
                                     role="advisor", advisor="astra-high")
        self.assertEqual((meta["role"], meta["advisor"], meta["can_delegate"]),
                         ("advisor", "astra-high", False))
        env = pal.backend_env(meta)
        self.assertEqual((env["PAL_ROLE"], env["PAL_CAN_DELEGATE"], env["PAL_CAN_SEEK_ADVISOR"]),
                         ("advisor", "0", "0"))

    def test_worker_brief_identifies_delegation(self) -> None:
        args = argparse.Namespace(no_brief=True, shared=False)
        prompt = pal.start_prompt(args, "/tmp/repo", {
            "role": "worker", "route": "sol-xhigh", "model": "gpt-5.6-sol",
            "effort": "xhigh", "service_tier": "ultrafast",
            "parent": "lead",
        }, "task")
        self.assertIn("You are being delegated to", prompt)
        self.assertIn("/Users/aliabassi/.codex/policies/project-acceptance.md", prompt)

    def test_nested_worker_cannot_escalate_to_premium_route(self) -> None:
        args = argparse.Namespace(backend="codex", route="sol-xhigh", model=None, effort=None)
        with self.assertRaises(SystemExit):
            pal.validate_child_target(args, 2, "worker")
        pi_args = argparse.Namespace(backend="pi", route=None, model="openai-codex/gpt-5.6-sol", effort="xhigh")
        with self.assertRaises(SystemExit):
            pal.validate_child_target(pi_args, 2, "worker")

    def test_advisor_process_runs_and_child_delegation_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "bin"
            binary.mkdir()
            codex = binary / "codex"
            codex.write_text(FAKE_CODEX)
            codex.chmod(0o755)
            home = root / "home"
            env = os.environ | {"PAL_HOME": str(home), "PATH": f"{binary}:{os.environ['PATH']}"}
            started = subprocess.run(
                [sys.executable, str(PAL), "advise", "astra-high", "-n", "advisor", "-C", str(root), "question"],
                env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            meta = json.loads((home / "sessions" / "advisor" / "meta.json").read_text())
            self.assertEqual((meta["role"], meta["advisor"], meta["parent"] if "parent" in meta else None),
                             ("advisor", "astra-high", None))
            prompt = (home / "sessions" / "advisor" / "turns" / "001.prompt.md").read_text()
            self.assertIn("You are being consulted", prompt)
            refused = subprocess.run(
                [sys.executable, str(PAL), "start", "codex", "-n", "child", "task"],
                env=env | {"PAL_SESSION": "advisor", "PAL_ROLE": "advisor", "PAL_CAN_DELEGATE": "0"},
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("advisor sessions cannot delegate", refused.stderr)

    def test_mcp_advisor_tool_is_explicit_and_forwards_target(self) -> None:
        with patch.object(mcp_server, "run_pal", return_value="ok") as run:
            self.assertEqual(mcp_server.tool_advisor({
                "advisor": "astra-high", "prompt": "strategy", "wait": False,
            }), "ok")
        self.assertEqual(run.call_args.args[0][:3], ["advise", "astra-high", "--timeout"])
        names = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertIn("pal_advisor", names)
        self.assertIn("pal_advisors", names)


if __name__ == "__main__":
    unittest.main()
