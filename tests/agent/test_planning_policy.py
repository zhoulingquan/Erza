"""P4: PlanningPolicy — deterministic per-turn planning router.

Single-model simplification: no FAST/MANAGED modes, no planner_model, no
use_planner flag. The deterministic ``should_plan(task_text)`` heuristic
decides per turn whether the task warrants a plan; ``force_plan`` provides a
deterministic override for tests / embedders.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.loop import AgentLoop
from erza.agent.planning_policy import PlanningPolicy
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.bus.queue import MessageBus
from erza.providers.base import LLMProvider, LLMResponse


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _spec(messages: list[dict[str, str]], **kwargs: Any) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=messages,
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        **kwargs,
    )


# --- 1-6: should_plan deterministic routing --------------------------------


def test_should_plan_empty_or_none_task_is_false() -> None:
    assert PlanningPolicy().should_plan(None) is False
    assert PlanningPolicy().should_plan("") is False


def test_should_plan_trivial_task_is_false() -> None:
    """Short chat (< 24 chars) never plans."""
    assert PlanningPolicy().should_plan("hello there") is False


def test_should_plan_numbered_list_task_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("请执行以下步骤：\n1. 检查配置\n2. 运行测试\n3. 提交代码") is True
    assert policy.should_plan("Do these steps: 1. read 2. write 3. commit") is True


def test_should_plan_bulleted_list_task_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("请帮我：\n- 读取文件\n- 修改代码\n- 推送") is True


def test_should_plan_two_distinct_step_markers_is_true() -> None:
    policy = PlanningPolicy()
    assert policy.should_plan("首先分析需求，然后编写代码，最后运行测试") is True
    # "then" + "after that" — two distinct English step markers.
    assert policy.should_plan("First read the config, then patch, after that run the tests") is True


def test_should_plan_long_task_is_true() -> None:
    policy = PlanningPolicy()
    long_task = "任务：" + "详细说明内容 " * 50  # > 400 chars
    assert policy.should_plan(long_task) is True


def test_should_plan_long_but_plain_text_is_false() -> None:
    """A long sentence without step markers or lists stays plain ReAct."""
    policy = PlanningPolicy()
    text = "请给我写一篇关于夏天度假的文章，要包含海滩、美食、风景和历史文化。"
    assert policy.should_plan(text) is False


# --- 7: force_plan deterministic override -----------------------------------


def test_force_plan_overrides_heuristics() -> None:
    assert PlanningPolicy(force_plan=True).should_plan("hi") is True
    assert PlanningPolicy(force_plan=False).should_plan("请执行以下步骤：\n1. 检查\n2. 测试") is False


# --- 8: AgentLoop resolution ------------------------------------------------


def test_agent_loop_accepts_direct_planning_policy(tmp_path) -> None:
    policy = PlanningPolicy(force_plan=True)
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        planning_policy=policy,
    )
    assert loop.planning_policy is policy


def test_agent_loop_builds_default_routing_policy(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        planner_max_replans=7,
    )
    assert loop.planning_policy is not None
    assert loop.planning_policy.planner_max_replans == 7
    assert loop.planning_policy.force_plan is None


# --- 9: AgentRunSpec propagation ---------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_spec_carries_planning_policy(tmp_path) -> None:
    from erza.bus.events import InboundMessage

    from erza.config.schema import Config

    config = Config.model_validate(
        {
            "agents": {"defaults": {"plannerMaxReplans": 2}},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )
    loop = AgentLoop.from_config(config, provider=FakeProvider())
    captured_spec: AgentRunSpec | None = None
    original_run = loop.runner.run

    async def capturing_run(spec: Any) -> Any:
        nonlocal captured_spec
        captured_spec = spec
        return await original_run(spec)

    loop.runner.run = capturing_run

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="test",
            content="hello",
        )
    )

    assert captured_spec is not None
    assert captured_spec.planning_policy is not None
    assert captured_spec.planning_policy.planner_max_replans == 2


# --- 10-11: init_planner routes by task -------------------------------------


@pytest.mark.asyncio
async def test_trivial_task_skips_planner() -> None:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "ship"}],
    )

    result = await runner.init_planner(spec)

    assert result == (None, None, None, None)
    assert not provider.chat_with_retry.called


@pytest.mark.asyncio
async def test_multi_step_task_invokes_planner_with_execution_model() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "test"}]}),
            tool_calls=[],
            usage={},
        )
    )
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "首先分析，然后实现，最后验证"}],
    )

    planner, plan, task_text, _tools_summary = await runner.init_planner(spec)

    assert planner is not None
    assert plan is not None
    assert plan.goal == "ship"
    assert task_text == "首先分析，然后实现，最后验证"
    # Single-model: planner reused the execution model.
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_force_plan_invokes_planner_for_trivial_task() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "x", "steps": [{"id": 1, "action": "a"}]}),
            tool_calls=[],
            usage={},
        )
    )
    runner = AgentRunner(provider)
    spec = _spec(
        [{"role": "user", "content": "ship"}],
        planning_policy=PlanningPolicy(force_plan=True),
    )

    _planner, plan, _task, _summary = await runner.init_planner(spec)

    assert plan is not None
    assert plan.goal == "x"
