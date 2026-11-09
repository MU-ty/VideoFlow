"""Regression checks for visual decisions and the frozen paired baseline."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from vision_probe import parse_visual, run_case, visual_reply


class VisionProbeTest(unittest.TestCase):
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
