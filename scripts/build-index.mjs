#!/usr/bin/env node
// Build a memory index JSON for jev-relevance from a directory of
// markdown memory files. Node.js stdlib port of build-index.py — the
// Python script is the behavioral reference; byte parity of the output
// JSON is the contract.
//
// Usage:
//   build-index.mjs --dir ~/.pi/agent/memory --out ~/.pi/agent/memory_index.json \
//       [--index-file ~/.pi/agent/AGENTS.md] \
//       [--exclude error_log.md] [--exclude 'draft-*']
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as process from 'node:process';

const DESC_MAX = 220;
// An index line is any bullet that references `memory/<slug>.md` in backticks
// (the reference may sit mid-line, and one line may reference several files).
const INDEX_REF = /`memory\/([^`\/]+\.md)`/;
const INDEX_REF_G = /`memory\/([^`\/]+\.md)`/g;

// Python str.strip() whitespace: \t\n\r\v\f, \x1c-\x1f, \x85, \xa0,
// \u1680, \u2000-\u200a, \u2028, \u2029, \u202f, \u205f, \u3000
const PY_WS = ' \\t\\n\\x0B\\f\\r\\x1C-\\x1F\\x85\\xA0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000';
const RE_STRIP = new RegExp(`^[${PY_WS}]+|[${PY_WS}]+$`, 'g');

function pyStrip(s) {
  return s.replace(RE_STRIP, '');
}

// Python str.splitlines() boundaries (used by derive_desc on the read text).
const RE_SPLITLINES = /\r\n|\r|\n|\x0B|\f|\x1C|\x1D|\x1E|\x85|\u2028|\u2029/;
function pySplitLines(s) {
  return s.split(RE_SPLITLINES);
}

// Universal-newline iteration (Python file object): \n, \r\n, \r.
const RE_UNIVERSAL_NEWLINE = /\r\n|\r|\n/;

function stripHashes(s) {
  // str.strip("#"): leading and trailing '#'
  return s.replace(/^#+|#+$/g, '');
}

function deriveDesc(text) {
  for (const raw of pySplitLines(text)) {
    const line = pyStrip(stripHashes(pyStrip(raw)));
    if (line && !line.startsWith('>') && !line.startsWith('---')) {
      return Array.from(line).slice(0, DESC_MAX).join('');
    }
  }
  return '';
}

function osPathSplitext(name) {
  // os.path.splitext: strip the last extension; a leading dot is not an ext.
  const idx = name.lastIndexOf('.');
  if (idx > 0) return name.slice(0, idx);
  return name;
}

function parseIndexLines(indexPath) {
  // Extract {slug: index line} from an AGENTS.md-style index file.
  // Entries are BULLETS, not lines: a bullet starts at a "- " line and
  // swallows its wrapped continuation lines (plain text until the next
  // bullet or a section header).
  const lines = new Map(); // setdefault semantics: first attach wins
  let bullet = null;
  const flush = () => {
    if (bullet) {
      const m = INDEX_REF.exec(bullet);
      if (m) {
        INDEX_REF_G.lastIndex = 0;
        for (const mm of bullet.matchAll(INDEX_REF_G)) {
          const slug = osPathSplitext(mm[1]);
          if (!lines.has(slug)) lines.set(slug, bullet);
        }
      }
    }
  };
  const text = fs.readFileSync(indexPath, 'utf8'); // errors="replace" default
  for (const raw of text.split(RE_UNIVERSAL_NEWLINE)) {
    const line = raw.replace(RE_STRIP, ''); // raw.rstrip()
    const stripped = pyStrip(line);
    if (stripped.startsWith('- ')) {
      flush();
      bullet = pyStrip(stripped.slice(2));
    } else if (stripped.startsWith('#')) {
      flush();
      bullet = null;
    } else if (stripped && bullet !== null) {
      bullet += ' ' + stripped;
    }
  }
  flush();
  return lines;
}

function escapeRegExpChar(c) {
  return c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
}

// fnmatch (posix, case-sensitive) translation, like Python's fnmatch.translate:
// `*` -> `.*` (dotall), `?` -> `.`, `[seq]` / `[!seq]` character classes.
function fnmatchRegex(pat) {
  let re = '';
  let i = 0;
  const n = pat.length;
  while (i < n) {
    const c = pat[i];
    if (c === '*') {
      re += '.*';
      i++;
    } else if (c === '?') {
      re += '.';
      i++;
    } else if (c === '[') {
      let j = i + 1;
      if (j < n && (pat[j] === '!' || pat[j] === '^')) j++;
      if (j < n && pat[j] === ']') j++;
      while (j < n && pat[j] !== ']') j++;
      if (j >= n) {
        re += '\\[';
        i++;
      } else {
        let stuff = pat.slice(i + 1, j);
        if (stuff[0] === '!') stuff = '^' + stuff.slice(1);
        else if (stuff[0] === '^') stuff = '\\^' + stuff.slice(1);
        re += '[' + stuff + ']';
        i = j + 1;
      }
    } else {
      re += escapeRegExpChar(c);
      i++;
    }
  }
  return new RegExp('^' + re + '$', 's'); // dotAll flag (ES2018) — NOT the inline (?s:...) group (ES2025, breaks on Node ≤ 22)
}

function listMdFiles(dir) {
  // glob.glob(os.path.join(dir, "*.md")): *.md matches non-dot files
  // (dotfiles require the pattern to start with '.').
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    if (ent.name.startsWith('.')) continue;
    if (!ent.name.endsWith('.md')) continue;
    if (!ent.isFile()) continue;
    out.push(path.join(dir, ent.name));
  }
  return out;
}

function excludeSet(dir, patterns) {
  // glob.glob(dir/pat) then basenames: match pattern against dir entries
  // with fnmatch semantics (glob's leading-dot rule included).
  const excluded = new Set();
  for (const pat of patterns) {
    const re = fnmatchRegex(pat);
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      if (ent.name.startsWith('.') && !pat.startsWith('.')) continue;
      if (re.test(ent.name)) excluded.add(ent.name);
    }
  }
  return excluded;
}

function parseArgs(argv) {
  const opts = { dir: null, out: null, indexFile: null, exclude: [] };
  const args = argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    let key = a;
    let val = null;
    const eq = a.indexOf('=');
    if (a.startsWith('--') && eq !== -1) {
      key = a.slice(0, eq);
      val = a.slice(eq + 1);
    }
    switch (key) {
      case '--dir':
        opts.dir = val !== null ? val : args[++i];
        break;
      case '--out':
        opts.out = val !== null ? val : args[++i];
        break;
      case '--index-file':
        opts.indexFile = val !== null ? val : args[++i];
        break;
      case '--exclude':
        opts.exclude.push(val !== null ? val : args[++i]);
        break;
      default:
        process.stderr.write(`unrecognized arguments: ${a}\n`);
        process.exit(2);
    }
  }
  if (!opts.dir || !opts.out) {
    process.stderr.write(
      'usage: build-index.mjs --dir DIR --out OUT [--index-file FILE] [--exclude PAT]\n',
    );
    process.exit(2);
  }
  return opts;
}

function main() {
  const args = parseArgs(process.argv);

  const excluded = excludeSet(args.dir, args.exclude);
  const indexLines = args.indexFile ? parseIndexLines(args.indexFile) : new Map();

  const files = listMdFiles(args.dir).sort((a, b) => {
    // Python sorted(): lexicographic by Unicode code point.
    const ca = Array.from(a);
    const cb = Array.from(b);
    const n = Math.min(ca.length, cb.length);
    for (let i = 0; i < n; i++) {
      if (ca[i] !== cb[i]) return ca[i] < cb[i] ? -1 : 1;
    }
    return ca.length - cb.length;
  });

  const memories = [];
  for (const f of files) {
    if (excluded.has(path.basename(f))) continue;
    // fh.read(8192): first 8192 CHARACTERS, invalid bytes -> U+FFFD
    const buf = fs.readFileSync(f);
    const text = buf.toString('utf8');
    const head = Array.from(text).slice(0, 8192).join('');
    const desc = deriveDesc(head);
    const slug = osPathSplitext(path.basename(f));
    const entry = { id: slug, path: path.resolve(f) };
    if (indexLines.has(slug)) entry.index_line = indexLines.get(slug);
    entry.desc_en = desc;
    entry.desc_src = 'derived';
    memories.push(entry);
  }

  if (memories.length === 0) {
    process.stderr.write(`no .md files under ${args.dir}\n`);
    return 1;
  }

  // json.dump(indent=1, ensure_ascii=False), no trailing newline.
  fs.writeFileSync(args.out, JSON.stringify({ memories }, null, 1), 'utf8');

  const nIl = memories.filter((m) => 'index_line' in m).length;
  const extra = args.indexFile ? ` (${nIl} with index_line)` : '';
  console.log(`${memories.length} entries${extra} → ${args.out}`);
  return 0;
}

process.exit(main());
