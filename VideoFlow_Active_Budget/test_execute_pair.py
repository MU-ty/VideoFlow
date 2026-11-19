"""Exercise the paired worker and lineage with a deterministic in-memory provider."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from active_budget import replay
from execute_pair import NATURAL, render_arm

sys.path.insert(0, str(NATURAL))
from run_joint import KEY, generated_manifest, live_setup, load_json, log

API = live_setup(NATURAL / 'runtime')
from artifacts import archive_current
from recovery_reference_comfy import ReferenceFrames


class LocalProvider:
    base = 'mock:h3'

    def __init__(self):
        self.outputs = {}
        self.last_inspection = None

    def submit(self, workflow, prompt_id):
        self.outputs[prompt_id] = f'mock://output/view?filename={prompt_id}.mp4'

    def inspect(self, prompt_id):
        self.last_inspection = dict(prompt_id=prompt_id, history_entry={'status': {'messages': []}})
        return 'succeeded', self.outputs[prompt_id]


class LocalReferences(ReferenceFrames):
    def stage(self, url):
        self.staged[url] = dict(source_video_sha256=API['fingerprint'](url),
                                frame_sha256='mock-frame', provider_image='mock.png')


def local_archive(runtime, folder, backend, **kwargs):
    def download(url, backend, target, max_bytes, timeout):
        # The fake URL is itself the content delivered by the fake provider.
        from json import dumps
        Path(target).write_bytes(dumps(url, separators=(',', ':')).encode())
    return archive_current(runtime, folder, backend, downloader=download, probe=False)


def main():
    cases = load_json(Path(__file__).with_name('holdout_930_protocol.json'))['cases']
    case = next(c for c in cases.values() if c['expected'] == ['S4'] and c['edges'][0][0] != 'S4')
    target, = case['expected']
    distractor, = set(case['candidates']) - {target}
    bank = {target: [{'votes': ['target', 'target']}] * 4,
            distractor: [{'votes': ['distinct', 'distinct']}] * 4}
    api = dict(API, ReferenceFrames=LocalReferences, archive_current=local_archive)
    args = SimpleNamespace(timeout=3, poll=.001)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = api['AssetRuntime'](root / 'source.db')
        provider = LocalProvider()
        refs = LocalReferences(root / 'refs-before', provider)
        runtime.initialize(generated_manifest(case))
        try:
            for _ in range(8):
                for job in runtime.pending():
                    for dep in runtime._inputs(job['asset'])['dependencies']:
                        refs.stage(dep['output'])
                api['pass_once'](runtime, provider, refs.build, max_submit=6)
                if runtime.snapshot()['timeline']:
                    break
            initial = local_archive(runtime, root / 'source_media', provider.base)
        finally:
            runtime.close()
        origin = {entry['asset']: entry for entry in initial['assets']}
        for arm, jobs, exact in [('fixed', 5, False), ('uncertainty', 2, True)]:
            plan = replay(case, bank, arm, 3, [target, distractor])
            evidence = dict(plan, evidence=[dict(shot=s, view=i, **bank[s][i-1]) for s, i in plan['trace']])
            row = render_arm(args, api, provider, root / arm, root, origin, case, evidence, KEY, log)
            assert row['first_publication_covers_required']
            assert row['first_publication_exact'] == exact
            assert row['completed_edit_jobs'] == row['acknowledged_submissions'] == jobs
        print('Paired execution passed: S4 target, two propagated distractor descendants, exact publication and source reuse.')


if __name__ == '__main__':
    main()
