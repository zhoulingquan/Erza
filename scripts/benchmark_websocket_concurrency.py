"""WebSocket concurrency load test for the Erza WS server channel.

Measures per-client connect latency, first-frame latency (a proxy for server
queueing delay), and turn-end round-trip duration across N concurrent
connections. A --self-test mode runs the same measurement against an in-process
mock server so the script can be validated without a live Erza deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter
from typing import Any

import websockets
import websockets.exceptions

DEFAULT_MESSAGE = "用一句话介绍一下你自己"


def _client_url(base: str, client_id: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}client_id={client_id}"


def _outbound(message: str) -> str:
    return json.dumps({"content": message}, ensure_ascii=False)


def _is_turn_end(raw: Any) -> bool:
    """True when the (JSON) frame carries event == 'turn_end'."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("event") == "turn_end"


async def _run_client(
    client_id: str, url: str, message: str, timeout_s: float, start_delay: float
) -> dict[str, Any]:
    if start_delay > 0.0:
        await asyncio.sleep(start_delay)
    status = "ok"
    reason: str | None = None
    connect_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    frames = 0
    t0 = time.monotonic()
    try:
        async with websockets.connect(url, open_timeout=timeout_s) as ws:
            connect_ms = (time.monotonic() - t0) * 1000.0
            sent_at = time.monotonic()
            await asyncio.wait_for(ws.send(_outbound(message)), timeout_s)
            deadline = time.monotonic() + timeout_s
            first_at: float | None = None
            end_at: float | None = None
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        status = "timeout"
                        reason = "no turn_end before overall timeout"
                        break
                    raw = await asyncio.wait_for(ws.recv(), remaining)
                    frames += 1
                    if first_at is None:
                        first_at = time.monotonic()
                    if _is_turn_end(raw):
                        end_at = time.monotonic()
                        break
            except asyncio.TimeoutError:
                status = "timeout"
                reason = "no turn_end before overall timeout"
            if status == "ok":
                ttfb_ms = (first_at - sent_at) * 1000.0
                total_ms = (end_at - sent_at) * 1000.0
    except asyncio.TimeoutError:
        status = "connect_timeout"
        reason = "handshake/connect timed out"
    except websockets.exceptions.ConnectionClosed as exc:
        status = "disconnected"
        reason = f"connection closed: {exc.rcvd or exc}"
    except OSError as exc:
        status = "error"
        reason = f"{type(exc).__name__}: {exc}"
    return {
        "client_id": client_id,
        "ok": status == "ok",
        "status": status,
        "connect_ms": connect_ms,
        "ttfb_ms": ttfb_ms,
        "total_ms": total_ms,
        "frames": frames,
        "reason": reason,
    }


async def _run_clients(
    url: str, clients: int, message: str, timeout_s: float, stagger_ms: float
) -> tuple[list[dict[str, Any]], float]:
    stagger_s = stagger_ms / 1000.0
    ids = [f"loadtest-{i:02d}" for i in range(clients)]
    t0 = time.monotonic()
    results = await asyncio.gather(
        *(
            _run_client(cid, _client_url(url, cid), message, timeout_s, i * stagger_s)
            for i, cid in enumerate(ids)
        )
    )
    return results, time.monotonic() - t0


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, round(p * (len(sorted_vals) - 1)))]


def _print_summary(results: list[dict[str, Any]], elapsed: float) -> None:
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    timeouts = [r for r in failed if r["status"] in ("timeout", "connect_timeout")]

    print("\n=== WebSocket concurrency summary ===")
    print(
        f"clients: {len(results)}  success: {len(ok)}  failed: {len(failed)}  "
        f"timeout: {len(timeouts)}  elapsed: {elapsed:.2f}s"
    )

    def row(name: str, values: list[float]) -> None:
        vals = sorted(values)
        p50 = _percentile(vals, 0.5)
        p95 = _percentile(vals, 0.95)
        mx = max(vals) if vals else float("nan")
        mn = min(vals) if vals else float("nan")
        print(f"{name:<12} p50={p50:9.1f} p95={p95:9.1f} max={mx:9.1f} min={mn:9.1f}")

    row(
        "connect(ms)",
        [r["connect_ms"] for r in results if r["connect_ms"] is not None],
    )
    row("ttfb(ms)", [r["ttfb_ms"] for r in ok if r["ttfb_ms"] is not None])
    row("total(ms)", [r["total_ms"] for r in ok if r["total_ms"] is not None])

    reasons = Counter(r["reason"] or r["status"] for r in failed)
    print("\nfailure reasons (top 5):")
    for reason, count in reasons.most_common(5):
        print(f"  {count:4d}  {reason}")


# --- self-test: in-process mock server --------------------------------------
async def _mock_handler(websocket) -> None:
    """Emulate a slow server: queue up, emit a delta, then finish the turn."""
    try:
        async for _message in websocket:
            await asyncio.sleep(random.uniform(0.1, 0.5))
            await websocket.send(
                json.dumps({"event": "delta", "content": "mock reply"}, ensure_ascii=False)
            )
            await asyncio.sleep(0.2)
            await websocket.send(json.dumps({"event": "turn_end"}, ensure_ascii=False))
    except websockets.exceptions.ConnectionClosed:
        pass


async def _run_self_test(args: argparse.Namespace) -> tuple[list[dict[str, Any]], float]:
    async with websockets.serve(_mock_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/"
        return await _run_clients(url, 5, args.message, args.timeout, 0)


# --- CLI --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark_websocket_concurrency",
        description=(
            "WebSocket concurrency load test for the Erza WS server: measures "
            "connect latency, first-frame (queueing) latency, and turn-end "
            "round-trip duration across N concurrent clients."
        ),
    )
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:8765/",
        help="base WS URL (default: ws://127.0.0.1:8765/)",
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=20,
        help="number of concurrent clients (default: 20)",
    )
    parser.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help="message text to send (default: short Chinese intro)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="per-client total timeout seconds (default: 300)",
    )
    parser.add_argument(
        "--stagger-ms",
        type=float,
        default=0,
        help="stagger between client start times in ms (default: 0 = simultaneous)",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="write results as JSON to this path",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run against an in-process mock server with 5 clients",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.clients < 1:
        print("--clients must be at least 1", file=sys.stderr)
        return 1
    if args.self_test:
        results, elapsed = asyncio.run(_run_self_test(args))
    else:
        results, elapsed = asyncio.run(
            _run_clients(args.url, args.clients, args.message, args.timeout, args.stagger_ms)
        )
    _print_summary(results, elapsed)
    if args.json_out:
        payload = {"elapsed_s": elapsed, "results": results}
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"results written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
