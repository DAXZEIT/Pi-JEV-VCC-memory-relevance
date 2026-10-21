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
| smoke 2K | #693–695 | 29,311 / 29,712 / 29,776 | 0 % each | 16.6–17.1 s |
| anchor probe #1 | #697 → #698 → #699 | 6,979 cold → 7,314 → 7,348 | 0 → **95.4 %** → 0 % | 3.8 s / **244 ms** / 4.0 s |
| resubmits, round 1 (03:16:30) | #729–732 | 27,628 / 29,311 / 29,712 / 29,776 | 55.6 % / **100 %** ×3 | 7.5 s / **37–38 ms** |
| resubmits, round 2 (03:17:11) | #733–736 | same 4 prompts | **100 %** ×4 | **286 ms / 37–42 ms** |
| new candidate | **#737** (03:17:13) | 30,907 | **27,627 (89.4 %)** | **2.1 s** |
| next new candidates | #738–740 | 29,412 / 29,797 / 28,522 | **0 %** | 16.6–16.9 s |
| matrix (03:21–03:23) | #742 / #743 · #745 / #746 | 29,040 / 28,311 · 31,322 / 32,772 | 12,800 (44.1 %) / 0 · 21,760 (69.5 %) / 0 | 9.9 / 16.0 · 5.9 / 19.0 s |

The same pattern appears earlier (01:28, ANLI bench, ~3.7K-token prompts):
req#493 reused 91.1 % (3,393/3,725) in 238 ms, then req#494–504 all 0 % at
~2.0 s.

Two properties isolate the mechanism:

1. Identical-prompt resubmissions (no extension) keep hitting while the
   corresponding state/endpoint is resident — two rounds ~40 s apart, under
   a saturated pool, each request at ~1 new token (no allocation pressure).
   Time and repetition alone do not kill the state.
2. The shared state serves the **first new candidate that prefills beyond
   it** (probe #1: 697→698; probe #2: 729→737); subsequent new candidates
   see 0 % (699; 738–740). In probe #1 the state was gone within 4 s of the
   first divergent completion (698 wrote only ~336 new tokens); in probe #2
   it survived two zero-allocation resubmit rounds (~40 s) and died with
   #737's 3,280-token prefill. All of this with a single client and no
   concurrent traffic (this agent's pi session was verifiably idle
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

On a saturated pool, the prefill allocation of the **first new candidate
beyond the shared state** evicts it: the allocation is satisfied by
evicting something, and the superseded fork state is the first target —
any size suffices (336 new tokens in probe #1; 3,280 in probe #2), and the
state is gone before the next new candidate arrives. Zero-allocation
requests (100 % resubmits, ~1 new token) never trigger an eviction, which
is why the state serves repeated resubmissions for ~40 s. On a
non-saturated pool, no eviction fires between resumes, `note_hit`
re-asserts the shared state on every resume, and the chain continues. The
signature is an **eviction artifact**, not an intrinsic consume-on-use.

## Live pool state of the 8 GiB instance (pre-F1, `/stats`)

Snapshot taken live via `GET /stats` on the running instance shortly before
the F1 swap (this session, 2026-10-21 ~04:07); not persisted as an
artifact — the F1 stats pair below brackets the 16 GiB side, and the
request-level evidence above stands independently of these counters.

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
rule predicts (c2 and c3 resumed from the state superseded by c1's
extension). The mechanism diagnosis is confirmed. What F1 does *not* yet
show: retention under steady-state occupancy — this probe ran on a cold
pool with no resident load. Before the equal-budget campaign is declared
unblocked: re-run the probe after the resident session has re-occupied the
pool, and run the campaign with the session inactive, keeping `/stats`
before and after.

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

1. **Keep :18086 at 16 GiB — DECIDED (user, 2026-10-21, after this
   diagnosis)**; rollback setting remains
   `scripts/serve_xl_vision_mtp_18086.sh` (8192). 16 GiB pinned cost vs the
   resident session + campaign fitting comfortably; 8 GiB was the previous
   production setting.
2. **F2 (code, ninfer-all, separate GO)**: a `--fork-retention N` /
   `retain_resume` flag — do not supersede the resumed-from snapshot when
   sibling continuations are expected; the fork state stays retained and is
   evicted only by GDSF value (frequency already prices the fork point).
3. **F3 (workload, no engine change)**: 4K blocks (halves the fork count),
   importance-ordered candidate schedule, and shrinking the 27.6K shared
   context (re-prefilled at every fork = the largest single cost).
4. **The equal-budget 1K/2K/4K campaign** (plan block 4, ~160 candidates,
   ~32 min) can be scheduled on F1 — with the steady-state probe (above) as
   its first gate.

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

- Reviewed 2026-10-21 by an independent read-only sub-agent reviewer
  (gpt-5.6-luna, session `1021-043315-7811`): the mechanism claims checked
  against spec/code/log; the symptom-table token counts and the 01:28 line
  were transcription errors and are corrected in the follow-up commit; the
  conclusion was toned down to match the Limitations section.

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
