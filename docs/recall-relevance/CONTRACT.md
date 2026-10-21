# Offline recall relevance — source contract (prototype)

2026-10-21. User authorized prototype blocks 0–3 only. No production hook,
provider fallback, package activation, server change or publication.

## Verified dependencies

- Pi managed install 1.3.0: `dist/core/session-manager.js`, exports
  `buildSessionProjection` / `buildContextEntries`.
- pi-vcc 0.10.2: `src/core/global-indices.ts`, `buildGlobalIndexById`;
  `load-messages.ts`, `lineage.ts`, `recall-run.ts`, `content.ts`,
  `render-entries.ts`, `hooks/before-compact.ts`.
- Source contract independently reviewed in mission `1021-025301-8aac`.
  Parent replayed its synthetic Node assertions, exit 0:
  `OK projection/cuts/global/cutoff`,
  `OK installed recall/originals/paired-4000/full-result`,
  `OK raw/typed-selection/escaping/exact-roundtrip`.

The bridge imports the installed canonical functions; it does not duplicate
projection/cut selection in a second language. It reads only through an explicit
compaction ID and explicitly supplies both leaves. It fails closed for malformed
JSON, duplicate IDs, missing parents, cycles, absent cutoff and unsupported VCC.
No SessionManager is opened, migrated or saved. TypeScript is loaded by jiti with
filesystem cache disabled. Package paths/versions are recorded in every snapshot.

## Projection and index space

Global #N starts at 0 and counts every non-null persisted `message`, including
system messages and abandoned branches, in file order. Filtering never renumbers.
Only the active ancestry ending at the selected compaction is eligible.

Candidates are the nonempty contributions present in canonical projection at
`compaction.parentId` but absent at `compaction.id`, in pre-compaction order.
This includes the previous compaction's retained tail, but not previously
compacted-away messages. **firstKeptEntryId is excluded from the removed zone.**
Both retain-none sentinels (`""` from VCC and compaction's own ID from Pi) work
through canonical projection. The local vcc-digest's inclusive range is not used.

Context edits: latest selected branch-relative edit wins; null omits; replacement
is projected. Raw recall does not apply these edits. Source locators therefore
record the winning edit ID/hash where applicable, not merely the original #N.
System messages and context contributions without #N are excluded and counted;
this first prototype does not attempt to add new recall index semantics.

Current context is the compaction summary and retained conversational text at the
cutoff. It excludes system loadouts, opaque signatures and media. It is a
**textual persisted-context view**, not a claim to reconstruct transient extension
transformations, tool definitions or provider-level replay state. No future user
message is appended as an invented active request.

## Exact consultation

Recall `expand` renders text blocks, not thoughts. It summarizes tool arguments;
a paired result is clipped at 4000 chars unless explicitly expanded on its own.
Thoughts, full arguments and projected replacements are consequently consulted by
a **separate, explicit private-manifest `consult` command**. It is not described
as a hidden capability of `vcc_recall`.

Each admissible source part stores exact text and SHA256; each candidate stores
entry ID, #N, type, part index, source interval, provenance and text hash. Intervals
are **Python Unicode code point offsets**, not JS UTF-16 offsets or byte offsets.
Tool argument text is the bridge's JSON serialization of the arguments object.
The consult command verifies hashes and emits selected source spans with literal
angle escapes; original content is recoverable from the private manifest.
Opaque/redacted signatures and media payloads are not copied into candidate text.

## Segmentation

No importance classifier, keyword filter or generated candidate summary. Split
at message/part boundaries and paragraphs where possible; large paragraphs have
a deterministic Unicode-safe fallback. Small call/result pairs stay together.
Bounded overlap preserves surrounding text; it never replaces new source text.

Sizes 1K/2K/4K are **initial estimates**: ceil(escaped UTF-8 bytes / 3), including
headers. They are not tokenizer counts. Scoring must count the final rendered
prompt exactly before inference and reject an over-budget prompt. Every
admissible source character is covered; exclusions and overlap are counted.

## Scoring contract

Fixed question:

> Would consulting this earlier conversation block via recall help continue the current task correctly from the current context?

The existing CLI selects v1 / shared_examples_binary and converts noul into
ordered no/yes choices. Scores are its FP32 two-label softmax, not the base
builder's nine-level noul rating. Both labels must be present in top-20; absence
is an error, never a fabricated probability. New relevance scores are not
calibrated utilities or truth probabilities. Existing noise floors do not transfer.

Only loopback-literal endpoints; no redirects or hosted fallback. Common global
deadline; explicit complete/partial/failed and unscored coverage. No raw prompts
or source text in score results. Corpus/snapshot files are private, never committed.

## Block 0/1 verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p test_recall_extraction.py -v
```

Initial result: 10 tests passed. Tests use installed projection/global-index code
and manually written expectations for branch holes, exclusive boundaries,
replacement/omission, two compactions, both compact-all sentinels, future cutoff,
duplicate refusal, thought/argument exact consultation, Unicode segmentation,
coverage, deterministic output, tamper rejection and small call/result pairing.
An independent per-character mask checks coverage, not just production aggregates.

No live retrieval quality, cache efficiency or end-to-end production behavior is
claimed by these CPU tests. Those are subsequent prototype measurements.
