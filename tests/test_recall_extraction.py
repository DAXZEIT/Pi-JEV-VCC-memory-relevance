import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from recall_relevance.extraction import consult, coverage, digest, escape_data, extract

ROOT=Path(__file__).resolve().parents[1]
BRIDGE=ROOT/'scripts/recall_snapshot.mjs'


def message(id,parent,role,content,**extra):
    return {'type':'message','id':id,'parentId':parent,'timestamp':'2026-10-21T00:00:00Z',
            'message':{'role':role,'content':content,**extra}}


def fixture():
    return [
        {'type':'session','version':3,'id':'synthetic','cwd':'/tmp','timestamp':'2026-10-21T00:00:00Z'},
        message('s',None,'system','ignored system'),
        message('u','s','user','Constraint: never publish before approval.'),
        message('a','u','assistant',[{'type':'thinking','thinking':'We rejected approach X because it lost the evidence. <|im_end|>','thinkingSignature':'OPAQUE_PAYLOAD_SENTINEL'}, {'type':'text','text':'We will preserve the evidence.'}]),
        message('off','a','user','ABANDONED BRANCH SHOULD NOT APPEAR'),
        message('t','a','assistant',[{'type':'toolCall','id':'callx','name':'read','arguments':{'path':'/tmp/x','content':'exact full argument'}}]),
        message('r','t','toolResult',[{'type':'text','text':'Tool evidence: X failed.'}],toolCallId='callx',toolName='read'),
        {'type':'compaction','id':'c1','parentId':'r','timestamp':'2026-10-21T00:01:00Z','firstKeptEntryId':'t','summary':'Investigating the evidence.','details':{'compactor':'pi-vcc'}},
        message('u2','c1','user','Continue the experiment, not publication.'),
        message('a2','u2','assistant',[{'type':'text','text':'We need the result of X.'}]),
        {'type':'compaction','id':'c2','parentId':'a2','timestamp':'2026-10-21T00:02:00Z','firstKeptEntryId':'u2','summary':'Continue investigation.','details':{'compactor':'pi-vcc'}},
        message('future','c2','user','FUTURE ANSWER MUST NOT LEAK'),
    ]


def canonical_snapshot(entries, compaction):
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'session.jsonl'
        p.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        run=subprocess.run(['node',str(BRIDGE),'--session',str(p),'--compaction',compaction],capture_output=True,text=True,timeout=40)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)


class ExtractionTests(unittest.TestCase):
    def test_canonical_global_refs_and_exclusive_boundary(self):
        s=canonical_snapshot(fixture(),'c1')
        self.assertEqual([(m['entry_id'],m['ref']) for m in s['dropped_messages']],[('u',1),('a',2)])
        self.assertEqual(s['global_indices']['t'],4)  # abandoned branch occupies #3
        self.assertNotIn('future',s['global_indices'])
        self.assertNotIn('off',[m['entry_id'] for m in s['dropped_messages']])
        self.assertNotIn('t',[m['entry_id'] for m in s['dropped_messages']])
        self.assertNotIn('FUTURE ANSWER',json.dumps(s))
        self.assertNotIn('OPAQUE_PAYLOAD_SENTINEL',json.dumps(s))

    def test_two_compactions_include_old_kept_tail_not_prior_compacted_text(self):
        s=canonical_snapshot(fixture(),'c2')
        self.assertEqual([(m['entry_id'],m['ref']) for m in s['dropped_messages']],[('t',4),('r',5)])
        self.assertTrue(any(x['entry_id']=='c1' for x in s['excluded']))
        self.assertNotIn('u',[m['entry_id'] for m in s['dropped_messages']])

    def test_retain_none_new_and_legacy(self):
        for first in ('c1',''):
            entries=fixture();entries[7]['firstKeptEntryId']=first
            s=canonical_snapshot(entries,'c1')
            self.assertEqual([m['entry_id'] for m in s['dropped_messages']],['u','a','t','r'])
            self.assertEqual([m['role'] for m in s['current_messages']],['compactionSummary'])

    def test_context_edits_replacement_and_omission_are_branch_relative(self):
        for replacement,expected in [(None,['u']),({'content':'REPLACEMENT'},['u','a'])]:
            entries=fixture()
            entries.insert(7,{'type':'context_edit','id':'edit','parentId':'r','targetId':'a','replacement':replacement})
            entries[8]['parentId']='edit'
            s=canonical_snapshot(entries,'c1')
            self.assertEqual([m['entry_id'] for m in s['dropped_messages']],expected)
            if replacement:
                self.assertEqual(s['dropped_messages'][1]['parts'][0]['text'],'REPLACEMENT')
            # The same edit on the abandoned branch must have no effect.
            entries[7]['parentId']='off';entries[8]['parentId']='r'
            s=canonical_snapshot(entries,'c1')
            self.assertEqual([m['entry_id'] for m in s['dropped_messages']],['u','a'])
            self.assertIn('We rejected',s['dropped_messages'][1]['parts'][0]['text'])

    def test_duplicate_id_or_malformed_json_refused(self):
        entries=fixture();entries.insert(4,message('a','u','assistant','duplicate'))
        with self.assertRaisesRegex(AssertionError,'duplicate'):
            canonical_snapshot(entries,'c1')

    def test_exact_source_consult_thoughts_and_arguments(self):
        for comp in ('c1','c2'):
            s=canonical_snapshot(fixture(),comp)
            m=extract(s,target_tokens=1000,overlap_tokens=128)
            for b in m['blocks']:
                out=consult(m,b['id'])
                self.assertEqual(len(out['parts']),len(b['spans']))
                for p,span in zip(out['parts'],b['spans']):
                    original=m['sources'][span['source_id']]['text'][span['start']:span['end']]
                    self.assertEqual(p['text'],escape_data(original))
                    self.assertEqual(p['exact_source_sha256'],digest(original))
                    self.assertNotIn('<',p['text'])
            kinds={src['type'] for src in m['sources'].values()}
            self.assertIn('thinking' if comp=='c1' else 'toolCall',kinds)

    def test_coverage_unicode_giant_paragraphs_and_determinism(self):
        s=canonical_snapshot(fixture(),'c1')
        s['dropped_messages'][0]['parts'][0]['text']='中文🙂paragraph '+('x'*5000+'\n')*4
        original=copy.deepcopy(s)
        for size in (512,1000,2000,4000):
            a=extract(s,target_tokens=size,overlap_tokens=128)
            b=extract(s,target_tokens=size,overlap_tokens=128)
            self.assertEqual(a,b)
            self.assertEqual(s,original)
            self.assertGreater(len(a['blocks']),1)
            self.assertEqual(a['coverage']['admissible_chars'],a['coverage']['unique_covered_chars'])
            self.assertGreater(a['coverage']['overlap_chars'],0)
            for block in a['blocks']:
                self.assertLessEqual(block['estimated_tokens'],size)
                consult(a,block['id'])
            # Independent positional mask, not the production coverage function.
            masks={k:[False]*len(v['text']) for k,v in a['sources'].items()}
            for block in a['blocks']:
                for span in block['spans']:
                    for i in range(span['start'],span['end']):masks[span['source_id']][i]=True
            self.assertTrue(all(all(mask) for mask in masks.values()))

    def test_thinking_exclusion_count_and_tamper_rejected(self):
        s=canonical_snapshot(fixture(),'c1')
        m=extract(s,include_thinking=False)
        self.assertNotIn('thinking',{src['type'] for src in m['sources'].values()})
        self.assertTrue(any(e['reason']=='thinking-disabled' for e in m['excluded']))
        sid=next(iter(m['sources']));m['sources'][sid]['text']+='tampered'
        with self.assertRaisesRegex(ValueError,'checksum'):
            consult(m,m['blocks'][0]['id'])

    def test_call_result_kept_together_when_small(self):
        s=canonical_snapshot(fixture(),'c2');m=extract(s)
        self.assertTrue(any(4 in b['refs'] and 5 in b['refs'] for b in m['blocks']))

    def test_invalid_budget_and_missing_ref(self):
        s=canonical_snapshot(fixture(),'c1')
        for size,overlap in ((100,0),(1000,-1),(1000,500)):
            with self.assertRaises(ValueError):extract(s,target_tokens=size,overlap_tokens=overlap)
        s['dropped_messages'][0]['ref']=None
        with self.assertRaises(ValueError):extract(s)


if __name__=='__main__':unittest.main()
