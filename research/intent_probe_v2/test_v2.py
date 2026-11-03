import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run as probe
from cases import suite

class IntentV2Tests(unittest.TestCase):
    def test_manually_checked_new_cases(self):
        cases={c['id']:c for c in suite()}
        self.assertEqual(len(cases),16)
        self.assertEqual(sum(c['split']=='dev' for c in cases.values()),4)
        self.assertEqual(sum(c['split']=='eval' for c in cases.values()),12)
        checks=[('eval-subtitle-history','S5','subtitle','起航'),('eval-light-exception','S5','light','sunset'),('eval-color-initial','S3','color','blue'),('eval-undo-undo','S1','color','red'),('eval-noop','S5','color','blue'),('eval-copy-revert','S3','color','black'),('eval-two-rounds','S3','subtitle','开场'),('eval-two-rounds','S3','color','blue'),('eval-after-rollback','S5','color','blue'),('eval-multifield','S1','subtitle','夜行'),('eval-global-slice','S5','color','black')]
        for name,shot,key,value in checks:self.assertEqual(cases[name]['turns'][-1]['expected'][shot][key],value)

    def test_protocol_tamper_and_gold_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);p=probe.prepare(out);probe.prepare(out);probe.load_protocol(out)
            for c in p['cases']:
                for n in range(len(c['turns'])):
                    for arm in probe.ARMS:
                        m=probe.messages(c,n,arm,[],c['initial'],{})
                        public=json.loads(m[-1]['content'])
                        self.assertFalse(set(public)&{'expected','annotation_ops','addressed','family','split'})
                        self.assertEqual('self_generated_memory' in public,arm in ['notes','ledger'])
            path=out/'protocol.json';path.write_text(path.read_text().replace('black','white',1))
            with self.assertRaises(ValueError):probe.load_protocol(out)

    def test_api_payload_no_gold_or_key_persisted(self):
        c=suite()[0];payloads=[]
        def fake_call(base,model,key,msgs,max_tokens,timeout):
            public=json.loads(msgs[-1]['content']);payloads.append(public)
            step=c['turns'][public['turn']-1]
            return json.dumps({'state':step['expected'],'memory':{}}),{'total_tokens':1},model,None
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            with patch.object(probe,'suite',return_value=[c]):
                probe.prepare(out)
                args=argparse.Namespace(output=out,split='dev',model='fake-unit-only',base_url='http://127.0.0.1:8000/v1',repeats=1,max_tokens=2048,timeout=1)
                with patch.dict(os.environ,{'INTENT_API_KEY':'unit-secret'}),patch.object(probe,'call_model',side_effect=fake_call),contextlib.redirect_stdout(io.StringIO()):
                    probe.run(args);probe.score(args)
                report=json.loads((out/'dev-run/report.txt').read_text())
                self.assertEqual(report['paired_ledger_vs_baselines']['notes'],{'tie':1})
                for f in (out/'dev-run').glob('*.json'):self.assertNotIn('unit-secret',f.read_text())
                with patch.dict(os.environ,{'INTENT_API_KEY':'unit-secret'}):
                    with self.assertRaises(ValueError):probe.run(args)
        self.assertEqual(len(payloads),len(c['turns'])*4)

    def test_invalid_output_and_incomplete_denominator(self):
        with self.assertRaises(ValueError):probe.parse_output('not JSON',suite()[0]['initial'])
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);p=probe.prepare(out);d=out/'eval-run';d.mkdir()
            (d/'configuration.json').write_text(json.dumps({'protocol_hash':probe.fingerprint(p),'split':'eval','repeats':1,'evidence':'synthetic_unit_test'}))
            c=next(c for c in p['cases'] if c['split']=='eval')
            for arm in probe.ARMS:
                rows=[{'prediction':s['expected'],'status':'returned'} for s in c['turns']]
                if arm=='ledger':rows[-1]={'status':'returned','format_error':'invalid'}
                (d/(arm+'.json')).write_text(json.dumps({'case':c['id'],'repeat':0,'arm':arm,'status':'complete','turns':rows}))
            with contextlib.redirect_stdout(io.StringIO()):probe.score(argparse.Namespace(output=out,split='eval'))
            r=json.loads((d/'report.txt').read_text())
            self.assertEqual(r['metrics']['state']['sequence_success_rate'],1/12)
            for b in ['state','notes','undo']:
                self.assertEqual(r['paired_ledger_vs_baselines'][b],{'ledger_loss':1,'missing_pair':11})

if __name__=='__main__':unittest.main()
