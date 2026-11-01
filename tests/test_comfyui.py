import json, os, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from comfyui import ComfyUI, h3_workflow, SubmissionRejected, SubmissionUncertain
from videoflow import Runtime
from worker import pass_once

class Handler(BaseHTTPRequestHandler):
    jobs={}; posts=0; mode='ok'
    def log_message(self,*args): pass
    def answer(self,code,data):
        b=json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length']))); type(self).posts+=1
        if self.mode=='reject': return self.answer(400,{'error':'bad graph'})
        type(self).jobs[data['prompt_id']]={}
        if self.mode=='ambiguous': return self.answer(500,{'error':'lost response'})
        self.answer(200,{'prompt_id':data['prompt_id']})
    def do_GET(self):
        if self.path.startswith('/history/'):
            pid=self.path.rsplit('/',1)[1]; return self.answer(200,{pid:self.jobs[pid]} if self.jobs.get(pid) else {})
        self.answer(200,{'queue_running':[], 'queue_pending':[[1,pid] for pid,v in self.jobs.items() if not v]})

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def setUp(self):
        Handler.jobs={}; Handler.posts=0; Handler.mode='ok'; self.tmp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.tmp.name,'db'); self.r=Runtime(self.path); self.r.add_scene('s','a scene'); self.b=ComfyUI('http://127.0.0.1:'+str(self.server.server_port)); self.build=lambda prompt,tid:h3_workflow(prompt,prefix='videoflow/'+tid)
    def tearDown(self): self.r.db.close(); self.tmp.cleanup()
    def test_restart_and_output(self):
        pass_once(self.r,self.b,self.build); self.assertEqual(Handler.posts,1); self.r.db.close(); self.r=Runtime(self.path)
        t=self.r.db.execute('select * from tasks').fetchone(); pid=t['external_id']
        Handler.jobs[pid]={'status':{'status_str':'success','completed':True},'outputs':{'14':{'videos':[{'filename':'clip.mp4','subfolder':'video','type':'output'}]}}}
        pass_once(self.r,self.b,self.build); row=self.r.db.execute('select * from scenes').fetchone(); self.assertIn('/view?',row['asset']); self.assertEqual(Handler.posts,1)
    def test_ambiguous_no_resubmit_and_stale(self):
        Handler.mode='ambiguous'; pass_once(self.r,self.b,self.build); self.assertEqual(self.r.db.execute('select status from tasks').fetchone()[0],'submitting')
        pass_once(self.r,self.b,self.build); self.assertEqual(Handler.posts,1)
        old=self.r.db.execute('select * from tasks').fetchone(); self.r.revise('s','new'); self.assertEqual(self.r.complete(old['id'],old['external_id'],'old'),'stale')
    def test_reject(self):
        Handler.mode='reject'; pass_once(self.r,self.b,self.build); self.assertEqual(self.r.db.execute('select status from tasks').fetchone()[0],'failed'); pass_once(self.r,self.b,self.build); self.assertEqual(Handler.posts,1)
    def test_validation(self):
        with self.assertRaises(ValueError): h3_workflow('x',frames=125)

if __name__=='__main__': unittest.main()
