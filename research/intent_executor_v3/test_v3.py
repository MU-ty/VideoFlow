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
            execute([dict(op='set',shot='S2',field='color',value='blue'),
                     dict(op='restore_before',shot='S4',field='color',turn=9)],state,[])
        self.assertEqual(state,original)
        result=execute([dict(op='set',shot='S2',field='color',value='blue'),
                        dict(op='copy_current',shot='S4',field='color',source='S2')],state,[])
        self.assertEqual(result['S4']['color'],'white')
        with self.assertRaises(OperationError):
            execute([dict(op='restore_before',shot='S4',field='color',turn=True)],state,[{'before':state}])
        with self.assertRaises(OperationError):
            execute([dict(op='copy_current',shot='S4',field='color',source='S2')],state,[],patch_only=True)
        with self.assertRaises(ValueError): decode('{"ops":[],"ops":[]}','patch',state,[])

    def test_equal_information_and_gold_isolation(self):
        case=suite()[0]; poisoned=copy.deepcopy(case)
        poisoned['turns'][0]['expected']='DO_NOT_LEAK'
        poisoned['turns'][0]['annotation_ops']='DO_NOT_LEAK'
        msgs=[run.messages(poisoned,0,a,[],case['initial']) for a in run.ARMS]
        self.assertEqual(len({m[1]['content'] for m in msgs}),1)
        self.assertNotIn('DO_NOT_LEAK',json.dumps(msgs))

    def test_fake_end_to_end_and_missing_pair(self):
        # Synthetic perfect replies test bookkeeping, NOT language-model accuracy.
        def fake(base,model,key,msgs,max_tokens,timeout):
            public=json.loads(msgs[1]['content']); t=public['turn']-1
            c=next(c for c in suite() if c['initial']==public['initial'] and t<len(c['turns']) and
                   c['turns'][t]['instruction']==public['instruction'])
            step=c['turns'][t]; sys=msgs[0]['content']
            if sys.endswith(run.POLICIES['state']): out={'state':step['expected']}
            elif sys.endswith(run.POLICIES['patch']):
                out={'ops':[dict(op='set',shot=s,field=f,value=step['expected'][s][f]) for s,f in step['addressed']]}
            else: out={'ops':step['annotation_ops']}
            return json.dumps(out),dict(prompt_tokens=10,completion_tokens=10,total_tokens=20),model,None
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(output=Path(temp),split='eval',base_url='http://127.0.0.1:11434/v1',
                                 model='FAKE',repeats=1,max_tokens=2048,timeout=600)
            run.prepare(args.output)
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
