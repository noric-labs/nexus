#!/usr/bin/env python3
"""Unit + proxy tests for lane/toolfix.py. No live inference: everything runs against
canned responses or lane/mock_openai.py on localhost.

    python3 lane/test_toolfix.py -v
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import toolfix  # noqa: E402

# The request tools as pi sends them for game.ts (strict mode adds additionalProperties).
TOOLS_REQ = [
    {"type": "function", "function": {"name": "send_keys", "description": "type keys", "strict": True,
                                      "parameters": {"type": "object", "additionalProperties": False,
                                                     "required": ["keys"],
                                                     "properties": {"keys": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "screen", "description": "look", "strict": True,
                                      "parameters": {"type": "object", "additionalProperties": False,
                                                     "properties": {}}}},
]
TOOLS = toolfix.declared_tools({"tools": TOOLS_REQ})

QWEN = {"content": "", "tool_calls": None,
        "reasoning_content": '<tool_call>\n{"name": "send_keys", "arguments": {"keys": "i"}}\n</tool_call>'}
GLM = {"content": '<tool_call>send_keys("<Down>")', "tool_calls": []}
GPTOSS = {"content": '{"keys":"j"}', "tool_calls": []}


def completion(model, msg_fields, finish="stop"):
    return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": model,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"index": 0, "message": dict({"role": "assistant"}, **msg_fields),
                         "finish_reason": finish}]}


def sse(model, msg_fields, pieces=3, finish="stop"):
    """Stream the fields as SSE the way an OpenAI-compatible server would."""
    def ev(delta, fr=None, usage=None):
        c = {"id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1, "model": model,
             "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": fr}]}
        if usage:
            c["usage"] = usage
        return b"data: " + json.dumps(c).encode() + b"\n\n"
    out = ev({"role": "assistant", "content": ""})
    tc = msg_fields.get("tool_calls")
    for f, v in msg_fields.items():
        if f == "tool_calls" or not v:
            continue
        step = max(1, len(v) // pieces)
        for i in range(0, len(v), step):
            out += ev({f: v[i:i + step], "tool_calls": tc})
    out += ev({}, finish)
    out += ev(None, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    return out + b"data: [DONE]\n\n"


def stream_calls(raw):
    """Reassemble tool calls + finish + text from SSE bytes, as pi would."""
    calls, finish, text, reasoning, usage = {}, None, "", "", None
    for _, d in toolfix.parse_sse(raw):
        if not isinstance(d, dict):
            continue
        usage = d.get("usage") or usage
        for ch in d.get("choices") or []:
            finish = ch.get("finish_reason") or finish
            dl = ch.get("delta") or {}
            text += dl.get("content") or ""
            reasoning += dl.get("reasoning_content") or ""
            for tc in dl.get("tool_calls") or []:
                c = calls.setdefault(tc["index"], {"name": "", "arguments": "", "id": ""})
                c["id"] = tc.get("id") or c["id"]
                c["name"] += (tc.get("function") or {}).get("name") or ""
                c["arguments"] += (tc.get("function") or {}).get("arguments") or ""
    return [calls[i] for i in sorted(calls)], finish, text, reasoning, usage


class NonStreaming(unittest.TestCase):
    def check(self, fields, model, name, args, shape):
        resp, info = toolfix.fix_completion(completion(model, fields), TOOLS)
        self.assertEqual(info["outcome"], "recovered")
        self.assertEqual(info["shape"], shape)
        ch = resp["choices"][0]
        self.assertEqual(ch["finish_reason"], "tool_calls")
        tcs = ch["message"]["tool_calls"]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["type"], "function")
        self.assertTrue(tcs[0]["id"])
        self.assertEqual(tcs[0]["function"]["name"], name)
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]), args)
        return ch["message"]

    def test_qwen_reasoning(self):
        msg = self.check(QWEN, "@cf/qwen/qwen3-30b-a3b-fp8", "send_keys", {"keys": "i"},
                         "reasoning_content:tool_call_json")
        self.assertEqual(msg["reasoning_content"], "")
        self.assertIsNone(msg["content"])

    def test_glm_content(self):
        msg = self.check(GLM, "@cf/zai-org/glm-4.7-flash", "send_keys", {"keys": "<Down>"},
                         "content:tool_call_func")
        self.assertIsNone(msg["content"])

    def test_gptoss_bare_args(self):
        self.check(GPTOSS, "@cf/openai/gpt-oss-20b", "send_keys", {"keys": "j"}, "content:bare_json_args")

    def test_structured_untouched(self):
        orig = completion("@cf/google/gemma-4-26b-a4b-it", {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "send_keys", "arguments": '{"keys":"x"}'}}]},
            finish="tool_calls")
        snap = json.dumps(orig)
        resp, info = toolfix.fix_completion(orig, TOOLS)
        self.assertEqual(info["outcome"], "structured")
        self.assertEqual(json.dumps(resp), snap)

    def test_variants(self):
        cases = [
            ('<tool_call>screen()</tool_call>', "screen", {}),
            ("<tool_call>send_keys(keys='look<Enter>')", "send_keys", {"keys": "look<Enter>"}),
            ('<tool_call>send_keys({"keys": "hjkl"})', "send_keys", {"keys": "hjkl"}),
            ("<tool_call>send_keys(<Up><Up>)", "send_keys", {"keys": "<Up><Up>"}),
            ("<tool_call>send_keys(keys=<Esc>)", "send_keys", {"keys": "<Esc>"}),
            ('<tool_call>{"name": "send_keys", "arguments": "{\\"keys\\": \\"q\\"}"}</tool_call>',
             "send_keys", {"keys": "q"}),
            ('I will move down.\nsend_keys("jjj")', "send_keys", {"keys": "jjj"}),
            ('```json\n{"keys": "x<Enter>"}\n```', "send_keys", {"keys": "x<Enter>"}),
            ('{"name": "send_keys", "parameters": {"keys": "y"}}', "send_keys", {"keys": "y"}),
            ('<|channel|>commentary to=functions.send_keys <|constrain|>json<|message|>{"keys":"k"}',
             "send_keys", {"keys": "k"}),
            ('{"keys": 5}', "send_keys", {"keys": "5"}),
        ]
        for text, name, args in cases:
            with self.subTest(text=text):
                resp, info = toolfix.fix_completion(completion("m", {"content": text}), TOOLS)
                self.assertEqual(info["outcome"], "recovered", text)
                fn = resp["choices"][0]["message"]["tool_calls"][0]["function"]
                self.assertEqual((fn["name"], json.loads(fn["arguments"])), (name, args))

    def test_leftover_text_kept(self):
        resp, _ = toolfix.fix_completion(
            completion("m", {"content": 'Going down.\n<tool_call>send_keys("j")</tool_call>'}), TOOLS)
        self.assertEqual(resp["choices"][0]["message"]["content"], "Going down.")

    def test_multiple_calls(self):
        text = '<tool_call>{"name":"screen","arguments":{}}</tool_call>\n<tool_call>send_keys("a")</tool_call>'
        resp, info = toolfix.fix_completion(completion("m", {"content": text}), TOOLS)
        names = [t["function"]["name"] for t in resp["choices"][0]["message"]["tool_calls"]]
        self.assertEqual(names, ["screen", "send_keys"])

    def test_never_invents(self):
        cases = [
            "", "I will keep exploring.", "Let me look at the screen first.",
            '{"direction": "north"}',                      # keys match no tool
            "{}",                                          # empty object is not a call
            '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>',   # undeclared tool
            '<tool_call>{"name": "send_keys", "arguments": {"key": "j"}}',  # wrong param
            '<tool_call>{"name": "send_keys", "arguments": {}}',            # missing required
            "<tool_call>send_keys(1, 2)",                                  # too many positionals
            "rm_rf('/')", '{"keys": "j"} and more text', "send_keys is the tool to use",
            "Then call screen to look", '<tool_call>{"name": "send_keys", "argu',  # truncated
        ]
        for text in cases:
            with self.subTest(text=text):
                orig = completion("m", {"content": text, "tool_calls": []})
                snap = json.dumps(orig)
                resp, info = toolfix.fix_completion(orig, TOOLS)
                self.assertEqual(info["outcome"], "none")
                self.assertEqual(json.dumps(resp), snap)

    def test_no_tools_declared(self):
        resp, info = toolfix.fix_completion(completion("m", GPTOSS), {})
        self.assertEqual(info["outcome"], "none")


class Streaming(unittest.TestCase):
    def check(self, fields, model, name, args):
        out, info = toolfix.fix_stream(sse(model, fields), TOOLS)
        self.assertEqual(info["outcome"], "recovered")
        calls, finish, text, reasoning, usage = stream_calls(out)
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], name)
        self.assertEqual(json.loads(calls[0]["arguments"]), args)
        self.assertTrue(calls[0]["id"])
        self.assertEqual(usage["total_tokens"], 15)
        self.assertTrue(out.endswith(b"data: [DONE]\n\n"))
        self.assertNotIn("<tool_call>", text + reasoning)
        return info

    def test_qwen(self):
        self.check(QWEN, "@cf/qwen/qwen3-30b-a3b-fp8", "send_keys", {"keys": "i"})

    def test_glm(self):
        self.check(GLM, "@cf/zai-org/glm-4.7-flash", "send_keys", {"keys": "<Down>"})

    def test_gptoss(self):
        self.check(GPTOSS, "@cf/openai/gpt-oss-20b", "send_keys", {"keys": "j"})

    def test_one_char_chunks(self):
        for fields in (QWEN, GLM, GPTOSS):
            out, info = toolfix.fix_stream(sse("m", fields, pieces=10**6), TOOLS)
            self.assertEqual(info["outcome"], "recovered")

    def test_passthrough_bytes(self):
        for fields in ({"content": "I will keep exploring."}, {"content": "{}"}):
            raw = sse("m", fields)
            out, info = toolfix.fix_stream(raw, TOOLS)
            self.assertEqual(info["outcome"], "none")
            self.assertIs(out, raw)
        raw = b"data: " + json.dumps({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c", "type": "function", "function": {"name": "screen", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}).encode() + b"\n\ndata: [DONE]\n\n"
        out, info = toolfix.fix_stream(raw, TOOLS)
        self.assertEqual(info["outcome"], "structured")
        self.assertIs(out, raw)


def slurp(path):
    with open(path) as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, t=10):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} never opened")


class ProxyAgainstMock(unittest.TestCase):
    """toolfix.py in front of mock_openai.py --text-tool-shape, over real HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="toolfix-test-")
        cls.procs = []
        cls.ports = {}
        for shape in ("qwen", "glm", "gptoss"):
            mp = free_port()
            cls.procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "mock_openai.py"),
                                               "--port", str(mp), "--latency", "0",
                                               "--text-tool-shape", shape], stderr=subprocess.DEVNULL))
            wait_port(mp)
            pf = os.path.join(cls.tmp, shape + ".port")
            cls.procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "toolfix.py"),
                                               "--upstream", f"http://127.0.0.1:{mp}/v1", "--port-file", pf,
                                               "--log", os.path.join(cls.tmp, shape + ".jsonl"),
                                               "--stats", os.path.join(cls.tmp, shape + ".stats.json")],
                                              stderr=subprocess.DEVNULL))
            for _ in range(100):
                if os.path.exists(pf):
                    break
                time.sleep(0.05)
            cls.ports[shape] = int(slurp(pf))

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.kill()
            p.wait()

    def post(self, shape, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.ports[shape]}/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json", "authorization": "Bearer x"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get("content-type"), r.read()

    def test_all_shapes_both_modes(self):
        for shape in ("qwen", "glm", "gptoss"):
            for stream in (False, True):
                with self.subTest(shape=shape, stream=stream):
                    for _ in range(4):  # covers screen() turns too (qwen/glm)
                        ctype, data = self.post(shape, {"model": "m", "stream": stream, "tools": TOOLS_REQ,
                                                        "messages": [{"role": "user", "content": "go"}]})
                        if stream:
                            self.assertIn("text/event-stream", ctype)
                            calls, finish, *_ = stream_calls(data)
                        else:
                            ch = json.loads(data)["choices"][0]
                            finish = ch["finish_reason"]
                            calls = [t["function"] for t in ch["message"]["tool_calls"]]
                        self.assertEqual(finish, "tool_calls")
                        self.assertEqual(len(calls), 1)
                        self.assertIn(calls[0]["name"], ("send_keys", "screen"))
                        args = json.loads(calls[0]["arguments"])
                        self.assertEqual(set(args), {"keys"} if calls[0]["name"] == "send_keys" else set())
            stats = json.loads(slurp(os.path.join(self.tmp, shape + ".stats.json")))
            self.assertEqual(stats["recovered"], 8)
            self.assertEqual(stats["none"], 0)
            lines = [json.loads(l) for l in slurp(os.path.join(self.tmp, shape + ".jsonl")).splitlines()]
            self.assertEqual(len(lines), 8)
            self.assertTrue(all(l["event"] == "recovered" and l["model"] == "m" for l in lines))

    def test_get_models_passthrough(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ports['glm']}/models", timeout=5) as r:
            self.assertIn("@cf/zai-org/glm-4.7-flash", r.read().decode())

    def test_upstream_unreachable(self):
        pf = os.path.join(self.tmp, "dead.port")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "toolfix.py"), "--upstream",
                              f"http://127.0.0.1:{free_port()}/v1", "--port-file", pf], stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if os.path.exists(pf):
                    break
                time.sleep(0.05)
            req = urllib.request.Request(f"http://127.0.0.1:{int(slurp(pf))}/chat/completions",
                                         data=b"{}", headers={"content-type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 502)
            cm.exception.close()
        finally:
            p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
