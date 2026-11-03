"""Build anonymous fixed-pool review pages and compute descriptive hindsight headroom."""
import argparse
import html
import itertools
import json
from pathlib import Path
import random
import shutil
import sys
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import checksum,write_json,probe_media
from asset_runtime import fingerprint


def page(stage,data,study_hash):
    payload=json.dumps(dict(stage=stage,study_hash=study_hash,items=data),ensure_ascii=False).replace('<','\\u003c')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>候选池盲评</title>
<style>body{max-width:1100px;margin:30px auto;padding:20px;font:16px/1.6 system-ui;color:#172230}video{width:31%;min-width:220px;background:#111}section{border:1px solid #ccd5df;margin:20px 0;padding:16px}label{display:inline-block;margin:8px}button{padding:12px}select,input{padding:7px}summary{cursor:pointer;font-weight:bold}</style>
<h1>候选池探索性盲评</h1><p id="instructions"></p>
<p>评者编号（例如 rater-a；无需姓名）：<input id="rater" required></p><p>分数：1=严重问题，2=明显问题，3=基本可用，4=良好，5=很好。播放默认静音，本轮不评价音频。请完整观看每段视频；视角、动作本身允许不同。若画面无法播放，请停止评分并报告问题。</p>
<div id="forms"></div><button id="export">导出评分 JSON</button><p id="error"></p><script>
const study=PAYLOAD;
const single=study.stage==='individual';
document.querySelector('#instructions').textContent=single?'单镜头阶段：先独立评分，再查看任何组合。修改成功仅判断目标蓝色及该镜头请求是否完成；单镜头质量综合形状、画质、动作自然度。不要比较其他候选。':'整组阶段：最好由未见单镜头评分的另一位评者完成。评价三镜头是否像同一件产品、场景是否兼容及动作逻辑。允许正常视角和构图变化。';
function text(tag,s){const e=document.createElement(tag);e.textContent=s;return e;}
function choice(field,options){const label=document.createElement('label');label.append(text('span',field+' '));const s=document.createElement('select');s.name=field;s.add(new Option('请选择',''));for(const [v,l] of options)s.add(new Option(l,v));s.required=true;label.append(s);return label;}
function video(id,caption){const box=document.createElement('span');box.append(text('small',caption+' '));const v=document.createElement('video');v.src='media/'+id+'.mp4';v.controls=true;v.muted=true;v.preload='none';box.append(v);return box;}
for(const item of study.items){const section=document.createElement('section');section.dataset.id=item.id;const detail=document.createElement('details');detail.append(text('summary',item.id+' / '+item.story));detail.append(text('p',item.contract));
 if(single){detail.append(video(item.id,item.shot));detail.append(text('p',item.prompt));}
 else{for(const c of item.clips)detail.append(video(c.id,c.shot));const ctx=document.createElement('details');ctx.append(text('summary','查看保留的场景镜头（无目标产品身份参考）'));for(const c of item.context)ctx.append(video(c.id,c.shot));detail.append(ctx);}
 detail.append(choice(single?'edit_success':'acceptable',[['yes','是'],['no','否'],['uncertain','不确定']]));detail.append(choice(single?'single_quality':'joint_quality',[1,2,3,4,5].map(x=>[String(x),String(x)])));
 const note=document.createElement('input');note.name='notes';note.placeholder='依据／具体时间点（可选）';detail.append(note);section.append(detail);document.querySelector('#forms').append(section);}
const storageKey='joint-pool:'+study.study_hash+':'+study.stage;
try{const old=JSON.parse(localStorage.getItem(storageKey)||'null');if(old){document.querySelector('#rater').value=old.rater_id||'';for(const sec of document.querySelectorAll('section'))for(const input of sec.querySelectorAll('select,input'))input.value=old.values?.[sec.dataset.id]?.[input.name]||'';}}catch(e){}
function save(){const values={};for(const sec of document.querySelectorAll('section')){values[sec.dataset.id]={};for(const input of sec.querySelectorAll('select,input'))values[sec.dataset.id][input.name]=input.value;}try{localStorage.setItem(storageKey,JSON.stringify({rater_id:document.querySelector('#rater').value,values}));}catch(e){}}
document.addEventListener('change',save);
document.querySelector('#export').onclick=()=>{save();const rater=document.querySelector('#rater').value.trim();if(!rater){document.querySelector('#error').textContent='请填写评者编号';return;}const rows=[];for(const sec of document.querySelectorAll('section')){const row={id:sec.dataset.id};for(const input of sec.querySelectorAll('select,input')){if(input.required&&!input.value){document.querySelector('#error').textContent='尚未完成 '+sec.dataset.id;return;}row[input.name]=input.name.endsWith('quality')?Number(input.value):input.value;}rows.push(row);}const blob=new Blob([JSON.stringify({stage:study.stage,study_hash:study.study_hash,rater_id:rater,scope:'user-entered ratings; viewing completion not instrumented',rows},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=study.stage+'_ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};
</script></html>'''.replace('PAYLOAD',payload)


def build_review(output):
    from research.run_joint_pool import load_plan
    out=Path(output);plan=load_plan(out)
    status=json.loads((out/'status.json').read_text())
    if status['status']!='complete' or status['evidence']!='remote_comfyui_unvalidated_quality':raise ValueError('only complete real-provider pools get playable review pages')
    archive=json.loads((out/'artifacts/revision-1.json').read_text());by={a['asset']:a for a in archive['assets']}
    review=out/'review';media=review/'media';media.mkdir(parents=True,exist_ok=True)
    contracts={s['id']:s['contract'] for s in plan['protocol']['stories']};individual=[];contexts={s:[] for s in contracts}
    private=[];hashes={}
    for j in plan['jobs']:
        a=by[j['id']];source=out/'artifacts'/a['file']
        if not source.resolve().is_relative_to((out/'artifacts').resolve()) or checksum(source)!=a['sha256']:raise ValueError('candidate checksum/path mismatch')
        probe_media(source)
        shutil.copyfile(source,media/(j['blind_id']+'.mp4'));hashes[j['blind_id']]=a['sha256']
        individual.append(dict(id=j['blind_id'],story=j['story'],shot=j['scene'],contract=contracts[j['story']],prompt=j['prompt']))
        private.append(dict(blind_id=j['blind_id'],job=j['id'],seed=j['seed'],sha256=a['sha256']))
    for i,a in enumerate(plan['anchors']):
        alias=f'A{i+1:03d}';src=out/'anchors'/a['story']/(a['shot']+'.mp4');shutil.copyfile(src,media/(alias+'.mp4'))
        hashes[alias]=a['sha256'];contexts[a['story']].append(dict(id=alias,shot=a['shot']))
    rng=random.Random(plan['protocol']['blind_order_seed']);rng.shuffle(individual);combinations=[]
    for story in contracts:
        options=[[j for j in individual if j['story']==story and j['shot']==shot] for shot in plan['protocol']['shots']]
        for group in itertools.product(*options):
            combinations.append(dict(story=story,contract=contracts[story],clips=[dict(id=j['id'],shot=j['shot']) for j in group],context=contexts[story]))
    rng.shuffle(combinations)
    for i,c in enumerate(combinations):c['id']=f'C{i+1:03d}'
    identity=fingerprint(dict(plan=plan['plan_hash'],media=hashes,individual=individual,combinations=combinations))
    study=dict(study_hash=identity,plan_hash=plan['plan_hash'],individual=individual,combinations=combinations,media_hashes=hashes)
    write_json(out/'private/review_key.json',dict(candidates=private,study_hash=identity))
    write_json(review/'study.json',study)
    (review/'individual.html').write_text(page('individual',individual,identity))
    (review/'joint.html').write_text(page('joint',combinations,identity))
    (review/'README.txt').write_text('Extract the entire ZIP. Open individual.html for the individual rater and joint.html for a separate joint rater. Keep the exported rating JSON files. This is a two-story exploratory study, not a formal user study. Do not share plan.json or private/review_key.json with raters before scoring.\n')
    with zipfile.ZipFile(out/'review.zip','w',zipfile.ZIP_DEFLATED) as z:
        for f in review.rglob('*'):
            if f.is_file():z.write(f,'review/'+str(f.relative_to(review)))
    return study


def validate_ratings(document,study,stage):
    expected={x['id'] for x in study['individual' if stage=='individual' else 'combinations']}
    if document.get('stage')!=stage or document.get('study_hash')!=study['study_hash']:raise ValueError('rating stage/study mismatch')
    rows=document.get('rows',[])
    if len(rows)!=len(expected) or {r['id'] for r in rows}!=expected:raise ValueError('missing/duplicate/unknown rating ids')
    if not document.get('rater_id','').strip():raise ValueError('rater id required')
    score='single_quality' if stage=='individual' else 'joint_quality';valid='edit_success' if stage=='individual' else 'acceptable'
    for r in rows:
        if type(r.get(score))!=int or not 1<=r[score]<=5 or r.get(valid) not in ('yes','no','uncertain'):raise ValueError('invalid score/decision')
    return {r['id']:r for r in rows}


def analyze(study,individual,joint):
    unary=validate_ratings(individual,study,'individual');group=validate_ratings(joint,study,'joint');rows=[]
    for story in sorted({c['story'] for c in study['combinations']}):
        options={shot:[c for c in study['individual'] if c['story']==story and c['shot']==shot] for shot in ('S2','S4','S6')}
        selected=[]
        for shot,cs in options.items():
            eligible=[c for c in cs if unary[c['id']]['edit_success']=='yes']
            if not eligible:break
            selected.append(min(eligible,key=lambda c:(-unary[c['id']]['single_quality'],c['id']))['id'])
        combos=[c for c in study['combinations'] if c['story']==story]
        eligible=[c for c in combos if all(unary[x['id']]['edit_success']=='yes' for x in c['clips'])]
        if len(selected)!=3 or not eligible:
            rows.append(dict(story=story,status='abstain_no_edit_valid_combination'));continue
        independent=next(c for c in combos if {x['id'] for x in c['clips']}==set(selected))
        oracle=min(eligible,key=lambda c:(-group[c['id']]['joint_quality'],c['id']))
        a=group[independent['id']];b=group[oracle['id']]
        rows.append(dict(story=story,status='scored',independent=independent['id'],oracle=oracle['id'],
            independent_joint_quality=a['joint_quality'],oracle_joint_quality=b['joint_quality'],
            gap=b['joint_quality']-a['joint_quality'],independent_acceptable=a['acceptable'],oracle_acceptable=b['acceptable']))
    return dict(study_hash=study['study_hash'],rows=rows,
        signal_present=len(rows)==2 and all(r.get('gap',-1)>=1 and r.get('oracle_acceptable')=='yes' for r in rows),
        same_rater=individual['rater_id']==joint['rater_id'],
        interpretation='Descriptive two-story hindsight headroom, not a learned policy result, independent replication or population significance.',
        budget='12 shared new candidate jobs. No adaptive allocation has been tested.',
        next_step='If signal is absent, pause this candidate; if present, obtain independent replication before developing a joint policy.')


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('build');a.add_argument('--output',required=True)
    a=s.add_parser('analyze');a.add_argument('--output',required=True);a.add_argument('--individual',required=True);a.add_argument('--joint',required=True)
    a=p.parse_args();out=Path(a.output)
    if a.command=='build':build_review(out);return
    study=json.loads((out/'review/study.json').read_text());individual=json.loads(Path(a.individual).read_text());joint=json.loads(Path(a.joint).read_text())
    for alias,digest in study['media_hashes'].items():
        if checksum(out/'review/media'/(alias+'.mp4'))!=digest:raise ValueError('review media changed')
    report=analyze(study,individual,joint);write_json(out/'analysis.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
