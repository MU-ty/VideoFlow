import sqlite3
import uuid

class Runtime:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS scenes(id TEXT PRIMARY KEY, prompt TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, asset TEXT);
        CREATE TABLE IF NOT EXISTS deps(parent TEXT REFERENCES scenes(id), child TEXT REFERENCES scenes(id), PRIMARY KEY(parent,child));
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, scene TEXT REFERENCES scenes(id), revision INTEGER, status TEXT, external_id TEXT, asset TEXT, UNIQUE(scene,revision));
        ''')

    def add_scene(self, scene, prompt):
        with self.db:
            self.db.execute('INSERT INTO scenes(id,prompt) VALUES (?,?)', (scene,prompt))
            self._task(scene, 1)

    def _task(self, scene, revision):
        self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex,scene,revision,'pending',None,None))

    def affected(self, scene):
        if not self.db.execute('SELECT 1 FROM scenes WHERE id=?',(scene,)).fetchone(): raise ValueError('unknown scene')
        return [r[0] for r in self.db.execute('''WITH RECURSIVE a(id) AS (SELECT ? UNION SELECT d.child FROM deps d JOIN a ON d.parent=a.id) SELECT id FROM a ORDER BY id''',(scene,))]

    def depend(self, parent, child):
        with self.db:
            if parent == child or parent in self.affected(child): raise ValueError('cycle')
            self.db.execute('INSERT INTO deps VALUES (?,?)',(parent,child))

    def revise(self, scene, prompt):
        with self.db:
            ids=self.affected(scene)
            for sid in ids:
                self.db.execute("UPDATE tasks SET status='superseded' WHERE scene=? AND status IN ('pending','submitted')",(sid,))
                self.db.execute('UPDATE scenes SET revision=revision+1,asset=NULL WHERE id=?',(sid,))
                rev=self.db.execute('SELECT revision FROM scenes WHERE id=?',(sid,)).fetchone()[0]
                self._task(sid,rev)
            self.db.execute('UPDATE scenes SET prompt=? WHERE id=?',(prompt,scene))
        return ids

    def ready(self):
        return self.db.execute("""SELECT t.* FROM tasks t WHERE status='pending' AND NOT EXISTS (SELECT 1 FROM deps d JOIN scenes p ON p.id=d.parent WHERE d.child=t.scene AND p.asset IS NULL) ORDER BY scene""").fetchall()

    def submit(self, task, external_id):
        with self.db:
            cur=self.db.execute("UPDATE tasks SET status='submitted',external_id=? WHERE id=? AND status='pending'",(external_id,task))
            if cur.rowcount != 1: raise ValueError('task not pending')

    def complete(self, task, external_id, asset):
        with self.db:
            t=self.db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            if not t or t['external_id'] != external_id: raise ValueError('unknown task or external id')
            if t['status']=='superseded': return 'stale'
            if t['status']=='succeeded':
                if t['asset'] != asset: raise ValueError('conflicting callback')
                return 'duplicate'
            if t['status']!='submitted': raise ValueError('task not submitted')
            cur=self.db.execute('UPDATE scenes SET asset=? WHERE id=? AND revision=?',(asset,t['scene'],t['revision']))
            if not cur.rowcount: return 'stale'
            self.db.execute("UPDATE tasks SET status='succeeded',asset=? WHERE id=?",(asset,task))
            return 'applied'

    def recover(self, lookup):
        results=[]
        for t in self.db.execute("SELECT * FROM tasks WHERE status='submitted'").fetchall():
            asset=lookup(t['external_id'])
            if asset is not None: results.append(self.complete(t['id'],t['external_id'],asset))
        return results

    def snapshot(self):
        return {table:[dict(r) for r in self.db.execute('SELECT * FROM '+table)] for table in ('scenes','deps','tasks')}
