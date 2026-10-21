"""CPU evaluation against frozen source-span annotations, not model self-grading."""
from __future__ import annotations
import math

from .extraction import digest


def _covered(interval, spans):
    a,b=interval
    pieces=sorted((max(a,x),min(b,y)) for x,y in spans if y>a and x<b)
    end=a
    for x,y in pieces:
        if x>end:return False
        end=max(end,y)
    return end>=b


def _span_stats(sources, blocks):
    intervals={k:[] for k in sources};total=0;unique=0
    for block in blocks:
        for s in block['spans']:
            a,b=s['start'],s['end'];intervals[s['source_id']].append((a,b));total+=b-a
    for spans in intervals.values():
        end=0
        for a,b in sorted(spans):
            unique+=max(0,b-max(a,end));end=max(end,b)
    return {'unique_source_chars':unique,'total_source_span_chars':total,
            'redundant_source_chars':total-unique,
            'redundant_fraction':(total-unique)/total if total else 0}


def evaluate(manifest, scores, annotations):
    if annotations.get('schema')!=1 or annotations.get('compaction_id')!=manifest['compaction_id']:
        raise ValueError('annotation compaction mismatch')
    if annotations.get('source_prefix_sha256')!=manifest['source_prefix_sha256']:
        raise ValueError('annotation source cutoff mismatch')
    budget=annotations['consultation_budget_estimated_tokens']
    if type(budget) is not int or budget<=0:raise ValueError('invalid consultation budget')
    items=annotations['items'];ids=set()
    for item in items:
        if item['id'] in ids:raise ValueError('duplicate annotation id')
        ids.add(item['id'])
        source=manifest['sources'].get(item['source_id'])
        if not source:raise ValueError('annotation source missing')
        a,b=item['start'],item['end']
        if not isinstance(a,int) or not isinstance(b,int) or not 0<=a<b<=len(source['text']):
            raise ValueError('invalid annotation interval')
        if digest(source['text'][a:b])!=item['text_sha256']:raise ValueError('annotation source checksum mismatch')
    blocks={b['id']:b for b in manifest['blocks']}
    rows=scores.get('rows',[]);all_seen=set();seen=set()
    for r in rows:
        if r['id'] not in blocks or r['id'] in all_seen:raise ValueError('score block missing or duplicate')
        all_seen.add(r['id'])
        if r.get('status')!='scored':
            if r.get('score') is not None:raise ValueError('unscored block has a score')
            continue
        seen.add(r['id'])
        if type(r['score']) not in (int,float) or not math.isfinite(r['score']) or not 0<=r['score']<=1:raise ValueError('invalid score')
    # One shared pool avoids giving baselines references JEV never evaluated.
    pool=[b for b in manifest['blocks'] if b['id'] in seen]
    score_by_id={r['id']:r['score'] for r in rows if r.get('status')=='scored'}
    orders={
        'jev':sorted(pool,key=lambda b:(-score_by_id[b['id']],b['id'])),
        'recency':list(reversed(pool)),
        'chronological':pool,
    }
    digest_refs=annotations.get('digest_refs',[])
    if not isinstance(digest_refs,list) or any(not isinstance(r,int) for r in digest_refs):
        raise ValueError('invalid digest refs')
    rank={r:i for i,r in enumerate(digest_refs)}
    orders['digest_shaped_pointers']=sorted(
        (b for b in pool if any(r in rank for r in b['refs'])),
        key=lambda b:(min(rank[r] for r in b['refs'] if r in rank),manifest['blocks'].index(b)),
    )
    methods={}
    for name,order in orders.items():
        selected=[];cost=0;intervals={k:[] for k in manifest['sources']}
        for block in order:
            size=block['estimated_tokens']
            if cost+size>budget:continue
            selected.append(block['id']);cost+=size
            for span in block['spans']:intervals[span['source_id']].append((span['start'],span['end']))
        covered=[item['id'] for item in items if _covered((item['start'],item['end']),intervals[item['source_id']])]
        critical=[i['id'] for i in items if i.get('importance')=='critical']
        selected_stats=_span_stats(manifest['sources'],[blocks[x] for x in selected])
        methods[name]={'selected_ids':selected,'estimated_consultation_tokens':cost,
                       'source_coverage':selected_stats,
                       'covered_items':covered,'covered_count':len(covered),'total_items':len(items),
                       'critical_covered':len(set(critical)&set(covered)),'critical_total':len(critical),
                       'missed_items':[i['id'] for i in items if i['id'] not in covered]}
    evaluated_intervals={k:[] for k in manifest['sources']}
    for b in pool:
        for s in b['spans']:evaluated_intervals[s['source_id']].append((s['start'],s['end']))
    available=[i['id'] for i in items if _covered((i['start'],i['end']),evaluated_intervals[i['source_id']])]
    pool_stats=_span_stats(manifest['sources'],pool)
    admissible=sum(len(s['text']) for s in manifest['sources'].values())
    pool_stats.update(admissible_source_chars=admissible,
                      unique_fraction=pool_stats['unique_source_chars']/admissible if admissible else 1)
    for method in methods.values():
        method['miss_causes']={i:('not_fully_in_scored_pool' if i not in available else 'not_selected_within_budget')
                               for i in method['missed_items']}
    return {'schema':1,'compaction_id':manifest['compaction_id'],'source_prefix_sha256':manifest['source_prefix_sha256'],
            'budget_tokens':budget,'budget_method':'ceil(escaped UTF-8 bytes/3), NOT exact tokenizer tokens',
            'score_status':scores.get('status'),'scored_blocks':len(pool),'total_blocks':len(blocks),
            'annotation_items_in_scored_pool':available,'scoring_source_coverage':pool_stats,
            'score_reasons':dict(scores.get('reasons',{})),
            'failed_blocks':sum(r.get('status')=='failed' for r in rows),
            'not_attempted_blocks':sum(r.get('status')=='not_attempted' for r in rows),
            'methods':methods,
            'limitations':['relevance labels are human hypotheses, not causal proof of task success',
                           'all baselines use the same scored subset; incomplete pool is not full-zone retrieval',
                           'digest-shaped pointer baseline is recomputed on canonical zone, not the shipped digest extension',
                           'budget uses declared escaped-byte estimate, not exact tokenizer counts']}
