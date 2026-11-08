"""Live H3 fan-out, consecutive edits, and controlled loss of task observations."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import shutil
import time

from recovery_reference_comfy import (
    CHAINS, PROMPTS, ReferenceFrames, check_backend, initialize_reference_project,
    reference_evidence,
)
from artifacts import archive_current, write_json
from asset_runtime import AssetRuntime, fingerprint
from asset_worker import pass_once
from comfyui import ComfyUI, SubmissionUncertain
from provider_audit import capture_environment, summarize_inspections
from recovery_comfy import reconcile
from recovery_matrix import SubmissionView
from recovery_observability_comfy import (
    PollingView, WorkerBackend, inspect_upstream, replace_blocking_shot,
)


POLICIES = {'project_version_grace': 'full', 'worklist_grace': 'attr_dag', 'videoflow': 'dair'}
FAULTS = ('obsolete_only', 'joint_opaque')
EDGES = CHAINS['fanout']


class ReferenceFault:
    """Hide the first S2 ACK in each armed phase after real upstream acceptance."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.base = upstream.base
        self.armed = None
        self.targets = {}
        self.reveal_at = {}
        self.accepted = {}
        self.masked_polls = Counter()
        self.last_inspection = None

    def for_shot(self, shot, workflow, prompt_id):
        confirmed = self.upstream.submit(workflow, prompt_id)
        self.accepted[confirmed] = {'shot': shot, 'accepted_wall': time.time()}
        if shot == 'S2' and self.armed is not None:
            self.targets[self.armed] = confirmed
            self.reveal_at[confirmed] = math.inf
            self.armed = None
            raise SubmissionUncertain('injected ACK loss after upstream acceptance')
        return confirmed

    def inspect(self, prompt_id):
        self.last_inspection = None
        if time.monotonic() < self.reveal_at.get(prompt_id, 0):
            self.masked_polls[prompt_id] += 1
            return 'unknown', None
        result = self.upstream.inspect(prompt_id)
        self.last_inspection = self.upstream.last_inspection
        return result


def archive_assets(runtime, root, upstream):
    manifest = archive_current(runtime, root / 'media', upstream.base, probe=True)
    return {item['asset']: item for item in manifest['assets']}


def count_repeated_work(jobs, accepted):
    signatures = Counter()
    for job in jobs:
        if job['external_id'] in accepted:
            graph = json.loads(job['execution_json'])
            del graph['14']['inputs']['filename_prefix']
            signatures[(job['asset'], fingerprint(graph))] += 1
    return sum(count - 1 for count in signatures.values())


def run_trial(root, upstream, policy, fault, seed, *, cap=2, poll=5, grace=15,
              obsolete_mask=12, setup_timeout=3600, horizon=1800, drain=600):
    root.mkdir()
    check_backend(upstream)
    auditor = ComfyUI(upstream.base, upstream.timeout)
    transport = ReferenceFault(upstream)
    refs = ReferenceFrames(root / 'references', upstream)
    runtime = AssetRuntime(str(root / 'project.db'))
    observed, replacements, first_unknown, streaks = {}, {}, {}, {}
    try:
        initialize_reference_project(runtime, 'fanout', seed)
        capture_environment(runtime, upstream)
        backend = WorkerBackend(transport, runtime, backoff_seconds=poll)
        polling = PollingView(runtime, backend)
        use_worklist = policy != 'videoflow'

        def poll_jobs():
            backend.probed.clear()
            backend.unknown_probed.clear()
            if use_worklist:
                return reconcile(polling, backend)
            return pass_once(polling, backend, max_submit=0)

        def submit_ready(limit, deadline=None):
            tracked = len(runtime.unresolved_remote())
            free = min(limit, max(0, cap - tracked))
            for job in runtime.pending()[:free]:
                for dependency in runtime._inputs(job['asset'])['dependencies']:
                    refs.stage(dependency['output'])
            if deadline is not None and time.monotonic() >= deadline:
                return [], tracked
            return pass_once(SubmissionView(runtime), backend, build=refs.build, max_submit=free), tracked

        def log_pass(phase, events, tracked, snapshot):
            jobs = snapshot['jobs']
            row = dict(phase=phase, at=time.time(), events=events, tracked_before_submit=tracked,
                       tracked_after_submit=len(runtime.unresolved_remote()),
                       targets=transport.targets, masked_polls=transport.masked_polls)
            with (root / 'worker.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(row) + '\n')
            if any(j['status'] == 'failed' for j in jobs) or 'failed' in observed.values():
                raise RuntimeError('provider job failed; inspect the archived ledger')
            if any(j['submission_intents'] and j['external_id'] not in transport.accepted for j in jobs):
                raise RuntimeError('uncontrolled submission uncertainty')

        def edit(revision, color, method):
            return runtime.edit(dict(id=f'edit-{revision}', expected_revision=revision,
                                     target='S1', key='color', value=color, method=method))

        def drive_setup(phase, limit, stop):
            deadline = time.monotonic() + setup_timeout
            while time.monotonic() < deadline:
                events = pass_once(polling, backend, max_submit=0)
                submits, tracked = submit_ready(limit)
                snapshot = runtime.snapshot()
                log_pass(phase, events + submits, tracked, snapshot)
                if stop(snapshot):
                    return
                time.sleep(min(poll, max(0, deadline - time.monotonic())))
            raise TimeoutError(f'{phase} did not reach its barrier before deadline')

        drive_setup('initial', cap, lambda state: state['timeline'] is not None)
        initial = archive_assets(runtime, root, upstream)
        # All arms share the setup edit. Apply the policy only after an identical barrier.
        setup_plan = edit(1, 'blue', 'attr_dag')
        transport.armed = 'obsolete'
        drive_setup('first_edit', 1, lambda _: 'obsolete' in transport.targets)
        old_id = transport.targets['obsolete']
        old_job = next(j for j in runtime.active() if j['external_id'] == old_id)
        if (old_job['revision'], old_job['status']) != (2, 'unknown'):
            raise RuntimeError('obsolete S2 was not unknown before the second edit')
        old_dependency, = json.loads(old_job['input_json'])['dependencies']
        if (old_dependency['asset'], old_dependency['revision']) != ('S1', 2):
            raise RuntimeError('obsolete S2 did not consume revision-2 S1')
        old_probes = transport.masked_polls[old_id]
        events = pass_once(polling, backend, max_submit=0)
        if transport.masked_polls[old_id] != old_probes + 1:
            raise RuntimeError('the worker did not observe a masked S2 probe before edit 2')
        log_pass('barrier', events, len(runtime.unresolved_remote()), runtime.snapshot())
        inspect_upstream(runtime, auditor, transport.accepted, observed)
        if observed.get(old_id) != 'running' or len(runtime.unresolved_remote()) != 1:
            raise RuntimeError('second-edit barrier requires exactly one in-flight S2')
        write_json(root / 'boundary.json', dict(target=old_id, ledger_status=old_job['status'],
                   provider_status=observed[old_id], available_slots=cap - 1, setup_plan=setup_plan,
                   masked_probes=transport.masked_polls[old_id], old_s2_dependency=old_dependency))
        plan = edit(2, 'red', POLICIES[policy])
        started = time.monotonic()
        deadline = started + horizon
        transport.reveal_at[old_id] = started + obsolete_mask
        transport.armed = 'current' if fault == 'joint_opaque' else None
        while time.monotonic() < deadline:
            events = poll_jobs()
            elapsed = time.monotonic() - started
            if elapsed >= horizon:
                log_pass('second_edit', events, len(runtime.unresolved_remote()), runtime.snapshot())
                break
            if use_worklist:
                for pid in backend.probed - backend.unknown_probed:
                    first_unknown.pop(pid, None)
                due = {pid for pid in backend.unknown_probed
                       if elapsed - first_unknown.setdefault(pid, elapsed) >= grace}
                if due:
                    replace_blocking_shot(runtime, 1, {}, replacements, elapsed, due)
            else:
                streaks = replace_blocking_shot(runtime, 3, streaks, replacements, elapsed,
                                                backend.unknown_probed)
            submits, tracked = submit_ready(cap, deadline)
            snapshot = runtime.snapshot()
            inspect_upstream(runtime, auditor, transport.accepted, observed)
            log_pass('second_edit', events + submits, tracked, snapshot)
            old = next(j for j in snapshot['jobs'] if j['id'] == old_job['id'])
            if snapshot['timeline'] is not None and old['remote_state'] == 'succeeded':
                break
            time.sleep(min(poll, max(0, deadline - time.monotonic())))

        # Drain only the independent observer, without worker polls or submissions.
        drain_deadline = time.monotonic() + drain
        while True:
            inspect_upstream(runtime, auditor, transport.accepted, observed)
            if all(state in ('succeeded', 'failed') for state in observed.values()):
                break
            if time.monotonic() >= drain_deadline:
                break
            time.sleep(min(poll, max(0, drain_deadline - time.monotonic())))

        snapshot = runtime.snapshot()
        jobs = {(j['asset'], j['revision']): j for j in snapshot['jobs']}
        published = runtime.db.execute('SELECT revision,published_at FROM vf_timelines ORDER BY revision').fetchall()
        revisions = [row['revision'] for row in published]
        if revisions not in ([1], [1, 3]):
            raise RuntimeError(f'unexpected manifest revisions: {revisions}')
        expected = list(PROMPTS) if policy == 'project_version_grace' else list(PROMPTS)[:-1]
        if plan['regenerate'] != expected:
            raise RuntimeError(f'unexpected final edit plan: {plan}')
        after = archive_assets(runtime, root, upstream) if revisions == [1, 3] else None
        references = reference_evidence((initial, after), jobs, refs, EDGES) if after else []
        reused, latency, s4_after_s2 = None, None, None
        if after:
            reused = all(initial['S5'][k] == after['S5'][k] for k in ('output', 'input_hash', 'sha256'))
            if reused != (policy != 'project_version_grace'):
                raise RuntimeError('S5 reuse differs from the declared policy')
            s2 = jobs['S2', after['S2']['revision']]
            s4 = jobs['S4', after['S4']['revision']]
            s4_after_s2 = s4['reserved_at'] >= s2['completed_at']
            if not s4_after_s2:
                raise RuntimeError('S4 was reserved before its selected S2 committed')
            applied = runtime.db.execute('SELECT applied_at FROM vf_events WHERE revision=3').fetchone()[0]
            latency = published[-1]['published_at'] - applied
        obsolete = jobs[('S2', 2)]
        stale_rejected = (obsolete['status'] == 'superseded'
                          and obsolete['remote_state'] == 'succeeded' and obsolete['output'] is None)
        current_id = transport.targets.get('current')
        if after and (not stale_rejected or not transport.masked_polls[old_id]):
            raise RuntimeError('completed run lacks a proved obsolete-S2 rejection')
        if after and fault == 'joint_opaque':
            if (not current_id or not transport.masked_polls[current_id]
                    or replacements.get('S2', {}).get('original_id') != current_id
                    or after['S2']['revision'] != 4):
                raise RuntimeError('completed joint run lacks a proved current-S2 replacement')
        report = dict(policy=policy, fault=fault, seed=seed, plan=plan,
                      targets=transport.targets, masked_polls=dict(transport.masked_polls),
                      provider_acceptances=transport.accepted, provider_outcomes=observed,
                      provider_history=summarize_inspections(runtime),
                      submission_intents=sum(j['submission_intents'] for j in jobs.values()),
                      repeated_acceptances_same_workflow=count_repeated_work(jobs.values(), transport.accepted),
                      local_replacements=replacements, manifest_revisions=revisions, references=references,
                      obsolete_result_rejected=stale_rejected, independent_reused=reused,
                      s4_reserved_after_selected_s2_commit=s4_after_s2,
                      last_edit_to_publication_seconds=latency,
                      correct_by_deadline=latency is not None and latency <= horizon,
                      unresolved_at_end=[j['external_id'] for j in runtime.unresolved_remote()])
        write_json(root / 'report.json', report)
        return report
    finally:
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--faults', nargs='+', choices=FAULTS, default=list(FAULTS))
    parser.add_argument('--seeds', nargs='+', type=int, default=[3107])
    parser.add_argument('--cap', type=int, default=2)
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--grace-seconds', type=float, default=15)
    parser.add_argument('--obsolete-mask-seconds', type=float, default=12)
    parser.add_argument('--setup-timeout-seconds', type=float, default=3600)
    parser.add_argument('--horizon-seconds', type=float, default=1800)
    parser.add_argument('--drain-seconds', type=float, default=600)
    args = parser.parse_args()
    timings = (args.poll_seconds, args.grace_seconds, args.obsolete_mask_seconds,
               args.setup_timeout_seconds, args.horizon_seconds, args.drain_seconds)
    if (args.cap < 2 or any(not math.isfinite(t) or t <= 0 for t in timings)
            or args.horizon_seconds <= max(args.obsolete_mask_seconds, args.grace_seconds)
            or any(not 0 <= seed <= 2**64 - len(PROMPTS) for seed in args.seeds)
            or any(len(items) != len(set(items)) for items in (args.policies, args.faults, args.seeds))):
        parser.error('invalid cap, timings, seeds, or duplicate conditions')
    if not all(shutil.which(exe) for exe in ('ffmpeg', 'ffprobe')):
        parser.error('ffmpeg and ffprobe must be available')
    upstream = ComfyUI(args.server)
    args.output.mkdir(parents=True)
    write_json(args.output / 'protocol.json', dict(vars(args), output=str(args.output),
               evidence='live_comfyui_reference_fault', graph='fanout', edges=EDGES,
               setup_planner='attr_dag', policy_planners=POLICIES, provider='MiniMax H3 FL2VA',
               retry_unknown_probe_threshold=3,
               policy_order='rotated by seed index and fault name index'))
    rows = []
    for seed_index, seed in enumerate(args.seeds):
        for fault in args.faults:
            shift = (seed_index + FAULTS.index(fault)) % len(args.policies)
            for policy in args.policies[shift:] + args.policies[:shift]:
                label = f'{seed}-{fault}-{policy}'
                print(f'START {label}', flush=True)
                try:
                    row = run_trial(args.output / label, upstream, policy, fault, seed,
                                    cap=args.cap, poll=args.poll_seconds, grace=args.grace_seconds,
                                    obsolete_mask=args.obsolete_mask_seconds,
                                    setup_timeout=args.setup_timeout_seconds, horizon=args.horizon_seconds,
                                    drain=args.drain_seconds)
                except Exception as exc:
                    write_json(args.output / 'summary.json', dict(complete=False, failed=label,
                               error=str(exc), rows=rows))
                    raise
                rows.append(row)
                write_json(args.output / 'summary.json', dict(
                    complete=len(rows) == len(args.seeds) * len(args.faults) * len(args.policies), rows=rows))
                print(f"DONE {label}: on-time={row['correct_by_deadline']}, "
                      f"acceptances={len(row['provider_acceptances'])}", flush=True)


if __name__ == '__main__':
    main()
