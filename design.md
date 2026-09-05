# Design

**Agent-selected working policy:** Python 3.10+ standard library, POSIX process groups and file locks, existing backend CLIs and authentication. CLI and MCP server live together. Default mutable state, worktrees, and temporary cleanup directories live under PAL_HOME; environment overrides remain available. The MCP adapter invokes the adjacent CLI with the same Python interpreter.

Delegation is opt-in. Workers run with broad local authority; cwd and worktrees are not security isolation. Users review diffs, files, and behavior before acceptance. Do not publish credentials, runtime state, private prompts, or original workspace history.
