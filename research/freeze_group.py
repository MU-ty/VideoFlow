"""Freeze an entire matched four-method post-generation case, refusing overwrite."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint
from research.compare_post import compare, markdown
from research.freeze_case import inventory, verify

METHODS = ('full', 'suffix_safe', 'dag', 'dair')


def freeze_group(source, destination, decode=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination == source or destination.is_relative_to(source):
        raise ValueError('choose a new destination outside the source')
    for method in METHODS:
        root = source / method
        if any(root.glob('*.db-wal')) or any(root.glob('*.db-shm')):
            raise ValueError('stop SQLite writers and checkpoint before freezing')
        db = sqlite3.connect((root / 'project.db').as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            if db.execute('pragma integrity_check').fetchone()[0] != 'ok':
                raise ValueError('database corruption')
        finally:
            db.close()
    before = inventory(source)
    result = compare([source / m for m in METHODS], decode)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.freeze-', dir=destination.parent))
    try:
        for method in METHODS:
            shutil.copytree(source / method, temporary / method)
            if inventory(source / method) != inventory(temporary / method):
                raise ValueError('copy mismatch')
        if inventory(source) != before:
            raise ValueError('source changed during freeze')
        write_json(temporary / 'comparison.json', result)
        (temporary / 'comparison.md').write_text(markdown(result))
        receipt = dict(schema=2, case_id=source.name,
                       classification='designed single real-provider case; one run per method',
                       files=inventory(temporary))
        receipt['inventory_hash'] = fingerprint(receipt['files'])
        write_json(temporary / 'freeze.json', receipt)
        verify(temporary)
        os.rename(temporary, destination)
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('source'); p.add_argument('--output', required=True)
    p.add_argument('--decode', action='store_true'); a = p.parse_args()
    r = freeze_group(a.source, a.output, a.decode)
    print(json.dumps(dict(inventory_hash=r['inventory_hash'], files=len(r['files'])), indent=2))
