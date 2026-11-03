import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import run
from cases import suite
from engine import execute, decode, OperationError
from schema import response_format,validate


class Tests(unittest.TestCase):
    def test_gold_independent_and_history_immutable(self):
        for case in suite():
            state=copy.deepcopy(case['initial']); history=[]
            for step in case['turns']:
                old=copy.deepcopy(state); old_history=copy.deepcopy(history)
                result=execute(step['annotation_ops'],state,history)
                self.assertEqual(result,step['expected'],(case['id'],step['instruction']))
                self.assertEqual(state,old); self.assertEqual(history,old_history)
                history.append({'before':old,'after':result}); state=result

    def test_transaction_and_snapshot_copy(self):
        state=suite()[0]['initial']; original=copy.deepcopy(state)
        with self.assertRaises(OperationError):
            execute([dict(op='set',shot='S21',field='color',value='blue'),
                     dict(op='restore_before',shot='S22',field='color',turn=9)],state,[])
        self.assertEqual(state,original)
        result=execute([dict(op='set',shot='S21',field='color',value='blue'),
                        dict(op='copy_current',shot='S22',field='color',source='S21')],state,[])
        self.assertEqual(result['S22']['color'],'red')
        with self.assertRaises(OperationError):
            execute([dict(op='restore_before',shot='S22',field='color',turn=True)],state,[{'before':state}])
        with self.assertRaises(OperationError):
            execute([dict(op='copy_current',shot='S22',field='color',source='S21')],state,[],patch_only=True)
        with self.assertRaises(ValueError): decode('{"ops":[],"ops":[]}','patch',state,[])

    def test_equal_information_and_gold_isolation(self):
        case=suite()[0]; poisoned=copy.deepcopy(case)
        poisoned['turns'][0]['expected']='DO_NOT_LEAK'
        poisoned['turns'][0]['annotation_ops']='DO_NOT_LEAK'
        msgs=[run.messages(poisoned,0,a,[],case['initial']) for a in run.ARMS]
        for arm in run.ARMS:
            self.assertEqual(response_format(arm,case['initial']),response_format(arm,poisoned['initial']))
        self.assertEqual(len({m[1]['content'] for m in msgs}),1)
        self.assertNotIn('DO_NOT_LEAK',json.dumps(msgs))

    def test_schemas_and_uniform_rejection(self):
        state=suite()[0]['initial']
        for arm in run.ARMS:
            fmt=response_format(arm,state)['json_schema']['schema']
            good={'state':state} if arm=='state' else {'ops':[]}
            validate(good,fmt)
            with self.assertRaises(ValueError): validate([],fmt)
            with self.assertRaises(ValueError): validate({**good,'history':[]},fmt)
        with self.assertRaises(ValueError):
            validate({'ops':[dict(op='restore_before',shot='S21',field='color',turn=1)]},response_format('patch',state)['json_schema']['schema'])
        validate({'ops':[dict(op='restore_before',shot='S21',field='color',turn=1)]},response_format('program',state)['json_schema']['schema'])

    def test_transport_sends_schema_and_records_finish(self):
        state=suite()[0]['initial']; fmt=response_format('state',state)
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'state':state})},'finish_reason':'stop'}],
                                        'model':'fake-return','usage':{'total_tokens':7}}).encode())
        with patch.object(run.urllib.request,'urlopen',return_value=response) as opened:
            result=run.call_model('http://localhost:11434/v1','FAKE','placeholder',[],2048,600,fmt)
        payload=json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['response_format'],fmt)
        self.assertEqual(payload['temperature'],0)
        self.assertEqual(result[-1],'stop')
        self.assertEqual(opened.call_count,1)

    def test_preflight_checks_and_no_fallback(self):
        def fake(base,model,key,msgs,max_tokens,timeout,fmt):
            def fill(s):
                if 'enum' in s:return s['enum'][0]
                if s['type']=='object':return {k:fill(v) for k,v in s['properties'].items()}
                if s['type']=='array':return []
                return '检查'
            return json.dumps(fill(fmt['json_schema']['schema'])),{},model,None,'stop'
        with tempfile.TemporaryDirectory() as tmp:
            args=SimpleNamespace(output=Path(tmp),model='FAKE',base_url='http://127.0.0.1:11434/v1',max_tokens=2048,timeout=600)
            run.prepare(args.output)
            with patch.dict(os.environ,{'INTENT_API_KEY':'placeholder'}), patch.object(run,'call_model',side_effect=fake) as calls, contextlib.redirect_stdout(io.StringIO()):
                run.preflight(args); self.assertEqual(calls.call_count,7)
                with self.assertRaises(FileExistsError):run.preflight(args)
            self.assertTrue(json.loads((args.output/'preflight.json').read_text())['passed'])
        with tempfile.TemporaryDirectory() as tmp:
            args.output=Path(tmp);run.prepare(args.output)
            with patch.dict(os.environ,{'INTENT_API_KEY':'placeholder'}), patch.object(run,'call_model',return_value=('[]',{},'FAKE',None,'stop')) as calls:
                with self.assertRaises(ValueError):run.preflight(args)
                self.assertEqual(calls.call_count,1)
            self.assertFalse(json.loads((args.output/'preflight.json').read_text())['passed'])

    def test_fake_end_to_end_and_missing_pair(self):
        # Synthetic perfect replies test bookkeeping, NOT language-model accuracy.
        def fake(base,model,key,msgs,max_tokens,timeout,fmt):
            public=json.loads(msgs[1]['content']); t=public['turn']-1
            c=next(c for c in suite() if c['initial']==public['initial'] and t<len(c['turns']) and
                   c['turns'][t]['instruction']==public['instruction'] and
                   [s['instruction'] for s in c['turns'][:t]]==[h['instruction'] for h in public['history']])
            step=c['turns'][t]; sys=msgs[0]['content']
            if sys.endswith(run.POLICIES['state']): out={'state':step['expected']}
            elif sys.endswith(run.POLICIES['patch']):
                out={'ops':[dict(op='set',shot=s,field=f,value=step['expected'][s][f]) for s,f in step['addressed']]}
            else: out={'ops':step['annotation_ops']}
            validate(out,fmt['json_schema']['schema'])
            return json.dumps(out),dict(prompt_tokens=10,completion_tokens=10,total_tokens=20),model,None,'stop'
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(output=Path(temp),split='eval',base_url='http://127.0.0.1:11434/v1',
                                 model='FAKE',repeats=1,max_tokens=2048,timeout=600)
            p=run.prepare(args.output)
            (args.output/'preflight.json').write_text(json.dumps({'passed':True,'configuration':run.capability_config(args,p)}))
            with patch.dict(os.environ,{'INTENT_API_KEY':'placeholder'}), patch.object(run,'call_model',side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                run.run(args); run.score(args)
                with self.assertRaises(ValueError): run.run(args)
            report=json.loads((args.output/'eval-run/report.txt').read_text())
            for arm in run.ARMS:
                self.assertEqual(report['metrics'][arm]['counts']['correct_sequences'],12)
            self.assertFalse(report['continuation_gate']['pass']) # ties fail
            next((args.output/'eval-run').glob('*-program.json')).unlink()
            with contextlib.redirect_stdout(io.StringIO()): run.score(args)
            report=json.loads((args.output/'eval-run/report.txt').read_text())
            self.assertFalse(report['continuation_gate']['complete'])
            self.assertEqual(report['metrics']['program']['sequence_success_rate'],11/12)
            (args.output/'protocol.sha256').write_text('tampered')
            with self.assertRaises(ValueError): run.load_protocol(args.output)


if __name__=='__main__': unittest.main()
