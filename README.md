# Pi-JEV-VCC-memory-relevance

![The noise floor, the graduated ranking, the dashed cut, and the hand that picks only the brightest](docs/hero.png)

**A lightweight memory relevance scorer for the Pi coding agent.**

Long-running agents accumulate memory. The hard part is not storing it — it's knowing
which parts might matter *right now*.

This extension uses a small local classifier to score the relevance of each memory
block against the agent's current context.

It does **not** retrieve or inject memories automatically.

It simply gives the agent a ranked hint about where to look.

---

## The idea

Pi already keeps long-term memory as Markdown files:

```text
~/.pi/agent/memory/*.md
```

This extension keeps a lightweight index of those files and asks a local
yes/no classifier, using **native token logits**, how useful each memory would
be for continuing the current task.

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

The scores are a **prior, not a verdict**.

The agent still decides what to read.

Nothing is automatically injected.

---

## Why logits?

This started from an experiment with JEV-style classification on a local Qwen 3.8 27B.

Instead of embeddings, the scorer reads the model's native probability for
a yes/no decision:

> **"For continuing the current task correctly from this context, how useful would it be to consult this memory block?"**

That gives a graded ranking rather than a single nearest-neighbour result.

A score of `0.98` says "look here first".

A score of `0.26` does **not** mean "irrelevant".

It simply means "much weaker signal than the memories above it".

That lower part of the ranking is still useful: weak links can point the agent
toward adjacent context it would not otherwise consider.

---

## How it works

```text
                    Current context
                           │
                           ▼
                         Pi-VCC
                           │
                           ▼
                    context snapshot
                           │
                           ▼
                     local classifier
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
          memory A      memory B      memory C
            0.98          0.71          0.04
             │             │             │
             └─────────────┴─────────────┘
                           │
                           ▼
                    ranked memory hints
                           │
                           ▼
                       Pi agent
                           │
                    read / ignore
```

There is deliberately no LLM in the middle:

```text
VCC → scorer → ranking
```

Pi-VCC already provides the current task context. The scorer only ranks the
existing memory index against it.

---

## When scoring happens

There are three entry points.

| Trigger | When | What it does |
|---|---|---|
| **Cold start** | Before the first LLM call of a new session | Adds a small relevance hint to the first prompt |
| **Post-compaction** | After a Pi-VCC compaction | Re-ranks memories against the rebuilt context |
| **On demand** | `memory_relevance` tool call | Returns the full ranking when the agent explicitly asks |

The automatic paths never force a memory read.

If scoring fails, the session continues normally and Pi falls back to its
static memory index.

---

## Why this is useful

A normal memory system answers:

> "What should I retrieve?"

This one answers something slightly different:

> **"Where might useful context be?"**

That distinction matters because the main agent remains in control.

A typical flow is:

```text
user request
    ↓
memory relevance scores
    ↓
agent notices:
  "pi-vcc.md looks useful"
    ↓
read pi-vcc.md
    ↓
continue
```

The scorer does not know what the agent ultimately needs.
It only makes the search surface easier to navigate.

---

## Memory index

The index is intentionally small.

Each entry contains an identifier, a path and a short description:

```json
{
  "memories": [
    {
      "id": "jev-typesafe",
      "path": "~/.pi/agent/memory/jev-typesafe.md",
      "description": "JEV, classification, calibration and routing"
    },
    {
      "id": "pi-vcc",
      "path": "~/.pi/agent/memory/pi-vcc.md",
      "description": "Pi-VCC compaction and context reconstruction"
    }
  ]
}
```

The scorer receives the **description**, not the full memory file.

Good descriptions matter.

Garbage descriptions produce garbage priors.

The auto-generated descriptions are a first draft — the best index comes from
curating them. If you keep an AGENTS.md-style static index (one
`trigger → file` line per memory), pass `--index-file` to
`scripts/build-index.py` and those trigger lines become the descriptions
(`"desc_src": "index"` — the provenance field in
`examples/memory_index.example.json` records where each description came
from).

---

## Performance

The scorer runs locally and uses shared-prefix KV reuse between memory questions.

On the reference setup:

- ~25 memory blocks: **~15 seconds**
- shared-prefix anchor: **~0.4 seconds**
- 24-branch production-shaped run: **~22.7 seconds**, down from ~150 seconds

The operation is intentionally limited to context boundaries and explicit
on-demand use rather than running on every turn.

---

## What the experiments showed

This project started from a simpler question:

> Can a local Qwen 3.8 27B reproduce useful JEV-style decision signals?

The answer was **yes**, but with an important caveat.

A controlled ANLI experiment gave the hosted JEV setup about **0.79** 3-way
accuracy and the local model about **0.71** at the initial operating point.
Isotonic calibration could close that gap in-sample. On FEVER out-of-sample,
that calibration did not transfer: all tested systems were around **0.52**.

What did transfer was the useful part for this project:

- local and hosted decisions agreed about **92%** of the time;
- the local confidence signal remained useful for escalation;
- the same model could therefore act as a cheap **relevance/routing signal**
  without being a drop-in replacement for JEV itself.

---

## Installation

### 1. Scorer

The bundled **`jev/` skill** is the reference setup: a native-logit yes/no
scorer CLI following the [simple-jev](https://github.com/featherless-ai/simple-jev)
protocol, with a local backend (a llama-server-style endpoint, default
`127.0.0.1:5000`, override with `--server`) and an optional hosted backend
(`--backend jev` — that call **leaves the machine**, the local one is the
default: zero egress, zero cost).

```bash
pip install jinja2 numpy pydantic
ln -s "$PWD/jev"   ~/.pi/agent/skills/jev   # pi auto-discovers the skill
ln -sf "$PWD/jev/jev" ~/.local/bin/jev      # the CLI on PATH
jev selftest                                # 6-branch canary regression, expect 6/6
```

Alternatives: the bundled `scripts/scorer-openai.py` with any
OpenAI-compatible endpoint exposing logprobs (`chmod +x` first), or point
`JEV_CMD` at any CLI implementing `docs/scorer-contract.md`.

### 2. Build the memory index

```bash
python3 scripts/build-index.py   --dir ~/.pi/agent/memory   --out ~/.pi/agent/memory_index.json
```

Review the generated descriptions before relying on them (or curate them with
`--index-file`, see Memory index above).

### 3. Install the Pi extension

```bash
ln -s "$PWD/jev-relevance.ts"   ~/.pi/agent/extensions/jev-relevance.ts
```

Then reload Pi.

---

## Configuration

Optional environment variables:

```text
JEV_CMD            scorer CLI (default: the jev skill's CLI on PATH)
JEV_MEMORY_INDEX   memory index path (default: ~/.pi/agent/memory_index.json)
JEV_ENDPOINT       OpenAI-compatible endpoint for scripts/scorer-openai.py
JEV_MODEL          model name for the reference scorer
OPENROUTER_API_KEY OpenRouter key for the jev skill's hosted backend
                   (fallback: Pi's auth store — see jev_key() in jev/jev)
```

Requirements:

- **Pi coding agent ≥ 1.0** — the post-compaction trigger uses the
  `session_compact` event. On pi 1.0 this hook fires synchronously (pi awaits
  it before resuming the agent) but is not yet covered by the public docs;
  on older pi the trigger is silently absent.
- **Pi-VCC (optional)** — without it, the cold start scores the raw context
  and the post-compaction trigger simply never fires.
- Node.js ≥ 20
- a local logit-capable scorer (the bundled `jev/` skill needs
  `jinja2 numpy pydantic`)

---

## Troubleshooting

| Symptom | What it means |
|---|---|
| Tool returns a skip notice (exit 2) | The scorer timed out or returned empty — a degraded (quantized) local model can answer with zero tokens. No retry by design; the next trigger or the on-demand tool is the fallback. |
| All scores ≈ 0.000–0.001 | Normal: that is the **noise floor** (calibrated P("Yes") on irrelevant memories). The signal is the gradient above it. |
| First call is slow | ~15-35 s cold, ~10 s warm (shared-prefix KV reuse). |
| `memory index not found` | No default path is trusted — set `JEV_MEMORY_INDEX` or pass `--index`. |
| Nothing happens after compaction | Needs pi ≥ 1.0 (see Configuration). |

---

## Files

```text
jev-relevance.ts            Pi extension
docs/scorer-contract.md     scorer contract + calibration notes
docs/hero.png               README banner
scripts/scorer-openai.py    reference scorer (OpenAI-compatible logprobs)
scripts/build-index.py      memory dir → index JSON (+ --index-file curation)
examples/memory_index.example.json
jev/                        the jev skill — the reference scorer
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
