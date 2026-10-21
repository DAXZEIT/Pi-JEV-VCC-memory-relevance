"""Opt-in offline CLI. No hooks, no provider fallback, no raw stdout by default."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .extraction import consult, digest, estimate, extract

ROOT=Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text())


def write_private(path, value):
    path=Path(path)
    parent=path.parent
    if not parent.is_dir() or (parent.stat().st_mode & 0o077):
        raise ValueError('output parent must exist with mode 0700 (no group/other access)')
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,'O_NOFOLLOW'):flags |= os.O_NOFOLLOW
    fd=os.open(path,flags,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(value,out,ensure_ascii=False,indent=2,allow_nan=False)
        out.write('\n')
        out.flush();os.fsync(out.fileno())


def snapshot_command(args):
    out=Path(args.out).resolve()
    if out.exists():raise ValueError('output directory already exists; no clobber')
    if not all(isinstance(x,int) and x>=256 for x in args.sizes) or len(set(args.sizes))!=len(args.sizes):
        raise ValueError('sizes must be unique integer estimates >=256')
    out.mkdir(mode=0o700,parents=True)
    cmd=['node',str(ROOT/'scripts/recall_snapshot.mjs'),'--session',str(Path(args.session).resolve()),
         '--compaction',args.compaction,'--max-bytes',str(args.max_bytes)]
    if args.pi_package:cmd+=['--pi-package',args.pi_package]
    if args.vcc_package:cmd+=['--vcc-package',args.vcc_package]
    run=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
    if run.returncode:
        # Bridge errors contain metadata only; never echo private stdout on failure.
        raise ValueError('canonical snapshot failed: '+run.stderr[:1500])
    snap=json.loads(run.stdout)
    write_private(out/'snapshot.json',snap)
    manifests=[]
    for size in args.sizes:
        manifest=extract(snap,target_tokens=size,overlap_tokens=args.overlap,include_thinking=not args.no_thinking)
        p=out/f'manifest-{size}.json';write_private(p,manifest)
        manifests.append({'path':str(p),'sha256':digest(p.read_text()),'blocks':len(manifest['blocks']),
                          'coverage':manifest['coverage'],'current_context_estimated_tokens':estimate(manifest['current_context'])})
    print(json.dumps({'ok':True,'compaction_id':snap['compaction_id'],'source_prefix_sha256':snap['source_prefix_sha256'],
                      'snapshot':str(out/'snapshot.json'),'manifests':manifests},indent=2))


def schedule(blocks, method):
    if method=='chronological':return list(blocks)
    if method!='spread':raise ValueError('unknown schedule')
    if len(blocks)<3:return list(reversed(blocks))
    order=[len(blocks)-1,0];queue=[(1,len(blocks)-2)]
    for lo,hi in queue:
        if lo>hi:continue
        mid=(lo+hi)//2;order.append(mid)
        queue.extend([(lo,mid-1),(mid+1,hi)])
    return [blocks[i] for i in order]


def score_command(args):
    from .scoring import score_blocks
    p=Path(args.manifest);raw=p.read_text();manifest=json.loads(raw)
    output=Path(args.output)
    if output.exists() or output.is_symlink() or not output.parent.is_dir() or output.parent.stat().st_mode & 0o077:
        raise ValueError('output must be new and its parent private before inference')
    annotation_raw=Path(args.annotations).read_text() if args.annotations else None
    annotation_hash=digest(annotation_raw) if annotation_raw is not None else None
    if annotation_raw is not None:
        from .evaluation import evaluate
        evaluate(manifest,{'rows':[]},json.loads(annotation_raw))
    # Validate exact consultability before sending any candidate to the model.
    for b in manifest['blocks']:consult(manifest,b['id'])
    result=score_blocks(manifest['current_context'],schedule(manifest['blocks'],args.schedule),server=args.server,
                        timeout_s=args.timeout,max_prompt_tokens=args.max_prompt_tokens,max_blocks=args.max_blocks)
    if p.read_text()!=raw:raise ValueError('manifest changed during scoring')
    if args.annotations and digest(Path(args.annotations).read_text())!=annotation_hash:
        raise ValueError('annotations changed during scoring')
    result['manifest_sha256']=digest(raw)
    result['schedule']=args.schedule
    result['session_id']=manifest['session_id'];result['compaction_id']=manifest['compaction_id']
    result['source_prefix_sha256']=manifest['source_prefix_sha256']
    if args.annotations:
        result['annotations_sha256']=annotation_hash
    write_private(args.output,result)
    print(json.dumps({'ok':True,'result':str(Path(args.output).resolve()),'status':result.get('status'),
                      'scored':result.get('scored'),'total':result.get('total'),'sha256':digest(Path(args.output).read_text())}))


def consult_command(args):
    manifest=read_json(args.manifest)
    result=consult(manifest,args.block)
    print(json.dumps(result,ensure_ascii=False,indent=2))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('snapshot',help='CPU only: canonical projection and 1K/2K/4K manifests')
    p.add_argument('--session',required=True);p.add_argument('--compaction',required=True)
    p.add_argument('--out',required=True);p.add_argument('--sizes',type=int,nargs='+',default=[1000,2000,4000])
    p.add_argument('--overlap',type=int,default=128);p.add_argument('--no-thinking',action='store_true')
    p.add_argument('--max-bytes',type=int,default=512*1024*1024)
    p.add_argument('--pi-package');p.add_argument('--vcc-package');p.set_defaults(run=snapshot_command)
    p=sub.add_parser('score',help='Explicit local scoring, no Pi hook')
    p.add_argument('--manifest',required=True);p.add_argument('--server',required=True)
    p.add_argument('--timeout',type=float,default=90);p.add_argument('--max-prompt-tokens',type=int,default=16384)
    p.add_argument('--max-blocks',type=int);p.add_argument('--annotations');p.add_argument('--output',required=True)
    p.add_argument('--schedule',choices=['spread','chronological'],default='spread')
    p.set_defaults(run=score_command)
    p=sub.add_parser('consult',help='Read exact private snapshot source spans, with safe angle escapes')
    p.add_argument('--manifest',required=True);p.add_argument('--block',required=True);p.set_defaults(run=consult_command)
    p=sub.add_parser('evaluate',help='Compare ranking to frozen span annotations at a fixed consultation budget')
    p.add_argument('--manifest',required=True);p.add_argument('--scores',required=True)
    p.add_argument('--annotations',required=True);p.add_argument('--output',required=True)
    def evaluate_command(args):
        from .evaluation import evaluate
        m=read_json(args.manifest);s=read_json(args.scores);a=read_json(args.annotations)
        if s.get('manifest_sha256')!=digest(Path(args.manifest).read_text()):raise ValueError('scores/manifest binding mismatch')
        if s.get('annotations_sha256')!=digest(Path(args.annotations).read_text()):raise ValueError('scores/annotation freeze binding mismatch')
        result=evaluate(m,s,a);write_private(args.output,result)
        print(json.dumps(result,indent=2))
    p.set_defaults(run=evaluate_command)
    args=parser.parse_args(argv)
    try:
        args.run(args)
    except (ValueError,OSError,KeyError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({'ok':False,'error':str(exc)[:1600]}),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
