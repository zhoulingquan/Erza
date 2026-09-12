"""Regression tests for outbound delivery robustness (H5, H6).

H5: ``publish_runtime_model_update`` used ``put_nowait`` on the bounded
outbound queue; a saturated bus raised ``asyncio.QueueFull`` into the caller
(``set_model_preset``), which is synchronous and cannot await.

H6: the outbound dispatcher is one task shared by every channel, and
``_send_with_retry`` had no per-attempt timeout — a channel whose ``send``
never returns (e.g. a websocket client that stops reading) froze delivery for
all channels.
"""

from __future__ import annotations

import asyncio

import pytest

from erza.bus.events import OutboundMessage
from erza.bus.queue import MessageBus
from erza.channels.base import BaseChannel
from erza.channels.manager import ChannelManager
from erza.channels.websocket._session import publish_runtime_model_update
from erza.config.schema import Config


def test_publish_runtime_model_update_survives_full_queue() -> None:
    bus = MessageBus()
    # Fill the bounded outbound queue to capacity.
    for _ in range(MessageBus._MAX_QUEUE_SIZE):
        bus.outbound.put_nowait(
            OutboundMessage(channel="websocket", chat_id="*", content="filler")
        )
    assert bus.outbound.full()

    # Must not raise QueueFull into the (synchronous) caller.
    publish_runtime_model_update(bus, "openai/gpt-4.1", "fast")


def test_publish_runtime_model_update_enqueues_when_room() -> None:
    bus = MessageBus()
    publish_runtime_model_update(bus, "openai/gpt-4.1", "fast")

    msg = bus.outbound.get_nowait()
    assert msg.channel == "websocket"
    assert msg.chat_id == "*"
    assert msg.metadata["_runtime_model_updated"] is True
    assert msg.metadata["model"] == "openai/gpt-4.1"
    assert msg.metadata["model_preset"] == "fast"


class _StallingChannel(BaseChannel):
    """A channel whose ``send`` never returns, simulating a stalled client."""

    name = "stalling"
    display_name = "Stalling"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.send_calls = 0

    async def start(self) -> None:  # pragma: no cover - not used
        pass

    async def stop(self) -> None:  # pragma: no cover - not used
        pass

    async def send(self, message) -> None:
        self.send_calls += 1
        await asyncio.Event().wait()  # blocks forever


class _RecordingChannel(BaseChannel):
    name = "recording"
    display_name = "Recording"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:  # pragma: no cover - not used
        pass

    async def stop(self) -> None:  # pragma: no cover - not used
        pass

    async def send(self, message) -> None:
        self.sent.append(message)


def _make_manager(config: Config, bus: MessageBus, channel: BaseChannel) -> ChannelManager:
    manager = ChannelManager(config, bus)
    manager.channels[channel.name] = channel
    return manager


@pytest.mark.asyncio
async def test_send_with_retry_times_out_on_stalled_channel() -> None:
    cfg = Config()
    cfg.channels.send_timeout_s = 0.05
    cfg.channels.send_max_retries = 1
    bus = MessageBus()
    channel = _StallingChannel(cfg, bus)
    manager = _make_manager(cfg, bus, channel)

    msg = OutboundMessage(channel=channel.name, chat_id="c1", content="hi")

    # Must return promptly instead of hanging the shared dispatcher.
    await asyncio.wait_for(manager._send_with_retry(channel, msg), timeout=5)
    assert channel.send_calls == 1


@pytest.mark.asyncio
async def test_send_with_retry_retries_after_timeout() -> None:
    cfg = Config()
    cfg.channels.send_timeout_s = 0.05
    cfg.channels.send_max_retries = 2
    bus = MessageBus()
    channel = _StallingChannel(cfg, bus)
    manager = _make_manager(cfg, bus, channel)

    msg = OutboundMessage(channel=channel.name, chat_id="c1", content="hi")

    await asyncio.wait_for(manager._send_with_retry(channel, msg), timeout=10)
    # One attempt per configured retry.
    assert channel.send_calls == 2


@pytest.mark.asyncio
async def test_send_with_retry_timeout_zero_disables_bound() -> None:
    cfg = Config()
    cfg.channels.send_timeout_s = 0
    bus = MessageBus()
    channel = _RecordingChannel(cfg, bus)
    manager = _make_manager(cfg, bus, channel)

    msg = OutboundMessage(channel=channel.name, chat_id="c1", content="hi")
    await asyncio.wait_for(manager._send_with_retry(channel, msg), timeout=5)
    assert len(channel.sent) == 1
