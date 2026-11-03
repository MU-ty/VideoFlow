import unittest
from research.audit_schedule_runtime import audit
from research.schedule_sim_v2 import Policy,simulate
from research.run_schedule_diagnosis import suite,context
from tests.test_schedule_sim import tiny

class DiagnosisTests(unittest.TestCase):
    def test_actual_runtime_probes(self):
        r=audit();self.assertEqual(r['undo']['old_result_outcome'],'stale')
        self.assertEqual(r['direct_parent_readiness']['pending_after_A_C_invalidated'],['A','C'])

    def test_revision_mode_does_not_rebind_undo(self):
        t=tiny([(0,'red'),(1,'black'),(2,'red')]);t['cache_mode']='revision'
        r=simulate(t,Policy('immediate'),audit=True)
        self.assertEqual(r['submission_intents'],3)
        self.assertFalse(r['jobs'][0]['used']);self.assertFalse(r['jobs'][1]['used']);self.assertTrue(r['jobs'][2]['used'])

    def test_direct_parent_not_prefix(self):
        t=tiny([(30,'blue')],5);t['readiness']='runtime_edges';t['cache_mode']='revision'
        t['story']['scenes']=[dict(id='A',cost=5,reads=['x']),dict(id='B',cost=5,reads=[]),dict(id='C',cost=5,reads=['x'])]
        t['story']['edges']=[dict(parent='A',child='B',kind='order'),dict(parent='B',child='C',kind='order')]
        t['events'][0]['edit']['scope']='global'
        r=simulate(t,Policy('immediate'),audit=True)
        later=[j for j in r['jobs'] if j['submitted']>=30]
        self.assertEqual([j['asset'] for j in later],['A','C']);self.assertEqual(later[0]['submitted'],later[1]['submitted'])

    def test_split_balanced_and_fresh(self):
        w=suite(4)
        self.assertFalse({t['seed'] for t in w['development']}&{t['seed'] for t in w['heldout']})
        self.assertTrue(all(t['seed']>=61000 for ts in (w['development'],w['heldout']) for t in ts))
        for ts in (w['development'],w['heldout']):self.assertEqual(len({context(t) for t in ts}),4)
        with self.assertRaises(ValueError):suite(5)

    def test_selection_locked_and_hindsight_is_bounded(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from research.run_schedule_diagnosis import run
        seen=[]
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'run'
            def wrapped(t,p,audit=False):
                if t['id'].startswith('heldout'):
                    self.assertTrue((out/'selection.json').exists());seen.append((out/'selection.json').read_bytes())
                return simulate(t,p,audit)
            with patch('research.run_schedule_diagnosis.simulate',side_effect=wrapped):r=run(out,4)
            self.assertTrue(all(x==seen[0] for x in seen))
            self.assertEqual(r['heldout_traces'],20)
            for oracle,fixed in zip(r['rows']['hindsight_64'],r['rows']['context_fixed']):
                self.assertLessEqual(oracle['objective'],fixed['objective']+1e-8)
