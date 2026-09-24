#!/usr/bin/env python3
"""Local stand-in for a real OpenAI-compatible chat endpoint — CI use only.

``tests/test_openai_compatible_integration.py`` makes REAL network calls to
whatever ``OPENAI_COMPATIBLE_BASE_URL`` points at (MiniMax in production:
paid, rate-limited, and unsuitable as a CI dependency — see that file's
docstring). The ``openai-compatible-integration`` job in ``ci.yml`` points
those tests at this local server instead, so the reasoning-sanitizer round
trip (``src/agents_system/agent/reasoning.py::ReasoningSanitizedChatOpenAI``)
still runs over a REAL HTTP request/response cycle in CI, just not against
the paid remote model.

It speaks just enough of the OpenAI chat-completions API to answer the two
request shapes those tests send:

- No ``tools`` in the request  -> a plain answer, phrased with a leading
  ``<think>...</think>`` block, mirroring MiniMax's undisablable inline
  reasoning.
- ``tools`` in the request     -> a ``tool_calls`` response invoking
  ``catalog_search``, so the sanitizer's "strip reasoning, keep tool_calls"
  behavior gets exercised for real.

This is not a general-purpose OpenAI API mock — it understands exactly those
two shapes and nothing else.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _chat_completion(model: str, message: dict[str, object]) -> dict[str, object]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # keep the CI log free of a request line per test

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        request_body = json.loads(self.rfile.read(length) or b"{}")

        if request_body.get("tools"):
            message: dict[str, object] = {
                "content": "<think>Checking the catalog for that item.</think>",
                "tool_calls": [
                    {
                        "id": "call_fake_1",
                        "type": "function",
                        "function": {
                            "name": "catalog_search",
                            "arguments": json.dumps({"query": "Coca Cola 2L"}),
                        },
                    }
                ],
            }
        else:
            message = {
                "content": "<think>The user wants a one-word reply.</think>PONG",
            }

        model = str(request_body.get("model", "fake-integration-model"))
        payload = json.dumps(_chat_completion(model, message)).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8811)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"fake-openai-compatible-server listening on 127.0.0.1:{args.port}", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
