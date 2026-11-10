"""Asset-granular DAIR execution. One project per SQLite database, stdlib only."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import re
import sqlite3
import time
import uuid

from dair import plan
from planning import execution_plan


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
        CREATE TABLE IF NOT EXISTS vf_evidence(
            revision INTEGER NOT NULL, child TEXT NOT NULL REFERENCES vf_assets(id),
            source_input_hash TEXT NOT NULL, decision TEXT, evidence_sha256 TEXT, result TEXT,
            PRIMARY KEY(revision, child));
        CREATE TABLE IF NOT EXISTS vf_timelines(revision INTEGER PRIMARY KEY, entries TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vf_provider_environment(id INTEGER PRIMARY KEY,backend TEXT NOT NULL,observed_at REAL NOT NULL,payload TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS vf_provider_inspections(id INTEGER PRIMARY KEY,job TEXT NOT NULL REFERENCES vf_jobs(id),backend TEXT NOT NULL,payload TEXT NOT NULL,first_seen_at REAL NOT NULL,UNIQUE(job,backend,payload));
        ''')
        # Historical records stay NULL: migration must not invent measurements.
        for table, columns in {
            'vf_jobs': {'created_at':'REAL', 'reserved_at':'REAL',
                        'accepted_observed_at':'REAL', 'completed_at':'REAL',
                        'submission_intents':'INTEGER NOT NULL DEFAULT 0',
                        'remote_state':'TEXT', 'remote_observed_at':'REAL',
                        'remote_terminal_at':'REAL', 'remote_output':'TEXT'},
            'vf_events': {'applied_at':'REAL'},
            'vf_timelines': {'published_at':'REAL'},
        }.items():
            existing = {r['name'] for r in self.db.execute('PRAGMA table_info('+table+')')}
            for name, definition in columns.items():
                if name not in existing:
                    self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)

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
        self.db.execute('INSERT INTO vf_jobs(id,asset,revision,status,created_at) VALUES (?,?,?,?,?)',
                        (uuid.uuid4().hex, aid, revision, 'pending', time.time()))

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
            self._check_pending_evidence(project['revision'], event)
            assets = self._assets()
            specs = {a['id']:json.loads(a['spec']) for a in assets}
            if event['target'] not in specs or event['key'] not in specs[event['target']]['reads']:
                raise ValueError('edit must target a declared input attribute')
            edges = json.loads(project['edges'])
            repairs = self._state_repairs(event, assets, specs, edges)
            edges.extend(repairs)
            p = self._plan(assets, specs, edges, event)
            state_changed = self._state_changes(assets, specs, edges, event)
            revision = project['revision'] + 1
            self._pin_evidence(event, assets, specs, revision)
            self._invalidate(assets, specs, p.regenerate, state_changed, event)
            self.db.execute('UPDATE vf_project SET revision=?,edges=? WHERE id=1', (revision, encode(edges)))
            result = json.loads(encode(dict(asdict(p), revision=revision, state_changed=sorted(state_changed))))
            if repairs:
                result['state_repairs'] = repairs
            self.db.execute('INSERT INTO vf_events(id,payload,revision,plan,applied_at) VALUES (?,?,?,?,?)', (event['id'], payload, revision, encode(result), time.time()))
            self._publish()
            return result

    @staticmethod
    def _plan(assets, specs, edges, event):
        story = {'scenes': [dict(id=a['id'], cost=specs[a['id']]['cost'], reads=specs[a['id']]['reads'])
                            for a in assets], 'edges': edges}
        return execution_plan(story, event, event.get('method', 'dair'))

    @staticmethod
    def _state_changes(assets, specs, edges, event):
        ids = [a['id'] for a in assets]
        scope = event.get('scope', 'local')
        eligible = ids if scope == 'global' else ids[ids.index(event['target']):] if scope == 'onward' else [event['target']]
        key = event['key']
        changed = {a for a in eligible if key in specs[a]['reads']}
        while True:
            more = {e['child'] for e in edges if e['kind'] == 'state' and e['parent'] in changed and key in e['keys']}
            if more <= changed:
                break
            changed |= more
        for e in edges:
            if e['kind'] == 'state' and e['child'] in changed and key in e['keys']:
                parent_value = event['value'] if e['parent'] in changed else specs[e['parent']]['values'][key]
                if parent_value != event['value']:
                    raise Conflict('edit contradicts an upstream state dependency')
        return changed

    def _invalidate(self, assets, specs, selected, changed, event):
        selected = set(selected)
        for a in assets:
            aid = a['id']
            if aid not in selected:
                continue
            spec = specs[aid]
            if aid in changed:
                spec['values'][event['key']] = event['value']
            self.db.execute("UPDATE vf_jobs SET status='superseded' WHERE asset=? AND status IN ('pending','submitting','submitted','unknown')", (aid,))
            self.db.execute('UPDATE vf_assets SET spec=?, revision=revision+1, output=NULL, committed_hash=NULL WHERE id=?', (encode(spec), aid))
            self._job(aid, a['revision']+1)

    def _check_pending_evidence(self, revision, event):
        rows = self.db.execute('SELECT child, decision FROM vf_evidence WHERE revision=?',
                               (revision,)).fetchall()
        if not any(row['decision'] is None for row in rows):
            return
        previous = json.loads(self.db.execute('SELECT payload FROM vf_events WHERE revision=?',
                                               (revision,)).fetchone()['payload'])
        candidates = event.get('evidence_candidates')
        if (not isinstance(candidates, list) or any(not isinstance(c, str) for c in candidates)
                or not {row['child'] for row in rows}.issubset(candidates)
                or (previous['target'], previous['key']) != (event['target'], event['key'])):
            raise Conflict('pending evidence must be re-pinned for a superseding edit of the same target and key')

    def _pin_evidence(self, event, assets, specs, revision):
        candidates = event.get('evidence_candidates', [])
        if not isinstance(candidates, list) or any(not isinstance(child, str) for child in candidates):
            raise ValueError('evidence_candidates must be a list of shot IDs')
        if not candidates:
            return
        if (len(set(candidates)) != len(candidates) or event.get('state_repairs')
                or event.get('scope', 'local') != 'local' or event.get('method', 'dair') != 'dair'):
            raise ValueError('deferred repair requires unique candidates and a local DAIR edit')
        by_id = {a['id']: a for a in assets}
        parent = by_id[event['target']]
        source = self.db.execute('SELECT entries FROM vf_timelines ORDER BY revision DESC LIMIT 1').fetchone()
        if not source or parent['kind'] != 'video':
            raise Conflict('deferred repair requires a published video source')
        hashes = {entry['asset']: entry['input_hash'] for entry in json.loads(source['entries'])}
        for child in candidates:
            a = by_id.get(child)
            if (not a or a['kind'] != 'video' or a['position'] <= parent['position']
                    or event['key'] not in specs[child]['reads']):
                raise ValueError('deferred repair candidates require downstream video attribute bindings')
            self.db.execute('INSERT INTO vf_evidence(revision,child,source_input_hash) VALUES (?,?,?)',
                            (revision, child, hashes[child]))

    def resolve_evidence(self, revision, child, source_input_hash, evidence_sha256, decision):
        """Resolve one pinned candidate and expand the current edit atomically."""
        if decision not in ('match', 'distinct', 'fallback'):
            raise ValueError('expected a definite verdict or an explicit conservative fallback')
        if not isinstance(evidence_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', evidence_sha256):
            raise ValueError('invalid evidence digest')
        with self.transaction():
            project = self._project()
            if revision != project['revision']:
                raise Conflict('evidence refers to a superseded edit')
            row = self.db.execute('SELECT * FROM vf_evidence WHERE revision=? AND child=?',
                                  (revision, child)).fetchone()
            if not row or row['source_input_hash'] != source_input_hash:
                raise Conflict('evidence does not match the pinned shot source')
            if row['decision'] is not None:
                if (row['decision'], row['evidence_sha256']) != (decision, evidence_sha256):
                    raise Conflict('candidate already resolved by another verdict')
                return json.loads(row['result'])

            event = json.loads(self.db.execute('SELECT payload FROM vf_events WHERE revision=?',
                                               (revision,)).fetchone()['payload'])
            result = dict(revision=revision, child=child, decision=decision, regenerate=[], state_repair=None)
            if decision != 'distinct':
                result['state_repair'] = dict(parent=event['target'], child=child, kind='state', keys=[event['key']])
                assets = self._assets()
                specs = {a['id']: json.loads(a['spec']) for a in assets}
                if specs[child]['values'][event['key']] != event['value']:
                    # Traverse the new edge from the edit anchor to this child.
                    # Propagate from the child over the declared graph, leaving
                    # already-valid work on the anchor untouched.
                    delta = dict(event, target=child)
                    edges = json.loads(project['edges'])
                    plan = self._plan(assets, specs, edges, delta)
                    changed = self._state_changes(assets, specs, edges, delta)
                    self._invalidate(assets, specs, plan.regenerate, changed, delta)
                    result['regenerate'] = list(plan.regenerate)
            self.db.execute('UPDATE vf_evidence SET decision=?,evidence_sha256=?,result=? WHERE revision=? AND child=?',
                            (decision, evidence_sha256, encode(result), revision, child))
            self._publish()
            return result

    def _state_repairs(self, event, assets, specs, edges):
        """Bind new state edges to the current artifact observed by the caller."""
        requests = event.get('state_repairs', [])
        if not isinstance(requests, list):
            raise ValueError('state_repairs must be a list')
        by_id = {asset['id']: asset for asset in assets}
        parent, key = event['target'], event['key']
        if requests and by_id[parent]['kind'] != 'video':
            raise ValueError('state repairs require a video edit target')
        seen, repairs = set(), []
        for request in requests:
            if (not isinstance(request, dict)
                    or set(request) != {'child', 'source_input_hash', 'evidence_sha256'}
                    or not isinstance(request['child'], str)
                    or any(not isinstance(request[field], str) or not re.fullmatch(r'[a-f0-9]{64}', request[field])
                           for field in ('source_input_hash', 'evidence_sha256'))):
                raise ValueError('invalid state repair evidence record')
            child = request['child']
            if (child in seen or child not in by_id or by_id[child]['kind'] != 'video'
                    or by_id[parent]['position'] >= by_id[child]['position']):
                raise ValueError('state repairs require unique, downstream video consumers')
            seen.add(child)
            if (by_id[child]['output'] is None
                    or by_id[child]['committed_hash'] != request['source_input_hash']):
                raise Conflict('state repair evidence refers to a noncurrent source artifact')
            if (key not in specs[child]['reads']
                    or specs[child]['values'][key] != specs[parent]['values'][key]):
                raise Conflict('state repair requires a compatible, declared input binding')
            if any(e['kind'] == 'state' and e['parent'] == parent and e['child'] == child
                   and key in e['keys'] for e in edges):
                continue
            repairs.append(dict(parent=parent, child=child, kind='state', keys=[key]))
        return repairs

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
            self.db.execute("UPDATE vf_jobs SET status='submitting',external_id=?,backend=?,input_json=?,execution_json=?,input_hash=?,reserved_at=? WHERE id=?",
                            (pid, backend, encode(inputs), encode(execution), digest, time.time(), jid))
            return dict(id=jid, external_id=pid, execution=execution, input_hash=digest)

    def record_submission_intent(self, jid):
        # Intent is durable before HTTP; a crash here does not prove a POST occurred.
        with self.transaction():
            cur = self.db.execute("UPDATE vf_jobs SET submission_intents=submission_intents+1 WHERE id=? AND status='submitting' AND submission_intents=0", (jid,))
            return cur.rowcount == 1

    def _observe_remote(self, job, state, output=None):
        if job['backend'] in (None, 'local://subtitle-v1'):
            return
        if state not in ('running', 'unknown', 'succeeded', 'failed'):
            raise ValueError('invalid remote state')
        if job['remote_state'] in ('succeeded', 'failed'):
            # Delayed acceptance/queue observations cannot undo a terminal result.
            if state in ('running', 'unknown'):
                return
            if state != job['remote_state'] or (state == 'succeeded' and output != job['remote_output']):
                raise Conflict('conflicting terminal provider observation')
            return
        now = time.time()
        self.db.execute('UPDATE vf_jobs SET remote_state=?,remote_observed_at=?,remote_terminal_at=?,remote_output=? WHERE id=?',
                        (state, now, now if state in ('succeeded','failed') else None, output, job['id']))

    def unresolved_remote(self, backend=None):
        sql = """SELECT * FROM vf_jobs WHERE external_id IS NOT NULL
                 AND backend IS NOT NULL AND backend!='local://subtitle-v1'
                 AND status IN ('submitting','submitted','unknown','superseded')
                 AND (remote_state IS NULL OR remote_state NOT IN ('succeeded','failed'))"""
        args = ()
        if backend is not None:
            sql += ' AND backend=?'
            args = (backend,)
        return [dict(j) for j in self.db.execute(sql+' ORDER BY created_at,id', args)]

    def mark(self, jid, status, error=None):
        if status not in ('submitted', 'unknown', 'failed'):
            raise ValueError('invalid status')
        with self.transaction():
            job = self.db.execute('SELECT * FROM vf_jobs WHERE id=?', (jid,)).fetchone()
            if not job:
                raise ValueError('unknown task')
            if job['status'] in ('submitting','submitted','unknown','superseded'):
                self._observe_remote(job, {'submitted':'running','unknown':'unknown','failed':'failed'}[status])
            self.db.execute("UPDATE vf_jobs SET status=?,error=?,accepted_observed_at=CASE WHEN ?='submitted' THEN COALESCE(accepted_observed_at,?) ELSE accepted_observed_at END WHERE id=? AND status IN ('submitting','submitted','unknown')", (status, error, status, time.time(), jid))

    def active(self, include_superseded=False):
        rows = [dict(j) for j in self.db.execute("SELECT * FROM vf_jobs WHERE status IN ('submitting','submitted','unknown')")]
        if include_superseded:
            seen = {j['id'] for j in rows}
            rows.extend(j for j in self.unresolved_remote() if j['id'] not in seen)
        return rows

    def complete(self, jid, external_id, output):
        if not isinstance(output, str) or not output:
            raise ValueError('empty output')
        with self.transaction():
            j = self.db.execute('SELECT * FROM vf_jobs WHERE id=?', (jid,)).fetchone()
            if not j or j['external_id'] != external_id:
                raise ValueError('unknown task/external ID')
            self._observe_remote(j, 'succeeded', output)
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
            self.db.execute("UPDATE vf_jobs SET status='succeeded',output=?,error=NULL,completed_at=? WHERE id=?", (output, time.time(), jid))
            self.db.execute('UPDATE vf_assets SET output=?,committed_hash=? WHERE id=? AND revision=?',
                            (output, j['input_hash'], j['asset'], j['revision']))
            self._publish()
            return 'applied'

    def _publish(self):
        revision = self._project()['revision']
        if self.db.execute('SELECT 1 FROM vf_evidence WHERE revision=? AND decision IS NULL',
                           (revision,)).fetchone():
            return
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
        self.db.execute('INSERT OR IGNORE INTO vf_timelines(revision,entries,published_at) VALUES (?,?,?)', (revision, encode(entries), time.time()))

    def snapshot(self):
        with self.transaction():
            project = self._project()
            timeline = self.db.execute('SELECT entries FROM vf_timelines WHERE revision=?', (project['revision'],)).fetchone()
            return dict(revision=project['revision'], assets=[dict(a) for a in self._assets()],
                        jobs=[dict(j) for j in self.db.execute('SELECT * FROM vf_jobs ORDER BY asset,revision')],
                        timeline=None if timeline is None else json.loads(timeline['entries']))
