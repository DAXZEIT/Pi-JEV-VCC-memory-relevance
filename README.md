# Pi-JEV-VCC-memory-relevance

**Logit-scored memory relevance for the pi coding agent.** Your agent keeps a
folder of long-term memory files (`~/.pi/agent/memory/*.md`). This extension
scores, at the right moments, how relevant each file is to what is happening
*right now* — using a calibrated yes/no classifier that reads **native logits**
(P("Yes")), not embeddings — and hands the agent a graduated ranking.

The scores are a **prior, not a verdict**: the agent decides what to read.
Nothing is ever injected.

```
<memory_relevance>
Potentially useful memories for the current task (local relevance scores,
informational only — 0.000-0.001 is the noise floor, skip those; anything
above is a hint you can verify by reading, weighted by score, not a verdict):
deploy-runbook.md: 0.998
project-foo-status.md: 0.205
user-preferences.md: 0.153
</memory_relevance>
```

## Why logit scoring instead of embeddings

- **A probability is a prior.** P(Yes) = 0.06 means something concrete: "this
  is probably noise — but verify by reading if it's cheap". Cosine similarity
  has no calibrated semantics; nobody knows what 0.63 "means".
- **The gradient is the product.** With ~25 memories you don't want a
  top-1 — you want to *see* the shape of the ranking: cliffs, plateaus, and a
  visible noise floor around 0.000–0.001 that teaches itself where to cut.
- **You can calibrate it.** Because the output is a probability, a
  confidence→error curve can be measured (ours is monotone), thresholds can be
  set per task, and two different backends can be compared quantitatively.

## The three triggers

| Trigger | Hook | When | Output |
|---|---|---|---|
| **Cold start** | `before_agent_start` | First real user prompt of the session, *before* the first LLM call (synchronous, ~15-20 s) | top-10 block in the very first prefill |
| **Post-compaction** | `session_compact` | pi-vcc's compaction summary, synchronously-blocking the resume (~15-20 s) | top-10 block in the first post-compaction prefill |
| **On-demand** | `memory_relevance` tool | Mid-task, when the agent suspects a memory holds needed context | FULL ranking as the tool result |

The two pushes exist because the agent cannot know what it is missing at
startup or after a context wipe. The pull exists because a state-driven
trigger cannot see a question the agent only formulates mid-task. Failure at
any point = skip + log line (`~/.cache/jev-relevance/log.jsonl`) — the
session is never blocked and the agent falls back to its static index.

## Reading hygiene (baked into the output)

- `0.000–0.001` — noise floor. Skip.
- Anything above — a **hint you can verify by reading**, weighted by score,
  not a verdict. A mid-gradient cliff is *not* a truth boundary: a 0.06 file
  can be exactly the right pointer in a broader sweep.
- Verify by reading. A read costs nothing; the score orients, the content
  decides.

## Install

```bash
# 1. scorer — either the bundled reference scorer against any
#    OpenAI-compatible endpoint with logprobs (llama-server, vLLM, …):
chmod +x scripts/scorer-openai.py
#    (point JEV_CMD at it), or any CLI implementing docs/scorer-contract.md.

# 2. memory index — build it from your memory dir and review the descriptions:
python3 scripts/build-index.py --dir ~/.pi/agent/memory \
    --out ~/.pi/agent/memory_index.json --exclude error_log.md
#    (exclude append-only hypothesis/incident logs — only durable-fact
#    memories belong in the ranking)

# 3. extension — symlink into the pi agent dir:
ln -s "$PWD/jev-relevance.ts" ~/.pi/agent/extensions/jev-relevance.ts
```

Optional env overrides: `JEV_CMD` (scorer binary), `JEV_MEMORY_INDEX`
(index JSON path).

Requirements: the [pi coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(`~/.pi/agent/extensions/*.ts` auto-load), Node ≥ 20, a running
OpenAI-compatible completions endpoint for the reference scorer.

## Costs

~15-20 s per scoring (25 parallel single-token completions against a local
small model, ~1000 tok total), zero egress. Two pushes per session maximum
(one cold start, one per compaction) plus ≤1 on-demand call per task by
contract. The output block costs ~100 tokens in context.

## Validation status (measured on the reference setup)

- ANLI 1000-line bench: hosted typed classifier 3-way **0.79**, local 8-bit
  model **0.71**; isotonic recalibration reaches parity in-sample.
- **FEVER out-of-sample: parity does NOT transfer** (all arms ~0.52 vs 0.79) —
  thresholds are per-task; calibration does not cross task families. This is
  documented, not hidden.
- The local backend stays usable as a prior: confidence→error curve monotone,
  ranking correlation local↔hosted ~0.92 (same decision 92.4% of the time).
- Shared-prefix KV anchoring: 24 branches in ~28 s instead of ~150 s.

## Limitations

- Scores orient retrieval; they do not replace reading. Treat them as a
  search engine, not an oracle.
- Quality of the ranking = quality of the `desc_en` descriptions in the
  index. Garbage descriptions in, garbage priors out.
- The protocol framings (`FRAMINGS` in the extension) are frozen wording —
  they are the metric. Reword them only together with a re-measurement.
- Post-compaction scoring is coupled to the `pi-vcc` compactor id; other
  compactors are skipped (log `skip: not-pi-vcc`).

## Layout

```
jev-relevance.ts            the extension (drop-in, ~400 lines, no deps)
docs/scorer-contract.md     the scorer CLI contract + calibration notes
scripts/scorer-openai.py    reference scorer (OpenAI-compatible logprobs)
scripts/build-index.py      memory dir → index JSON
examples/memory_index.example.json
```

## License

MIT — see LICENSE.
