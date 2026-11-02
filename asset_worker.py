"""DAIR asset worker. Reserved/unknown video jobs are queried, never resubmitted."""
import argparse
import json
from pathlib import Path

from asset_runtime import AssetRuntime, render
from comfyui import ComfyUI, SubmissionRejected, SubmissionUncertain, h3_workflow

LOCAL = 'local://subtitle-v1'


def build_h3(inputs, job_id):
    if any(d['kind'] == 'reference' for d in inputs['dependencies']):
        raise ValueError('H3 adapter has no reference loader; refusing to ignore reference inputs')
    return h3_workflow(render(inputs['spec']), prefix='videoflow/'+job_id,
                       **inputs['spec'].get('config', {}))


def build_subtitle(inputs, job_id):
    if any(d['kind'] == 'reference' for d in inputs['dependencies']):
        raise ValueError('subtitle adapter cannot consume reference assets')
    return {'text':render(inputs['spec'])}


def pass_once(runtime, backend, build=build_h3, max_submit=1):
    if max_submit < 0:
        raise ValueError('max_submit must be nonnegative')
    log = []
    for j in runtime.active(include_superseded=True):
        if j['backend'] == LOCAL:
            result = runtime.complete(j['id'], j['external_id'], json.loads(j['execution_json'])['text'])
            log.append(dict(job=j['id'], action='subtitle_recovered', result=result))
        elif j['backend'] != backend.base:
            log.append(dict(job=j['id'], action='backend_mismatch'))
        else:
            try:
                state, value = backend.inspect(j['external_id'])
                if state == 'succeeded':
                    result = runtime.complete(j['id'], j['external_id'], value)
                    log.append(dict(job=j['id'], action='complete', result=result))
                elif state == 'failed':
                    runtime.mark(j['id'], 'failed', str(value))
                    log.append(dict(job=j['id'], action='failed', detail=str(value)))
                elif state == 'running':
                    runtime.mark(j['id'], 'submitted')
                else:
                    runtime.mark(j['id'], 'unknown', 'not found in backend history/queue')
                    log.append(dict(job=j['id'], action='unknown_no_resubmit'))
            except (OSError, ValueError) as exc:
                log.append(dict(job=j['id'], action='inspection_error', detail=str(exc)))
    submitted = 0
    for j in runtime.pending():
        a = runtime.db.execute('SELECT kind FROM vf_assets WHERE id=?', (j['asset'],)).fetchone()
        is_text = a['kind'] == 'subtitle'
        if not is_text and submitted >= max_submit:
            continue
        try:
            reserved = runtime.prepare(j['id'], LOCAL if is_text else backend.base,
                                       build_subtitle if is_text else build)
        except (ValueError, TypeError, KeyError) as exc:
            log.append(dict(job=j['id'], action='validation_error', detail=str(exc)))
            continue
        if reserved is None:
            continue
        if is_text:
            result = runtime.complete(j['id'], reserved['external_id'], reserved['execution']['text'])
            log.append(dict(job=j['id'], action='subtitle_updated', result=result))
            continue
        if not runtime.record_submission_intent(j['id']):
            continue
        submitted += 1
        try:
            backend.submit(reserved['execution'], reserved['external_id'])
            runtime.mark(j['id'], 'submitted')
            log.append(dict(job=j['id'], action='submitted'))
        except SubmissionRejected as exc:
            runtime.mark(j['id'], 'failed', str(exc))
            log.append(dict(job=j['id'], action='rejected', detail=str(exc)))
        except (SubmissionUncertain, OSError) as exc:
            runtime.mark(j['id'], 'unknown', str(exc))
            log.append(dict(job=j['id'], action='unknown_no_resubmit', detail=str(exc)))
    return log


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='assets.db')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('manifest')
    edit = sub.add_parser('edit'); edit.add_argument('event')
    sub.add_parser('status')
    run = sub.add_parser('run-once')
    run.add_argument('--server', default='http://127.0.0.1:8188')
    run.add_argument('--max-submit', type=int, default=1, help='maximum new video submissions per pass; not an inflight limit')
    args = p.parse_args()
    runtime = AssetRuntime(args.db)
    try:
        if args.command == 'init':
            runtime.initialize(json.loads(Path(args.manifest).read_text(encoding='utf-8')))
            result = runtime.snapshot()
        elif args.command == 'edit':
            result = runtime.edit(json.loads(Path(args.event).read_text(encoding='utf-8')))
        elif args.command == 'status':
            result = runtime.snapshot()
        else:
            result = pass_once(runtime, ComfyUI(args.server), max_submit=args.max_submit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
