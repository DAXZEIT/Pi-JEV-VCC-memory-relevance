# Scorer contract

`jev-relevance` talks to its scorer through exactly one seam: a CLI invoked as

```
<scorer> ask --state <file> --questions <file> --timeout <seconds>
```

that prints a single JSON object on stdout and exits 0.

## Inputs

**`--state`** — path to a JSON file containing a single string: the text to
score against (first 2000 chars of the user's prompt at cold start, the
compaction summary post-compaction, or the agent's own question for the
on-demand tool).

**`--questions`** — path to a JSON file: a dict `{qid: question}` where

```json
{
  "deploy-runbook": {
    "type": "noul",
    "instructions": "Given the user's request, would consulting the memory block 'deploy-runbook' (…) be useful to handle it correctly?",
    "criteria": { "true": "…", "false": "…" }
  }
}
```

- `instructions` — the full natural-language yes/no question (frozen protocol
  wording, see `FRAMINGS` in the extension).
- `criteria.true` / `criteria.false` — calibration criteria shown to the
  classifier: when to lean Yes vs No. These are what make the score a
  calibrated *prior* rather than a vibe.
- `type: "noul"` — a plain scalar-output question. (The contract has room
  for richer question types; this extension only uses noul.)

## Output

```json
{
  "ok": true,
  "results": { "deploy-runbook": { "probs": { "yes": 0.981 } } },
  "anchor": null
}
```

- `ok` — `false`/absent = whole batch failed; the extension skips and logs.
- `results[qid].probs.yes` — P(Yes) ∈ [0,1]. **This is the ranking score.**
  Per-question entries may instead carry `{"error": "…"}` — those rows are
  dropped from the ranking, the rest survive.
- `anchor` — optional, opaque. Reserved for shared-prefix KV-cache anchors
  (a large speedup when the state prefix is identical across all questions:
  24 branches in ~28 s instead of ~150 s in our setup). `null` is fine.

## Implementations

- `scripts/scorer-openai.py` (this repo) — reference implementation against
  any OpenAI-compatible `/v1/completions` endpoint with logprobs
  (llama-server, vLLM, …). Score = P("Yes") normalized over {"Yes","No"}
  from the first generated token.
- The Jev/TypeSafe API CLI (`jev`) — the original backend this was built
  against: a typed, calibrated classifier service with native-logit access.

## Calibration honesty

The absolute quality of the scores depends on the classifier. Measured on
the reference setup (ANLI 1000-line bench): a strong hosted classifier
reaches ~0.79 3-way accuracy vs ~0.71 for a local 8-bit model; a local
small model is fine as a *prior* (its confidence→error curve stays monotone,
and its rankings correlate ~0.92 with the hosted one — same decision
92.4% of the time). Thresholds are per-task: calibration does NOT transfer
across task families, so re-measure your cutoff on your own task. Do not
blindly trust absolute scores from an uncalibrated backend; the gradient
plus the noise floor remains informative.
