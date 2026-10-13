#!/usr/bin/env python3
"""Build a memory index JSON for jev-relevance.

Two derivation modes:

  glob mode (default) — entries = every .md file under --dir (minus
  --exclude). For setups without a curated static index: the whole
  directory is the channel.

  gate mode (--gate) — entries = the `memory/<slug>.md` references in
  the `### Index` section of --index-file (an AGENTS.md-style static
  index). The static index is the SINGLE SOURCE OF TRUTH: files not
  referenced are "orphans" (reported as info — below the routing
  threshold by design, they simply do not enter the channel), and refs
  to missing files are errors (no write). The index cap is 42 entries
  (warning when exceeded — a dated cleaning plan is expected).

Fields (see examples/memory_index.example.json):
  id         = filename without extension
  path       = absolute path to the file
  index_line = the trigger line from the AGENTS.md-style index that
               references this file (curation context only — the
               extension ignores it at scoring time)
  desc_en    = one-line description of what the file holds. Curated
               descriptions are PRESERVED across regenerations (loaded
               from the existing --out file — never overwritten).
               New entries: LLM-generated when --llm is available,
               otherwise derived (first heading in glob mode, index
               line FR in gate mode — desc_src "fr-fallback").
               The ranking quality is exactly the curation quality.
  desc_src   = "curated" | "llm" | "derived" | "fr-fallback"

The "source" field records which mode wrote the file (fingerprint).
--check warns if an existing --out was written by the OTHER mode —
cross-builder guard: running one mode where the other one lives
silently switches the channel model (incident 2026-10-13).

Usage:
  build-index.py --dir DIR --out OUT [--index-file FILE] [--gate]
                 [--exclude PAT]... [--no-preserve] [--llm [URL]] [--check]

Gate mode requires --index-file (--dir is used for orphan/missing
detection). --llm without a URL defaults to $JEV_INDEX_LLM_URL or the
local scorer server (http://127.0.0.1:5000/v1/chat/completions — zero
egress; override URL/model with the env vars or the URL argument).
Without --llm, new entries get the derived fallback and no server is
ever contacted. --check never writes; it exits 1 on dead refs (gate),
out-of-sync or corrupt --out.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

DESC_MAX = 220
INDEX_CAP = 42  # « ### Index » entry cap — dated cleaning plan expected beyond
INDEX_REF = re.compile(r"`memory/([\w\-]+)\.md`")
GATE_SOURCE = ("AGENTS.md section '### Index' — DÉRIVÉ : ne pas éditer "
               "id/path/index_line à la main ; desc_en : curée ou générée "
               "(llm), préservée entre régénérations — seul AGENTS.md "
               "s'édite à la main")
GLOB_SOURCE = ("derived by build-index.py (glob mode) from --dir/*.md "
               "(minus --exclude) — do not hand-edit id/path/index_line; "
               "desc_en is curated or derived and preserved across "
               "regenerations; regenerate to update")
LLM_URL_DEFAULT = "http://127.0.0.1:5000/v1/chat/completions"
LLM_TIMEOUT_S = 60


def derive_desc(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip("#").strip()
        if line and not line.startswith(">") and not line.startswith("---"):
            return line[:DESC_MAX]
    return ""


def parse_index_section(text: str) -> list[str]:
    """Bullets of the `### Index` section (wrapped lines regrouped into
    their bullet)."""
    m = re.search(r"^### Index[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        sys.exit("ERREUR: section '### Index' introuvable dans --index-file")
    lines: list[str] = []
    cur: str | None = None
    for raw in m.group(1).splitlines():
        if raw.startswith("- "):
            if cur is not None:
                lines.append(cur)
            cur = raw[2:]
        elif raw.strip() and cur is not None:
            cur += " " + raw.strip()
    if cur is not None:
        lines.append(cur)
    return lines


def index_slugs(index_file: str) -> dict[str, str]:
    """{slug: first bullet referencing it} from the ### Index section."""
    with open(index_file, encoding="utf-8", errors="replace") as fh:
        lines = parse_index_section(fh.read())
    slugs: dict[str, str] = {}
    for line in lines:
        for slug in INDEX_REF.findall(line):
            slugs.setdefault(slug, line)
    return slugs


def generate_desc_en(slug: str, context: str, llm_url: str) -> str | None:
    """One English sentence for a new entry (local LLM by default — zero
    egress). Returns None on any failure; the caller falls back."""
    llm_model = os.environ.get("JEV_INDEX_LLM_MODEL",
                               "local/Qwen3.8-27B-UD-Q4_K_XL")
    prompt = (
        "Write ONE English sentence describing what this memory file covers, "
        "for a memory routing system. Reply with the sentence only, no quotes.\n"
        f"Slug: {slug}\n{context[:220]}"
    )
    body = json.dumps(
        {"model": llm_model, "temperature": 0, "max_tokens": 300,
         "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    req = urllib.request.Request(
        llm_url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as r:
            d = json.loads(r.read())
        content = (d["choices"][0]["message"].get("content") or "").strip()
        return content or None
    except Exception:
        return None


def load_out(out: str) -> dict | None:
    """The existing out file as a dict, or None (missing/unreadable)."""
    try:
        with open(out, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True,
                   help="memory directory (entry source in glob mode; "
                        "orphan/missing detection in gate mode)")
    p.add_argument("--out", required=True)
    p.add_argument("--index-file", default=None,
                   help="AGENTS.md-style static index. gate mode: the "
                        "single source of truth (### Index section). "
                        "glob mode: curation context only (index_line)")
    p.add_argument("--gate", action="store_true",
                   help="entries = references in the ### Index section of "
                        "--index-file (orphans = below the routing "
                        "threshold, by design)")
    p.add_argument("--exclude", action="append", default=[],
                   help="filename or glob to skip (repeatable)")
    p.add_argument("--no-preserve", action="store_true",
                   help="ignore desc_en/desc_src in an existing --out file "
                        "(fresh pass over the whole index)")
    p.add_argument("--llm", nargs="?", const=os.environ.get(
        "JEV_INDEX_LLM_URL", LLM_URL_DEFAULT), default=None, metavar="URL",
        help="generate desc_en for new entries via a local LLM (default: "
             "$JEV_INDEX_LLM_URL or the localhost scorer; a remote URL is "
             "never contacted unless explicitly given)")
    p.add_argument("--check", action="store_true",
                   help="verify without writing (exit 1: dead refs in gate "
                        "mode, out-of-sync or corrupt --out)")
    args = p.parse_args()

    if args.gate and not args.index_file:
        print("--gate requires --index-file", file=sys.stderr)
        return 2

    excluded = set()
    for pat in args.exclude:
        excluded.update(os.path.basename(f) for f in
                        glob.glob(os.path.join(args.dir, pat)))

    dir_files = sorted(glob.glob(os.path.join(args.dir, "*.md")))
    mem_files = {os.path.splitext(os.path.basename(f))[0] for f in dir_files
                 if os.path.basename(f) not in excluded}

    # Cross-builder guard: which mode wrote the existing out file?
    old = load_out(args.out)
    if old is not None:
        old_source = old.get("source", "")
        if old_source:
            old_gate = old_source.startswith("AGENTS.md section")
            if old_gate != args.gate:
                print(f"AVERTISSEMENT — {args.out} a été écrit par le mode "
                      f"{'gate' if old_gate else 'glob'} ; ce run est en "
                      f"mode {'gate' if args.gate else 'glob'} — le modèle "
                      f"de canal change, vérifier l'intention")

    slugs = index_slugs(args.index_file) if args.index_file else {}

    if args.gate:
        with open(args.index_file, encoding="utf-8", errors="replace") as fh:
            lines = parse_index_section(fh.read())
        if len(lines) > INDEX_CAP:
            print(f"AVERTISSEMENT — index {len(lines)}/{INDEX_CAP} entrées : "
                  f"plafond dépassé (plan de cleaning daté attendu)")
        entries: dict[str, dict] = {}
        for line in lines:
            for slug in INDEX_REF.findall(line):
                entries.setdefault(
                    slug,
                    {"id": slug,
                     "path": os.path.abspath(
                         os.path.join(args.dir, f"{slug}.md")),
                     "index_line": line[:220]})
        orphans = sorted(mem_files - set(entries))
        missing = sorted(set(entries) - mem_files)
        if missing:
            print(f"ERREUR — refs vers fichiers absents : {missing}",
                  file=sys.stderr)
            return 1
    else:
        entries = {}
        for f in dir_files:
            slug = os.path.splitext(os.path.basename(f))[0]
            if slug in excluded:
                continue
            entry = {"id": slug, "path": os.path.abspath(f)}
            if slug in slugs:
                entry["index_line"] = slugs[slug]
            entries[slug] = entry
        orphans = []
        missing = []

    if args.check:
        if old is None:
            print("--check : OK (pas de out existant, rien à synchroniser)")
            return 0
        old_ids = {m["id"] for m in old.get("memories", [])}
        want = set(entries)
        gone = sorted(old_ids - want)
        new = sorted(want - old_ids)
        if gone or new:
            print(f"--check : OUT OF SYNC — dans le out mais plus attendu : "
                  f"{gone} ; attendu mais absent du out : {new}")
            return 1
        if orphans:
            print(f"orphans (info — sous le seuil de routing) : {orphans}")
        print("--check : OK")
        return 0

    # Preservation: curated desc_en survive every regeneration.
    preserve: dict[str, tuple[str, str]] = {}
    if not args.no_preserve and old is not None:
        for m in old.get("memories", []):
            if m.get("desc_en"):
                preserve[m["id"]] = (m["desc_en"],
                                     m.get("desc_src") or "curated")

    memories = []
    n_llm = n_fb = 0
    for m in sorted(entries.values(), key=lambda e: e["id"]):
        if m["id"] in preserve:
            m["desc_en"], m["desc_src"] = preserve[m["id"]]
        else:
            ctx = (f"French index line: {m['index_line']}"
                   if m.get("index_line") else f"File: {m['id']}.md")
            if args.llm:
                gen = generate_desc_en(m["id"], ctx, args.llm)
                if gen:
                    m["desc_en"], m["desc_src"] = gen, "llm"
                    n_llm += 1
                    print(f"desc_en générée (llm) : {m['id']}")
                    memories.append(m)
                    continue
            if m.get("index_line"):
                m["desc_en"] = m["index_line"][:160]
                m["desc_src"] = "fr-fallback"
                n_fb += 1
                print(f"AVERTISSEMENT — fallback FR (llm indisponible) : "
                      f"{m['id']}")
            else:
                with open(m["path"], encoding="utf-8",
                          errors="replace") as fh:
                    m["desc_en"] = derive_desc(fh.read(8192))
                m["desc_src"] = "derived"
        memories.append(m)

    if not memories:
        print(f"no .md files under {args.dir}", file=sys.stderr)
        return 1

    payload = json.dumps(
        {"source": GATE_SOURCE if args.gate else GLOB_SOURCE,
         "memories": memories},
        indent=2 if args.gate else 1, ensure_ascii=False) + "\n"
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(payload)

    n_il = sum(1 for m in memories if "index_line" in m)
    n_pr = sum(1 for m in memories if m["desc_src"] == "curated")
    print(f"{len(memories)} entries ({n_il} with index_line, {n_pr} curated"
          + (f", {n_llm} llm" if n_llm else "")
          + (f", {n_fb} fr-fallback" if n_fb else "") + f") → {args.out}")
    if orphans:
        print(f"orphans (pas dans l'index, info — sous le seuil de routing) : "
              f"{orphans}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
