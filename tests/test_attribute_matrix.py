import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from dair import plan
from planning import execution_plan
from attribute_baseline import attribute_plan
from research.make_multistory import build
from research.run_matrix import execute

class AttributeMatrixTests(unittest.TestCase):
    def test_baseline_does_not_call_dair_selector(self):
        story=dict(scenes=[dict(id=x,cost=1,reads=['x']) for x in 'ABCD'],
                   edges=[dict(parent='A',child='B',kind='order'),
                          dict(parent='A',child='C',kind='state',keys=['x']),
                          dict(parent='C',child='D',kind='reference')])
        with patch('planning.plan',side_effect=AssertionError('DAIR selector called')):
            r=execution_plan(story,dict(target='A',key='x',scope='local'),'attr_dag')
        self.assertEqual(r.regenerate,('A','C','D'))

    def test_equivalence_on_varied_explicit_dags(self):
        rng=random.Random(83)
        for _ in range(150):
            nodes=[dict(id=str(i),cost=rng.randrange(1,9),reads=['x'] if rng.random()<.5 else []) for i in range(8)]
            edges=[]
            for i in range(8):
                for j in range(i+1,8):
                    if rng.random()<.15:
                        kind=rng.choice(['state','reference','order'])
                        edges.append(dict(parent=str(i),child=str(j),kind=kind,keys=[rng.choice(['x','y'])]))
            story=dict(scenes=nodes,edges=edges)
            edit=dict(target=str(rng.randrange(8)),key='x',scope=rng.choice(['local','onward','global']))
            self.assertEqual(attribute_plan(story,edit),plan(story,edit))

    def test_invalid_input_rejected(self):
        story=dict(scenes=[dict(id='A',cost=1,reads=['x'])],edges=[])
        for edit in [dict(target='Z',key='x'),dict(target='A',key='x',scope='bad')]:
            with self.assertRaises(ValueError): attribute_plan(story,edit)
        story['scenes'][0]['cost']=-1
        with self.assertRaises(ValueError):attribute_plan(story,dict(target='A',key='x'))

    def test_generated_matrix_mock_and_tamper_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=build(root/'plan');b=build(root/'same')
            self.assertEqual(a['plan_hash'],b['plan_hash'])
            self.assertEqual(len(a['blocks']),9)
            self.assertEqual(set(a['methods']),{'full','suffix_safe','dag','attr_dag','dair'})
            rows=execute(root/'plan/plan.json',root/'runs',stories=['ceramics'],seeds=[42])
            by_method={r['method']:r for r in rows}
            self.assertEqual(len(rows),5)
            self.assertEqual(by_method['attr_dag']['metrics']['edit_submission_intents'],3)
            self.assertEqual(by_method['dair']['metrics']['planned_regenerate'],['S2.video','S4.video','S6.video'])
            self.assertTrue(all(r['evidence']=='synthetic_injected_backend' for r in rows))
            manifest=root/'plan/ceramics/seed-42/manifest.json';manifest.write_text('{}')
            with self.assertRaisesRegex(ValueError,'manifest/edit differs'):
                execute(root/'plan/plan.json',root/'bad',stories=['ceramics'],seeds=[42])
