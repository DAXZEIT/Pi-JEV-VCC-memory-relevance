# Pi-JEV-VCC Memory Relevance — Public Package Spec

## Status

Draft implementation spec for the public Pi package.

Target: publish `Pi-JEV-VCC-memory-relevance` as a normal Pi npm package installable with:

```bash
pi install npm:@daxzeit/pi-jev-vcc-memory-relevance
```

The project is **Pi-first**. Do not turn this into a generic agent framework or perform a broad architecture rewrite for v1.

---

## 1. Product goal

Ship a Pi extension that adds **memory relevance scoring** to the Pi coding agent.

The extension should use the **real JEV backend by default** for the public distribution, while retaining the existing **fully local scorer as an optional advanced backend**.

The core semantic contract remains:

```text
current state + memory descriptions
        ↓
relevance scorer
        ↓
graduated ranking
        ↓
Pi agent decides what to read
```

The scorer produces a **prior**, not a verdict.

The extension must never automatically retrieve or inject the contents of memory files based on the score.

---

## 2. Public backend strategy

### 2.1 Default backend: hosted JEV

The public npm package should default to the real JEV / Decisions API.

Goals:

- no Python dependency for the normal public installation;
- no requirement for a local Qwen model;
- no requirement for llama-server;
- minimal setup;
- use JEV's native typed decision semantics;
- preserve the existing question framing and `noul = p(true)` interpretation.

Default model should be **pinned**, not a floating latest alias.

Initial default:

```text
typesafe/jev-1.13
```

The model identifier should be configurable for experimentation, but the package default must remain deterministic.

[AM5] The default is also the **higher-performing measured choice on the
reference benchmark**, not only the zero-dependency one. Measured on the
reference setup (ANLI 3-way, 1000 lines): hosted JEV ≈ 0.79 vs local ≈ 0.71
at the initial operating point; on FEVER out-of-sample neither calibration
transfers, but local and hosted decisions agree ≈ 92% of the time. The local
lane is therefore positioned in the docs as a **zero-egress alternative with
locally controlled scoring**, not a "free version" of the scorer. ("Calibrated"
is deliberately not used for the local lane: the measured OOS result is that
local calibration does not transfer.)

[AM9] Cost datum for the README (measured): hosted scoring sends **one
fanned-out request per trigger** — every memory question is evaluated in
parallel on the same state — at ≈ $0.00002–0.0002 per call (400–600 input
tokens, $0.042/Mtok input, output free; the 1000-call bench lane of
2026-09-30 cost $0.021 total). An interactive session (cold start + a few
compactions or tool calls) costs micro-dollars. The README must state this:
for an egress-by-default lane, cost is part of the trust contract, exactly
like the concrete payload of §7.

### 2.2 Optional backend: local

The existing local scorer remains supported.

Local mode:

```text
Pi extension
    ↓
jev CLI
    ↓
127.0.0.1:5000
    ↓
local logit-capable model
```

The local backend is intended for:

- zero-egress operation;
- offline/local use;
- advanced users;
- reproducibility experiments;
- users who already operate the Qwen/llama-server setup.

The local lane must preserve the current scorer implementation, protocol checks, template checks, canaries and timeout semantics.

### 2.3 Backend selection

Introduce a single explicit configuration switch, for example:

```text
PI_JEV_BACKEND=jev
PI_JEV_BACKEND=local
```

Default:

```text
PI_JEV_BACKEND=jev
```

The extension must never silently fall back from hosted JEV to the local backend or to a custom endpoint.

A missing/unusable hosted configuration should degrade cleanly according to the existing "signal is optional" behavior.

---

## 3. Hosted JEV implementation

For the public extension, the hosted path should be implemented directly from TypeScript rather than:

```text
Pi TS → Python CLI → curl → JEV API
```

Preferred:

```text
Pi TS → JEV/OpenRouter HTTP API
```

An official TypeScript SDK may be used when it provides a stable, lightweight implementation. Native `fetch` is acceptable when it keeps the dependency surface smaller.

[AM3] Timeout semantics for the hosted transport (measured requirement): the
request must use a **hard TOTAL timeout** (e.g. `AbortController`), not a
connect/per-operation timeout. On this endpoint, per-operation socket
timeouts were observed to be non-binding (a 15 s per-socket budget returned
at 30.5 s, while a total-time flag was respected) — the endpoint holds
connections through capacity queueing. A per-operation timeout in the TS
client would silently inherit the same hole.

[AM8] Concrete hosted transport facts (measured, 2026-10-04):

- **Path**: `POST https://openrouter.ai/api/alpha/decisions` — the
  `/api/v1/*` paths return 403/400 on this API; the alpha path was read
  from the official SDK source (`@openrouter/sdk`). It is an alpha
  endpoint: treat its stability as unguaranteed and keep it behind the
  scorer abstraction.
- **Response**: `answers.<questionId>.noul` carries P(Yes); the normalizer
  maps that field into the scorer contract.
- **No local probing in hosted mode**: the `JEV_CMD` executable validation
  at load must run only when the local backend is selected — otherwise a
  clean hosted install shows a spurious warning on the exact §18
  acceptance path.

The extension should send the same logical payload already used by the current CLI:

```text
model
state
questions
```

and normalize the response into the scorer contract consumed by the extension.

The existing relevance code should not have to care whether the score came from remote JEV or the local CLI.

---

## 4. Scorer abstraction inside the existing extension

Do **not** build a generic framework.

Introduce only the smallest internal abstraction needed to keep the two backends interchangeable.

Conceptually:

```ts
interface RelevanceScorer {
  score(
    state: string,
    memories: MemoryEntry[],
    framing: Framing,
    options?: ScoreOptions,
  ): Promise<RelevanceResult>;
}
```

Two implementations are sufficient for v1:

```text
JevRemoteScorer
LocalCliScorer
```

The existing `jev/` CLI can remain intact as the local implementation.

The Pi extension should consume only normalized results.

---

## 5. Existing Pi behavior to preserve

All current extension behavior remains.

### Cold start

Trigger:

```text
before_agent_start
```

Behavior:

- fire once for the first meaningful user prompt;
- do not score short prompts or slash commands;
- score against the first 2000 characters of the request;
- block only within the existing hard budget;
- provide a top-N relevance hint to the first model context;
- no memory contents are injected;
- failure => session continues normally.

### Post-compaction

Trigger:

```text
session_compact
```

Behavior:

- score the Pi-VCC compaction summary;
- skip unsupported/non-Pi-VCC compactions;
- skip empty summaries;
- skip retry events;
- preserve current synchronous delivery behavior;
- failure => session continues normally.

[AM11] Hook-stability risk (public distribution): `session_compact` is
present in pi 1.0's type declarations but absent from the public docs. The
package must record the pi version it was tested against (the README
already states pi ≥ 1.0; §18 requires recording it in the README), and if the hook is
ever renamed or removed this trigger must no-op silently — already true by
the skip design. Cold start and the on-demand tool are unaffected either
way.

### On-demand tool

Tool:

```text
memory_relevance
```

Behavior:

- accepts a query formulated by the agent;
- returns the full ranking;
- keeps the existing "slow, at most once per task" contract;
- no automatic memory retrieval;
- agent chooses which memory file to read.

---

## 6. Memory data model

The scorer must continue to score **memory descriptions**, not the contents of memory files.

Important product guarantee:

```text
The scorer never reads your memory files.
It only sees memory IDs and descriptions from the index.
```

The generated memory index remains user-curated.

The installer/docs must clearly warn that relevance quality depends on description quality.

[AM7] Index generation on the hosted lane (gap closed 2026-10-04): §11
forbids a Python dependency on the default path, but the only shipped index
builder is `scripts/build-index.py` — a clean hosted install had no
supported way to obtain the index the feature scores. Decision: the package
will ship a dependency-free TypeScript builder (`scripts/build-index.mjs`
— to be implemented, step 1b of §22 — Node stdlib only: scan `memory/*.md`,
derive `desc_en` from the first heading, mark entries `"desc_src":
"derived"` for human review). It will emit the same index JSON format; the LLM-assisted `desc_en` rewrite and the
curation pipeline stay in the Python script, which remains the local/dev
tool.

Do not change the existing memory-index format unless required for package installation.

---

## 7. Privacy / egress model

The public documentation must distinguish the two lanes clearly.

### Hosted JEV

When:

```text
PI_JEV_BACKEND=jev
```

the scoring state is sent to the hosted JEV service.

The documentation must explicitly warn that the state may contain:

- project names;
- local paths;
- work excerpts;
- conversation content;
- compaction summaries.

[AM5] The docs should state the **concrete payload**, not only the
categories. Complete egress contract:

```text
Cold start:
  first 2000 chars of the first user prompt
  + memory index descriptions
Post-compaction:
  full Pi-VCC compaction summary
  + memory index descriptions
On-demand:
  agent-provided question
  + memory index descriptions
Never:
  memory file contents
```

For an egress-by-default lane, specificity is the trust mechanism.

Users should not use the hosted lane for confidential sessions unless they accept that egress.

### Local

When:

```text
PI_JEV_BACKEND=local
```

the extension targets the configured local scorer endpoint, defaulting to loopback.

The extension itself does not make a built-in third-party network request in the local path.

If the local scorer command is user-customized, it remains a trusted executable boundary and may have its own behavior.

---

## 8. Authentication

Hosted JEV authentication should support the user's normal OpenRouter/JEV credential configuration.

At minimum:

```text
OPENROUTER_API_KEY
```

The existing Pi auth-store fallback is **not** reused in the public TS extension in v1 (superseded by [AM5] below — it stays in the local CLI only), and the public extension must never require users to expose the key in repository files.

[AM5] Decision for v1: the public hosted lane reads `OPENROUTER_API_KEY`
**only**. The Pi auth-store fallback stays in the local CLI (where it already
exists) and is not wired into the public TS extension — the auth store is an
internal pi format with an unguaranteed shape, and coupling the default
egress path to it buys little convenience for extra surface. Revisit once
the auth-store shape is documented/stable.

No credentials may be written to:

```text
memory/
~/.cache/jev-relevance/
session context
repository files
logs
```

Error messages must not echo the key.

---

## 9. Configuration surface

Keep the public configuration intentionally small.

Proposed:

```text
PI_JEV_BACKEND=jev|local
PI_JEV_MODEL=typesafe/jev-1.13
PI_JEV_ENDPOINT=<optional override for hosted backend>
JEV_CMD=<local scorer executable>
JEV_MEMORY_INDEX=<memory index path>
```

Existing local variables may remain supported for compatibility.

Avoid introducing a large configuration file in v1.

---

## 10. npm / Pi package packaging

Add a `package.json` declaring the Pi package contents.

The package must expose:

```text
./jev-relevance.ts
./jev
```

through Pi's package manifest.

Conceptually:

```json
{
  "name": "@daxzeit/pi-jev-vcc-memory-relevance",
  "version": "0.1.0",
  "license": "MIT",
  "keywords": ["pi-package"],
  "pi": {
    "extensions": [
      "./jev-relevance.ts"
    ],
    "skills": [
      "./jev"
    ]
  }
}
```

The exact Pi package manifest should follow the currently supported Pi package schema.

[AM1] `keywords: ["pi-package"]` is what makes the package discoverable in
the Pi package gallery (pi.dev/packages) — verified against the current pi
package schema. The npm scope `@daxzeit` is already published (pipeline-moe
clients), so there is no namespace risk.

The package must not require users to manually create symlinks.

After installation, Pi should discover both:

```text
extension: jev-relevance.ts
skill:     jev/
```

---

## 11. Python dependency policy

The hosted/default lane must **not** require:

```text
Python
numpy
pydantic
jinja2
llama-server
local GGUF
```

Those dependencies belong to the optional local/reference scorer lane.

The repository may continue to include them for:

- local operation;
- reproducibility;
- protocol verification;
- development;
- canary testing.

The README should distinguish clearly between:

```text
Quick install / hosted JEV
```

and:

```text
Advanced local backend
```

---

## 12. Local scorer compatibility

The existing local scorer remains the reference implementation.

Do not regress:

- single-token boundary checks;
- template freshness checking;
- shared-prefix KV anchor;
- timeout/deadline handling;
- directional canary suite;
- `--dump-prompt` safety behavior;
- protocol vendoring and attribution;
- exit-code semantics.

The local scorer should continue to expose the existing CLI contract:

```text
jev ask --state <file> --questions <file> --timeout <seconds>
```

The extension should invoke it only when local mode is selected.

---

## 13. Score semantics

The extension must preserve:

```text
score = P(Yes)
```

for the binary relevance question.

Scores are for ranking.

Do not present:

```text
0.98 = truth
0.98 = certainty
0.98 = retrieval mandate
```

The UI/model-facing explanation should continue to say that the score is:

```text
informational
prior
weighted hint
verify by reading
```

The existing noise-floor guidance may remain.

[AM10] The noise-floor wording is local-lane calibrated: the 0.000–0.001
floor was measured on the local raw-logit scorer, while hosted JEV
quantizes probabilities to 2 decimals (measured) — the in-noise gradient
disappears (0.00 vs 0.01) and the rendered guidance line ("0.000-0.001 is
the noise floor") becomes partially inaccurate on the default lane. When
the hosted backend is active, validate the block wording against hosted
score distributions and adapt the numbers — same doctrine (prior, not
verdict), backend-accurate figures.

Do not introduce a universal "relevance threshold" in v1.

---

## 14. Model/version policy

Default hosted model:

```text
typesafe/jev-1.13
```

Do not default to a floating `latest` alias.

Reason:

```text
model change
    ↓
score distribution change
    ↓
ranking change
    ↓
agent behavior change
```

The model identifier should be exposed as an override for experimentation.

Changes to the frozen question framing remain a **measurement-changing change** and should not be casually edited.

---

## 15. Error behavior

The relevance feature is always optional.

Failure modes:

```text
missing key       → skip
remote timeout    → skip
remote API error  → skip
local scorer fail → skip
local timeout     → skip
invalid score     → skip
empty ranking     → skip
```

The agent session must continue.

Do not add automatic retry loops.

The existing timeout discipline remains authoritative.

For remote JEV, use a bounded request timeout appropriate for an interactive Pi session.

[AM2] Queue expectation (measured, honestly sourced): hosted JEV latency is
~0.7 s typical — 22 consecutive calls on 2026-10-03 (0.39–1.33 s, median
0.71 s) and the 2×1000 bench calls of 2026-09-30 (0.8–1.1 s) show no queue.
A capacity queue of 180–270 s was **documented once (2026-09-28**,
contemporaneous record: 0.71 → 180.6×4 → 269 → 272 → 0.76 s pattern, TypeSafe
"very high demand" doc) and has **not been reproduced** across the ~2000
subsequent calls. The interactive budgets (45 s cold start / 120 s
post-compaction) are deliberately shorter than 180 s so that, if such an
event ever recurs, the hosted lane times out and skips. That skip is the
**expected behavior of the default backend, not a fault** — the README must
say so explicitly, or a public user whose first cold start was skipped reads
the package as broken. README wording: "queue documented 2026-09-28, not
reproduced since" — the timeout policy is tail insurance, not a recurring
expectation.

---

## 16. Logging

Continue writing diagnostic information to the existing local cache log.

Logs may include:

```text
trigger
timestamp
latency
number of scored memories
top-N IDs and scores
backend
failure reason
```

Logs must not contain:

- API keys;
- full prompt/state;
- full memory contents;
- rendered classifier prompts.

Do not log remote state bodies.

---

## 17. Licensing

Keep the current license structure:

```text
project code        MIT
jev/protocol/       Apache-2.0
```

Keep:

```text
jev/protocol/LICENSE
jev/protocol/ATTRIBUTION.md
```

and the pinned upstream attribution.

Do not merge the vendored protocol license into the top-level MIT license.

---

## 18. Release acceptance criteria

The package is ready for public release when all of the following are true:

### Installation

```bash
npm pack --dry-run
```

contains the extension, skill, vendored protocol and required runtime files.

[AM12] `package.json` must declare a `files` whitelist (not `.npmignore`),
and the dry-run check must explicitly verify the tarball contains no
`__pycache__/`, no `*.pyc`, and no editor/cache artifacts — a
`jev/protocol/__pycache__/` directory exists in the working tree today and
would otherwise be published.

A clean install can run:

```bash
pi install npm:@daxzeit/pi-jev-vcc-memory-relevance
```

without manual symlinks.

### Hosted path

A clean Pi installation (record the tested pi version in the README) with only an OpenRouter/JEV credential can:

```text
start session
→ score memory relevance
→ receive ranking
→ continue normally
```

without Python/local-model dependencies.

[AM4] This test is capacity-flaky by construction (see the queue expectation
above): run it in a non-queue window (probe a few standalone calls first),
and treat "clean skip on timeout, session continues normally" as a **PASS**
for the resilience half of the test whenever the queue is active. Do not
block release on a green ranking test.

### Local path

A user with the existing local scorer can explicitly select local mode and obtain the same relevance feature.

### Failure testing

Verify:

```text
no key
bad remote endpoint
remote timeout
missing local scorer
local timeout
malformed scorer response
```

all degrade without killing the session.

### Regression

Run:

```text
tsc --strict
jev selftest
```

and retain the existing directional-canary wording:

```text
directional canaries only
does NOT validate calibration or general accuracy
```

### Privacy

Verify that:

```text
hosted mode → intentional JEV egress
local mode   → local scorer path
```

is accurately reflected in the README.

### Security

Verify:

- no historical secrets;
- no credentials in package contents;
- `JEV_CMD` is explicitly documented as a trusted executable boundary;
- no shell execution is introduced.

---

## 19. Documentation structure

README should lead with the public user journey:

```text
What it does
↓
Why relevance is useful
↓
Install with pi install
↓
Hosted JEV setup
↓
Privacy / egress
↓
Local backend (advanced)
↓
Memory index
↓
Troubleshooting
↓
Technical details
```

The README should not make the local Qwen setup look like the normal installation path.

Suggested positioning:

> **Pi memory relevance, powered by JEV.**
>
> Scores your indexed memories against the current task and gives Pi a ranked hint about where useful context may live.
>
> The default backend is JEV. A fully local scorer is available for users who want zero-egress operation.

[AM6] The restructure is a **dedicated pass, not a byproduct of step 7**: the
current README is a local-first narrative with ADV-review artifacts to
preserve (hero image, troubleshooting table, MIT/Apache license split,
trusted-executable boundary paragraph). Plan it as its own task with a
checklist of what must survive the rewrite. Also add one line to the local
backend docs: the local scorer is a POSIX script (Python + curl); Windows
support is out of scope for v1.

---

## 20. Explicit non-goals for v1

Do **not**:

- build a generic multi-agent framework;
- abstract Pi lifecycle into a separate harness SDK;
- implement automatic memory retrieval;
- automatically inject memory file contents;
- add embeddings;
- add a database/vector store;
- add automatic threshold tuning;
- make the hosted backend optional only;
- require local model infrastructure for normal installation;
- replace the existing protocol with a new scoring format.

The objective is a **small, installable Pi plugin backed by real JEV, with the existing local scorer as an advanced alternative**.

---

## 21. Proposed final architecture

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
          (default)                (optional)
                │                     │
        TypeScript API          jev CLI
                │                     │
          JEV / hosted            llama-server
                                      │
                                  local logits

                           ↓
                  ranked memory hints
                           ↓
                    Pi agent chooses
                           ↓
                     read / ignore
```

The important invariant is:

```text
                  SCORE
                    ↓
                  PRIOR
                    ↓
                 HINT
                    ↓
              AGENT DECIDES
```

not:

```text
SCORE → RETRIEVE → INJECT
```

---

## 22. Implementation order

Status 2026-10-04 — ALL DONE. Published `@daxzeit/pi-jev-vcc-memory-relevance@0.1.0`.

0. ✅ [AM2] Measure the hosted JEV latency distribution (N calls, including
   queue detection) and document the expected skip-during-queue behavior in
   the README. This is a product datum, not an implementation detail — it
   determines what the default backend is *expected* to do. (2026-10-03,
   3 clean windows; README carries the wording.)
1. ✅ Add `package.json` with Pi package metadata. (`0f3d68c` — pack dry-run
   31 files, `pi -e` discovery smoke: tool + skill resolved.)
1b. ✅ [AM7] Port the index builder to dependency-free TypeScript
    (`scripts/build-index.mjs`) so the hosted lane can generate an index
    without Python; `build-index.py` stays as the curation pipeline.
    (`4b37c1e` — byte-parity oracle vs Python: fixture + real dir, 26 entries.)
2. ✅ Introduce the minimal internal `RelevanceScorer` abstraction. (`79dc982`)
3. ✅ Move the current local CLI invocation behind `LocalCliScorer`. (`79dc982`)
4. ✅ Add `JevRemoteScorer` in TypeScript. (`79dc982` — live unit: directional
   oracle 0.8/0.01, 1.3 s, $0.0000202; no-key skip clean.)
5. ✅ Make hosted JEV the default backend. (`2acef3d` — E2E: no-key clean skip,
   `PI_JEV_BACKEND=local` cold start scored. AM10 per-backend noise-floor
   wording shipped in the same commit.)
6. ✅ Add backend/model configuration. (`79dc982` — `PI_JEV_BACKEND`/
   `PI_JEV_MODEL`/`PI_JEV_ENDPOINT` wired.)
7. ✅ Update README installation and privacy sections. (`2acef3d` — §19
   journey, AM2/AM5/AM9/AM11, ADV artifacts preserved, `pi-jev-build-index`
   bin, `prepack` __pycache__ hook.)
8. ✅ Run local regression suite. (Continuous: `tsc --strict` clean, `jev
   selftest` 6/6 with provenance shas, local+hosted E2E after each step.)
9. ✅ Run a clean hosted-mode installation test without Python. (2026-10-04:
   temp `PI_CODING_AGENT_DIR`, `pi install <path>`, `JEV_CMD=/nonexistent`
   (never probed — AM8), `OPENROUTER_API_KEY` only credential → cold start
   scored 25 entries in 2.4 s, $0.000133, hint `custom_message display:false`
   present before the first assistant turn with the hosted noise-floor
   wording. Note: tested from a local path source; the npm source variant
   is step 12.)
10. ✅ `npm pack --dry-run`. (Green — 32 files, zero pyc; CI enforces it on
    the tarball artifact directly, Node 20/22/24.)
11. ✅ Publish the package. (2026-10-04 — `@daxzeit/pi-jev-vcc-memory-relevance@0.1.0`,
    PUT 200, public access; ~4 min registry propagation before the first GET.)
12. ✅ Verify `pi install npm:@daxzeit/pi-jev-vcc-memory-relevance` on a clean environment.
    (2026-10-04: fresh agent dir, real npm source, no Python,
    `JEV_CMD=/nonexistent`, `OPENROUTER_API_KEY` only → cold start hosted
    scored 25 entries in 2.6 s ($0.000133, top-1 jev-typesafe 0.33 on a
    relevance question), session continued normally. Failure matrix spot-
    checks: no key → clean skip; invalid endpoint → clean skip; invalid
    `PI_JEV_BACKEND` → config-error + disabled. Repo public since
    2026-10-04; CI green on the publish commit.)

---

## Amendments — 2026-10-03 (agent review of this draft)

Six amendments were inserted inline, marked `[AM1]`–`[AM6]`:

- **[AM1]** §10 — `keywords: ["pi-package"]` for gallery discoverability; npm scope `@daxzeit` already published.
- **[AM2]** §15 + §22 — queue policy re-sourced after step 0 (2026-10-03): 180–270 s documented 2026-09-28, **not reproduced** across ~2000 subsequent calls (2026-09-30 bench 2×1000 + 2026-10-03 22 calls, all < 1.4 s) ⇒ timeout = tail insurance, skip = expected behavior only if such an event recurs; implementation step 0 (latency-distribution measurement) done.
- **[AM3]** §3 — hard total timeout (`AbortController`) for the hosted transport; per-operation timeouts measured non-binding on this endpoint (15 s budget → 30.5 s response).
- **[AM4]** §18 — the hosted acceptance test is capacity-flaky by construction; non-queue window + clean-skip = pass for the resilience half.
- **[AM5]** §2.1, §7, §8 — quality argument for the hosted default (0.79 vs 0.71, ≈92% decision agreement; local lane = zero-egress alternative with locally controlled scoring — "calibrated" avoided on purpose: measured OOS calibration does not transfer); concrete egress payload (cold start / post-compaction / on-demand, index descriptions always, file contents never); v1 hosted auth = `OPENROUTER_API_KEY` only. *(Luna review 2026-10-03: "quality-maximizing" → "higher-performing measured choice on the reference benchmark"; on-demand egress case added to the §7 contract.)*
- **[AM6]** §19 — the README restructure is a dedicated pass preserving the ADV artifacts; local lane documented as POSIX.

## Amendments — 2026-10-04 (second agent review, claims verified against source)

Six more amendments, marked `[AM7]`–`[AM12]`, plus one unmarked phrasing
fix (§2.3: "fall back … to a remote/custom endpoint" → "to the local
backend or to a custom endpoint" — the original read backwards):

- **[AM7]** §6 + §22 — the hosted lane had no supported way to generate the
  memory index without Python (the only builder is `scripts/build-index.py`):
  the package ships a dependency-free TS builder (`build-index.mjs`); the
  Python script stays as the curation pipeline. Closes the gap between the
  §11 no-Python promise and the §18 clean-install test.
- **[AM8]** §3 — pinned the measured endpoint (`POST
  https://openrouter.ai/api/alpha/decisions`; `/api/v1/*` returns 403/400;
  alpha path = unguaranteed stability), the response field
  (`answers.<id>.noul`), and the rule that hosted mode must not probe or
  require `JEV_CMD`.
- **[AM9]** §2.1 — cost datum for the README (measured: one fanned-out
  request per trigger, ≈ $0.00002–0.0002/call, micro-dollars per session) —
  part of the egress trust contract, like the §7 payload.
- **[AM10]** §13 — the noise-floor wording (0.000–0.001) is local-lane
  calibrated; hosted quantizes probabilities to 2 decimals ⇒ validate and
  adapt the rendered guidance per backend.
- **[AM11]** §5 + §18 — `session_compact` is in pi 1.0's type declarations
  but not the public docs: record the tested pi version; if the hook
  disappears the trigger no-ops silently.
- **[AM12]** §18 — `files` whitelist in `package.json`; the dry-run must
  verify no `__pycache__`/`*.pyc` in the tarball (one exists in the working
  tree today).

Post-script — 2026-10-04 (consolidation pass, advisor subagent
1004-031620-78f9, kimi-for-coding, read-only): the document was re-read
end-to-end as a consolidated whole — no blocking contradiction, all §
cross-references resolve to existing sections, §22 consistent with the
section contents. Three wording fixes applied inline: the §8 auth-store
sentence (last residual contradiction with [AM5]), the [AM7] tense (the
TS builder is future work, step 1b), and the [AM11] cross-reference
(§18 requires the recording, it does not record).

Post-script 2 — 2026-10-04 (external review #2, GPT Luna, HEAD `1257068`,
claims re-verified against source before acting): 3 real defects found and
fixed — (1) `build-index.mjs` used the ES2025 inline regex group `(?s:…)`
(works on Node ≥ 23, SyntaxError on Node ≤ 22 — the advertised floor;
the parity suite had genuinely passed, but only on Node 26): fixed to the
ES2018 `dotAll` constructor flag, parity re-proven on 7 combinations;
(2) `jev/jev` hardcoded the author's GGUF path: now `JEV_GGUF_PATH` env,
`jev check` template check skips gracefully when unset; (3) an invalid
`PI_JEV_BACKEND` silently selected the local backend: now a config-error
log + scoring disabled, never a silent fallback. Plus hardening: score
range guard [0,1] in the normalizer, README cost anchored to the two real
measurements ($0.00002 minimal / $0.00013 25-memory cold start), npx
version-pin note, and a minimal CI workflow (typecheck + builder parity
vs the Python oracle + pack hygiene, Node 20/22/24) — the regex bug is
exactly what it would have caught.
