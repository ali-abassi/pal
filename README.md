# PAL

Persistent conversations with coding agents, from your terminal or an MCP client.

PAL wraps **Codex CLI**, **Claude Code**, and **Pi**. Start a named session, send follow-ups to the same backend-native conversation, inspect what happened, and collect results from background work. Python standard library only; no hosted PAL service or additional API key.

**PAL launches agents with broad local authority.** Codex bypasses approval prompts and its sandbox; Claude skips permission checks; Pi runs with its configured tools. A working directory or Git worktree is not a security sandbox. Use trusted tasks in an environment where the agent is authorized to operate. Backend usage is billed or metered by your existing provider/account.

## Install

Requires Python 3.10+, Git, and at least one installed, authenticated backend CLI on `PATH`. PAL uses POSIX file locks and process groups: macOS and Linux are the intended platforms; native Windows is unsupported. The `gc` command requires macOS with Xcode's `xcrun simctl`.

```sh
git clone https://github.com/ali-abassi/pal.git
cd pal
python3 pal.py --help

# Optional: expose the CLI on PATH. This fails rather than overwriting an existing pal.
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/pal.py" "$HOME/.local/bin/pal"
export PATH="$HOME/.local/bin:$PATH"
```

Keep the checkout in place while using the symlink. You can also always run `python3 /absolute/path/to/pal/pal.py` directly.

## Start and continue a session

```sh
pal start codex -n implementation -C /absolute/path/to/repo --bg \
  "Implement the requested change and verify its behavior."
pal wait implementation --timeout 60
pal log implementation
pal diff implementation --full
pal say implementation "Fix the identified regression and rerun the focused check."
pal read implementation --all
pal status implementation
```

`start` creates a session; `say` resumes its native thread. Codex's thread ID comes from structured backend events. Pi uses a separate session store. Claude starts with an explicit UUID and resumes it on later turns. PAL rejects a reported session-ID mismatch.

Use `-m MODEL` and `--effort LEVEL` to choose settings supported by your installed backend and account. PAL does not choose an optimal model or verify a model's self-description. Without an override, Codex/Pi use their configured defaults and Claude defaults to `sonnet`.

```sh
pal start claude -n review -C /absolute/path/to/repo "Review the specified change."
pal start pi -n investigation -C /absolute/path/to/repo "Investigate the specified failure."
```

Pi's optional `--agent NAME` reads a Markdown role from `PAL_PI_AGENTS_DIR`; no private or predefined roles are bundled. Claude's `--agent` uses a custom agent already configured in Claude Code.

## Isolated edits and background work

```sh
pal start codex -n worker-a -C /absolute/path/to/repo \
  --worktree feature/worker-a --bg "Complete the first independent task."
pal wait worker-a worker-b --timeout 60
pal ls --tree
pal stop worker-a
```

The `wait` example assumes both named sessions already exist. A timeout stops waiting, not the agent. Exit 124 means work remains running; inspect it or use `stop`. Completed errors return nonzero; stopped work returns 130 from a waiting command.

Worktrees live under `$PAL_HOME/worktrees/<session>` by default. For a new branch PAL fetches origin and uses `origin/main` if available, otherwise `HEAD`. Existing branches are reused. Choose the source state deliberately; uncommitted source changes are not copied into a new worktree.

`pal rm NAME` deletes session history but keeps its worktree. `pal rm NAME --worktree` also removes a clean recorded worktree; `--force` permits removing a dirty one. Stopping an agent cannot reverse external actions it already performed.

## MCP setup

The bundled stdio MCP server invokes the adjacent `pal.py` with the same Python interpreter, so it does not depend on a separate `pal` installation or your original skill directories.

From the cloned repository:

```sh
# Choose the client you use. These commands change that client's MCP configuration.
codex mcp add pal -- python3 "$PWD/mcp_server.py"
claude mcp add --scope user pal -- python3 "$PWD/mcp_server.py"
```

For clients accepting JSON configuration:

```json
{
  "mcpServers": {
    "pal": {
      "command": "python3",
      "args": ["/absolute/path/to/pal/mcp_server.py"]
    }
  }
}
```

Tools: `pal_start`, `pal_say`, `pal_wait`, `pal_read`, `pal_log`, `pal_diff`, `pal_list`, `pal_status`, and `pal_stop`. Start long work with `wait: false`, then poll with bounded `pal_wait` calls. Blocking MCP waits are capped at 540 seconds; timeout leaves the backend running. Tool calls run in separate threads, so a wait does not serialize all client requests.

## Recommended agent instruction

Add this to your own `AGENTS.md` or `CLAUDE.md` if you want explicit control over delegation:

> Do the task yourself by default. Use PAL only when the user explicitly requests delegation or agent orchestration for this task. Remain accountable for delegated work: inspect the complete candidate, verify behavior, and fix or reject deficient output. A completion message is not acceptance.

PAL does not mechanically enforce this instruction, code review, merge approval, credit budgets, or concurrency limits. It caps nested delegation depth at two and records parent/session identity. Review `git status`, staged and unstaged diffs, committed changes, and untracked files as appropriate: `pal diff --full` alone is not a complete acceptance check.

## State and configuration

| Variable | Default | Purpose |
|---|---|---|
| `PAL_HOME` | `~/.pal` | Session metadata, prompts, events, replies, transcripts, archive, and price table |
| `PAL_WORKTREE_ROOT` | `$PAL_HOME/worktrees` | Managed Git worktrees |
| `PAL_TMP_ROOT` | `$PAL_HOME/tmp` | Temporary directories considered by cleanup |
| `PAL_PI_AGENTS_DIR` | `~/.pi-x/agent/agents` | Optional Pi Markdown roles |

Runtime records can contain source code, prompts, command output, and other sensitive material. Keep them private and out of Git. Authenticate through the backend CLIs; PAL inherits their environment and does not supply credentials.

Codex token usage comes from backend events. Optional `$PAL_HOME/prices.json` supplies your own current USD-per-million-token rates:

```json
{
  "your-model-id": {
    "placeholder": true,
    "input_usd_per_million": 0,
    "cached_input_usd_per_million": 0,
    "output_usd_per_million": 0
  }
}
```

Replace the placeholder with verified rates and set `placeholder` to `false` to enable estimation. Missing or placeholder rates produce unknown cost. Cached input is part of input, not additional input. Estimated dollars are not subscription credits.

`pal ls` shows recent sessions plus running work; `--all` includes older sessions. `pal archive --older-than 2 --dry-run` previews moving old idle/stopped sessions into the archive.

**Cleanup:** inspect `pal gc --dry-run` before using `gc`. It considers simulator names starting with `pal-` and temporary directory names ending in `-dd` or containing `prior-art`; it protects recorded running sessions and Git worktrees. Name-based matching is not proof of ownership. Do not point `PAL_TMP_ROOT` at a shared temporary directory. No cleanup runs automatically.

## Development and verification

The focused checks use synthetic Codex events and temporary directories; they make no paid model calls:

```sh
python3 -m unittest discover -s tests -p test_pal.py -k test_worktree_path_and_branch_creation
python3 -m unittest discover -s tests -p test_pal.py -k test_codex_usage_tokens_and_unknown_cost
python3 -m unittest discover -s tests -p test_pal.py -k test_native_session_continuation
python3 -m unittest discover -s tests -p test_pal.py -k test_mcp_discovery_and_listing
```

This initial public export was checked on macOS with Python 3.14.7. These checks establish the exercised PAL behavior, not compatibility with every backend version, live model, or Linux environment. Backend CLI flags and event formats can change. Report failures with CLI/Python versions and sanitized reproduction details; do not upload private sessions.

MIT licensed. This is an independent project, not an official OpenAI, Anthropic, or Pi product.
