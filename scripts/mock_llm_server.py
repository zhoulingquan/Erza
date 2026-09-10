"""Mock OpenAI-compatible LLM server for end-to-end Erza load testing.

Serves /chat/completions and /v1/chat/completions with a configurable delay
(MOCK_LLM_DELAY_S, default 2.0) before replying, so load-test turn durations
are deterministic and cost no tokens. Streams SSE when ``stream: true``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from aiohttp import web

REPLY = "你好，我是压测用的模拟模型，这轮回复到此结束。"
_REQUEST_COUNT = 0


def _chunk(model: str, delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "mock-loadtest",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def chat_completions(request: web.Request) -> web.Response:
    global _REQUEST_COUNT
    _REQUEST_COUNT += 1
    print(f"[mock-llm] request #{_REQUEST_COUNT}", file=sys.stderr, flush=True)
    body = await request.json()
    model = body.get("model", "mock-chat")
    delay = float(os.environ.get("MOCK_LLM_DELAY_S", "2.0"))
    await asyncio.sleep(delay)
    if body.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for delta, finish in (
            ({"role": "assistant"}, None),
            ({"content": REPLY}, None),
            ({}, "stop"),
        ):
            chunk = json.dumps(_chunk(model, delta, finish), ensure_ascii=False)
            await resp.write(f"data: {chunk}\n\n".encode())
            await asyncio.sleep(0.05)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    return web.json_response(
        {
            "id": "mock-loadtest",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": REPLY},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
        }
    )


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/chat/completions", chat_completions)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible LLM server")
    parser.add_argument("--port", type=int, default=8971)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"[mock-llm] listening on http://{args.host}:{args.port}", file=sys.stderr, flush=True)
    web.run_app(build_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
