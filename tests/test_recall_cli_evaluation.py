import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from unittest.mock import patch

from recall_relevance.cli import main, schedule, write_private
from recall_relevance.evaluation import evaluate
from recall_relevance.extraction import digest


class CliEvaluationTests(unittest.TestCase):
    def data(self):
        source={'s':{'text':'abcdefghij'}}
        blocks=[]
        for i,(a,b) in enumerate([(0,4),(4,8),(8,10)]):
            blocks.append({'id':str(i),'refs':[i+1],'estimated_tokens':100,
                           'spans':[{'source_id':'s','start':a,'end':b}]})
        m={'compaction_id':'c','source_prefix_sha256':'prefix','sources':source,'blocks':blocks}
        a={'schema':1,'compaction_id':'c','source_prefix_sha256':'prefix',
           'consultation_budget_estimated_tokens':200,'digest_refs':[1],
           'items':[{'id':'joint','source_id':'s','start':2,'end':6,'text_sha256':digest('cdef'),'importance':'critical'},
                    {'id':'end','source_id':'s','start':8,'end':10,'text_sha256':digest('ij'),'importance':'useful'}]}
        s={'status':'complete','rows':[{'id':str(i),'status':'scored','score':p} for i,p in enumerate([.9,.8,.1])]}
        return m,a,s

    def test_schedule_is_full_permutation_and_not_relevance_filter(self):
        for n in range(50):
            blocks=[{'id':i} for i in range(n)]
            out=schedule(blocks,'spread')
            self.assertEqual(sorted(x['id'] for x in out),list(range(n)))
            if n>=3:self.assertEqual([x['id'] for x in out[:2]],[n-1,0])
            self.assertEqual(schedule(blocks,'chronological'),blocks)
        with self.assertRaises(ValueError):schedule([],'unknown')

    def test_equal_cost_and_full_span_not_any_overlap(self):
        m,a,s=self.data();r=evaluate(m,s,a)
        self.assertEqual(r['methods']['jev']['covered_items'],['joint'])
        self.assertEqual(r['methods']['recency']['covered_items'],['end'])
        self.assertEqual(r['scoring_source_coverage']['unique_fraction'],1)
        self.assertEqual(r['methods']['jev']['source_coverage']['unique_source_chars'],8)
        self.assertEqual(r['methods']['jev']['source_coverage']['redundant_source_chars'],0)
        self.assertEqual(r['methods']['jev']['critical_covered'],1)
        self.assertEqual(r['methods']['recency']['critical_covered'],0)
        self.assertEqual(r['methods']['digest_shaped_pointers']['covered_items'],[])
        for method in r['methods'].values():self.assertLessEqual(method['estimated_consultation_tokens'],200)

    def test_partials_use_identical_available_pool(self):
        m,a,s=self.data()
        s['status']='partial';s['rows'][0].update(status='not_attempted',score=None)
        r=evaluate(m,s,a)
        self.assertEqual(r['scored_blocks'],2)
        self.assertEqual(r['not_attempted_blocks'],1)
        self.assertEqual(r['scoring_source_coverage']['unique_fraction'],.6)
        self.assertEqual(r['methods']['jev']['miss_causes']['joint'],'not_fully_in_scored_pool')
        self.assertEqual(r['annotation_items_in_scored_pool'],['end'])
        for v in r['methods'].values():self.assertNotIn('0',v['selected_ids'])

    def test_bad_bindings_spans_and_scores_rejected(self):
        m,a,s=self.data()
        for mutate in [lambda x:x.update(compaction_id='future'),
                       lambda x:x.update(source_prefix_sha256='future'),
                       lambda x:x['items'][0].update(text_sha256='wrong'),
                       lambda x:x['items'][0].update(end=100),
                       lambda x:x['items'].append(copy.deepcopy(x['items'][0]))]:
            b=copy.deepcopy(a);mutate(b)
            with self.assertRaises(ValueError):evaluate(m,s,b)
        for value in (None,float('nan'),float('inf'),True,-.1,1.1):
            b=copy.deepcopy(s);b['rows'][0]['score']=value
            with self.assertRaises(ValueError):evaluate(m,b,a)
        b=copy.deepcopy(s);b['rows'].append(dict(b['rows'][0]))
        with self.assertRaises(ValueError):evaluate(m,b,a)

    def test_private_writes_no_clobber_or_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'new.json';write_private(p,{'x':1})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):write_private(p,{'x':2})
            link=Path(td)/'link';link.symlink_to(p)
            with self.assertRaises(FileExistsError):write_private(link,{'x':2})
            os.chmod(td,0o755)
            with self.assertRaisesRegex(ValueError,'0700'):write_private(Path(td)/'other',{})

    def test_existing_output_refused_before_inference(self):
        with tempfile.TemporaryDirectory() as td:
            m=Path(td)/'m.json';m.write_text('{}');out=Path(td)/'out';out.write_text('keep')
            with patch('recall_relevance.scoring.score_blocks',side_effect=AssertionError('inference forbidden')) as scorer,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['score','--manifest',str(m),'--output',str(out),'--server','http://127.0.0.1:1'])
            self.assertEqual(code,1);scorer.assert_not_called();self.assertEqual(out.read_text(),'keep')

    def test_cli_complete_score_evaluate_and_mutation_refusal(self):
        from test_recall_extraction import canonical_snapshot,fixture
        from test_recall_scoring import FakeBackend
        from recall_relevance.extraction import extract
        m=extract(canonical_snapshot(fixture(),'c1'),overlap_tokens=0)
        sid=next(iter(m['sources']));source=m['sources'][sid]
        a={'schema':1,'compaction_id':m['compaction_id'],'source_prefix_sha256':m['source_prefix_sha256'],
           'consultation_budget_estimated_tokens':8000,
           'items':[{'id':'constraint','source_id':sid,'start':0,'end':10,'text_sha256':digest(source['text'][:10]),'importance':'critical'}]}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);write_private(root/'m',m);write_private(root/'a',a)
            with FakeBackend() as backend,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['score','--manifest',str(root/'m'),'--annotations',str(root/'a'),'--server',backend.server,
                           '--max-prompt-tokens','100000','--output',str(root/'s')])
                self.assertEqual(code,0)
                code=main(['evaluate','--manifest',str(root/'m'),'--annotations',str(root/'a'),'--scores',str(root/'s'),'--output',str(root/'e')])
                self.assertEqual(code,0)
            e=json.loads((root/'e').read_text());self.assertEqual(e['score_status'],'complete')
            self.assertEqual(e['methods']['jev']['critical_covered'],1)
            def mutate(path,payload,n):
                if path.endswith('completions'):(root/'a').write_text((root/'a').read_text()+'\n')
            with FakeBackend(mutate) as backend,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['score','--manifest',str(root/'m'),'--annotations',str(root/'a'),'--server',backend.server,
                           '--max-prompt-tokens','100000','--output',str(root/'mutation-must-not-save')])
            self.assertEqual(code,1);self.assertFalse((root/'mutation-must-not-save').exists())

    def test_anchor_probe_cpu_preserves_candidate_prompts(self):
        import importlib.util
        from test_recall_extraction import canonical_snapshot,fixture
        from test_recall_scoring import FakeBackend
        from recall_relevance.extraction import extract
        entries=fixture();entries[2]['message']['content']='Alpha previous source. '*100
        m=extract(canonical_snapshot(entries,'c1'),target_tokens=256,overlap_tokens=0)
        p=Path(__file__).resolve().parents[1]/'scripts'/'recall_anchor_probe.py'
        spec=importlib.util.spec_from_file_location('offline_anchor_probe',p)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with FakeBackend() as backend:
            r=module.probe(m,server=backend.server,timeout_s=5,max_blocks=3,max_prompt_tokens=100000)
        calls=[body for path,body in backend.calls if path.endswith('completions')]
        self.assertEqual(len(calls),4)
        self.assertNotIn('logprobs',calls[0])
        for body in calls[1:]:
            self.assertEqual(body['logprobs'],20)
            self.assertTrue(body['prompt'].startswith(calls[0]['prompt']))
        self.assertEqual(r['scored'],3)
        self.assertLess(r['global_elapsed_s'],5)
        self.assertEqual([row['score'] for row in r['rows'][:3]],[.75]*3)

    def test_evaluate_cli_requires_prebound_annotation_hash(self):
        m,a,s=self.data()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name,value in [('manifest',m),('annotations',a)]:write_private(root/name,value)
            s['manifest_sha256']=digest((root/'manifest').read_text());s['annotations_sha256']='wrong'
            write_private(root/'scores',s)
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['evaluate','--manifest',str(root/'manifest'),'--scores',str(root/'scores'),'--annotations',str(root/'annotations'),'--output',str(root/'result')])
            self.assertEqual(code,1);self.assertFalse((root/'result').exists())


if __name__=='__main__':unittest.main()
