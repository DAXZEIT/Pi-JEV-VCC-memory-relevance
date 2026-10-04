/**
 * jev-relevance — logit-scored memory relevance for the pi coding agent.
 *
 * Scores every entry of a long-term memory index (~25 markdown files) for
 * relevance to the current moment, using a calibrated yes/no classifier that
 * reads NATIVE LOGITS (P("Yes")) instead of embeddings. The scores are
 * delivered to the agent as a graduated ranking — a prior to verify by
 * reading, never an injection. The agent alone decides what to read.
 *
 * Three triggers — two automatic pushes + one on-demand pull:
 *
 * 1. COLD START — hook `before_agent_start` (after the user prompt, BEFORE
 *    the first LLM call): fires exactly once per session on the first real
 *    user message (prompts < 30 chars or slash-commands ignored). Runs
 *    SYNCHRONOUSLY (~15-35 s measured, hard budget 45 s) so the very first prefill
 *    already contains the hint. Result = custom message `memory_relevance`
 *    (top-10, display: false → invisible in the TUI, present in the model
 *    context and the session jsonl).
 *
 * 2. POST-COMPACTION — hook `session_compact` (fired by pi's compactor —
 *    in pi 1.0.0 this hook is in the type declarations but not covered by
 *    the public docs; only `session_compact_failed` is documented):
 *    the post-compaction summary IS the state to score against. Runs
 *    SYNCHRONOUSLY-BLOCKING: pi awaits this emit before resuming the agent,
 *    so the handler delays resumption by ~15-35 s and queues the hint
 *    BEFORE returning — it is drained into the FIRST post-compaction prefill,
 *    exactly like the cold start. TUI shows a status line while waiting.
 *    Skips non-pi-vcc compactions, empty summaries and retries.
 *
 * 3. ON-DEMAND — tool call `memory_relevance` (query-driven, pull): the
 *    agent formulates its own question mid-task and receives the FULL
 *    ranking (every row) as the tool result. The tool description carries
 *    the cost contract ("SLOW — at most once per task"). Failure = failed
 *    tool result → the agent falls back to its static index. Catches what a
 *    state-driven trigger cannot see mid-task; the two pushes stay necessary
 *    (the agent does not know what it is missing at startup).
 *
 * Common: state = text (first 2000 chars of the user prompt / the
 * compaction summary), one noul question per memory (frozen protocol
 * framings per trigger — do not reword without re-measuring; the wording
 * IS the metric), failure = skip + log, never a throw into the session.
 *
 * Backends (spec §2/§4) — one RelevanceScorer interface, two implementations:
 *   jev (DEFAULT): the hosted Decisions API, model pinned via PI_JEV_MODEL
 *     (default typesafe/jev-1.13), OPENROUTER_API_KEY only.
 *   local (PI_JEV_BACKEND=local): `jev ask` CLI on the loopback logit
 *     server — zero egress. JEV_CMD is probed only when this backend runs.
 *
 * Requires: a scorer CLI implementing the `ask` contract for the local lane
 * (see docs/scorer-contract.md) and a memory index JSON
 * (see examples/memory_index.example.json + scripts/build-index.py).
 */
import { execFile } from "node:child_process";
import {
  accessSync,
  appendFileSync,
  constants,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

type PiOn = { on?: (event: string, handler: (event?: any, ctx?: any) => unknown) => void };

// Configuration. Override via environment; defaults match the conventional
// pi agent dir layout (~/.pi/agent) and a scorer named `jev` on PATH.
// Scorer CLI (local backend). JEV_CMD is a TRUSTED-EXECUTABLE boundary: it is
// invoked directly (no shell) but the configured path is fully trusted — it
// receives the scoring state (a snapshot of the current context) as a file
// argument and runs with the user's privileges. Probed only when the local
// backend is selected (spec AM8: hosted mode must not require or probe it).
const JEV = process.env.JEV_CMD ?? path.join(os.homedir(), ".local", "bin", "jev");
// Backend selection (spec §2.3/§9). Public default: hosted JEV (§22 step 5) —
// zero-dependency install; the local lane is the advanced zero-egress option.
const BACKEND = (process.env.PI_JEV_BACKEND ?? "jev").toLowerCase();
const JEV_MODEL = process.env.PI_JEV_MODEL ?? "typesafe/jev-1.13"; // pinned, never a floating alias (spec §14)
const JEV_ENDPOINT = process.env.PI_JEV_ENDPOINT ?? "https://openrouter.ai/api/alpha/decisions"; // alpha path (spec AM8)
const INDEX = process.env.JEV_MEMORY_INDEX ?? path.join(os.homedir(), ".pi", "agent", "memory_index.json");
const DIR = path.join(os.homedir(), ".cache", "jev-relevance");
const LOG = path.join(DIR, "log.jsonl");
const STATE_F = path.join(DIR, "state.json");
const QUESTIONS_F = path.join(DIR, "questions.json");
const JEV_TIMEOUT_S = 120; // scorer budget post-compaction (25 branches ≈ 15-20 s with shared-prefix anchor)
const COLD_TIMEOUT_S = 45; // scorer budget at cold start — the user is waiting for a first token
const COLD_MIN_PROMPT_CHARS = 30; // shorter = no signal (slash-commands included)
const COLD_STATE_MAX_CHARS = 2000; // state = prompt head (the anchor carries the rest)

let busy = false;

const log = (entry: Record<string, unknown>): void => {
  try {
    if (!existsSync(DIR)) mkdirSync(DIR, { recursive: true });
    appendFileSync(LOG, JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n");
  } catch {
    /* logging must never kill anything */
  }
};

interface MemoryEntry {
  id: string;
  path: string;
  desc_en: string;
}

const loadIndex = (): MemoryEntry[] => {
  const raw = JSON.parse(readFileSync(INDEX, "utf-8"));
  const mems = (raw?.memories ?? raw) as MemoryEntry[];
  if (!Array.isArray(mems) || mems.length === 0) throw new Error("empty index");
  return mems;
};

// Two protocol framings (fidelity: do not reword between runs without
// re-measuring — each wording carries its own metric).
const FRAMINGS = {
  vcc: {
    instructions: (id: string, desc: string): string =>
      `Given the current session state, would consulting the memory block '${id}' (${desc}) be useful to continue the current task correctly?`,
    true: "The state describes active work, open decisions, or pending items that this memory block directly covers.",
    false: "This memory block covers a different, inactive domain that the state does not touch.",
  },
  cold: {
    instructions: (id: string, desc: string): string =>
      `Given the user's request, would consulting the memory block '${id}' (${desc}) be useful to handle it correctly?`,
    true: "The request touches active work, open decisions, or pending items that this memory block directly covers.",
    false: "This memory block covers a different, inactive domain that the request does not touch.",
  },
} as const;
type Framing = keyof typeof FRAMINGS;

const buildQuestions = (mems: MemoryEntry[], framing: Framing): Record<string, unknown> => {
  const f = FRAMINGS[framing];
  const qs: Record<string, unknown> = {};
  for (const m of mems) {
    qs[m.id] = {
      type: "noul",
      instructions: f.instructions(m.id, m.desc_en ?? ""),
      criteria: { true: f.true, false: f.false },
    };
  }
  return qs;
};

// Scorer abstraction (spec §4) — deliberately minimal: one interface, two
// backends. The triggers below consume only RelevanceResult and never care
// which backend produced the scores.
interface ScoreOptions {
  timeoutS: number;
}
interface RelevanceResult {
  rows: Array<{ id: string; path: string; score: number }>;
  wallS: number;
  anchor?: unknown; // local shared-prefix KV anchor stats (hosted: undefined)
  cost?: number | null; // hosted usage cost when reported (local: undefined)
}
interface RelevanceScorer {
  readonly backend: "local" | "jev";
  usable(): { ok: boolean; why: string };
  score(state: string, mems: MemoryEntry[], framing: Framing, opts: ScoreOptions): Promise<RelevanceResult>;
}

const sortRows = (mems: MemoryEntry[], yesById: Map<string, number>): Array<{ id: string; path: string; score: number }> => {
  const byId = new Map(mems.map((m) => [m.id, m]));
  const rows: Array<{ id: string; path: string; score: number }> = [];
  for (const [qid, yes] of yesById) {
    const m = byId.get(qid);
    if (typeof yes === "number" && Number.isFinite(yes) && yes >= 0 && yes <= 1 && m) rows.push({ id: m.id, path: m.path, score: yes }); // invalid score (incl. outside [0,1]) → skipped (spec §15)
  }
  rows.sort((a, b) => b.score - a.score);
  return rows;
};

// LOCAL — the reference scorer: `jev ask` CLI on the loopback logit server.
export class LocalCliScorer implements RelevanceScorer {
  readonly backend = "local" as const;
  private readonly readiness = (() => {
    try {
      if (!statSync(JEV).isFile()) return { ok: false, why: `not a regular file: ${JEV}` };
      accessSync(JEV, constants.X_OK);
      return { ok: true, why: "" };
    } catch {
      return {
        ok: false,
        why: process.env.JEV_CMD
          ? `JEV_CMD not usable (${JEV}) — it must be a regular executable file`
          : `scorer not found: ${JEV} — install the jev skill or set JEV_CMD`,
      };
    }
  })();

  usable(): { ok: boolean; why: string } {
    return this.readiness;
  }

  score(state: string, mems: MemoryEntry[], framing: Framing, opts: ScoreOptions): Promise<RelevanceResult> {
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      if (!this.readiness.ok) {
        reject(new Error(`scorer unavailable: ${this.readiness.why}`));
        return;
      }
      try {
        writeFileSync(STATE_F, JSON.stringify(state));
        writeFileSync(QUESTIONS_F, JSON.stringify(buildQuestions(mems, framing), null, 1));
      } catch (e) {
        reject(e instanceof Error ? e : new Error(String(e)));
        return;
      }
      execFile(
        JEV,
        ["ask", "--state", STATE_F, "--questions", QUESTIONS_F, "--timeout", String(opts.timeoutS)],
        { timeout: (opts.timeoutS + 30) * 1000, maxBuffer: 16 * 1024 * 1024 }, // hard net
        (err, stdout) => {
          const wallS = (Date.now() - t0) / 1000;
          if (err) {
            reject(new Error(`jev ask: exit=${(err as { code?: number })?.code ?? "?"} ${String(err.message).slice(0, 200)}`));
            return;
          }
          try {
            const d = JSON.parse(stdout);
            if (d?.ok !== true) throw new Error(`ok!=true: ${JSON.stringify(d).slice(0, 200)}`);
            // The CLI returns `results` as a dict {qid: {...}} — not an array of pairs.
            const yesById = new Map<string, number>();
            for (const [qid, r] of Object.entries(d.results ?? {}) as Array<[string, { probs?: { yes?: number } }]>) {
              const yes = r?.probs?.yes;
              if (typeof yes === "number") yesById.set(qid, yes);
            }
            resolve({ rows: sortRows(mems, yesById), wallS, anchor: d.anchor });
          } catch (e) {
            reject(e instanceof Error ? e : new Error(String(e)));
          }
        },
      );
    });
  }
}

// HOSTED JEV — the real Decisions API (spec §3; becomes the default at §22
// step 5). Transport facts (spec AM8/AM3, measured): POST {model, state,
// questions} to the OpenRouter ALPHA decisions endpoint (/api/v1/* 403s),
// Bearer = OPENROUTER_API_KEY (only — no auth-store fallback in v1, spec §8),
// answers.<qid>.noul = P(Yes). HARD TOTAL timeout via AbortSignal —
// per-socket timeouts were measured non-binding on this endpoint.
export class JevRemoteScorer implements RelevanceScorer {
  readonly backend = "jev" as const;

  usable(): { ok: boolean; why: string } {
    const key = process.env.OPENROUTER_API_KEY;
    return key && key.trim()
      ? { ok: true, why: "" }
      : { ok: false, why: "OPENROUTER_API_KEY not set — the hosted JEV backend needs it (or set PI_JEV_BACKEND=local)" };
  }

  async score(state: string, mems: MemoryEntry[], framing: Framing, opts: ScoreOptions): Promise<RelevanceResult> {
    const u = this.usable();
    if (!u.ok) throw new Error(`scorer unavailable: ${u.why}`);
    const t0 = Date.now();
    let resp: Awaited<ReturnType<typeof fetch>>;
    try {
      resp = await fetch(JEV_ENDPOINT, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`,
        },
        body: JSON.stringify({ model: JEV_MODEL, state, questions: buildQuestions(mems, framing) }),
        signal: AbortSignal.timeout(opts.timeoutS * 1000), // hard TOTAL budget (queue = skip, never a retry loop)
      });
    } catch (e) {
      throw new Error(`jev remote: ${String(e instanceof Error ? e.message : e).slice(0, 200)}`);
    }
    const wallS = (Date.now() - t0) / 1000;
    const text = await resp.text();
    if (resp.status !== 200) throw new Error(`jev remote HTTP ${resp.status}: ${text.slice(0, 200)}`);
    const d = JSON.parse(text);
    const yesById = new Map<string, number>();
    const answers = (d?.answers ?? {}) as Record<string, { noul?: unknown }>;
    for (const m of mems) {
      const noul = answers[m.id]?.noul;
      if (typeof noul === "number") yesById.set(m.id, noul);
    }
    const usage = (d?.usage ?? {}) as { cost?: unknown };
    const cost = typeof usage.cost === "number" ? usage.cost : typeof d?.cost === "number" ? d.cost : null;
    return { rows: sortRows(mems, yesById), wallS, cost };
  }
}

const scorer: RelevanceScorer | null =
  BACKEND === "jev" ? new JevRemoteScorer() : BACKEND === "local" ? new LocalCliScorer() : null;
if (!scorer) {
  // Invalid config must NEVER silently select a backend (a typo falling back
  // to local would execute JEV_CMD; falling back to hosted would egress).
  log({
    event: "config-error",
    var: "PI_JEV_BACKEND",
    value: process.env.PI_JEV_BACKEND ?? null,
    expected: "jev|local",
    effect: "relevance scoring disabled",
  });
}
const scorerUnusable = { ok: false, why: 'invalid PI_JEV_BACKEND (expected "jev" or "local") — relevance scoring disabled' };
const scorerBackend = (): "local" | "jev" | "disabled" => scorer?.backend ?? "disabled";

const deliver = (pi: unknown, rows: Array<{ id: string; score: number }>): void => {
  const piAny = pi as {
    sendMessage?: (
      m: { customType: string; content: string; display: boolean },
      o?: { deliverAs?: "steer" | "followUp" | "nextTurn" },
    ) => unknown;
  };
  if (typeof piAny.sendMessage !== "function") {
    throw new Error("pi.sendMessage unavailable");
  }
  // Same custom message as cold start (top-10, display: false). Called while
  // the awaited `session_compact` emit is in flight: the entry is
  // queued/appended BEFORE pi hands back to the runner. steer (agent
  // mid-turn) = queue drained by the steering poll before the first
  // post-compaction response; idle (manual compact outside a run) = direct
  // append, in context at the next turn.
  piAny.sendMessage(
    { customType: "memory_relevance", content: buildRelevanceBlock(rows), display: false },
    { deliverAs: "steer" },
  );
};

const buildRelevanceBlock = (rows: Array<{ id: string; score: number }>, limit = 10): string => {
  // Graduated scores (not a binary) — the agent calibrates where to cut.
  // Push (cold start/post-compaction) = top-10 by default: what matters is
  // not the raw score but the distance to the ~0.001 noise floor — even a
  // 0.12 is a hint. The tool returns the FULL ranking (limit = rows.length)
  // because an enumerative query must not lose anything to an arbitrary cap.
  // The framing line encodes the reading hygiene: ~0.000-0.001 = noise
  // floor, skip; above = a hint you can verify by reading, weighted by
  // score, not a verdict — a mid-gradient cliff is NOT a truth boundary
  // (a 0.06 file can be exactly the right pointer in a broader sweep, and
  // calibration shows the score is a reliable prior, never an absolute).
  // Scores stay informational; the agent alone decides what to read
  // (lazy retrieval — nothing is injected). This is RENDER, not protocol:
  // rewording it breaks no metric (the frozen protocol wordings are the JEV
  // questions in FRAMINGS, not this line).
  // Noise-floor wording is per-backend (spec AM10): local raw logits show a
  // ~0.000-0.001 floor; hosted JEV quantizes scores to 2 decimals (floor ≈ 0.00-0.01).
  const backend = scorerBackend();
  const floor = backend === "jev" ? "0.00-0.01" : backend === "local" ? "0.000-0.001" : "n/a";
  const scorerName = backend === "jev" ? "hosted JEV" : backend === "local" ? "local" : "disabled";
  const lines = rows.slice(0, limit).map((r) => `${r.id}.md: ${r.score.toFixed(3)}`);
  return (
    `<memory_relevance>\n` +
    `Potentially useful memories for the current task (relevance scores from the ${scorerName} scorer, informational only — ${floor} is the noise floor, skip those; anything above is a hint you can verify by reading, weighted by score, not a verdict):\n` +
    lines.join("\n") +
    `\n</memory_relevance>`
  );
};

export default (pi: unknown): void => {
  log({ event: "loaded" });

  // COLD START — first user message of the session, before the first LLM call.
  // SYNCHRONOUS by design: the initial prefill must contain the hint.
  // Failure = silence, never a block.
  const coldDone = new Set<string>();
  (pi as PiOn).on?.("before_agent_start", async (event: any, ctx: any) => {
    const prompt: string = event?.prompt ?? "";
    if (prompt.length < COLD_MIN_PROMPT_CHARS || prompt.startsWith("/")) return;
    let sessionFile = "unknown";
    try {
      const sm = ctx?.sessionManager;
      if (!sm?.getEntries) return;
      const hasAssistant = (sm.getEntries() ?? []).some(
        (e: any) => e?.type === "message" && e?.message?.role === "assistant",
      );
      if (hasAssistant) return; // not a cold start (session already engaged / resumed)
      sessionFile = sm.getSessionFile?.() ?? "unknown";
    } catch {
      return; // no usable ctx = no cold start
    }
    if (coldDone.has(sessionFile)) return;
    if (busy) {
      log({ skip: "cold-busy" });
      return;
    }
    coldDone.add(sessionFile);
    busy = true;
    log({ start: true, trigger: "cold-start", prompt_chars: prompt.length });
    try {
      const ready = scorer?.usable() ?? scorerUnusable;
      if (!ready.ok || !existsSync(INDEX)) {
        throw new Error(`missing: ${!ready.ok ? ready.why + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
      }
      const mems = loadIndex();
      const { rows, wallS, anchor, cost } = await scorer!.score(
        prompt.slice(0, COLD_STATE_MAX_CHARS), mems, "cold", { timeoutS: COLD_TIMEOUT_S },
      );
      if (rows.length === 0) throw new Error("0 scored rows");
      log({
        ok: true,
        trigger: "cold-start",
        backend: scorer!.backend,
        wall_s: Number(wallS.toFixed(1)),
        n: rows.length,
        anchor: anchor ?? null,
        cost: cost ?? null,
        top5: rows.slice(0, 5).map((r) => `${r.id}:${r.score.toFixed(2)}`),
      });
      return {
        message: { customType: "memory_relevance", content: buildRelevanceBlock(rows), display: false },
      };
    } catch (e) {
      log({ ok: false, trigger: "cold-start", error: String(e instanceof Error ? e.message : e).slice(0, 300) });
      return; // the turn starts without the hint
    } finally {
      busy = false;
    }
  });

  // POST-COMPACTION — SYNCHRONOUS-BLOCKING by design: pi awaits this emit
  // before resuming the agent, so the handler delays resumption by ~15-20 s
  // and deliver() queues the hint before returning (drained by the steering
  // poll → first post-compaction prefill, like cold start). The TUI is not
  // silent while waiting: setStatus. Failure = skip + log, the agent resumes
  // without the hint.
  (pi as PiOn).on?.("session_compact", async (event: any, ctx: any) => {
    const status = (text: string | undefined): void => {
      try {
        ctx?.ui?.setStatus?.("jev-relevance", text);
      } catch {
        /* status must never kill the scoring */
      }
    };
    try {
      const c = event?.compactionEntry;
      const compactor = c?.details?.compactor;
      const summary = typeof c?.summary === "string" ? c.summary : "";
      if (compactor !== "pi-vcc") {
        log({ skip: "not-pi-vcc", compactor: compactor ?? null });
        return;
      }
      if (!summary) {
        log({ skip: "no-summary" });
        return;
      }
      if (event?.willRetry === true) {
        log({ skip: "willRetry" });
        return;
      }
      if (busy) {
        log({ skip: "busy" });
        return;
      }
      busy = true;
      status("jev-relevance: scoring memories… (~15 s)");
      log({ start: true, summary_chars: summary.length });
      try {
        const ready = scorer?.usable() ?? scorerUnusable;
        if (!ready.ok || !existsSync(INDEX)) {
          throw new Error(`missing: ${!ready.ok ? ready.why + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
        }
        const mems = loadIndex();
        const { rows, wallS, anchor, cost } = await scorer!.score(summary, mems, "vcc", { timeoutS: JEV_TIMEOUT_S });
        if (rows.length === 0) throw new Error("0 scored rows");
        deliver(pi, rows);
        log({
          ok: true,
          backend: scorer!.backend,
          wall_s: Number(wallS.toFixed(1)),
          n: rows.length,
          anchor: anchor ?? null,
          cost: cost ?? null,
          top5: rows.slice(0, 5).map((r) => `${r.id}:${r.score.toFixed(2)}`),
        });
      } finally {
        busy = false;
        status(undefined);
      }
    } catch (e) {
      status(undefined);
      log({ ok: false, error: String(e instanceof Error ? e.message : e).slice(0, 300) });
    }
  });

  // ON-DEMAND — `memory_relevance` tool (query-driven, pull). See header §3.
  const piTools = pi as { registerTool?: (t: Record<string, unknown>) => void };
  piTools.registerTool?.({
    name: "memory_relevance",
    label: "Memory relevance",
    description:
      `Score your long-term memory files (e.g. ~/.pi/agent/memory/*.md) for relevance to a question YOU formulate, via a logit-level scorer (~15-35 s local, ~1 s hosted; ${scorerBackend() === "jev" ? "the scoring state is sent to the hosted JEV service" : scorerBackend() === "local" ? "no third-party egress with a local scorer endpoint" : "scoring is disabled because PI_JEV_BACKEND is invalid"}). ` +
      "Returns the FULL ranking of all memory files (sorted, graduated scores — the gradient shows where to cut). SLOW — call at most once per task, only when you suspect a memory file holds needed context and your static index lines don't tell you which. " +
      "Scores are informational only: afterwards, read whichever file(s) you choose with the read tool — nothing is injected.",
    parameters: {
      type: "object",
      properties: {
        question: {
          type: "string",
          description:
            'What to score the memories against — phrase what you are looking for, e.g. "anything in memory about systemd drop-ins on this machine".',
        },
      },
      required: ["question"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { question?: string }) {
      const question = (params?.question ?? "").trim();
      if (!question) throw new Error("memory_relevance: empty question");
      if (busy) throw new Error("memory_relevance: scoring already running (busy)");
      busy = true;
      log({ start: true, trigger: "query", question_chars: question.length });
      try {
        const ready = scorer?.usable() ?? scorerUnusable;
        if (!ready.ok || !existsSync(INDEX)) {
          throw new Error(`missing: ${!ready.ok ? ready.why + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
        }
        const mems = loadIndex();
        const { rows, wallS, anchor, cost } = await scorer!.score(question, mems, "cold", { timeoutS: COLD_TIMEOUT_S });
        if (rows.length === 0) throw new Error("0 scored rows");
        log({
          ok: true,
          trigger: "query",
          backend: scorer!.backend,
          wall_s: Number(wallS.toFixed(1)),
          n: rows.length,
          anchor: anchor ?? null,
          cost: cost ?? null,
          top5: rows.slice(0, 5).map((r) => `${r.id}:${r.score.toFixed(2)}`),
        });
        return {
          content: [{ type: "text", text: buildRelevanceBlock(rows, rows.length) }],
          details: { wall_s: Number(wallS.toFixed(1)), n: rows.length },
        };
      } catch (e) {
        log({ ok: false, trigger: "query", error: String(e instanceof Error ? e.message : e).slice(0, 300) });
        throw e; // failed tool result — the agent falls back to its static index
      } finally {
        busy = false;
      }
    },
  });
};
