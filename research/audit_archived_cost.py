"""Recover provider execution spans and final-manifest use from v3.8 ledgers."""
import argparse
from collections import defaultdict
import json
from pathlib import Path, PurePosixPath
import sqlite3
from statistics import median
import tempfile
from zipfile import ZipFile

from recovery_observability_comfy import FAULTS
from artifacts import write_json


def trial(zipped, filename, temp):
    path = PurePosixPath(filename)
    read = lambda name: json.loads(zipped.read(str(path.parent / name)))
    protocol = json.loads(zipped.read(str(path.parent.parent / 'protocol.json')))
    report, boundary = read('report.json'), read('boundary.json')
    temp.write_bytes(zipped.read(filename))
    with sqlite3.connect(temp) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        jobs = {job['external_id']: job for job in db.execute(
            'SELECT * FROM vf_jobs WHERE submission_intents=1')}
        timeline = db.execute('SELECT entries,published_at FROM vf_timelines WHERE revision=2').fetchone()
        applied = db.execute('SELECT applied_at FROM vf_events WHERE revision=2').fetchone()[0]
        histories = {}
        for row in db.execute('SELECT job,payload FROM vf_provider_inspections'):
            item = json.loads(row['payload'])
            pid = item['prompt_id']
            if item['history_entry']:
                assert row['job'] == jobs[pid]['id']
                histories[pid] = item['history_entry']
    assert set(jobs) == set(report['upstream_acceptances']) == set(report['upstream_states']) == set(histories)
    assert len(jobs) == report['accepted_count']
    on_time = report['correct_publication_seconds'] is not None
    if on_time:
        assert timeline and abs(timeline['published_at'] - applied - report['correct_publication_seconds']) < 1e-6
    used = {(item['asset'], item['output']) for item in json.loads(timeline['entries'])} if on_time else set()
    costs = []
    for pid, job in jobs.items():
        history = histories[pid]['status']
        assert report['upstream_states'][pid] == 'succeeded' and history['completed'] and history['status_str'] == 'success'
        stamps = {key: value['timestamp'] for key, value in history['messages']
                  if key in ('execution_start', 'execution_success') and value['prompt_id'] == pid}
        elapsed = (stamps['execution_success'] - stamps['execution_start']) / 1000
        assert elapsed >= 0
        costs.append(dict(shot=job['asset'], original=pid in boundary['original_ids'].values(),
                          in_final_manifest=(job['asset'], job['output']) in used if on_time else None,
                          ledger_status=job['status'], backend_span_seconds=elapsed))
    unused = [job for job in costs if job['in_final_manifest'] is False]
    return dict(batch=path.parts[0], trial=path.parent.name, repetition=int(path.parent.name.split('-')[0]),
                suite=protocol.get('suite', 'primary'), story=report.get('story', 'independent'),
                cap=report.get('recovery_cap', 3), fault=report['fault'], policy=report['policy'],
                published_by_deadline=on_time, jobs=costs,
                backend_span_seconds=round(sum(job['backend_span_seconds'] for job in costs), 3),
                unused_backend_span_seconds=round(sum(job['backend_span_seconds'] for job in unused), 3) if on_time else None)


def primary_comparison(rows):
    matches = defaultdict(dict)
    for row in rows:
        if row['suite'] == 'primary' and row['fault'] in FAULTS:
            matches[(row['fault'], row['repetition'])][row['policy']] = row
    pairs = defaultdict(list)
    for (fault, rep), group in sorted(matches.items()):
        base, vf = [group[key] for key in ('project_version', 'replace_after_3')]
        pairs[fault].append(dict(
            repetition=rep, acceptances_delta=len(vf['jobs']) - len(base['jobs']),
            backend_span_seconds_delta=round(vf['backend_span_seconds'] - base['backend_span_seconds'], 3),
            unused_backend_span_seconds_delta=round(vf['unused_backend_span_seconds']
                                                    - base['unused_backend_span_seconds'], 3)))
    return {fault: dict(pairs=values, medians={
        key: median(row[key] for row in values) for key in values[0] if key != 'repetition'
    }) for fault, values in sorted(pairs.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path, help='evidence directory from the v3.8 reproduction package')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sources = sorted((*args.evidence.glob('comfy-observability-*.zip'), *args.evidence.glob('grace15-*.zip')))
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory) / 'trial.db'
        for source in sources:
            with ZipFile(source) as zipped:
                for path in sorted(zipped.namelist()):
                    if path.endswith('/project.db'):
                        rows.append(trial(zipped, path, temp))
    keys = {(row['suite'], row['story'], row['cap'], row['fault'], row['policy'], row['repetition']) for row in rows}
    if len(rows) != 96 or len(keys) != 96 or sum(len(row['jobs']) for row in rows) != 464:
        raise ValueError('expected the 96 unique v3.8 trials and 464 accepted jobs')
    write_json(args.output, dict(evidence='archived_live_comfyui_reanalysis', trials=len(rows),
                                 successful_provider_histories=464,
                                 timing='provider wall execution span includes host work; not GPU device time',
                                 unused_definition='accepted output absent from an on-time manifest; missing for censored trials',
                                 primary_paired_delta='replace_after_3 minus project_version',
                                 primary=primary_comparison(rows), rows=rows))


if __name__ == '__main__':
    main()
