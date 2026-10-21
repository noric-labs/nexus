#!/usr/bin/env python3
"""Tests for tools/agents_check.py: the seed lane/agents.json, broken copies, lane resolution.

    python3 tools/test_agents_check.py
"""

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECK = os.path.join(HERE, "agents_check.py")
SEED = json.load(open(os.path.join(ROOT, "lane", "agents.json")))
QWEN, DEEPSEEK = "@cf/qwen/qwen3-30b-a3b-fp8", "@cf/deepseek-ai/deepseek-v4.1-flash"


class AgentsCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_check(self, cfg, *args, lanes=None):
        path = os.path.join(self.tmp.name, "agents.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        cmd = [sys.executable, CHECK, "--agents", path]
        if lanes is not None:
            lp = os.path.join(self.tmp.name, "lanes.txt")
            with open(lp, "w") as f:
                f.write(lanes)
            cmd += ["--lanes", lp]
        return subprocess.run(cmd + list(args), capture_output=True, text=True)

    def broken(self, mutate, *expect):
        cfg = copy.deepcopy(SEED)
        mutate(cfg)
        r = self.run_check(cfg)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for e in expect:
            self.assertIn(e, r.stderr)

    def test_seed_ok(self):
        r = self.run_check(SEED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)

    def test_unknown_model(self):
        self.broken(lambda c: c["profiles"]["fast"].update(model="@cf/nope/x"),
                    "profiles.fast.model: unknown model '@cf/nope/x'")
        self.broken(lambda c: c["models"].update({"@cf/nope/x": {"min_interval_ms": 1}}),
                    "models.@cf/nope/x: unknown model")

    def test_unknown_profile(self):
        self.broken(lambda c: c["defaults"].update(profile="nope"), "defaults.profile: unknown profile 'nope'")
        self.broken(lambda c: c["games"]["dcss"].update(profile="nope"), "games.dcss.profile: unknown profile 'nope'")

    def test_bad_style(self):
        self.broken(lambda c: c["profiles"]["fast"].update(style="fast"), "profiles.fast.style: must be 'win' or 'explore'")

    def test_bad_interval(self):
        self.broken(lambda c: c["profiles"]["fast"].update(interval_ms=100), "profiles.fast.interval_ms: must be an integer >= 250")
        self.broken(lambda c: c["profiles"]["fast"].update(interval_ms="1000"), "profiles.fast.interval_ms")
        self.broken(lambda c: c["games"]["greed"].update(interval_ms=249), "games.greed.interval_ms")

    def test_bad_game_prompt_keys(self):
        self.broken(lambda c: c["games"].update(tetris={}), "games.tetris: unknown game")
        self.broken(lambda c: c["games"]["greed"].update(prompt="nope.md"), "games.greed.prompt: 'nope.md' is not a file")
        self.broken(lambda c: c["games"]["greed"].update(prompt="../agents.json"), "games.greed.prompt")
        self.broken(lambda c: c["games"]["greed"].update(keys=["<Bogus>"]), "unknown named key '<Bogus>'")
        self.broken(lambda c: c["games"]["greed"].update(keys="hjkl"), "games.greed.keys: must be a non-empty list")

    def test_version(self):
        self.broken(lambda c: c.update(version=2), "version: must be 1")

    def test_unknown_field_warns(self):
        cfg = copy.deepcopy(SEED)
        cfg["profiles"]["fast"]["colour"] = "red"
        r = self.run_check(cfg)
        self.assertEqual(r.returncode, 0)
        self.assertIn("unknown field 'colour'", r.stderr)

    def emit(self, cfg, lanes, *args):
        r = self.run_check(cfg, "--emit", *args, lanes=lanes)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = {}
        for line in r.stdout.splitlines():
            lane, b64 = line.split()
            env = {}
            for kv in base64.b64decode(b64).decode().splitlines():
                k, v = kv.split("=", 1)
                env[k] = v.strip("'")
            out[lane] = env
        return out, r.stderr

    def test_resolution(self):
        lanes = ("l-1 frotz-zork1 none 1\n"           # game profile: thinker
                 "l-2 greed none 2\n"                 # game profile: fast
                 "l-3 greed none 3 profile:thinker\n"
                 "l-4 greed none 4 glm53\n"           # bare profile name
                 f"l-5 frotz-zork1 none 5 {QWEN}\n"   # model id: first profile using it
                 "l-6 greed none 6 @cf/google/gemma-4-26b-a4b-it\n")  # no profile: swapped in
        out, _ = self.emit(SEED, lanes)
        got = {k: (v["NEXUS_PROFILE"], v["NEXUS_MODEL"], v["NEXUS_ACTION_INTERVAL_MS"]) for k, v in out.items()}
        self.assertEqual(got, {
            "l-1": ("thinker", DEEPSEEK, "7000"),
            "l-2": ("fast", QWEN, "1000"),
            "l-3": ("thinker", DEEPSEEK, "7000"),
            "l-4": ("glm53", "@cf/zai-org/glm-5.3", "5000"),
            "l-5": ("fast", QWEN, "1000"),
            "l-6": ("fast", "@cf/google/gemma-4-26b-a4b-it", "1000"),
        })
        self.assertEqual(out["l-1"]["NEXUS_PROMPT_FILE"], "frotz-zork1.md")

    def test_interval_is_max_with_model_min(self):
        cfg = copy.deepcopy(SEED)
        cfg["games"]["greed"]["interval_ms"] = 300
        out, _ = self.emit(cfg, "l-1 greed none 1\nl-2 greed none 2 thinker\n")
        self.assertEqual(out["l-1"]["NEXUS_ACTION_INTERVAL_MS"], "1000")   # qwen3 min 1000
        self.assertEqual(out["l-2"]["NEXUS_ACTION_INTERVAL_MS"], "7000")   # deepseek min 7000

    def test_style_override_and_concurrency_warning(self):
        lanes = "".join(f"l-{i} dcss none {i}\n" for i in range(5))
        out, err = self.emit(SEED, lanes, "--style", "explore")
        self.assertTrue(all(v["NEXUS_PLAY_STYLE"] == "explore" for v in out.values()))
        self.assertIn(f"5 lanes use {DEEPSEEK}, above its max_concurrent_lanes 4", err)

    def test_bad_lanes(self):
        for lanes, msg in (("l-1 greed none 1 nope\n", "unknown profile 'nope'"),
                           ("l-1 greed none 1 profile:nope\n", "unknown profile 'nope'"),
                           ("l-1 greed none 1 @cf/nope/x\n", "unknown model '@cf/nope/x'"),
                           ("l-1 tetris none 1\n", "unknown game 'tetris'")):
            r = self.run_check(SEED, "--plan", lanes=lanes)
            self.assertEqual(r.returncode, 1, lanes)
            self.assertIn(msg, r.stderr)


if __name__ == "__main__":
    unittest.main()
