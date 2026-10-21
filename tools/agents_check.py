#!/usr/bin/env python3
"""Validate lane/agents.json and resolve each lane's agent settings (D-040). Stdlib only.

  tools/agents_check.py [--agents lane/agents.json]
      validate; exit 1 with one line per error
  tools/agents_check.py --lanes FILE [-n N] [--style win|explore] --plan
      also resolve the first N lanes and print lane -> game, profile, model, style, interval
  tools/agents_check.py --lanes FILE [-n N] [--style S] --emit
      for scripts/host-up.sh: one line per lane, "<id> <base64 of lane.env lines>"

Lanes file: "id game fault seed [agent]". agent is `profile:<name>`, a bare profile name
(no '/' or '@'), or a model id from lane/models.json (back-compat: the game's profile if it
uses that model, else the first profile that does, else the game's profile with the model
swapped in). Without it: games[game].profile, else defaults.profile.
Interval = max(games[game].interval_ms or profile.interval_ms, models[model].min_interval_ms).
"""

import argparse
import base64
import json
import os
import re
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES = ("win", "explore")
MIN_INTERVAL = 250
# Named keys game.ts accepts (lane/game.ts NAMED, plus F1..F12, C-x, M-x, C-M-x, lt).
NAMED = {"enter", "ret", "return", "cr", "esc", "escape", "tab", "btab", "bspace", "backspace",
         "bs", "space", "up", "down", "left", "right", "home", "end", "pgup", "pageup", "ppage",
         "pgdn", "pagedown", "npage", "del", "delete", "dc", "ins", "insert", "ic", "lt"}
KNOWN = {
    "top": {"version", "defaults", "profiles", "models", "games"},
    "profile": {"model", "style", "interval_ms", "max_tokens", "sampling", "notes"},
    "model": {"min_interval_ms", "max_concurrent_lanes", "notes"},
    "game": {"profile", "prompt", "keys", "goal", "interval_ms"},
}


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def named_key_ok(entry):
    m = re.fullmatch(r"<([^<>\s]{1,12})>", entry)
    if not m:
        return True  # plain text: each character is an allowed key
    low = m.group(1).lower()
    return (low in NAMED or re.fullmatch(r"f([1-9]|1[0-2])", low) is not None
            or re.fullmatch(r"[cm]-.", low) is not None or re.fullmatch(r"c-m-.", low) is not None)


def load_model_ids(path):
    d = json.load(open(path))
    return [m["id"] for p in d.get("providers", {}).values() for m in p.get("models", [])]


def load_game_names(path):
    # Section headers only, so no tomllib (3.11+) is needed.
    return re.findall(r"^\[games\.([A-Za-z0-9_.-]+)\]", open(path).read(), re.M)


def validate(cfg, model_ids, game_names, prompts_dir):
    errs, warns = [], []

    def unknown(where, obj, kind):
        for k in obj:
            if k not in KNOWN[kind]:
                warns.append(f"{where}: unknown field '{k}' (ignored)")

    def interval(where, v):
        if not is_int(v) or v < MIN_INTERVAL:
            errs.append(f"{where}.interval_ms: must be an integer >= {MIN_INTERVAL}, got {v!r}")

    if not isinstance(cfg, dict):
        return ["top level: must be a JSON object"], warns
    unknown("top level", cfg, "top")
    if cfg.get("version") != 1:
        errs.append(f"version: must be 1, got {cfg.get('version')!r}")
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        errs.append("profiles: must be a non-empty object")
        profiles = {}
    for name, p in profiles.items():
        w = f"profiles.{name}"
        if not isinstance(p, dict):
            errs.append(f"{w}: must be an object")
            continue
        unknown(w, p, "profile")
        if p.get("model") not in model_ids:
            errs.append(f"{w}.model: unknown model {p.get('model')!r} (not in lane/models.json: "
                        f"{', '.join(model_ids)})")
        if p.get("style") not in STYLES:
            errs.append(f"{w}.style: must be 'win' or 'explore', got {p.get('style')!r}")
        interval(w, p.get("interval_ms"))
        if "max_tokens" in p and (not is_int(p["max_tokens"]) or p["max_tokens"] < 1):
            errs.append(f"{w}.max_tokens: must be a positive integer, got {p['max_tokens']!r}")
        if "sampling" in p and not isinstance(p["sampling"], dict):
            errs.append(f"{w}.sampling: must be an object")
        if "notes" in p and not isinstance(p["notes"], str):
            errs.append(f"{w}.notes: must be a string")
    defaults = cfg.get("defaults")
    if not isinstance(defaults, dict) or defaults.get("profile") not in profiles:
        got = defaults.get("profile") if isinstance(defaults, dict) else defaults
        errs.append(f"defaults.profile: unknown profile {got!r} (profiles: {', '.join(profiles)})")
    models = cfg.get("models", {})
    if not isinstance(models, dict):
        errs.append("models: must be an object")
        models = {}
    for mid, m in models.items():
        w = f"models.{mid}"
        if mid not in model_ids:
            errs.append(f"{w}: unknown model (not in lane/models.json)")
        if not isinstance(m, dict):
            errs.append(f"{w}: must be an object")
            continue
        unknown(w, m, "model")
        if not is_int(m.get("min_interval_ms")) or m["min_interval_ms"] < 0:
            errs.append(f"{w}.min_interval_ms: must be an integer >= 0, got {m.get('min_interval_ms')!r}")
        if "max_concurrent_lanes" in m and (not is_int(m["max_concurrent_lanes"]) or m["max_concurrent_lanes"] < 1):
            errs.append(f"{w}.max_concurrent_lanes: must be an integer >= 1, got {m['max_concurrent_lanes']!r}")
    games = cfg.get("games", {})
    if not isinstance(games, dict):
        errs.append("games: must be an object")
        games = {}
    for g, e in games.items():
        w = f"games.{g}"
        if g not in game_names:
            errs.append(f"{w}: unknown game (not in harness/games.toml)")
        if not isinstance(e, dict):
            errs.append(f"{w}: must be an object")
            continue
        unknown(w, e, "game")
        if "profile" in e and e["profile"] not in profiles:
            errs.append(f"{w}.profile: unknown profile {e['profile']!r}")
        if "prompt" in e:
            pr = e["prompt"]
            if (not isinstance(pr, str) or not pr or os.path.isabs(pr) or ".." in pr.split("/")
                    or not os.path.isfile(os.path.join(prompts_dir, pr))):
                errs.append(f"{w}.prompt: {pr!r} is not a file under lane/prompts/")
        if "keys" in e:
            ks = e["keys"]
            if not isinstance(ks, list) or not ks or not all(isinstance(k, str) and k for k in ks):
                errs.append(f"{w}.keys: must be a non-empty list of non-empty strings")
            else:
                for k in ks:
                    if not named_key_ok(k):
                        errs.append(f"{w}.keys: unknown named key {k!r} (see lane/game.ts key notation)")
        if "goal" in e and (not isinstance(e["goal"], str) or not e["goal"].strip()):
            errs.append(f"{w}.goal: must be a non-empty string")
        if "interval_ms" in e:
            interval(w, e["interval_ms"])
    return errs, warns


def read_lanes(path, n):
    lanes = []
    for line in open(path):
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        if len(f) < 4:
            raise SystemExit(f"{path}: need 'id game fault seed [agent]', got: {line.strip()}")
        lanes.append((f[0], f[1], f[4] if len(f) > 4 else ""))
    if n:
        if len(lanes) < n:
            raise SystemExit(f"{path} has {len(lanes)} lanes, asked for {n}")
        lanes = lanes[:n]
    return lanes


def resolve(cfg, model_ids, lane_id, game, agent, style_override):
    """Effective settings for one lane, or raise ValueError."""
    profiles, games = cfg["profiles"], cfg.get("games", {})
    ge = games.get(game, {})
    model = None
    if agent.startswith("profile:"):
        prof = agent[len("profile:"):]
    elif agent and ("/" in agent or "@" in agent):
        if agent not in model_ids:
            raise ValueError(f"lane {lane_id}: unknown model {agent!r} (not in lane/models.json)")
        # The game's (or default) profile if it uses this model, else the first profile that
        # does, else the game's profile with its model replaced.
        prof = ge.get("profile") or cfg["defaults"]["profile"]
        if profiles[prof]["model"] != agent:
            same = [n for n, p in profiles.items() if p["model"] == agent]
            if same:
                prof = same[0]
            else:
                model = agent
    elif agent:
        prof = agent
    else:
        prof = ge.get("profile") or cfg["defaults"]["profile"]
    if prof not in profiles:
        raise ValueError(f"lane {lane_id}: unknown profile {prof!r} (profiles: {', '.join(profiles)})")
    p = profiles[prof]
    r = {"lane": lane_id, "game": game, "profile": prof, "model": model or p["model"],
         "style": style_override or p["style"]}
    base = ge.get("interval_ms", p["interval_ms"])
    r["interval_ms"] = max(base, cfg.get("models", {}).get(r["model"], {}).get("min_interval_ms", 0))
    # A swapped-in model keeps the profile's style and interval, but not its sampling and
    # max_tokens: those were chosen for the profile's own model.
    if not model:
        if "max_tokens" in p:
            r["max_tokens"] = p["max_tokens"]
        if p.get("sampling"):
            r["sampling"] = p["sampling"]
    for k in ("prompt", "keys", "goal"):
        if k in ge:
            r[k] = ge[k]
    return r


def lane_env(r):
    """lane.env lines (sourced by scripts/lane.sh with `set -a`)."""
    kv = [("NEXUS_PROFILE", r["profile"]), ("NEXUS_MODEL", r["model"]),
          ("NEXUS_PLAY_STYLE", r["style"]), ("NEXUS_ACTION_INTERVAL_MS", str(r["interval_ms"]))]
    if "prompt" in r:
        kv.append(("NEXUS_PROMPT_FILE", r["prompt"]))
    if "keys" in r:
        kv.append(("NEXUS_ALLOWED_KEYS", json.dumps(r["keys"])))
    if "goal" in r:
        kv.append(("NEXUS_GOAL", r["goal"]))
    if "max_tokens" in r:
        kv.append(("NEXUS_MODEL_MAX_TOKENS", str(r["max_tokens"])))
    if "sampling" in r:
        kv.append(("NEXUS_SAMPLING", json.dumps(r["sampling"])))
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in kv)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", default=os.path.join(ROOT, "lane", "agents.json"))
    ap.add_argument("--models", default=os.path.join(ROOT, "lane", "models.json"))
    ap.add_argument("--games", default=os.path.join(ROOT, "harness", "games.toml"))
    ap.add_argument("--prompts", default=os.path.join(ROOT, "lane", "prompts"))
    ap.add_argument("--lanes", help="lanes file to resolve")
    ap.add_argument("-n", type=int, default=0, help="first N lanes (default all)")
    ap.add_argument("--style", choices=STYLES, help="override every lane's play style")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the resolved lane table")
    mode.add_argument("--emit", action="store_true", help="lane.env lines per lane, for host-up.sh")
    a = ap.parse_args()

    try:
        cfg = json.load(open(a.agents))
    except (OSError, ValueError) as e:
        print(f"agents_check: {a.agents}: {e}", file=sys.stderr)
        return 1
    model_ids, game_names = load_model_ids(a.models), load_game_names(a.games)
    errs, warns = validate(cfg, model_ids, game_names, a.prompts)
    for w in warns:
        print(f"agents_check: warning: {a.agents}: {w}", file=sys.stderr)
    if errs:
        for e in errs:
            print(f"agents_check: error: {a.agents}: {e}", file=sys.stderr)
        return 1
    if not a.lanes:
        if a.plan or a.emit:
            ap.error("--plan/--emit need --lanes")
        print(f"agents_check: {a.agents} OK ({len(cfg['profiles'])} profiles, "
              f"{len(cfg.get('models', {}))} models, {len(cfg.get('games', {}))} games)")
        return 0

    rows, bad = [], []
    for lane_id, game, agent in read_lanes(a.lanes, a.n):
        if game not in game_names:
            bad.append(f"lane {lane_id}: unknown game {game!r} (not in harness/games.toml)")
            continue
        try:
            rows.append(resolve(cfg, model_ids, lane_id, game, agent, a.style))
        except ValueError as e:
            bad.append(str(e))
    if bad:
        for e in bad:
            print(f"agents_check: error: {a.lanes}: {e}", file=sys.stderr)
        return 1
    count = {}
    for r in rows:
        count[r["model"]] = count.get(r["model"], 0) + 1
    for mid, c in count.items():
        cap = cfg.get("models", {}).get(mid, {}).get("max_concurrent_lanes")
        if cap and c > cap:
            print(f"agents_check: warning: {c} lanes use {mid}, above its max_concurrent_lanes "
                  f"{cap}; expect 429s (continuing)", file=sys.stderr)
    if a.emit:
        for r in rows:
            print(r["lane"], base64.b64encode(lane_env(r).encode()).decode())
    else:
        hdr = ("lane", "game", "profile", "model", "style", "interval_ms", "prompt", "keys", "goal")
        table = [hdr] + [(r["lane"], r["game"], r["profile"], r["model"], r["style"], str(r["interval_ms"]),
                          r.get("prompt", "-"), " ".join(r["keys"]) if "keys" in r else "-",
                          "yes" if "goal" in r else "-") for r in rows]
        widths = [max(len(t[i]) for t in table) for i in range(len(hdr))]
        for t in table:
            print("  ".join(c.ljust(w) for c, w in zip(t, widths)).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
