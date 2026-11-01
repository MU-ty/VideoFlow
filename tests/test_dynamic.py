import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asset_runtime import AssetRuntime, Conflict
from asset_worker import pass_once
from research.mock_provider import MockProvider
from research.compare_dynamic import compare
from research.compare_reports import load_run as load_post_generation
from research.run_dynamic import run_dynamic, validate_schedule

ROOT=Path(__file__).resolve().parents[1]


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.manifest=json.loads((ROOT/'fixtures/assets.json').read_text())
        self.schedule=json.loads((ROOT/'fixtures/dynamic_edits.json').read_text())
        self.backend=MockProvider()

    def tearDown(self):self.tmp.cleanup()

    def run_method(self,method='dair',**kwargs):
        return run_dynamic(self.manifest,self.schedule,self.root/method,method=method,
            backend=self.backend,downloader=self.backend.download,poll_seconds=0,
            timeout_seconds=20,max_inflight=2,**kwargs)

    def test_two_inflight_edits_reject_old_outputs(self):
        result=self.run_method()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['current_revision'],3)
        self.assertEqual(result['applied_events'],2)
        self.assertEqual(result['metrics']['stale_results_observed'],2)
        self.assertEqual(result['metrics']['max_remote_unresolved_observed'],2)
        self.assertEqual(result['metrics']['remote_unresolved'],0)
        self.assertIsNone(result['events'][0]['own_revision_latency_seconds'])
        self.assertIsNotNone(result['events'][1]['own_revision_latency_seconds'])
        archive=json.loads((self.root/'dair/artifacts/revision-3.json').read_text())
        self.assertFalse((self.root/'dair/artifacts/revision-2.json').exists())
        stale={j['output'] for j in result['stale_observations']}
        for a in archive['assets']:
            self.assertNotIn(a['output'],stale)
            if a['asset'] in ('S2.video','S4.video','S6.video'):
                self.assertIn(b'blue',(self.root/'dair/artifacts'/a['file']).read_bytes())

    def test_full_vs_dair_under_same_state_triggers(self):
        full=self.run_method('full')
        dair=self.run_method('dair')
        self.assertEqual(full['status'],'complete');self.assertEqual(dair['status'],'complete')
        self.assertEqual(full['metrics']['submission_intents'],10)
        self.assertEqual(dair['metrics']['submission_intents'],8)
        self.assertEqual(full['metrics']['stale_results_observed'],4)
        self.assertEqual(dair['metrics']['stale_results_observed'],2)
        self.assertIsNone(dair['metrics']['gpu_seconds'])

    def test_compare_matched_dynamic_runs_and_protocol_guard(self):
        self.run_method('full');self.run_method('dair')
        report=compare(self.root/'full',self.root/'dair')
        self.assertEqual(report['rows'][0]['submission_intents'],10)
        self.assertEqual(report['rows'][1]['submission_intents'],8)
        self.assertIsNone(report['quality_comparison'])
        with self.assertRaises(ValueError):load_post_generation(self.root/'dair')
        archive_path=self.root/'dair/artifacts/revision-3.json'
        archive=json.loads(archive_path.read_text())
        video=self.root/'dair/artifacts'/archive['assets'][0]['file']
        video.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):compare(self.root/'full',self.root/'dair')

    def test_resume_after_event_applied_no_duplicate(self):
        first=self.run_method(max_passes=1)
        self.assertEqual(first['status'],'paused')
        self.assertEqual(first['applied_events'],1)
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        self.assertEqual(len(self.backend.posts),8)
        self.assertEqual(len(final['sessions']),2)
        again=self.run_method()
        self.assertEqual(again['status'],'complete')
        self.assertEqual(len(self.backend.posts),8)

    def test_crash_after_edit_commit_before_runner_observation(self):
        original=AssetRuntime.edit
        def crash(runtime,event):
            result=original(runtime,event)
            if event['id']=='dynamic-black-001':raise OSError('injected crash after edit transaction committed')
            return result
        with patch.object(AssetRuntime,'edit',crash):
            paused=self.run_method()
        self.assertEqual(paused['status'],'error')
        self.assertEqual(paused['applied_events'],1)
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        self.assertEqual(len(self.backend.posts),8)

    def test_superseded_remote_work_still_consumes_slots(self):
        self.backend.ready=False
        first=self.run_method(max_passes=1)
        self.assertEqual(first['metrics']['superseded_remote_unresolved'],1)
        second=self.run_method(max_passes=4)
        self.assertEqual(second['status'],'paused')
        self.assertEqual(second['metrics']['remote_unresolved'],2)
        self.assertEqual(len(self.backend.posts),2)
        self.backend.ready=True
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        self.assertEqual(len(self.backend.posts),8)

    def test_final_ready_waits_for_unknown_obsolete_remote(self):
        self.run_method(max_passes=1)
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:
            old=next(j for j in r.unresolved_remote() if j['asset']=='S2.video')
        finally:r.close()
        data=self.backend.jobs.pop(old['external_id'])
        held=self.run_method(max_passes=40)
        self.assertEqual(held['status'],'paused')
        self.assertTrue(held['project_ready'])
        self.assertEqual(held['phase'],'final_ready_waiting_remote')
        self.assertEqual(held['metrics']['superseded_remote_unresolved'],1)
        self.assertEqual(len(self.backend.posts),8)
        self.backend.jobs[old['external_id']]=data
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        self.assertEqual(final['metrics']['remote_unresolved'],0)
        self.assertEqual(len(self.backend.posts),8)

    def test_superseded_remote_failure_does_not_fail_current_project(self):
        self.run_method(max_passes=1)
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:old=next(j for j in r.unresolved_remote() if j['asset']=='S2.video')
        finally:r.close()
        inspect=self.backend.inspect
        self.backend.inspect=lambda pid:('failed','injected old failure') if pid==old['external_id'] else inspect(pid)
        final=self.run_method()
        self.assertEqual(final['status'],'complete')
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:
            j=r.db.execute('SELECT * FROM vf_jobs WHERE id=?',(old['id'],)).fetchone()
            self.assertEqual(j['status'],'superseded')
            self.assertEqual(j['remote_state'],'failed')
        finally:r.close()

    def test_unreachable_trigger_reports_blocked(self):
        self.schedule[0]['trigger']['revision']=99
        result=self.run_method()
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['applied_events'],0)
        self.assertTrue(result['project_ready'])
        self.assertEqual(len(self.backend.posts),6)

    def test_changed_schedule_cannot_resume(self):
        self.run_method(max_passes=0)
        self.schedule[0]['edit']['value']='red'
        with self.assertRaises(Conflict):self.run_method()

    def test_unscheduled_database_edit_blocks_resume(self):
        self.run_method(max_passes=0)
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:
            event=dict(self.schedule[0]['edit'],id='rogue',expected_revision=1)
            r.edit(event)
        finally:r.close()
        result=self.run_method()
        self.assertEqual(result['status'],'error')
        self.assertIn('schedule prefix',result['error'])
        self.assertEqual(len(self.backend.posts),0)

    def test_invalid_schedule_rejected_without_output(self):
        schedule=copy.deepcopy(self.schedule)
        schedule[1]['edit']['id']=schedule[0]['edit']['id']
        with self.assertRaises(ValueError):validate_schedule(self.manifest,schedule,'dair')
        schedule=copy.deepcopy(self.schedule);schedule[0]['trigger']['revision']=0
        with self.assertRaises(ValueError):validate_schedule(self.manifest,schedule,'dair')

    def test_terminal_provider_observation_cannot_regress(self):
        self.run_method(max_passes=1)
        r=AssetRuntime(str(self.root/'dair/project.db'))
        try:
            job=next(j for j in r.unresolved_remote() if j['asset']=='S2.video')
            self.assertEqual(r.complete(job['id'],job['external_id'],'mock://old'),'stale')
            self.assertNotIn(job['id'],{j['id'] for j in r.unresolved_remote()})
            r.mark(job['id'],'unknown');r.mark(job['id'],'submitted')
            recorded=r.db.execute('SELECT * FROM vf_jobs WHERE id=?',(job['id'],)).fetchone()
            self.assertEqual(recorded['remote_state'],'succeeded')
            self.assertEqual(recorded['status'],'superseded')
            with self.assertRaises(Conflict):r.complete(job['id'],job['external_id'],'mock://different')
        finally:r.close()

if __name__=='__main__':unittest.main()
