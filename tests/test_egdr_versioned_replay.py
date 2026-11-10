import copy
import json
import unittest

from research.dependency_probe.replay_versioned_egdr import FIXTURES, POLICIES, replay, run


class VersionedEvidenceReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = FIXTURES / 'egdr_versioned_replay_v1.json'
        cls.protocol = json.loads(fixture.read_text(encoding='utf-8'))
        cls.report = run(cls.protocol, FIXTURES)
        cls.rows = {(row['trace'], row['policy']): row for row in cls.report['rows']}

    def test_shared_traces_match_online_worklist(self):
        for trace in self.protocol['traces']:
            trace_id = trace['id']
            with self.subTest(trace=trace_id):
                delta = self.rows[trace_id, 'egdr_delta']
                control = self.rows[trace_id, 'online_worklist']
                wait = self.rows[trace_id, 'wait_worklist']
                self.assertEqual(delta, dict(control, policy='egdr_delta'))
                for row in (delta, control, wait):
                    self.assertTrue(row['correct_publication'])
                    self.assertEqual((row['missed'], row['excess']), ([], []))
                self.assertLess(delta['final_latency_ticks'], wait['final_latency_ticks'])

    def test_late_match_only_adds_consumer_and_reference_descendant(self):
        events = self.rows['late-match', 'egdr_delta']['events']
        match = next(row for row in events if row['action'] == 'evidence')
        self.assertEqual(match['added'], ['S2', 'S5'])
        complete_s2 = next(row for row in events if row['action'] == 'complete' and row['shot'] == 'S2')
        submit_s2 = next(row for row in events if row['action'] == 'submit' and row['shot'] == 'S2')
        submit_s5 = next(row for row in events if row['action'] == 'submit' and row['shot'] == 'S5')
        self.assertGreaterEqual(submit_s5['tick'], complete_s2['tick'])
        self.assertEqual(submit_s5['references']['S2'], submit_s2['fingerprint'])

    def test_unknown_cannot_release_publication_and_decoy_is_reused(self):
        trace = copy.deepcopy(self.protocol['traces'][1])
        trace['evidence'].pop()  # Remove the distinct-instance confirmation.
        for policy in POLICIES:
            with self.subTest(policy=policy):
                blocked = replay(self.protocol, trace, policy, self.report['source_sha256'])
                self.assertFalse(blocked['published'])
                row = self.rows[trace['id'], policy]
                self.assertEqual(row['publications'][0]['regenerated'], ['S1', 'S2', 'S5'])
                self.assertFalse(any(event['action'] == 'submit' and event['shot'] == 'S3'
                                     for event in row['events']))

    def test_new_edit_rejects_old_verdict_and_wrong_source(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                row = self.rows['superseded-evidence', policy]
                self.assertEqual([event['result'] for event in row['events'] if event['action'] == 'evidence'],
                                 ['source_or_scope_mismatch', 'stale_revision', 'accepted'])
                self.assertEqual([item['revision'] for item in row['publications']], [3])
                self.assertEqual(row['stale_completions'], int(policy != 'wait_worklist'))

    def test_gold_cannot_change_execution(self):
        trace = dict(self.protocol['traces'][0], gold=['S3'])
        row = replay(self.protocol, trace, 'egdr_delta', self.report['source_sha256'])
        original = self.rows[trace['id'], 'egdr_delta']
        self.assertEqual(row['events'], original['events'])
        self.assertFalse(row['correct_publication'])
