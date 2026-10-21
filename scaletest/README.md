# scaletest

Scale test phase A (see [`docs/PLAN.md`](../docs/PLAN.md), "Scale test"): an inference probe
that ramps open-loop traffic at Workers AI through the broker and finds the sustainable
req/min per model. It also shows whether the limit is per model or account-wide, and how
many keys each tool call batches.

- `probe/`: the probe (Python stdlib only, no dependencies).
- `mock_server/`: an OpenAI-compatible mock with per-model and account-wide rate limits
  (429 beyond them), simulated latency that rises near saturation, and `send_keys`/`screen`
  tool calls. The tests and dry runs use it.
- `tests/`: short ramps against the mock.

## What a request looks like

Each request has the lane system prompt, the two tools (`send_keys(keys: string[])` and
`screen()`), the current and previous 80x24 screens, and a recent-actions log padded to
about 5k input tokens. It is sent with `tool_choice: "auto"` and `max_tokens: 200`. To see
one, run `python3 -m probe --show-request`. The padding uses a chars/token estimate
calibrated against o200k_base. Each live record's `usage.prompt_tokens` gives the real
count; if it is off, adjust `--target-tokens`.

## Ramp

- **Steps:** 60, 120, 240, 300 and 360 req/min per model by default, 5 min each
  (`--steps`, `--step-seconds`).
- **Arrivals:** Poisson and open-loop, so slow replies don't lower the offered rate. Each
  request runs on its own thread, and `--max-inflight` bounds them.
- **Mode A1:** one model at a time, each through its own full ramp.
- **Mode A2:** all models at once, at the same per-model rate.
  - The default is `--mode A1,A2`.
  - When live, there is a 60 s cooldown between ramps so the per-minute windows drain.
- **Stop rule:** a model's ramp stops after the first step where 429s exceed 1% or p95
  latency exceeds 5 s. p95 is measured over 200 responses.

## Output

The JSONL file (`--out`) has one record per request:

- `ts`, `mode`, `step`, `target_rpm`, `model`
- `status`, `latency_s`, `is_429`, `is_5xx`
- `tokens_in`, `tokens_out`
- `valid_tool_call`, `tool`, `keys`, and `error` when there is one

The summary table has one row per step and model:

- achieved sent/ok req/min
- 429 %, 5xx count, errors
- p50/p95 latency
- valid tool-call %, mean keys per `send_keys` call
- mean tokens in/out
- est. $/hour at the plan's prices: the ok rate × tokens × price. The prices are in
  `probe/summary.py:PRICES`.

After the table comes the sustainable req/min per model for A1 and A2, plus a per-model vs
account-wide verdict. The verdict says account-wide if any model's A2 knee is below its A1
knee. Use `--summary-json` to also save the rows.

## Safety

- The default is a **dry run**:
  - An in-process mock is started.
  - `--base-url` may only point at localhost.
  - Use `--mock-rpm`, `--mock-account-rpm`, `--mock-window` and `--mock-latency` to shape
    the mock.
- **Real traffic needs `--live`.** The probe then:
  1. checks the broker's `/health` and refuses unless `cloudflare_token_loaded` and
     `cloudflare_account_loaded` are both true;
  2. prints the planned request count and an upper-bound token and dollar spend;
  3. asks you to type `yes`, unless you pass `--yes`.
- `--max-requests` is a hard cap on the total requests sent (default 25000). One full mode
  across 4 models is 21,600 requests if nothing trips; A1 and A2 together is 43,200. Upper
  bound for both: about 216M input tokens, or about $25 at plan prices.

## Run

```sh
cd scaletest
python3 -m unittest discover -s tests -v          # ~2 min; or: uv run python -m unittest discover -s tests -v

# Dry run with a quick ramp against the mock (knee at 240 because the mock allows 180/min)
python3 -m probe --steps 60,120,240,360 --step-seconds 15 --mock-rpm 180 --mock-window 10 \
  --models @cf/qwen/qwen3-30b-a3b-fp8,@cf/openai/gpt-oss-20b

# Standalone mock, e.g. for pointing other tools at it
python3 -m mock_server --port 8787 --rpm 300 --account-rpm 0
python3 -m probe --base-url http://127.0.0.1:8787/v1 --steps 60,120 --step-seconds 20
```

### Live, once Cloudflare is loaded on the broker

First check that `curl -s http://100.64.255.254:8899/health` shows
`"cloudflare_token_loaded": true` and `"cloudflare_account_loaded": true`. Then do a 1-minute
smoke run on one model:

```sh
python3 -m probe --live --mode A1 --models @cf/qwen/qwen3-30b-a3b-fp8 \
  --steps 30 --step-seconds 60 --max-requests 60 --out smoke.jsonl
```

Then the full phase A, with A1 for each model followed by A2. That takes about 2.2 h if no
step trips:

```sh
python3 -m probe --live --mode A1,A2 \
  --models @cf/qwen/qwen3-30b-a3b-fp8,@cf/zai-org/glm-4.7-flash,@cf/google/gemma-4-26b-a4b-it,@cf/openai/gpt-oss-20b \
  --steps 60,120,240,300,360 --step-seconds 300 --max-requests 45000 \
  --out phaseA.jsonl --summary-json phaseA-summary.json 2>&1 | tee phaseA.txt
```

The live default base URL is `http://100.64.255.254:8899/workers-ai/v1`. The box sends
`Authorization: Bearer placeholder` and the broker swaps in the real credential.

## Open issues

- Check the model ids for GLM and Gemma against the Workers AI catalog before the live run.
  Only the Qwen id appears in CONTRACTS.md. Unknown ids are billed in the estimate at a
  conservative fallback price.
- Workers AI may return tool calls in `content` text for some models. These count as
  invalid, and the raw body isn't kept for 200s, so add logging if valid% is low.
- The mock's latency curve and rate-limit window are guesses. Cloudflare's actual
  window and burst behaviour are what phase A measures.
