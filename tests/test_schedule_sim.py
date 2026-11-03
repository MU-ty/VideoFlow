from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from research.schedule_sim import Policy,simulate
from research.schedule_traces import suite
from research.run_schedule_benchmark import candidates,run


def tiny(events,cost=90):
    return dict(id='analytic',family='test',seed=42,readiness='independent',max_inflight=2,
        story=dict(scenes=[dict(id='A',cost=cost,reads=['x'])],edges=[]),
        events=[dict(at=t,edit=dict(target='A',key='x',scope='local',value=v)) for t,v in events])


class ScheduleTests(unittest.TestCase):
    def test_one_asset_analytic_completion(self):
        r=simulate(tiny([(0,'blue')]),Policy('immediate'),audit=True)
        self.assertEqual(r['submission_intents'],1)
        self.assertAlmostEqual(r['final_visible_at'],r['jobs'][0]['service'])
        self.assertEqual(r['unconsumed_service_seconds'],0)
        self.assertTrue(r['final_correct'])

    def test_burst_coalescing_and_obsolete_drain(self):
        t=tiny([(0,'red'),(1,'black'),(2,'blue')])
        a=simulate(t,Policy('immediate'),audit=True);b=simulate(t,Policy('fixed',wait=10),audit=True)
        self.assertEqual(a['submission_intents'],3);self.assertEqual(b['submission_intents'],1)
        self.assertEqual(a['final_input_keys'],b['final_input_keys'])
        self.assertGreater(a['unconsumed_service_seconds'],0)
        self.assertEqual(b['jobs'][0]['submitted'],12)
        self.assertTrue(all(j['done'] for j in a['jobs']))

    def test_undo_reuses_old_result_and_keeps_remote_slot(self):
        r=simulate(tiny([(0,'red'),(1,'black'),(2,'red')]),Policy('immediate'),audit=True)
        self.assertEqual(r['submission_intents'],2)
        self.assertTrue(r['jobs'][0]['used']);self.assertFalse(r['jobs'][1]['used'])
        self.assertLess(r['final_visible_at'],r['remote_drained_at']);self.assertEqual(r['remote_unresolved'],0)

    def test_prefix_decisions_do_not_see_future_arrivals(self):
        a=tiny([(0,'red'),(5,'black'),(100,'blue')]);b=tiny([(0,'red'),(5,'black'),(150,'green')])
        for p in [Policy('immediate'),Policy('fixed',wait=10),Policy('adaptive',wait=5)]:
            ra=simulate(a,p,audit=True);rb=simulate(b,p,audit=True)
            self.assertEqual([e for e in ra['events'] if e['at']<40],[e for e in rb['events'] if e['at']<40])

    def test_ordered_readiness_is_shared(self):
        t=tiny([(0,'blue')],5);t['readiness']='ordered'
        t['story']['scenes'] += [dict(id='B',cost=3,reads=['x']),dict(id='C',cost=2,reads=['x'])]
        t['events'][0]['edit']['scope']='global'
        for p in [Policy('immediate',order='latest'),Policy('fixed',wait=2,order='shortest'),Policy('adaptive',wait=3,order='stable')]:
            jobs=simulate(t,p,audit=True)['jobs']
            self.assertEqual([j['asset'] for j in jobs],['A','B','C'])
            self.assertTrue(all(jobs[i+1]['submitted']>=jobs[i]['finish'] for i in range(2)))

    def test_split_and_selection_locked_before_evaluation(self):
        a=suite(2);self.assertEqual(a,suite(2))
        self.assertFalse({t['seed'] for t in a['development']}&{t['seed'] for t in a['heldout']})
        self.assertEqual(len(candidates('fixed')),32);self.assertEqual(len(candidates('adaptive')),32)
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'probe';seen=[]
            def wrapped(trace,policy,audit=False):
                if trace['id'].startswith('heldout'):
                    self.assertTrue((out/'selection.json').exists());seen.append((out/'selection.json').read_bytes())
                return simulate(trace,policy,audit)
            with patch('research.run_schedule_benchmark.simulate',side_effect=wrapped):r=run(out,1)
            self.assertEqual(r['heldout_traces'],5);self.assertTrue(all(s==seen[0] for s in seen))
            self.assertIsNone(r['gpu_seconds'])

    def test_bad_inputs_fail(self):
        with self.assertRaises(ValueError):simulate(tiny([(2,'a'),(1,'b')]),Policy('immediate'))
        with self.assertRaises(ValueError):simulate(tiny([(0,'a')]),Policy('fixed',wait=-1))
        t=tiny([(0,'a')]);t['story']['edges']=[dict(parent='A',child='A',kind='reference')]
        with self.assertRaises(ValueError):simulate(t,Policy('immediate'))
