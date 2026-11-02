import copy
import json
from pathlib import Path
import tempfile
import unittest

from asset_runtime import AssetRuntime
from asset_worker import pass_once
from planning import execution_plan
from provider_audit import save_inspection,summarize_inspections,capture_environment
from research.compare_baselines import compare as compare_baselines
from research.mock_provider import MockProvider
from research.run_dynamic import run_dynamic
from research.run_experiment import run
from research.freeze_case import freeze,verify

ROOT=Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.manifest=json.loads((ROOT/'fixtures/assets.json').read_text())
        self.edit=json.loads((ROOT/'fixtures/edit_costume.json').read_text())

    def tearDown(self):self.tmp.cleanup()

    def test_typed_dag_and_suffix_are_distinct(self):
        story=dict(scenes=[dict(id=x,cost=1,reads=['hero.costume'] if x=='B' else []) for x in 'ABCD'],
            edges=[dict(parent='B',child='D',kind='order')])
        edit=dict(target='B',key='hero.costume',scope='local')
        self.assertEqual(execution_plan(story,edit,'dair').regenerate,('B',))
        self.assertEqual(execution_plan(story,edit,'dag').regenerate,('B','D'))
        self.assertEqual(execution_plan(story,edit,'suffix_safe').regenerate,('B','C','D'))
        self.assertEqual(execution_plan(story,edit,'full').regenerate,('A','B','C','D'))

    def test_suffix_keeps_global_earlier_and_reference_constraints(self):
        story=dict(scenes=[dict(id=x,cost=1,reads=['x']) for x in 'ABCD'],edges=[dict(parent='A',child='B',kind='reference')])
        p=execution_plan(story,dict(target='C',key='x',scope='global'),'suffix_safe')
        self.assertEqual(p.regenerate,('A','B','C','D'))

    def test_new_methods_execute_dynamic_sequence(self):
        schedule=json.loads((ROOT/'fixtures/dynamic_edits.json').read_text())
        for method in ('full','suffix_safe','dag','dair'):
            backend=MockProvider()
            report=run_dynamic(self.manifest,schedule,self.root/method,method=method,poll_seconds=0,timeout_seconds=10,max_inflight=2,backend=backend,downloader=backend.download)
            self.assertEqual(report['status'],'complete')
            self.assertEqual(report['applied_events'],2)
            self.assertIsNone(report['metrics']['gpu_seconds'])
        compared=compare_baselines([self.root/m for m in ('full','suffix_safe','dag','dair')])
        self.assertEqual(len(compared['rows']),4)
        self.assertIsNone(compared['quality_comparison'])

    def test_freeze_idempotency_tamper_detection_and_source_preservation(self):
        for method in ('full','dair'):
            backend=MockProvider()
            report=run(self.manifest,self.edit,self.root/method,method=method,poll_seconds=0,timeout_seconds=10,max_inflight=2,backend=backend,downloader=backend.download)
            self.assertEqual(report['status'],'complete')
        original=(self.root/'dair/project.db').read_bytes()
        out=self.root/'frozen'
        receipt=freeze(self.root/'full',self.root/'dair',out,'test')
        self.assertEqual(verify(out),receipt)
        self.assertEqual(freeze(self.root/'full',self.root/'dair',out,'test'),receipt)
        self.assertEqual((self.root/'dair/project.db').read_bytes(),original)
        (out/'dair/report.json').write_text('{}')
        with self.assertRaises(ValueError):verify(out)
        with self.assertRaises(ValueError):freeze(self.root/'full',self.root/'dair',out,'test')

    def test_freeze_refuses_sqlite_sidecars(self):
        for method in ('full','dair'):
            root=self.root/method;root.mkdir();(root/'project.db-wal').write_bytes(b'live')
        with self.assertRaises(ValueError):freeze(self.root/'full',self.root/'dair',self.root/'out')
        self.assertFalse((self.root/'out').exists())

    def test_provider_history_is_saved_and_cache_is_not_gpu_time(self):
        runtime=AssetRuntime(str(self.root/'db'))
        try:
            runtime.initialize(self.manifest);backend=MockProvider();pass_once(runtime,backend,max_submit=1)
            job=runtime.active()[0]
            backend.last_inspection=dict(prompt_id=job['external_id'],history_entry={'status':{'messages':[
                ['execution_start',{'timestamp':1000}],['execution_cached',{'nodes':['2','3']}],
                ['execution_success',{'timestamp':4200}]]}},queue=None)
            save_inspection(runtime,backend,job);save_inspection(runtime,backend,job)
            self.assertEqual(runtime.db.execute('SELECT COUNT(*) FROM vf_provider_inspections').fetchone()[0],1)
            audit=summarize_inspections(runtime)
            self.assertEqual(audit['jobs'][0]['cached_nodes'],['2','3'])
            self.assertEqual(audit['jobs'][0]['execution_span_seconds'],3.2)
            self.assertIsNone(audit['gpu_seconds'])
            backend.last_inspection['prompt_id']='wrong'
            with self.assertRaises(ValueError):save_inspection(runtime,backend,job)
        finally:runtime.close()

    def test_environment_failure_is_explicit_and_does_not_fabricate_stats(self):
        runtime=AssetRuntime(str(self.root/'db'))
        try:
            runtime.initialize(self.manifest);backend=MockProvider()
            backend.environment=lambda:{'system':{'comfyui_version':'test'},'devices':[]}
            capture_environment(runtime,backend)
            def fail():raise OSError('unavailable')
            backend.environment=fail;capture_environment(runtime,backend)
            observations=summarize_inspections(runtime)['environments']
            self.assertEqual(observations[0]['payload']['system']['comfyui_version'],'test')
            self.assertIsNone(observations[1]['payload'])
            self.assertEqual(observations[1]['error'],'unavailable')
        finally:runtime.close()

    def test_missing_history_remains_unknown(self):
        runtime=AssetRuntime(str(self.root/'db'))
        try:
            runtime.initialize(self.manifest);backend=MockProvider();pass_once(runtime,backend,max_submit=1)
            job=runtime.active()[0]
            backend.last_inspection=dict(prompt_id=job['external_id'],history_entry=None,queue={})
            save_inspection(runtime,backend,job)
            row=summarize_inspections(runtime)['jobs'][0]
            self.assertFalse(row['history_seen']);self.assertIsNone(row['execution_span_seconds'])
        finally:runtime.close()

if __name__=='__main__':unittest.main()
