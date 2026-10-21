"""OpenAI-compatible mock of the broker's Workers AI route, for probe tests and dry runs.

POST {prefix}/chat/completions with a JSON body. Behaviour:
- Rate limit: a sliding window per model (`rpm` per minute, measured over `window_s`
  seconds and scaled), plus an optional account-wide limit across all models. Over the
  limit -> 429 with a Workers AI style error body. Rejected requests do not use quota.
- Latency: base + lognormal jitter, inflated as the model's load approaches its limit.
- Response: a tool call to send_keys with 1-6 keys, sometimes screen(), and sometimes
  (invalid_pct) a plain-text reply with no tool call. usage carries estimated tokens.
GET /health returns {"ok": true}. GET /stats returns counters per model.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYS = list("hjklyubnis><gx?") + ["Up", "Down", "Left", "Right", "Enter", "Escape", "Space"]


class Limiter:
    def __init__(self, rpm: float, window_s: float):
        self.cap = max(1, int(round(rpm * window_s / 60.0))) if rpm > 0 else 0
        self.window_s = window_s
        self.hits: collections.deque[float] = collections.deque()

    def _trim(self, now: float) -> None:
        while self.hits and now - self.hits[0] > self.window_s:
            self.hits.popleft()

    def load(self, now: float) -> float:
        if not self.cap:
            return 0.0
        self._trim(now)
        return len(self.hits) / self.cap

    def would_allow(self, now: float) -> bool:
        if not self.cap:
            return True
        self._trim(now)
        return len(self.hits) < self.cap

    def take(self, now: float) -> None:
        if self.cap:
            self.hits.append(now)


class MockState:
    def __init__(self, rpm=300.0, account_rpm=0.0, window_s=60.0, base_latency_s=0.6,
                 jitter=0.35, invalid_pct=3.0, screen_pct=5.0, error_pct=0.0, seed=None,
                 model_rpm: dict[str, float] | None = None):
        self.rpm, self.window_s = rpm, window_s
        self.model_rpm = model_rpm or {}
        self.account = Limiter(account_rpm, window_s)
        self.models: dict[str, Limiter] = {}
        self.base_latency_s, self.jitter = base_latency_s, jitter
        self.invalid_pct, self.screen_pct, self.error_pct = invalid_pct, screen_pct, error_pct
        self.lock = threading.Lock()
        self.rng = random.Random(seed)
        self.stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    def admit(self, model: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self.lock:
            lim = self.models.get(model)
            if lim is None:
                lim = self.models[model] = Limiter(self.model_rpm.get(model, self.rpm), self.window_s)
            if not (lim.would_allow(now) and self.account.would_allow(now)):
                self.stats[model]["429"] += 1
                return False, 0.0
            lim.take(now)
            self.account.take(now)
            self.stats[model]["200"] += 1
            load = max(lim.load(now), self.account.load(now))
            g = self.rng.gauss(0, 1)
        # Latency grows sharply as load nears the limit (a queue near saturation).
        lat = self.base_latency_s * math.exp(self.jitter * g) * (1 + 3 * load ** 6)
        return True, lat


class Handler(BaseHTTPRequestHandler):
    server_version = "nexus-mock/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st: MockState = self.server.state
        if self.path.endswith("/health"):
            return self._send(200, {"ok": True, "service": "nexus-mock"})
        if self.path.endswith("/stats"):
            with st.lock:
                return self._send(200, {m: dict(c) for m, c in st.stats.items()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        st: MockState = self.server.state
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(raw)
            model = body["model"]
        except (ValueError, KeyError):
            return self._send(400, {"error": "bad request"})
        ok, lat = st.admit(model)
        if not ok:
            return self._send(429, {"errors": [{"message": "AiError: Capacity temporarily exceeded, please try again.", "code": 3040}], "success": False})
        time.sleep(lat)
        with st.lock:
            r = st.rng.random() * 100
            nkeys = st.rng.choice([1, 2, 3, 3, 4, 4, 5, 5, 5, 6])
            keys = [st.rng.choice(KEYS) for _ in range(nkeys)]
        if r < st.error_pct:
            return self._send(500, {"errors": [{"message": "internal error"}], "success": False})
        prompt_tokens = int(len(raw) / 4.2)
        if r < st.error_pct + st.invalid_pct:
            msg = {"role": "assistant", "content": "I will move left: hhh"}
            finish, out_tok = "stop", 12
        else:
            name, args = ("screen", {}) if r > 100 - st.screen_pct else ("send_keys", {"keys": keys})
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
            finish, out_tok = "tool_calls", 20 + 6 * nkeys
        self._send(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": out_tok,
                      "total_tokens": prompt_tokens + out_tok},
        })


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512

    def __init__(self, addr, state: MockState):
        super().__init__(addr, Handler)
        self.state = state


def start(host="127.0.0.1", port=0, **kw) -> tuple[MockServer, str]:
    """Start a mock in a background thread; returns (server, base_url ending in /v1)."""
    srv = MockServer((host, port), MockState(**kw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://%s:%d/v1" % (host, srv.server_address[1])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mock_server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--rpm", type=float, default=300, help="per-model limit, requests/min (0 = none)")
    ap.add_argument("--model-rpm", action="append", default=[], metavar="MODEL=RPM", help="per-model override")
    ap.add_argument("--account-rpm", type=float, default=0, help="account-wide limit across models (0 = none)")
    ap.add_argument("--window", type=float, default=60, help="sliding window seconds for the limits")
    ap.add_argument("--latency", type=float, default=0.6, help="base latency seconds")
    ap.add_argument("--jitter", type=float, default=0.35, help="lognormal sigma")
    ap.add_argument("--invalid-pct", type=float, default=3)
    ap.add_argument("--screen-pct", type=float, default=5)
    ap.add_argument("--error-pct", type=float, default=0)
    ap.add_argument("--seed", type=int)
    a = ap.parse_args(argv)
    model_rpm = {k: float(v) for k, v in (s.rsplit("=", 1) for s in a.model_rpm)}
    srv = MockServer((a.host, a.port), MockState(
        rpm=a.rpm, account_rpm=a.account_rpm, window_s=a.window, base_latency_s=a.latency, jitter=a.jitter,
        invalid_pct=a.invalid_pct, screen_pct=a.screen_pct, error_pct=a.error_pct, seed=a.seed, model_rpm=model_rpm))
    print("mock listening on http://%s:%d/v1" % srv.server_address, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
