#!/usr/bin/env python3
"""Build a memory index JSON for jev-relevance from a directory of
markdown memory files.

id      = filename without extension
path    = absolute path to the file
desc_en = one-line English description of what the file holds — taken from
          the first markdown heading if present, else the first non-empty
          line, truncated to 220 chars. Review and hand-tune: this text is
          what the scorer sees; quality of the description = quality of
          the ranking.

Usage:
  build-index.py --dir ~/.pi/agent/memory --out ~/.pi/agent/memory_index.json \
      [--exclude error_log.md] [--exclude 'draft-*']

Tip: exclude append-only hypothesis/incident logs from the index — only
long-term durable-fact memories should be scored.
"""
import argparse
import glob
import json
import os

DESC_MAX = 220


def derive_desc(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip("#").strip()
        if line and not line.startswith(">") and not line.startswith("---"):
            return line[:DESC_MAX]
    return ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--exclude", action="append", default=[],
                   help="filename or glob to skip (repeatable)")
    args = p.parse_args()
    import fnmatch
    excluded = set()
    for pat in args.exclude:
        excluded.update(os.path.basename(f) for f in glob.glob(os.path.join(args.dir, pat)))
    memories = []
    for f in sorted(glob.glob(os.path.join(args.dir, "*.md"))):
        if os.path.basename(f) in excluded:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            desc = derive_desc(fh.read(8192))
        memories.append({
            "id": os.path.splitext(os.path.basename(f))[0],
            "path": os.path.abspath(f),
            "desc_en": desc,
        })
    if not memories:
        print(f"no .md files under {args.dir}", file=__import__("sys").stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"memories": memories}, fh, indent=1, ensure_ascii=False)
    print(f"{len(memories)} entries → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
