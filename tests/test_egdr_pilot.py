"""Regression checks for grounded review and hard-dependency closure."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from egdr import build_plan, confirm_negatives, grounded_verdict, public_case, recall_checklist, shot_sources


class EvidenceProbeTest(unittest.TestCase):
    def test_only_supplied_quotes_can_clear_a_shot(self):
        sources = {'metadata': 'The strap is in view.'}
        fabricated = json.dumps({
            'visibility': {'answer': 'no', 'source': 'observation', 'quote': 'No buckle visible.'},
            'identity': {'answer': 'yes', 'source': 'metadata', 'quote': 'strap is in view'},
        })
        self.assertEqual(grounded_verdict(fabricated, sources)['verdict'], 'uncertain')
        supported = json.dumps({
            'visibility': {'answer': 'no', 'source': 'metadata', 'quote': 'No buckle visible.'},
        })
        self.assertEqual(grounded_verdict(supported, {'metadata': 'No buckle visible.'})['verdict'],
                         'unaffected')
        self.assertEqual(grounded_verdict('not JSON', sources)['verdict'], 'uncertain')

    def test_second_pass_demotes_unconfirmed_negatives(self):
        first = grounded_verdict(json.dumps({
            'identity': {'answer': 'yes', 'source': 'observation', 'quote': 'A white can'},
            'scope': {'answer': 'no', 'source': 'observation', 'quote': 'stands upright'},
        }), {'observation': 'A white can stands upright.'})
        for reply in ('{"scope":false}', '{}', '{"scope":"true"}', 'invalid'):
            self.assertEqual(confirm_negatives(first, reply)['verdict'], 'uncertain')
        self.assertEqual(confirm_negatives(first, '{"scope":true}')['verdict'], 'unaffected')
        self.assertEqual(first['verdict'], 'unaffected')

    def test_affected_and_hard_edges_survive_review_and_budget(self):
        case = dict(shots=dict.fromkeys(['S1', 'S2', 'S3', 'S4', 'S5'], ''),
                    declared=['S1'], edges=[
                        dict(parent='S2', child='S3', kind='reference'),
                        dict(parent='S2', child='S5', kind='order'),
                    ])
        verdicts = {shot: {'verdict': verdict} for shot, verdict in [
            ('S1', 'unaffected'), ('S2', 'affected'), ('S3', 'unaffected'),
            ('S4', 'uncertain'), ('S5', 'unaffected'),
        ]}
        self.assertEqual(build_plan(case, verdicts)['regenerate'], ['S1', 'S2', 'S3', 'S4'])
        self.assertEqual(build_plan(case, verdicts, budget=1)['regenerate'], ['S1', 'S2', 'S3'])

    def test_evidence_ablation_and_gold_exclusion(self):
        case = dict(entity='strap', attribute='color', edit='recolor', shots={'S1': 'x'},
                    declared=[], gold=['S1'])
        self.assertNotIn('gold', public_case(case))
        self.assertEqual(shot_sources('A strap. Frame observation: A buckle.', False),
                         {'metadata': 'A strap.'})
        self.assertEqual(shot_sources('A strap. Frame observation: A buckle.', True),
                         {'metadata': 'A strap.', 'observation': 'A buckle.'})

    def test_checklist_recall_requires_local_positive_evidence_and_closes_references(self):
        case = dict(shots=dict.fromkeys(['S1', 'S2', 'S3', 'S4', 'S5'], ''), declared=[],
                    edges=[dict(parent='S2', child='S3', kind='reference')])
        sources = {shot: {'metadata': f'Evidence for {shot}'} for shot in case['shots']}

        def review(server, model, case, shot, sources):
            if shot == 'S3':
                return 'invalid'
            quote = 'Evidence for S2' if shot == 'S4' else sources[shot]['metadata']
            return json.dumps({
                key: {'answer': 'yes', 'source': 'metadata', 'quote': quote}
                for key in ('identity', 'target', 'visibility', 'scope')
                if shot != 'S5' or key != 'target'
            })

        with patch('egdr.review_omission', side_effect=review) as mocked:
            selected, audit = recall_checklist('server', 'model', case, sources, {'S1'})
        self.assertEqual([call.args[3] for call in mocked.call_args_list], ['S2', 'S3', 'S4', 'S5'])
        self.assertEqual(selected, {'S1', 'S2', 'S3'})
        self.assertEqual(audit['added'], ['S2'])
        self.assertEqual(audit['closure_added'], ['S3'])
        self.assertEqual(audit['extra_model_calls'], 4)
        self.assertEqual(audit['shots']['S5']['checks']['target'], {'answer': 'unknown'})


if __name__ == '__main__':
    unittest.main()
