# Design

**Agent-selected working policy:** Python 3.10+ standard library, POSIX process groups and file locks, existing backend CLIs and authentication. CLI and MCP server live together. Default mutable state, worktrees, and temporary cleanup directories live under PAL_HOME; environment overrides remain available. The MCP adapter invokes the adjacent CLI with the same Python interpreter.

Delegation is opt-in. Workers run with broad local authority; cwd and worktrees are not security isolation. Users review diffs, files, and behavior before acceptance. Do not publish credentials, runtime state, private prompts, or original workspace history.

## Shared mode (agent-selected working policy, 2026-09-06)

Explicit `--shared` uses one canonical checkout with exact file ownership in a PAL_HOME-local board. Claims are atomic and remain until explicit release; no timeout transfers ownership. The board refreshes at turn boundaries. Workers return text replacement proposals with original SHA256 hashes. The lead reviews and applies them serially through PAL; Git operations and acceptance remain the lead's responsibility.

On macOS, an outer sandbox-exec process denies checkout, Git metadata, and coordinator-state writes to the backend and descendants. Shared mode fails closed on unsupported platforms. This is a local write guard, not a general security sandbox: network services, preexisting MCP servers, other PAL_HOME instances, and unrelated processes are not coordinated. No duplicated checkout, dependency installation, or build output is required per worker. Builds that write the checkout run through the lead.

The coordinator uses Python stdlib flock, atomic file replacement, and a write-ahead journal. Every proposal is validated before mutation; interrupted application may be completed only while affected files match their recorded before/after hashes. Unexpected external edits block recovery. The journal is not a filesystem-wide atomic transaction, and external readers can observe intermediate files.
