"""Compare completed post-generation runs, verify reuse and optionally decoded video."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import checksum, write_json
from asset_runtime import fingerprint
from research.compare_reports import load_run


def decoded_hash(path):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                             '-f', 'hash', '-hash', 'sha256', '-'],
                            capture_output=True, check=True, timeout=300)
    return result.stdout.decode().strip()


def compare(directories, decode=False):
    rows, configs, finals, methods = [], [], {}, set()
    cache = {}
    for directory in directories:
        root = Path(directory).resolve()
        config, report = load_run(root)
        method = report['method']
        if method in methods:
            raise ValueError('duplicate method')
        methods.add(method)
        configs.append(config)
        archives = []
        for revision in (1, report['project_revision']):
            archive = json.loads((root / f'artifacts/revision-{revision}.json').read_text())
            entries = {}
            for asset in archive['assets']:
                if asset['asset'] in entries:
                    raise ValueError('duplicate artifact')
                path = (root / 'artifacts' / asset['file']).resolve()
                if not path.is_relative_to(root / 'artifacts') or checksum(path) != asset['sha256']:
                    raise ValueError('artifact hash/path mismatch')
                entries[asset['asset']] = asset
                if decode and asset['kind'] == 'video':
                    if asset['sha256'] not in cache:
                        cache[asset['sha256']] = decoded_hash(path)
                    asset['decoded_video_hash'] = cache[asset['sha256']]
            archives.append(entries)
        initial, final = archives
        if initial.keys() != final.keys():
            raise ValueError('asset set changed')
        retained = [k for k in initial if initial[k]['revision'] == final[k]['revision']]
        for k in retained:
            if any(initial[k][v] != final[k][v] for v in ('file', 'sha256', 'input_hash', 'output')):
                raise ValueError('retained artifact changed')
        if set(retained) != set(report['metrics']['planned_reuse']):
            raise ValueError('observed reuse differs from plan')
        edit_jobs = {x['job'] for x in report['task_observations']
                     if x['revision'] > 1 and x['asset'].endswith('.video')}
        observations = [x for x in report.get('provider_observations', {}).get('jobs', [])
                        if x['job'] in edit_jobs]
        span = (sum(x['execution_span_seconds'] for x in observations)
                if len(observations) == len(edit_jobs) and all(x['execution_span_seconds'] is not None for x in observations)
                else None)
        rows.append(dict(method=method, edit_submission_intents=report['metrics']['edit_submission_intents'],
                         edit_completion_observed_seconds=report['metrics']['edit_completion_observed_seconds'],
                         edit_server_execution_span_seconds=span, retained_assets=retained,
                         retained_videos=[k for k in retained if final[k]['kind'] == 'video'],
                         all_archive_hashes_verified=True))
        finals[method] = final
    if len(rows) < 2 or any(c != configs[0] for c in configs):
        raise ValueError('need at least two distinct matched runs')
    base = finals.get('full', finals[rows[0]['method']])
    for method, final in finals.items():
        if final.keys() != base.keys():
            raise ValueError('method asset sets differ')
    equality = None
    if decode:
        equality = {k: all(f[k]['decoded_video_hash'] == base[k]['decoded_video_hash'] for f in finals.values())
                    for k in base if base[k]['kind'] == 'video'}
    return dict(protocol='initial_complete_then_edit_v1', matched_configuration_hash=fingerprint(configs[0]),
                rows=rows, decoded_video_equal_by_asset=equality, gpu_seconds=None, api_cost=None,
                semantic_quality=None, interpretation='Single matched case; decoded equality covers video frames only, not audio. '
                'Server spans include loading/CPU/I/O. Client time includes polling/queue. No quality or GPU-cost claim.')


def markdown(result):
    lines = ['| Method | Edit video intents | Client seconds | Server span seconds | Retained videos |',
             '|---|---:|---:|---:|---:|']
    for r in result['rows']:
        span = r['edit_server_execution_span_seconds']
        lines.append(f"| {r['method']} | {r['edit_submission_intents']} | {r['edit_completion_observed_seconds']:.2f} | {format(span, '.3f') if span is not None else 'unknown'} | {len(r['retained_videos'])} |")
    return '\n'.join(lines) + '\n\n' + result['interpretation'] + '\n'


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directories', nargs='+'); p.add_argument('--decode', action='store_true')
    p.add_argument('--output', required=True); p.add_argument('--markdown')
    a = p.parse_args(); result = compare(a.directories, a.decode); write_json(a.output, result)
    table = markdown(result)
    if a.markdown:
        Path(a.markdown).write_text(table)
    print(table)
