"""Explicit, bounded advisory targets for PAL."""

from __future__ import annotations

import os
from dataclasses import dataclass


ADVISOR_NAMES = ("astra-high", "fable-5.1")
SOL_MODELS = frozenset(("gpt-5.6-sol", "openai-codex/gpt-5.6-sol"))


@dataclass(frozen=True)
class AdvisorDecision:
    name: str
    backend: str
    model: str
    effort: str


def can_seek(model: str | None, effort: str | None) -> bool:
    return model in SOL_MODELS and effort == "xhigh"


def resolve(name: str, model_override: str | None = None) -> AdvisorDecision:
    if name == "astra-high":
        if model_override and model_override != "gpt-6-astra":
            raise ValueError("astra-high is fixed to the verified gpt-6-astra model")
        return AdvisorDecision(name, "codex", "gpt-6-astra", "high")
    if name == "fable-5.1":
        model = model_override or os.environ.get("PAL_FABLE_ADVISOR_MODEL")
        if not model:
            raise ValueError(
                "fable-5.1 needs a verified provider model id; pass --model or set "
                "PAL_FABLE_ADVISOR_MODEL (PAL will not invent a Fable CLI id)"
            )
        return AdvisorDecision(name, "claude", model, "high")
    raise ValueError(f"unknown advisor '{name}'; choose from {', '.join(ADVISOR_NAMES)}")


def describe() -> list[dict[str, str | bool | None]]:
    return [
        {
            "advisor": "astra-high",
            "backend": "codex",
            "model": "gpt-6-astra",
            "effort": "high",
            "requires_model_id": False,
        },
        {
            "advisor": "fable-5.1",
            "backend": "claude",
            "model": None,
            "effort": "high",
            "requires_model_id": True,
        },
    ]
