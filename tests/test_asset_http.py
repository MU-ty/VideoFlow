"""Wire-level integration with ComfyUI adapter and the asset worker."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asset_runtime import AssetRuntime
from asset_worker import pass_once
from comfyui import ComfyUI


class Handler(BaseHTTPRequestHandler):
    jobs = {}
    posts = 0
    lose_response = False

    def log_message(self, *args):
        pass

    def answer(self, code, body):
        data=json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).posts+=1
        type(self).jobs[body['prompt_id']]={}
        self.answer(500 if self.lose_response else 200, {'prompt_id':body['prompt_id']})

    def do_GET(self):
        if self.path.startswith('/history/'):
            pid=self.path.rsplit('/',1)[1]
            self.answer(200,{pid:self.jobs[pid]} if self.jobs.get(pid) else {})
        else:
            self.answer(200,{'queue_running':[],'queue_pending':[[0,pid] for pid,data in self.jobs.items() if not data]})


class HttpAssetTests(unittest.TestCase):
    def setUp(self):
        Handler.jobs={}; Handler.posts=0; Handler.lose_response=False
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db')
        self.r=AssetRuntime(self.path)
        assets=json.loads((Path(__file__).resolve().parents[1]/'fixtures/assets.json').read_text())['assets'][:3]
        self.r.initialize({'assets':assets})
        self.b=ComfyUI('http://127.0.0.1:'+str(self.server.server_port))

    def tearDown(self):
        self.r.close(); self.tmp.cleanup()
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def finish(self):
        for pid in Handler.jobs:
            Handler.jobs[pid]={'status':{'status_str':'success','completed':True},
                'outputs':{'14':{'videos':[{'filename':pid+'.mp4','type':'output'}]}}}

    def test_restart_commit_and_subtitle_edit(self):
        pass_once(self.r,self.b,max_submit=20)
        self.assertEqual(Handler.posts,2)
        self.r.close(); self.r=AssetRuntime(self.path)
        self.finish(); pass_once(self.r,self.b)
        state=self.r.snapshot()
        self.assertIsNotNone(state['timeline'])
        self.assertTrue(all('/view?' in a['output'] for a in state['assets'] if a['kind']=='video'))
        self.r.edit(dict(id='sub',expected_revision=1,target='S2.subtitle',key='subtitle.text',value='Run!',scope='local'))
        pass_once(self.r,self.b)
        self.assertEqual(Handler.posts,2)
        self.assertGreater(self.r.db.execute('SELECT COUNT(*) FROM vf_provider_inspections').fetchone()[0],0)
        self.assertEqual(self.r.snapshot()['revision'],2)
        self.assertIsNotNone(self.r.snapshot()['timeline'])

    def test_obsolete_http_task_is_still_reconciled(self):
        pass_once(self.r,self.b,max_submit=20)
        old=next(j for j in self.r.active() if j['asset']=='S2.video')
        self.r.edit(dict(id='costume',expected_revision=1,target='S2.video',key='hero.costume',value='black',scope='onward'))
        self.assertEqual(len(self.r.unresolved_remote(self.b.base)),2)
        self.finish();log=pass_once(self.r,self.b,max_submit=0)
        self.assertTrue(any(e.get('result')=='stale' for e in log))
        recorded=self.r.db.execute('SELECT * FROM vf_jobs WHERE id=?',(old['id'],)).fetchone()
        self.assertEqual(recorded['status'],'superseded')
        self.assertEqual(recorded['remote_state'],'succeeded')
        self.assertEqual(self.r.unresolved_remote(self.b.base),[])
        pass_once(self.r,self.b,max_submit=1);self.finish();pass_once(self.r,self.b,max_submit=0)
        self.assertIsNotNone(self.r.snapshot()['timeline'])
        self.assertNotIn(recorded['remote_output'],[a['output'] for a in self.r.snapshot()['assets']])

    def test_http_500_accepted_no_duplicate(self):
        Handler.lose_response=True
        pass_once(self.r,self.b,max_submit=20)
        self.assertTrue(all(j['status']=='unknown' for j in self.r.active()))
        self.finish(); pass_once(self.r,self.b)
        self.assertEqual(Handler.posts,2)
        self.assertIsNotNone(self.r.snapshot()['timeline'])

if __name__=='__main__': unittest.main()
