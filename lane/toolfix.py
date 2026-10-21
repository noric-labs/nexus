#!/usr/bin/env python3
"""toolfix: a local OpenAI-compatible proxy that recovers text-encoded tool calls.

Through Workers AI only Gemma returns structured `tool_calls` (D-016). The other models
write the call as text instead:

    qwen3    content '', tool_calls null,
             reasoning_content '<tool_call>\\n{"name": "send_keys", "arguments": {...}}\\n</tool_call>'
    glm      content '<tool_call>send_keys("<Down>")', tool_calls []
    gpt-oss  content '{"keys":"j"}', tool_calls []

pi talks to this proxy instead of the broker. Requests are forwarded unchanged. When a
response to a request that declared tools has no structured tool call, but its content or
reasoning holds a recognisable call to one of those declared tools, the proxy rewrites it
into a proper `tool_calls` entry (JSON arguments checked against the tool's schema) and
sets finish_reason to "tool_calls". Nothing parseable -> the response passes through
untouched; calls are never invented.

Streaming (pi always sends stream:true for openai-completions): when the request declares
tools, the SSE stream is buffered to the end, then either replayed byte-for-byte or, if a
call was recovered, re-emitted as a synthetic stream (role, leftover text, tool_calls,
finish, usage, [DONE]). Requests without tools stream through live.

Recovery rules (only for tool names the request declared):
  1. <tool_call> ... </tool_call> blocks (closing tag optional) holding either
     JSON {"name": N, "arguments"|"parameters": {...} or "<json string>"} or N(args).
  2. harmony-style `functions.N` (or `to=functions.N`) followed by a JSON object.
  3. N(args) at the start of a line (optionally inside a ``` fence); args may be
     positional literals, keyword literals, or one JSON object; for a tool with exactly one
     string parameter a single positional value (or unquoted text) maps onto it.
  4. A bare JSON object as the whole text (fences allowed): {"name": N, "arguments": ...},
     or a non-empty object whose keys match exactly one declared tool's schema
     (required <= keys <= properties), e.g. {"keys":"j"} -> send_keys.
Arguments must validate: no unknown keys, required present, string params get strings
(numbers/bools are stringified). Content is searched first, then reasoning; the matched
span is removed from that field and any remaining text is kept.

Each response to a tool-declaring request is counted (structured / recovered / none);
every recovery and every miss is appended as one JSON line to --log, and the running
counts are written to --stats (and served at GET /toolfix/stats).

    python3 lane/toolfix.py --upstream http://100.64.255.254:8899/workers-ai/v1 \\
        [--host 127.0.0.1] [--port 0] [--port-file F] [--log toolfix.jsonl] \\
        [--stats toolfix.stats.json] [--parent-pid PID]

Point pi's baseUrl at http://127.0.0.1:PORT (no /v1): the request path is appended to
--upstream as is.
"""

import argparse
import ast
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}

# ---------------------------------------------------------------------------- tools


def flatten_text_content(body):
    """Workers AI's schema for some models (qwen3, gpt-oss) only accepts string message
    content and rejects OpenAI multi-part arrays with a 400. Join text-only part arrays
    into one string, and give assistant tool-call turns "" instead of null content (also a
    400 there). Returns True when the body changed."""
    changed = False
    for m in (body or {}).get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content") is None:
            m["content"] = ""
            changed = True
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and c and all(
                isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                for p in c):
            m["content"] = "\n".join(p["text"] for p in c)
            changed = True
    return changed


def declared_tools(body):
    """{name: parameters-schema} for the request's function tools."""
    out = {}
    for t in (body or {}).get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            params = fn.get("parameters")
            out[name] = params if isinstance(params, dict) else {}
    return out


def _props(schema):
    p = schema.get("properties")
    return p if isinstance(p, dict) else {}


def _single_string_param(schema):
    props = _props(schema)
    if len(props) == 1:
        (k, v), = props.items()
        if isinstance(v, dict) and v.get("type") in ("string", None):
            return k
    return None


def coerce_args(schema, args):
    """Validate/coerce args against a JSON schema object. Returns dict or None."""
    props = _props(schema)
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    if isinstance(args, str):
        s = args.strip()
        if s.startswith("{"):
            try:
                args = json.loads(s)
            except ValueError:
                return None
        else:
            key = _single_string_param(schema)
            if key is None:
                return None
            args = {key: args}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    if props and any(k not in props for k in args):
        return None
    if not props and args and schema.get("additionalProperties") is False:
        return None
    if any(r not in args for r in required):
        return None
    out = {}
    for k, v in args.items():
        t = (props.get(k) or {}).get("type") if isinstance(props.get(k), dict) else None
        if t == "string" and not isinstance(v, str):
            if isinstance(v, bool) or v is None or not isinstance(v, (int, float)):
                return None
            v = str(v)
        elif t in ("integer", "number") and isinstance(v, str):
            try:
                v = int(v) if t == "integer" else float(v)
            except ValueError:
                return None
        out[k] = v
    return out


# ---------------------------------------------------------------------------- parsing

_DEC = json.JSONDecoder()


def _json_at(text, i):
    """Decode one JSON value starting at text[i] (after whitespace). -> (value, end) or None."""
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    try:
        return _DEC.raw_decode(text, i)
    except ValueError:
        return None


def _paren_span(text, i):
    """text[i] == '('; return index after the matching ')' honouring quotes, or None."""
    depth, q, esc = 0, None, False
    for j in range(i, len(text)):
        c = text[j]
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c in "\"'":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j + 1 if c == ")" else None
    return None


def _call_args(inner, schema):
    """Parse the text between N( and ) into an args dict (or None)."""
    s = inner.strip()
    if not s:
        return coerce_args(schema, {})
    if s.startswith("{"):
        r = _json_at(s, 0)
        if r and not s[r[1]:].strip() and isinstance(r[0], dict):
            return coerce_args(schema, r[0])
    try:
        node = ast.parse("f(" + s + ")", mode="eval").body
        pos = [ast.literal_eval(a) for a in node.args]
        kw = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg}
        if len(kw) != len(node.keywords):
            return None
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        pos, kw = None, None
    if pos is None:
        # Not a Python literal list (e.g. JSON true/null, or an unquoted key string).
        try:
            pos, kw = json.loads("[" + s + "]"), {}
        except ValueError:
            key = _single_string_param(schema)
            if not key or "\n" in s:
                return None
            m = re.match(r"(\w+)\s*[=:]\s*(.*)$", s, re.S)
            if m and m.group(1) == key:  # keys=<Down> (unquoted keyword)
                s = m.group(2).strip()
            return coerce_args(schema, {key: s})
    props = list(_props(schema))
    if len(pos) == 1 and isinstance(pos[0], dict) and not kw:
        return coerce_args(schema, pos[0])
    if len(pos) > len(props):
        return None
    args = dict(zip(props, pos))
    for k, v in kw.items():
        if k in args:
            return None
        args[k] = v
    return coerce_args(schema, args)


def _from_obj(obj, tools):
    """A JSON object naming a tool: {"name", "arguments"|"parameters"} (or OpenAI-ish nesting)."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name")
    if not isinstance(name, str) or name not in tools:
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    a = coerce_args(tools[name], args)
    return (name, a) if a is not None else None


def _from_keys(obj, tools):
    """A bare non-empty args object whose keys fit exactly one declared tool."""
    if not isinstance(obj, dict) or not obj:
        return None
    hits = []
    for name, schema in tools.items():
        props = _props(schema)
        if props and all(k in props for k in obj):
            a = coerce_args(schema, obj)
            if a is not None:
                hits.append((name, a))
    return hits[0] if len(hits) == 1 else None


def _func_call(text, i, tools):
    """At text[i] expect `name(`; return ((name, args), end) or None."""
    m = re.compile(r"([A-Za-z_][\w.-]*)\s*\(").match(text, i)
    if not m or m.group(1) not in tools:
        return None
    end = _paren_span(text, m.end() - 1)
    if end is None:
        return None
    args = _call_args(text[m.end():end - 1], tools[m.group(1)])
    return ((m.group(1), args), end) if args is not None else None


_TAG = re.compile(r"<tool_call>", re.I)
_TAG_END = re.compile(r"\s*</tool_call>", re.I)
_HARMONY = re.compile(r"(?:to=)?functions\.([A-Za-z_][\w-]*)")
_LINE_CALL = re.compile(r"(?m)^[ \t>*`-]*(?=[A-Za-z_][\w.-]*\s*\()")
_FENCE = re.compile(r"^\s*```[\w-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def find_calls(text, tools):
    """-> (calls [(name, args)], spans [(start, end)], shape) for one text field."""
    if not text or not tools:
        return [], [], None
    calls, spans = [], []

    # 1. <tool_call> blocks.
    for m in _TAG.finditer(text):
        j = m.end()
        r = _json_at(text, j)
        got = None
        if r and isinstance(r[0], dict):
            got, end = _from_obj(r[0], tools), r[1]
            shape = "tool_call_json"
        else:
            k = j
            while k < len(text) and text[k] in " \t\r\n":
                k += 1
            fr = _func_call(text, k, tools)
            if fr:
                got, end = fr
                shape = "tool_call_func"
        if got:
            e = _TAG_END.match(text, end)
            calls.append(got)
            spans.append((m.start(), e.end() if e else end))
    if calls:
        return calls, spans, shape

    # 2. harmony: functions.NAME ... {json}
    for m in _HARMONY.finditer(text):
        if m.group(1) not in tools:
            continue
        j = text.find("{", m.end())
        if j < 0 or j - m.end() > 80:
            continue
        r = _json_at(text, j)
        if r and isinstance(r[0], dict):
            a = coerce_args(tools[m.group(1)], r[0])
            if a is not None:
                calls.append((m.group(1), a))
                spans.append((m.start(), r[1]))
    if calls:
        return calls, spans, "harmony"

    # 3. NAME(args) at the start of a line.
    for m in _LINE_CALL.finditer(text):
        fr = _func_call(text, m.end(), tools)
        if fr:
            calls.append(fr[0])
            spans.append((m.end(), fr[1]))
    if calls:
        return calls, spans, "func"

    # 4. the whole text is one JSON object (optionally fenced).
    body = text.strip()
    fm = _FENCE.match(body)
    if fm:
        body = fm.group(1).strip()
    if body.startswith("{"):
        r = _json_at(body, 0)
        if r and not body[r[1]:].strip():
            got = _from_obj(r[0], tools)
            shape = "bare_json_named"
            if not got:
                got, shape = _from_keys(r[0], tools), "bare_json_args"
            if got:
                return [got], [(0, len(text))], shape
    return [], [], None


def _cut(text, spans):
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + text[e:]
    return text.strip()


def recover(fields, tools):
    """fields: {"content": str, "<reasoning field>": str}. Content first, then reasoning.

    -> None or {"calls": [...], "field": f, "shape": "<field>:<shape>", "leftover": {field: text}}.
    """
    for f in ("content",) + REASONING_FIELDS:
        text = fields.get(f)
        if not isinstance(text, str) or not text.strip():
            continue
        calls, spans, shape = find_calls(text, tools)
        if calls:
            left = dict(fields)
            left[f] = _cut(text, spans)
            return {"calls": calls, "field": f, "shape": f"{f}:{shape}", "leftover": left}
    return None


def _tool_call_objs(calls):
    return [{"index": i, "id": "call_tf" + uuid.uuid4().hex[:12], "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]


def fix_completion(resp, tools):
    """Non-streaming body. Returns (resp, info) with resp modified in place when recovered."""
    choices = resp.get("choices") if isinstance(resp, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return resp, {"outcome": "none", "why": "no choices"}
    msg = choices[0].get("message") or {}
    if msg.get("tool_calls"):
        return resp, {"outcome": "structured"}
    fields = {f: msg.get(f) for f in ("content",) + REASONING_FIELDS if isinstance(msg.get(f), str)}
    rec = recover(fields, tools)
    if not rec:
        return resp, {"outcome": "none", "fields": fields}
    for f, v in rec["leftover"].items():
        if f == "content":
            msg[f] = v or None
        else:
            msg[f] = v
    tcs = _tool_call_objs(rec["calls"])
    for tc in tcs:
        del tc["index"]
    msg["tool_calls"] = tcs
    choices[0]["message"] = msg
    choices[0]["finish_reason"] = "tool_calls"
    return resp, {"outcome": "recovered", **rec}


def parse_sse(raw):
    """bytes -> list of (event_bytes, data_obj_or_None_or_'[DONE]')."""
    out = []
    for ev in re.split(rb"\r?\n\r?\n", raw):
        if not ev.strip():
            continue
        data = [ln[5:].lstrip() for ln in re.split(rb"\r?\n", ev) if ln.startswith(b"data:")]
        payload = b"\n".join(data).strip()
        if payload == b"[DONE]":
            out.append((ev, "[DONE]"))
            continue
        try:
            out.append((ev, json.loads(payload) if payload else None))
        except ValueError:
            out.append((ev, None))
    return out


def fix_stream(raw, tools):
    """Buffered SSE bytes -> (bytes_to_send, info)."""
    events = parse_sse(raw)
    fields = {}
    structured, finish, usage, meta = False, None, None, {}
    for _, d in events:
        if not isinstance(d, dict):
            continue
        for k in ("id", "created", "model", "object", "system_fingerprint"):
            if k in d and k not in meta:
                meta[k] = d[k]
        if d.get("usage"):
            usage = d["usage"]
        for ch in d.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}
            if delta.get("tool_calls"):
                structured = True
            for f in ("content",) + REASONING_FIELDS:
                v = delta.get(f)
                if isinstance(v, str) and v:
                    fields[f] = fields.get(f, "") + v
    if structured:
        return raw, {"outcome": "structured"}
    rec = recover(fields, tools)
    if not rec:
        return raw, {"outcome": "none", "fields": fields, "finish": finish}
    base = {"id": meta.get("id") or "chatcmpl-tf" + uuid.uuid4().hex[:10],
            "object": meta.get("object") or "chat.completion.chunk",
            "created": meta.get("created") or int(time.time()), "model": meta.get("model") or ""}
    chunks = []

    def add(delta, finish_reason=None):
        chunks.append(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish_reason}]))

    add({"role": "assistant", "content": ""})
    for f in REASONING_FIELDS:
        if rec["leftover"].get(f):
            add({f: rec["leftover"][f]})
    if rec["leftover"].get("content"):
        add({"content": rec["leftover"]["content"]})
    for tc in _tool_call_objs(rec["calls"]):
        add({"tool_calls": [tc]})
    add({}, "tool_calls")
    if usage:
        chunks.append(dict(base, choices=[], usage=usage))
    out = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
    return out, {"outcome": "recovered", **rec}


# ---------------------------------------------------------------------------- server


class Recorder:
    def __init__(self, log_path, stats_path):
        self.log_path, self.stats_path = log_path, stats_path
        self.lock = threading.Lock()
        self.counts = {"requests": 0, "tool_requests": 0, "structured": 0, "recovered": 0,
                       "none": 0, "upstream_errors": 0, "by_shape": {}, "by_model": {}}

    def record(self, model, stream, info, status=200):
        with self.lock:
            c = self.counts
            outcome = info.get("outcome")
            if status != 200:
                c["upstream_errors"] += 1
                outcome = "error"
            else:
                c[outcome] += 1
            m = c["by_model"].setdefault(model, {"structured": 0, "recovered": 0, "none": 0, "error": 0})
            m[outcome] += 1
            line = None
            if outcome == "recovered":
                c["by_shape"][info["shape"]] = c["by_shape"].get(info["shape"], 0) + 1
                line = {"ts": time.time(), "event": "recovered", "model": model, "stream": stream,
                        "shape": info["shape"], "calls": [{"name": n, "arguments": a} for n, a in info["calls"]],
                        "n": c["recovered"]}
            elif outcome == "none":
                text = {k: v[:300] for k, v in (info.get("fields") or {}).items() if v}
                line = {"ts": time.time(), "event": "no_call", "model": model, "stream": stream, "text": text}
            if line and self.log_path:
                with open(self.log_path, "a") as f:
                    f.write(json.dumps(line) + "\n")
            self._write_stats()

    def _write_stats(self):
        if not self.stats_path:
            return
        tmp = self.stats_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(self.counts, updated=time.time()), f)
        os.replace(tmp, self.stats_path)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None  # urllib.parse.SplitResult
    rec = None
    timeout_s = 300

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, headers, body):
        self.send_response(code)
        for k, v in headers:
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _conn(self):
        u = self.upstream
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port, timeout=self.timeout_s)

    def do_GET(self):
        if self.path.rstrip("/") == "/toolfix/stats":
            with self.rec.lock:
                body = json.dumps(self.rec.counts).encode()
            return self._send(200, [("content-type", "application/json")], body)
        self._forward(b"")

    def do_POST(self):
        self._forward(self.rfile.read(int(self.headers.get("content-length") or 0)))

    def _forward(self, raw):
        path = self.upstream.path.rstrip("/") + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["accept-encoding"] = "identity"
        headers["host"] = self.upstream.netloc
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        tools = declared_tools(body) if isinstance(body, dict) and self.path.rstrip("/").endswith(
            "/chat/completions") else {}
        stream = bool(isinstance(body, dict) and body.get("stream"))
        model = body.get("model", "") if isinstance(body, dict) else ""
        if isinstance(body, dict) and flatten_text_content(body):
            raw = json.dumps(body).encode()
            for k in [k for k in headers if k.lower() == "content-length"]:
                del headers[k]
            headers["content-length"] = str(len(raw))
        with self.rec.lock:
            self.rec.counts["requests"] += 1
            if tools:
                self.rec.counts["tool_requests"] += 1
        conn = self._conn()
        try:
            conn.request(self.command, path, body=raw if raw else None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            msg = json.dumps({"error": {"message": f"toolfix: upstream unreachable: {e}", "type": "proxy"}})
            return self._send(502, [("content-type", "application/json")], msg.encode())
        try:
            ctype = resp.getheader("content-type", "")
            is_sse = "text/event-stream" in ctype
            if resp.status != 200 or not tools:
                if tools:
                    self.rec.record(model, stream, {"outcome": "error"}, resp.status)
                dump = os.environ.get("NEXUS_TOOLFIX_DUMP")
                if dump and resp.status >= 400:  # debugging aid: keep the rejected request
                    with open(dump, "a") as f:
                        f.write(json.dumps({"status": resp.status, "request": body}) + "\n")
                if is_sse:
                    return self._relay_stream(resp)
                return self._send(resp.status, resp.getheaders(), resp.read())
            data = resp.read()
            if is_sse:
                out, info = fix_stream(data, tools)
                self.rec.record(model, True, info)
                self.send_response(200)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            try:
                obj = json.loads(data)
            except ValueError:
                return self._send(resp.status, resp.getheaders(), data)
            obj, info = fix_completion(obj, tools)
            self.rec.record(model, False, info)
            out = data if info["outcome"] != "recovered" else json.dumps(obj).encode()
            return self._send(200, resp.getheaders(), out)
        finally:
            conn.close()

    def _relay_stream(self, resp):
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                b = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not b:
                    break
                self.wfile.write(b)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--upstream", required=True, help="OpenAI-compatible base URL, e.g. .../workers-ai/v1")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p.add_argument("--port-file", default="", help="write the bound port here once listening")
    p.add_argument("--log", default="", help="JSONL: one line per recovery / missed call")
    p.add_argument("--stats", default="", help="JSON counts, rewritten atomically")
    p.add_argument("--parent-pid", type=int, default=0, help="exit when this pid is gone")
    a = p.parse_args()
    Proxy.upstream = urllib.parse.urlsplit(a.upstream.rstrip("/"))
    Proxy.rec = Recorder(a.log, a.stats)
    srv = ThreadingHTTPServer((a.host, a.port), Proxy)
    srv.daemon_threads = True
    port = srv.server_address[1]
    if a.port_file:
        tmp = a.port_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{port}\n")
        os.replace(tmp, a.port_file)
    print(f"toolfix listening on http://{a.host}:{port} -> {a.upstream}", file=sys.stderr, flush=True)
    if a.parent_pid:
        def watch():
            while True:
                time.sleep(1)
                try:
                    os.kill(a.parent_pid, 0)
                except ProcessLookupError:
                    os._exit(0)
                except PermissionError:
                    pass
        threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
