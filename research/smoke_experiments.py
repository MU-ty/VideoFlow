"""Integration tests with a virtual clock and synthetic HTTP provider. No GPU."""
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from threading import Thread
import time
from unittest.mock import patch
from urllib import parse

import recovery_observability_comfy as workflow
import recovery_reference_comfy as reference
from comfyui import ComfyUI
from smoke_grace import Clock, Provider
from summarize_workflow import summarize


def smoke_workflow(root):
    rows = []
    for repetition in range(1, 4):
        for story, cap in [('independent', 2), ('independent', 3), ('independent', 4), ('ordered', 3)]:
            for fault in workflow.WORKFLOW_FAULTS:
                for method in workflow.WORKFLOW_POLICIES:
                    clock = Clock()
                    provider = Provider(clock)
                    label = f'{repetition}-{story}-{cap}-{fault}-{method}'
                    with patch.object(time, 'monotonic', clock.now), patch.object(time, 'sleep', clock.sleep), \
                            patch.object(time, 'time', lambda: 1_800_000_000 + clock.now()), \
                            patch.object(workflow, 'ComfyUI', lambda *_: provider):
                        row = workflow.run_trial(root / label, provider, method, fault, 5, 180,
                                                 10000 * (repetition - 1), cap, story)
                    assert row['correct_publication_seconds'] is not None
                    assert not row['stale_publications']
                    assert row['original_s6_reused'] == (story == 'independent')
                    assert row['accepted_count'] == (5 if fault == 'opaque' else 4)
                    assert set(row['replacements']) == ({'S4'} if fault == 'opaque' else set())
                    if fault == 'opaque':
                        assert row['replacements']['S4']['queued_after_seconds'] == 15
                    rows.append(dict(repetition=repetition, **row))
    results = summarize(rows, require_boundary=True)
    for stratum in results:
        assert stratum['both_published'] == 3
        assert all(pair['publication_seconds_delta'] == 0 and pair['acceptances_delta'] == 0
                   for pair in stratum['pairs'])
    mismatched = dict(rows[0], workflow_pair=dict(rows[0]['workflow_pair'], first_poll_slots=1))
    try:
        summarize([mismatched, rows[1]], require_boundary=True)
    except ValueError as error:
        assert 'mismatched workflow pair boundary' in str(error)
    else:
        raise AssertionError('mismatched capacity was accepted')
    censored = dict(rows[0], correct_publication_seconds=None)
    censor_summary = summarize([censored, rows[1]])[0]
    assert censor_summary['both_published'] == 0 and censor_summary['median_publication_seconds_delta'] is None
    sensitivity = []
    for method in workflow.WORKFLOW_POLICIES:
        clock = Clock()
        provider = Provider(clock)
        with patch.object(time, 'monotonic', clock.now), patch.object(time, 'sleep', clock.sleep), \
                patch.object(time, 'time', lambda: 1_800_000_000 + clock.now()), \
                patch.object(workflow, 'ComfyUI', lambda *_: provider):
            result = workflow.run_trial(root / ('sensitivity-' + method), provider, method, 'transient_wall',
                                        5, 180, 0, transient_seconds=18, workflow_grace=20,
                                        workflow_fixed_poll=True)
        sensitivity.append(dict(repetition=1, **result))
    sensitivity_result = summarize(sensitivity)[0]
    assert sensitivity_result['pairs'][0]['acceptances_delta'] == 1
    return dict(evidence='deterministic_simulation_no_gpu', trials=len(rows),
                paired_strata=results, deadline_censor_test='passed',
                sensitivity_trials=2, sensitivity=sensitivity_result)


def smoke_reference(root):
    videos = {}
    frames = {}
    for color in ('white', 'blue', 'red', 'green'):
        video, frame = root / f'{color}.mp4', root / f'{color}.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=32x32:d=0.4',
                        '-threads', '1', '-pix_fmt', 'yuv420p', str(video)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-frames:v', '1', '-threads', '1',
                        str(frame)], check=True)
        videos[color], frames[frame.read_bytes()] = video.read_bytes(), color
    jobs, uploaded = {}, {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            url = parse.urlsplit(self.path)
            path = url.path
            if path == '/system_stats':
                self.respond(dict(synthetic=True))
            elif path == '/queue':
                self.respond(dict(queue_pending=[], queue_running=[
                    [0, pid] for pid, job in jobs.items() if job['checks'] == 0]))
            elif path == '/object_info/MiniMaxH3ImageToVideo':
                self.respond(dict(MiniMaxH3ImageToVideo=dict(input=dict(optional=dict(first_frame=['IMAGE'])))))
            elif path == '/object_info/LoadImage':
                self.respond(dict(LoadImage={}))
            elif path.startswith('/history/'):
                pid = path.rsplit('/', 1)[1]
                job = jobs[pid]
                job['checks'] += 1
                if job['checks'] <= 2:
                    self.respond({pid: {'status': {'completed': False}}})
                else:
                    self.respond({pid: {'status': {'completed': True}, 'outputs': {'14': {
                        'videos': [dict(filename=pid + '.mp4', type='output')]}}}})
            elif path == '/view':
                query = parse.parse_qs(url.query)
                name = query['filename'][0]
                if query['type'] == ['input']:
                    self.respond(uploaded['videoflow_refs/' + name])
                else:
                    self.respond(videos[jobs[name.removesuffix('.mp4')]['color']])
            else:
                self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path == '/upload/image':
                header = ('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode()
                message = BytesParser(policy=policy.default).parsebytes(header + body)
                parts = {part.get_param('name', header='Content-Disposition'): part for part in message.iter_parts()}
                name = parts['image'].get_filename()
                subfolder = parts['subfolder'].get_payload(decode=True).decode()
                uploaded[subfolder + '/' + name] = parts['image'].get_payload(decode=True)
                self.respond(dict(name=name, subfolder=subfolder, type='input'))
            elif self.path == '/prompt':
                payload = json.loads(body)
                graph = payload['prompt']
                node = graph['5']['inputs']
                if 'first_frame' in node:
                    assert node['first_frame'] == ['15', 0]
                    color = frames[uploaded[graph['15']['inputs']['image']]]
                else:
                    color = next((color for color in videos if f'of a {color} ceramic' in node['prompt']), 'green')
                pid = payload['prompt_id']
                jobs[pid] = dict(color=color, checks=0)
                self.respond(dict(prompt_id=pid))
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        backend = ComfyUI(f'http://127.0.0.1:{server.server_port}', timeout=5)
        for story in reference.CHAINS:
            for method in ('dair', 'attr_dag', 'full'):
                jobs.clear()
                report = reference.run_trial(root / f'{story}-{method}', backend, story, method, 3107,
                                             cap=2, poll=0.01, horizon=25, supersede=True)
                assert report['stale_result_rejected']
                assert len(report['changed_reference_frame_edges']) == 3
                assert report['independent_reused'] == (method != 'full')
                assert report['submission_intents'] == (11 if method == 'full' else 10)
                results.append({key: report[key] for key in (
                    'story', 'method', 'manifest_revisions', 'changed_reference_frame_edges',
                    'stale_result_rejected', 'independent_reused', 'submission_intents')})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return dict(evidence='synthetic_comfyui_http_fixture_no_gpu', trials=len(results), cases=results)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        recovery, references = root / 'recovery', root / 'references'
        recovery.mkdir()
        references.mkdir()
        report = dict(workflow=smoke_workflow(recovery), references=smoke_reference(references))
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + '\n')
    print('50 deterministic workflow trials, six synthetic reference trials, and one censor check passed.')


if __name__ == '__main__':
    main()
