"""Generate a small designed diagnostic suite; never label it a held-out benchmark."""
import argparse
import copy
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifacts import write_json


def create_suite(base, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('suite destination already exists')
    destination.mkdir(parents=True)
    cases = []
    def add(name, manifest, edit=None, schedule=None, mode='post'):
        folder = destination / name; folder.mkdir()
        write_json(folder / 'manifest.json', manifest)
        write_json(folder / ('schedule.json' if schedule else 'edit.json'), schedule or edit)
        cases.append(dict(id=name, mode=mode, manifest=f'{name}/manifest.json',
                          input=f'{name}/'+('schedule.json' if schedule else 'edit.json')))
    def event(name, target, key, value, scope='local'):
        return dict(id=name, expected_revision=1, target=target, key=key, value=value, scope=scope)
    # Base comes from the matched state-v6 configuration, never raw result labels.
    add('state_costume', copy.deepcopy(base), event('state-blue', 'S2.video', 'hero.costume', 'bright saturated royal-blue'))
    isolated = copy.deepcopy(base)
    isolated['edges'] = [e for e in isolated['edges'] if e['kind'] != 'state']
    add('local_costume', isolated, event('local-blue', 'S2.video', 'hero.costume', 'bright saturated royal-blue'))
    add('subtitle', copy.deepcopy(base), event('subtitle-update', 'S2.subtitle', 'subtitle.text', 'We must leave now.'))
    global_case = copy.deepcopy(base)
    for a in global_case['assets']:
        if a['kind'] == 'video':
            a['spec']['reads'].append('scene.light')
            a['spec']['values']['scene.light'] = 'clear neutral daylight'
            for phrase in (' in clear daylight', 'Clear daylight.', 'Neutral natural lighting, '):
                a['spec']['template'] = a['spec']['template'].replace(phrase, '')
            a['spec']['template'] += ' Overall illumination: {{scene.light}}.'
    add('global_light', global_case, event('global-light', 'S1.video', 'scene.light', 'warm sunset illumination', 'global'))
    reference = copy.deepcopy(isolated)
    reference['edges'].append(dict(parent='S2.video', child='S4.video', kind='reference'))
    reference['edges'].append(dict(parent='S4.video', child='S6.video', kind='reference'))
    add('reference_change', reference, event('reference-blue', 'S2.video', 'hero.costume', 'bright saturated royal-blue'), mode='reference_plan_only')
    schedule = [dict(trigger=dict(after_submission_intents=2, asset='S2.video', revision=1),
                     edit=event('inflight-black', 'S2.video', 'hero.costume', 'solid black')),
                dict(trigger=dict(asset='S2.video', revision=2),
                     edit=dict(event('inflight-blue', 'S2.video', 'hero.costume', 'bright saturated royal-blue'), expected_revision=2))]
    add('inflight_costume', copy.deepcopy(base), schedule=schedule, mode='dynamic')
    write_json(destination / 'suite.json', dict(classification='designed diagnostic suite; not held out', cases=cases))
    return cases


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--manifest', required=True); p.add_argument('--output', required=True)
    a = p.parse_args(); print(json.dumps(create_suite(json.loads(Path(a.manifest).read_text()), a.output), indent=2))
