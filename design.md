# Design

**Agent-selected working policy:** Python 3.10+ standard library, POSIX process groups and file locks, existing backend CLIs and authentication. CLI and MCP server live together. Default mutable state, worktrees, and temporary cleanup directories live under PAL_HOME; environment overrides remain available. The MCP adapter invokes the adjacent CLI with the same Python interpreter.

Delegation is opt-in. Workers run with broad local authority; cwd and worktrees are not security isolation. Users review diffs, files, and behavior before acceptance. Do not publish credentials, runtime state, private prompts, or original workspace history.

## Shared mode (agent-selected working policy, 2026-09-06)

Explicit `--shared` uses one canonical checkout with exact file ownership in a PAL_HOME-local board. Claims are atomic and remain until explicit release; no timeout transfers ownership. The board refreshes at turn boundaries. Workers return text replacement proposals with original SHA256 hashes. The lead reviews and applies them serially through PAL; Git operations and acceptance remain the lead's responsibility.

On macOS, an outer sandbox-exec process denies checkout, Git metadata, and coordinator-state writes to the backend and descendants. Shared mode fails closed on unsupported platforms. This is a local write guard, not a general security sandbox: network services, preexisting MCP servers, other PAL_HOME instances, and unrelated processes are not coordinated. No duplicated checkout, dependency installation, or build output is required per worker. Builds that write the checkout run through the lead.

The coordinator uses Python stdlib flock, atomic file replacement, and a write-ahead journal. Every proposal is validated before mutation; interrupted application may be completed only while affected files match their recorded before/after hashes. Unexpected external edits block recovery. The journal is not a filesystem-wide atomic transaction, and external readers can observe intermediate files.

## Model routing and review (user-stated direction, 2026-09-06)

The default route for Codex and Pi is `luna-fast`: Codex receives `gpt-5.6-luna`, `xhigh`, and requested `service_tier="fast"`; Pi receives `openai-codex/gpt-5.6-luna` and `xhigh`. `sol-xhigh` is the explicit alternate: Codex receives `gpt-5.6-sol`, `xhigh`, and requested `service_tier="ultrafast"`; Pi receives `openai-codex/gpt-5.6-sol` and `xhigh`. `backend-default` preserves backend configuration. When Claude is selected without a route, PAL preserves its existing backend default; an explicit OpenAI route requires an explicit Claude model. Explicit `--model` and `--effort` override route values; an explicit model without an explicit route does not inherit the default service tier.

PAL records the route, model, effort, requested service tier, and source in session metadata and exports them as `PAL_ROUTE`, `PAL_MODEL`, `PAL_EFFORT`, and `PAL_SERVICE_TIER`. The execution brief tells the worker to understand intent, make the smallest change, inspect the complete diff, run the narrowest authoritative check, critically review, fix findings, and advise with exact blockers when evidence is incomplete. This is a worker self-review contract; it is not independent validation or automatic acceptance. The lead remains responsible for inspecting the candidate and correcting or rejecting it.

## Advisor consults (user-stated direction, 2026-09-06)

`pal advise astra-high` and `pal advise fable-5.1` create an explicit, persistent advisory session. Astra High is fixed to the verified Codex model `gpt-6-astra` at high reasoning. Fable 5.1 requires a verified provider model id through `--model` or `PAL_FABLE_ADVISOR_MODEL`; PAL never guesses a provider identifier. A Sol xhigh PAL worker may request one of these consults from its own session; a top-level user may also create one explicitly. There is no automatic advisor call or vote.

Advisor sessions record `role=advisor`, receive a no-edit/no-delegation guidance brief, export `PAL_CAN_DELEGATE=0`, and are rejected if they try to start a child. Nested workers cannot select the premium Sol or Astra routes. These are behavioral and process guards around classic backend CLIs, not a general filesystem sandbox; the requesting lead still owns implementation, verification, and acceptance.
