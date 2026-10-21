"""Deterministic offline segmentation; no relevance filter, network or Pi mutation."""
from __future__ import annotations
import hashlib
import json
import math


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def escape_data(text: str) -> str:
    """Literal escape strings, not Unicode characters decoded back into delimiters."""
    return text.replace("<", r"\u003c").replace(">", r"\u003e")


def estimate(text):
    return math.ceil(len(escape_data(text).encode()) / 3)


def _prefix_end(text, start, budget):
    # Escape before budgeting. Binary search in character space preserves UTF-8.
    lo, hi = start + 1, len(text)
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(escape_data(text[start:mid]).encode()) <= budget:
            lo = mid + 1
        else:
            hi = mid - 1
    if hi <= start:
        raise ValueError("byte budget cannot fit a character")
    return hi


def _fragments(text, budget):
    start = 0
    while start < len(text):
        end = _prefix_end(text, start, budget)
        if end < len(text):
            cut = text.rfind("\n", start + (end-start)//2, end)
            if cut >= start:
                end = cut + 1
        yield start, end
        start = end


def _header(span):
    return (f"[ref #{span['ref']} | entry {span['entry_id']} | role {span['role']} | "
            f"part {span['part_index']} {span['type']} | chars {span['start']}:{span['end']}" +
            (f" | tool {span['tool_name']}" if span.get('tool_name') else "") +
            (f" | call {span['tool_call_id']}" if span.get('tool_call_id') else "") + "]\n")


def _render(span, sources):
    text = sources[span['source_id']]['text'][span['start']:span['end']]
    return _header(span) + text


def _tail(spans, sources, token_budget):
    if not token_budget:
        return []
    out, budget = [], token_budget * 3
    for old in reversed(spans):
        span = dict(old)
        text = sources[span['source_id']]['text'][span['start']:span['end']]
        if len(escape_data(text).encode()) > budget:
            # Find the maximal suffix fitting this overlap budget.
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi) // 2
                if len(escape_data(text[mid:]).encode()) <= budget:
                    hi = mid
                else:
                    lo = mid + 1
            span['start'] += lo
        if span['start'] < span['end']:
            span['overlap'] = True
            out.insert(0, span)
            budget -= len(escape_data(sources[span['source_id']]['text'][span['start']:span['end']]).encode())
        if budget <= 0:
            break
    return out


def extract(snapshot: dict, *, target_tokens=2000, overlap_tokens=128, include_thinking=True):
    if not isinstance(target_tokens, int) or target_tokens < 256:
        raise ValueError('target_tokens must be an integer >=256 (estimate, not tokenizer count)')
    if not isinstance(overlap_tokens, int) or not 0 <= overlap_tokens < target_tokens//2:
        raise ValueError('overlap_tokens must be nonnegative and < half the target')
    if snapshot.get('schema') != 1:
        raise ValueError('unsupported snapshot schema')
    sources, groups, excluded = {}, [], list(snapshot.get('excluded', []))
    # Small fragments let whole messages pack together and make bounded overlaps possible.
    atom_budget = min(1800, max(256, target_tokens*3//3))
    admitted_chars = 0
    for message in snapshot['dropped_messages']:
        ref = message.get('ref')
        if not isinstance(ref, int) or isinstance(ref, bool) or ref < 0:
            raise ValueError('message lacks exact global recall ref')
        excluded.extend(dict(x, entry_id=message['entry_id'], ref=ref) for x in message.get('excluded', []))
        group = []
        for part in message['parts']:
            if part['type'] == 'thinking' and not include_thinking:
                excluded.append({'entry_id':message['entry_id'],'ref':ref,'type':'thinking','reason':'thinking-disabled','chars':len(part['text'])})
                continue
            text = part['text']
            if not text:
                continue
            sid = f"{message['entry_id']}:{part['part_index']}:{part['type']}"
            if sid in sources:
                raise ValueError('ambiguous source part')
            source = {'entry_id':message['entry_id'], 'ref':ref,'role':message['role'],
                      'part_index':part['part_index'],'type':part['type'],'text':text,
                      'sha256':digest(text), 'consultation':'private-snapshot-source',
                      'journal_raw_entry_sha256':message['raw_entry_sha256'],
                      'projected_message_sha256':message['projected_message_sha256'],
                      'origin':message.get('origin',{'kind':'message','entry_id':message['entry_id']}),
                      'offset_units':'Python Unicode code points'}
            sources[sid] = source
            admitted_chars += len(text)
            for start,end in _fragments(text, atom_budget):
                group.append({'source_id':sid,'entry_id':message['entry_id'],'ref':ref,
                              'role':message['role'],'part_index':part['part_index'],
                              'type':part['type'],'start':start,'end':end,'overlap':False,
                              'tool_name':part.get('name') or message.get('tool_name'),
                              'tool_call_id':part.get('call_id') or message.get('tool_call_id')})
        if group:
            groups.append(group)
    # Pair an assistant call with its following result when the pair is small.
    merged = []
    i = 0
    while i < len(groups):
        g = groups[i]
        calls = {s.get('tool_call_id') for s in g if s['type']=='toolCall'} - {None}
        if calls and i+1 < len(groups):
            nxt = groups[i+1]
            if nxt[0]['role']=='toolResult' and nxt[0].get('tool_call_id') in calls:
                pair = g+nxt
                if estimate('\n\n'.join(_render(s,sources) for s in pair)) <= target_tokens:
                    merged.append(pair); i += 2; continue
        merged.append(g); i += 1
    blocks, pending = [], []
    def flush():
        nonlocal pending
        if not pending:
            return
        text = '\n\n'.join(_render(s,sources) for s in pending)
        if estimate(text) > target_tokens:
            raise ValueError('internal block estimate exceeded target')
        key = digest({'session':snapshot['session_id'],'compaction':snapshot['compaction_id'],
                      'version':1,'target':target_tokens,'overlap':overlap_tokens,'spans':pending})
        blocks.append({'id':'recall-'+key[:20], 'refs':list(dict.fromkeys(s['ref'] for s in pending)),
                       'spans':[dict(s) for s in pending], 'text':text,'text_sha256':digest(text),
                       'estimated_tokens':estimate(text),'size_method':'ceil(escaped_utf8_bytes/3)',
                       'consultation':'vcc_recall for text; consult command for exact source spans'})
        pending = _tail(pending, sources, overlap_tokens)
    for group in merged:
        rendered = '\n\n'.join(_render(s,sources) for s in group)
        if pending and estimate('\n\n'.join(_render(s,sources) for s in pending+group)) > target_tokens:
            flush()
        if estimate(rendered) <= target_tokens:
            if pending and estimate('\n\n'.join(_render(s,sources) for s in pending+group)) > target_tokens:
                pending=[]  # Drop overlap only; previously emitted source coverage is preserved.
            pending.extend(group)
        else:
            for span in group:
                if pending and estimate('\n\n'.join(_render(s,sources) for s in pending+[span])) > target_tokens:
                    flush()
                if pending and estimate('\n\n'.join(_render(s,sources) for s in pending+[span])) > target_tokens:
                    pending=[]
                pending.append(span)
    if any(not s['overlap'] for s in pending):
        flush()
    covered = coverage(sources, blocks)
    if covered['unique_covered_chars'] != admitted_chars:
        raise ValueError('source coverage is incomplete')
    context_parts=[]
    for m in snapshot['current_messages']:
        if m['role']=='system' or m.get('exclude_from_context'):
            continue
        if m.get('summary'):
            context_parts.append('[current compaction summary]\n'+m['summary'])
        for p in m.get('parts',[]):
            if p['type']=='thinking' and not include_thinking:
                continue
            context_parts.append(f"[current {m['role']} ref {m['ref']} type {p['type']}]\n"+p['text'])
    return {'schema':1, 'session_id':snapshot['session_id'], 'compaction_id':snapshot['compaction_id'],
            'source_prefix_sha256':snapshot['source_prefix_sha256'],
            'source_prefix_lines':snapshot['source_prefix_lines'],
            'projection_engine':snapshot['projection_engine'],
            'config':{'target_tokens_estimate':target_tokens,'overlap_tokens_estimate':overlap_tokens,'include_thinking':include_thinking},
            'current_context':'\n\n'.join(context_parts),'sources':sources,'blocks':blocks,
            'excluded':excluded,'coverage':covered}


def coverage(sources, blocks):
    intervals={k:[] for k in sources}
    total=0
    for block in blocks:
        for s in block['spans']:
            intervals[s['source_id']].append((s['start'],s['end']))
            total += s['end']-s['start']
    unique=0
    for sid, spans in intervals.items():
        end=0
        for a,b in sorted(spans):
            if a>end:
                raise ValueError('gap in source coverage')
            unique += max(0,b-max(a,end)); end=max(end,b)
        if end != len(sources[sid]['text']):
            raise ValueError('uncovered source tail')
    return {'source_parts':len(sources),'admissible_chars':sum(len(s['text']) for s in sources.values()),
            'unique_covered_chars':unique,'total_span_chars':total,'overlap_chars':total-unique,
            'blocks':len(blocks)}


def consult(manifest, block_id):
    """Exact source text exists privately; printed representation is always escaped."""
    matches=[b for b in manifest['blocks'] if b['id']==block_id]
    if len(matches)!=1:
        raise ValueError('block id missing or ambiguous')
    block=matches[0]
    chunks=[]
    for span in block['spans']:
        source=manifest['sources'][span['source_id']]
        if digest(source['text']) != source['sha256']:
            raise ValueError('source checksum mismatch')
        text=source['text'][span['start']:span['end']]
        chunks.append({'ref':span['ref'],'entry_id':span['entry_id'],'type':span['type'],
                       'start':span['start'],'end':span['end'],'text':escape_data(text),
                       'origin':source.get('origin'),'offset_units':'Python Unicode code points',
                       'exact_source_sha256':digest(text),'representation':'literal-angle-escapes'})
    rebuilt='\n\n'.join(_render(s,manifest['sources']) for s in block['spans'])
    if digest(rebuilt)!=block['text_sha256'] or rebuilt!=block['text']:
        raise ValueError('block checksum mismatch')
    return {'schema':1,'id':block_id,'refs':block['refs'],'source':'private manifest projected source; not vcc_recall thinking output',
            'parts':chunks}
