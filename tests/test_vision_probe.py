"""Regression checks for visual decisions and the frozen paired baseline."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from vision_probe import parse_visual, run_case


class VisionProbeTest(unittest.TestCase):
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
