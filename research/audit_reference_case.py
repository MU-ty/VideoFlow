"""Check a reference-chain trial from archived bytes and its read-only SQLite ledger."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import checksum, write_json
from asset_runtime import fingerprint
from recovery_reference_comfy import CHAINS, PROMPTS


def read(path):
    return json.loads(path.read_text())


def audit(trial, frames=None):
    report = read(trial / 'report.json')
    if report['manifest_revisions'] != [1, 3]:
        raise ValueError('expected the two-edit supersession protocol')
    media = (trial / 'media').resolve()
    edges = CHAINS[report['story']]
    archived = {}
    with closing(sqlite3.connect((trial / 'project.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('invalid trial database')
        jobs = {(row['asset'], row['revision']): row for row in db.execute('SELECT * FROM vf_jobs')}
        manifests = db.execute('SELECT * FROM vf_timelines ORDER BY revision').fetchall()
        if [row['revision'] for row in manifests] != [1, 3]:
            raise ValueError('unexpected published revisions')
        for row in manifests:
            revision = row['revision']
            ledger = json.loads(row['entries'])
            manifest = read(media / f'revision-{revision}.json')
            assets = {item['asset']: item for item in manifest['assets']}
            if (manifest['revision'] != revision or fingerprint(ledger) != manifest['timeline_hash']
                    or len(ledger) != len(assets) or set(assets) != set(PROMPTS)):
                raise ValueError(f'manifest does not match revision {revision}')
            for entry in ledger:
                item = assets[entry['asset']]
                if any(item[key] != value for key, value in entry.items()):
                    raise ValueError(f'archived entry differs from ledger: {entry}')
                video = (media / item['file']).resolve()
                if not video.is_relative_to(media) or checksum(video) != item['sha256']:
                    raise ValueError(f'archive hash mismatch: {item["file"]}')
            archived[revision] = assets
        stale = jobs[('S1', 2)]
        if (stale['status'], stale['remote_state'], stale['output']) != ('superseded', 'succeeded', None):
            raise ValueError('revision-2 result was not proven rejected')
        applied = db.execute('SELECT applied_at FROM vf_events WHERE revision=3').fetchone()[0]
        latency = manifests[-1]['published_at'] - applied

    before, after = archived[1], archived[3]
    s5_reused = all(before['S5'][k] == after['S5'][k] for k in ('output', 'input_hash', 'sha256'))
    if s5_reused != (report['method'] != 'full') or s5_reused != report['independent_reused']:
        raise ValueError('unexpected S5 reuse')
    refs = {(item['parent'], item['child'], item['parent_revision']): item for item in report['references']}
    if set(refs) != {(a, b, rev) for a, b in edges for rev in (1, 3)}:
        raise ValueError('unexpected reference edges')
    checks = []
    for (parent, child, rev), item in refs.items():
        parent_item, child_item = (archived[rev][shot] for shot in (parent, child))
        job = jobs[(child, rev)]
        dep, = json.loads(job['input_json'])['dependencies']
        graph = json.loads(job['execution_json'])
        frame_hash = item['frame_sha256']
        frame = trial / 'references' / (frame_hash + '.png')
        expected_ref = 'videoflow_refs/' + frame.name
        if (parent_item['revision'] != rev or child_item['revision'] != rev
                or item['child_revision'] != rev
                or (dep['asset'], dep['kind'], dep['revision'], dep['input_hash'], dep['output'])
                != (parent, 'reference', rev, parent_item['input_hash'], parent_item['output'])
                or parent_item['sha256'] != item['source_video_sha256']
                or item['provider_image'] != expected_ref or checksum(frame) != frame_hash
                or job['input_hash'] != child_item['input_hash'] or job['output'] != child_item['output']
                or graph['15']['class_type'] != 'LoadImage'
                or graph['15']['inputs']['image'] != expected_ref
                or graph['5']['inputs']['first_frame'] != ['15', 0]):
            raise ValueError(f'broken reference binding: {parent}->{child} revision {rev}')
        checks.append(dict(parent=parent, child=child, revision=rev, frame_sha256=frame_hash))

    changed = [f'{a}->{b}' for a, b in edges
               if refs[(a, b, 1)]['frame_sha256'] != refs[(a, b, 3)]['frame_sha256']]
    intents = sum(job['submission_intents'] for job in jobs.values())
    if (changed != report['changed_reference_frame_edges'] or intents != report['submission_intents']
            or abs(latency - report['last_edit_to_publication_seconds']) > 1e-6):
        raise ValueError('report differs from audited records')

    if frames is not None:
        frames.mkdir(parents=True, exist_ok=False)
        for parent, child in edges:
            for rev in (1, 3):
                digest = refs[(parent, child, rev)]['frame_sha256']
                (frames / f'r{rev}-{parent}-to-{child}-input.png').write_bytes(
                    (trial / 'references' / (digest + '.png')).read_bytes())
        for rev, assets in archived.items():
            for shot, item in assets.items():
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '0.2',
                                '-i', str(media / item['file']), '-map', '0:v:0',
                                '-frames:v', '1', '-threads', '1', str(frames / f'r{rev}-{shot}-output.png')],
                               check=True, timeout=60)

    return dict(story=report['story'], method=report['method'], seed=report['seed'],
                manifest_revisions=[1, 3], independent_reused=s5_reused, stale_result_rejected=True,
                changed_reference_frame_edges=changed, references=checks, submission_intents=intents,
                last_edit_to_publication_seconds=latency)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trial', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--frames', type=Path, help='export verified input PNGs and sampled output frames')
    args = parser.parse_args()
    write_json(args.output, audit(args.trial, args.frames))


if __name__ == '__main__':
    main()
