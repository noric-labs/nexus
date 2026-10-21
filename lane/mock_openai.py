#!/usr/bin/env python3
"""A tiny OpenAI-compatible chat-completions server for testing lanes without Cloudflare.

It answers every request with a tool call: send_keys with a random but plausible key
batch, and every few turns a screen() call. Streaming (SSE) and non-streaming requests
both work, usage is reported (prompt_tokens ~= request chars / 4), and each request is
logged as one JSON line so you can check that tokens per turn stay flat.

    python3 lane/mock_openai.py --port 8899 [--log /tmp/mock.ndjson] [--text-every N]
                                [--fail-rate 0.05] [--latency 0.5]
                                [--text-tool-shape qwen|glm|gptoss|cycle]

--text-tool-shape replays the Workers AI failure shapes from D-016 instead of structured
tool_calls (to exercise lane/toolfix.py):
    qwen    content '', tool_calls null, reasoning_content '<tool_call>\n{"name": ..., "arguments": {...}}\n</tool_call>'
    glm     content '<tool_call>send_keys("<Down>")', tool_calls []
    gptoss  content '{"keys":"j"}', tool_calls []   (send_keys only: a bare {} is not recoverable)
    cycle   rotates through the three, one per request

Routes: POST /v1/chat/completions and /workers-ai/v1/chat/completions, GET .../models.
Point lane/agent.sh at it with NEXUS_INFERENCE_URL=http://127.0.0.1:8899/v1.
"""

import argparse
import json
import random
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYS = [
    "hjkl", "jjjj", "llll<Enter>", "<Up><Up><Right>", "<Down><Down><Left>", "look<Enter>",
    "north<Enter>", "open mailbox<Enter>", "inventory<Enter>", "i<Esc>", "<Esc>", "s",
    "yes<Enter>", "<Enter>", "x<Enter>", "hello world<Enter>", "<Tab><Enter>",
    "12345<BSpace><BSpace><Enter>", "<Space><Space>",
]

MODELS = [
    "@cf/qwen/qwen3-30b-a3b-fp8", "@cf/zai-org/glm-4.7-flash",
    "@cf/google/gemma-4-26b-a4b-it", "@cf/openai/gpt-oss-20b",
]

lock = threading.Lock()
state = {"n": 0}


SHAPES = ("qwen", "glm", "gptoss")


def text_shape(args, n):
    s = args.text_tool_shape
    return SHAPES[n % len(SHAPES)] if s == "cycle" else s


def shaped_message(shape, name, payload):
    """The assistant message fields a text-tool-call model sends instead of tool_calls."""
    if shape == "qwen":
        return {"content": "", "tool_calls": None, "reasoning_content":
                "<tool_call>\n" + json.dumps({"name": name, "arguments": payload}) + "\n</tool_call>"}
    if shape == "glm":
        arg = json.dumps(payload["keys"]) if "keys" in payload else ""
        return {"content": f"<tool_call>{name}({arg})", "tool_calls": []}
    if shape == "gptoss":
        return {"content": json.dumps(payload, separators=(",", ":")), "tool_calls": []}
    raise ValueError(shape)


def pick(body, args):
    """Return (tool_name, arguments_dict, n) or (None, text, n)."""
    with lock:
        state["n"] += 1
        n = state["n"]
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    if args.text_every and n % args.text_every == 0:
        return None, "I will keep exploring.", n
    shape = text_shape(args, n) if args.text_tool_shape else None
    if "screen" in tools and n % 4 == 1 and shape != "gptoss":
        return "screen", {}, n
    if "send_keys" in tools:
        k = random.choice(KEYS)
        if random.random() < 0.3:
            k += random.choice(KEYS)
        return "send_keys", {"keys": k}, n
    return None, "no tools offered", n


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    args = None

    def log_message(self, fmt, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": "bad json"})
        a = self.args
        if a.latency:
            time.sleep(a.latency)
        if a.fail_rate and random.random() < a.fail_rate:
            return self._json(429, {"error": {"message": "mock rate limit", "type": "rate_limit"}})

        name, payload, n = pick(body, a)
        shape = text_shape(a, n) if (a.text_tool_shape and name) else None
        prompt_tokens = max(1, len(raw) // 4)
        completion_tokens = 12 if name else 8
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        model = body.get("model", "mock")
        call_id = "call_" + uuid.uuid4().hex[:10]
        if a.log:
            with lock, open(a.log, "a") as f:
                f.write(json.dumps({"ts": time.time(), "model": model, "bytes": len(raw),
                                    "messages": len(body.get("messages") or []),
                                    "prompt_tokens": prompt_tokens, "reply": name or "text", "shape": shape,
                                    "args": payload if name else None}) + "\n")

        if not body.get("stream"):
            msg = {"role": "assistant", "content": None if name else payload}
            if shape:
                msg.update(shaped_message(shape, name, payload))
                return self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()),
                                        "model": model, "usage": usage,
                                        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})
            if name:
                msg["tool_calls"] = [{"id": call_id, "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(payload)}}]
            return self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()),
                                    "model": model, "usage": usage,
                                    "choices": [{"index": 0, "message": msg,
                                                 "finish_reason": "tool_calls" if name else "stop"}]})

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

        def chunk(delta, finish=None, use=None):
            c = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else []}
            if use:
                c["usage"] = use
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            if shape:
                # Split each text field in three, the way a real stream dribbles tokens.
                fields = shaped_message(shape, name, payload)
                tc = fields.pop("tool_calls")
                for f, v in fields.items():
                    if v:
                        step = max(1, len(v) // 3)
                        for i in range(0, len(v), step):
                            chunk({f: v[i:i + step], "tool_calls": tc})
                chunk({}, "stop")
            elif name:
                args_s = json.dumps(payload)
                chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                       "function": {"name": name, "arguments": ""}}]})
                mid = len(args_s) // 2
                for part in (args_s[:mid], args_s[mid:]):
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": part}}]})
                chunk({}, "tool_calls")
            else:
                chunk({"content": payload})
                chunk({}, "stop")
            chunk(None, use=usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--log", default="")
    p.add_argument("--text-every", type=int, default=0, help="every Nth reply is plain text (ends the run)")
    p.add_argument("--fail-rate", type=float, default=0.0, help="fraction of requests answered with 429")
    p.add_argument("--latency", type=float, default=0.3, help="seconds before each reply")
    p.add_argument("--text-tool-shape", choices=SHAPES + ("cycle",), default=None,
                   help="emit tool calls as text in this model's failure shape (see docstring)")
    a = p.parse_args()
    H.args = a
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"mock_openai listening on http://{a.host}:{a.port}/v1", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
