"""Matched ComfyUI fault injection for revision-safe, shot-local recovery."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifacts import write_json
from asset_runtime import AssetRuntime
from asset_worker import pass_once
from comfyui import ComfyUI, SubmissionUncertain
from provider_audit import capture_environment, save_inspection, summarize_inspections
from recovery_comfy import SEEDS, TEMPLATES, apply_local_edit
from recovery_matrix import SubmissionView, reject_old_project_work


POLICIES = ('project_version', 'wait', 'replace_now', 'replace_after_3')
FAULTS = ('transient', 'opaque')
THRESHOLDS = {'replace_now': 1, 'replace_after_3': 3, 'backoff_after_3': 3}
CAPACITY = 3
TRANSIENT_SECONDS = 12
BACKOFF_MAX_SECONDS = 40
FIXED_GRACE_SECONDS = 15
S6_TEMPLATE = 'Static close-up of a {{color}} paper lantern on a dark shelf, natural light.'
ORDERED_TEMPLATES = {
    'S2': 'Static close-up of a {{color}} map cover beside a trail marker.',
    'S4': 'Static close-up of a {{color}} canvas tent in a forest clearing.',
    'S6': 'Static close-up of a {{color}} metal compass on a tree stump.',
}


class FaultTransport:
    """Intercept the worker's ACK/inspection interface above actual HTTP calls."""

    def __init__(self, upstream, fault):
        self.upstream = upstream
        self.base = upstream.base
        self.fault = fault
        self.target = None
        self.masked_polls = 0
        self.accepted = {}
        self.last_inspection = None
        self.started = None

    def for_shot(self, shot, workflow, prompt_id):
        # Only the first S4 submission is targeted. The genuine response is
        # recorded for the independent observer before the worker loses it.
        confirmed = self.upstream.submit(workflow, prompt_id)
        self.accepted[confirmed] = {'shot': shot, 'accepted_at': time.monotonic()}
        if shot == 'S4' and self.target is None:
            self.target = confirmed
            raise SubmissionUncertain('injected ACK loss after upstream acceptance')
        return confirmed

    def inspect(self, prompt_id):
        self.last_inspection = None
        if prompt_id == self.target and (
                self.fault == 'opaque'
                or (self.fault == 'transient' and self.masked_polls < 2)
                or (self.fault == 'transient_wall' and time.monotonic() - self.started < TRANSIENT_SECONDS)):
            self.masked_polls += 1
            return 'unknown', None
        result = self.upstream.inspect(prompt_id)
        self.last_inspection = self.upstream.last_inspection
        return result


class WorkerBackend:
    """Bind worker submissions to the shot encoded in the durable job ledger."""

    def __init__(self, transport, runtime, backoff_seconds=None):
        self.transport = transport
        self.runtime = runtime
        self.base = transport.base
        self.backoff_seconds = backoff_seconds
        self.next_probe = {}
        self.delays = {}
        self.probed = set()
        self.unknown_probed = set()

    @property
    def last_inspection(self):
        return self.transport.last_inspection

    def inspect(self, prompt_id):
        state, value = self.transport.inspect(prompt_id)
        self.probed.add(prompt_id)
        if state == 'unknown':
            self.unknown_probed.add(prompt_id)
        if self.backoff_seconds is not None:
            if state == 'unknown':
                delay = self.delays.get(prompt_id, self.backoff_seconds)
                self.next_probe[prompt_id] = time.monotonic() + delay
                self.delays[prompt_id] = min(delay * 2, BACKOFF_MAX_SECONDS)
            else:
                self.next_probe.pop(prompt_id, None)
                self.delays.pop(prompt_id, None)
        return state, value

    def submit(self, workflow, prompt_id):
        job = self.runtime.db.execute(
            'SELECT asset FROM vf_jobs WHERE external_id=?', (prompt_id,)).fetchone()
        return self.transport.for_shot(job['asset'], workflow, prompt_id)


class PollingView:
    """Filter only inspections. The worker still sees all pending submissions."""

    def __init__(self, runtime, backend):
        self.runtime = runtime
        self.backend = backend

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def active(self, include_superseded=False):
        now = time.monotonic()
        return [job for job in self.runtime.active(include_superseded=include_superseded)
                if self.backend.next_probe.get(job['external_id'], 0) <= now]


def replace_blocking_shot(runtime, threshold, streaks, replaced, elapsed, probed):
    unknown = [job for job in runtime.active()
               if job['status'] == 'unknown' and job['accepted_observed_at'] is None
               and job['asset'] not in replaced]
    streaks = {job['id']: streaks.get(job['id'], 0) + (job['external_id'] in probed)
               for job in unknown}
    for job in unknown:
        if streaks[job['id']] < threshold:
            continue
        shot, rev = job['asset'], job['revision']
        with runtime.transaction():
            runtime.db.execute("UPDATE vf_jobs SET status='superseded' WHERE id=?", (job['id'],))
            changed = runtime.db.execute(
                'UPDATE vf_assets SET revision=revision+1 WHERE id=? AND revision=? AND output IS NULL',
                (shot, rev))
            if changed.rowcount != 1:
                raise RuntimeError('replacement no longer targets the current blocked shot')
            runtime._job(shot, rev + 1)
        replaced[shot] = {'original_id': job['external_id'], 'queued_after_seconds': elapsed}
    return streaks


def inspect_upstream(runtime, auditor, accepted, observed):
    for job in runtime.snapshot()['jobs']:
        prompt_id = job['external_id']
        if prompt_id not in accepted or observed.get(prompt_id) in ('succeeded', 'failed'):
            continue
        state, _ = auditor.inspect(prompt_id)
        save_inspection(runtime, auditor, job)
        observed[prompt_id] = state


def stale_publications(runtime):
    snapshot = runtime.snapshot()
    revisions = {asset['id']: asset['revision'] for asset in snapshot['assets']}
    jobs = {job['output']: job for job in snapshot['jobs'] if job['output']}
    stale = []
    for publication in runtime.db.execute('SELECT revision,entries FROM vf_timelines'):
        for item in json.loads(publication['entries']):
            shot = item['asset']
            job = jobs.get(item['output'])
            expected_color = 'blue' if shot == 'S2' else 'white'
            if (publication['revision'] != 2 or not job or job['asset'] != shot
                    or job['status'] != 'succeeded' or job['revision'] != revisions[shot]
                    or item['revision'] != revisions[shot]
                    or item['input_hash'] != job['input_hash']
                    or json.loads(job['input_json'])['spec']['values']['color'] != expected_color):
                stale.append({'revision': publication['revision'], 'shot': shot})
    return stale


def run_trial(root, upstream, policy, fault, poll, horizon, seed_offset,
              recovery_cap=CAPACITY, story='independent'):
    root.mkdir()
    queue = upstream._request('/queue')
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise RuntimeError('use a dedicated idle ComfyUI queue')
    auditor = ComfyUI(upstream.base, upstream.timeout)
    transport = FaultTransport(upstream, fault)
    runtime = AssetRuntime(str(root / 'project.db'))
    try:
        templates = ORDERED_TEMPLATES if story == 'ordered' else {**TEMPLATES, 'S6': S6_TEMPLATE}
        edges = [{'parent': 'S2', 'child': 'S6', 'kind': 'order'}] if story == 'ordered' else []
        expected_initial = {'S2', 'S4'} if story == 'ordered' else set(templates)
        runtime.initialize({'assets': [
            dict(id=shot, scene=shot, kind='video', spec=dict(
                template=template, reads=['color'], values={'color': 'white'},
                cost=1, config={'seed': SEEDS.get(shot, 2718) + seed_offset}))
            for shot, template in templates.items()
        ], 'edges': edges})
        capture_environment(runtime, upstream)
        initial_events = pass_once(runtime, WorkerBackend(transport, runtime), max_submit=CAPACITY)
        original_ids = {info['shot']: pid for pid, info in transport.accepted.items()}
        if set(original_ids) != expected_initial or len(transport.accepted) != len(expected_initial) or transport.target is None:
            raise RuntimeError(f'initial submissions failed: {initial_events}')
        observed = {}
        inspect_upstream(runtime, auditor, transport.accepted, observed)
        if set(observed.values()) != {'running'} or len(observed) != len(expected_initial):
            raise RuntimeError(f'initial jobs did not reach the in-flight barrier: {observed}')
        write_json(root / 'boundary.json', dict(initial_events=initial_events,
                   original_ids=original_ids, hidden_ack=transport.target, upstream=observed))
        started = time.monotonic()
        transport.started = started
        apply_local_edit(runtime, 1, 'S2', 'blue')
        if policy == 'project_version':
            reject_old_project_work(runtime)
        runtime.close()
        runtime = AssetRuntime(str(root / 'project.db'))
        backend = WorkerBackend(transport, runtime, poll if policy == 'backoff_after_3' else None)
        polling = PollingView(runtime, backend)
        replacements, streaks = {}, {}
        ready = None
        peak = len(original_ids)
        maximum = max(peak, recovery_cap)
        first_admission = None
        threshold = THRESHOLDS.get(policy)
        with (root / 'worker.jsonl').open('w', encoding='utf-8') as log:
            while time.monotonic() - started < horizon:
                # Match the simulator: capacity comes from the ledger at the
                # start of the poll, including superseded unconfirmed jobs.
                tracked_before = len(runtime.unresolved_remote())
                capacity = max(0, recovery_cap - tracked_before)
                backend.probed.clear()
                backend.unknown_probed.clear()
                probe_started = time.monotonic() - started
                events = pass_once(polling, backend, max_submit=0)
                elapsed = time.monotonic() - started
                if threshold is not None:
                    streaks = replace_blocking_shot(runtime, threshold, streaks, replacements, elapsed,
                                                    backend.probed)
                elif policy == 'fixed_grace15' and probe_started >= FIXED_GRACE_SECONDS:
                    # Use a fresh observation from this pass, not an older unknown.
                    replace_blocking_shot(runtime, 1, {}, replacements, elapsed, backend.unknown_probed)
                accepted_before = len(transport.accepted)
                events.extend(pass_once(SubmissionView(runtime), backend, max_submit=capacity))
                admitted = len(transport.accepted) - accepted_before
                if admitted > capacity:
                    raise RuntimeError('submission exceeded the recovery capacity')
                if admitted and first_admission is None:
                    first_admission = elapsed
                snapshot = runtime.snapshot()
                unresolved = runtime.unresolved_remote()
                peak = max(peak, len(unresolved))
                if snapshot['timeline'] and ready is None:
                    ready = runtime.db.execute(
                        'SELECT published_at FROM vf_timelines WHERE revision=2').fetchone()[0]
                inspect_upstream(runtime, auditor, transport.accepted, observed)
                log.write(json.dumps(dict(elapsed=elapsed, events=events, tracked=len(unresolved),
                                          tracked_before=tracked_before, admitted=admitted,
                                          probe_started=probe_started,
                                          target_probed=transport.target in backend.probed,
                                          masked_polls=transport.masked_polls, upstream=observed)) + '\n')
                log.flush()
                if any(job['status'] == 'failed' for job in snapshot['jobs']) or 'failed' in observed.values():
                    raise RuntimeError('ComfyUI reported a failed job; inspect worker log and ledger')
                unknown_submits = [job['external_id'] for job in snapshot['jobs']
                                   if job['submission_intents'] and job['external_id'] not in transport.accepted]
                if unknown_submits:
                    raise RuntimeError(f'uncontrolled submission uncertainty: {unknown_submits}')
                if ready is not None and all(state == 'succeeded' for state in observed.values()):
                    break
                remaining = horizon - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(poll, remaining))

        stale = stale_publications(runtime)
        accepted_by_shot = dict(Counter(info['shot'] for info in transport.accepted.values()))
        edited = runtime.db.execute('SELECT applied_at FROM vf_events WHERE revision=2').fetchone()[0]
        latency = ready - edited if ready is not None else None
        inspections = summarize_inspections(runtime)
        result = dict(
            policy=policy, fault=fault, horizon_seconds=horizon, poll_seconds=poll,
            story=story, recovery_cap=recovery_cap, pre_edit_inflight=len(original_ids),
            first_admission_seconds=first_admission,
            correct_publication_seconds=latency if not stale and latency is not None and latency <= horizon else None,
            stale_publications=stale, replacements=replacements,
            accepted_by_shot=accepted_by_shot, accepted_count=len(transport.accepted),
            upstream_acceptances={
                pid: {'shot': info['shot'], 'seconds_from_edit_barrier': info['accepted_at'] - started}
                for pid, info in transport.accepted.items()
            },
            original_s6_reused='S6' in original_ids and any(item['asset'] == 'S6' and item['revision'] == 1
                                   for item in runtime.snapshot()['timeline'] or []),
            upstream_states=observed,
            tracked_unresolved=[{'shot': j['asset'], 'id': j['external_id']}
                                for j in runtime.unresolved_remote()],
            peak_tracked_unresolved=peak, masked_polls=transport.masked_polls,
            provider_history=inspections,
        )
        write_json(root / 'report.json', result)
        if stale or peak > maximum or not transport.masked_polls:
            raise RuntimeError(f'experiment invariant failed: {root}/report.json')
        if any(state != 'succeeded' for state in observed.values()):
            raise RuntimeError(f'provider did not drain by the horizon: {root}/report.json')
        return result
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--horizon-seconds', type=float, default=1200)
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--start-repetition', type=int, default=1)
    parser.add_argument('--suite', choices=('primary', 'capacity', 'backoff', 'ordered', 'ordered_recovery', 'grace'),
                        default='primary')
    parser.add_argument('--pilot', action='store_true', help='run only the opaque/replace-after-3 cell')
    args = parser.parse_args()
    if (not math.isfinite(args.horizon_seconds)
            or not 0 < args.poll_seconds < args.horizon_seconds
            or min(args.repetitions, args.start_repetition) < 1):
        parser.error('require 0 < poll < horizon and positive repetition numbers')
    if args.pilot and args.suite != 'primary':
        parser.error('--pilot requires --suite primary')
    if args.suite in ('backoff', 'grace') and (args.poll_seconds != 5 or args.horizon_seconds <= 30):
        parser.error('backoff and grace suites require 5-second polls and a horizon above 30 seconds')
    if args.pilot:
        cells = [('opaque', 'replace_after_3', CAPACITY, 'independent')]
    elif args.suite == 'capacity':
        cells = [(fault, policy, cap, 'independent') for cap in (2, 4) for fault in FAULTS
                 for policy in ('project_version', 'replace_after_3')]
    elif args.suite == 'backoff':
        cells = [(fault, policy, CAPACITY, 'independent') for fault in ('transient_wall', 'opaque')
                 for policy in ('replace_after_3', 'backoff_after_3')]
    elif args.suite == 'grace':
        cells = [(fault, policy, CAPACITY, 'independent') for fault in ('transient_wall', 'opaque')
                 for policy in ('fixed_grace15', 'backoff_after_3')]
    elif args.suite == 'ordered_recovery':
        cells = [(fault, policy, CAPACITY, 'ordered') for fault in FAULTS
                 for policy in ('wait', 'replace_now', 'replace_after_3')]
    elif args.suite == 'ordered':
        cells = [(fault, policy, CAPACITY, 'ordered') for fault in FAULTS
                 for policy in ('project_version', 'replace_after_3')]
    else:
        cells = [(fault, policy, CAPACITY, 'independent') for fault in FAULTS for policy in POLICIES]
    upstream = ComfyUI(args.server)
    upstream.environment()
    args.output.mkdir(parents=True)
    protocol = dict(evidence='real_comfyui_client_transport_fault_injection',
                    pilot=args.pilot, suite=args.suite, poll_seconds=args.poll_seconds,
                    horizon_seconds=args.horizon_seconds, capacity=CAPACITY,
                    repetitions=args.repetitions, start_repetition=args.start_repetition,
                    cells=[[fault, policy] for fault, policy, _, _ in cells] if args.suite == 'primary' else cells,
                    admission_cap_applies='after edit; initial accepted jobs depend on the story',
                    order='forward in odd repetitions, reverse in even repetitions',
                    seed_offset='10000 * zero-based repetition index')
    if args.suite in ('backoff', 'grace'):
        protocol.update(transient_mask_seconds=TRANSIENT_SECONDS, backoff_max_seconds=BACKOFF_MAX_SECONDS)
    if args.suite == 'grace':
        protocol['fixed_grace_seconds'] = FIXED_GRACE_SECONDS
    write_json(args.output / 'protocol.json', protocol)
    rows = []
    for rep in range(args.start_repetition, args.start_repetition + args.repetitions):
        for fault, policy, cap, story in (cells if rep % 2 else reversed(cells)):
            suffix = f'-cap{cap}' if args.suite == 'capacity' else ''
            label = f'{rep:02d}-{fault}-{policy}{suffix}'
            print(f'START {label}', flush=True)
            try:
                result = run_trial(args.output / label, upstream, policy, fault,
                                   args.poll_seconds, args.horizon_seconds, 10000 * (rep - 1),
                                   recovery_cap=cap, story=story)
            except Exception as exc:
                write_json(args.output / 'summary.json', dict(complete=False, failed=label,
                           error=str(exc), rows=rows))
                raise
            rows.append(dict(repetition=rep, **result))
            print(f'DONE {label}: {result["correct_publication_seconds"]} s, '
                  f'{result["accepted_count"]} accepted', flush=True)
            write_json(args.output / 'summary.json', dict(complete=len(rows) == args.repetitions * len(cells),
                       rows=rows))


if __name__ == '__main__':
    main()
