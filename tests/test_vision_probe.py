"""Regression checks for visual decisions and the frozen paired baseline."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from vision_probe import CONFLICT_METHOD, parse_conflict_review, parse_visual, run_case, visual_reply


class VisionProbeTest(unittest.TestCase):
    def test_conflict_review_removes_only_soft_seeds_with_confirmed_distinct_identity(self):
        shots = ('anchor', 'match', 'decoy', 'unknown', 'unclear', 'malformed', 'hard', 'decoy_child')
        case = dict(id='edit', story='story', entity='dial', attribute='color',
                    edit='Recolor the dial', shots=dict.fromkeys(shots, 'workshop'),
                    declared=['anchor'], gold=['anchor', 'match', 'unknown', 'unclear', 'malformed', 'hard'],
                    edges=[dict(parent='match', child='hard', kind='reference'),
                           dict(parent='decoy', child='decoy_child', kind='reference')])
        sheets = {shot: Path(f'{shot}.jpg') for shot in shots}
        positive = '{"decision":"yes","panel":1,"evidence":"marked housing"}'
        negative = '{"decision":"no","panel":null,"evidence":"another housing"}'
        unknown = '{"decision":"unknown","panel":null,"evidence":""}'
        confirmed = '{"identity":"yes","part":"yes","position":"yes","panel":1,"evidence":"marked dial"}'
        distinct = ('{"decision":"distinct","panel":2,"identity_marker":"orange diamond",'
                    '"evidence":"The dial is mounted next to the diamond on the same housing."}')
        review_unknown = '{"decision":"unknown","panel":null,"identity_marker":"","evidence":""}'
        visual_by_shot = {'match': positive, 'unknown': unknown}
        raw_by_shot = {'match': confirmed, 'unclear': review_unknown, 'malformed': 'not JSON'}
        logs = []
        with patch('vision_probe.predict', side_effect=[
                (['decoy', 'unknown', 'unclear', 'malformed', 'hard'], 'text'), ([], 'observed')]), \
             patch('vision_probe.visual_reply',
                   side_effect=lambda _, __, ___, sheet, **kw: visual_by_shot.get(sheet.stem, negative)), \
             patch('vision_probe.image_reply',
                   side_effect=lambda _, __, sheet, *args: raw_by_shot.get(sheet.stem, distinct)):
            report = run_case(case, sheets, 'server', 'text-model', 'vision-model', logs.append,
                              visibility_question='Is the striped press dial visible?',
                              verify_visual=True, review_conflicts=True)
        methods = {row['method']: row for row in report['results']}
        self.assertEqual(methods['union_plus_verified']['selected'], sorted(shots))
        self.assertEqual(methods[CONFLICT_METHOD]['selected'], sorted(case['gold']))
        self.assertEqual(report['conflict_calls'], 4)
        self.assertEqual(report['conflict_vetoed_seeds'], ['decoy', 'hard'])
        self.assertEqual(report['conflict_removed'], ['decoy', 'decoy_child'])
        self.assertEqual(report['conflict_invalid'], ['malformed'])
        self.assertEqual([item['shot'] for item in logs if item['method'] == 'vision_conflict_review'],
                         ['decoy', 'hard', 'malformed', 'unclear'])

    def test_distinct_requires_visible_marker_and_localized_evidence(self):
        review = dict(decision='distinct', panel=1, identity_marker='orange diamond', evidence='on housing')
        self.assertEqual(parse_conflict_review(json.dumps(review)), review)
        for overrides in ({'panel': None}, {'panel': True}, {'identity_marker': ' '}, {'evidence': ''}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                parse_conflict_review(json.dumps(review | overrides))

    def test_visual_verification_requires_all_checks_and_preserves_raw_baseline(self):
        case = dict(id='edit', story='story', entity='dial', attribute='color',
                    edit='Recolor the dial', shots={'S1': 'dial', 'S2': 'handwheel'},
                    declared=[], gold=['S1'])
        sheets = {shot: Path(f'{shot}.jpg') for shot in case['shots']}
        positive = '{"decision":"yes","panel":1,"evidence":"round object"}'
        accepted = '{"identity":"yes","part":"yes","position":"yes","panel":1,"evidence":"marked front dial"}'
        rejected = '{"identity":"yes","part":"unknown","position":"no","panel":null,"evidence":"side wheel"}'
        logs = []
        with patch('vision_probe.predict', return_value=([], '{}')), \
             patch('vision_probe.visual_reply', return_value=positive), \
             patch('vision_probe.image_reply', side_effect=[accepted, rejected]) as verify:
            report = run_case(case, sheets, 'server', 'text-model', 'vision-model', logs.append,
                              visibility_question='Is the front dial visible?', verify_visual=True)
        methods = {row['method']: row for row in report['results']}
        self.assertEqual(methods['vision']['selected'], ['S1', 'S2'])
        self.assertEqual(methods['vision_verified']['selected'], ['S1'])
        self.assertEqual(methods['union_plus_verified']['selected'], ['S1'])
        self.assertEqual(report['verification_calls'], 2)
        self.assertEqual([log['raw'] for log in logs if log['method'] == 'vision_verify'],
                         [accepted, rejected])
        self.assertTrue(all(call.args[3].endswith('Is the front dial visible?')
                            for call in verify.call_args_list))

    def test_retries_invalid_visual_reply_without_scoring_persistent_format_errors(self):
        case = dict(id='edit', story='story', entity='buckle', attribute='color',
                    edit='Recolor the buckle', shots={'S1': 'strap', 'S2': 'tool'},
                    declared=[], gold=['S1'])
        sheets = {shot: Path(f'{shot}.jpg') for shot in case['shots']}
        missing_panel = '{"decision":"yes","panel":null,"evidence":"buckle"}'
        replies = [missing_panel, '{"decision":"yes","panel":1,"evidence":"buckle"}',
                   'invalid JSON', missing_panel]
        logs = []
        with patch('vision_probe.predict', return_value=([], '{}')):
            with patch('vision_probe.visual_reply', side_effect=replies) as vision:
                report = run_case(case, sheets, 'server', 'text-model', 'vision-model', logs.append)
        self.assertEqual(report['visual_retries'], ['S1', 'S2'])
        self.assertEqual(report['visual_positives'], ['S1'])
        self.assertEqual(report['visual_invalid'], ['S2'])
        self.assertEqual(report['visual_unknown'], [])
        self.assertEqual(report['vision_calls'], 4)
        self.assertNotIn('vision', {result['method'] for result in report['results']})
        self.assertEqual([log['attempt'] for log in logs[2:]], [1, 2, 1, 2])
        self.assertEqual([log['raw'] for log in logs[2:]], replies)
        self.assertEqual([call.kwargs.get('format_retry', False) for call in vision.call_args_list],
                         [False, True, False, True])

    def test_source_question_omits_edit_and_accepts_observed_evidence_key(self):
        reply = '{"decision":"yes","panel":1,"evidence_description":"visible buckle"}'
        response = io.BytesIO(json.dumps({'message': {'content': reply}}).encode())
        with patch('vision_probe.urlopen', return_value=response) as open_url:
            result = visual_reply('http://localhost', 'vision-model', 'Recolor the buckle orange',
                                  Path(__file__), visibility_question='Is the yellow buckle visible?')
        message, = json.loads(open_url.call_args.args[0].data)['messages']
        self.assertTrue(message['content'].startswith('Is the yellow buckle visible?'))
        self.assertNotIn('orange', message['content'])
        self.assertEqual(parse_visual(result)['evidence'], 'visible buckle')

    def test_positive_requires_valid_panel_and_evidence(self):
        self.assertEqual(parse_visual('{"decision":"yes","panel":4,"evidence":"visible buckle"}')
                         ['decision'], 'yes')
        for panel in (None, True, 0, 5):
            with self.subTest(panel=panel), self.assertRaises(ValueError):
                parse_visual(json.dumps(dict(decision='yes', panel=panel, evidence='visible buckle')))
        with self.assertRaises(ValueError):
            parse_visual('{"decision":"yes","panel":2,"evidence":" "}')
        self.assertEqual(parse_visual('{"decision":"unknown","panel":null,"evidence":""}')
                         ['decision'], 'unknown')

    def test_union_and_visual_policies_use_same_hard_closure(self):
        case = dict(id='edit', story='story', entity='strap', attribute='color',
                    edit='Recolor the front strap', shots={
                        'S1': 'wide. Frame observation: strap.',
                        'S2': 'detail. Frame observation: buckle.',
                        'S3': 'cutaway. Frame observation: tool.',
                        'S4': 'rear. Frame observation: zipper.',
                    }, declared=[], gold=['S1', 'S2', 'S3'],
                    edges=[dict(parent='S2', child='S3', kind='reference')])
        sheets = {shot: Path(f'{shot}.jpg') for shot in case['shots']}
        logs = []
        with patch('vision_probe.predict', side_effect=[(['S1'], 'text'), ([], 'observed')]) as baseline:
            with patch('vision_probe.visual_reply', side_effect=[
                '{"decision":"unknown","panel":null,"evidence":""}',
                '{"decision":"yes","panel":2,"evidence":"visible front strap"}',
                '{"decision":"no","panel":null,"evidence":"tool"}',
                '{"decision":"yes","panel":1,"evidence":"zipper misidentified as strap"}',
            ]) as vision:
                report = run_case(case, sheets, 'server', 'text-model', 'vision-model', logs.append)
        text_input, observed_input = [call.args[2] for call in baseline.call_args_list]
        self.assertEqual(text_input['shots']['S1'], 'wide.')
        self.assertEqual(observed_input['shots']['S1'], 'wide. strap.')
        self.assertNotIn('gold', observed_input)
        self.assertEqual(vision.call_args.args, ('server', 'vision-model', case['edit'], sheets['S4']))
        rows = {row['method']: row for row in report['results']}
        self.assertEqual(rows['checklist_union']['missed'], ['S2', 'S3'])
        self.assertEqual(rows['vision']['missed'], ['S1'])
        self.assertEqual(rows['union_plus_vision']['missed'], [])
        self.assertEqual(rows['union_plus_vision']['excess'], ['S4'])
        self.assertEqual(report['visual_unknown'], ['S1'])
        self.assertEqual(len(logs), 6)


if __name__ == '__main__':
    unittest.main()
