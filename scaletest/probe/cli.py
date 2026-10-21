"""Phase A inference probe CLI.

Default is a dry run: an in-process mock (scaletest/mock_server) is started and the ramp
runs against it. Real traffic requires --live; the probe then checks the broker's /health,
prints the planned request count and estimated token spend, and asks for confirmation
(or --yes). A hard cap (--max-requests) bounds every run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from . import summary
from .request import build_request, estimate_tokens
from .runner import Config, Probe, planned_requests, report

BROKER = "http://100.64.255.254:8899"
LIVE_BASE = BROKER + "/workers-ai/v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="run against a mock (default)")
    g.add_argument("--live", action="store_true", help="send real traffic to --base-url (default: the broker)")
    ap.add_argument("--yes", action="store_true", help="skip the live spend confirmation prompt")
    ap.add_argument("--skip-health", action="store_true", help="live: do not require cloudflare_token_loaded on the broker")
    ap.add_argument("--base-url", help="OpenAI-compatible base (…/v1). Dry run default: an in-process mock")
    ap.add_argument("--models", default=",".join(summary.DEFAULT_MODELS), help="comma-separated model ids")
    ap.add_argument("--steps", default="60,120,240,300,360", help="req/min per model, comma-separated")
    ap.add_argument("--step-seconds", type=float, default=300)
    ap.add_argument("--mode", default="A1,A2", help="A1 (one model at a time), A2 (all concurrently), or both")
    ap.add_argument("--target-tokens", type=int, default=5000, help="approx input tokens per request")
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--max-requests", type=int, default=25000, help="hard cap on total requests sent")
    ap.add_argument("--max-inflight", type=int, default=1000)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--cooldown", type=float, default=None, help="seconds between ramps (default: 60 live, 0 dry)")
    ap.add_argument("--out", default=None, help="JSONL path (default: probe-<ts>.jsonl in cwd)")
    ap.add_argument("--summary-json", default=None, help="also write the step rows as JSON")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--show-request", action="store_true", help="print one request body and exit")
    # mock knobs (dry run with in-process mock only)
    ap.add_argument("--mock-rpm", type=float, default=300, help="mock per-model limit")
    ap.add_argument("--mock-account-rpm", type=float, default=0, help="mock account-wide limit (0 = none)")
    ap.add_argument("--mock-window", type=float, default=60, help="mock limiter window seconds")
    ap.add_argument("--mock-latency", type=float, default=0.6)
    a = ap.parse_args(argv)
    if a.live:
        a.dry_run = False
    return a


def broker_health() -> dict:
    with urllib.request.urlopen(BROKER + "/health", timeout=5) as r:
        return json.loads(r.read())


def estimate_spend(cfg: Config) -> tuple[int, float, float, float]:
    n = min(planned_requests(cfg), cfg.max_requests)
    per_model = n / max(1, len(cfg.models))
    tin = n * cfg.target_tokens
    tout = n * cfg.max_tokens
    usd = sum(per_model * (cfg.target_tokens * summary.price_for(m)[0] + cfg.max_tokens * summary.price_for(m)[1]) / 1e6
              for m in cfg.models)
    return n, tin, tout, usd


def main(argv=None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    steps = [float(x) for x in a.steps.split(",") if x.strip()]
    modes = [x.strip().upper() for x in a.mode.split(",") if x.strip()]
    if not modes or set(modes) - {"A1", "A2"}:
        print("--mode must be A1, A2 or A1,A2", file=sys.stderr)
        return 2

    if a.show_request:
        import random
        body = build_request(models[0], random.Random(a.seed), a.target_tokens, a.max_tokens)
        print(json.dumps(body, indent=1))
        print("# estimated input tokens: %d" % estimate_tokens(body), file=sys.stderr)
        return 0

    mock = None
    base = a.base_url
    if a.dry_run:
        if base:
            host = urllib.parse.urlparse(base).hostname
            if host not in LOCAL_HOSTS:
                print("refusing: dry run only targets a local mock (got %s); pass --live for real traffic" % base, file=sys.stderr)
                return 2
        else:
            from mock_server import start
            mock, base = start(rpm=a.mock_rpm, account_rpm=a.mock_account_rpm, window_s=a.mock_window,
                               base_latency_s=a.mock_latency, seed=a.seed)
    else:
        base = base or LIVE_BASE

    cfg = Config(
        base_url=base, models=models, steps=steps, step_s=a.step_seconds, modes=modes,
        target_tokens=a.target_tokens, max_tokens=a.max_tokens, max_requests=a.max_requests,
        max_inflight=a.max_inflight, timeout_s=a.timeout, seed=a.seed,
        out_path=a.out or "probe-%s.jsonl" % time.strftime("%Y%m%dT%H%M%S"),
        cooldown_s=a.cooldown if a.cooldown is not None else (60.0 if a.live else 0.0),
    )

    n, tin, tout, usd = estimate_spend(cfg)
    hours = sum(cfg.step_s for _ in steps) * (len(models) * ("A1" in modes) + ("A2" in modes)) / 3600
    print("probe: %s  target=%s" % ("LIVE" if a.live else "dry run (mock)", base))
    print("  models: %s" % ", ".join(models))
    print("  steps: %s req/min per model, %gs each; modes: %s; max wall time ~%.1f h" % (a.steps, cfg.step_s, ",".join(modes), hours))
    print("  planned requests (if no step trips): %d (hard cap %d)" % (planned_requests(cfg), cfg.max_requests))
    print("  est. spend upper bound: %.1fM input + %.2fM output tokens = $%.2f at plan prices" % (tin / 1e6, tout / 1e6, usd))
    print("  jsonl: %s" % cfg.out_path)

    if a.live:
        if not a.skip_health and base.startswith(BROKER):
            try:
                h = broker_health()
            except OSError as e:
                print("broker /health unreachable: %s" % e, file=sys.stderr)
                return 2
            if not (h.get("cloudflare_token_loaded") and h.get("cloudflare_account_loaded")):
                print("refusing: broker reports cloudflare_token_loaded=%s cloudflare_account_loaded=%s" % (
                    h.get("cloudflare_token_loaded"), h.get("cloudflare_account_loaded")), file=sys.stderr)
                return 2
        if not a.yes:
            if not sys.stdin.isatty():
                print("refusing: live run needs --yes when stdin is not a terminal", file=sys.stderr)
                return 2
            if input("Send real traffic? type 'yes': ").strip() != "yes":
                print("aborted")
                return 1

    probe = Probe(cfg)
    rows = probe.run()
    print(report(rows, cfg))
    print("\nsent %d requests%s; records in %s" % (probe.sent, " (HARD CAP HIT)" if probe.capped else "", cfg.out_path))
    if a.summary_json:
        with open(a.summary_json, "w") as f:
            json.dump(rows, f, indent=1)
    if mock:
        mock.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
