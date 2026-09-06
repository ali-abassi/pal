# PAL

Persistent conversations with coding agents, from your terminal or an MCP client.

PAL wraps **Codex CLI**, **Claude Code**, and **Pi**. Start a named session, send follow-ups to the same backend-native conversation, inspect what happened, and collect results from background work. Python standard library only; no hosted PAL service or additional API key.

**Classic PAL sessions launch agents with broad local authority.** Codex bypasses approval prompts and its sandbox; Claude skips permission checks; Pi runs with its configured tools. A working directory or Git worktree is not a security sandbox. Use trusted tasks in an environment where the agent is authorized to operate. Backend usage is billed or metered by your existing provider/account.

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

Keep the checkout in place while using the symlink. `pal.py`, `shared_workspace.py`, and `mcp_server.py` must stay together; do not copy just the entry script. You can also always run `python3 /absolute/path/to/pal/pal.py` directly.

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

## Shared checkout mode (macOS)

Use this when the user has requested parallel delegation and separate checkouts would add unnecessary storage. It works with Codex, Pi, and Claude adapters. The lead owns integration and verification; shared workers read the checkout and return proposals. No worktree or per-worker dependency copy is created.

```sh
pal start codex -n header -C /absolute/path/to/repo --shared \
  --files src/header.tsx --files tests/header.test.tsx --bg \
  "Improve the header and propose its focused test. Return the shared JSON format."
pal start codex -n footer -C /absolute/path/to/repo --shared \
  --files src/footer.tsx --bg "Improve the footer. Return the shared JSON format."
pal shared board -C /absolute/path/to/repo
pal wait header footer --timeout 60
pal read header
```

Choose explicit model/effort settings supported by your installed backend. `--files` is repeatable and accepts exact paths relative to the Git checkout root, including new files in existing directories. Directories, patterns, symlinks, hardlinks, path traversal and Git metadata are unsupported. Case/Unicode-equivalent ownership paths conservatively conflict. `--shared` cannot be combined with `--worktree`; `PAL_HOME` must be outside the checkout. The shared contract always applies, even with `--no-brief`.

Every turn receives current ownership, the worker's baseline hashes, and short summaries of active peers. Workers can read `pal shared board -C /repo` during a long turn. This is explicit shared state, not automatic synchronization of model context or a transactional source snapshot. The lead must coordinate changes to shared interfaces even when files do not overlap.

### Review and apply

A worker returns a JSON object like this (replace the illustrative hash with the board's actual SHA256):

```json
{
  "summary": "Explain the proposed change and verification needs",
  "changes": [
    {
      "path": "src/header.tsx",
      "before_sha256": "SHA256_FROM_THE_BOARD",
      "content": "Complete replacement UTF-8 file contents\n"
    }
  ]
}
```

Use `before_sha256: null` for a new file and `content: null` for deletion. Proposals support up to 100 text changes and 4 MB of JSON; new files get mode 0644 and replacements preserve permissions. Binary assets and directory creation belong to the lead. The lead extracts and reviews the JSON from the worker's reply, saving it **outside the checkout**, then applies it:

```sh
pal shared apply header -C /absolute/path/to/repo --file /tmp/header-proposal.json
# Or pipe the reviewed JSON to --file -.
# Inspect the complete candidate and run affected checks before accepting it.
pal shared release header -C /absolute/path/to/repo
```

All ownership and original file hashes are checked before mutation. A stale hash rejects the proposal; do not change the claimed hash to force it through. Release the old reservation and start a new named session against the current files. An applied reservation remains owned until explicit release. `pal say` can continue an unreleased session and receives updated hashes. Released sessions cannot resume shared work.

Wait or stop a session before applying or releasing it. Timeouts, completion, archive, and `rm` never transfer claims automatically. A failed start can leave a conservative reservation; inspect the board and release its recorded name explicitly. Release uses the board and checkout, so it also works after an idle session was removed or archived.

### Recovery and enforcement boundary

PAL serializes its own reservations, starts, and applications within one `PAL_HOME`. Known classic PAL turns in overlapping checkout paths are blocked while shared reservations exist, and running classic sessions must stop before shared reservations can start. Use the same updated PAL installation and `PAL_HOME` for all participants.

A write-ahead journal records each application before files change. If an application is interrupted, keep the journal and run:

```sh
pal shared recover -C /absolute/path/to/repo
```

Recovery accepts only recorded before/after file hashes; unexpected edits block it for manual reconciliation. The pending journal stays intact on failure. Individual replacements are atomic, but a multi-file application is not instantaneous to external readers. Pause external writers during apply/recover: ordinary filesystems provide no compare-and-swap guarantee against an unrelated editor racing between a hash check and replacement. Back up and reconcile unexpected edits manually; never delete a pending journal just to bypass a conflict.

On macOS the backend and its descendants run under `/usr/bin/sandbox-exec`, which denies writes to the checkout, external Git metadata, and PAL's coordination directory. The mode fails before backend launch when that guard is unavailable. Backend logs and scratch files outside the protected roots remain writable. Run tests that generate checkout/build outputs through the lead. Stock mode remains available on Linux; shared mode does not silently degrade to advisory locks.

This is **not a general hostile-code sandbox**. Other processes, other PAL_HOME stores, preexisting MCP servers, network services, and editors are outside the guard. Do not use external tools to circumvent it. Workers still have the configured account's broader capabilities outside the protected paths. The guard prevents ordinary worker checkout collisions; it does not prove correctness, semantic compatibility, model quality, or readiness to merge.

### MCP equivalents

`pal_start` accepts `shared: true` and `files: ["src/header.tsx"]`. The additional tools are:

| Tool | Inputs | Purpose |
|---|---|---|
| `pal_shared_board` | `cwd` | Read current ownership and pending state |
| `pal_shared_apply` | `cwd`, `name`, `proposal` (JSON string) | Lead applies a reviewed proposal |
| `pal_shared_release` | `cwd`, `name` | Lead releases an idle owner's files |
| `pal_shared_recover` | `cwd` | Lead resumes a recorded application |

Workers may read the board. Mutating shared commands reject invocation from a PAL worker environment. Existing MCP processes need reconnecting after upgrade to discover the new tools.

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

Tools: `pal_start`, `pal_say`, `pal_wait`, `pal_read`, `pal_log`, `pal_diff`, `pal_list`, `pal_status`, `pal_stop`, and the four `pal_shared_*` tools above. Start long work with `wait: false`, then poll with bounded `pal_wait` calls. Blocking MCP waits are capped at 540 seconds; timeout leaves the backend running. Tool calls run in separate threads, so a wait does not serialize all client requests.

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
python3 -m unittest discover -s tests -v
```

This initial public export was checked on macOS with Python 3.14.7. These checks establish the exercised PAL behavior, not compatibility with every backend version, live model, or Linux environment. Backend CLI flags and event formats can change. Report failures with CLI/Python versions and sanitized reproduction details; do not upload private sessions.

MIT licensed. This is an independent project, not an official OpenAI, Anthropic, or Pi product.
