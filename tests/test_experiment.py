import copy
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from artifacts import archive_current, checksum, probe_media
from asset_runtime import AssetRuntime, Conflict
from asset_worker import pass_once
from research.mock_provider import MockProvider
from research.compare_reports import compare
from research.run_experiment import run, runner_lock

ROOT=Path(__file__).resolve().parents[1]


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.manifest=json.loads((ROOT/'fixtures/assets.json').read_text())
        self.edit=json.loads((ROOT/'fixtures/edit_costume.json').read_text())
        self.backend=MockProvider()

    def tearDown(self):
        self.tmp.cleanup()

    def run_method(self,method='dair',**kwargs):
        return run(self.manifest,self.edit,self.root/method,method=method,backend=self.backend,
                   downloader=self.backend.download,poll_seconds=0,timeout_seconds=20,max_inflight=2,**kwargs)

    def test_full_vs_dair_same_semantic_edit(self):
        full=self.run_method('full')
        dair=self.run_method('dair')
        self.assertEqual(full['status'],'complete')
        self.assertEqual(dair['status'],'complete')
        self.assertEqual(full['metrics']['edit_submission_intents'],6)
        self.assertEqual(dair['metrics']['edit_submission_intents'],3)
        self.assertIsNone(dair['metrics']['gpu_seconds'])
        self.assertIsNone(dair['metrics']['edit_success_rate'])
        for method in ('full','dair'):
            state=json.loads((self.root/method/'artifacts/revision-2.json').read_text())
            videos=[a for a in state['assets'] if a['asset'] in ('S2.video','S4.video','S6.video')]
            for a in videos:
                data=(self.root/method/'artifacts'/a['file']).read_bytes()
                self.assertIn(b'black',data)

    def test_compare_matched_runs_and_reject_mismatch(self):
        self.run_method('full'); self.run_method('dair')
        result=compare(self.root/'full',self.root/'dair')
        self.assertEqual(result['edit_submission_intents_difference'],3)
        self.assertIsNone(result['quality_comparison'])
        path=self.root/'dair/configuration.json'
        config=json.loads(path.read_text());config['manifest']['assets'][0]['spec']['template']='other'
        path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):compare(self.root/'full',self.root/'dair')

    def test_probe_failure_records_error(self):
        if not shutil.which('ffprobe'):
            self.skipTest('ffprobe absent')
        result=self.run_method(probe=True)
        self.assertEqual(result['status'],'error')
        self.assertIsNotNone(result['error'])
        self.assertFalse((self.root/'dair/artifacts/revision-1.json').exists())

    def test_resume_no_duplicate_submission(self):
        first=self.run_method(max_passes=1)
        self.assertEqual(first['status'],'paused')
        before=len(self.backend.posts)
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        self.assertEqual(len(self.backend.posts),9)
        self.assertEqual(final['metrics']['total_submission_intents'],9)
        self.assertEqual(len(final['sessions']),2)
        self.assertGreater(before,0)
        again=self.run_method()
        self.assertEqual(again['status'],'complete')
        self.assertEqual(len(self.backend.posts),9)

    def test_changed_config_cannot_resume(self):
        self.run_method(max_passes=0)
        self.manifest['assets'][0]['spec']['template']='different'
        with self.assertRaises(Conflict): self.run_method()

    def test_unknown_job_pause_not_resubmit(self):
        self.run_method(max_passes=1)
        before=len(self.backend.posts)
        self.backend.jobs.clear()
        result=self.run_method(max_passes=3)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(len(self.backend.posts),before)
        self.assertEqual(result['metrics']['active_unknown_jobs'],2)

    def test_ref_adapter_blocks_with_report(self):
        self.manifest['edges']=[dict(parent='S2.video',child='S4.video',kind='reference')]
        result=self.run_method()
        self.assertEqual(result['status'],'blocked')
        self.assertIsNotNone(result['error'])
        self.assertTrue((self.root/'dair/report.json').exists())

    def test_runner_exclusive_lock(self):
        with runner_lock(self.root):
            with self.assertRaises(sqlite3.OperationalError):
                with runner_lock(self.root): self.fail('second lock acquired')
        with runner_lock(self.root): pass

    def test_schema_upgrade_preserves_missing_historical_times(self):
        path=self.root/'old.db'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE vf_jobs(id TEXT PRIMARY KEY, asset TEXT, revision INTEGER, status TEXT, external_id TEXT, backend TEXT, input_json TEXT, execution_json TEXT, input_hash TEXT, output TEXT, error TEXT, UNIQUE(asset,revision))')
        db.execute("INSERT INTO vf_jobs(id,status) VALUES ('old','succeeded')")
        db.commit();db.close()
        r=AssetRuntime(str(path))
        try:
            job=r.db.execute("SELECT * FROM vf_jobs WHERE id='old'").fetchone()
            self.assertIsNone(job['created_at'])
            self.assertIsNone(job['completed_at'])
            self.assertEqual(job['submission_intents'],0)
        finally:r.close()

    def test_invalid_manifest_emits_error_report(self):
        self.manifest['assets'][0]['spec']['cost']=-1
        report=self.run_method()
        self.assertEqual(report['status'],'error')
        self.assertIsNotNone(report['error'])

    def test_archive_reuse_and_corruption_repair(self):
        self.run_method()
        base=self.root/'dair/artifacts'
        initial=json.loads((base/'revision-1.json').read_text())
        final=json.loads((base/'revision-2.json').read_text())
        first={a['asset']:a for a in initial['assets']}
        for a in final['assets']:
            if a['asset'] in ('S1.video','S3.video','S5.video','S2.subtitle'):
                self.assertEqual(a['file'],first[a['asset']]['file'])
            self.assertEqual(checksum(base/a['file']),a['sha256'])
        self.assertEqual(self.backend.downloads,9)
        corrupt=next(a for a in final['assets'] if a['asset']=='S2.video')
        (base/corrupt['file']).write_bytes(b'corrupt')
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:
            archive_current(r,base,self.backend.base,downloader=self.backend.download)
            self.assertEqual(checksum(base/corrupt['file']),corrupt['sha256'])
            self.assertEqual(self.backend.downloads,10)
        finally:r.close()

    def test_recorded_timing_does_not_claim_gpu_measurement(self):
        report=self.run_method()
        self.assertIsNotNone(report['metrics']['edit_completion_observed_seconds'])
        self.assertEqual(len(report['task_observations']),10)  # 9 video jobs + initial subtitle
        self.assertTrue(all(row['reserve_to_completion_observed_seconds']>=0 for row in report['task_observations']))
        self.assertIsNone(report['metrics']['api_cost'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'optional media executables absent')
    def test_probe_real_container_and_reject_mock_bytes(self):
        video=self.root/'test.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:d=0.2','-c:v','mpeg4','-y',str(video)],check=True,capture_output=True)
        probe=probe_media(video)
        self.assertTrue(any(s.get('codec_type')=='video' for s in probe['streams']))
        video.write_bytes(b'not a video')
        with self.assertRaises(subprocess.CalledProcessError):probe_media(video)

if __name__=='__main__':unittest.main()
