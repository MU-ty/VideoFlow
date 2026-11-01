import os, tempfile, unittest
from videoflow import Runtime

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.temp.name,'db'); self.r=Runtime(self.path)
        for s in ('A','B','C'): self.r.add_scene(s,s)
        self.r.depend('A','B')
    def tearDown(self): self.r.db.close(); self.temp.cleanup()
    def task(self,s): return self.r.db.execute('SELECT * FROM tasks WHERE scene=? ORDER BY revision DESC',(s,)).fetchone()
    def test_revisions(self):
        self.assertEqual({t['scene'] for t in self.r.ready()},{'A','C'})
        for s in ('A','C'):
            t=self.task(s); self.r.submit(t['id'],s); self.r.complete(t['id'],s,s+'.mp4')
        t=self.task('B'); self.r.submit(t['id'],'old')
        self.assertEqual(self.r.revise('A','new'),['A','B'])
        self.assertEqual(self.r.complete(t['id'],'old','old.mp4'),'stale')
        self.assertEqual(self.r.db.execute("SELECT asset FROM scenes WHERE id='C'").fetchone()[0],'C.mp4')
        self.assertNotIn('B',{t['scene'] for t in self.r.ready()})
    def test_recovery(self):
        t=self.task('A'); self.r.submit(t['id'],'job'); self.r.db.close(); self.r=Runtime(self.path)
        self.assertEqual(self.r.recover(lambda _:None),[])
        self.assertEqual(self.r.recover(lambda _:'a.mp4'),['applied'])
        self.assertEqual(self.r.complete(t['id'],'job','a.mp4'),'duplicate')
        with self.assertRaises(ValueError): self.r.complete(t['id'],'job','other')
    def test_cycle(self):
        with self.assertRaises(ValueError): self.r.depend('B','A')
        with self.assertRaises(ValueError): self.r.revise('missing','x')
if __name__=='__main__': unittest.main()
