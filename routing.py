"""Explicit, inspectable model routes for PAL sessions."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_ROUTE = "luna-fast"
ROUTE_NAMES = ("luna-fast", "sol-xhigh", "backend-default")


@dataclass(frozen=True)
class RoutingDecision:
    route: str
    model: str | None
    effort: str | None
    service_tier: str | None
    source: str


ROUTES: dict[str, dict[str, str | None]] = {
    "luna-fast": {
        "codex_model": "gpt-5.6-luna",
        "pi_model": "openai-codex/gpt-5.6-luna",
        "effort": "xhigh",
        "codex_service_tier": "fast",
    },
    "sol-xhigh": {
        "codex_model": "gpt-5.6-sol",
        "pi_model": "openai-codex/gpt-5.6-sol",
        "effort": "xhigh",
        "codex_service_tier": "ultrafast",
    },
    "backend-default": {
        "codex_model": None,
        "pi_model": None,
        "effort": None,
        "codex_service_tier": None,
    },
}


def resolve(
    backend: str,
    route: str | None,
    model_override: str | None,
    effort_override: str | None,
) -> RoutingDecision:
    selected = route if route is not None else default_route(backend)
    if selected not in ROUTES:
        raise ValueError(f"unknown route '{selected}'; choose from {', '.join(ROUTE_NAMES)}")
    validate_route(backend, selected, model_override)
    profile_model, profile_effort, profile_tier = route_profile(backend, ROUTES[selected])
    return RoutingDecision(
        route=selected, model=value_or_fallback(model_override, profile_model),
        effort=value_or_fallback(effort_override, profile_effort),
        service_tier=resolved_service_tier(route, model_override, profile_tier),
        source=source_for(selected, model_override, effort_override),
    )


def default_route(backend: str) -> str:
    return DEFAULT_ROUTE if backend in ("codex", "pi") else "backend-default"


def route_profile(
    backend: str, profile: dict[str, str | None],
) -> tuple[str | None, str | None, str | None]:
    effort = profile["effort"] if backend in ("codex", "pi") else None
    service_tier = profile["codex_service_tier"] if backend == "codex" else None
    return profile.get(f"{backend}_model"), effort, service_tier


def validate_route(backend: str, selected: str, model_override: str | None) -> None:
    if backend == "claude" and model_override is None and selected != "backend-default":
        raise ValueError("Luna and Sol routes support codex or pi; choose --model for claude")


def value_or_fallback(override: str | None, fallback: str | None) -> str | None:
    return override if override is not None else fallback


def resolved_service_tier(
    route: str | None, model_override: str | None, profile_tier: str | None,
) -> str | None:
    if model_override is not None and route is None:
        return None
    return profile_tier


def source_for(selected: str, model_override: str | None, effort_override: str | None) -> str:
    if model_override is not None or effort_override is not None:
        return "explicit override"
    return f"route:{selected}"


def describe() -> list[dict[str, str | None]]:
    return [
        {
            "route": name,
            "codex_model": values["codex_model"],
            "pi_model": values["pi_model"],
            "effort": values["effort"],
            "codex_service_tier": values["codex_service_tier"],
        }
        for name, values in ROUTES.items()
    ]
