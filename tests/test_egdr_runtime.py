import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from asset_runtime import AssetRuntime, Conflict
from asset_worker import pass_once
from research.mock_provider import MockProvider


class StateRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'project.db'
        self.runtime = AssetRuntime(self.path)
        self.addCleanup(lambda: self.runtime.close())
        self.backend = MockProvider()
        self.runtime.initialize({
            'assets': [dict(id=shot, scene=shot, kind='video',
                            spec=dict(template=template, reads=list(values), values=values, cost=1))
                       for shot, template, values in [
                           ('S1', 'Source {{press.color}}', {'press.color': 'amber'}),
                           ('S2', 'Shared {{press.color}}', {'press.color': 'amber'}),
                           ('S3', 'Independent', {}),
                           ('S4', 'Reference consumer', {}),
                           ('S5', 'Order-only consumer', {}),
                       ]],
            'edges': [dict(parent='S2', child='S4', kind='reference'),
                      dict(parent='S2', child='S5', kind='order')],
        })
        self.drain()

    @staticmethod
    def build(inputs, _):
        return {'5': {'inputs': {'prompt': json.dumps(inputs)}}}

    def step(self, cap=8):
        return pass_once(self.runtime, self.backend, self.build, max_submit=cap)

    def drain(self):
        for _ in range(8):
            events = self.step()
            self.assertTrue(all(event['action'] in ('submitted', 'complete') for event in events), events)
            if self.runtime.snapshot()['timeline']:
                return
        self.fail('mock publication did not finish')

    def edit(self, **overrides):
        return dict(dict(id='blue', expected_revision=1, target='S1',
                         key='press.color', value='blue'), **overrides)

    def evidence(self, child):
        source = {row['asset']: row for row in self.runtime.snapshot()['timeline']}
        return dict(child=child, source_input_hash=source[child]['input_hash'],
                    evidence_sha256='a' * 64)

    def test_edit_persists_edge_and_rejects_stale_completion_after_restart(self):
        before = {row['asset']: row for row in self.runtime.snapshot()['timeline']}
        event = self.edit(state_repairs=[self.evidence('S2')])
        plan = self.runtime.edit(event)
        self.assertEqual(plan['regenerate'], ['S1', 'S2', 'S4'])
        self.assertEqual(plan['state_changed'], ['S1', 'S2'])
        self.assertEqual(plan['state_repairs'],
                         [dict(parent='S1', child='S2', kind='state', keys=['press.color'])])
        self.assertEqual(self.runtime.edit(event), plan)
        self.step(cap=1)
        self.step(cap=1)
        obsolete = next(job for job in self.runtime.active() if job['asset'] == 'S2')
        self.assertEqual(json.loads(obsolete['input_json'])['spec']['values']['press.color'], 'blue')
        self.runtime.close()
        self.runtime = AssetRuntime(self.path)
        red = self.runtime.edit(self.edit(id='red', expected_revision=2, value='red'))
        self.assertEqual(red['regenerate'], plan['regenerate'])
        self.assertIn(dict(job=obsolete['id'], action='complete', result='stale'), self.step())
        self.drain()
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot['revision'], 3)
        timeline = {row['asset']: row for row in snapshot['timeline']}
        self.assertEqual(timeline['S3'], before['S3'])
        self.assertEqual(timeline['S5'], before['S5'])
        jobs = {(job['asset'], job['revision']): job for job in snapshot['jobs']}
        s2 = json.loads(jobs['S2', 3]['input_json'])
        s4 = json.loads(jobs['S4', 3]['input_json'])
        self.assertEqual(s2['spec']['values']['press.color'], 'red')
        self.assertEqual(s2['dependencies'][0]['values'], {'press.color': 'red'})
        self.assertEqual(s4['dependencies'][0]['revision'], 3)
        self.assertEqual(s4['dependencies'][0]['input_hash'], timeline['S2']['input_hash'])
        self.assertEqual([r[0] for r in self.runtime.db.execute(
            'SELECT revision FROM vf_timelines ORDER BY revision')], [1, 3])

    def test_invalid_repairs_do_not_advance_or_modify_project(self):
        original = self.runtime.snapshot()
        original_edges = self.runtime._project()['edges']
        for invalid in (
            dict(self.evidence('S2'), source_input_hash='b' * 64),
            self.evidence('S3'),
            self.evidence('S1'),
            dict(self.evidence('S2'), kind='reference'),
        ):
            with self.subTest(invalid=invalid), self.assertRaises((Conflict, ValueError)):
                self.runtime.edit(self.edit(state_repairs=[self.evidence('S2'), invalid]))
            self.assertEqual(self.runtime.snapshot(), original)
            self.assertEqual(self.runtime._project()['edges'], original_edges)
        with self.assertRaises(Conflict):
            self.runtime.edit(self.edit(state_repairs=[
                dict(self.evidence('S2'), source_input_hash='b' * 64)
            ]))

    def test_deferred_repair_blocks_publish_then_regenerates_only_the_delta(self):
        source = {row['asset']: row for row in self.runtime.snapshot()['timeline']}
        request = self.evidence('S2')
        first = self.edit(evidence_candidates=['S2'])
        self.assertEqual(self.runtime.edit(first)['regenerate'], ['S1'])
        self.step()
        self.step()
        anchor = next(a for a in self.runtime.snapshot()['assets'] if a['id'] == 'S1')
        self.assertIsNotNone(anchor['output'])
        self.assertIsNone(self.runtime.snapshot()['timeline'])
        self.runtime.close()
        self.runtime = AssetRuntime(self.path)
        with self.assertRaises(Conflict):
            self.runtime.resolve_evidence(2, 'S2', 'b' * 64, 'a' * 64, 'match')
        result = self.runtime.resolve_evidence(2, decision='match', **request)
        self.assertEqual(result['regenerate'], ['S2', 'S4'])
        self.assertEqual(self.runtime.resolve_evidence(2, decision='match', **request), result)
        with self.assertRaises(Conflict):
            self.runtime.resolve_evidence(2, decision='distinct', **request)
        self.drain()
        snap = self.runtime.snapshot()
        self.assertEqual(next(a for a in snap['assets'] if a['id'] == 'S1'), anchor)
        timeline = {row['asset']: row for row in snap['timeline']}
        self.assertEqual(timeline['S3'], source['S3'])
        self.assertEqual(timeline['S5'], source['S5'])
        job = next(j for j in snap['jobs'] if j['asset'] == 'S4' and j['revision'] == 2)
        self.assertEqual(json.loads(job['input_json'])['dependencies'][0]['input_hash'],
                         timeline['S2']['input_hash'])

    def test_new_revision_rejects_late_evidence_and_late_render(self):
        request = self.evidence('S2')
        self.runtime.edit(self.edit(evidence_candidates=['S2']))
        self.runtime.resolve_evidence(2, decision='match', **request)
        self.step(cap=1)
        self.step(cap=1)
        obsolete = next(job for job in self.runtime.active() if job['asset'] == 'S2')
        self.runtime.close()
        self.runtime = AssetRuntime(self.path)
        self.runtime.edit(self.edit(id='red', expected_revision=2, value='red', evidence_candidates=['S2']))
        with self.assertRaises(Conflict):
            self.runtime.resolve_evidence(2, decision='match', **request)
        result = self.runtime.resolve_evidence(3, decision='fallback', **request)
        self.assertEqual(result['regenerate'], ['S2', 'S4'])
        self.assertIn(dict(job=obsolete['id'], action='complete', result='stale'), self.step())
        self.drain()
        self.assertEqual([r[0] for r in self.runtime.db.execute(
            'SELECT revision FROM vf_timelines ORDER BY revision')], [1, 3])
        fresh = next(job for job in self.runtime.snapshot()['jobs'] if job['asset'] == 'S2' and job['revision'] == 3)
        self.assertEqual(json.loads(fresh['input_json'])['spec']['values']['press.color'], 'red')

    def test_unknown_does_not_release_barrier_and_new_negative_expires_prior_match(self):
        initial = self.evidence('S2')
        self.runtime.edit(self.edit(evidence_candidates=['S2']))
        with self.assertRaises(Conflict):
            self.runtime.edit(self.edit(id='unsafe', expected_revision=2, value='red'))
        with self.assertRaises(ValueError):
            self.runtime.resolve_evidence(2, decision='unknown', **initial)
        self.runtime.resolve_evidence(2, decision='match', **initial)
        self.drain()
        before = {row['asset']: row for row in self.runtime.snapshot()['timeline']}
        new = self.evidence('S2')
        self.runtime.edit(self.edit(id='red', expected_revision=2, value='red', evidence_candidates=['S2']))
        self.assertEqual(self.runtime.resolve_evidence(3, decision='distinct', **new)['regenerate'], [])
        self.drain()
        after = {row['asset']: row for row in self.runtime.snapshot()['timeline']}
        self.assertEqual(after['S2'], before['S2'])
        self.assertEqual(after['S4'], before['S4'])


if __name__ == '__main__':
    unittest.main()
