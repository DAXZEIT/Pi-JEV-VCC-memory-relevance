#!/usr/bin/env python3
"""Reference scorer for jev-relevance — implements the `ask` CLI contract
(see docs/scorer-contract.md) against ANY OpenAI-compatible endpoint that
exposes completion logprobs (llama-server, vLLM, …).

Score = P("Yes") normalized over {"Yes", "No"} from the FIRST generated
token's logprobs. One completion per question; questions run in parallel
(adjust --workers to your server's slot count).

Usage:
  scorer-openai.py ask --state state.json --questions questions.json \
      --endpoint http://127.0.0.1:8080/v1/completions --model my-model \
      --workers 8 [--timeout 120]

Env: OPENAI_API_KEY / JEV_API_KEY for authenticated endpoints (optional).
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

YES_VARIANTS = ("Yes", "yes", "YES")
NO_VARIANTS = ("No", "no", "NO")


def build_prompt(state: str, instructions: str, criteria: dict) -> str:
    crit_yes = criteria.get("true", "it is relevant.")
    crit_no = criteria.get("false", "it is not relevant.")
    return (
        f"<session-state>\n{state}\n</session-state>\n\n"
        f"{instructions}\n\n"
        f"Calibration criteria — answer Yes only if: {crit_yes}\n"
        f"Answer No if: {crit_no}\n\n"
        "Answer with a single word: Yes or No.\nAnswer:"
    )


def score_one(endpoint: str, model: str, api_key: str | None, prompt: str,
              timeout: int) -> float:
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": 1,
        "temperature": 0.0,
        "logprobs": 5,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    choices = d.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    lp = choices[0].get("logprobs") or {}
    toks = lp.get("tokens") or []
    lps = lp.get("token_logprobs") or []
    if not toks or not lps or lps[0] is None:
        raise RuntimeError(f"no logprobs: {json.dumps(lp)[:200]}")
    top = (lp.get("top_logprobs") or [{}])[0]
    # P over case variants: take the best logprob per polarity.
    p_yes = max([l for t, l in top.items() if t in YES_VARIANTS], default=None)
    p_no = max([l for t, l in top.items() if t in NO_VARIANTS], default=None)
    if p_yes is None or p_no is None:
        # fall back to the sampled token itself.
        # RECOVERY HEURISTIC, NOT CALIBRATION: the fake opposite logprob of -20.0
        # saturates the probability to ~1.0/~0.0 and must not be read as a
        # calibrated value — a proper scorer exposes both polarity logits in top_logprobs.
        if toks[0] in YES_VARIANTS:
            p_yes, p_no = lps[0], -20.0
        elif toks[0] in NO_VARIANTS:
            p_yes, p_no = -20.0, lps[0]
        else:
            raise RuntimeError(f"polarity token not found: {toks[0]!r}")
    my, mn = math.exp(p_yes), math.exp(p_no)
    return my / (my + mn)


def cmd_ask(args) -> int:
    state = json.loads(open(args.state).read())
    questions = json.loads(open(args.questions).read())
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("OPENAI_API_KEY")
    t0 = time.time()
    results: dict = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(score_one, args.endpoint, args.model, api_key,
                      build_prompt(state, q.get("instructions", ""),
                                   q.get("criteria", {})), args.timeout): qid
            for qid, q in questions.items()
        }
        for fut, qid in futs.items():
            try:
                results[qid] = {"probs": {"yes": round(fut.result(), 4)}}
            except Exception as e:  # per-question failure ≠ batch failure
                results[qid] = {"error": str(e)[:200]}
    print(json.dumps({"ok": True, "results": results, "anchor": None,
                      "wall_s": round(time.time() - t0, 2)}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("--state", required=True)
    a.add_argument("--questions", required=True)
    a.add_argument("--timeout", type=int, default=120)
    a.add_argument("--endpoint", default=os.environ.get("JEV_ENDPOINT",
                    "http://127.0.0.1:8080/v1/completions"))
    a.add_argument("--model", default=os.environ.get("JEV_MODEL", "local"))
    a.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    if args.cmd == "ask":
        return cmd_ask(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
