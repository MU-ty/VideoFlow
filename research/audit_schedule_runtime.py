"""Executable SQLite runtime probes; fake outputs only, no provider calls."""
import json
from pathlib import Path
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_runtime import AssetRuntime

def asset(a,reads):
    return dict(id=a,scene=a,kind='video',spec=dict(template=' '.join('{{'+k+'}}' for k in reads),reads=reads,
        values={k:'initial' for k in reads},cost=10,config=dict(seed=42)))

def reserve(rt,a):
    job=next(j for j in rt.pending() if j['asset']==a)
    reserved=rt.prepare(job['id'],'mock://audit',lambda inputs,jid:dict(prompt=inputs['spec']['values']))
    rt.record_submission_intent(job['id']);rt.mark(job['id'],'submitted')
    return job['id'],reserved['external_id']

def edit(rt,ident,value,scope='local'):
    return rt.edit(dict(id=ident,expected_revision=rt.snapshot()['revision'],target='A',key='x',value=value,scope=scope,method='attr_dag'))

def audit():
    with tempfile.TemporaryDirectory() as tmp:
        rt=AssetRuntime(str(Path(tmp)/'undo.db'));rt.initialize(dict(assets=[asset('A',['x'])],edges=[]))
        jid,pid=reserve(rt,'A');edit(rt,'black','black');edit(rt,'undo','initial')
        outcome=rt.complete(jid,pid,'mock://old-initial.mp4')
        undo=dict(old_result_outcome=outcome,current_output=rt.snapshot()['assets'][0]['output'],pending=len(rt.pending()))
        assert outcome=='stale' and undo['current_output'] is None and undo['pending']==1
        rt.close()
        rt=AssetRuntime(str(Path(tmp)/'ready.db'))
        rt.initialize(dict(assets=[asset('A',['x']),asset('B',[]),asset('C',['x'])],edges=[dict(parent='A',child='B',kind='order'),dict(parent='B',child='C',kind='order')]))
        for a in ('A','B','C'):
            jid,pid=reserve(rt,a);assert rt.complete(jid,pid,'mock://'+a)=='applied'
        edit(rt,'global','new','global');pending=[j['asset'] for j in rt.pending()]
        assert pending==['A','C'];rt.close()
    return dict(evidence='Executed actual AssetRuntime against temporary SQLite databases; fake outputs, no GPU.',
       undo=undo,direct_parent_readiness=dict(pending_after_A_C_invalidated=pending,legacy_prefix_model_would_block_C=True),
       remaining_gaps=['5-second client polling not simulated','Actual cold loading/offloading and host overhead not calibrated',
         'One FIFO server assumption; actual adapter/provider behavior must be instrumented','No faults, ambiguous submissions or cancellation',
         'Independent profile bypasses actual declared edges; semantic cache profile is hypothetical'])

if __name__=='__main__':print(json.dumps(audit(),indent=2))
