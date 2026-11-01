import copy
import json
from pathlib import Path
import unittest
from dair import plan
from research.benchmark import run

CASES = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/edits.json').read_text())

class PlannerTests(unittest.TestCase):
    def test_annotated_cases(self):
        for c in CASES:
            with self.subTest(c=c['id']):
                p = plan(c['story'], c['edit'])
                self.assertEqual(set(p.regenerate), set(c['expected']))
                self.assertFalse(set(p.regenerate) & set(p.reuse))
                self.assertEqual(len(p.regenerate)+len(p.reuse), len(c['story']['scenes']))

    def test_risk_budget_and_cost(self):
        story = {'scenes':[{'id':'A','cost':1},
            {'id':'B','cost':10,'uncertain':{'x':0.4}},
            {'id':'C','cost':2,'uncertain':{'x':0.4}}]}
        edit = {'target':'A','key':'x','scope':'global'}
        p = plan(story, edit, 0.4)
        self.assertEqual(p.regenerate, ('A','C'))
        self.assertEqual(p.validation, ('B',))
        self.assertEqual(plan(story,edit).regenerate, ('A','B','C'))

    def test_reference_closure_in_cost_search(self):
        story = {'scenes':[{'id':'A','cost':1},
            {'id':'B','cost':1,'uncertain':{'x':0.4}},
            {'id':'C','cost':3,'uncertain':{'x':0.4}},
            {'id':'D','cost':20}],
            'edges':[{'parent':'B','child':'D','kind':'reference'}]}
        p = plan(story, {'target':'A','key':'x','scope':'global'}, 0.4)
        self.assertEqual(p.regenerate, ('A','C'))
        self.assertEqual(p.estimated_cost,4)

    def test_reject_invalid(self):
        c = CASES[0]
        with self.assertRaises(ValueError): plan(c['story'], c['edit'], float('nan'))
        s = copy.deepcopy(c['story']); s['edges'].append({'parent':'S6','child':'S2','kind':'reference'})
        with self.assertRaises(ValueError): plan(s,c['edit'])
        s = copy.deepcopy(c['story']); s['scenes'][0]['cost']=-1
        with self.assertRaises(ValueError): plan(s,c['edit'])

    def test_gold_labels_never_used_by_planner(self):
        changed = copy.deepcopy(CASES)
        for c in changed: c['expected'] = []
        original = run(CASES); altered = run(changed)
        self.assertEqual([r['regenerated'] for r in original], [r['regenerated'] for r in altered])

    def test_unannotated_no_quality_guarantee(self):
        c = copy.deepcopy(CASES[0]); c['story']['scenes'][3]['reads']=[]
        self.assertNotIn('S4',plan(c['story'],c['edit']).regenerate)
        # Missing annotations are a documented false-negative failure mode.

if __name__ == '__main__': unittest.main()
