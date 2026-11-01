"""Asset-granular DAIR execution. One project per SQLite database, stdlib only."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import re
import sqlite3
import uuid

from dair import plan


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def placeholders(template):
    return set(re.findall(r'\{\{([\w.]+)\}\}', template))


def render(spec):
    return re.sub(r'\{\{([\w.]+)\}\}', lambda m: spec['values'][m[1]], spec['template'])


class Conflict(ValueError):
    pass


class AssetRuntime:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS vf_project(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, edges TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vf_assets(id TEXT PRIMARY KEY, position INTEGER NOT NULL, scene TEXT NOT NULL, kind TEXT NOT NULL, spec TEXT NOT NULL, revision INTEGER NOT NULL, output TEXT, committed_hash TEXT);
        CREATE TABLE IF NOT EXISTS vf_jobs(id TEXT PRIMARY KEY, asset TEXT NOT NULL REFERENCES vf_assets(id), revision INTEGER NOT NULL, status TEXT NOT NULL, external_id TEXT, backend TEXT, input_json TEXT, execution_json TEXT, input_hash TEXT, output TEXT, error TEXT, UNIQUE(asset,revision));
        CREATE TABLE IF NOT EXISTS vf_events(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL, plan TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vf_timelines(revision INTEGER PRIMARY KEY, entries TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def _project(self):
        p = self.db.execute('SELECT * FROM vf_project').fetchone()
        if p is None:
            raise ValueError('initialize a project first')
        return p

    def _assets(self):
        return self.db.execute('SELECT * FROM vf_assets ORDER BY position').fetchall()

    def _job(self, aid, revision):
        self.db.execute('INSERT INTO vf_jobs(id,asset,revision,status) VALUES (?,?,?,?)',
                        (uuid.uuid4().hex, aid, revision, 'pending'))

    def initialize(self, manifest):
        assets = manifest['assets']
        if not assets:
            raise ValueError('empty project')
        edges = manifest.get('edges', [])
        nodes = []
        by_id = {}
        for a in assets:
            if not isinstance(a['id'], str) or not a['id'] or not isinstance(a['scene'], str):
                raise ValueError('invalid asset identity')
            if a['kind'] not in ('video', 'subtitle'):
                raise ValueError('supported kinds: video, subtitle')
            spec = a['spec']
            if not isinstance(spec['template'], str) or not isinstance(spec['values'], dict):
                raise ValueError('invalid asset spec')
            reads = set(spec.get('reads', []))
            if reads != placeholders(spec['template']) or reads != set(spec['values']):
                raise ValueError('reads, template placeholders and values must match exactly')
            if not all(isinstance(v, str) for v in spec['values'].values()):
                raise ValueError('state values must be strings')
            if spec.get('uncertain'):
                raise ValueError('execution requires explicit dependencies; uncertain reuse is offline only')
            nodes.append({'id':a['id'], 'cost':spec['cost'], 'reads':sorted(reads)})
            by_id[a['id']] = a
        # Reuse DAIR validation for ordering, costs, kinds and graph endpoints.
        plan({'scenes':nodes, 'edges':edges}, {'target':assets[0]['id'], 'key':'__validate__'})
        for e in edges:
            if e['kind'] == 'state':
                keys = e.get('keys', [])
                if not keys or len(keys) != len(set(keys)):
                    raise ValueError('state edges require unique keys')
                for key in keys:
                    parent = by_id[e['parent']]['spec']['values']
                    child = by_id[e['child']]['spec']['values']
                    if key not in parent or key not in child or parent[key] != child[key]:
                        raise ValueError('state dependency values must be declared and agree')
        with self.transaction():
            if self.db.execute('SELECT 1 FROM vf_project').fetchone():
                raise Conflict('project already initialized')
            self.db.execute('INSERT INTO vf_project VALUES (1,1,?)', (encode(edges),))
            for i, a in enumerate(assets):
                self.db.execute('INSERT INTO vf_assets VALUES (?,?,?,?,?,1,NULL,NULL)',
                                (a['id'], i, a['scene'], a['kind'], encode(a['spec'])))
                self._job(a['id'], 1)

    def edit(self, event):
        """Atomic planning+mutation. Replay event ID is idempotent; revision is a CAS."""
        payload = encode(event)
        if not isinstance(event['id'], str) or not event['id']:
            raise ValueError('event requires a nonempty id')
        if not isinstance(event['value'], str):
            raise ValueError('edit value must be a string')
        with self.transaction():
            old = self.db.execute('SELECT * FROM vf_events WHERE id=?', (event['id'],)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise Conflict('event ID reused with a different payload')
                return json.loads(old['plan'])
            project = self._project()
            if event['expected_revision'] != project['revision']:
                raise Conflict('project changed; reload before editing')
            assets = self._assets()
            specs = {a['id']:json.loads(a['spec']) for a in assets}
            if event['target'] not in specs or event['key'] not in specs[event['target']]['reads']:
                raise ValueError('edit must target a declared input attribute')
            edges = json.loads(project['edges'])
            story = {'scenes':[dict(id=a['id'], cost=specs[a['id']]['cost'], reads=specs[a['id']]['reads']) for a in assets], 'edges':edges}
            p = plan(story, event)  # Execution always uses zero risk budget.
            selected = set(p.regenerate)
            # State values propagate only across a matching state edge or within scope.
            ids = [a['id'] for a in assets]
            scope = event.get('scope', 'local')
            eligible = set(ids if scope == 'global' else ids[ids.index(event['target']):] if scope == 'onward' else [event['target']])
            state_changed = {a for a in eligible if event['key'] in specs[a]['reads']}
            while True:
                more = {e['child'] for e in edges if e['kind'] == 'state' and e['parent'] in state_changed and event['key'] in e['keys']}
                if more <= state_changed:
                    break
                state_changed |= more
            # Reject a local edit that would contradict a declared upstream state.
            for e in edges:
                if e['kind'] == 'state' and e['child'] in state_changed and event['key'] in e['keys']:
                    parent_value = event['value'] if e['parent'] in state_changed else specs[e['parent']]['values'][event['key']]
                    if parent_value != event['value']:
                        raise Conflict('edit contradicts an upstream state dependency')
            revision = project['revision'] + 1
            for a in assets:
                if a['id'] not in selected:
                    continue
                spec = specs[a['id']]
                if a['id'] in state_changed:
                    spec['values'][event['key']] = event['value']
                self.db.execute("UPDATE vf_jobs SET status='superseded' WHERE asset=? AND status IN ('pending','submitting','submitted','unknown')", (a['id'],))
                self.db.execute('UPDATE vf_assets SET spec=?, revision=revision+1, output=NULL, committed_hash=NULL WHERE id=?', (encode(spec), a['id']))
                self._job(a['id'], a['revision']+1)
            self.db.execute('UPDATE vf_project SET revision=? WHERE id=1', (revision,))
            result = json.loads(encode(dict(asdict(p), revision=revision, state_changed=sorted(state_changed))))
            self.db.execute('INSERT INTO vf_events VALUES (?,?,?,?)', (event['id'], payload, revision, encode(result)))
            self._publish()
            return result

    def _inputs(self, aid):
        a = self.db.execute('SELECT * FROM vf_assets WHERE id=?', (aid,)).fetchone()
        deps = []
        for e in json.loads(self._project()['edges']):
            if e['child'] != aid:
                continue
            parent = self.db.execute('SELECT * FROM vf_assets WHERE id=?', (e['parent'],)).fetchone()
            if e['kind'] == 'reference':
                deps.append(dict(kind='reference', asset=parent['id'], revision=parent['revision'],
                                 input_hash=parent['committed_hash'], output=parent['output']))
            elif e['kind'] == 'state':
                values = json.loads(parent['spec'])['values']
                deps.append(dict(kind='state', asset=parent['id'], values={k:values[k] for k in e['keys']}))
        return dict(asset=aid, revision=a['revision'], kind=a['kind'], spec=json.loads(a['spec']), dependencies=deps)

    def _ready(self, aid):
        for e in json.loads(self._project()['edges']):
            if e['child'] == aid:
                p = self.db.execute('SELECT output FROM vf_assets WHERE id=?', (e['parent'],)).fetchone()
                if p['output'] is None:
                    return False
        return True

    def pending(self):
        return [dict(j) for j in self.db.execute("SELECT j.* FROM vf_jobs j JOIN vf_assets a ON a.id=j.asset AND a.revision=j.revision WHERE j.status='pending' ORDER BY a.position") if self._ready(j['asset'])]

    def prepare(self, jid, backend, build):
        """Persist exact inputs/workflow before side effects. Only one caller reserves."""
        with self.transaction():
            j = self.db.execute('SELECT * FROM vf_jobs WHERE id=?', (jid,)).fetchone()
            if not j or j['status'] != 'pending' or not self._ready(j['asset']):
                return None
            inputs = self._inputs(j['asset'])
            if j['revision'] != inputs['revision']:
                return None
            execution = build(inputs, jid)  # Must be pure; validated before reservation.
            pid = str(uuid.uuid4())
            digest = fingerprint(dict(inputs=inputs, backend=backend, execution=execution))
            self.db.execute("UPDATE vf_jobs SET status='submitting',external_id=?,backend=?,input_json=?,execution_json=?,input_hash=? WHERE id=?",
                            (pid, backend, encode(inputs), encode(execution), digest, jid))
            return dict(id=jid, external_id=pid, execution=execution, input_hash=digest)

    def mark(self, jid, status, error=None):
        if status not in ('submitted', 'unknown', 'failed'):
            raise ValueError('invalid status')
        with self.transaction():
            self.db.execute("UPDATE vf_jobs SET status=?,error=? WHERE id=? AND status IN ('submitting','submitted','unknown')", (status, error, jid))

    def active(self):
        return [dict(j) for j in self.db.execute("SELECT * FROM vf_jobs WHERE status IN ('submitting','submitted','unknown')")]

    def complete(self, jid, external_id, output):
        if not isinstance(output, str) or not output:
            raise ValueError('empty output')
        with self.transaction():
            j = self.db.execute('SELECT * FROM vf_jobs WHERE id=?', (jid,)).fetchone()
            if not j or j['external_id'] != external_id:
                raise ValueError('unknown task/external ID')
            if j['status'] == 'superseded':
                return 'stale'
            if j['status'] == 'succeeded':
                if j['output'] != output:
                    raise Conflict('conflicting duplicate result')
                return 'duplicate'
            if j['status'] not in ('submitting', 'submitted', 'unknown'):
                raise Conflict('task was not submitted')
            inputs = self._inputs(j['asset'])
            expected = fingerprint(dict(inputs=inputs, backend=j['backend'], execution=json.loads(j['execution_json'])))
            if expected != j['input_hash']:
                self.db.execute("UPDATE vf_jobs SET status='superseded',error='input fingerprint changed' WHERE id=?", (jid,))
                return 'stale'
            self.db.execute("UPDATE vf_jobs SET status='succeeded',output=?,error=NULL WHERE id=?", (output, jid))
            self.db.execute('UPDATE vf_assets SET output=?,committed_hash=? WHERE id=? AND revision=?',
                            (output, j['input_hash'], j['asset'], j['revision']))
            self._publish()
            return 'applied'

    def _publish(self):
        assets = self._assets()
        if any(a['output'] is None for a in assets):
            return
        # Revalidate every current artifact before atomic manifest publication.
        for a in assets:
            j = self.db.execute("SELECT * FROM vf_jobs WHERE asset=? AND revision=? AND status='succeeded'", (a['id'], a['revision'])).fetchone()
            if not j:
                raise Conflict('artifact has no committed provenance')
            expected = fingerprint(dict(inputs=self._inputs(a['id']), backend=j['backend'], execution=json.loads(j['execution_json'])))
            if expected != a['committed_hash']:
                raise Conflict('incompatible artifact provenance')
        entries = [dict(asset=a['id'], scene=a['scene'], kind=a['kind'], revision=a['revision'], input_hash=a['committed_hash'], output=a['output']) for a in assets]
        self.db.execute('INSERT OR IGNORE INTO vf_timelines VALUES (?,?)', (self._project()['revision'], encode(entries)))

    def snapshot(self):
        with self.transaction():
            project = self._project()
            timeline = self.db.execute('SELECT entries FROM vf_timelines WHERE revision=?', (project['revision'],)).fetchone()
            return dict(revision=project['revision'], assets=[dict(a) for a in self._assets()],
                        jobs=[dict(j) for j in self.db.execute('SELECT * FROM vf_jobs ORDER BY asset,revision')],
                        timeline=None if timeline is None else json.loads(timeline['entries']))
