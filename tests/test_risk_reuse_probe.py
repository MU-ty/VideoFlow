import copy
import unittest
from research.risk_reuse_probe import ORDERS,GRID,ladder,select,calibrate,loss


def features(story='one'):
    return dict(story_id=story,nodes=[dict(id='A',score=.8,cost=1),dict(id='B',score=.1,cost=100),dict(id='C',score=.7,cost=1)],hard=[],reference_edges=[['A','B']])


class RiskReuseTests(unittest.TestCase):
    def test_nested_monotone_graph_closed_for_every_order(self):
        f=features();y={'A':True,'B':False,'C':True}
        for order in ORDERS:
            previous=set();previous_loss=1
            for t in GRID:
                r=select(f,t,order);s=set(r['regenerate']);self.assertTrue(previous<=s)
                self.assertFalse('A' in s and 'B' not in s)
                self.assertLessEqual(loss(f,y,r),previous_loss);previous=s;previous_loss=loss(f,y,r)
            self.assertEqual(previous,{'A','B','C'})

    def test_closure_cost_changes_first_choice(self):
        self.assertEqual(ladder(features(),'score')[1][1],frozenset(['A','B']))
        self.assertEqual(ladder(features(),'closure_gain')[1][1],frozenset(['C']))

    def test_small_data_abstains_from_reuse(self):
        ex=[(features(str(i)),{'A':False,'B':False,'C':False}) for i in range(8)]
        c=calibrate(ex,alpha=.1);self.assertEqual(c['parameter'],1)
        self.assertIn('fallback',c['selection'])

    def test_nine_zero_losses_allow_zero_at_alpha_point_one(self):
        ex=[(features(str(i)),{'A':False,'B':False,'C':False}) for i in range(9)]
        c=calibrate(ex,alpha=.1);self.assertEqual(c['parameter'],0)
        self.assertAlmostEqual(c['curve'][0]['corrected_risk'],.1)

    def test_known_complete_dependencies_cannot_be_saved(self):
        f=features();f['hard']=['A','C']
        for order in ORDERS:
            for t in GRID:self.assertEqual(select(f,t,order)['regenerate'],['A','B','C'])

    def test_duplicate_story_and_label_leak_rejected(self):
        f=features();y={'A':True,'B':False,'C':False}
        with self.assertRaises(ValueError):calibrate([(f,y),(copy.deepcopy(f),y)])
        f['labels']=y
        with self.assertRaises(ValueError):select(f,.5)

    def test_bad_inputs(self):
        f=features();f['nodes'][0]['score']=float('nan')
        with self.assertRaises(ValueError):select(f,.1)
