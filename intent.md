# Intent

**User-stated, 2026-09-06:** Add shared coordination to PAL so delegated agents can work from one checkout without overwriting one another, and update the local installation and public repository.

**User-stated, 2026-09-06:** Route most delegated OpenAI work to GPT-5.6 Luna at xhigh with Fast processing where supported, keep GPT-5.6 Sol xhigh as the stronger alternate, and make the worker advise, review, and fix its own work before reporting completion.

**User-stated, 2026-09-06:** Add an explicit advisor option so a Sol xhigh worker can ask Astra High or a configured Fable 5.1 backend for strategic guidance, while advisors cannot delegate onward. Every delegated worker's first message must clearly identify that it is being delegated to.

**Agent-selected working policy:** Add opt-in macOS shared mode: exact file reservations, a shared task board, read-only worker processes, and lead-applied version-checked text proposals. Preserve persistent backend identity and existing classic/worktree modes. Verify with synthetic backends and temporary state; do not incur model usage or touch existing sessions. Ship directly to the public repository after focused review.

Acceptance requires conflicting reservations and stale proposals to fail, actual descendant write denial, safe interrupted-application recovery, CLI/MCP parity, documented enforcement boundaries, deterministic route resolution, backend command forwarding, and a visible self-review/fix contract. Human or external services and unrelated processes are outside the process guard; model quality and final acceptance remain the lead's responsibility. Explicit model and effort overrides remain authoritative; PAL never trusts a worker's self-reported identity.
