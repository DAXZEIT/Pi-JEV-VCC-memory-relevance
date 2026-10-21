# Offline recall-relevance prototype

**Source checkout only. No installed tool, automatic Pi hook or memory-backend
replacement.** See [source contract](CONTRACT.md) and
[measured limitations](RESULTS-2026-10-21.md) before running a real campaign.
This prototype has not passed its full-zone quality/latency gate.

The 2026-10-21 shared-prefix retention failure on NInfer (saturated 8 GiB
host pool evicting fork states between divergent candidates) is documented
in [DIAGNOSIS-prefix-retention-2026-10-21.md](DIAGNOSIS-prefix-retention-2026-10-21.md).
The equal-budget campaign should run with `--host-cache-mib >= 16384`.

## Requirements

- Run commands from the source repository root, with Python 3.10+ and the
  existing local JEV dependencies (`jinja2`, `numpy`, `pydantic`).
- Canonical snapshot bridge: an installed managed Pi, its `jiti` dependency,
  and pi-vcc **0.10.2**. The observed Pi version is **1.3.0**; paths can be
  specified using `--pi-package` and `--vcc-package`. Revalidate for upgrades.
- Scoring: an already-running local endpoint with `/tokenize`,
  `/v1/completions`, native top-20 logprobs and the validated JEV tokenizer /
  template. No model/service configuration is modified by these commands.

## 1. Freeze one compaction and segment on CPU

Use an explicit session path and compaction entry ID, not a moving latest leaf.
The case output directory must not already exist; it will be created 0700.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m recall_relevance.cli snapshot \
  --session /absolute/path/to/session.jsonl \
  --compaction COMPACTION_ID \
  --out "$HOME/.cache/jev-recall/my-new-case" \
  --sizes 1000 2000 4000 --overlap 128
```

This is CPU-only. It reads through the compaction cutoff, invokes canonical
projection/index functions, and writes `snapshot.json` plus three manifests.
It does not open/migrate/save a SessionManager. Files are 0600; raw source text
stays private. `--no-thinking` explicitly excludes and counts plaintext thoughts;
opaque signatures/media are always excluded from text scoring.

1K/2K/4K are escaped-byte **estimates**, not tokenizer token counts. The complete
scoring prompt will be tokenized independently before inference.

## 2. Inspect exact source and freeze annotations BEFORE scoring

Each manifest has source parts, hashes, source intervals, block IDs and exact
recall refs. Use block metadata to choose what to inspect; do not dump whole
manifests into an agent context.

```bash
python3 -B -m recall_relevance.cli consult \
  --manifest "$HOME/.cache/jev-recall/my-new-case/manifest-2000.json" \
  --block recall-BLOCK_ID
```

This deliberately prints selected source spans with angle-marker escapes.
It is **not `vcc_recall`**, and does not imply that recall expand exposes thoughts.
Offsets are Python Unicode code points. Manifests retain exact originals.

For evaluation, manually create a private `annotations.json` before any score
is seen. Required shape:

```json
{
  "schema": 1,
  "compaction_id": "COMPACTION_ID",
  "source_prefix_sha256": "COPY_FROM_MANIFEST",
  "consultation_budget_estimated_tokens": 8000,
  "items": [
    {
      "id": "constraint-1",
      "source_id": "COPY_EXACT_SOURCE_ID",
      "start": 0,
      "end": 100,
      "text_sha256": "SHA256_OF_THAT_EXACT_SOURCE_SUBSTRING",
      "importance": "critical"
    }
  ],
  "digest_refs": [1, 3]
}
```

Replace placeholders, offsets and refs with actual observations. Do not invent
an annotation to fill a category absent from the case. IDs are diagnostic
metadata, not a place to copy secrets. Fix consultation budget, quality criteria
and runtime ceiling before evaluating a held-out case. The CLI verifies source
checksums and binds the annotation file before inference; changes during scoring
cause refusal to save results.

## 3. Explicit bounded local scoring

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m recall_relevance.cli score \
  --manifest "$HOME/.cache/jev-recall/my-new-case/manifest-2000.json" \
  --annotations "$HOME/.cache/jev-recall/my-new-case/annotations.json" \
  --server http://127.0.0.1:18086 \
  --timeout 60 --max-prompt-tokens 49152 --max-blocks 8 \
  --output "$HOME/.cache/jev-recall/my-new-case/scores-new.json"
```

- The output must be new, in an existing private directory. No clobber.
- Server must be a loopback IP literal (127/8 or ::1), not a hostname. No proxy,
  redirects or hosted fallback, regardless of the extension's backend setting.
- One total scoring deadline, not a fresh timeout per candidate. No retry loop.
- Every input has an output row. Unscored rows have null score and a reason.
- Rows keep scheduled order; they are **not a sorted display hint**.
- Default schedule `spread` visits latest, earliest, midpoint, then remaining
  intervals breadth-first. It is a deterministic permutation, **not a semantic
  importance filter**. `--schedule chronological` is an explicit alternative.
- Prompt ceilings include the whole rendered prompt; inputs are never silently
  truncated. An over-limit candidate fails without completion inference.
- Results contain numeric metadata/refs/hashes, not source blocks or completions.
  Unknown backend metric paths are omitted and flagged.
- A partial run exits normally with `status=partial`; inspect coverage, not just
  process exit status. Do not resume timeout runs until they appear complete.

`score_blocks(context, blocks, *, server, timeout_s=90,
max_prompt_tokens=16384, max_blocks=None)` is the callable interface.
Blocks require string `id`, string `text`, integer-list `refs`. It has no file
writes, global CLI, hook or provider fallback.

## 4. Evaluate fixed-budget rankings

```bash
python3 -B -m recall_relevance.cli evaluate \
  --manifest "$HOME/.cache/jev-recall/my-new-case/manifest-2000.json" \
  --scores "$HOME/.cache/jev-recall/my-new-case/scores-new.json" \
  --annotations "$HOME/.cache/jev-recall/my-new-case/annotations.json" \
  --output "$HOME/.cache/jev-recall/my-new-case/evaluation-new.json"
```

This is CPU-only. It verifies manifest/annotation bindings, compares JEV,
recency, chronological and digest-shaped-pointer orders under the same estimated
consultation ceiling and the **same actually scored pool**, and reports:

- total/scored/failed/not-attempted blocks and failure reasons;
- unique source characters scored versus all admissible source characters;
- full-span annotation coverage (a tiny overlap is not a covered annotation);
- consultation cost and selected source redundancy;
- whether each missed item was outside the scored pool or not selected.

The pointer control is not the exact delivered digest. A partial, tiny pool can
make all methods tie because everything fits under the ceiling. Neither ties
nor synthetic smoke controls establish full-zone retrieval quality.

## Optional diagnostic only: raw-prefix anchor

`scripts/recall_anchor_probe.py` adds one completion of the strict common prefix
and discards its output before submitting identical candidate prompts. At most
16 candidates and 120 s total; default three candidates / 90 s. It requires the
same private inputs, explicit loopback server and new output path.

This is an experiment, **not a cache fix**: the observed reuse did not persist
across unseen candidates on the active server. Repeated full prompts can hit
old endpoints; do not misattribute those hits to the anchor. Do not change
runtime flags, flush caches or restart a daily server for this diagnostic.

## Verification and production boundary

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_recall_*.py' -v
npm run typecheck
git diff --check
```

The tests use synthetic inputs and a loopback fake HTTP server, **not live model
inference**. Existing JEV canaries are a separate explicit live check.

Do not wire any of these functions into `session_compact`, change the memory
index backend, or delete the cold-start/on-demand paths. Integration requires
another design decision and explicit GO after quality and latency validation.
