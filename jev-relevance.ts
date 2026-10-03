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
 * Requires: a scorer CLI implementing the `ask` contract
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
// Scorer CLI. JEV_CMD is a TRUSTED-EXECUTABLE boundary: it is invoked
// directly (no shell) but the configured path is fully trusted — it receives
// the scoring state (a snapshot of the current context) as a file argument
// and runs with the user's privileges. Resolved and validated once at load.
const JEV = process.env.JEV_CMD ?? path.join(os.homedir(), ".local", "bin", "jev");
const jevUsable = (() => {
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

const runJev = (state: string, mems: MemoryEntry[], framing: Framing, timeoutS: number): Promise<{ rows: Array<{ id: string; path: string; score: number }>; wallS: number; anchor?: unknown }> =>
  new Promise((resolve, reject) => {
    const t0 = Date.now();
    if (!jevUsable.ok) {
      reject(new Error(`scorer unavailable: ${jevUsable.why}`));
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
      ["ask", "--state", STATE_F, "--questions", QUESTIONS_F, "--timeout", String(timeoutS)],
      { timeout: (timeoutS + 30) * 1000, maxBuffer: 16 * 1024 * 1024 }, // hard net
      (err, stdout) => {
        const wallS = (Date.now() - t0) / 1000;
        if (err) {
          reject(new Error(`jev ask: exit=${(err as { code?: number })?.code ?? "?"} ${String(err.message).slice(0, 200)}`));
          return;
        }
        try {
          const d = JSON.parse(stdout);
          if (d?.ok !== true) throw new Error(`ok!=true: ${JSON.stringify(d).slice(0, 200)}`);
          const rows: Array<{ id: string; path: string; score: number }> = [];
          const byId = new Map(mems.map((m) => [m.id, m]));
          // The CLI returns `results` as a dict {qid: {...}} — not an array of pairs.
          for (const [qid, r] of Object.entries(d.results ?? {}) as Array<[string, { probs?: { yes?: number } }]>) {
            const yes = r?.probs?.yes;
            const m = byId.get(qid);
            if (typeof yes === "number" && m) rows.push({ id: m.id, path: m.path, score: yes });
          }
          rows.sort((a, b) => b.score - a.score);
          resolve({ rows, wallS, anchor: d.anchor });
        } catch (e) {
          reject(e instanceof Error ? e : new Error(String(e)));
        }
      },
    );
  });

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
  const lines = rows.slice(0, limit).map((r) => `${r.id}.md: ${r.score.toFixed(3)}`);
  return (
    `<memory_relevance>\n` +
    `Potentially useful memories for the current task (local relevance scores, informational only — 0.000-0.001 is the noise floor, skip those; anything above is a hint you can verify by reading, weighted by score, not a verdict):\n` +
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
      if (!existsSync(JEV) || !existsSync(INDEX)) {
        throw new Error(`missing: ${!existsSync(JEV) ? JEV + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
      }
      const mems = loadIndex();
      const { rows, wallS, anchor } = await runJev(
        prompt.slice(0, COLD_STATE_MAX_CHARS), mems, "cold", COLD_TIMEOUT_S,
      );
      if (rows.length === 0) throw new Error("0 scored rows");
      log({
        ok: true,
        trigger: "cold-start",
        wall_s: Number(wallS.toFixed(1)),
        n: rows.length,
        anchor: anchor ?? null,
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
        if (!existsSync(JEV) || !existsSync(INDEX)) {
          throw new Error(`missing: ${!existsSync(JEV) ? JEV + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
        }
        const mems = loadIndex();
        const { rows, wallS, anchor } = await runJev(summary, mems, "vcc", JEV_TIMEOUT_S);
        if (rows.length === 0) throw new Error("0 scored rows");
        deliver(pi, rows);
        log({
          ok: true,
          wall_s: Number(wallS.toFixed(1)),
          n: rows.length,
          anchor: anchor ?? null,
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
      "Score your long-term memory files (e.g. ~/.pi/agent/memory/*.md) for relevance to a question YOU formulate, via a logit-level scorer (~15-35 s; no egress with a local scorer endpoint). " +
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
        if (!existsSync(JEV) || !existsSync(INDEX)) {
          throw new Error(`missing: ${!existsSync(JEV) ? JEV + " " : ""}${!existsSync(INDEX) ? INDEX : ""}`);
        }
        const mems = loadIndex();
        const { rows, wallS, anchor } = await runJev(question, mems, "cold", COLD_TIMEOUT_S);
        if (rows.length === 0) throw new Error("0 scored rows");
        log({
          ok: true,
          trigger: "query",
          wall_s: Number(wallS.toFixed(1)),
          n: rows.length,
          anchor: anchor ?? null,
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
