#!/usr/bin/env python3
"""Offline experiment: one raw shared-prefix anchor, then identical JEV prompts.

Not a production hook or cache configuration change. Discard anchor completion.
One total wall budget includes rendering, anchor and sequential candidate calls.
Never persist prompts/source text. Requires a private existing output parent.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recall_relevance.cli import schedule, write_private
from recall_relevance.extraction import consult, digest
from recall_relevance import scoring


def probe(manifest, *, server, timeout_s=90, max_blocks=3, max_prompt_tokens=49152):
    started=time.monotonic();deadline=scoring._Deadline(started,timeout_s)
    endpoint=scoring._endpoint(server)
    blocks=schedule(manifest['blocks'],'spread')
    for block in blocks:consult(manifest,block['id'])
    jev=scoring._load_jev();template=jev.load_template();prompts=[]
    for block in blocks[:max_blocks]:
        request=jev.build_request({'current_context':scoring._escape(manifest['current_context']),
                                   'earlier_block':scoring._escape(block['text'])},
                                  {'recall_help':{'type':'noul','instructions':scoring.QUESTION,
                                                  'criteria':dict(scoring.CRITERIA)}},jev.DEFAULT_MODEL_LOCAL)
        plan,keys=jev.hfp.prepare_policy(request,jev.POLICY,jev.POLICY_NAME)
        q=plan.questions[0]
        if keys!=('recall_help',) or q.output_labels!=('A','B'):raise ValueError('unexpected probe protocol')
        native,thinking=jev.compile_branch(request,plan,q)
        prompts.append(jev.render_text(native,thinking,template,q.answer_prefix))
    prefix=os.path.commonprefix(prompts)
    if len(prompts)<2 or len(prefix)<256 or prefix in prompts:raise ValueError('no useful strict prefix')
    tokens=scoring._post(endpoint,'/tokenize',{'content':prefix},deadline).get('tokens')
    if not isinstance(tokens,list) or any(type(t) is not int or t<0 for t in tokens):raise ValueError('invalid probe tokenizer')
    if len(tokens)>max_prompt_tokens:raise ValueError('anchor over token ceiling')
    anchor={'prompt_sha256':digest(prefix),'n_tokens':len(tokens)}
    anchor_start=time.monotonic()
    response=scoring._post(endpoint,'/v1/completions',{'prompt':prefix,'max_tokens':1,
                                                     'temperature':0.0,'cache_prompt':True},deadline)
    scoring._metadata(response,anchor)
    anchor['latency_s']=time.monotonic()-anchor_start
    result=scoring.score_blocks(manifest['current_context'],blocks,server=server,
                                timeout_s=deadline.guard(),max_prompt_tokens=max_prompt_tokens,
                                max_blocks=max_blocks)
    result['experiment']='one-raw-prefix-anchor-v1'
    result['anchor']=anchor
    result['global_elapsed_s']=time.monotonic()-started
    result['global_timeout_s']=timeout_s
    result['schedule']='spread'
    result['probe_source_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # Independent comparison against the exact pre-anchor candidate renderings.
    for row,prompt in zip(result['rows'],prompts):
        if row.get('prompt_sha256') and row['prompt_sha256']!=digest(prompt):raise ValueError('probe changed candidate rendering')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True);parser.add_argument('--annotations',required=True)
    parser.add_argument('--server',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--timeout',type=float,default=90);parser.add_argument('--max-blocks',type=int,default=3)
    parser.add_argument('--max-prompt-tokens',type=int,default=49152)
    args=parser.parse_args()
    if not 0<args.timeout<=120 or not 2<=args.max_blocks<=16 or args.max_prompt_tokens<=0:
        parser.error('probe budget: timeout<=120, 2..16 blocks, positive prompt ceiling')
    output=Path(args.output)
    if output.exists() or output.is_symlink() or not output.parent.is_dir() or output.parent.stat().st_mode&0o077:
        parser.error('output must be new, in existing private directory')
    mp=Path(args.manifest);ap=Path(args.annotations);mraw=mp.read_text();araw=ap.read_text()
    result=probe(json.loads(mraw),server=args.server,timeout_s=args.timeout,
                 max_blocks=args.max_blocks,max_prompt_tokens=args.max_prompt_tokens)
    if mp.read_text()!=mraw or ap.read_text()!=araw:raise ValueError('probe inputs changed')
    result['manifest_sha256']=digest(mraw);result['annotations_sha256']=digest(araw)
    write_private(output,result)
    print(json.dumps({'result':str(output),'status':result['status'],'scored':result['scored'],
                      'global_elapsed_s':result['global_elapsed_s'],'anchor':result['anchor'],
                      'candidate_timings':[r.get('timings') for r in result['rows'][:args.max_blocks]]}))


if __name__=='__main__':main()
