# Pi-JEV-VCC-memory-relevance

![The noise floor, the graduated ranking, the dashed cut, and the hand that picks only the brightest](docs/hero.png)

**Pi memory relevance, powered by JEV.**

Long-running agents accumulate memory. The hard part is not storing it — it's
knowing which parts might matter *right now*.

This extension scores your indexed memories against the agent's current
context and gives Pi a **ranked hint** about where useful context may live —
using a yes/no classifier read from **native token logits**, not embeddings.

It does **not** retrieve or inject memories automatically. The scores are a
**prior, not a verdict** — the agent alone decides what to read.

**The scorer never reads your memory files.** It only sees the memory IDs and
descriptions in the index (see [Memory index](#memory-index)).

The default backend is **hosted JEV** (zero-dependency install). A fully
**local scorer** is available for users who want zero-egress operation.

---

## Why relevance is useful

A normal memory system answers:

> "What should I retrieve?"

This one answers something slightly different:

> **"Where might useful context be?"**

That distinction matters because the main agent remains in control:

```text
user request
    ↓
memory relevance scores
    ↓
agent notices:  "jev-typesafe.md looks useful"
    ↓
read jev-typesafe.md
    ↓
continue
```

The result looks like this:

```text
<memory_relevance>
jev-typesafe.md:      0.985
pi-reload-self.md:    0.708
pi-ask-parent.md:     0.479
user-profile.md:      0.093
moltbook.md:          0.043
</memory_relevance>
```

A score of `0.98` says "look here first". A score of `0.26` does **not** mean
"irrelevant" — it means "much weaker signal than the memories above it", and
weak links can still point the agent toward adjacent context. The signal is
the **gradient**, not any absolute threshold.

---

## Install

```bash
pi install npm:@daxzeit/pi-jev-vcc-memory-relevance
```

One command, no symlinks, no Python, no local model. Pi discovers both the
extension and the bundled `jev` skill from the package.

Then:

1. **Set your OpenRouter key** (the hosted lane reads *only* this variable):

   ```bash
   export OPENROUTER_API_KEY=sk-or-…
   ```

2. **Build your memory index** (no Python needed):

   ```bash
   npx -y --package @daxzeit/pi-jev-vcc-memory-relevance \
     pi-jev-build-index --dir ~/.pi/agent/memory --out ~/.pi/agent/memory_index.json
   ```

   (Pin the version you installed — for this release, e.g. `--package @daxzeit/pi-jev-vcc-memory-relevance@0.1.2`
   — to build with the same builder as your installed extension; bare `npx`
   fetches latest.)

   Then **review the descriptions** — the scorer ranks descriptions, not
   contents, so ranking quality is exactly description quality (see
   [Memory index](#memory-index)).

3. Start a session. On the first real prompt, scoring runs and the hint is
   part of the first model context.

Upgrade later with `pi update --extensions`.

---

## Hosted JEV (default backend)

The default lane scores through the hosted JEV / Decisions API
(`typesafe/jev-1.13`, reached via OpenRouter).

- **Credentials:** `OPENROUTER_API_KEY` only. The public extension does not
  read Pi's auth store in v1 (that fallback exists only in the optional local
  CLI). No credential is ever written to the index, the cache, the logs or
  the session, and error messages never echo the key.
- **Pinned model:** the default is `typesafe/jev-1.13`, deliberately not a
  floating `latest` alias — a model change shifts the score distribution,
  which shifts the ranking, which changes agent behavior. Override with
  `PI_JEV_MODEL` for experimentation; the package default stays deterministic.
- **Cost:** each trigger sends **one fanned-out request** — every memory
  question is evaluated in parallel on the same state. Measured on the
  reference setup: **≈ $0.00002** for a minimal state, **≈ $0.00013** for a
  25-memory cold start (output is free) — an interactive session costs
  micro-dollars. Actual cost scales with payload size and provider pricing.
- **Latency & the queue:** typical latency is ~0.7–1.3 s. A capacity queue of
  180–270 s was documented once (2026-09-28) and has **not been reproduced**
  across ~2000 calls since. If such an event ever recurs, the trigger hits
  its time budget and **skips** — the session continues without the hint.
  That skip is the *expected* behavior of the default backend, not a
  malfunction; the next trigger or the on-demand tool is the fallback.
- **Endpoint override:** `PI_JEV_ENDPOINT` changes the remote destination.
  It does not make scoring local or reduce egress — the scoring state follows
  that endpoint. Treat a custom endpoint as a trusted data destination.
- **Why hosted by default:** beyond the zero-dependency install, it is the
  higher-performing measured choice on the reference benchmark (see
  [What the experiments showed](#what-the-experiments-showed)).

---

## Privacy / data egress

With the default (`PI_JEV_BACKEND=jev`), the scoring state leaves the
machine. The complete egress contract:

| Trigger | What is sent |
|---|---|
| Cold start | first **2000 chars** of the first user prompt + memory index **descriptions** |
| Post-compaction | the full Pi-VCC compaction summary + memory index descriptions |
| On-demand tool | the agent-formulated question + memory index descriptions |
| **Never** | **memory file contents** |

The state may contain project names, local paths, work excerpts and
conversation content. **Do not use the hosted lane for confidential sessions**
unless you accept that egress — use the local backend instead.

With `PI_JEV_BACKEND=local`, the extension targets the configured local
scorer endpoint (default loopback) and itself makes no third-party network
request. A custom `JEV_CMD` is trusted code and may have its own behavior
(see below).

Diagnostic logs (`~/.cache/jev-relevance/log.jsonl`) contain trigger,
latency, entry count, top-N ids/scores, backend and failure reasons — never
state bodies, memory contents, classifier prompts or keys.

---

## Local backend (advanced, zero-egress)

The fully local lane reproduces the JEV protocol on your own logit-capable
model. It is a **zero-egress alternative with locally controlled scoring** —
not a "free version" of the hosted scorer (measured out-of-sample calibration
does not transfer between lanes; decisions agree ≈ 92% of the time, absolute
values differ).

```bash
export PI_JEV_BACKEND=local
pip install jinja2 numpy pydantic    # the bundled jev skill's CLI
jev selftest                         # 6-branch canary regression, expect 6/6
```

The **`jev` skill** (bundled in the package) is the reference scorer: a
native-logit yes/no CLI following the
[simple-jev](https://github.com/featherless-ai/simple-jev) protocol, scoring
against a llama-server-style endpoint (default `127.0.0.1:5000`, override
with `--server`). You need a local GGUF served with logprobs enabled (the
reference setup is a Qwen 3.8 27B quant). `jev check` compares the vendored
chat template against your GGUF — set `JEV_GGUF_PATH` to enable that check
(unset or missing, it skips gracefully; `jev ask` never needs it).
`jev selftest` runs directional
canaries only — it validates protocol direction, **not** calibration or
general accuracy. The CLI also has an optional `--backend jev` mode (hosted),
which reads `OPENROUTER_API_KEY` then Pi's auth store as a fallback.

Alternatives: the bundled `scripts/scorer-openai.py` against any
OpenAI-compatible endpoint exposing logprobs, or point `JEV_CMD` at any CLI
implementing `docs/scorer-contract.md`.

`JEV_CMD` is a **trusted-executable boundary**: the configured path is
invoked directly (no shell) and is fully trusted — it receives the scoring
state as a file argument and runs with your privileges. The extension
validates it once when the local backend is selected (must be a regular,
executable file) and skips scoring with a clear log entry otherwise.

**Platform:** the local scorer is a POSIX script (Python + curl) — Windows
support is out of scope for v1. The hosted default lane is pure TypeScript
and works anywhere Pi runs.

---

## Memory index

The index is intentionally small — one entry per memory file:

```json
{
  "memories": [
    {
      "id": "jev-typesafe",
      "path": "/home/you/.pi/agent/memory/jev-typesafe.md",
      "index_line": "optional trigger line from your static index",
      "desc_en": "JEV, classification, calibration and routing",
      "desc_src": "curated"
    }
  ]
}
```

The scorer receives **`desc_en` (the description), never the file contents**.

Good descriptions matter. Garbage descriptions produce garbage priors.

- `scripts/build-index.mjs` (shipped as the `pi-jev-build-index` bin) builds
  the index with **Node.js only** — it derives a first-draft description from
  each file's first heading and marks entries `"desc_src": "derived"` for
  your review.
- `scripts/build-index.py` is the curation pipeline: same output, plus
  `--index-file` support that attaches trigger lines from an AGENTS.md-style
  static index as `index_line` context. (Python is only needed for this
  optional tool.)
- Tip: exclude append-only hypothesis/incident logs from the index
  (`--exclude`) — only durable-fact memories should be scored.

The extension reads `~/.pi/agent/memory_index.json` by default
(`JEV_MEMORY_INDEX` to override).

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PI_JEV_BACKEND` | `jev` | `jev` (hosted) or `local` |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | hosted model id (pinned) |
| `PI_JEV_ENDPOINT` | OpenRouter decisions API | hosted endpoint override; changing it changes the egress destination |
| `OPENROUTER_API_KEY` | — | the only credential the hosted lane reads |
| `JEV_CMD` | `~/.local/bin/jev` | local scorer CLI (trusted-executable boundary) |
| `JEV_MEMORY_INDEX` | `~/.pi/agent/memory_index.json` | index path |

Local-lane extras (reference scorers, not the extension): `JEV_ENDPOINT` /
`JEV_MODEL` for `scripts/scorer-openai.py`, `--server` for the `jev` CLI.

Requirements:

- **Pi coding agent ≥ 1.0** (tested on **1.0.1**) — the post-compaction
  trigger uses the `session_compact` event, which fires synchronously in pi
  1.0 but is not yet covered by the public docs; on older pi the trigger is
  silently absent (cold start and the on-demand tool are unaffected).
- **Pi-VCC (optional)** — without it, the cold start scores the raw context
  and the post-compaction trigger simply never fires.
- **Node.js ≥ 20** (the extension is TypeScript loaded by Pi; the index
  builder is a stdlib-only `.mjs`).
- Hosted lane: nothing else. Local lane: Python 3 + `jinja2 numpy pydantic`
  and a logit-capable server (POSIX only).

---

## Troubleshooting

| Symptom | What it means |
|---|---|
| A trigger silently does nothing | Expected on any failure — check `~/.cache/jev-relevance/log.jsonl` (`backend`, failure reason). The session is never blocked. |
| Hosted: `OPENROUTER_API_KEY not set` in the log | Set the key, or `PI_JEV_BACKEND=local`. |
| A cold start was skipped once | Most likely the remote queue — skip is the expected behavior (see Hosted JEV). The on-demand tool is the manual fallback. |
| Hosted: all scores ≈ 0.00–0.01 | Normal: the **noise floor** — hosted JEV quantizes scores to 2 decimals. The signal is the gradient above the floor. |
| Local: all scores ≈ 0.000–0.001 | Normal: the noise floor (raw logits). Same reading rule. |
| Local tool returns a skip notice (exit 2) | The scorer timed out or returned empty — a degraded (quantized) local model can answer with zero tokens. No retry by design. |
| First local call is slow | ~15-35 s cold, ~10 s warm (shared-prefix KV reuse). Hosted is ~1 s. |
| `empty index` in the log | Build the index (`pi-jev-build-index`) or set `JEV_MEMORY_INDEX`. |
| Nothing happens after compaction | Needs pi ≥ 1.0 **and** Pi-VCC (see Configuration). |

---

## Technical details

### When scoring happens

| Trigger | When | What it does |
|---|---|---|
| **Cold start** | Before the first LLM call of a new session | Scores the prompt head synchronously (budget 45 s) so the first prefill already contains the hint |
| **Post-compaction** | After a Pi-VCC compaction | Scores the rebuilt summary synchronously (budget 120 s); the hint lands in the first post-compaction prefill |
| **On demand** | `memory_relevance` tool call | The agent formulates its own question and gets the **full** ranking |

The automatic paths never force a memory read. Failure always degrades to
"session continues without the hint".

### Architecture

```text
                    Pi coding agent
                           │
                           ▼
                  jev-relevance.ts
                           │
                    relevance layer
                           │
                ┌──────────┴──────────┐
                │                     │
          hosted JEV             local scorer
          (default)              (optional)
                │                     │
        TypeScript fetch         jev CLI
                │                     │
          JEV / hosted           llama-server
                                     │
                                local logits
                           ↓
                  ranked memory hints
                           ↓
                    Pi agent chooses
                           ↓
                     read / ignore
```

The invariant is `SCORE → PRIOR → HINT → AGENT DECIDES`, never
`SCORE → RETRIEVE → INJECT`.

### What the experiments showed

This project started from a simpler question: can a local Qwen 3.8 27B
reproduce useful JEV-style decision signals? The answer was yes, with a
caveat.

A controlled ANLI experiment (1000 lines, 3-way) gave the hosted JEV setup
about **0.79** accuracy and the local model about **0.71** at the initial
operating point. Isotonic calibration could close that gap in-sample. On
FEVER out-of-sample, that calibration did not transfer: all tested systems
were around **0.52**.

What did transfer was the useful part for this project:

- local and hosted decisions agreed about **92%** of the time;
- the confidence signal remained useful for escalation (monotone
  confidence→error curve);
- both lanes therefore work as a **relevance/routing signal** — a prior to
  verify by reading, never a truth value.

### Score semantics

`score = P(Yes)` for the binary relevance question. Scores are for ranking.
`0.98` is not truth, not certainty, not a retrieval mandate. The noise floor
is per-backend: ~0.000–0.001 on local raw logits; ≈ 0.00–0.01 on hosted JEV,
which quantizes probabilities to 2 decimals (the hint text adapts its
guidance to the active backend). The frozen question framings are part of the
measurement — rewording them is a metric-changing change.

### Performance (local lane)

- ~25 memory blocks: **~15 s** (shared-prefix KV reuse between questions)
- 24-branch production-shaped run: **~22.7 s**, down from ~150 s
- hosted lane: **~0.7–1.3 s** per trigger (single fanned-out request)

Scoring is intentionally limited to context boundaries and explicit on-demand
use rather than running on every turn.

### Files

```text
package.json                Pi package manifest (extensions, skills, bin)
jev-relevance.ts            Pi extension (3 triggers, 2 scorer backends)
docs/scorer-contract.md     scorer contract + calibration notes
docs/hero.png               README banner
scripts/build-index.mjs     memory dir → index JSON (Node stdlib, no Python)
scripts/build-index.py      index builder + --index-file curation pipeline
scripts/scorer-openai.py    reference scorer (OpenAI-compatible logprobs)
examples/memory_index.example.json
jev/                        the jev skill — the reference local scorer
jev/jev                     the scorer CLI (ask / selftest / check)
jev/protocol/               vendored simple-jev protocol — Apache 2.0
jev/canaris/                6-branch canary regression (states, questions, refs)
jev/presets/                ready-made noul question presets
jev/template_qwen38.j2      chat template for the reference local GGUF
jev/SKILL.md                the pi skill (when to use it, exit-code discipline)
jev/NOTES.md                design notes + measured proof
```

---

## License

MIT — see LICENSE, **except `jev/protocol/`**, which is vendored
byte-identical (unmodified) from
[simple-jev](https://github.com/featherless-ai/simple-jev) under the Apache
License 2.0 — see `jev/protocol/LICENSE` and `jev/protocol/ATTRIBUTION.md`
(upstream URL, pinned commit, per-file sha256).
