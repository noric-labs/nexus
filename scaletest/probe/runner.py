"""Open-loop ramp runner: Poisson arrivals per model, one thread per request."""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import summary
from .request import build_request


@dataclass
class Config:
    base_url: str
    models: list[str]
    steps: list[float]
    step_s: float = 300.0
    modes: list[str] = field(default_factory=lambda: ["A1", "A2"])
    target_tokens: int = 5000
    max_tokens: int = 200
    max_requests: int = 25000
    max_inflight: int = 1000
    timeout_s: float = 30.0
    api_key: str = "placeholder"
    seed: int = 1
    out_path: str = "probe.jsonl"
    cooldown_s: float = 0.0
    quiet: bool = False


def planned_requests(cfg: Config) -> int:
    per_model = sum(r * cfg.step_s / 60.0 for r in cfg.steps)
    return int(per_model * len(cfg.models) * len(cfg.modes))


class Probe:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"
        self.lock = threading.Lock()
        self.sent = 0
        self.capped = False
        self.inflight = threading.BoundedSemaphore(cfg.max_inflight)
        self.out = open(cfg.out_path, "a", buffering=1)
        rng = random.Random(cfg.seed)
        # Pre-build a pool of bodies per model so request building never limits the rate.
        self.pool = {m: [json.dumps(build_request(m, rng, cfg.target_tokens, cfg.max_tokens)).encode() for _ in range(24)]
                     for m in cfg.models}
        self.rows: list[dict] = []

    def log(self, *a) -> None:
        if not self.cfg.quiet:
            print(*a, flush=True)

    # -- one request ---------------------------------------------------------------
    def _do(self, rec: dict, body: bytes, sink: list, threads_done: threading.Semaphore) -> None:
        t0 = time.monotonic()
        status: int | str
        resp = None
        try:
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + self.cfg.api_key})
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:
                status = r.status
                resp = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                rec["error"] = e.read()[:300].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        except (TimeoutError, OSError) as e:
            status = "timeout" if "timed out" in str(e) else "conn_error"
            rec["error"] = str(e)[:200]
        except ValueError as e:
            status = "bad_json"
            rec["error"] = str(e)[:200]
        finally:
            self.inflight.release()
        rec["latency_s"] = round(time.monotonic() - t0, 4)
        rec["status"] = status
        rec["is_429"] = status == 429
        rec["is_5xx"] = isinstance(status, int) and status >= 500
        usage = (resp or {}).get("usage") or {}
        rec["tokens_in"] = usage.get("prompt_tokens") or 0
        rec["tokens_out"] = usage.get("completion_tokens") or 0
        if status == 200 and resp is not None:
            valid, keys, tool = summary.parse_tool_call(resp)
        else:
            valid, keys, tool = False, 0, ""
        rec.update(valid_tool_call=valid, keys=keys, tool=tool)
        with self.lock:
            sink.append(rec)
            self.out.write(json.dumps(rec) + "\n")
        threads_done.release()

    # -- one model at one rate for one step -------------------------------------------
    def _arrivals(self, mode: str, step: int, model: str, rpm: float, t_end: float,
                  sink: list, started: list, done: threading.Semaphore, rng: random.Random) -> None:
        pool = self.pool[model]
        lam = rpm / 60.0
        t = time.monotonic()
        while True:
            t += rng.expovariate(lam)  # open loop: schedule from the clock, not from replies
            if t >= t_end:
                return
            delay = t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            with self.lock:
                if self.sent >= self.cfg.max_requests:
                    self.capped = True
                    return
                self.sent += 1
            rec = {"ts": time.time(), "mode": mode, "step": step, "target_rpm": rpm, "model": model}
            if not self.inflight.acquire(blocking=False):
                rec.update(status="skipped_inflight", latency_s=0.0, is_429=False, is_5xx=False,
                           tokens_in=0, tokens_out=0, valid_tool_call=False, keys=0, tool="")
                with self.lock:
                    sink.append(rec)
                    self.out.write(json.dumps(rec) + "\n")
                continue
            started.append(1)
            threading.Thread(target=self._do, args=(rec, rng.choice(pool), sink, done), daemon=True).start()

    def run_step(self, mode: str, step: int, rpm: float, models: list[str]) -> list[dict]:
        t_start = time.monotonic()
        t_end = t_start + self.cfg.step_s
        sinks = {m: [] for m in models}
        started = {m: [] for m in models}
        done = {m: threading.Semaphore(0) for m in models}
        ths = []
        for m in models:
            rng = random.Random("%s-%s-%d-%s" % (self.cfg.seed, mode, step, m))
            th = threading.Thread(target=self._arrivals, args=(mode, step, m, rpm, t_end, sinks[m], started[m], done[m], rng), daemon=True)
            th.start()
            ths.append(th)
        for th in ths:
            th.join()
        duration = max(1e-6, min(time.monotonic(), t_end) - t_start)
        # Drain: wait for this step's in-flight requests (bounded by the request timeout).
        for m in models:
            for _ in range(len(started[m])):
                done[m].acquire(timeout=self.cfg.timeout_s + 5)
        rows = []
        for m in models:
            s = summary.step_stats(sinks[m], duration, rpm)
            s["model"] = m
            s["mode"] = mode
            s["verdict"] = summary.tripped(s)
            rows.append(s)
            self.log(summary.fmt_row(mode, s))
        return rows

    def ramp(self, mode: str, models: list[str]) -> None:
        active = list(models)
        for i, rpm in enumerate(self.cfg.steps):
            if not active or self.capped:
                break
            rows = self.run_step(mode, i, rpm, active)
            self.rows.extend(rows)
            for s in rows:
                if s["verdict"]:
                    self.log("     stop %s at %d req/min: %s" % (s["model"], rpm, s["verdict"]))
                    active.remove(s["model"])
            if self.capped:
                self.log("     hard cap of %d requests reached; stopping" % self.cfg.max_requests)

    def run(self) -> list[dict]:
        self.log(summary.HEADER)
        first = True
        for mode in self.cfg.modes:
            if mode == "A1":
                for m in self.cfg.models:
                    if not first and self.cfg.cooldown_s:
                        time.sleep(self.cfg.cooldown_s)  # let rate-limit windows drain between ramps
                    first = False
                    self.ramp("A1", [m])
            elif mode == "A2":
                if not first and self.cfg.cooldown_s:
                    time.sleep(self.cfg.cooldown_s)
                first = False
                self.ramp("A2", list(self.cfg.models))
            else:
                raise ValueError("unknown mode %r" % mode)
        self.out.close()
        return self.rows


def report(rows: list[dict], cfg: Config) -> str:
    lines = ["", "== Summary per step ==", summary.HEADER]
    lines += [summary.fmt_row(s["mode"], s) for s in rows]
    lines += ["", "== Sustainable req/min per model (highest step that passed) =="]
    by_mode: dict[str, dict[str, float]] = {}
    for mode in cfg.modes:
        by_mode[mode] = summary.sustainable([s for s in rows if s["mode"] == mode])
    for m in cfg.models:
        cells = ["%s=%s" % (mode, int(by_mode[mode].get(m, 0))) for mode in cfg.modes]
        lines.append("  %-34s %s" % (m, "  ".join(cells)))
    if "A1" in by_mode and "A2" in by_mode:
        a1 = [by_mode["A1"].get(m, 0) for m in cfg.models]
        a2 = [by_mode["A2"].get(m, 0) for m in cfg.models]
        if all(y >= x for x, y in zip(a1, a2)):
            lines.append("  limit looks PER MODEL (running all models together did not lower any knee)")
        else:
            lines.append("  limit looks ACCOUNT-WIDE or shared (A2 knee below A1 for: %s)" % ", ".join(
                m for m, x, y in zip(cfg.models, a1, a2) if y < x))
    return "\n".join(lines)
