"""Observe part-linked visual markers before comparing them with edit rules."""
import json
import re


MARKER_METHOD = 'union_plus_marker_review'
MARKER_INSTRUCTIONS = (
    'Inspect the four original frames separately, in reading order. Panel numbers: '
    '1 top left, 2 top right, 3 bottom left, 4 bottom right. For the named part, look '
    'for colored markings on the same physical object or housing it is attached to. '
    'Describe what is visible using short color-and-shape phrases, including count '
    'when clear. Do not infer any intended edit or object identity. For each panel '
    'return linked if every visible instance of the part can be linked to a housing '
    'with at least one visible marker. Include all those markers and describe the '
    'visible part-to-housing connection. Return absent if the part is not visible '
    'in a readable panel. Return unclear if the panel is unreadable, a part might '
    'be present, or any visible instance cannot be linked to a clearly marked housing. '
    'For absent and unclear use an empty markers list. Return exactly four panel records.\n'
)
PANEL_FIELDS = ('panel', 'status', 'markers', 'evidence')
MARKER_SCHEMA = {
    'type': 'object',
    'required': ['panels'],
    'additionalProperties': False,
    'properties': {
        'panels': {
            'type': 'array',
            'minItems': 4,
            'maxItems': 4,
            'items': {
                'type': 'object',
                'required': list(PANEL_FIELDS),
                'additionalProperties': False,
                'properties': {
                    'panel': {'enum': [1, 2, 3, 4]},
                    'status': {'enum': ['linked', 'absent', 'unclear']},
                    'markers': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence': {'type': 'string'},
                },
            },
        },
    },
}


def normalize_marker(marker):
    return ' '.join(re.findall(r'[a-z0-9]+', marker.casefold()))


def validate_marker_rules(rules):
    if not isinstance(rules, dict) or set(rules) != {'part', 'target', 'excluded'}:
        raise ValueError('marker rule requires part, target and excluded')
    if not isinstance(rules['part'], str) or not rules['part'].strip():
        raise ValueError('part must be a nonempty string')
    normalized = {}
    for kind in ('target', 'excluded'):
        aliases = rules[kind]
        if (not isinstance(aliases, list) or not aliases
                or not all(isinstance(alias, str) and normalize_marker(alias) for alias in aliases)):
            raise ValueError(f'{kind} must be a nonempty list of marker aliases')
        normalized[kind] = {normalize_marker(alias) for alias in aliases}
    if normalized['target'] & normalized['excluded']:
        raise ValueError('target and excluded aliases overlap')
    return dict(part=rules['part'], **normalized)


def compare_markers(raw, rules):
    answer = json.loads(raw)
    if (not isinstance(answer, dict) or set(answer) != {'panels'}
            or not isinstance(answer['panels'], list) or len(answer['panels']) != 4):
        raise ValueError('marker reply requires four panel records')
    seen, observed = set(), set()
    unclear = False
    for row in answer['panels']:
        if not isinstance(row, dict) or set(row) != set(PANEL_FIELDS):
            raise ValueError('invalid marker panel record')
        panel, status, markers, evidence = (row[key] for key in PANEL_FIELDS)
        if type(panel) is not int or not 1 <= panel <= 4 or panel in seen:
            raise ValueError('marker panels must be numbered 1 through 4 exactly once')
        if (status not in ('linked', 'absent', 'unclear') or not isinstance(evidence, str)
                or not isinstance(markers, list)
                or not all(isinstance(marker, str) and normalize_marker(marker) for marker in markers)):
            raise ValueError('invalid marker observation')
        if status == 'linked':
            if not markers or not evidence.strip():
                raise ValueError('linked panel requires markers and connection evidence')
            observed.update(normalize_marker(marker) for marker in markers)
        elif markers:
            raise ValueError('absent or unclear panels must have an empty markers list')
        seen.add(panel)
        unclear |= status == 'unclear'

    if observed & rules['target']:
        decision, reason = 'match', 'target_marker_observed'
    elif observed and observed <= rules['excluded'] and not unclear:
        decision, reason = 'distinct', 'only_explicitly_excluded_markers_observed'
    else:
        decision, reason = 'unknown', 'identity_not_resolved'
    return dict(decision=decision, reason=reason, observed_markers=sorted(observed),
                panels=answer['panels'])
