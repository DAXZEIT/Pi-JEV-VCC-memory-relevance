#!/usr/bin/env python3
"""Build a memory index JSON for jev-relevance from a directory of
markdown memory files.

Fields (see examples/memory_index.example.json):
  id         = filename without extension
  path       = absolute path to the file
  index_line = optional; the trigger line from an AGENTS.md-style index
               that references this file (see --index-file). Curation
               context — the extension ignores it at scoring time.
  desc_en    = one-line description of what the file holds. The script
               derives a start from the first markdown heading; the
               measured setup rewrites every desc_en by hand (one English
               line) before scoring — the ranking quality is exactly the
               curation quality. Curated descriptions are PRESERVED across
               regenerations: if --out already exists, the desc_en/desc_src
               of every entry whose id matches is kept (new files get the
               derived first draft, deleted files drop out).
  desc_src   = "derived" (script only) or "curated" (human reviewed).

Usage:
  build-index.py --dir ~/.pi/agent/memory --out ~/.pi/agent/memory_index.json \
      [--index-file ~/.pi/agent/AGENTS.md] \
      [--exclude error_log.md] [--exclude 'draft-*'] [--no-preserve]

After generation: review desc_en (rewrite it in English), optionally
index_line, and flip desc_src to "curated" on the entries you touched.
--no-preserve forces a fresh derived pass over the whole index.

Tip: exclude append-only hypothesis/incident logs from the index — only
long-term durable-fact memories should be scored.
"""
import argparse
import glob
import json
import os
import re
import sys

DESC_MAX = 220
# Provenance note — the extension ignores it, humans read it.
SOURCE = ("derived by build-index.py from --dir/*.md (minus --exclude) — "
          "do not hand-edit id/path/index_line; desc_en is curated or derived "
          "and preserved across regenerations; regenerate to update")
# An index line is any bullet that references `memory/<slug>.md` in backticks
# (the reference may sit mid-line, and one line may reference several files).
INDEX_REF = re.compile(r"`memory/([^`/]+\.md)`")


def derive_desc(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip("#").strip()
        if line and not line.startswith(">") and not line.startswith("---"):
            return line[:DESC_MAX]
    return ""


def parse_index_lines(path: str) -> dict:
    """Extract {slug: index line} from an AGENTS.md-style index file.

    Entries are BULLETS, not lines: a bullet starts at a "- " line and
    swallows its wrapped continuation lines (plain text until the next
    bullet or a section header). The `memory/<slug>.md` reference is
    usually on a continuation line, so matching per line would miss it.
    """
    lines = {}
    bullet = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip()
            stripped = line.strip()
            if stripped.startswith("- "):
                _flush(lines, bullet)
                bullet = stripped[2:].strip()
            elif stripped.startswith("#"):
                _flush(lines, bullet)
                bullet = None
            elif stripped and bullet is not None:
                bullet += " " + stripped
        _flush(lines, bullet)
    return lines


def _flush(lines: dict, bullet) -> None:
    if bullet and INDEX_REF.search(bullet):
        _attach(lines, bullet)


def _attach(lines: dict, bullet: str) -> None:
    for m in INDEX_REF.finditer(bullet):
        slug = os.path.splitext(m.group(1))[0]
        lines.setdefault(slug, bullet)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--index-file", default=None,
                   help="AGENTS.md-style index file: bullet lines referencing "
                        "`memory/<slug>.md` in backticks are attached to the "
                        "matching entry as index_line (curation context only)")
    p.add_argument("--exclude", action="append", default=[],
                   help="filename or glob to skip (repeatable)")
    p.add_argument("--no-preserve", action="store_true",
                   help="ignore desc_en/desc_src in an existing --out file "
                        "(fresh derived pass over the whole index)")
    args = p.parse_args()
    preserve = {}
    if not args.no_preserve and os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                for m in json.load(fh).get("memories", []):
                    if m.get("desc_en"):
                        preserve[m["id"]] = (m["desc_en"],
                                             m.get("desc_src", "curated"))
        except (OSError, ValueError):
            pass  # unreadable/corrupt out file: fresh pass
    excluded = set()
    for pat in args.exclude:
        excluded.update(os.path.basename(f) for f in glob.glob(os.path.join(args.dir, pat)))
    index_lines = parse_index_lines(args.index_file) if args.index_file else {}
    memories = []
    for f in sorted(glob.glob(os.path.join(args.dir, "*.md"))):
        if os.path.basename(f) in excluded:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            desc = derive_desc(fh.read(8192))
        slug = os.path.splitext(os.path.basename(f))[0]
        entry = {"id": slug, "path": os.path.abspath(f)}
        if slug in index_lines:
            entry["index_line"] = index_lines[slug]
        if slug in preserve:
            entry["desc_en"], entry["desc_src"] = preserve[slug]
        else:
            entry["desc_en"] = desc
            entry["desc_src"] = "derived"
        memories.append(entry)
    if not memories:
        print(f"no .md files under {args.dir}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"source": SOURCE, "memories": memories},
                  fh, indent=1, ensure_ascii=False)
    n_il = sum(1 for m in memories if "index_line" in m)
    n_pr = sum(1 for m in memories if m["desc_src"] != "derived")
    extra = f" ({n_il} with index_line)" if args.index_file else ""
    extra += f" ({n_pr} desc_en preserved)" if n_pr else ""
    print(f"{len(memories)} entries{extra} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
