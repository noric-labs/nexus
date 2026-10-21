"""Response parsing, per-step statistics, the stop rule and the summary table."""

from __future__ import annotations

import json
import statistics

# $/M input, $/M output from the plan's model table (Workers AI pricing, 2026-10).
# Keys are matched by substring against the model id, so "@cf/qwen/qwen3-30b-a3b-fp8"
# matches "qwen3-30b-a3b-fp8".
PRICES = {
    "qwen3-30b-a3b-fp8": (0.051, 0.335),
    "glm-4.7-flash": (0.06, 0.40),
    "gemma-4-26b-a4b-it": (0.10, 0.30),
    "gpt-oss-20b": (0.20, 0.30),
}
FALLBACK_PRICE = (0.20, 0.40)  # used for unknown models; conservative

DEFAULT_MODELS = [
    "@cf/qwen/qwen3-30b-a3b-fp8",
    "@cf/zai-org/glm-4.7-flash",
    "@cf/google/gemma-4-26b-a4b-it",
    "@cf/openai/gpt-oss-20b",
]

STOP_429_PCT = 1.0
STOP_P95_S = 5.0


def price_for(model: str) -> tuple[float, float]:
    for k, v in PRICES.items():
        if k in model:
            return v
    return FALLBACK_PRICE


def parse_tool_call(resp: dict) -> tuple[bool, int, str]:
    """Return (valid, keys_batched, tool_name) for an OpenAI chat.completions response.

    Valid means: first choice carries a tool call to send_keys with JSON arguments holding a
    non-empty `keys` (list of strings, or a string which we split on whitespace), or a call
    to screen(). Keys count is 0 for screen()."""
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False, 0, ""
    calls = msg.get("tool_calls") or []
    if not calls:
        return False, 0, ""
    fn = (calls[0] or {}).get("function") or {}
    name = fn.get("name") or ""
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return False, 0, name
    if not isinstance(args, dict):
        return False, 0, name
    if name == "screen":
        return True, 0, name
    if name != "send_keys":
        return False, 0, name
    keys = args.get("keys")
    if isinstance(keys, str):
        keys = keys.split()
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return False, 0, name
    return True, len(keys), name


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[i]


def step_stats(records: list[dict], duration_s: float, target_rpm: float) -> dict:
    n = len(records)
    ok = [r for r in records if r["status"] == 200]
    n429 = sum(1 for r in records if r["status"] == 429)
    n5xx = sum(1 for r in records if isinstance(r["status"], int) and r["status"] >= 500)
    nerr = sum(1 for r in records if not isinstance(r["status"], int))  # timeout / conn error
    lat = [r["latency_s"] for r in ok]
    valid = [r for r in ok if r["valid_tool_call"]]
    keyed = [r["keys"] for r in valid if r.get("tool") == "send_keys"]
    tin = statistics.fmean([r["tokens_in"] for r in ok if r["tokens_in"]] or [0])
    tout = statistics.fmean([r["tokens_out"] for r in ok if r["tokens_out"]] or [0])
    model = records[0]["model"] if records else ""
    pin, pout = price_for(model)
    ok_rpm = len(ok) / duration_s * 60 if duration_s else 0.0
    usd_h = ok_rpm * 60 * (tin * pin + tout * pout) / 1e6
    return {
        "model": model,
        "target_rpm": target_rpm,
        "sent": n,
        "sent_rpm": n / duration_s * 60 if duration_s else 0.0,
        "ok_rpm": ok_rpm,
        "pct_429": 100.0 * n429 / n if n else 0.0,
        "n_5xx": n5xx,
        "n_err": nerr,
        "p50_s": pct(lat, 50),
        "p95_s": pct(lat, 95),
        "valid_pct": 100.0 * len(valid) / len(ok) if ok else 0.0,
        "keys_per_call": statistics.fmean(keyed) if keyed else 0.0,
        "tokens_in": tin,
        "tokens_out": tout,
        "usd_per_hour": usd_h,
    }


def tripped(s: dict) -> str:
    """The stop-rule reason for a step, or '' if the step passed."""
    why = []
    if s["pct_429"] > STOP_429_PCT:
        why.append("429 %.1f%% > %g%%" % (s["pct_429"], STOP_429_PCT))
    if s["p95_s"] == s["p95_s"] and s["p95_s"] > STOP_P95_S:  # nan-safe
        why.append("p95 %.2fs > %gs" % (s["p95_s"], STOP_P95_S))
    if s["sent"] and s["ok_rpm"] == 0:
        why.append("no successful requests")
    return "; ".join(why)


HEADER = (
    "%-4s %-34s %6s %7s %7s %6s %4s %4s %7s %7s %6s %5s %6s %5s %8s  %s"
    % ("mode", "model", "target", "sent/m", "ok/m", "429%", "5xx", "err", "p50s", "p95s", "valid%", "keys", "tokIn", "tokOut", "$/hour", "verdict")
)


def fmt_row(mode: str, s: dict) -> str:
    verdict = s.get("verdict") or "ok"
    return "%-4s %-34s %6d %7.1f %7.1f %6.2f %4d %4d %7.2f %7.2f %6.1f %5.2f %6.0f %5.0f %8.2f  %s" % (
        mode, s["model"][-34:], s["target_rpm"], s["sent_rpm"], s["ok_rpm"], s["pct_429"], s["n_5xx"], s["n_err"],
        s["p50_s"], s["p95_s"], s["valid_pct"], s["keys_per_call"], s["tokens_in"], s["tokens_out"], s["usd_per_hour"], verdict,
    )


def sustainable(rows: list[dict]) -> dict[str, float]:
    """Highest target rpm per model whose step passed the stop rule (0 if none)."""
    out: dict[str, float] = {}
    for s in rows:
        out.setdefault(s["model"], 0.0)
        if not s.get("verdict"):
            out[s["model"]] = max(out[s["model"]], s["target_rpm"])
    return out
