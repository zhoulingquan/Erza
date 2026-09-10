"""Tests for the ``/api/screenshot`` system-level screen capture route."""

import asyncio
import functools
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from erza.channels.websocket import WebSocketChannel

_PORT = 29850

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _ch(bus: Any, *, port: int = _PORT) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return WebSocketChannel(cfg, bus)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _http_get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


async def _mint_token(port: int) -> str:
    boot = await _http_get(f"http://127.0.0.1:{port}/webui/bootstrap")
    assert boot.status_code == 200
    return boot.json()["token"]


@pytest.mark.asyncio
async def test_screenshot_requires_bearer_token(bus: MagicMock) -> None:
    channel = _ch(bus, port=29851)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get("http://127.0.0.1:29851/api/screenshot")
        assert resp.status_code == 401
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_screenshot_returns_png_for_localhost(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from erza.channels.websocket.handlers import screenshot as screenshot_handler

    monkeypatch.setattr(
        screenshot_handler, "capture_screen_png", lambda: _PNG_MAGIC + b"payload"
    )
    channel = _ch(bus, port=29852)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = await _mint_token(29852)
        resp = await _http_get(
            "http://127.0.0.1:29852/api/screenshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.content.startswith(_PNG_MAGIC)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_screenshot_rejects_remote_connections(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from erza.channels.websocket.handlers import screenshot as screenshot_handler

    monkeypatch.setattr(
        screenshot_handler, "capture_screen_png", lambda: _PNG_MAGIC + b"payload"
    )
    channel = _ch(bus, port=29853)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        # 先以 localhost 身份取 token,再切换为"远程连接"再放行校验。
        token = await _mint_token(29853)
        monkeypatch.setattr(channel, "_is_localhost_connection", lambda connection: False)
        resp = await _http_get(
            "http://127.0.0.1:29853/api/screenshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_screenshot_unavailable_when_capture_fails(
    bus: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from erza.channels.websocket.handlers import screenshot as screenshot_handler

    monkeypatch.setattr(screenshot_handler, "capture_screen_png", lambda: None)
    channel = _ch(bus, port=29854)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = await _mint_token(29854)
        resp = await _http_get(
            "http://127.0.0.1:29854/api/screenshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
    finally:
        await channel.stop()
        await server_task
