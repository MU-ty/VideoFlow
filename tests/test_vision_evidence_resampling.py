"""Verify fixed trim schedules, balanced controls and isolation of cache arms."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research/dependency_probe'))
from vision_evidence_resampling import FRAMES, SWEEP, TRIMS, evaluate, grid, matched_controls


def capture(condition, indices, witness):
    return dict(story='story', id=condition, condition=condition, question='target', gold=['S1'],
                probes={'S1': dict(decision='yes', calls=2, panels=[str(i) for i in indices],
                                   frame_indices=list(indices), witness=str(witness))})


class ResamplingTests(unittest.TestCase):
    def test_controls_change_three_indices_and_keep_only_the_intended_old_index(self):
        initial = set(grid(0, FRAMES))
        for witness in initial:
            keep, remove = map(set, matched_controls(witness))
            self.assertEqual(len(keep), 4)
            self.assertEqual(len(remove), 4)
            self.assertEqual(keep & initial, {witness})
            self.assertEqual(len(remove & initial), 1)
            self.assertNotIn(witness, remove)
            self.assertEqual(keep - initial, remove - initial)

    def test_fixed_trim_grids_have_overlap_and_nonoverlap(self):
        original = set(grid(0, FRAMES))
        overlaps = [len(original.intersection(grid(*window))) for window in TRIMS.values()]
        self.assertEqual(overlaps, [1, 1, 1, 0])
        self.assertEqual(len(SWEEP), 24)

    def test_alternative_resamples_cannot_prime_each_other(self):
        initial = capture('original', (5, 16, 28, 40), 5)
        a = capture('trim_h2_t0', (7, 18, 29, 40), 7)
        b = capture('trim_h3_t4', (7, 17, 27, 37), 7)
        kept = capture('retain_witness', (4, 5, 15, 26), 5)
        result = evaluate([(initial, [a, b, kept])])
        rows = {r['condition']: r for r in result['rows'] if r['policy'] == 'witness_reuse'}
        self.assertEqual(rows['trim_h2_t0']['model_calls'], 2)
        self.assertEqual(rows['trim_h3_t4']['model_calls'], 2)
        self.assertEqual(rows['retain_witness']['cache_hits'], {'S1': 'verified_panel'})
        self.assertEqual(result['natural_witness_survival'], {'eligible': 2, 'survived': 0})
