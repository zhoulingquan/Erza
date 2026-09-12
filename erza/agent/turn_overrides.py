"""Per-turn runtime overrides for provider / model / light-context.

Some background turns (most notably the heartbeat) need to run with a
different LLM provider, model, or a lighter prompt context than the main
conversation.  The original implementation mutated the loop's *shared*
``provider`` / ``model`` / ``_light_context`` attributes and restored them in a
``finally`` block::

    orig = agent.provider
    agent.provider = hb_provider
    try:
        await agent.process_direct(...)
    finally:
        agent.provider = orig

Because asyncio interleaves concurrent turns on a single thread, that global
swap leaked into any user turn that happened to be in flight — a user turn
could silently be answered by the heartbeat's model, or lose its bootstrap
context.  It also raced on restore (last writer wins).

``ContextVar`` scopes the override to the task that set it, because asyncio
copies the current context into every task it creates.  A heartbeat turn can
therefore ride a dedicated provider/model *without* touching shared state, and
concurrent user turns keep seeing the loop's real values.  The overrides are
consulted by ``AgentLoop.provider`` / ``AgentLoop.model``,
``AgentRunner.provider`` and the prompt builder's ``light_context`` flag.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import Context, ContextVar, Token, copy_context
from typing import Any, Iterator

__all__ = [
    "context_without_overrides",
    "current_light_context",
    "current_model_override",
    "current_provider_override",
    "turn_runtime_overrides",
]

_turn_provider_override: ContextVar[Any] = ContextVar(
    "erza_turn_provider_override", default=None
)
_turn_model_override: ContextVar[str | None] = ContextVar(
    "erza_turn_model_override", default=None
)
_turn_light_context_override: ContextVar[bool | None] = ContextVar(
    "erza_turn_light_context_override", default=None
)


def current_provider_override() -> Any:
    """Return the provider bound to the current turn, or ``None``."""
    return _turn_provider_override.get()


def current_model_override() -> str | None:
    """Return the model bound to the current turn, or ``None``."""
    return _turn_model_override.get()


def current_light_context(default: bool = False) -> bool:
    """Return the effective light-context flag for the current turn."""
    value = _turn_light_context_override.get()
    return default if value is None else value


def _clear_overrides() -> None:
    for var in (
        _turn_provider_override,
        _turn_model_override,
        _turn_light_context_override,
    ):
        if var.get() is not None:
            var.set(None)


def context_without_overrides() -> Context:
    """Return a copy of the current context with all turn overrides cleared.

    Use this when spawning *detached* background work (``asyncio.create_task``
    copies the caller's context): per-turn overrides such as the heartbeat's
    provider/model should end with the turn that set them, not be inherited by
    long-lived background tasks.
    """
    ctx = copy_context()
    ctx.run(_clear_overrides)
    return ctx


@contextmanager
def turn_runtime_overrides(
    *,
    provider: Any | None = None,
    model: str | None = None,
    light_context: bool | None = None,
) -> Iterator[None]:
    """Bind per-turn overrides for the duration of the ``with`` block.

    Only the values that are not ``None`` are bound; omitted ones keep
    whatever the surrounding task already had.  Every bound value is reset on
    exit, including on exception, so a failure inside the turn cannot leak the
    override into later work on the same task.
    """
    tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
    if provider is not None:
        tokens.append((_turn_provider_override, _turn_provider_override.set(provider)))
    if model is not None:
        tokens.append((_turn_model_override, _turn_model_override.set(model)))
    if light_context is not None:
        tokens.append(
            (
                _turn_light_context_override,
                _turn_light_context_override.set(bool(light_context)),
            )
        )
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
