import json
from pathlib import Path
import tempfile
import unittest
from asset_runtime import AssetRuntime
from asset_worker import build_h3
from research.compare_post import compare
from research.freeze_group import freeze_group
from research.freeze_case import verify
from research.mock_provider import MockProvider
from research.run_experiment import run
ROOT = Path(__file__).resolve().parents[1]

class PostComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        folder = ROOT / 'fixtures/diagnostics_v7/state_costume'
        self.manifest = json.loads((folder / 'manifest.json').read_text())
        self.edit = json.loads((folder / 'edit.json').read_text())
        for method in ('full', 'suffix_safe', 'dag', 'dair'):
            b = MockProvider()
            r = run(self.manifest, self.edit, self.root / 'runs' / method, method=method,
                    backend=b, downloader=b.download, poll_seconds=0, timeout_seconds=20, max_inflight=2)
            self.assertEqual(r['status'], 'complete')
    def tearDown(self): self.tmp.cleanup()
    def test_four_methods_and_actual_reuse(self):
        r = compare([self.root / 'runs' / m for m in ('full', 'suffix_safe', 'dag', 'dair')])
        self.assertEqual([x['edit_submission_intents'] for x in r['rows']], [6, 5, 5, 3])
        self.assertEqual(r['rows'][-1]['retained_videos'], ['S1.video', 'S3.video', 'S5.video'])
        self.assertIsNone(r['decoded_video_equal_by_asset']); self.assertIsNone(r['gpu_seconds'])
    def test_reject_initial_archive_corruption(self):
        d = self.root / 'runs/dair'
        a = json.loads((d / 'artifacts/revision-1.json').read_text())['assets']
        x = next(x for x in a if x['asset'] == 'S2.video')
        (d / 'artifacts' / x['file']).write_bytes(b'changed')
        with self.assertRaises(ValueError): compare([d, self.root / 'runs/full'])
    def test_freeze_and_detect_tampering(self):
        out = self.root / 'frozen'; receipt = freeze_group(self.root / 'runs', out)
        self.assertEqual(verify(out)['inventory_hash'], receipt['inventory_hash'])
        with self.assertRaises(ValueError): freeze_group(self.root / 'runs', out)
        (out / 'full/report.json').write_text('{}')
        with self.assertRaises(ValueError): verify(out)
    def test_suite_expected_regeneration_and_reference_gate(self):
        expected = {'subtitle': 0, 'local_costume': 1, 'state_costume': 3, 'global_light': 6, 'reference_change': 3}
        for case, count in expected.items():
            folder = ROOT / 'fixtures/diagnostics_v7' / case
            rt = AssetRuntime(str(self.root / f'{case}.db'))
            try:
                rt.initialize(json.loads((folder / 'manifest.json').read_text()))
                plan = rt.edit(dict(json.loads((folder / 'edit.json').read_text()), method='dair'))
                self.assertEqual(sum(x.endswith('.video') for x in plan['regenerate']), count)
            finally: rt.close()
        with self.assertRaisesRegex(ValueError, 'no reference loader'):
            build_h3(dict(dependencies=[dict(kind='reference')]), 'test')

if __name__ == '__main__': unittest.main()
