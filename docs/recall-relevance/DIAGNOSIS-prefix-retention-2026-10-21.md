# Prefix retention diagnosis — NInfer hybrid cache, 2026-10-21

**Verdict: the shared-prefix anchor is captured and selected correctly; it is
evicted before the second divergent continuation on the 8 GiB host pool.
Root cause = pool saturation (99.2 % used) + the §9.3 supersession policy,
which pre-targets fork states for eviction. Verified fixed by F1: a flag-only
restart at `--host-cache-mib 16384` makes all three divergent candidates hit
the same anchor state (0 evictions).**

This diagnosis was done read-only against the running server (:18086) on
2026-10-21, then confirmed by a controlled experiment (F1) under a separate,
explicit user authorization (04:19, the same day). The blocks 0–3 prototype
GO (see `RESULTS-2026-10-21.md`) did **not** cover this restart; it is
recorded here as its own authorization.

## Context

The recall-relevance prototype replays frozen memory-file blocks as JEV
choice prompts against the local NInfer server. A 2026-10-01 experiment on
llama.cpp showed that sending one raw shared-prefix anchor before the
candidate prompts lets divergent candidates reuse a shared prefix state.
On 2026-10-21 the same probe was ported to NInfer (:18086,
`Qwen3.8-27B-UD-Q4_K_XL-text-vision-mtp`, KV rk4v4 240K, MTP,
`--use-alt-prefix-caching --host-cache-mib 8192`): the first divergent
candidate hit, every subsequent one paid full prefill. The equal-budget
1K/2K/4K campaign (plan block 4) was blocked on this.

## Symptom — the single-consumer signature (8 GiB, saturated pool)

From the debug log `ninfer-xl-compare/logs/serve-xl-18086-20261021.log`
(reconstructing the 03:08–03:24 campaign, req#687–764):

| Window | Requests | Prompt (tokens) | Reused | TTFT |
|---|---|---|---|---|
| smoke 2K | #693–695 | 29,311 / 29,712 / 29,776 | 0 % each | 16.5–17.0 s |
| anchor probe #1 | #697 → #698 → #699 | 6,979 cold → 9,693 → 10,414 | 0 → **95.4 %** → 0 % | 7.4 s / **0.24 s** / 16.6 s |
| anchor resubmits | #729–736 (×8) | 27,628 each | **100 %** | **37 ms** |
| new candidate | **#737** | 30,907 | **27,627 (89.4 %)** | **2.09 s** |
| next new candidates | #738–740 | 30,907 (new suffixes) | **0 %** | 16.5–16.6 s |

The same pattern appears earlier (01:28, ANLI bench zone): req#493 reused
91.1 % (49,852/54,706), then req#494–504 (51.3K tokens) all 0 % at ~18.7 s.

Two properties isolate the mechanism:

1. Identical-prompt resubmissions (no extension) keep hitting at 37 ms —
   including *after* the first divergent candidate already superseded the
   state. Time and repetition alone do not kill the state.
2. The state serves exactly the **first** divergent continuation, then
   subsequent divergent candidates see 0 % — even with a single client and
   no concurrent traffic (this agent's pi session was verifiably idle
   03:05–03:30; the 01:28 occurrence happened during this session's own
   bench runs, but the 03:17 occurrence rules out interference).

## Evidence and method

Three independent layers, all read-only except the F1 experiment:

1. **Request-level log reconstruction** (above): the debug log logs every
   request with `req#`, prompt tokens, `reused N (P%)`, TTFT, selection
   label and cancellation reason. This pinned the single-consumer signature
   and excluded client interference.
2. **Spec + code** (ninfer-all working tree at `074071f7`, test-only delta;
   binary sha256 `1c3cd38c25509a47…`): the hybrid cache semantics below.
3. **Live `/stats`** (`GET http://127.0.0.1:18086/stats`, unauthenticated,
   read-only): cumulative runtime counters and pool occupancy of the
   running instance — captured before and after the F1 probe.

## Mechanism

NInfer's hybrid context cache keeps two cooperating structures:

- **Content-addressed 64-token KV blocks** in a radix tree, retained by
  GDSF (frequency + recency + restore cost).
- **Explicit GDN state snapshots** (tap): a state image (146.8 MiB for this
  build) plus a small KV tail (~1.09 MiB), in a pinned host pool
  (`--host-cache-mib`) with 5 device slots. Resuming from a snapshot costs
  restore + prefill of the extension; the selector picks the minimum-cost
  candidate, and `snapshot_valid` only checks residency.

The two rules that decide retention (spec §9.3–9.5, confirmed in
`src/models/qwen3_5/program/prefix/hybrid_program.cpp` and
`src/runtime/prefix_cache/prefix_index.cpp`):

1. **Supersession.** As soon as a request resumed from snapshot S publishes
   a deeper snapshot, `hybrid_supersede_resume` marks S *superseded*. A
   superseded snapshot is the **first eviction target — both device and
   host copies, with no priority test** (`allocate_slabs`).
2. **Re-assertion.** Resuming *from* a superseded snapshot re-asserts it
   (`note_hit`: "a retry, or a second branch"). A superseded-but-resident
   state therefore remains matchable and reusable.
3. **Unbacked loss.** When the host pool is full, device-side LRU eviction
   can drop a node's last copy; §9.4 then deletes the subtree **with its
   anchored snapshots** (`unbacked_node_losses` counter).

On a saturated pool, each candidate's own release (endpoint image write +
path write-through) triggers an eviction; the superseded fork state is the
first victim — so each candidate's completion kills the state the next
candidate needs. On a non-saturated pool, no eviction fires between
resumes, `note_hit` keeps re-asserting the shared state, and the chain
continues. The single-consumer signature is an **eviction artifact**, not
an intrinsic consume-on-use.

## Live pool state of the 8 GiB instance (pre-F1, `/stats`)

| Counter | Value |
|---|---|
| host_used / host_capacity | 8,517,402,624 / 8,589,418,496 bytes = **99.2 %** |
| host_write_bytes (21 h) | 160.4 GB |
| host_snapshot_evictions | 934 |
| unbacked_node_losses | 995 |
| snapshots alive / taps created | 27 / 837 |
| reused_tokens | 14.59 M ≈ 113 self-endpoint turns of the resident pi agent session |

The largest single tenant of the pool was the resident agent session itself
(~191K-token context ≈ 3.3 GB KV path plus endpoint/tap images,
`last_selection.frontier_tokens = 191016`), with bench runs on top. A
fork-heavy campaign and a live agent session compete for the same 8 GiB.

## F1 verification (16 GiB, controlled experiment)

**Procedure** (explicit GO, 04:19): flag-only swap — same binary, argv
byte-identical except `--host-cache-mib 8192 → 16384`
(`ninfer-xl-compare/scripts/serve_xl_vision_mtp_18086_f1.sh`); watchdog with
240 s health gate and automatic rollback to 8192
(`/tmp/ninfer-f1-swap-20261021.sh`). Old instance (pid 813938) killed at
04:21:26; new instance healthy 10 s after launch. RAM at swap time: 38 GB
available of 62 GB.

**Probe**: the same `scripts/recall_anchor_probe.py`, same private
manifest/annotations as the campaign (sha256 prefix `7882ed78…` /
`42e2ae1d…`), spread schedule, 3 candidates, 120 s budget, cold pool
after restart:

| Request | Prompt | Reused | Prompt time |
|---|---|---|---|
| anchor (sha `9ec4b049…`) | 27,628 | 0 (cold) | 15,540 ms (1,778 tok/s) |
| c1 | 29,311 | **27,627 (94.2 %)** | **1,131 ms** |
| c2 | 29,712 | **27,627 (93.0 %)** | **1,380 ms** |
| c3 | 29,776 | **27,627 (92.8 %)** | **1,398 ms** |

Total wall 19.95 s, exit 0.

**`/stats` after the probe**: host_used 1.73/17.18 GiB = **10.1 %**;
`evicted_blocks = 0`, `host_snapshot_evictions = 0`,
`unbacked_node_losses = 0`; `snapshot_hits = 3`; `reused_tokens = 82,881`
(= 3 × 27,627); `taps_created = 7`; `last_selection.frontier_tokens = 27627`
(c3 resumed from the anchor state).

**Conclusion**: with pressure removed, all three divergent continuations
reused the one shared state — exactly the behavior the spec's re-assertion
rule predicts. The diagnosis is confirmed; the equal-budget campaign gate
is unblocked on the F1 configuration.

## 8 GiB (saturated) vs 16 GiB (F1), same anchor workload

| | 8 GiB, 03:17 (99.2 % used) | 16 GiB, 04:21 (10.1 % used) |
|---|---|---|
| first new candidate | hit 27,627 @ 2.09 s | c1 hit 27,627 @ 1.13 s |
| subsequent new candidates | 0 % @ 16.5–16.6 s (×3+) | **hits @ 1.38–1.40 s (×2)** |
| snapshot evictions (window) | (cumulative 934 over 21 h) | **0** |

## Why the 2026-10-01 llama.cpp anchor did not transfer

llama.cpp's slot-based hybrid cache has no supersession policy: shared
states are slots, evicted by LRU/recency only. The layout (64-token blocks
+ a snapshot at the shared boundary) ported cleanly; the engine's retention
semantics did not. Porting a cache layout between engines without porting
the eviction policy is what turned a working anchor into a single-consumer
one.

## Open decisions and levers

1. **Keep :18086 at 16 GiB (current state) or roll back to 8192**
   (`scripts/serve_xl_vision_mtp_18086.sh`). 16 GiB pinned cost vs the
   resident session + campaign fitting comfortably; 8 GiB is the previous
   production setting.
2. **F2 (code, ninfer-all, separate GO)**: a `--fork-retention N` /
   `retain_resume` flag — do not supersede the resumed-from snapshot when
   sibling continuations are expected; the fork state stays retained and is
   evicted only by GDSF value (frequency already prices the fork point).
3. **F3 (workload, no engine change)**: 4K blocks (halves the fork count),
   importance-ordered candidate schedule, and shrinking the 27.6K shared
   context (re-prefilled at every fork = the largest single cost).
4. **The equal-budget 1K/2K/4K campaign** (plan block 4, ~160 candidates,
   ~32 min) can now be scheduled on F1.

## Limitations

- The F1 probe is one anchor × 3 candidates, run once, right after a cold
  restart. Steady-state occupancy with the resident session active will be
  higher than 10.1 %; long-run retention under concurrent load is not
  measured here.
- `/stats` counters are cumulative since boot; the before/after pair
  brackets only the probe window.
- The 8 GiB numbers come from the 03:17 campaign on the pre-swap instance;
  the 16 GiB numbers from the post-restart probe. Same binary, same argv
  except the pool flag, same anchor sha.
- No quality measurement is claimed in this document: candidate scores are
  a side output of the probe, not an evaluation.

## Provenance and artifacts

- F1 probe output + `/stats` pair: `/home/dax/.cache/jev-recall/20261021-f1-probe/`
  (0700; the probe persists no prompts or source text by contract).
- Watchdog trace: `/tmp/ninfer-f1-swap-20261021.out`.
- Server logs: `ninfer-xl-compare/logs/serve-xl-18086-20261021.log`
  (8 GiB era) and `…-f1-20261021.log` (F1), both `--log-level debug`.
- F1 script: `ninfer-xl-compare/scripts/serve_xl_vision_mtp_18086_f1.sh`.
- Engine: ninfer-all @ `074071f7` (test-only delta), binary sha256
  `1c3cd38c25509a47…`; spec
  `docs/maintainer/hybrid-prefix-cache-spec.md` §9.2–9.5; code
  `src/models/qwen3_5/program/prefix/hybrid_program.cpp`,
  `src/runtime/prefix_cache/prefix_index.cpp`.
