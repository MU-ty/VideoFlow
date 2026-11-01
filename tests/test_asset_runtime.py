import copy
import json
from pathlib import Path
import tempfile
import unittest

from asset_runtime import AssetRuntime, Conflict, encode
from asset_worker import LOCAL, build_h3, build_subtitle, pass_once
from comfyui import SubmissionUncertain, SubmissionRejected

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/assets.json'


class FakeBackend:
    base = 'mock://h3'

    def __init__(self):
        self.posts = []
        self.jobs = {}
        self.mode = 'ok'

    def submit(self, graph, pid):
        self.posts.append((pid, copy.deepcopy(graph)))
        if self.mode == 'reject':
            raise SubmissionRejected('invalid workflow')
        self.jobs[pid] = ('running', None)
        if self.mode == 'lost':
            raise SubmissionUncertain('response lost after acceptance')

    def inspect(self, pid):
        return self.jobs.get(pid, ('unknown', None))

    def finish(self):
        for pid in self.jobs:
            self.jobs[pid] = ('succeeded', 'mock://output/'+pid+'.mp4')


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'project.db')
        self.r = AssetRuntime(self.path)
        self.manifest = json.loads(FIXTURE.read_text())
        self.r.initialize(self.manifest)
        self.b = FakeBackend()

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def finish(self, build=build_h3):
        for _ in range(15):
            pass_once(self.r, self.b, build=build, max_submit=20)
            self.b.finish()
            if self.r.snapshot()['timeline'] is not None:
                return
        self.fail('pipeline did not complete')

    def event(self, **overrides):
        event = dict(id='e1', expected_revision=self.r.snapshot()['revision'],
                     target='S2.video', key='hero.costume', value='black', scope='onward')
        event.update(overrides)
        return event

    def latest(self, aid):
        return self.r.db.execute('SELECT * FROM vf_jobs WHERE asset=? ORDER BY revision DESC', (aid,)).fetchone()

    def test_subtitle_edit_no_video_submission(self):
        self.finish()
        before = self.r.snapshot()
        posts = len(self.b.posts)
        result = self.r.edit(self.event(target='S2.subtitle', key='subtitle.text', value='Run!', scope='local'))
        self.assertEqual(result['regenerate'], ['S2.subtitle'])
        self.assertIsNone(self.r.snapshot()['timeline'])
        pass_once(self.r, self.b)
        self.assertEqual(len(self.b.posts), posts)
        after = self.r.snapshot()
        self.assertEqual(next(a['output'] for a in after['assets'] if a['kind']=='subtitle'), 'Run!')
        self.assertIsNotNone(after['timeline'])
        self.assertEqual([a for a in before['assets'] if a['kind']=='video'], [a for a in after['assets'] if a['kind']=='video'])

    def test_costume_executes_only_selected_new_prompts(self):
        self.finish()
        before = {a['id']:a for a in self.r.snapshot()['assets']}
        posts = len(self.b.posts)
        result = self.r.edit(self.event())
        self.assertEqual(result['regenerate'], ['S2.video','S4.video','S6.video'])
        self.finish()
        self.assertEqual(len(self.b.posts)-posts, 3)
        for _, workflow in self.b.posts[posts:]:
            self.assertIn('black', workflow['5']['inputs']['prompt'])
        for a in self.r.snapshot()['assets']:
            if a['id'] in result['reuse']:
                self.assertEqual(a, before[a['id']])

    def test_continuous_edits_stale_callback(self):
        pass_once(self.r, self.b, max_submit=20)
        old = dict(self.latest('S2.video'))
        self.r.edit(self.event())
        pass_once(self.r, self.b, max_submit=20)
        mid = dict(self.latest('S2.video'))
        self.r.edit(self.event(id='e2', value='blue'))
        self.assertEqual(self.r.complete(old['id'], old['external_id'], 'old.mp4'), 'stale')
        self.assertEqual(self.r.complete(mid['id'], mid['external_id'], 'mid.mp4'), 'stale')
        self.finish()
        current = self.latest('S2.video')
        self.assertEqual(current['revision'], 3)
        self.assertIn('blue', current['execution_json'])
        self.assertEqual(self.r.snapshot()['revision'], 3)

    def test_event_replay_and_cas(self):
        event = self.event()
        result = self.r.edit(event)
        self.assertEqual(result, self.r.edit(event))
        with self.assertRaises(Conflict):
            self.r.edit(dict(event, value='red'))
        with self.assertRaises(Conflict):
            self.r.edit(dict(event, id='other'))
        self.assertEqual(self.r.snapshot()['revision'], 2)

    def test_two_connections_stale_plan_rejected(self):
        other = AssetRuntime(self.path)
        try:
            event = self.event()
            other.edit(event)
            with self.assertRaises(Conflict):
                self.r.edit(dict(event, id='second'))
        finally:
            other.close()

    def test_restart_recovers_original_video_tasks(self):
        pass_once(self.r, self.b, max_submit=20)
        posts = len(self.b.posts)
        self.r.close(); self.r = AssetRuntime(self.path)
        self.b.finish()
        pass_once(self.r, self.b, max_submit=20)
        self.assertEqual(len(self.b.posts), posts)
        self.assertIsNotNone(self.r.snapshot()['timeline'])

    def test_accepted_lost_response_never_resubmits(self):
        self.b.mode = 'lost'
        pass_once(self.r, self.b, max_submit=20)
        posts = len(self.b.posts)
        self.assertEqual(len(self.r.active()), 6)
        self.b.jobs.clear()  # Queue/history absent, ambiguity persists.
        for _ in range(3):
            pass_once(self.r, self.b, max_submit=20)
        self.assertEqual(len(self.b.posts), posts)
        self.assertTrue(all(j['status']=='unknown' for j in self.r.active()))
        for pid, _ in self.b.posts:
            self.b.jobs[pid] = ('succeeded','mock://'+pid)
        pass_once(self.r,self.b)
        self.assertIsNotNone(self.r.snapshot()['timeline'])

    def test_crash_after_reservation_before_submit_stays_unknown(self):
        job = self.latest('S2.video')
        reserved = self.r.prepare(job['id'], self.b.base, build_h3)
        self.r.close(); self.r = AssetRuntime(self.path)
        pass_once(self.r,self.b,max_submit=0)
        self.assertEqual(self.latest('S2.video')['status'], 'unknown')
        self.assertEqual(self.b.posts, [])
        self.assertIsNotNone(reserved)

    def test_subtitle_crash_recovers_local_execution(self):
        job = self.latest('S2.subtitle')
        self.r.prepare(job['id'], LOCAL, build_subtitle)
        self.r.close(); self.r = AssetRuntime(self.path)
        pass_once(self.r,self.b,max_submit=0)
        self.assertEqual(self.latest('S2.subtitle')['status'],'succeeded')
        self.assertEqual(self.b.posts, [])

    def test_duplicate_completion(self):
        self.finish()
        job = self.latest('S2.video')
        self.assertEqual(self.r.complete(job['id'],job['external_id'],job['output']), 'duplicate')
        with self.assertRaises(Conflict):
            self.r.complete(job['id'],job['external_id'],'conflict.mp4')

    def test_backend_mismatch_skips_recovery(self):
        pass_once(self.r,self.b,max_submit=20)
        self.b.finish(); self.b.base='mock://different'
        events=pass_once(self.r,self.b,max_submit=0)
        self.assertTrue(all(e['action']=='backend_mismatch' for e in events))
        self.assertIsNone(self.r.snapshot()['timeline'])

    def test_definitive_failure_no_automatic_retry(self):
        self.b.mode='reject'
        pass_once(self.r,self.b,max_submit=20)
        posts=len(self.b.posts)
        pass_once(self.r,self.b,max_submit=20)
        self.assertEqual(len(self.b.posts),posts)
        self.assertEqual(self.latest('S2.video')['status'],'failed')

    def test_input_fingerprint_blocks_changed_inputs(self):
        pass_once(self.r,self.b,max_submit=20)
        job=self.latest('S2.video')
        # Inject corruption/bypass of the public API to exercise the final guard.
        a=self.r.db.execute("SELECT spec FROM vf_assets WHERE id='S2.video'").fetchone()
        spec=json.loads(a['spec']); spec['values']['hero.costume']='corrupt'
        self.r.db.execute("UPDATE vf_assets SET spec=? WHERE id='S2.video'",(encode(spec),))
        self.assertEqual(self.r.complete(job['id'],job['external_id'],'wrong.mp4'),'stale')
        self.assertIsNone(self.r.snapshot()['timeline'])

    def test_reference_provenance_and_unsupported_adapter(self):
        # Add a declared reference to the fixture before importing into a fresh DB.
        self.r.close(); self.r=AssetRuntime(str(Path(self.tmp.name)/'references.db'))
        manifest=copy.deepcopy(self.manifest)
        manifest['edges']=[dict(parent='S2.video',child='S4.video',kind='reference')]
        self.r.initialize(manifest)
        pass_once(self.r,self.b,max_submit=20); self.b.finish()
        events=pass_once(self.r,self.b,max_submit=20)
        self.assertEqual(self.latest('S4.video')['status'],'pending')
        self.assertTrue(any(e['action']=='validation_error' for e in events))
        def reference_build(inputs,jid):
            # Test-only adapter demonstrating dependency snapshot consumption.
            if inputs['dependencies']:
                self.assertIsNotNone(inputs['dependencies'][0]['input_hash'])
            return dict(mock_references=inputs['dependencies'])
        self.finish(build=reference_build)
        posts=len(self.b.posts)
        p=self.r.edit(self.event(scope='local'))
        self.assertEqual(p['regenerate'],['S2.video','S4.video'])
        self.finish(build=reference_build)
        self.assertEqual(len(self.b.posts)-posts,2)
        inputs=json.loads(self.latest('S4.video')['input_json'])
        self.assertEqual(inputs['dependencies'][0]['revision'],2)

    def test_state_dependency_propagates_and_rejects_conflict(self):
        self.r.close(); self.r=AssetRuntime(str(Path(self.tmp.name)/'state.db'))
        manifest=copy.deepcopy(self.manifest)
        manifest['edges']=[dict(parent='S2.video',child='S4.video',kind='state',keys=['hero.costume'])]
        self.r.initialize(manifest)
        self.finish()
        with self.assertRaises(Conflict):
            self.r.edit(self.event(target='S4.video',scope='local'))
        self.assertEqual(self.r.snapshot()['revision'],1)
        p=self.r.edit(self.event(scope='local'))
        self.assertEqual(p['state_changed'],['S2.video','S4.video'])
        self.finish()

    def test_bad_manifest_rolls_back(self):
        other=AssetRuntime(str(Path(self.tmp.name)/'invalid.db'))
        try:
            invalid=copy.deepcopy(self.manifest)
            invalid['assets'][1]['spec']['reads']=[]
            with self.assertRaises(ValueError): other.initialize(invalid)
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM vf_assets').fetchone()[0],0)
        finally:
            other.close()

    def test_double_reservation_is_noop(self):
        job=self.latest('S2.video')
        first=self.r.prepare(job['id'],self.b.base,build_h3)
        self.assertIsNotNone(first)
        self.assertIsNone(self.r.prepare(job['id'],self.b.base,build_h3))

if __name__=='__main__': unittest.main()
