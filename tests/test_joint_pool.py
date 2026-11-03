import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from artifacts import checksum,write_json
from asset_runtime import Conflict
from research.mock_provider import MockProvider
from research.run_joint_pool import prepare,run,load_plan
from research.joint_pool_review import build_review,analyze


def sources(root):
    for story in ('greenhouse','harbor'):
        base=root/(story+'-seed-42')/'dair';art=base/'artifacts';art.mkdir(parents=True)
        assets=[];archive=[]
        for i in range(1,7):
            aid=f'S{i}.video';spec=dict(template='A {{prop.color}} product.' if i%2==0 else 'Scene only.',reads=['prop.color'] if i%2==0 else [],values={'prop.color':'white'} if i%2==0 else {},cost=10,config={'seed':42+i})
            assets.append(dict(id=aid,scene=f'S{i}',kind='video',spec=spec))
            f=art/(f'S{i}.mp4');f.write_bytes(b'fixture only '+story.encode()+bytes([i]));archive.append(dict(asset=aid,file=f.name,sha256=checksum(f)))
        write_json(base/'configuration.json',dict(manifest=dict(assets=assets,edges=[])))
        write_json(art/'revision-1.json',dict(assets=archive))


class PoolTests(unittest.TestCase):
    def test_twelve_jobs_six_anchors_and_frozen_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sources(root/'source');out=root/'pool';p=prepare(root/'source',out)
            self.assertEqual(len(p['jobs']),12);self.assertEqual(len(p['anchors']),6)
            self.assertEqual(len({j['blind_id'] for j in p['jobs']}),12)
            for story in ('greenhouse','harbor'):
                for shot in ('S2','S4','S6'):
                    js=[j for j in p['jobs'] if j['story']==story and j['scene']==shot]
                    self.assertEqual(len({j['seed'] for j in js}),2);self.assertEqual(js[0]['prompt'],js[1]['prompt'])
            self.assertEqual(load_plan(out),p)
            p['jobs'][0]['seed']+=1;write_json(out/'plan.json',p)
            with self.assertRaises(Conflict):load_plan(out)

    def test_pause_resume_no_duplicate_submission_and_review_blinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sources(root/'source');out=root/'pool';prepare(root/'source',out)
            b=MockProvider();b.ready=False
            r=run(out,backend=b,downloader=b.download,max_passes=1)
            self.assertEqual(len(b.posts),2);self.assertEqual(r['remote_unresolved'],2)
            b.ready=True;r=run(out,backend=b,downloader=b.download)
            self.assertEqual(r['status'],'complete');self.assertEqual(len(b.posts),12);self.assertEqual(r['submission_intents'],12)
            run(out,backend=b,downloader=b.download);self.assertEqual(len(b.posts),12)
            with self.assertRaises(ValueError):build_review(out)
            r['evidence']='remote_comfyui_unvalidated_quality';write_json(out/'status.json',r)
            with patch('research.joint_pool_review.probe_media'):
                study=build_review(out)
            self.assertEqual(len(study['individual']),12);self.assertEqual(len(study['combinations']),16)
            for name in ('individual.html','joint.html','study.json'):
                public=(out/'review'/name).read_text()
                self.assertNotIn('candidate1',public);self.assertNotIn('candidate2',public)
                self.assertNotIn('"seed"',public)
            for story in ('greenhouse','harbor'):
                combos=[c for c in study['combinations'] if c['story']==story]
                self.assertEqual(len({tuple(x['id'] for x in c['clips']) for c in combos}),8)
            unary=dict(stage='individual',study_hash=study['study_hash'],rater_id='a',rows=[dict(id=c['id'],edit_success='yes',single_quality=4) for c in study['individual']])
            joint=dict(stage='joint',study_hash=study['study_hash'],rater_id='b',rows=[dict(id=c['id'],acceptable='yes',joint_quality=4) for c in study['combinations']])
            result=analyze(study,unary,joint);self.assertFalse(result['signal_present']);self.assertTrue(all(r['gap']==0 for r in result['rows']))
            for row in result['rows']:
                alternate=next(c for c in study['combinations'] if c['story']==row['story'] and c['id']!=row['independent'])
                next(r for r in joint['rows'] if r['id']==alternate['id'])['joint_quality']=5
            self.assertTrue(analyze(study,unary,joint)['signal_present'])
            bad=copy.deepcopy(unary);bad['rows'].pop()
            with self.assertRaises(ValueError):analyze(study,bad,joint)
            bad=copy.deepcopy(unary);bad['study_hash']='wrong'
            with self.assertRaises(ValueError):analyze(study,bad,joint)
            for r in unary['rows']:r['edit_success']='uncertain'
            result=analyze(study,unary,joint);self.assertFalse(result['signal_present']);self.assertTrue(all(r['status'].startswith('abstain') for r in result['rows']))
