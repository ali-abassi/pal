# Contributing instructions

Read vision.md, intent.md, and design.md before changes. Work directly; delegate only when the user explicitly requests it. Keep changes focused and preserve backend session identity. Use synthetic backends and temporary PAL_HOME for checks; do not run paid agents or cleanup against a real user state directory by default. Never commit session data, credentials, or private prompts. Review observable behavior and the exact diff before delivery.
