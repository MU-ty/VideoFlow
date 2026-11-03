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


class ProbeTests(unittest.TestCase):
    def test_manual_partial_restore_and_copy(self):
        c={c['id']:c for c in suite()}
        x=c['greenhouse-04']['turns'][-1]['expected']['S4']
        self.assertEqual(x,{'color':'blue','light':'day','subtitle':'停下'})
        self.assertEqual(c['harbor-08']['turns'][-1]['expected']['S2']['color'],'red')
        final=c['harbor-06']['turns'][-1]['expected']
        self.assertEqual([final[s]['color'] for s in ['S2','S4','S6']],['red','blue','blue'])
        self.assertTrue(all(v['light']=='day' and v['color'] in ['blue','black'] for v in c['greenhouse-10']['turns'][-1]['expected'].values()))

    def test_schema_and_no_gold_in_prompt(self):
        for c in suite():
            for t in range(len(c['turns'])):
                for arm in probe.ARMS:
                    m=probe.messages(c,t,arm,[],c['initial'],{})
                    public=json.loads(m[-1]['content'])
                    self.assertFalse({'expected','addressed','annotation_ops','family','split'} & set(public))
                    probe.validate_state(c['turns'][t]['expected'],c['initial'])
        with self.assertRaises(ValueError):probe.parse_output('```json\n{}\n```',suite()[0]['initial'])

    def test_freeze_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp); p=probe.prepare(out);probe.prepare(out)
            self.assertEqual(len(p['cases']),30)
            self.assertEqual(sum(len(c['turns']) for c in p['cases']),114)
            path=out/'protocol.json';path.write_text(path.read_text().replace('停下','停止'))
            with self.assertRaises(ValueError):probe.load_protocol(out)

    def test_scoring_failures_and_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);p=probe.prepare(out);d=out/'dev-run';d.mkdir()
            (d/'configuration.json').write_text(json.dumps({'protocol_hash':probe.fingerprint(p),'split':'dev','repeats':1,'evidence':'synthetic_unit_test'}))
            c=p['cases'][0]
            for arm in probe.ARMS:
                rows=[{'prediction':s['expected'],'status':'returned','latency_seconds':0} for s in c['turns']]
                if arm=='ledger': rows[-1]={'status':'returned','format_error':'bad json'}
                (d/(arm+'.json')).write_text(json.dumps({'case':c['id'],'arm':arm,'repeat':0,'status':'complete','turns':rows}))
            with contextlib.redirect_stdout(io.StringIO()):probe.score(argparse.Namespace(output=out,split='dev'))
            r=json.loads((d/'report.txt').read_text())
            self.assertEqual(r['metrics']['state']['sequence_success_rate'],0.1)
            self.assertEqual(r['paired_ledger_vs_state']['ledger_loss'],1)
            self.assertEqual(r['paired_ledger_vs_state']['missing_pair'],9)
            self.assertGreater(r['metrics']['ledger']['counts']['preservation_violations'],0)

    def test_transport_and_request_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);probe.prepare(out)
            args=argparse.Namespace(output=out,split='dev',model='unit-test',base_url='http://127.0.0.1:8000/v1',repeats=1,max_tokens=99,timeout=1)
            with patch.dict(os.environ,{'INTENT_API_KEY':'test-only'}), patch.object(probe,'call_model',side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):probe.run(args)
                with self.assertRaises(ValueError):probe.run(args)
            f=next((out/'dev-run').glob('costume*.json'));r=json.loads(f.read_text())
            self.assertEqual(r['status'],'interrupted')
            self.assertEqual(r['turns'][0]['status'],'transport_error')
            self.assertNotIn('test-only',f.read_text())

if __name__=='__main__': unittest.main()
