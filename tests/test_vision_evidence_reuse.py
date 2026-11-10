"""Check scope, frame provenance, and paired evidence accounting."""
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from vision_evidence_reuse import capture_probe, compare, digest


def edit(identifier, panels, *, question='striped press wheel', shot='S1', decision='yes', witness=None):
    return dict(id=identifier, condition=identifier, story='press', question=question,
                gold=[shot] if decision == 'yes' else [],
                probes={shot: dict(panels=panels, calls=2, decision=decision, witness=witness)})


class VisualEvidenceReuseTests(unittest.TestCase):
    def test_independent_caches_and_same_source_reuse(self):
        rows = compare([edit('blue', ['A', 'B'], witness='A'), edit('red', ['A', 'B'], witness='A')],
                       evidence='test')['rows']
        self.assertEqual([row['model_calls'] for row in rows], [2, 2, 2, 2, 0, 0])
        self.assertEqual(rows[-1]['cache_hits'], {'S1': 'exact_source'})

    def test_partial_source_change_and_disappearing_witness(self):
        rows = compare([edit('before', ['A', 'B'], witness='A'),
                        edit('unrelated_frame_replaced', ['A', 'C']),
                        edit('occluded', ['C', 'D'], decision='unknown')],
                       evidence='test')['rows']
        self.assertEqual(rows[4]['model_calls'], 2)
        self.assertEqual(rows[5]['cache_hits'], {'S1': 'verified_panel'})
        self.assertEqual(rows[8]['model_calls'], 2)
        self.assertEqual(rows[8]['unresolved'], ['S1'])

    def test_target_and_shot_switch_requery_even_with_identical_pixels(self):
        rows = compare([edit('before', ['A'], witness='A'),
                        edit('other_target', ['A'], question='orange press wheel'),
                        edit('other_shot', ['A'], shot='S2')], evidence='test')['rows']
        self.assertTrue(all(row['model_calls'] == 2 and not row['cache_hits'] for row in rows))

    def test_negative_and_unknown_are_not_cached(self):
        edits = [edit(str(i), ['A'], decision=decision)
                 for i, decision in enumerate(('no', 'unknown', 'no', 'unknown'))]
        self.assertTrue(all(row['model_calls'] == 2 for row in compare(edits, evidence='test')['rows']))

    def test_rejects_witness_not_bound_to_a_positive_panel(self):
        for probe in (edit('bad', ['A'], witness='B'),
                      edit('bad', ['A'], witness='A', decision='unknown')):
            with self.subTest(probe=probe), self.assertRaises(ValueError):
                compare([probe], evidence='test')


    def run_capture(self, verifier, detector=None):
        detection = '{"decision":"yes","panel":2,"evidence":"visible part"}'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            panels = [root / f'panel{i}.png' for i in range(1, 5)]
            for panel in panels:
                panel.write_bytes(panel.name.encode())
            with patch('vision_evidence_reuse.ffmpeg'), patch(
                'vision_evidence_reuse.visual_reply', side_effect=detector or [detection]
            ), patch('vision_evidence_reuse.image_reply', side_effect=verifier):
                log = StringIO()
                result = capture_probe('', '', 'target', panels, root / 'shot', log)
        return result, [json.loads(line)['stage'] for line in log.getvalue().splitlines()]

    def test_retries_single_frame_match_without_evidence(self):
        replies = [
            '{"identity":"yes","part":"yes","position":"yes","panel":null,"evidence":""}',
            '{"identity":"yes","part":"yes","position":"yes","panel":1,"evidence":"marked part"}',
        ]
        result, stages = self.run_capture(replies)
        self.assertEqual(stages, ['detect', 'verify_isolated_panel', 'verify_isolated_panel_retry'])
        self.assertEqual(result['calls'], 3)
        self.assertEqual(result['witness'], digest(b'panel2.png'))
        self.assertEqual(len(result['invalid_replies']), 1)

    def test_invalid_verification_stays_unresolved(self):
        replies = [
            '{"identity":"yes","part":"yes","position":"yes","panel":2,"evidence":"marked part"}',
            '{"identity":"yes","part":"yes","position":"yes","panel":1,"evidence":" "}',
        ]
        result, stages = self.run_capture(replies)
        self.assertEqual(result['calls'], len(stages))
        self.assertEqual(result['decision'], 'unknown')
        self.assertIsNone(result['witness'])
        self.assertEqual(len(result['invalid_replies']), 2)

    def test_invalid_detector_stays_unresolved_without_verification(self):
        result, stages = self.run_capture([], detector=['{}', 'bad JSON'])
        self.assertEqual(stages, ['detect', 'detect_retry'])
        self.assertEqual(result['decision'], 'unknown')
        self.assertIsNone(result['witness'])
        self.assertEqual(len(result['invalid_replies']), 2)
