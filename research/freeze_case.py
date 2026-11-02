"""Verify and freeze a completed Full/DAIR pair without modifying source files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import checksum,write_json
from asset_runtime import fingerprint
from research.compare_dynamic import compare as compare_dynamic
from research.compare_reports import compare as compare_post


def inventory(root):
    rows=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('symlinks cannot be frozen')
        if path.is_file() and path.name!='freeze.json':
            rows.append(dict(path=path.relative_to(root).as_posix(),bytes=path.stat().st_size,sha256=checksum(path)))
    return rows


def verify(directory):
    root=Path(directory).resolve()
    receipt=json.loads((root/'freeze.json').read_text(encoding='utf-8'))
    if inventory(root)!=receipt['files'] or fingerprint(receipt['files'])!=receipt['inventory_hash']:
        raise ValueError('frozen case files changed')
    return receipt


def freeze(full_directory,dair_directory,destination,case_id='h3-v3-pilot'):
    if not case_id or not isinstance(case_id,str):raise ValueError('case ID required')
    sources={'full':Path(full_directory).resolve(),'dair':Path(dair_directory).resolve()}
    dest=Path(destination).resolve()
    for source in sources.values():
        if dest==source or dest.is_relative_to(source):raise ValueError('destination cannot be inside source')
    if dest.exists():
        old=verify(dest)
        if old['case_id']!=case_id:raise ValueError('case ID conflict')
        for method,source in sources.items():
            original=inventory(source)
            frozen=[dict(row,path=row['path'][len(method)+1:]) for row in old['files'] if row['path'].startswith(method+'/')]
            if original!=frozen:raise ValueError('source differs from frozen case; choose another case ID/directory')
        return old
    # WAL/SHM snapshots are not safely represented by copying only project.db.
    for source in sources.values():
        if not source.is_dir():raise ValueError('missing run directory')
        if list(source.glob('*.db-wal')) or list(source.glob('*.db-shm')):
            raise ValueError('stop writers and checkpoint/close SQLite before freezing')
        db=sqlite3.connect((source/'project.db').as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('database corruption')
        finally:db.close()
    protocol=json.loads((sources['full']/'configuration.json').read_text())['protocol']
    if protocol=='inflight_edit_sequence_v1':
        comparison=compare_dynamic(sources['full'],sources['dair'])
    elif protocol=='initial_complete_then_edit_v1':
        comparison=compare_post(sources['full'],sources['dair'])
    else:
        raise ValueError('unsupported experiment protocol')
    before={m:inventory(p) for m,p in sources.items()}
    dest.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.freeze-',dir=dest.parent))
    try:
        for method,source in sources.items():
            shutil.copytree(source,temporary/method)
            if inventory(source)!=before[method] or inventory(temporary/method)!=before[method]:
                raise ValueError('source changed while freezing')
        receipt=dict(schema=1,case_id=case_id,classification='single designed real-provider pilot; not an independent benchmark',
            comparison=comparison,files=inventory(temporary),
            limitations=['No recorded GPU duration or monetary cost.','Source run did not record raw provider histories; cache behavior cannot be reconstructed.','Visual review is preliminary, not blinded quality evaluation.'])
        receipt['inventory_hash']=fingerprint(receipt['files'])
        write_json(temporary/'freeze.json',receipt)
        verify(temporary)
        os.rename(temporary,dest)
        return receipt
    finally:
        if temporary.exists():shutil.rmtree(temporary)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('full');p.add_argument('dair');p.add_argument('--output',required=True);p.add_argument('--case-id',default='h3-v3-pilot');a=p.parse_args()
    result=freeze(a.full,a.dair,a.output,a.case_id)
    print(json.dumps(dict(case_id=result['case_id'],inventory_hash=result['inventory_hash'],files=len(result['files'])),indent=2))
