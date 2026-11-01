"""Run one durable scheduling/reconciliation pass. Never retries ambiguous submissions."""
import argparse
import json
import sys
import uuid
from urllib.error import URLError
from videoflow import Runtime
from comfyui import ComfyUI, SubmissionRejected, SubmissionUncertain, h3_workflow, render_template

def pass_once(runtime, backend, build):
    # Reconcile reserved/submitted work first. An unknown remote job remains reserved.
    rows=runtime.db.execute("SELECT * FROM tasks WHERE status IN ('submitting','submitted')").fetchall()
    for t in rows:
        if t['backend_url'] != backend.base:
            print('skipping task for a different or unknown backend:',t['id'],file=sys.stderr)
            continue
        try:
            state,value=backend.inspect(t['external_id'])
            if state=='succeeded': runtime.complete(t['id'],t['external_id'],value)
            elif state=='failed':
                with runtime.db: runtime.db.execute("UPDATE tasks SET status='failed' WHERE id=? AND status IN ('submitting','submitted')",(t['id'],))
                print('failed',t['id'],value,file=sys.stderr)
            elif state=='running':
                with runtime.db: runtime.db.execute("UPDATE tasks SET status='submitted' WHERE id=? AND status='submitting'",(t['id'],))
            else: print('unknown; no automatic resubmission:',t['external_id'],file=sys.stderr)
        except (OSError, ValueError, URLError) as e: print('inspection error:',e,file=sys.stderr)
    for task in runtime.ready():
        scene=runtime.db.execute('SELECT * FROM scenes WHERE id=?',(task['scene'],)).fetchone()
        graph=build(scene['prompt'],task['id']) # validate before reservation
        pid=str(uuid.uuid4())
        with runtime.db:
            cur=runtime.db.execute("UPDATE tasks SET status='submitting',external_id=?,backend_url=? WHERE id=? AND status='pending'",(pid,backend.base,task['id']))
        if not cur.rowcount: continue
        try:
            backend.submit(graph,pid)
            with runtime.db: runtime.db.execute("UPDATE tasks SET status='submitted' WHERE id=? AND status='submitting'",(task['id'],))
        except SubmissionRejected as e:
            with runtime.db: runtime.db.execute("UPDATE tasks SET status='failed' WHERE id=? AND status='submitting'",(task['id'],))
            print('rejected:',e,file=sys.stderr)
        except SubmissionUncertain as e: print(e,file=sys.stderr)

def main():
    p=argparse.ArgumentParser(description='VideoFlow ComfyUI worker')
    p.add_argument('--db',default='videoflow.db'); p.add_argument('--server',default='http://127.0.0.1:8188')
    sub=p.add_subparsers(dest='command',required=True)
    add=sub.add_parser('add'); add.add_argument('scene'); add.add_argument('prompt')
    dep=sub.add_parser('depend'); dep.add_argument('parent'); dep.add_argument('child')
    edit=sub.add_parser('revise'); edit.add_argument('scene'); edit.add_argument('prompt')
    sub.add_parser('status')
    run=sub.add_parser('run-once'); run.add_argument('--workflow'); run.add_argument('--prompt-node',default='5'); run.add_argument('--prompt-field',default='prompt'); run.add_argument('--seed',type=int,default=42); run.add_argument('--width',type=int,default=864); run.add_argument('--height',type=int,default=480); run.add_argument('--frames',type=int,default=124); run.add_argument('--steps',type=int,default=20)
    a=p.parse_args(); r=Runtime(a.db)
    if a.command=='add': r.add_scene(a.scene,a.prompt)
    elif a.command=='depend': r.depend(a.parent,a.child)
    elif a.command=='revise': print('Affected:',r.revise(a.scene,a.prompt))
    elif a.command=='status': print(json.dumps(r.snapshot(),ensure_ascii=False,indent=2))
    else:
        if a.workflow:
            with open(a.workflow,encoding='utf-8') as f: template=json.load(f)
            build=lambda prompt,task: render_template(template,prompt,a.prompt_node,a.prompt_field)
        else: build=lambda prompt,task: h3_workflow(prompt,a.seed,a.width,a.height,a.frames,a.steps,'videoflow/'+task)
        pass_once(r,ComfyUI(a.server),build)
    r.db.close()
if __name__=='__main__': main()
