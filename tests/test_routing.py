from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server
import pal
import routing


class RoutingTests(unittest.TestCase):
    def test_luna_fast_is_the_default_route(self) -> None:
        decision = routing.resolve("codex", None, None, None)
        self.assertEqual(
            (decision.route, decision.model, decision.effort, decision.service_tier),
            ("luna-fast", "gpt-5.6-luna", "xhigh", "fast"),
        )
        pi = routing.resolve("pi", None, None, None)
        self.assertEqual((pi.model, pi.effort, pi.service_tier),
                         ("openai-codex/gpt-5.6-luna", "xhigh", None))

    def test_sol_route_is_explicit_and_fast_for_codex(self) -> None:
        decision = routing.resolve("codex", "sol-xhigh", None, None)
        self.assertEqual(
            (decision.model, decision.effort, decision.service_tier),
            ("gpt-5.6-sol", "xhigh", "ultrafast"),
        )
        self.assertEqual(routing.resolve("pi", "sol-xhigh", None, None).model,
                         "openai-codex/gpt-5.6-sol")

    def test_explicit_model_does_not_inherit_default_fast_tier(self) -> None:
        decision = routing.resolve("codex", None, "gpt-5.5", None)
        self.assertEqual((decision.model, decision.effort, decision.service_tier),
                         ("gpt-5.5", "xhigh", None))
        selected = routing.resolve("codex", "luna-fast", "gpt-5.5", None)
        self.assertEqual(selected.service_tier, "fast")

    def test_claude_preserves_backend_default_without_an_openai_route(self) -> None:
        default = routing.resolve("claude", None, None, None)
        self.assertEqual((default.route, default.model, default.effort),
                         ("backend-default", None, None))
        with self.assertRaisesRegex(ValueError, "support codex or pi"):
            routing.resolve("claude", "luna-fast", None, None)
        decision = routing.resolve("claude", None, "sonnet", None)
        self.assertEqual((decision.model, decision.effort), ("sonnet", None))

    def test_codex_command_records_route_settings(self) -> None:
        command, prompt_via = pal.codex_cmd(
            {"backend": "codex", "cwd": "/tmp/repo", "model": "gpt-5.6-luna",
             "effort": "xhigh", "service_tier": "fast"},
            {"last": Path("/tmp/last")},
        )
        self.assertEqual(prompt_via, "stdin")
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn('service_tier="fast"', command)
        self.assertIn("gpt-5.6-luna", command)

    def test_session_metadata_records_the_selected_route(self) -> None:
        args = argparse.Namespace(
            backend="codex", route=None, model=None, effort=None,
            agent=None, worktree=None,
        )
        meta = pal.new_session_meta(args, "worker", "/tmp/repo", None, None, 0)
        self.assertEqual(
            {key: meta[key] for key in ("route", "model", "effort", "service_tier")},
            {
                "route": "luna-fast", "model": "gpt-5.6-luna", "effort": "xhigh",
                "service_tier": "fast",
            },
        )

    def test_identity_environment_is_machine_selected(self) -> None:
        env = pal.backend_env({"name": "worker", "depth": 0, "route": "luna-fast",
                               "model": "gpt-5.6-luna", "effort": "xhigh",
                               "service_tier": "fast"})
        self.assertEqual(
            {key: env[key] for key in ("PAL_ROUTE", "PAL_MODEL", "PAL_EFFORT", "PAL_SERVICE_TIER")},
            {"PAL_ROUTE": "luna-fast", "PAL_MODEL": "gpt-5.6-luna",
             "PAL_EFFORT": "xhigh", "PAL_SERVICE_TIER": "fast"},
        )

    def test_mcp_forwards_route_without_overriding_it(self) -> None:
        with patch.object(mcp_server, "run_pal", return_value="ok") as run:
            mcp_server.tool_start({"backend": "codex", "prompt": "task",
                                   "route": "sol-xhigh", "wait": False})
        self.assertEqual(run.call_args.args[0][0:4], ["start", "codex", "--route", "sol-xhigh"])
        start_tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "pal_start")
        self.assertEqual(start_tool["inputSchema"]["properties"]["route"]["enum"],
                         list(routing.ROUTE_NAMES))


if __name__ == "__main__":
    unittest.main()
