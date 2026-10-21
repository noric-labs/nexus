"""Short ramps against the mock: the summary must find the 429 knee, the p95 stop, and
tell a per-model limit from an account-wide one. Run: python3 -m unittest discover -s tests -v
(from scaletest/), or `uv run python -m unittest discover -s tests -v`."""

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mock_server import start  # noqa: E402
from probe import cli, summary  # noqa: E402
from probe.request import build_request, estimate_tokens  # noqa: E402
from probe.runner import Config, Probe, report  # noqa: E402

QWEN = "@cf/qwen/qwen3-30b-a3b-fp8"
OSS = "@cf/openai/gpt-oss-20b"


def run(base, models, steps, step_s, modes, cooldown_s=0.0):
    fd, out = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    cfg = Config(base_url=base, models=models, steps=steps, step_s=step_s, modes=modes,
                 out_path=out, quiet=True, max_requests=5000, cooldown_s=cooldown_s)
    p = Probe(cfg)
    rows = p.run()
    return cfg, rows, out


class TestRequest(unittest.TestCase):
    def test_body_shape(self):
        b = build_request(QWEN, random.Random(1))
        self.assertEqual(b["tool_choice"], "auto")
        self.assertEqual(b["max_tokens"], 200)
        self.assertEqual([t["function"]["name"] for t in b["tools"]], ["send_keys", "screen"])
        self.assertIn("Current screen (80x24)", b["messages"][1]["content"])
        self.assertTrue(4800 <= estimate_tokens(b) <= 5400, estimate_tokens(b))

    def test_parse_tool_call(self):
        def resp(name, args):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}]}
        self.assertEqual(summary.parse_tool_call(resp("send_keys", '{"keys":["h","j","Enter"]}')), (True, 3, "send_keys"))
        self.assertEqual(summary.parse_tool_call(resp("send_keys", '{"keys":"h j"}')), (True, 2, "send_keys"))
        self.assertEqual(summary.parse_tool_call(resp("screen", "{}")), (True, 0, "screen"))
        self.assertFalse(summary.parse_tool_call(resp("send_keys", '{"keys":[]}'))[0])
        self.assertFalse(summary.parse_tool_call(resp("send_keys", "{not json"))[0])
        self.assertFalse(summary.parse_tool_call({"choices": [{"message": {"content": "hhh"}}]})[0])


class TestKnee(unittest.TestCase):
    def test_429_knee_per_model(self):
        # Mock limit 180 req/min per model over a 10 s window: 60 and 120 pass, 240 trips.
        srv, base = start(rpm=180, window_s=10, base_latency_s=0.15, seed=7)
        try:
            cfg, rows, out = run(base, [QWEN], [60, 120, 240, 360], 10, ["A1"])
        finally:
            srv.shutdown()
        print("\n" + report(rows, cfg))
        self.assertEqual([r["target_rpm"] for r in rows], [60, 120, 240], "ramp must stop at the knee")
        self.assertEqual(rows[0]["verdict"], "")
        self.assertEqual(rows[1]["verdict"], "")
        self.assertIn("429", rows[2]["verdict"])
        self.assertGreater(rows[2]["pct_429"], 1.0)
        self.assertEqual(summary.sustainable(rows)[QWEN], 120)
        ok = rows[1]
        self.assertGreater(ok["valid_pct"], 85)
        self.assertGreater(ok["keys_per_call"], 2)
        self.assertGreater(ok["tokens_in"], 4000)
        self.assertGreater(ok["usd_per_hour"], 0)
        with open(out) as f:
            recs = [json.loads(line) for line in f]
        need = {"model", "status", "latency_s", "is_429", "is_5xx", "tokens_in", "tokens_out", "valid_tool_call", "keys"}
        self.assertTrue(all(need <= r.keys() for r in recs))
        self.assertTrue(any(r["is_429"] for r in recs))

    def test_account_wide_limit_detected(self):
        # No per-model limit, 200 req/min across the account: A1 passes 120 per model,
        # A2 (two models -> 240 total) trips at 120. Cooldown lets the window drain.
        srv, base = start(rpm=0, account_rpm=200, window_s=10, base_latency_s=0.15, seed=7)
        try:
            cfg, rows, _ = run(base, [QWEN, OSS], [60, 120], 10, ["A1", "A2"], cooldown_s=10)
        finally:
            srv.shutdown()
        text = report(rows, cfg)
        print("\n" + text)
        a1 = summary.sustainable([r for r in rows if r["mode"] == "A1"])
        a2 = summary.sustainable([r for r in rows if r["mode"] == "A2"])
        self.assertEqual(a1, {QWEN: 120, OSS: 120})
        self.assertEqual(a2, {QWEN: 60, OSS: 60})  # A2 tripped at 120
        self.assertIn("ACCOUNT-WIDE", text)

    def test_p95_stop(self):
        srv, base = start(rpm=0, window_s=10, base_latency_s=5.5, jitter=0.05, seed=7)
        try:
            cfg, rows, _ = run(base, [QWEN], [60, 120], 4, ["A1"])
        finally:
            srv.shutdown()
        self.assertEqual(len(rows), 1)
        self.assertIn("p95", rows[0]["verdict"])


class TestSafety(unittest.TestCase):
    def test_dry_run_refuses_remote(self):
        self.assertEqual(cli.main(["--base-url", "http://100.64.255.254:8899/workers-ai/v1"]), 2)

    def test_live_refuses_without_yes_noninteractive(self):
        # Broker health is either unreachable or reports Cloudflare not loaded; both refuse.
        # Point at a local mock with --skip-health to exercise the --yes gate instead.
        srv, base = start()
        try:
            stdin = sys.stdin
            sys.stdin = open(os.devnull)
            try:
                rc = cli.main(["--live", "--base-url", base, "--steps", "60", "--step-seconds", "1"])
            finally:
                sys.stdin.close()
                sys.stdin = stdin
        finally:
            srv.shutdown()
        self.assertEqual(rc, 2)

    def test_hard_cap(self):
        srv, base = start(base_latency_s=0.01)
        try:
            fd, out = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
            cfg = Config(base_url=base, models=[QWEN], steps=[1200], step_s=5, modes=["A1"],
                         out_path=out, quiet=True, max_requests=15)
            p = Probe(cfg)
            p.run()
        finally:
            srv.shutdown()
        self.assertTrue(p.capped)
        self.assertEqual(p.sent, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
