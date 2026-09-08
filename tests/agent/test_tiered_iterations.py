"""Max tool iterations (single-tier, P4 simplification).

The config default is 200; explicit ``maxToolIterations`` wins.
No planning-mode tiers and no fast/managed fields.
"""

from __future__ import annotations

from erza.agent.loop import AgentLoop
from erza.config.schema import Config


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs):
        from erza.providers.base import LLMResponse

        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
        )

    async def chat_stream_with_retry(self, **kwargs):
        from erza.providers.base import LLMResponse

        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
        )


def _config(defaults: dict) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": defaults},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


def _loop(defaults: dict) -> AgentLoop:
    return AgentLoop.from_config(_config(defaults), provider=FakeProvider())


def test_default_max_iterations_200() -> None:
    loop = _loop({})
    assert loop.max_iterations == 200


def test_explicit_max_iterations_wins() -> None:
    loop = _loop({"maxToolIterations": 120})
    assert loop.max_iterations == 120


def test_config_propagation() -> None:
    """AgentLoop.from_config 全链路 config→builder→loop 字段到达"""
    config = _config({"maxToolIterations": 150})
    loop = AgentLoop.from_config(config, provider=FakeProvider())
    assert loop.max_iterations == 150
