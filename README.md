# Pi-JEV-VCC-memory-relevance

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

Use the bundled reference scorer with any OpenAI-compatible endpoint exposing
logprobs (for example `llama-server` or vLLM), or provide your own implementation
following `docs/scorer-contract.md`.

```bash
chmod +x scripts/scorer-openai.py
```

### 2. Build the memory index

```bash
python3 scripts/build-index.py   --dir ~/.pi/agent/memory   --out ~/.pi/agent/memory_index.json
```

Review the generated descriptions before relying on them.

### 3. Install the Pi extension

```bash
ln -s "$PWD/jev-relevance.ts"   ~/.pi/agent/extensions/jev-relevance.ts
```

Then reload Pi.

---

## Configuration

Optional environment variables:

```text
JEV_CMD
JEV_MEMORY_INDEX
```

The extension expects Pi's extension loader and a running OpenAI-compatible
completion endpoint for the reference scorer.

Requirements:

- Pi coding agent
- Node.js ≥ 20
- local logit-capable scorer

---

## Limitations

This is a **memory navigation aid**, not a truth oracle.

Scores should be interpreted as ranking signals and verified by reading the
memory itself.

The current experiments do not establish general retrieval quality across
hundreds of unrelated sessions. The memory-ranking behaviour is still an
active experiment.

The wording of the scoring prompts is deliberately frozen; changing it means
changing the measurement and should be followed by a re-evaluation.

---

## Files

```text
jev-relevance.ts            Pi extension
docs/scorer-contract.md     scorer contract + calibration notes
scripts/scorer-openai.py    reference scorer
scripts/build-index.py      memory directory → index JSON
examples/memory_index.example.json
```

---

## License

MIT
