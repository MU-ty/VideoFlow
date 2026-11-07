"""Run a real generated-reference chain using frames from committed H3 videos."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib import parse, request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import NoRedirect, archive_current, checksum, trusted_download, write_json
from asset_runtime import AssetRuntime, render
from asset_worker import pass_once
from comfyui import ComfyUI, h3_workflow
from provider_audit import capture_environment, summarize_inspections
from recovery_matrix import SubmissionView


CHAINS = {
    'chain': [('S1', 'S2'), ('S2', 'S3'), ('S3', 'S4')],
    'fanout': [('S1', 'S2'), ('S1', 'S3'), ('S2', 'S4')],
}
PROMPTS = {
    'S1': 'A close-up of a {{color}} ceramic teapot with a white stripe, resting on a wooden table, stable lighting.',
    'S2': 'Continue from this frame. Slowly move closer to the same striped ceramic teapot.',
    'S3': 'Continue from this frame. Slowly orbit the same striped ceramic teapot, keeping it centered.',
    'S4': 'Continue from this frame. Hold on the same striped ceramic teapot, preserving its glaze color.',
    'S5': 'A static cutaway of a wooden door with a brass handle, no teapot visible.',
}


def upload_frame(backend, frame, digest):
    """Upload an immutable input filename and verify what the provider will load."""
    filename = digest + '.png'
    boundary = 'videoflow-' + uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        'Content-Type: image/png\r\n\r\n',
        frame.read_bytes(),
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n'
        f'videoflow_refs\r\n--{boundary}--\r\n',
    ]
    body = b''.join(part.encode() if isinstance(part, str) else part for part in parts)
    req = request.Request(backend.base + '/upload/image', data=body,
                          headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with request.build_opener(NoRedirect()).open(req, timeout=backend.timeout) as response:
        uploaded = json.load(response)
    if (uploaded.get('name'), uploaded.get('subfolder'), uploaded.get('type')) != (
            filename, 'videoflow_refs', 'input'):
        raise RuntimeError(f'unexpected ComfyUI frame upload response: {uploaded}')
    url = backend.base + '/view?' + parse.urlencode(dict(
        filename=filename, subfolder='videoflow_refs', type='input'))
    with tempfile.TemporaryDirectory() as tmp:
        verified = Path(tmp) / filename
        trusted_download(url, backend.base, verified, 20 * 1024**2, backend.timeout)
        if checksum(verified) != digest:
            raise RuntimeError('uploaded reference bytes failed SHA-256 verification')
    return 'videoflow_refs/' + filename


class ReferenceFrames:
    def __init__(self, directory, backend):
        self.directory = directory
        self.backend = backend
        self.staged = {}
        self.names = {}
        directory.mkdir()

    def stage(self, url):
        if url in self.staged:
            return
        with tempfile.TemporaryDirectory(dir=self.directory) as tmp:
            video = Path(tmp) / 'source-video'
            frame = Path(tmp) / 'first.png'
            trusted_download(url, self.backend.base, video, 2 * 1024**3, self.backend.timeout)
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(video),
                            '-map', '0:v:0', '-frames:v', '1', '-threads', '1', str(frame)],
                           check=True, timeout=60)
            digest = checksum(frame)
            if digest not in self.names:
                self.names[digest] = upload_frame(self.backend, frame, digest)
                shutil.copyfile(frame, self.directory / (digest + '.png'))
            self.staged[url] = dict(source_video_sha256=checksum(video), frame_sha256=digest,
                                    provider_image=self.names[digest])
        write_json(self.directory / 'lineage.json', self.staged)

    def build(self, inputs, job_id):
        refs = [edge for edge in inputs['dependencies'] if edge['kind'] == 'reference']
        if len(refs) > 1:
            raise ValueError('the first-frame protocol supports one incoming reference per shot')
        workflow = h3_workflow(render(inputs['spec']), prefix='videoflow/ref-' + job_id,
                               **inputs['spec'].get('config', {}))
        if refs:
            url = refs[0]['output']
            if url not in self.staged:
                raise ValueError('reference frame was not staged from the current upstream artifact')
            workflow['15'] = {'class_type': 'LoadImage', 'inputs': {
                'image': self.staged[url]['provider_image']}}
            workflow['5']['inputs']['first_frame'] = ['15', 0]
        return workflow


def run_trial(root, backend, story, method, seed, cap, poll, horizon, supersede):
    root.mkdir()
    queue = backend._request('/queue')
    if any(queue.get(key) for key in ('queue_running', 'queue_pending')):
        raise RuntimeError('use a dedicated idle ComfyUI queue')
    inputs = backend._request('/object_info/MiniMaxH3ImageToVideo')
    image_node = backend._request('/object_info/LoadImage')
    if ('first_frame' not in inputs.get('MiniMaxH3ImageToVideo', {}).get('input', {}).get('optional', {})
            or 'LoadImage' not in image_node):
        raise RuntimeError('ComfyUI must expose H3 first_frame and LoadImage inputs')

    refs = ReferenceFrames(root / 'references', backend)
    edges = [dict(parent=a, child=b, kind='reference') for a, b in CHAINS[story]]
    runtime = AssetRuntime(str(root / 'project.db'))
    try:
        runtime.initialize(dict(assets=[
            dict(id=shot, scene=shot, kind='video',
                 spec=dict(template=prompt, values={'color': 'white'} if shot == 'S1' else {},
                           reads=['color'] if shot == 'S1' else [], cost=1,
                           config={'seed': seed + index}))
            for index, (shot, prompt) in enumerate(PROMPTS.items())], edges=edges))
        capture_environment(runtime, backend)

        def step(submission_limit=cap):
            events = pass_once(runtime, backend, build=refs.build, max_submit=0)
            free = min(submission_limit, max(0, cap - len(runtime.unresolved_remote())))
            if free:
                for job in runtime.pending()[:free]:
                    for edge in runtime._inputs(job['asset'])['dependencies']:
                        if edge['kind'] == 'reference':
                            refs.stage(edge['output'])
                events.extend(pass_once(SubmissionView(runtime), backend, build=refs.build, max_submit=free))
            with (root / 'worker.jsonl').open('a') as log:
                log.write(json.dumps(dict(at=time.time(), events=events)) + '\n')
            failures = [event for event in events
                        if event['action'] in ('failed', 'rejected', 'validation_error')]
            if failures:
                raise RuntimeError(f'workflow failed: {failures}')
            return events

        def wait_for_publication(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                step()
                if runtime.snapshot()['timeline'] is not None and not runtime.unresolved_remote():
                    return
                time.sleep(min(poll, max(0, deadline - time.monotonic())))
            raise TimeoutError('no complete, drained publication before the phase deadline')

        def edit(revision, color):
            return runtime.edit(dict(id=f'edit-{revision}', expected_revision=revision,
                                     target='S1', key='color', value=color, method=method))

        wait_for_publication(horizon)
        before = archive_current(runtime, root / 'media', backend.base, probe=True)
        plans = [edit(1, 'blue')]
        obsolete = None
        if supersede:
            step(submission_limit=1)
            jobs = [job for job in runtime.active() if job['asset'] == 'S1' and job['revision'] == 2]
            if len(jobs) != 1 or backend.inspect(jobs[0]['external_id'])[0] != 'running':
                raise RuntimeError('revision 2 S1 did not reach the in-flight edit barrier')
            obsolete = jobs[0]['id']
            plans.append(edit(2, 'red'))
        wait_for_publication(horizon)
        after = archive_current(runtime, root / 'media', backend.base, probe=True)
        snapshot = runtime.snapshot()
        initial = {row['asset']: row for row in before['assets']}
        final = {row['asset']: row for row in after['assets']}
        expected = list(PROMPTS) if method == 'full' else list(PROMPTS)[:-1]
        if any(plan['regenerate'] != expected for plan in plans) or set(final) != set(PROMPTS):
            raise RuntimeError('incorrect invalidation or incomplete manifest')
        independent_reused = all(final['S5'][field] == initial['S5'][field]
                                 for field in ('sha256', 'input_hash', 'output'))
        if independent_reused != (method != 'full'):
            raise RuntimeError('independent shot reuse disagrees with the declared method')
        jobs = {(job['asset'], job['revision']): job for job in snapshot['jobs']}
        evidence = []
        for manifest in (initial, final):
            for parent, child in CHAINS[story]:
                upstream, downstream = manifest[parent], manifest[child]
                job = jobs[(child, downstream['revision'])]
                dep, = json.loads(job['input_json'])['dependencies']
                workflow = json.loads(job['execution_json'])
                stage = refs.staged[upstream['output']]
                if (dep['asset'], dep['revision'], dep['input_hash'], dep['output']) != (
                        parent, upstream['revision'], upstream['input_hash'], upstream['output']):
                    raise RuntimeError(f'{child} did not consume the committed {parent} revision')
                if (stage['source_video_sha256'] != upstream['sha256']
                        or workflow['15']['inputs']['image'] != stage['provider_image']
                        or workflow['5']['inputs']['first_frame'] != ['15', 0]):
                    raise RuntimeError(f'{child} did not bind the verified upstream frame')
                evidence.append(dict(parent=parent, child=child, parent_revision=dep['revision'],
                                     child_revision=downstream['revision'], **stage))
        published_revisions = [row[0] for row in runtime.db.execute(
            'SELECT revision FROM vf_timelines ORDER BY revision')]
        stale_rejected = None
        if obsolete is not None:
            job = next(job for job in snapshot['jobs'] if job['id'] == obsolete)
            stale_rejected = (job['status'] == 'superseded' and job['remote_state'] == 'succeeded'
                              and job['output'] is None and 2 not in published_revisions)
            if not stale_rejected:
                raise RuntimeError('superseded S1 output was not proven to be rejected')
        publish_at = runtime.db.execute('SELECT published_at FROM vf_timelines WHERE revision=?',
                                        (after['revision'],)).fetchone()[0]
        edit_at = runtime.db.execute('SELECT applied_at FROM vf_events WHERE revision=?',
                                     (after['revision'],)).fetchone()[0]
        report = dict(last_edit_to_publication_seconds=publish_at - edit_at, story=story, method=method, seed=seed, plans=plans,
                      manifest_revisions=published_revisions, references=evidence,
                      changed_reference_frame_edges=[f"{a['parent']}->{a['child']}"
                                                     for a, b in zip(evidence[:3], evidence[3:])
                                                     if a['frame_sha256'] != b['frame_sha256']],
                      independent_reused=independent_reused, stale_result_rejected=stale_rejected,
                      submission_intents=sum(job['submission_intents'] for job in snapshot['jobs']),
                      provider_history=summarize_inspections(runtime))
        write_json(root / 'report.json', report)
        return report
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--story', choices=tuple(CHAINS), default='chain')
    parser.add_argument('--methods', choices=('dair', 'attr_dag', 'full'), nargs='+', default=['dair'])
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--start-repetition', type=int, default=1)
    parser.add_argument('--seed', type=int, default=3107)
    parser.add_argument('--cap', type=int, default=2)
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--horizon-seconds', type=float, default=3600)
    parser.add_argument('--supersede', action='store_true')
    args = parser.parse_args()
    if (not 0 < args.poll_seconds < args.horizon_seconds < float('inf')
            or min(args.repetitions, args.start_repetition, args.cap) < 1 or args.seed < 0
            or args.seed + 4093 * (args.start_repetition + args.repetitions - 2) + 4 >= 2**64
            or len(args.methods) != len(set(args.methods))):
        parser.error('use finite positive timeouts, unique methods, positive repetitions/cap and a nonnegative seed')
    if not all(shutil.which(exe) for exe in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    backend = ComfyUI(args.server)
    backend.environment()
    args.output.mkdir(parents=True)
    protocol = vars(args).copy()
    protocol.update(output=str(args.output), evidence='live_comfyui_reference_conditioning',
                    seed_step=4093, model='MiniMax H3 FL2VA', reference='first decoded frame')
    write_json(args.output / 'protocol.json', protocol)
    rows = []
    for index in range(args.start_repetition - 1, args.start_repetition - 1 + args.repetitions):
        for method in (args.methods if index % 2 == 0 else reversed(args.methods)):
            seed = args.seed + index * 4093
            label = f'{index+1:02d}-{method}'
            print(f'START {label}', flush=True)
            try:
                result = run_trial(args.output / label, backend, args.story, method, seed, args.cap,
                                   args.poll_seconds, args.horizon_seconds, args.supersede)
            except Exception as exc:
                write_json(args.output / 'summary.json', dict(complete=False, failed=label,
                           error=str(exc), rows=rows))
                raise
            rows.append(result)
            write_json(args.output / 'summary.json', dict(
                complete=len(rows) == args.repetitions * len(args.methods), rows=rows))
            print(f'DONE {label}: {result["submission_intents"]} submission intents', flush=True)


if __name__ == '__main__':
    main()
