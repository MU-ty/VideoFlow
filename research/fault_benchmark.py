"""Local synthetic execution probes; no GPU/API or video-quality measurements."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_runtime import AssetRuntime
from asset_worker import pass_once
from comfyui import SubmissionUncertain


class Backend:
    base = 'mock://fault-benchmark'
    def __init__(self):
        self.posts = []
        self.jobs = {}
        self.lose_response = False
    def submit(self, graph, pid):
        self.posts.append(pid)
        self.jobs[pid] = ('running', None)
        if self.lose_response:
            raise SubmissionUncertain('injected response loss after acceptance')
    def inspect(self, pid):
        return self.jobs.get(pid, ('unknown', None))
    def finish(self):
        for pid in self.jobs:
            self.jobs[pid] = ('succeeded', 'mock://artifact/'+pid)


def drain(runtime, backend):
    for _ in range(20):
        pass_once(runtime, backend, max_submit=20)
        backend.finish()
        if runtime.snapshot()['timeline'] is not None:
            return
    raise AssertionError('did not complete')


def run(root):
    manifest = json.loads((root/'fixtures/assets.json').read_text())
    rows = []
    for scenario in ('subtitle_only', 'costume_onward', 'restart', 'lost_response', 'stale_callback'):
        begin = perf_counter()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'project.db')
            runtime = AssetRuntime(path)
            try:
                runtime.initialize(manifest)
                backend = Backend()
                event = dict(id='edit-1', expected_revision=1, target='S2.video', key='hero.costume', value='black', scope='onward')
                if scenario in ('subtitle_only','costume_onward'):
                    drain(runtime,backend)
                    before = len(backend.posts)
                    if scenario == 'subtitle_only':
                        event.update(target='S2.subtitle', key='subtitle.text', value='Run!', scope='local')
                    result = runtime.edit(event)
                    drain(runtime,backend)
                    delta = len(backend.posts)-before
                    expected = 0 if scenario == 'subtitle_only' else 3
                    assert delta == expected
                    metrics = dict(new_video_submissions=delta, regenerated_assets=result['regenerate'])
                elif scenario == 'restart':
                    pass_once(runtime,backend,max_submit=20)
                    before = len(backend.posts)
                    runtime.close(); runtime=AssetRuntime(path)
                    drain(runtime,backend)
                    assert len(backend.posts) == before
                    metrics = dict(additional_submissions_after_restart=0, current_timeline_complete=True)
                elif scenario == 'lost_response':
                    backend.lose_response=True
                    pass_once(runtime,backend,max_submit=20)
                    before=len(backend.posts)
                    backend.jobs.clear()
                    for _ in range(3):
                        pass_once(runtime,backend,max_submit=20)
                    assert len(backend.posts)==before
                    unknown=sum(j['status']=='unknown' for j in runtime.active())
                    assert unknown == 6
                    for pid in backend.posts:
                        backend.jobs[pid]=('succeeded','mock://recovered/'+pid)
                    drain(runtime,backend)
                    metrics=dict(unknown_jobs_preserved=unknown, duplicate_submissions=0, current_timeline_complete=True)
                else:
                    pass_once(runtime,backend,max_submit=20)
                    old=next(j for j in runtime.active() if j['asset']=='S2.video')
                    runtime.edit(event)
                    result=runtime.complete(old['id'],old['external_id'],'mock://stale-result')
                    assert result=='stale'
                    drain(runtime,backend)
                    assert all(a['output']!='mock://stale-result' for a in runtime.snapshot()['assets'])
                    metrics=dict(stale_commit_count=0, rejected_old_results=1, current_timeline_complete=True)
                rows.append(dict(scenario=scenario, passed=True, metrics=metrics, local_wall_ms=(perf_counter()-begin)*1000))
            finally:
                runtime.close()
    return dict(evidence='synthetic execution with an in-memory backend and real local SQLite',
                limitations='Not real provider recovery, GPU cost, video quality, API charges or exactly-once evidence.', cases=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',default='results/runtime_faults.json'); args=p.parse_args()
    result=run(Path(__file__).resolve().parents[1])
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2)+'\n')
    for row in result['cases']:
        print(row['scenario'],row['passed'],row['metrics'])
