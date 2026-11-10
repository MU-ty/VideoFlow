"""Replay two published H3 target shots with one controlled prompt change."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifacts import checksum, trusted_download, write_json
from asset_runtime import fingerprint
from comfyui import ComfyUI, SubmissionUncertain, render_template


def paired_workflows(original):
    prompt = original['5']['inputs']['prompt']
    scene, separator, edit = prompt.partition(' Apply the following appearance instruction')
    if not separator or scene.count('ribbed amber handwheel') != 1 or 'to scarlet red' not in edit:
        raise ValueError('expected the archived press prompt and final red edit')
    neutral = scene.replace('ribbed amber handwheel', 'ribbed handwheel', 1)
    return dict(original=original, neutral=render_template(original, neutral + separator + edit, '5', 'prompt'))


def load_targets(root):
    arm = root / 'online_egdr'
    manifest = json.loads((arm / 'media' / 'revision-3.json').read_text(encoding='utf-8'))
    targets = {item['asset']: item for item in manifest['assets']}
    with closing(sqlite3.connect((arm / 'project.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        for shot in ('S1', 'S2'):
            item = targets[shot]
            job = db.execute(
                "SELECT execution_json,input_hash,output FROM vf_jobs WHERE asset=? AND revision=? AND status='succeeded'",
                (shot, item['revision']),
            ).fetchone()
            if not job or (job[1], job[2]) != (item['input_hash'], item['output']):
                raise ValueError(f'{shot}: published workflow does not match the ledger')
            yield shot, item['input_hash'], item['sha256'], paired_workflows(json.loads(job[0]))


def render_pair_member(backend, workflow, output, shot, style, timeout, poll):
    prompt_id = str(uuid4())
    record_path = output / f'{shot}-{style}.json'
    workflow['14']['inputs']['filename_prefix'] = f'videoflow/egdr_prompt_pair/{prompt_id}'
    record = dict(shot=shot, style=style, prompt_id=prompt_id, workflow=workflow)
    write_json(record_path, record)
    try:
        backend.submit(workflow, prompt_id)
    except SubmissionUncertain:
        # Inspect the original request ID. A second POST could duplicate work.
        record['acknowledgement'] = 'uncertain'
        write_json(record_path, record)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, value = backend.inspect(prompt_id)
        if state in ('succeeded', 'failed'):
            record.update(state=state, inspection=backend.last_inspection)
            write_json(record_path, record)
            if state == 'failed':
                raise RuntimeError(f'{shot}/{style}: {value}')
            filename = parse_qs(urlsplit(value).query)['filename'][0]
            path = output / f'{shot}-{style}{Path(filename).suffix.lower()}'
            trusted_download(value, backend.base, path, max_bytes=2 * 1024**3, timeout=60)
            record.update(file=path.name, sha256=checksum(path))
            write_json(record_path, record)
            return record
        time.sleep(poll)
    raise TimeoutError(f'{shot}/{style}: timed out inspecting {prompt_id}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--timeout', default=1800, type=float)
    parser.add_argument('--poll', default=3, type=float)
    args = parser.parse_args()
    if not all(0 < v < float('inf') for v in (args.timeout, args.poll)):
        parser.error('timeout and poll must be finite and positive')
    targets = list(load_targets(args.source_run))
    backend = ComfyUI(args.server)
    queue = backend._request('/queue')
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise RuntimeError('start with an idle, dedicated ComfyUI queue')
    args.output.mkdir(parents=True)
    rows = []
    for shot, input_hash, source_sha256, variants in targets:
        order = ('original', 'neutral') if shot == 'S1' else ('neutral', 'original')
        for style in order:
            print(f'{shot} {style}', flush=True)
            row = render_pair_member(backend, variants[style], args.output, shot, style, args.timeout, args.poll)
            row.update(archived_input_hash=input_hash, archived_output_sha256=source_sha256)
            rows.append(row)
    write_json(args.output / 'report.json', dict(evidence='paired_H3_prompt_diagnostic',
               quality='requires_independent_full_clip_review', rows=rows, workflow_digest=fingerprint(
                   [row['workflow'] for row in rows])))


if __name__ == '__main__':
    main()
