"""Author-designed development stories and a frozen multi-seed method schedule."""
import argparse
import copy
import json
from pathlib import Path
import random
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from asset_runtime import fingerprint

STORIES = {
 'ceramics': [
  'A wide establishing shot of a small pottery workshop. Shelves of unglazed bowls and a wooden workbench. No teapot visible. Slow camera pan.',
  'Close-up of a {{prop.color}} glazed ceramic teapot standing on a wooden pottery workbench. The entire teapot remains visible. Slow camera push in.',
  'A potter shapes a small plain clay bowl on a spinning wheel. No teapot visible. Close-up of hands and wet clay.',
  'A potter pours water from a {{prop.color}} glazed ceramic teapot into a plain white cup on a wooden workbench. Close-up, the teapot stays clearly visible.',
  'A wide view of a closed pottery kiln and shelves of raw clay bowls. No teapot visible. Stable camera.',
  'A {{prop.color}} glazed ceramic teapot sits beside a white cup on a wooden table in a pottery workshop. A hand gently rotates the teapot. Close-up.'
 ],
 'greenhouse': [
  'A wide establishing shot of a botanical greenhouse with glass walls and rows of green seedlings. No watering can visible. Slow camera pan.',
  'Close-up of a {{prop.color}} metal watering can on a greenhouse bench beside green seedlings. The entire watering can remains visible. Slow camera push in.',
  'A macro shot of water droplets on green seedling leaves in a greenhouse. No watering can visible. Stable camera.',
  'Close-up of hands watering seedlings with a {{prop.color}} metal watering can in a greenhouse. The entire watering can remains clearly visible.',
  'A wide upward shot of the glass roof and hanging green plants in a greenhouse. No watering can visible. Slow camera movement.',
  'A {{prop.color}} metal watering can is placed on a greenhouse bench beside trays of seedlings. Close-up of hands setting it down, the full can stays visible.'
 ],
 'harbor': [
  'A wide establishing shot of a quiet ferry harbor with white boats and wooden docks. No backpack visible. Slow camera pan.',
  'A {{prop.color}} fabric backpack stands upright on a wooden ferry-terminal bench beside a folded map. The full backpack remains visible. Close-up, slow camera push in.',
  'A close-up of white ferry ropes tied to a metal dock cleat beside the water. No backpack visible. Stable camera.',
  'Close-up of hands opening a {{prop.color}} fabric backpack on a wooden ferry-terminal bench and putting a folded map inside. The backpack remains clearly visible.',
  'A wide shot of a white ferry slowly leaving a quiet harbor. No passengers or backpacks visible. Stable camera.',
  'Close-up of a {{prop.color}} fabric backpack being gently set beside a wooden harbor railing. The full backpack remains visible against distant white boats.'
 ]
}
METHODS = ['full', 'suffix_safe', 'dag', 'attr_dag', 'dair']
SEEDS = [42, 314, 2718]


def build(destination):
    destination = Path(destination)
    if destination.exists(): raise ValueError('choose a fresh plan directory')
    destination.mkdir(parents=True)
    rng = random.Random(20261103)
    blocks = []
    for story, prompts in STORIES.items():
        base = dict(assets=[], edges=[])
        for i, prompt in enumerate(prompts, 1):
            reads = ['prop.color'] if '{{prop.color}}' in prompt else []
            spec = dict(template=prompt+' Clear neutral daylight. Realistic live-action cinematography. Stable natural motion.',
                        reads=reads, values={k:'clean white' for k in reads}, cost=10)
            base['assets'].append(dict(id=f'S{i}.video', scene=f'S{i}', kind='video', spec=spec))
        # Identical topology across stories isolates content variation; not topology generalization.
        base['edges'] = [dict(parent=f'S{i}.video',child=f'S{i+1}.video',kind='order') for i in range(1,6)]
        base['edges'] += [dict(parent='S2.video',child='S4.video',kind='state',keys=['prop.color']),
                          dict(parent='S4.video',child='S6.video',kind='state',keys=['prop.color'])]
        edit = dict(id=f'{story}-blue',expected_revision=1,target='S2.video',key='prop.color',
                    value='bright saturated royal-blue',scope='local')
        for seed in SEEDS:
            manifest = copy.deepcopy(base)
            for i,a in enumerate(manifest['assets']): a['spec']['config'] = dict(seed=seed + 97*i)
            name=f'{story}/seed-{seed}';folder=destination/name;folder.mkdir(parents=True)
            write_json(folder/'manifest.json',manifest);write_json(folder/'edit.json',edit)
            order=METHODS.copy();rng.shuffle(order)
            blocks.append(dict(id=f'{story}-seed-{seed}',story=story,seed=seed,method_order=order,
                               manifest=name+'/manifest.json',edit=name+'/edit.json',
                               manifest_hash=fingerprint(manifest),edit_hash=fingerprint(edit)))
    protocol=dict(schema=1,classification='author-designed development stories; not blind held-out data',
        story_count=3,seed_blocks=9,methods=METHODS,method_order_rng_seed=20261103,
        protocol='initial_complete_then_edit_v1',max_inflight=2,poll_seconds=5,
        warmup_policy='One untimed unmeasured server warmup before the session; never warm selected methods only. Record command/time separately. Cache kept under the same server settings.',
        unit_of_analysis='story, with seed repetitions nested in story; not 45 independent stories',
        expected_affected_assets=['S2.video','S4.video','S6.video'],
        annotation_status='author expectation only; independent review required before quality metrics',
        blocks=blocks)
    protocol['plan_hash']=fingerprint(protocol)
    write_json(destination/'plan.json',protocol)
    return protocol


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);a=p.parse_args()
    r=build(a.output);print(json.dumps(dict(blocks=len(r['blocks']),conditions=len(r['blocks'])*len(r['methods']),plan_hash=r['plan_hash']),indent=2))
