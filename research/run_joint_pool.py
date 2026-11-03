"""Generate exactly twelve candidate jobs from archived source stories; resumable."""
import argparse
import copy
import json
import random
import shutil
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import archive_current,checksum,write_json
from asset_runtime import AssetRuntime,Conflict,fingerprint,render
from asset_worker import pass_once
from comfyui import ComfyUI
from provider_audit import capture_environment,summarize_inspections
from research.run_experiment import runner_lock

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'fixtures/joint_pool_v1/protocol.json'
SOURCES=('research/run_joint_pool.py','research/joint_pool_review.py','fixtures/joint_pool_v1/protocol.json',
         'asset_runtime.py','asset_worker.py','comfyui.py','provider_audit.py','artifacts.py','planning.py','dair.py','attribute_baseline.py')

def prepare(source_root,output):
    source_root=Path(source_root).resolve();out=Path(output).resolve()
    if out.exists():raise ValueError('prepare requires a fresh output directory')
    protocol=json.loads(PROTOCOL.read_text());jobs=[];anchors=[];source_records=[]
    for story in protocol['stories']:
        matches=list(source_root.rglob(story['source_block']+'/dair/configuration.json'))
        if len(matches)!=1:raise ValueError(f"Expected one {story['source_block']}/dair/configuration.json under {source_root}; found {len(matches)}")
        conf=matches[0];art=conf.parent/'artifacts';revision=art/'revision-1.json'
        config=json.loads(conf.read_text());initial=json.loads(revision.read_text())
        original={a['asset']:a for a in initial['assets']}
        source_records.append(dict(story=story['id'],configuration_hash=fingerprint(config),revision_hash=fingerprint(initial)))
        for shot in protocol['locked_shots']:
            a=original[shot+'.video'];p=(art/a['file']).resolve()
            if not p.is_relative_to(art.resolve()) or checksum(p)!=a['sha256']:raise ValueError('source archive checksum/path failed')
            anchors.append(dict(story=story['id'],shot=shot,source=str(p),sha256=a['sha256']))
        for shot in protocol['shots']:
            asset=next(a for a in config['manifest']['assets'] if a['id']==shot+'.video')
            spec=copy.deepcopy(asset['spec']);values=spec['values']
            if 'prop.color' not in values:raise ValueError('source must declare prop.color')
            values['prop.color']='bright saturated royal-blue'
            prompt=render(spec)+'\nShared product specification: '+story['contract']
            base=spec.get('config',{}).get('seed',42)
            for k,offset in enumerate(protocol['seed_offsets']):
                jobs.append(dict(id=f"{story['id']}.{shot}.candidate{k+1}",scene=shot,story=story['id'],
                    seed=(base+offset)%(2**64),prompt=prompt))
    random.Random(protocol['generation_order_seed']).shuffle(jobs)
    aliases=list(range(1,len(jobs)+1));random.Random(protocol['blind_order_seed']).shuffle(aliases)
    for j,alias in zip(jobs,aliases):j['blind_id']=f'V{alias:03d}'
    manifest=dict(assets=[dict(id=j['id'],scene=j['scene'],kind='video',spec=dict(template=j['prompt'],values={},reads=[],cost=10,
            config=dict(seed=j['seed'],**protocol['generation']))) for j in jobs],edges=[])
    plan=dict(protocol=protocol,jobs=jobs,manifest=manifest,sources=source_records,
        anchors=[{k:v for k,v in a.items() if k!='source'} for a in anchors],source_hashes={f:fingerprint((ROOT/f).read_text()) for f in SOURCES})
    plan['plan_hash']=fingerprint(plan)
    out.mkdir(parents=True)
    for a in anchors:
        dst=out/'anchors'/a['story']/(a['shot']+'.mp4');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(a['source'],dst)
        if checksum(dst)!=a['sha256']:raise ValueError('anchor copy mismatch')
    write_json(out/'plan.json',plan)
    write_json(out/'status.json',dict(status='prepared',planned_video_jobs=len(jobs),source_anchors=len(anchors),gpu_seconds=None))
    return plan


def load_plan(output):
    out=Path(output);plan=json.loads((out/'plan.json').read_text());digest=plan.pop('plan_hash')
    if fingerprint(plan)!=digest:raise Conflict('plan hash mismatch')
    plan['plan_hash']=digest
    for f,h in plan['source_hashes'].items():
        if fingerprint((ROOT/f).read_text())!=h:raise Conflict('source changed since plan freeze: '+f)
    for a in plan['anchors']:
        if checksum(out/'anchors'/a['story']/(a['shot']+'.mp4'))!=a['sha256']:raise Conflict('anchor checksum mismatch')
    return plan


def run(output,server='http://127.0.0.1:8188',timeout=21600,*,backend=None,downloader=None,max_passes=None):
    out=Path(output).resolve();plan=load_plan(out)
    backend=ComfyUI(server) if backend is None else backend
    if downloader is None and shutil.which('ffprobe') is None:raise ValueError('ffprobe is required to validate playable review files before starting this experiment')
    config=dict(plan_hash=plan['plan_hash'],backend=backend.base,evidence='remote_comfyui_unvalidated_quality' if downloader is None else 'synthetic_not_playable_video')
    if timeout<=0:raise ValueError('positive timeout required')
    with runner_lock(out):
        p=out/'execution.json'
        if p.exists() and json.loads(p.read_text())!=config:raise Conflict('backend/evidence changed; resume refused')
        if not p.exists():
            if (out/'project.db').exists():raise Conflict('unowned project database')
            write_json(p,config)
        rt=AssetRuntime(str(out/'project.db'));start=time.monotonic();passes=0;status='paused';error=None
        try:
            if not rt.db.execute('SELECT 1 FROM vf_project').fetchone():rt.initialize(plan['manifest'])
            capture_environment(rt,backend)
            while True:
                snap=rt.snapshot();done=sum(j['status']=='succeeded' for j in snap['jobs'])
                if snap['timeline'] is not None and not rt.unresolved_remote(backend.base):
                    kwargs={} if downloader is None else dict(downloader=downloader)
                    archive_current(rt,out/'artifacts',backend.base,**kwargs);status='complete';break
                if any(j['status']=='failed' for j in snap['jobs']):error='failed job; inspect database, no automatic resubmission';break
                if time.monotonic()-start>=timeout or (max_passes is not None and passes>=max_passes):break
                slots=plan['protocol']['max_inflight']-len(rt.unresolved_remote(backend.base))
                events=pass_once(rt,backend,max_submit=max(0,slots));passes+=1
                write_json(out/'status.json',dict(status='running',completed=done,planned=12,passes=passes,evidence=config['evidence']))
                with (out/'events.jsonl').open('a') as f:
                    for e in events:f.write(json.dumps(dict(observed_at=time.time(),**e))+'\n')
                print(f'Completed {done}/12; unresolved {len(rt.unresolved_remote(backend.base))}',flush=True)
                if downloader is None:time.sleep(plan['protocol']['poll_seconds'])
            snap=rt.snapshot()
            report=dict(status=status,error=error,evidence=config['evidence'],plan_hash=plan['plan_hash'],
                submission_intents=sum(j['submission_intents'] for j in snap['jobs']),completed=sum(j['status']=='succeeded' for j in snap['jobs']),
                remote_unresolved=len(rt.unresolved_remote(backend.base)),provider_observations=summarize_inspections(rt),
                gpu_seconds=None,edit_success_rate=None,joint_quality=None)
            write_json(out/'status.json',report);return report
        finally:rt.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--source-root',required=True);a.add_argument('--output',required=True)
    a=sub.add_parser('generate');a.add_argument('--output',required=True);a.add_argument('--source-root');a.add_argument('--server',default='http://127.0.0.1:8188');a.add_argument('--timeout-seconds',type=float,default=21600)
    args=p.parse_args()
    if args.command=='prepare':result=prepare(args.source_root,args.output);print(json.dumps(dict(plan_hash=result['plan_hash'],jobs=len(result['jobs'])),indent=2));return
    if not (Path(args.output)/'plan.json').exists():
        if not args.source_root:p.error('--source-root required for first generation')
        prepare(args.source_root,args.output)
    report=run(args.output,args.server,args.timeout_seconds);print(json.dumps(report,indent=2))
    if report['status']!='complete':raise SystemExit(2)
    from research.joint_pool_review import build_review
    build_review(args.output)

if __name__=='__main__':main()
